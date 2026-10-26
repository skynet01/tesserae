"""HA discovery controls: lineup switches / buttons, per-display lineup
select, automation + quiet-hours switches, hold, wake interval, firmware
update, input events, notify, and the stale-lineup sweep."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from app.ha_discovery import (
    CMD_TOPIC_AUTOMATION,
    CMD_TOPIC_NOTIFY,
    CMD_TOPIC_QUIET_HOURS,
    DEVICE_CMD_WILDCARD,
    LINEUP_CMD_WILDCARD,
    LINEUP_NONE,
    STATE_TOPIC_AUTOMATION,
    STATE_TOPIC_QUIET_HOURS,
    HaHooks,
    HomeAssistantDiscovery,
)
from app.push import PushResult
from app.state.deck_model import Deck, DeckPage
from app.state.deck_nav_store import DeckNavStore
from app.state.deck_store import DeckStore
from app.state.device_rotation_state_model import DeviceRotationState
from app.state.device_rotation_state_store import DeviceRotationStateStore
from app.state.page_store import Page, PageStore, Panel
from app.state.settings_store import SettingsStore
from app.transport import BrokerConfig, MqttTransport


class _FakeMqttClient:
    on_connect = on_disconnect = on_message = None

    def __init__(self, *a, **kw):
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.subscribed: list[tuple[str, int]] = []

    def username_pw_set(self, *a, **kw):
        pass

    def connect(self, *a, **kw):
        return 0

    def disconnect(self):
        return 0

    def loop_start(self):
        return 0

    def loop_stop(self):
        return 0

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        return type("R", (), {"rc": 0})()

    def subscribe(self, topic, qos):
        self.subscribed.append((topic, qos))
        return (0, 1)


class _FakeDevice:
    def __init__(self, id: str, name: str, config_schema: dict | None = None) -> None:
        self.id = id
        self.display_name = name
        self.name = name
        self.kind_of = "esp32"
        self.panel = {"w": 800, "h": 480}
        self.manifest: dict = {}
        self.config_schema = config_schema or {}


class _FakeRegistry:
    def __init__(self, devices: list[_FakeDevice]) -> None:
        self._d = {d.id: d for d in devices}

    def all(self) -> list[_FakeDevice]:
        return list(self._d.values())

    def get(self, device_id: str):
        return self._d.get(device_id)


def _deck(**kw) -> Deck:
    base = dict(
        id="morning",
        name="Morning",
        device_ids=["lounge"],
        pages=[DeckPage(page_id="home"), DeckPage(page_id="weather")],
    )
    base.update(kw)
    return Deck(**base)


def _wire(tmp_path: Path, *, devices=None, hooks: HaHooks | None = None, device_status=None):
    fakes = {}

    def factory(client_id: str):
        fakes["client"] = _FakeMqttClient(client_id)
        return fakes["client"]

    transport = MqttTransport(BrokerConfig(host="x"), client_factory=factory)
    transport.connect()
    pm = MagicMock()
    pm.push.return_value = PushResult(status="sent", page_id="home")
    pm.warm_deck_page.return_value = True
    pm.promote_deck_page.return_value = False
    pm.latest_render_for.return_value = None
    page_store = PageStore(tmp_path / "pages.json")
    page_store.save(Page(id="home", name="Home", panel=Panel(w=800, h=480), device_ids=["lounge"]))
    page_store.save(
        Page(id="weather", name="Weather", panel=Panel(w=800, h=480), device_ids=["lounge"])
    )
    ha = HomeAssistantDiscovery(
        transport=transport,
        push_manager=pm,
        page_store=page_store,
        base_url_fn=lambda: "http://lan.test:8000",
        device_registry=devices,
        device_status=device_status,
        hooks=hooks,
        state_refresh_s=0,  # no ticker thread in tests
    )
    return ha, fakes["client"], pm, page_store


def _payload_for(client: _FakeMqttClient, topic: str) -> bytes | None:
    """Last payload published to ``topic`` (retained semantics)."""
    out = None
    for t, p, *_ in client.published:
        if t == topic:
            out = p
    return out


def _topics(client: _FakeMqttClient) -> list[str]:
    return [t for t, *_ in client.published]


def _stores(tmp_path: Path) -> tuple[DeckStore, DeckNavStore, SettingsStore]:
    return (
        DeckStore(tmp_path / "decks.json"),
        DeckNavStore(tmp_path / "nav.json"),
        SettingsStore(tmp_path / "settings.json"),
    )


# ---------- lineups on the hub ----------


def test_lineup_switch_and_buttons_published(tmp_path: Path) -> None:
    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck())
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav, settings_store=settings),
    )
    ha.start()
    topics = _topics(client)
    assert "homeassistant/switch/tesserae/lineup_morning/config" in topics
    assert "homeassistant/button/tesserae/lineup_morning_push/config" in topics
    assert "homeassistant/button/tesserae/lineup_morning_next/config" in topics
    assert "homeassistant/button/tesserae/lineup_morning_prev/config" in topics
    cfg = json.loads(_payload_for(client, "homeassistant/switch/tesserae/lineup_morning/config"))
    assert cfg["command_topic"] == "tesserae/ha/lineup/morning/cmd/enabled"
    assert cfg["name"] == "Morning"
    assert _payload_for(client, "tesserae/ha/lineup/morning/state/enabled") == b"ON"
    subs = [t for t, _ in client.subscribed]
    assert LINEUP_CMD_WILDCARD in subs and DEVICE_CMD_WILDCARD in subs


def test_lineup_without_displays_has_no_action_buttons(tmp_path: Path) -> None:
    decks, _nav, settings = _stores(tmp_path)
    decks.upsert(_deck(device_ids=[]))
    ha, client, _pm, _ = _wire(tmp_path, hooks=HaHooks(deck_store=decks, settings_store=settings))
    ha.start()
    topics = _topics(client)
    assert "homeassistant/switch/tesserae/lineup_morning/config" in topics
    assert "homeassistant/button/tesserae/lineup_morning_push/config" not in topics


def test_deck_store_change_republishes_lineup_configs(tmp_path: Path) -> None:
    decks, _nav, settings = _stores(tmp_path)
    ha, client, _pm, _ = _wire(tmp_path, hooks=HaHooks(deck_store=decks, settings_store=settings))
    ha.start()
    assert "homeassistant/switch/tesserae/lineup_evening/config" not in _topics(client)
    decks.upsert(_deck(id="evening", name="Evening"))
    assert "homeassistant/switch/tesserae/lineup_evening/config" in _topics(client)
    client.published.clear()
    decks.delete("evening")
    # Deleted lineup: config blanked (empty retained payload).
    assert _payload_for(client, "homeassistant/switch/tesserae/lineup_evening/config") == b""


def test_lineup_enabled_command_flips_deck_and_invalidates(tmp_path: Path) -> None:
    decks, _nav, settings = _stores(tmp_path)
    decks.upsert(_deck())
    invalidated: list[str] = []
    ha, client, _pm, _ = _wire(
        tmp_path,
        hooks=HaHooks(
            deck_store=decks,
            settings_store=settings,
            invalidate_deck_fn=lambda deck: invalidated.append(deck.id),
        ),
    )
    ha.start()
    ha._on_lineup_cmd("tesserae/ha/lineup/morning/cmd/enabled", b"OFF")
    assert decks.get("morning").enabled is False
    assert invalidated == ["morning"]
    assert _payload_for(client, "tesserae/ha/lineup/morning/state/enabled") == b"OFF"
    ha._on_lineup_cmd("tesserae/ha/lineup/morning/cmd/enabled", b"ON")
    assert decks.get("morning").enabled is True
    assert _payload_for(client, "tesserae/ha/lineup/morning/state/enabled") == b"ON"


def test_lineup_push_action_warms_and_pushes_entry(tmp_path: Path) -> None:
    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck())
    ha, _client, pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav, settings_store=settings),
    )
    ha.start()
    ha._on_lineup_cmd("tesserae/ha/lineup/morning/cmd/action", b"push")
    assert pm.warm_deck_page.call_count == 2
    pm.push.assert_called_once_with(
        "home",
        device_ids={"lounge"},
        respect_quiet_hours=False,
        force_publish=True,
        source="home_assistant",
    )
    assert (
        nav.get("lounge") == {"deck_id": "morning", "page_id": "home"}
        or nav.get("lounge")["page_id"] == "home"
    )


def test_lineup_next_and_prev_step_through_pages(tmp_path: Path) -> None:
    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck())
    nav.set("lounge", "morning", "home")
    ha, _client, pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav, settings_store=settings),
    )
    ha.start()
    ha._on_lineup_cmd("tesserae/ha/lineup/morning/cmd/action", b"next")
    assert pm.push.call_args.args[0] == "weather"
    assert nav.get("lounge")["page_id"] == "weather"
    ha._on_lineup_cmd("tesserae/ha/lineup/morning/cmd/action", b"prev")
    assert pm.push.call_args.args[0] == "home"
    assert nav.get("lounge")["page_id"] == "home"


# ---------- per-display lineup select ----------


def test_device_lineup_select_state_and_options(tmp_path: Path) -> None:
    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck())
    decks.upsert(_deck(id="evening", name="Evening", device_ids=[]))
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav, settings_store=settings),
    )
    ha.start()
    cfg = json.loads(_payload_for(client, "homeassistant/select/tesserae/dev_lounge_lineup/config"))
    assert cfg["options"] == [LINEUP_NONE, "Evening", "Morning"]
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/lineup") == b"Morning"


def test_device_lineup_select_binds_and_goes_live(tmp_path: Path) -> None:
    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck(id="evening", name="Evening", device_ids=[]))
    ha, client, pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav, settings_store=settings),
    )
    ha.start()
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/lineup", b"Evening")
    assert "lounge" in decks.get("evening").device_ids
    assert pm.push.call_args.kwargs["device_ids"] == {"lounge"}
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/lineup") == b"Evening"


def test_device_lineup_select_none_unbinds(tmp_path: Path) -> None:
    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck())
    nav.set("lounge", "morning", "home")
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav, settings_store=settings),
    )
    ha.start()
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/lineup", LINEUP_NONE.encode())
    assert decks.get("morning").device_ids == []
    assert nav.get("lounge") is None
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/lineup") == LINEUP_NONE.encode()


# ---------- hub switches ----------


def test_automation_switch_pauses_and_resumes(tmp_path: Path) -> None:
    _decks, _nav, settings = _stores(tmp_path)
    ha, client, _pm, _ = _wire(tmp_path, hooks=HaHooks(settings_store=settings))
    ha.start()
    assert "homeassistant/switch/tesserae/automation/config" in _topics(client)
    assert _payload_for(client, STATE_TOPIC_AUTOMATION) == b"ON"
    ha._on_automation_cmd(CMD_TOPIC_AUTOMATION, b"OFF")
    assert settings.get_section("app")["automation_paused"] is True
    assert _payload_for(client, STATE_TOPIC_AUTOMATION) == b"OFF"
    ha._on_automation_cmd(CMD_TOPIC_AUTOMATION, b"ON")
    assert settings.get_section("app")["automation_paused"] is False
    assert _payload_for(client, STATE_TOPIC_AUTOMATION) == b"ON"


def test_quiet_hours_switch_and_device_quiet_state(tmp_path: Path) -> None:
    _decks, _nav, settings = _stores(tmp_path)
    settings.update_section(
        "app", {"quiet_hours_start": "00:00", "quiet_hours_end": "23:59", "timezone": "UTC"}
    )
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(settings_store=settings, timezone_fn=lambda: UTC),
    )
    ha.start()
    assert _payload_for(client, STATE_TOPIC_QUIET_HOURS) == b"OFF"
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/quiet_now") == b"OFF"
    ha._on_quiet_hours_cmd(CMD_TOPIC_QUIET_HOURS, b"ON")
    assert settings.get_section("app")["quiet_hours_enabled"] is True
    assert _payload_for(client, STATE_TOPIC_QUIET_HOURS) == b"ON"
    # The (nearly) all-day window means the display is quiet right now.
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/quiet_now") == b"ON"


def test_device_quiet_override_switch_calls_hook(tmp_path: Path) -> None:
    _decks, _nav, settings = _stores(tmp_path)
    calls: list[tuple[str, bool]] = []
    device = _FakeDevice("lounge", "Lounge")

    def set_override(device_id: str, enabled: bool) -> str | None:
        calls.append((device_id, enabled))
        device.manifest["quiet_hours"] = {"enabled": enabled, "start": "22:00", "end": "07:00"}
        return None

    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([device]),
        hooks=HaHooks(settings_store=settings, set_device_quiet_override_fn=set_override),
    )
    ha.start()
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/quiet_override") == b"OFF"
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/quiet_override", b"ON")
    assert calls == [("lounge", True)]
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/quiet_override") == b"ON"


# ---------- hold ----------


def test_hold_number_sets_and_clears_override(tmp_path: Path) -> None:
    _decks, _nav, _settings = _stores(tmp_path)
    rot_state = DeviceRotationStateStore(tmp_path / "rot.json")
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(rotation_state_store=rot_state),
    )
    ha.start()
    assert "homeassistant/number/tesserae/dev_lounge_hold/config" in _topics(client)
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/hold") == b"0"
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/hold", b"90")
    state = rot_state.get("lounge")
    assert state is not None and state.override_until is not None
    remaining = (state.override_until - datetime.now(UTC)).total_seconds()
    assert 88 * 60 < remaining <= 90 * 60
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/hold") in (b"89", b"90")
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/hold", b"0")
    assert rot_state.get("lounge").override_until is None
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/hold") == b"0"


def test_hold_state_reflects_existing_override(tmp_path: Path) -> None:
    rot_state = DeviceRotationStateStore(tmp_path / "rot.json")
    rot_state.upsert(
        DeviceRotationState(
            device_id="lounge", override_until=datetime.now(UTC) + timedelta(minutes=30)
        )
    )
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(rotation_state_store=rot_state),
    )
    ha.start()
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/hold") in (b"29", b"30")


# ---------- wake interval ----------


def test_wake_interval_number_published_from_schema_and_clamped(tmp_path: Path) -> None:
    schema = {"sleep_interval_s": {"type": "int", "default": 900, "min": 60, "max": 86400}}
    device = _FakeDevice("lounge", "Lounge", config_schema=schema)
    config = {"sleep_interval_s": 900}
    updates: list[dict] = []

    def set_config(device_id: str, values: dict) -> str | None:
        updates.append(values)
        config.update(values)
        return None

    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([device]),
        hooks=HaHooks(device_config_fn=lambda _id: dict(config), set_device_config_fn=set_config),
    )
    ha.start()
    cfg = json.loads(
        _payload_for(client, "homeassistant/number/tesserae/dev_lounge_wake_interval/config")
    )
    assert cfg["min"] == 60 and cfg["max"] == 86400
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/wake_interval") == b"900"
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/wake_interval", b"10")
    assert updates == [{"sleep_interval_s": 60}]
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/wake_interval") == b"60"


def test_no_wake_interval_entity_without_schema(tmp_path: Path) -> None:
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(device_config_fn=lambda _id: {}),
    )
    ha.start()
    assert "homeassistant/number/tesserae/dev_lounge_wake_interval/config" not in _topics(client)


# ---------- firmware update ----------


def test_update_entity_only_for_ota_capable_and_install_calls_hook(tmp_path: Path) -> None:
    installs: list[str] = []
    fw = {
        "lounge": {
            "capable": True,
            "installed_version": "1.29.0",
            "latest_version": "1.30.1",
            "in_progress": False,
            "release_url": "https://example.test/r",
        },
        "hall": {"capable": False, "installed_version": None, "latest_version": None},
    }
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge"), _FakeDevice("hall", "Hall")]),
        hooks=HaHooks(
            firmware_state_fn=lambda device_id: fw[device_id],
            firmware_install_fn=lambda device_id: installs.append(device_id),
        ),
    )
    ha.start()
    assert "homeassistant/update/tesserae/dev_lounge_firmware/config" in _topics(client)
    assert "homeassistant/update/tesserae/dev_hall_firmware/config" not in _topics(client)
    state = json.loads(_payload_for(client, "tesserae/ha/dev/lounge/state/firmware"))
    assert state["installed_version"] == "1.29.0"
    assert state["latest_version"] == "1.30.1"
    assert state["release_url"] == "https://example.test/r"
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/firmware_install", b"install")
    assert installs == ["lounge"]


# ---------- input events, notify, refresh ----------


class _FakeButtonService:
    def __init__(self) -> None:
        self.listeners: list = []

    def add_listener(self, cb) -> None:
        self.listeners.append(cb)

    def remove_listener(self, cb) -> None:
        self.listeners.remove(cb)


def test_input_event_entity_publishes_non_retained_event(tmp_path: Path) -> None:
    svc = _FakeButtonService()
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(button_service=svc),
    )
    ha.start()
    cfg = json.loads(_payload_for(client, "homeassistant/event/tesserae/dev_lounge_input/config"))
    assert set(cfg["event_types"]) == {"button", "tap", "swipe", "slide"}
    assert len(svc.listeners) == 1
    svc.listeners[0](
        {
            "device_id": "lounge",
            "kind": "button",
            "name": "left",
            "outcome": "pushed",
            "page_id": "weather",
        }
    )
    rows = [
        (p, retain)
        for t, p, _q, retain in client.published
        if t == "tesserae/ha/dev/lounge/state/event"
    ]
    assert len(rows) == 1
    payload, retain = rows[0]
    assert retain is False
    body = json.loads(payload)
    assert body["event_type"] == "button"
    assert body["name"] == "left"
    assert body["page_name"] == "Weather"
    ha.stop()
    assert svc.listeners == []


def test_notify_commands_call_hook(tmp_path: Path) -> None:
    calls: list[tuple[str | None, str]] = []
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(notify_fn=lambda device_id, msg: calls.append((device_id, msg))),
    )
    ha.start()
    assert "homeassistant/notify/tesserae/notify_all/config" in _topics(client)
    assert "homeassistant/notify/tesserae/dev_lounge_notify/config" in _topics(client)
    ha._on_notify_all_cmd(CMD_TOPIC_NOTIFY, b"Dinner is ready")
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/notify", b"Bins tonight")
    assert calls == [(None, "Dinner is ready"), ("lounge", "Bins tonight")]


def test_refresh_action_pushes_the_page_on_glass(tmp_path: Path) -> None:
    ha, _client, pm, _ = _wire(tmp_path, devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]))
    pm.latest_render_for.return_value = {"page_id": "weather", "digest": "abc"}
    ha.start()
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/action", b"refresh")
    pm.push.assert_called_once_with(
        "weather",
        device_ids={"lounge"},
        force_publish=True,
        bypass_coalesce=True,
        source="home_assistant",
    )


def test_online_and_low_battery_binary_sensors(tmp_path: Path) -> None:
    now = datetime.now(UTC).timestamp()
    status = {
        "lounge": {"received_at": now - 60, "parsed": {"battery_pct": 9}},
        "hall": {"received_at": now - 86400, "parsed": {"battery_pct": 80}},
    }
    _decks, _nav, settings = _stores(tmp_path)
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge"), _FakeDevice("hall", "Hall")]),
        device_status=status,
        hooks=HaHooks(settings_store=settings, expected_interval_fn=lambda _id: 900),
    )
    ha.start()
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/online") == b"ON"
    assert _payload_for(client, "tesserae/ha/dev/hall/state/online") == b"OFF"
    assert "homeassistant/binary_sensor/tesserae/dev_lounge_low_battery/config" in _topics(client)
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/low_battery") == b"ON"
    assert _payload_for(client, "tesserae/ha/dev/hall/state/low_battery") == b"OFF"


def test_extra_heartbeat_sensors_are_lazy(tmp_path: Path) -> None:
    ha, client, _pm, _ = _wire(tmp_path, devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]))
    ha.start()
    assert "homeassistant/sensor/tesserae/dev_lounge_firmware_version/config" not in _topics(client)
    ha.note_device_heartbeat(
        "lounge", {"fw_version": "1.30.1", "temperature_c": 21.5, "uptime_s": 42}
    )
    topics = _topics(client)
    assert "homeassistant/sensor/tesserae/dev_lounge_firmware_version/config" in topics
    assert "homeassistant/sensor/tesserae/dev_lounge_temperature/config" in topics
    assert "homeassistant/sensor/tesserae/dev_lounge_uptime/config" in topics
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/firmware_version") == b"1.30.1"


# ---------- teardown ----------


def test_stop_blanks_every_published_config(tmp_path: Path) -> None:
    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck())
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav, settings_store=settings),
    )
    ha.start()
    configs = {t for t in _topics(client) if t.startswith("homeassistant/")}
    client.published.clear()
    ha.stop()
    blanked = {t for t, p, *_ in client.published if t.startswith("homeassistant/") and p == b""}
    assert configs <= blanked


def test_sweep_blanks_stale_lineup_configs(tmp_path: Path) -> None:
    decks, _nav, settings = _stores(tmp_path)
    decks.upsert(_deck())
    ha, client, _pm, _ = _wire(tmp_path, hooks=HaHooks(deck_store=decks, settings_store=settings))
    hub = {"identifiers": ["tesserae"]}
    stale = json.dumps({"object_id": "tesserae_lineup_gone_push", "device": hub}).encode()
    live = json.dumps({"object_id": "tesserae_lineup_morning_push", "device": hub}).encode()
    fixed = json.dumps({"object_id": "tesserae_automation", "device": hub}).encode()
    original_subscribe = ha._transport.subscribe

    def subscribe(topic, cb, *, qos=1):
        original_subscribe(topic, cb, qos=qos)
        if topic.startswith("homeassistant/+/"):
            cb("homeassistant/button/tesserae/lineup_gone_push/config", stale)
            cb("homeassistant/button/tesserae/lineup_morning_push/config", live)
            cb("homeassistant/switch/tesserae/automation/config", fixed)

    ha._transport.subscribe = subscribe  # type: ignore[method-assign]
    ha._sweep_stale_retained()
    blanked = {t for t, p, *_ in client.published if p == b""}
    assert "homeassistant/button/tesserae/lineup_gone_push/config" in blanked
    assert "homeassistant/button/tesserae/lineup_morning_push/config" not in blanked
    assert "homeassistant/switch/tesserae/automation/config" not in blanked


# ---------- per-display next / previous ----------


def _steps_hook(calls: list[tuple[str, int]], err: str | None = None):
    def step(device_id: str, delta: int) -> str | None:
        calls.append((device_id, delta))
        return err

    return step


def test_next_prev_buttons_published_on_the_display_with_a_step_hook(tmp_path: Path) -> None:
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(step_device_fn=_steps_hook([])),
    )
    ha.start()
    nxt = json.loads(_payload_for(client, "homeassistant/button/tesserae/dev_lounge_next/config"))
    prv = json.loads(_payload_for(client, "homeassistant/button/tesserae/dev_lounge_prev/config"))
    assert nxt["name"] == "Next" and prv["name"] == "Previous"
    assert nxt["command_topic"] == prv["command_topic"] == "tesserae/ha/dev/lounge/cmd/action"
    assert (nxt["payload_press"], prv["payload_press"]) == ("next", "prev")
    assert nxt["unique_id"] == "tesserae_dev_lounge_next"
    assert nxt["device"]["identifiers"] == ["tesserae_dev_lounge"]


def test_next_prev_buttons_absent_without_the_hook(tmp_path: Path) -> None:
    ha, client, _pm, _ = _wire(tmp_path, devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]))
    ha.start()
    topics = _topics(client)
    assert "homeassistant/button/tesserae/dev_lounge_next/config" not in topics
    assert "homeassistant/button/tesserae/dev_lounge_prev/config" not in topics
    # The refresh button needs no hook and is still there.
    assert "homeassistant/button/tesserae/dev_lounge_refresh/config" in topics


def test_next_prev_commands_route_to_the_hook(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(step_device_fn=_steps_hook(calls)),
    )
    ha.start()
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/action", b"next")
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/action", b" prev\n")
    assert calls == [("lounge", 1), ("lounge", -1)]
    # Busy is cleared even when nothing pushed (a promoted warm frame).
    assert _payload_for(client, "tesserae/ha/state/busy") == b"0"


def test_next_prev_ignore_junk_and_unknown_displays(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []
    ha, _client, pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(step_device_fn=_steps_hook(calls)),
    )
    ha.start()
    for junk in (b"", b"NEXT", b"next please", b"\xff\xfenext", b"nex", b"previous", b"1"):
        ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/action", junk)
    ha._on_device_cmd("tesserae/ha/dev/ghost/cmd/action", b"next")  # unknown / deleted
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/next", b"next")  # wrong leaf
    ha._on_device_cmd("tesserae/ha/devlounge/cmd/action", b"next")  # malformed topic
    assert calls == []
    pm.push.assert_not_called()


def test_step_error_and_exception_land_on_last_error(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(step_device_fn=_steps_hook(calls, err="Lounge: render failed")),
    )
    ha.start()
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/action", b"next")
    assert _payload_for(client, "tesserae/ha/state/last_error") == b"Lounge: render failed"

    def boom(_device_id: str, _delta: int) -> str | None:
        raise RuntimeError("nope")

    ha._hooks.step_device_fn = boom
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/action", b"prev")  # must not raise
    assert _payload_for(client, "tesserae/ha/state/last_error") == b"step failed"
    assert _payload_for(client, "tesserae/ha/state/busy") == b"0"


def test_next_steps_only_the_pressed_display_through_the_button_service(
    tmp_path: Path,
) -> None:
    from app.button_service import ButtonService
    from app.state.legacy_projections import RotationProjection

    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck(device_ids=["lounge", "hall"]))
    nav.set("lounge", "morning", "home")
    nav.set("hall", "morning", "home")
    holder: dict[str, ButtonService] = {}

    def step(device_id: str, delta: int) -> str | None:
        holder["svc"].step(device_id=device_id, forward=delta > 0)
        return None

    ha, client, pm, page_store = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge"), _FakeDevice("hall", "Hall")]),
        hooks=HaHooks(
            deck_store=decks, deck_nav_store=nav, settings_store=settings, step_device_fn=step
        ),
    )
    holder["svc"] = ButtonService(
        rotation_store=RotationProjection(decks),
        state_store=DeviceRotationStateStore(tmp_path / "rot.json"),
        settings_store=settings,
        page_store=page_store,
        push_manager=pm,
        deck_store=decks,
        deck_nav_store=nav,
    )
    ha.start()
    ha._on_device_cmd("tesserae/ha/dev/lounge/cmd/action", b"next")
    assert pm.push.call_args.args[0] == "weather"
    assert pm.push.call_args.kwargs["device_ids"] == {"lounge"}
    assert pm.push.call_args.kwargs["source"] == "home_assistant"
    assert nav.current_page("lounge", "morning") == "weather"
    assert nav.current_page("hall", "morning") == "home"
    # The pressed display's state was republished; it still follows Morning.
    assert _payload_for(client, "tesserae/ha/dev/lounge/state/lineup") == b"Morning"


# ---------- publish toggles ----------


def _blanked(client: _FakeMqttClient) -> set[str]:
    return {t for t, p, *_ in client.published if p == b""}


_LINEUP_TOPICS = (
    "homeassistant/switch/tesserae/lineup_morning/config",
    "homeassistant/button/tesserae/lineup_morning_push/config",
    "homeassistant/button/tesserae/lineup_morning_next/config",
    "homeassistant/button/tesserae/lineup_morning_prev/config",
    "homeassistant/select/tesserae/dev_lounge_lineup/config",
)
_PAGE_BUTTON_TOPICS = (
    "homeassistant/button/tesserae/page_home/config",
    "homeassistant/button/tesserae/page_weather/config",
)
_KEPT_TOPICS = (
    "homeassistant/select/tesserae/active_page/config",
    "homeassistant/select/tesserae/dev_lounge_dashboard/config",
    "homeassistant/button/tesserae/dev_lounge_next/config",
    "homeassistant/button/tesserae/dev_lounge_prev/config",
    "homeassistant/button/tesserae/dev_lounge_refresh/config",
    "homeassistant/number/tesserae/dev_lounge_hold/config",
    "homeassistant/switch/tesserae/automation/config",
)


def _toggle_rig(tmp_path: Path):
    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck())
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(
            deck_store=decks,
            deck_nav_store=nav,
            settings_store=settings,
            rotation_state_store=DeviceRotationStateStore(tmp_path / "rot.json"),
            step_device_fn=_steps_hook([]),
        ),
    )
    return ha, client, settings


def test_both_groups_publish_by_default(tmp_path: Path) -> None:
    ha, client, _settings = _toggle_rig(tmp_path)
    ha.start()
    topics = set(_topics(client))
    assert set(_LINEUP_TOPICS) <= topics
    assert set(_PAGE_BUTTON_TOPICS) <= topics
    assert set(_KEPT_TOPICS) <= topics


def test_lineup_controls_off_blanks_them_live_and_back_on_republishes(tmp_path: Path) -> None:
    ha, client, settings = _toggle_rig(tmp_path)
    ha.start()
    client.published.clear()

    settings.patch_section("app", {"ha_discovery_lineup_controls": False})
    ha.refresh_entity_configs()
    assert set(_LINEUP_TOPICS) <= _blanked(client)
    # Everything else stays: dashboard buttons, selects, next / prev, hold.
    assert not (set(_PAGE_BUTTON_TOPICS) | set(_KEPT_TOPICS)) & _blanked(client)
    for topic in _PAGE_BUTTON_TOPICS + _KEPT_TOPICS:
        assert _payload_for(client, topic) not in (None, b"")
    # Nobody reads the lineup states now; they stop being published.
    client.published.clear()
    ha.refresh_state()
    topics = _topics(client)
    assert "tesserae/ha/lineup/morning/state/enabled" not in topics
    assert "tesserae/ha/dev/lounge/state/lineup" not in topics

    client.published.clear()
    settings.patch_section("app", {"ha_discovery_lineup_controls": True})
    ha.refresh_entity_configs()
    for topic in _LINEUP_TOPICS:
        assert _payload_for(client, topic) not in (None, b"")


def test_dashboard_buttons_off_blanks_them_but_keeps_the_selects(tmp_path: Path) -> None:
    ha, client, settings = _toggle_rig(tmp_path)
    ha.start()
    client.published.clear()

    settings.patch_section("app", {"ha_discovery_dashboard_buttons": False})
    ha.refresh_entity_configs()
    assert set(_PAGE_BUTTON_TOPICS) <= _blanked(client)
    assert not (set(_LINEUP_TOPICS) | set(_KEPT_TOPICS)) & _blanked(client)
    select = json.loads(_payload_for(client, "homeassistant/select/tesserae/active_page/config"))
    assert select["options"] == ["Home", "Weather"]

    client.published.clear()
    settings.patch_section("app", {"ha_discovery_dashboard_buttons": True})
    ha.refresh_entity_configs()
    for topic in _PAGE_BUTTON_TOPICS:
        assert _payload_for(client, topic) not in (None, b"")


def test_a_new_dashboard_gets_no_button_while_they_are_off(tmp_path: Path) -> None:
    ha, client, settings = _toggle_rig(tmp_path)
    settings.patch_section("app", {"ha_discovery_dashboard_buttons": False})
    ha.start()
    ha._page_store.save(Page(id="news", name="News", panel=Panel(w=800, h=480)))
    assert "homeassistant/button/tesserae/page_news/config" not in _topics(client)


def test_starting_with_both_off_publishes_neither_group(tmp_path: Path) -> None:
    ha, client, settings = _toggle_rig(tmp_path)
    settings.patch_section(
        "app", {"ha_discovery_lineup_controls": False, "ha_discovery_dashboard_buttons": False}
    )
    ha.start()
    topics = set(_topics(client))
    assert not (set(_LINEUP_TOPICS) | set(_PAGE_BUTTON_TOPICS)) & topics
    assert set(_KEPT_TOPICS) <= topics


def test_settings_save_rebuild_honours_the_toggles(tmp_path: Path) -> None:
    # A Settings -> Server save rebuilds the transport: the old publisher
    # stops (blanking everything) and a fresh one starts from the saved
    # values, which is how ha_discovery_enabled takes effect live too.
    ha, client, settings = _toggle_rig(tmp_path)
    ha.start()
    settings.patch_section("app", {"ha_discovery_lineup_controls": False})
    ha.stop()
    assert set(_LINEUP_TOPICS) <= _blanked(client)
    client.published.clear()
    fresh = HomeAssistantDiscovery(
        transport=ha._transport,
        push_manager=ha._push_manager,
        page_store=ha._page_store,
        base_url_fn=lambda: "http://lan.test:8000",
        device_registry=ha._devices,
        hooks=ha._hooks,
        state_refresh_s=0,
    )
    fresh.start()
    topics = set(_topics(client))
    assert not set(_LINEUP_TOPICS) & topics
    assert set(_KEPT_TOPICS) <= topics


def test_missing_or_unreadable_settings_publish_everything(tmp_path: Path) -> None:
    decks, nav, _settings = _stores(tmp_path)
    decks.upsert(_deck())
    ha, client, _pm, _ = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav),  # no settings store
    )
    ha.start()
    assert set(_LINEUP_TOPICS) | set(_PAGE_BUTTON_TOPICS) <= set(_topics(client))

    broken = MagicMock()
    broken.get_section.side_effect = RuntimeError("settings unreadable")
    ha2, client2, _pm2, _ = _wire(
        tmp_path / "b",
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav, settings_store=broken),
    )
    ha2.start()
    assert set(_LINEUP_TOPICS) | set(_PAGE_BUTTON_TOPICS) <= set(_topics(client2))


def test_toggle_values_parse_like_switches(tmp_path: Path) -> None:
    from app.ha_discovery import _switch_on

    assert _switch_on({}, "k") is True
    assert _switch_on({"k": None}, "k") is True
    assert _switch_on({"k": False}, "k") is False
    assert _switch_on({"k": 0}, "k") is False
    assert _switch_on({"k": "off"}, "k") is False
    assert _switch_on({"k": "false"}, "k") is False
    assert _switch_on({"k": ""}, "k") is False
    assert _switch_on({"k": "on"}, "k") is True
    assert _switch_on({"k": "TRUE"}, "k") is True
    assert _switch_on({"k": 1}, "k") is True


def _feed_retained(ha: HomeAssistantDiscovery, retained: dict[str, dict]) -> None:
    original_subscribe = ha._transport.subscribe

    def subscribe(topic, cb, *, qos=1):
        original_subscribe(topic, cb, qos=qos)
        if topic.startswith("homeassistant/+/"):
            for t, cfg in retained.items():
                cb(t, json.dumps(cfg).encode())

    ha._transport.subscribe = subscribe  # type: ignore[method-assign]


def test_sweep_blanks_retained_entities_of_groups_switched_off(tmp_path: Path) -> None:
    ha, client, settings = _toggle_rig(tmp_path)
    settings.patch_section(
        "app", {"ha_discovery_lineup_controls": False, "ha_discovery_dashboard_buttons": False}
    )
    hub = {"identifiers": ["tesserae"]}
    lounge = {"identifiers": ["tesserae_dev_lounge"]}
    _feed_retained(
        ha,
        {
            "homeassistant/button/tesserae/page_home/config": {
                "object_id": "tesserae_page_home",
                "device": hub,
            },
            "homeassistant/switch/tesserae/lineup_morning/config": {
                "object_id": "tesserae_lineup_morning",
                "device": hub,
            },
            "homeassistant/select/tesserae/dev_lounge_lineup/config": {
                "object_id": "tesserae_dev_lounge_lineup",
                "device": lounge,
            },
            "homeassistant/select/tesserae/active_page/config": {
                "object_id": "tesserae_active_page",
                "device": hub,
            },
            "homeassistant/image/tesserae/dev_lounge_frame/config": {
                "object_id": "tesserae_dev_lounge_frame",
                "device": lounge,
            },
            "homeassistant/sensor/tesserae/dev_lounge_battery/config": {
                "object_id": "tesserae_dev_lounge_battery",
                "device": lounge,
            },
        },
    )
    ha._sweep_stale_retained()
    blanked = _blanked(client)
    assert "homeassistant/button/tesserae/page_home/config" in blanked
    assert "homeassistant/switch/tesserae/lineup_morning/config" in blanked
    assert "homeassistant/select/tesserae/dev_lounge_lineup/config" in blanked
    assert "homeassistant/select/tesserae/active_page/config" not in blanked
    assert "homeassistant/image/tesserae/dev_lounge_frame/config" not in blanked
    assert "homeassistant/sensor/tesserae/dev_lounge_battery/config" not in blanked


def test_sweep_blanks_retained_next_prev_when_the_hook_is_gone(tmp_path: Path) -> None:
    ha, client, _pm, _ = _wire(tmp_path, devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]))
    lounge = {"identifiers": ["tesserae_dev_lounge"]}
    _feed_retained(
        ha,
        {
            "homeassistant/button/tesserae/dev_lounge_next/config": {
                "object_id": "tesserae_dev_lounge_next",
                "device": lounge,
            },
            "homeassistant/button/tesserae/dev_lounge_refresh/config": {
                "object_id": "tesserae_dev_lounge_refresh",
                "device": lounge,
            },
        },
    )
    ha._sweep_stale_retained()
    blanked = _blanked(client)
    assert "homeassistant/button/tesserae/dev_lounge_next/config" in blanked
    assert "homeassistant/button/tesserae/dev_lounge_refresh/config" not in blanked


def test_sweep_keeps_lineup_buttons_only_while_they_can_act(tmp_path: Path) -> None:
    decks, _nav, settings = _stores(tmp_path)
    decks.upsert(_deck(device_ids=[]))  # no displays: switch only, no buttons
    ha, client, _pm, _ = _wire(tmp_path, hooks=HaHooks(deck_store=decks, settings_store=settings))
    hub = {"identifiers": ["tesserae"]}
    _feed_retained(
        ha,
        {
            "homeassistant/switch/tesserae/lineup_morning/config": {
                "object_id": "tesserae_lineup_morning",
                "device": hub,
            },
            "homeassistant/button/tesserae/lineup_morning_push/config": {
                "object_id": "tesserae_lineup_morning_push",
                "device": hub,
            },
        },
    )
    ha._sweep_stale_retained()
    blanked = _blanked(client)
    assert "homeassistant/button/tesserae/lineup_morning_push/config" in blanked
    assert "homeassistant/switch/tesserae/lineup_morning/config" not in blanked


# ---------- teardown with overlapping ids ----------


def test_removing_a_display_spares_one_whose_id_extends_it(tmp_path: Path) -> None:
    registry = _FakeRegistry([_FakeDevice("a", "A"), _FakeDevice("a_b", "A B")])
    ha, client, _pm, _ = _wire(tmp_path, devices=registry)
    ha.start()
    ha.note_device_heartbeat("a_b", {"battery_pct": 50})
    del registry._d["a"]
    client.published.clear()
    ha.refresh_entity_configs()
    blanked = _blanked(client)
    assert "homeassistant/image/tesserae/dev_a_frame/config" in blanked
    assert not {t for t in blanked if "/dev_a_b_" in t}
    assert ("sensor", "dev_a_b_battery") in ha._published_configs


def test_deleting_a_lineup_spares_one_whose_id_extends_it(tmp_path: Path) -> None:
    decks, _nav, settings = _stores(tmp_path)
    decks.upsert(_deck(id="a", name="A"))
    decks.upsert(_deck(id="a_b", name="A B"))
    ha, client, _pm, _ = _wire(tmp_path, hooks=HaHooks(deck_store=decks, settings_store=settings))
    ha.start()
    client.published.clear()
    decks.delete("a")
    blanked = _blanked(client)
    assert "homeassistant/switch/tesserae/lineup_a/config" in blanked
    assert not {t for t in blanked if "/lineup_a_b" in t}


def test_lineup_select_and_next_prev_agree_on_the_followed_lineup(tmp_path: Path) -> None:
    from app.button_service import ButtonService
    from app.state.legacy_projections import RotationProjection

    decks, nav, settings = _stores(tmp_path)
    decks.upsert(_deck(id="breakfast", name="Breakfast"))
    decks.upsert(_deck(id="dinner", name="Dinner"))
    decks.upsert(_deck(id="late", name="Late", enabled=False))
    ha, _client, pm, page_store = _wire(
        tmp_path,
        devices=_FakeRegistry([_FakeDevice("lounge", "Lounge")]),
        hooks=HaHooks(deck_store=decks, deck_nav_store=nav, settings_store=settings),
    )
    svc = ButtonService(
        rotation_store=RotationProjection(decks),
        state_store=DeviceRotationStateStore(tmp_path / "rot.json"),
        settings_store=settings,
        page_store=page_store,
        push_manager=pm,
        deck_store=decks,
        deck_nav_store=nav,
    )

    def both() -> tuple[str | None, str | None]:
        a, b = ha._lineup_for_device("lounge"), svc.following_deck("lounge")
        return (a.id if a else None, b.id if b else None)

    assert both() == ("breakfast", "breakfast")  # no nav record: first enabled
    nav.set("lounge", "dinner", "home")
    assert both() == ("dinner", "dinner")  # the one on the glass
    nav.set("lounge", "late", "home")  # picked through the Lineup select
    assert both() == ("late", "late")
    decks.upsert(_deck(id="late", name="Late", enabled=False, device_ids=[]))
    assert both() == ("breakfast", "breakfast")  # no longer binds the display
