"""Meural authentication and persistent cloud transport contracts."""

from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from app.meural_auth import AuthenticationBlocked, AuthResult, InvalidAuth, NetgearAuthenticator
from app.meural_cloud import CloudDelivery, MeuralCloudClient, save_session
from app.secret_box import SecretBox
from app.state.settings_store import SettingsStore


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


def test_exchanges_browser_cognito_result_for_meural_tokens(monkeypatch):
    requests = []
    replies = [
        {"data": {"authorizationCode": "one-use-code"}},
        {"data": {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}},
    ]

    def urlopen(request, timeout):
        requests.append(request)
        return response(replies.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = NetgearAuthenticator("trusted-install").exchange_cognito_token("browser-token")
    assert result.access_token == "access"
    assert result.refresh_token == "refresh"
    assert result.trust_id == "trusted-install"
    assert result.expires_at > time.time() + 3500
    assert requests[0].get_header("Authorization") == "Bearer browser-token"
    assert requests[0].full_url.startswith("https://accounts2.netgear.com/api/oauth/authorize?")
    assert requests[1].full_url == "https://accounts2.netgear.com/api/oauth/token?code=one-use-code"


def test_refresh_keeps_existing_refresh_token_when_not_rotated(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return response({"accessToken": "renewed", "expiresIn": 1800})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = NetgearAuthenticator("trusted-install").refresh("old-refresh")
    assert result.access_token == "renewed"
    assert result.refresh_token == "old-refresh"
    assert requests[0].full_url == "https://accounts2.netgear.com/api/getAccessToken"
    assert requests[0].get_header("Authorization") == "Bearer old-refresh"
    assert requests[0].get_header("Appkey") == "3ui6nklcaqoij8inrkm06gfk4s"


@pytest.mark.parametrize(
    "status,body,error",
    [
        (403, b"<html>CloudFront request blocked</html>", AuthenticationBlocked),
        (401, b'{"message":"expired"}', InvalidAuth),
    ],
)
def test_refresh_errors_are_classified_without_exposing_tokens(monkeypatch, status, body, error):
    handle = io.BytesIO(body)

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "failed", {}, handle)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(error) as caught:
        NetgearAuthenticator().refresh("private-refresh-token")
    assert "private-refresh-token" not in str(caught.value)
    assert handle.closed


def test_incomplete_oauth_result_is_not_accepted(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: response({"access_token": "a"}))
    with pytest.raises(InvalidAuth):
        NetgearAuthenticator().refresh("")


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "settings.json", secret_box=SecretBox(b"x" * 32))


def session(store, *, expiry=None):
    save_session(
        store,
        "lounge",
        AuthResult("access", "refresh", expiry or time.time() + 3600, "trusted"),
        email="owner@example.com",
        account_id="17",
        devices=[{"id": 16036, "alias": "Lounge Art", "localIp": "192.0.2.10"}],
    )


def test_session_is_encrypted_and_does_not_store_password(store, tmp_path):
    session(store)
    raw = (tmp_path / "settings.json").read_text()
    assert '"access"' not in raw and '"refresh"' not in raw
    cfg = store.get_section("devices")["lounge"]
    assert cfg["meural_access_token_secret"] == "access"
    assert cfg["meural_refresh_token_secret"] == "refresh"
    assert cfg["cloud_device_id"] == "16036"
    assert cfg["host"] == "192.0.2.10"
    assert "password" not in raw


def test_cloud_401_refreshes_once_and_repeats_the_original_request(store, monkeypatch):
    session(store)
    sent = []

    def urlopen(request, timeout):
        sent.append(request)
        if len(sent) == 1:
            raise urllib.error.HTTPError(request.full_url, 401, "expired", {}, io.BytesIO(b"{}"))
        if "getAccessToken" in request.full_url:
            return response({"access_token": "new-access", "refresh_token": "new-refresh"})
        return response({"data": {"id": 22}})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = MeuralCloudClient(store, "lounge")
    assert client.upload(b"jpeg", "tesserae-test.jpg") == "22"
    assert [r.get_method() for r in sent] == ["POST", "GET", "POST"]
    assert sent[0].data == sent[2].data
    assert sent[2].get_header("Authorization") == "Token new-access"
    assert store.get_section("devices")["lounge"]["meural_refresh_token_secret"] == "new-refresh"


