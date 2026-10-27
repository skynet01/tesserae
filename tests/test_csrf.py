"""App-wide cookie request provenance, including real proxy middleware."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from flask import Flask

from app import auth
from app.main import create_app


@pytest.fixture
def app(tmp_path: Path, monkeypatch) -> Flask:
    monkeypatch.setenv("TESSERAE_SECRET_KEY", "00" * 32)
    app = create_app(testing=True, data_root=tmp_path)
    app.config["CSRF_TEST_WRITES"] = []

    @app.route("/_csrf_probe/<item>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def mutate(item: str):
        app.config["CSRF_TEST_WRITES"].append(item)
        return "", 204

    return app


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "unknown"},
        {"Sec-Fetch-Site": ""},
        {"Origin": "http://localhost:9999"},
        {"Referer": "http://localhost:9999/form"},
        {"Origin": "https://localhost"},
        {"Origin": "http://elsewhere.test"},
        {"Origin": "null"},
        {"Origin": "broken"},
        {"Origin": "http://localhost:bad"},
        {"Origin": "http://localhost.attacker.test"},
        {"Origin": "http://localhost", "Sec-Fetch-Site": "same-site"},
        {"Origin": "null", "Referer": "http://localhost/form"},
    ],
)
def test_cross_origin_cookie_mutations_are_rejected(app, method, headers) -> None:
    client = app.test_client()
    # Presence matters, even when Flask cannot decode the cookie.
    client.set_cookie("session", "invalid-session")
    response = client.open("/_csrf_probe/one", method=method, headers=headers)
    assert response.status_code == 403
    assert "cross-origin" in response.text
    assert app.config["CSRF_TEST_WRITES"] == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
        {"Origin": "http://localhost"},
        {"Origin": "http://LOCALHOST:80"},
        {"Referer": "http://localhost/settings?tab=server"},
        {},  # Legacy browsers with every provenance header absent are allowed.
    ],
)
def test_same_origin_and_legacy_cookie_posts_are_accepted(app, headers) -> None:
    client = app.test_client()
    with client.session_transaction() as session:
        session["authed"] = True
    assert client.post("/_csrf_probe/one", headers=headers).status_code == 204
    assert app.config["CSRF_TEST_WRITES"] == ["one"]


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_are_not_blocked(app, method) -> None:
    client = app.test_client()
    client.set_cookie("session", "test")
    assert (
        client.open(
            "/_csrf_probe/one", method=method, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        != 403
    )


def test_cookie_name_is_configurable(app) -> None:
    app.config["SESSION_COOKIE_NAME"] = "tesserae-session"
    client = app.test_client()
    client.set_cookie("tesserae-session", "test")
    assert (
        client.post("/_csrf_probe/one", headers={"Origin": "http://localhost:9999"}).status_code
        == 403
    )


@pytest.mark.parametrize(
    "headers", [{"Sec-Fetch-Site": "same-site"}, {"Origin": "http://localhost:9999"}]
)
def test_logged_in_cross_port_form_cannot_disable_password(app, headers) -> None:
    store = app.config["SETTINGS_STORE"]
    auth.set_password(store, "csrf-test-password")
    auth.install_gate(app, store)
    client = app.test_client()
    with client.session_transaction() as session:
        session["authed"] = True
    path = "/settings/system/auth/disable"
    assert client.post(path, data={"confirmed": "1"}, headers=headers).status_code == 403
    assert auth.password_required(store)
    assert (
        client.post(
            path, data={"confirmed": "1"}, headers={"Origin": "http://localhost"}
        ).status_code
        == 302
    )
    assert not auth.password_required(store)


@pytest.mark.parametrize("source_header", ["Origin", "Referer"])
def test_ingress_forwarded_origin_is_accepted_but_another_port_is_rejected(
    app, source_header
) -> None:
    app.config["HA_INGRESS_MODE"] = True
    client = app.test_client()
    client.set_cookie("session", "test")
    headers = {
        "X-Forwarded-Host": "homeassistant.local",
        "X-Forwarded-Port": "8123",
        "X-Forwarded-Proto": "https",
        "X-Ingress-Path": "/api/hassio_ingress/test-prefix",
        source_header: "https://homeassistant.local:8123",
    }
    path = "/api/hassio_ingress/test-prefix/_csrf_probe/one"
    assert client.post(path, headers=headers).status_code == 204
    headers[source_header] = "https://homeassistant.local:9999"
    assert client.post(path, headers=headers).status_code == 403


def test_public_url_override_is_the_effective_origin(app) -> None:
    app.config["SETTINGS_STORE"].patch_section("app", {"public_url": "https://paper.test:8443"})
    client = app.test_client()
    client.set_cookie("session", "test")
    assert (
        client.post("/_csrf_probe/one", headers={"Origin": "https://paper.test:8443"}).status_code
        == 204
    )
    assert (
        client.post("/_csrf_probe/one", headers={"Origin": "http://localhost"}).status_code == 403
    )


def test_rejections_are_logged_once_per_route_without_request_secrets(app, caplog) -> None:
    client = app.test_client()
    client.set_cookie("session", "test")
    with caplog.at_level(logging.WARNING, logger="app.csrf"):
        for item in ("one", "two"):
            assert (
                client.post(
                    f"/_csrf_probe/{item}",
                    headers={"Referer": "https://elsewhere.test/?token=private"},
                ).status_code
                == 403
            )
    records = [record for record in caplog.records if record.name == "app.csrf"]
    assert len(records) == 1
    assert "/_csrf_probe/<item>" in records[0].message
    assert "private" not in records[0].message


@pytest.mark.parametrize(
    "path,body,headers,status",
    [
        ("/api/mcp/pages", {}, {}, 200),  # Direct loopback MCP auth.
        (
            "/api/mcp/pages",
            {},
            {"Authorization": "Bearer mcp-test", "X-Forwarded-For": "10.0.0.2"},
            200,
        ),
        ("/api/v1/push", {}, {"Authorization": "Bearer webhook-test"}, 400),
        ("/api/v1/device/probe/status", {}, {"Authorization": "Bearer bad-token"}, 401),
        ("/api/v1/device/register", {}, {}, 400),
    ],
)
def test_cookie_free_api_clients_reach_their_existing_auth_and_validation(
    app, path, body, headers, status
) -> None:
    app.config["SETTINGS_STORE"].patch_section(
        "app", {"mcp_token_secret": "mcp-test", "webhook_token_secret": "webhook-test"}
    )
    headers = {**headers, "Sec-Fetch-Site": "cross-site", "Origin": "http://elsewhere.test"}
    response = app.test_client().post(path, json=body, headers=headers)
    assert response.status_code == status


def test_device_registration_heartbeat_frame_and_ota_report_without_cookie(app) -> None:
    client = app.test_client()
    headers = {"Origin": "http://elsewhere.test", "Sec-Fetch-Site": "cross-site"}
    code = app.config["PAIRING_STORE"].issue(note="csrf-test").code
    response = client.post(
        "/api/v1/device/register",
        headers={**headers, "X-Pairing-Code": code},
        json={"device_id": "csrf-panel", "kind": "pico_bin_client"},
    )
    assert response.status_code == 201
    token = response.json["device_token"]
    headers["Authorization"] = f"Bearer {token}"
    response = client.post(
        "/api/v1/device/csrf-panel/status",
        headers=headers,
        json={"battery_pct": 80, "ota": {"schema": 1, "phase": "confirmed", "target_fw": "1.0"}},
    )
    assert response.status_code == 200
    assert client.get("/api/v1/device/csrf-panel/frame", headers=headers).status_code == 204
