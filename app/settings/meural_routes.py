"""Netgear sign-in and frame selection for Meural displays."""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from flask import Response, abort, current_app, jsonify, redirect, render_template, request, url_for
from werkzeug.wrappers import Response as RedirectResponse

from app import meural_auth
from app.device_loader import Device
from app.meural import MeuralError, release_sleep
from app.meural_cloud import (
    API,
    clear_session,
    delivery_for_device,
    get_cloud_client,
    resource_id,
    save_session,
    save_tokens,
)

from ._shared import bp, devices, rebuild_transport_fn, settings_store


def _device(instance_id: str) -> Device:
    device = devices().devices.get(instance_id)
    if device is None or device.kind_of != "meural":
        abort(404)
    return device


def _return_url(instance_id: str) -> str:
    return url_for("auth.device_page", instance_id=instance_id, _anchor="connection")


def _stop_publisher() -> None:
    publisher = current_app.config.get("MEURAL_PUBLISHER")
    if publisher is not None:
        publisher.stop()


def _config(instance_id: str) -> dict[str, Any]:
    return (settings_store().get_section("devices") or {}).get(instance_id) or {}


@contextmanager
def _changing_frame() -> Iterator[None]:
    _stop_publisher()
    try:
        with current_app.config.setdefault("MEURAL_IO_LOCK", threading.Lock()):
            yield
    finally:
        rebuild_transport_fn()()


def _release_native(device: Device) -> None:
    """Caller holds the delivery lock and keeps credentials until restoration succeeds."""
    settings_store().update_item("devices", device.id, {"meural_management_paused": True})
    delivery = delivery_for_device(current_app, device)
    try:
        release_sleep(device, str(_config(device.id).get("host") or ""))
        if delivery is not None:
            delivery[0].release(delivery[1])
    except (
        meural_auth.MeuralAuthError,
        meural_auth.HttpError,
        MeuralError,
        OSError,
        ValueError,
    ) as exc:
        abort(
            400,
            description=f"Could not restore Meural playback: {exc}. Saved settings were kept. Try again.",
        )