def test_switching_account_updates_lan_address_for_the_selected_frame(store):
    session(store)
    store.update_item("devices", "lounge", {"host": "192.0.2.99"})
    save_session(
        store,
        "lounge",
        AuthResult("other-access", "other-refresh", time.time() + 3600, "other-trust"),
        email="other@example.com",
        account_id="18",
        devices=[{"id": 16037, "alias": "Other frame", "localIp": "192.0.2.11"}],
    )
    cfg = store.get_section("devices")["lounge"]
    assert cfg["cloud_device_id"] == "16037"
    assert cfg["host"] == "192.0.2.11"
    assert cfg["meural_account_id"] == "18"


def test_expiring_session_refreshes_before_upload(store, monkeypatch):
    session(store, expiry=time.time() + 10)
    urls = []

    def urlopen(request, timeout):
        urls.append(request.full_url)
        if "getAccessToken" in request.full_url:
            return response({"access_token": "fresh"})
        return response({"data": {"id": 23}})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert MeuralCloudClient(store, "lounge").upload(b"jpeg", "t.jpg") == "23"
    assert "getAccessToken" in urls[0]
    assert urls[1] == "https://api.meural.com/v1/items"


class Cloud:
    account_id = "17"

    def __init__(self):
        self.uploads = []
        self.members = set()
        self.loaded = []
        self.deleted = []
        self.fail_load = False
        self.created = 0

    def create_gallery(self, name):
        self.created += 1
        return "300"

    def upload(self, image, name):
        item = str(100 + len(self.uploads))
        self.uploads.append((item, image, name))
        return item

    def find_upload(self, name):
        return next((i for i, _, n in self.uploads if n == name), None)

    def add_to_gallery(self, gallery, item):
        assert gallery == "300"
        self.members.add(item)

    def remove_from_gallery(self, gallery, item):
        self.members.discard(item)

    def load_gallery(self, target, gallery):
        if self.fail_load:
            raise OSError("offline")
        self.loaded.append((target, gallery, set(self.members)))

    def delete_item(self, item):
        self.deleted.append(item)


def test_cloud_holds_one_image_without_periodic_uploads_and_survives_restart(tmp_path):
    client = Cloud()
    path = tmp_path / "delivery.json"
    delivery = CloudDelivery(path, client, "Lounge")
    assert delivery.deliver(b"first", "first", "16036") is True
    for _ in range(3):
        assert CloudDelivery(path, client, "Lounge").deliver(b"first", "first", "16036") is False
    assert len(client.uploads) == 1
    assert client.loaded == [("16036", "300", {"100"})]
    assert client.created == 1


def test_new_pages_replace_managed_playlist_and_only_clean_up_owned_uploads(tmp_path):
    client = Cloud()
    delivery = CloudDelivery(tmp_path / "delivery.json", client, "Lounge")
    for page in ("first", "second", "third"):
        assert delivery.deliver(page.encode(), page, "16036")
    assert client.members == {"102"}
    assert client.deleted == ["100"]
    assert client.loaded[-1] == ("16036", "300", {"102"})


def test_interrupted_cloud_delivery_reuses_pending_image(tmp_path):
    client = Cloud()
    path = tmp_path / "delivery.json"
    client.fail_load = True
    with pytest.raises(OSError):
        CloudDelivery(path, client, "Lounge").deliver(b"jpeg", "first", "16036")
    client.fail_load = False
    assert CloudDelivery(path, client, "Lounge").deliver(b"jpeg", "first", "16036")
    assert len(client.uploads) == 1
    assert client.members == {"100"}
