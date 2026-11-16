"""Persistent Meural artwork delivery and encrypted account sessions."""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from flask import Flask

from app.meural_auth import (
    AuthResult,
    HttpError,
    InvalidAuth,
    MeuralAuthError,
    NetgearAuthenticator,
    request_json,
)
from app.state.settings_store import SettingsStore

API = "https://api.meural.com/v1/"
SESSION_FIELDS: list[dict[str, Any]] = [
    {"name": "meural_access_token", "secret": True},
    {"name": "meural_refresh_token", "secret": True},
] + [
    {"name": name}
    for name in (
        "meural_expires_at",
        "meural_trust_id",
        "meural_email",
        "meural_account_id",
        "meural_devices",
    )
]


def resource_id(value: Any) -> str:
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text):
        raise ValueError("Meural returned an invalid resource id")
    return text


def save_session(
    settings: SettingsStore,
    device_id: str,
    result: AuthResult,
    *,
    email: str,
    account_id: str,
    devices: list[dict[str, Any]],
) -> None:
    current = (settings.get_section("devices") or {}).get(device_id) or {}
    account_id = resource_id(account_id)
    inventory = []
    for device in devices:
        frame = {
            "id": resource_id(device.get("id")),
            "name": str(device.get("alias") or device.get("name") or device["id"])[:100],
        }
        try:
            frame["host"] = str(ipaddress.IPv4Address(device.get("localIp")))
        except (ValueError, TypeError):
            frame["host"] = ""
        inventory.append(frame)
    chosen = next((d for d in inventory if d["id"] == str(current.get("cloud_device_id"))), None)
    if chosen is None and len(inventory) == 1:
        chosen = inventory[0]
    selection: dict[str, Any] = {"cloud_device_id": chosen["id"] if chosen else ""}
    if chosen and (
        not current.get("host")
        or current.get("cloud_device_id") != chosen["id"]
        or current.get("meural_account_id") != account_id
    ):
        selection["host"] = chosen["host"]
    settings.update_for_namespace(
        "devices",
        device_id,
        {
            "meural_access_token": result.access_token,
            "meural_refresh_token": result.refresh_token,
            "meural_expires_at": result.expires_at,
            "meural_trust_id": result.trust_id,
            "meural_email": email,
            "meural_account_id": account_id,
            "meural_devices": inventory,
            **selection,
        },
        [*SESSION_FIELDS, {"name": "cloud_device_id"}, {"name": "host"}],
    )


def clear_session(settings: SettingsStore, device_id: str) -> None:
    keys = [f["name"] + ("_secret" if f.get("secret") else "") for f in SESSION_FIELDS]
    settings.update_item("devices", device_id, {}, remove=keys)