@bp.route("/settings/devices/<instance_id>/meural/login", methods=["GET", "POST"])
def meural_login(instance_id: str) -> Response | tuple[Response, int]:
    device = _device(instance_id)
    client = get_cloud_client(current_app, instance_id)
    sessions = current_app.config.setdefault("MEURAL_LOGIN_SESSIONS", {})
    if request.method == "GET":
        nonce = secrets.token_urlsafe(24)
        cfg = _config(instance_id)
        with client.lock:
            sessions[instance_id] = {
                "state": secrets.token_urlsafe(32),
                "trust_id": cfg.get("meural_trust_id") or str(uuid.uuid4()),
                "expires_at": time.monotonic() + 600,
            }
            pending = dict(sessions[instance_id])
        response = current_app.make_response(
            render_template(
                "meural_login.html",
                nonce=nonce,
                device_name=device.name,
                return_url=_return_url(instance_id),
                auth_config={
                    "state": pending["state"],
                    "trust_id": pending["trust_id"],
                    "email": cfg.get("meural_email", ""),
                    "cognito_client_id": meural_auth.COGNITO_CLIENT_ID,
                    "cognito_url": meural_auth.COGNITO_URL,
                    "response_keys": meural_auth.RESPONSE_KEY_MAP,
                    "password_exclusions": meural_auth.PASSWORD_CHALLENGE_EXCLUSION_KEYWORDS,
                    "migration_keywords": meural_auth.USER_MIGRATION_KEYWORDS,
                    "waf_keywords": meural_auth.WAF_ERROR_KEYWORDS,
                    "waf_page_keywords": meural_auth.WAF_BLOCK_PAGE_KEYWORDS,
                },
            )
        )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                    f"connect-src 'self' {meural_auth.COGNITO_URL}; "
                    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
                ),
            }
        )
        return response
    if not request.is_json or request.content_length is None or request.content_length > 16384:
        abort(400)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {
        "state",
        "trust_id",
        "email",
        "cognito_access_token",
    }:
        abort(400)
    email, token = data["email"], data["cognito_access_token"]
    if not isinstance(email, str) or not 3 <= len(email.strip()) <= 320 or "@" not in email:
        abort(400)
    if not isinstance(token, str) or not 100 <= len(token) <= 8192:
        abort(400)
    with client.lock:
        pending = sessions.get(instance_id)
        if not pending or pending.get("consumed") or pending["expires_at"] <= time.monotonic():
            abort(400)
        if not isinstance(data["state"], str) or not secrets.compare_digest(
            data["state"], pending["state"]
        ):
            abort(400)
        if data["trust_id"] != pending["trust_id"]:
            abort(400)
        pending["consumed"] = True
    try:
        result = meural_auth.NetgearAuthenticator(pending["trust_id"]).exchange_cognito_token(token)
        headers = {
            "Authorization": f"Token {result.access_token}",
            "Accept": "application/json",
            "x-meural-api-version": "4",
            "x-meural-source-platform": "web",
        }
        user = meural_auth.request_json("GET", API + "user", headers=headers)
        if not isinstance(user, dict):
            raise ValueError("Meural returned an invalid account")
        user = user.get("data", user)
        if not isinstance(user, dict):
            raise ValueError("Meural returned an invalid account")
        frames = []
        page = 1
        while True:
            inventory = meural_auth.request_json(
                "GET", API + f"user/devices?count=100&page={page}", headers=headers
            )
            if not isinstance(inventory, dict):
                raise ValueError("Meural returned an invalid frame list")
            rows = inventory.get("data", [])
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("Meural returned an invalid frame list")
            frames.extend(rows)
            if inventory.get("isLast", True):
                break
            page += 1
        account_id = resource_id(user.get("id"))
        with _changing_frame(), client.lock:
            if (
                sessions.get(instance_id) is not pending
                or devices().devices.get(instance_id) is not device
                or pending["expires_at"] <= time.monotonic()
            ):
                abort(400)
            sessions.pop(instance_id)
            if str(_config(instance_id).get("meural_account_id")) == account_id:
                # Reauthentication must also recover frames whose old tokens expired.
                save_tokens(settings_store(), instance_id, result)
            _release_native(device)
            save_session(
                settings_store(),
                instance_id,
                result,
                email=email.strip(),
                account_id=account_id,
                devices=frames,
            )
            client.reset_auth()
    except (meural_auth.MeuralAuthError, meural_auth.HttpError, ValueError) as exc:
        with client.lock:
            if sessions.get(instance_id) is pending:
                sessions.pop(instance_id)
        return jsonify(error=f"{exc} Reload this page to start again."), 400
    return jsonify(status="ok", return_url=_return_url(instance_id))


@bp.post("/settings/devices/<instance_id>/meural/logout")
def meural_logout(instance_id: str) -> RedirectResponse:
    device = _device(instance_id)
    client = get_cloud_client(current_app, instance_id)
    with _changing_frame(), client.lock:
        current_app.config.get("MEURAL_LOGIN_SESSIONS", {}).pop(instance_id, None)
        settings_store().update_item("devices", instance_id, {"meural_management_paused": True})
        _release_native(device)
        clear_session(settings_store(), instance_id)
        client.reset_auth()
    return redirect(_return_url(instance_id))


@bp.post("/settings/devices/<instance_id>/meural/frame")
def meural_select_frame(instance_id: str) -> RedirectResponse:
    device = _device(instance_id)
    client = get_cloud_client(current_app, instance_id)
    with _changing_frame(), client.lock:
        cfg = _config(instance_id)
        selected = request.form.get("cloud_device_id")
        frame = next((f for f in cfg.get("meural_devices", []) if f["id"] == selected), None)
        if frame is None:
            abort(400)
        if str(cfg.get("cloud_device_id")) != selected:
            _release_native(device)
        settings_store().update_item(
            "devices",
            instance_id,
            {
                "cloud_device_id": selected,
                "host": frame.get("host", ""),
                "meural_management_paused": False,
            },
        )
    return redirect(_return_url(instance_id))


@bp.post("/settings/devices/<instance_id>/meural/manage")
def meural_manage(instance_id: str) -> RedirectResponse:
    device = _device(instance_id)
    action = request.form.get("action")
    if action not in ("manage", "release"):
        abort(400)
    with _changing_frame(), get_cloud_client(current_app, instance_id).lock:
        settings_store().update_item(
            "devices",
            instance_id,
            {"meural_management_paused": action == "release"},
        )
        if action == "release":
            _release_native(device)
    return redirect(_return_url(instance_id))
