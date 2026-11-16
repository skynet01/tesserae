"""Authenticated Netgear sign-in, token storage and frame selection."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

import pytest

from app import device_service
from app.main import REPO_ROOT, create_app
from app.meural_auth import AuthResult


@pytest.fixture
def app(tmp_path):
    app = create_app(testing=True, data_root=tmp_path)
    result = device_service.create_instance(
        devices=app.config["DEVICE_REGISTRY"],
        renderers=app.config["RENDERER_REGISTRY"],
        data_root=app.config["DATA_ROOT"],
        instance_id="lounge",
        kind_id="meural",
        name="Lounge",
    )
    assert result.ok
    return app


def begin(client):
    response = client.get("/settings/devices/lounge/meural/login")
    assert response.status_code == 200
    match = re.search(
        r'<script[^>]*id="meural-auth-config"[^>]*>(.*?)</script>',
        response.get_data(as_text=True),
        re.S,
    )
    return response, json.loads(match.group(1))


def payload(config):
    return {
        "state": config["state"],
        "trust_id": config["trust_id"],
        "email": "owner@example.com",
        "cognito_access_token": "c" * 120,
    }


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(
        "app.meural_auth.NetgearAuthenticator.exchange_cognito_token",
        lambda self, token: AuthResult(
            "private-access", "private-refresh", time.time() + 3600, self.trust_id
        ),
    )

    def request(method, url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Token private-access"
        if "user/devices" in url:
            return {"data": [{"id": 16036, "alias": "Lounge Art", "localIp": "192.0.2.10"}]}
        return {"data": {"id": 17}}

    monkeypatch.setattr("app.meural_auth.request_json", request)


def test_signin_page_is_private_and_has_no_saved_secrets(app):
    response, config = begin(app.test_client())
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "cognito-idp.eu-west-1.amazonaws.com" in config["cognito_url"]
    assert len(config["state"]) >= 32
    assert "access_token" not in config


@pytest.mark.parametrize("scenario", ["password-mfa", "otp-first", "migration"])
def test_browser_challenges_keep_passwords_and_codes_at_netgear(app, scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the browser sign-in boundary test")
    _, config = begin(app.test_client())
    completed = subprocess.run(
        [
            node,
            str(REPO_ROOT / "tests/fixtures/meural_login_flow.cjs"),
            str(REPO_ROOT / "static/meural-login.js"),
            scenario,
        ],
        input=json.dumps(config),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"signed_in": True, "callbacks": 1}


def test_callback_saves_an_encrypted_session_and_selects_the_only_frame(app, provider):
    client = app.test_client()
    _, config = begin(client)
    response = client.post("/settings/devices/lounge/meural/login", json=payload(config))
    assert response.status_code == 200
    assert response.json["return_url"].endswith("/settings/devices/lounge#connection")
    cfg = app.config["SETTINGS_STORE"].get_section("devices")["lounge"]
    assert cfg["cloud_device_id"] == "16036"
    assert cfg["meural_refresh_token_secret"] == "private-refresh"
    page = client.get("/settings/devices/lounge").get_data(as_text=True)
    assert "Lounge Art" in page and "owner@example.com" in page
    assert "private-access" not in page and "private-refresh" not in page


def test_callback_is_single_use(app, provider):
    client = app.test_client()
    _, config = begin(client)
    assert (
        client.post("/settings/devices/lounge/meural/login", json=payload(config)).status_code
        == 200
    )
    assert (
        client.post("/settings/devices/lounge/meural/login", json=payload(config)).status_code
        == 400
    )


@pytest.mark.parametrize("invalid_endpoint", ["user", "user/devices"])
def test_invalid_provider_response_does_not_save_partial_session(
    app, provider, monkeypatch, invalid_endpoint
):
    from app import meural_auth

    original = meural_auth.request_json

    def request(method, url, **kwargs):
        if url.split("/v1/", 1)[1].split("?", 1)[0] == invalid_endpoint:
            return []
        return original(method, url, **kwargs)

    monkeypatch.setattr(meural_auth, "request_json", request)
    client = app.test_client()
    _, config = begin(client)
    assert (
        client.post("/settings/devices/lounge/meural/login", json=payload(config)).status_code
        == 400
    )
    assert not (app.config["SETTINGS_STORE"].get_section("devices").get("lounge") or {}).get(
        "meural_refresh_token_secret"
    )


@pytest.mark.parametrize("change", ["state", "trust_id", "cognito_access_token"])
def test_callback_rejects_wrong_state_identity_or_token(app, provider, change):
    client = app.test_client()
    _, config = begin(client)
    data = payload(config)
    data[change] = "wrong"
    assert client.post("/settings/devices/lounge/meural/login", json=data).status_code == 400


def test_signout_during_token_exchange_cannot_restore_the_session(app, provider, monkeypatch):
    client = app.test_client()
    _, config = begin(client)

    def exchange(self, token):
        assert client.post("/settings/devices/lounge/meural/logout").status_code == 302
        return AuthResult("private-access", "late-refresh", time.time() + 3600, self.trust_id)

    monkeypatch.setattr("app.meural_auth.NetgearAuthenticator.exchange_cognito_token", exchange)
    response = client.post("/settings/devices/lounge/meural/login", json=payload(config))
    assert response.status_code == 400
    assert not (app.config["SETTINGS_STORE"].get_section("devices").get("lounge") or {}).get(
        "meural_refresh_token_secret"
    )


def test_cross_origin_signin_result_is_rejected(app, provider):
    client = app.test_client()
    _, config = begin(client)
    response = client.post(
        "/settings/devices/lounge/meural/login",
        json=payload(config),
        headers={"Origin": "https://unrelated.example"},
    )
    assert response.status_code == 403


def test_signout_clears_tokens_without_deleting_cloud_artwork(app, provider):
    client = app.test_client()
    _, config = begin(client)
    assert (
        client.post("/settings/devices/lounge/meural/login", json=payload(config)).status_code
        == 200
    )
    assert client.post("/settings/devices/lounge/meural/logout").status_code == 302
    cfg = app.config["SETTINGS_STORE"].get_section("devices")["lounge"]
    assert not cfg.get("meural_refresh_token_secret")
    assert cfg["cloud_device_id"] == "16036"


def test_frame_picker_rejects_a_frame_outside_the_linked_account(app, provider):
    client = app.test_client()
    _, config = begin(client)
    client.post("/settings/devices/lounge/meural/login", json=payload(config))
    response = client.post("/settings/devices/lounge/meural/frame", data={"cloud_device_id": "999"})
    assert response.status_code == 400
    assert (
        app.config["SETTINGS_STORE"].get_section("devices")["lounge"]["cloud_device_id"] == "16036"
    )


def held_frame(app, client):
    from app.meural_cloud import delivery_for_device

    _, config = begin(client)
    assert (
        client.post("/settings/devices/lounge/meural/login", json=payload(config)).status_code
        == 200
    )
    delivery, target = delivery_for_device(app, app.config["DEVICE_REGISTRY"].devices["lounge"])
    original = {
        "gallery_id": "27",
        "item_id": "28",
        "settings": {"galleryRotation": True, "schedulerEnabled": False},
    }
    delivery.path.parent.mkdir(parents=True, exist_ok=True)
    delivery.path.write_text(
        json.dumps(
            {
                "scope": f"17:{target}",
                "owned": [],
                "original": original,
                "paused": True,
                "last_key": "current",
            }
        )
    )
    return delivery, original


def test_resume_native_playback_stops_delivery_and_restores_saved_settings(
    app, provider, monkeypatch
):
    client = app.test_client()
    delivery, original = held_frame(app, client)
    restored = []
    monkeypatch.setattr(
        "app.meural_cloud.MeuralCloudClient.restore_playback",
        lambda self, target, state: restored.append((target, state)),
    )
    response = client.post("/settings/devices/lounge/meural/manage", data={"action": "release"})
    assert response.status_code == 302
    assert restored == [("16036", original)]
    assert app.config["SETTINGS_STORE"].get_section("devices")["lounge"]["meural_management_paused"]
    page = client.get("/settings/devices/lounge").get_data(as_text=True)
    assert "creates no Meural galleries" in page
    assert "Let Tesserae manage" in page
    assert not json.loads(delivery.path.read_text()).get("original")
    assert (
        client.post("/settings/devices/lounge/meural/manage", data={"action": "manage"}).status_code
        == 302
    )
    assert not app.config["SETTINGS_STORE"].get_section("devices")["lounge"][
        "meural_management_paused"
    ]


def test_signout_restores_before_removing_credentials(app, provider, monkeypatch):
    client = app.test_client()
    _, original = held_frame(app, client)
    restored = []

    def restore(self, target, state):
        assert self.config()["meural_refresh_token_secret"] == "private-refresh"
        restored.append((target, state))

    monkeypatch.setattr("app.meural_cloud.MeuralCloudClient.restore_playback", restore)
    assert client.post("/settings/devices/lounge/meural/logout").status_code == 302
    assert restored == [("16036", original)]
    assert (
        not app.config["SETTINGS_STORE"]
        .get_section("devices")["lounge"]
        .get("meural_refresh_token_secret")
    )


def test_failed_restore_keeps_credentials_and_blocks_delivery_for_retry(app, provider, monkeypatch):
    from app.meural_auth import CannotConnect

    client = app.test_client()
    delivery, original = held_frame(app, client)

    def offline(*args):
        raise CannotConnect("offline")

    monkeypatch.setattr("app.meural_cloud.MeuralCloudClient.restore_playback", offline)
    assert client.post("/settings/devices/lounge/meural/logout").status_code == 400
    cfg = app.config["SETTINGS_STORE"].get_section("devices")["lounge"]
    assert cfg["meural_refresh_token_secret"] == "private-refresh"
    assert cfg["meural_management_paused"]
    assert json.loads(delivery.path.read_text())["original"] == original


def test_deleting_managed_frame_restores_native_playback_first(app, provider, monkeypatch):
    client = app.test_client()
    _, original = held_frame(app, client)
    restored = []
    monkeypatch.setattr(
        "app.meural_cloud.MeuralCloudClient.restore_playback",
        lambda self, target, state: restored.append((target, state)),
    )
    assert client.post("/settings/devices/lounge/delete").status_code == 302
    assert restored == [("16036", original)]
    assert "lounge" not in app.config["DEVICE_REGISTRY"].devices


def test_reauthentication_uses_new_tokens_to_restore_an_expired_session(app, provider, monkeypatch):
    from app.meural_auth import AuthResult
    from app.meural_cloud import save_tokens

    client = app.test_client()
    held_frame(app, client)
    save_tokens(
        app.config["SETTINGS_STORE"],
        "lounge",
        AuthResult("expired", "expired-refresh", 1, "trusted"),
    )
    restored = []

    def restore(self, target, original):
        assert self.config()["meural_access_token_secret"] == "private-access"
        restored.append(target)

    monkeypatch.setattr("app.meural_cloud.MeuralCloudClient.restore_playback", restore)
    _, config = begin(client)
    assert (
        client.post("/settings/devices/lounge/meural/login", json=payload(config)).status_code
        == 200
    )
    assert restored == ["16036"]


@pytest.mark.parametrize("owned_sleep", [True, False])
def test_release_wakes_only_a_frame_tesserae_put_to_sleep(app, provider, monkeypatch, owned_sleep):
    from pathlib import Path

    client = app.test_client()
    held_frame(app, client)
    device = app.config["DEVICE_REGISTRY"].devices["lounge"]
    sleep = Path(device.data_dir) / "meural_sleep.json"
    if owned_sleep:
        sleep.write_text(json.dumps({"host": "192.0.2.10"}))
    wake = []

    class Frame:
        def __init__(self, host):
            assert owned_sleep
            assert host == "192.0.2.10"

        def is_sleeping(self):
            return True

        def set_sleep(self, sleeping):
            wake.append(sleeping)

    monkeypatch.setattr("app.meural.MeuralClient", Frame)
    monkeypatch.setattr("app.meural_cloud.MeuralCloudClient.restore_playback", lambda *a: None)
    assert (
        client.post(
            "/settings/devices/lounge/meural/manage", data={"action": "release"}
        ).status_code
        == 302
    )
    assert wake == ([False] if owned_sleep else [])
    assert not sleep.exists()