class MeuralCloudClient:
    def __init__(self, settings: SettingsStore, device_id: str) -> None:
        self._settings = settings
        self._device_id = device_id
        self.lock = threading.RLock()
        self._retry_auth_at = 0.0
        self._failed_refresh = ""

    def config(self) -> dict[str, Any]:
        return (self._settings.get_section("devices") or {}).get(self._device_id) or {}

    @property
    def account_id(self) -> str:
        return str(self.config().get("meural_account_id") or "")

    def reset_auth(self) -> None:
        self._retry_auth_at = 0
        self._failed_refresh = ""

    def _token(self, *, rejected: str | None = None) -> str:
        with self.lock:
            cfg = self.config()
            token = str(cfg.get("meural_access_token_secret") or "")
            refresh = str(cfg.get("meural_refresh_token_secret") or "")
            expires = float(cfg.get("meural_expires_at") or 0)
            if token and token != rejected and expires > time.time() + 60:
                return token
            if not refresh:
                raise InvalidAuth("Sign in to Meural in this display's settings")
            if refresh == self._failed_refresh and time.monotonic() < self._retry_auth_at:
                raise InvalidAuth("Meural authentication failed. Wait a minute or sign in again.")
            try:
                result = NetgearAuthenticator(cfg.get("meural_trust_id")).refresh(refresh)
            except MeuralAuthError:
                self._failed_refresh = refresh
                self._retry_auth_at = time.monotonic() + 60
                raise
            self._settings.update_for_namespace(
                "devices",
                self._device_id,
                {
                    "meural_access_token": result.access_token,
                    "meural_refresh_token": result.refresh_token,
                    "meural_expires_at": result.expires_at,
                    "meural_trust_id": result.trust_id,
                },
                SESSION_FIELDS,
            )
            self.reset_auth()
            return result.access_token

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        token = self._token()
        for attempt in range(2):
            headers = {
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "x-meural-api-version": "4",
                "x-meural-source-platform": "web",
                **kwargs.get("headers", {}),
            }
            try:
                result = request_json(method, API + path, **{**kwargs, "headers": headers})
                return result
            except HttpError as exc:
                if exc.status != 401:
                    raise
                if attempt:
                    raise InvalidAuth(
                        "Meural rejected the renewed session. Sign in again."
                    ) from exc
                token = self._token(rejected=token)
        raise InvalidAuth("Sign in to Meural again")

    @staticmethod
    def _data(payload: Any) -> Any:
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def inspect_account(self) -> tuple[str, list[dict[str, Any]]]:
        user = self._data(self.request("GET", "user"))
        return resource_id(user["id"]), list(self.walk("user/devices"))

    def walk(self, path: str) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            payload = self.request("GET", f"{path}?count=100&page={page}")
            rows = self._data(payload)
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("Meural returned an invalid list")
            yield from rows
            if not isinstance(payload, dict) or payload.get("isLast", True):
                return
            page += 1

    def upload(self, jpeg: bytes, name: str) -> str:
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
            f'filename="{name}"\r\nContent-Type: image/jpeg\r\n\r\n'
        ).encode()
        body += jpeg + f"\r\n--{boundary}--\r\n".encode()
        data = self._data(
            self.request(
                "POST",
                "items",
                body=body,
                timeout=90,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
        )
        return resource_id(data.get("id"))

    def find_upload(self, name: str) -> str | None:
        for item in self.walk("user/items"):
            if item.get("name") in (name, name.removesuffix(".jpg")):
                return resource_id(item.get("id"))
        return None

    def create_gallery(self, name: str) -> str:
        data = self._data(self.request("POST", "galleries", json_data={"name": name}))
        return resource_id(data.get("id"))

    def add_to_gallery(self, gallery: str, item: str) -> None:
        path = f"galleries/{resource_id(gallery)}/items/{resource_id(item)}"
        try:
            self.request("POST", path)
        except HttpError as exc:
            if exc.status != 409:
                raise
            data = self._data(self.request("GET", f"galleries/{resource_id(gallery)}"))
            if item not in {str(i) for i in data.get("itemIds", [])}:
                raise

    def _delete(self, path: str) -> None:
        try:
            self.request("DELETE", path)
        except HttpError as exc:
            if exc.status != 404:
                raise

    def remove_from_gallery(self, gallery: str, item: str) -> None:
        self._delete(f"galleries/{resource_id(gallery)}/items/{resource_id(item)}")

    def delete_item(self, item: str) -> None:
        self._delete(f"items/{resource_id(item)}")

    def load_gallery(self, target: str, gallery: str) -> None:
        self.request("POST", f"devices/{resource_id(target)}/galleries/{resource_id(gallery)}")


def get_cloud_client(app: Flask, device_id: str) -> MeuralCloudClient:
    clients: dict[str, MeuralCloudClient] = app.config.setdefault("MEURAL_CLOUD_CLIENTS", {})
    if device_id not in clients:
        clients[device_id] = MeuralCloudClient(app.config["SETTINGS_STORE"], device_id)
    return clients[device_id]


class CloudDelivery:
    """Keep an app-created playlist on one frame, with only its latest image."""

    def __init__(
        self,
        path: Path,
        client: Any,
        name: str,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        self.path, self.client, self.name, self.cancelled = path, client, name, cancelled

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(self.path)

    def deliver(self, jpeg: bytes, key: str, target: str) -> bool:
        target = resource_id(target)
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            state = {}
        scope = f"{self.client.account_id}:{target}"
        if not isinstance(state, dict) or state.get("scope") != scope:
            state = {"scope": scope, "owner": uuid.uuid4().hex[:12], "owned": []}
        if self.cancelled():
            return False
        if state.get("last_key") == key:
            self._cleanup(state)
            return False
        gallery = state.get("gallery_id")
        if not gallery:
            gallery = self.client.create_gallery(f"Tesserae - {self.name}")
            state["gallery_id"] = gallery
            self._save(state)
        if self.cancelled():
            return False
        if state.get("pending_key") != key:
            state.update(pending_key=key, pending_item=None, upload_started=False)
            self._save(state)
        item = state.get("pending_item")
        if not item:
            filename = f"tesserae-{state['owner']}-{uuid.uuid5(uuid.NAMESPACE_URL, key).hex}.jpg"
            if state.get("upload_started"):
                item = self.client.find_upload(filename)
            if not item:
                state["upload_started"] = True
                self._save(state)
                item = self.client.upload(jpeg, filename)
            state["pending_item"] = item
            if item not in state["owned"]:
                state["owned"].append(item)
            self._save(state)
        if self.cancelled():
            return False
        self.client.add_to_gallery(gallery, item)
        for previous in state["owned"]:
            if self.cancelled():
                return False
            if previous != item:
                self.client.remove_from_gallery(gallery, previous)
        if self.cancelled():
            return False
        self.client.load_gallery(target, gallery)
        state.update(
            last_key=key,
            current_item=item,
            pending_key=None,
            pending_item=None,
            upload_started=False,
        )
        self._save(state)
        self._cleanup(state)
        return True

    def _cleanup(self, state: dict[str, Any]) -> None:
        for old in list(state["owned"][:-2]):
            if self.cancelled():
                return
            self.client.delete_item(old)
            state["owned"].remove(old)
            self._save(state)
