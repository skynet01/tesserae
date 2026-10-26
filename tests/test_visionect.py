"""app.visionect: the Visionect sign listener and its glue to the push loop.

The end-to-end tests run the real listener on a free localhost port and dial
it with a fake sign built from pyvisionect's own wire helpers: status packets
and ACK/NACKs in, image and command packets out. No hardware and no Visionect
server involved.
"""

from __future__ import annotations

import io
import json
import socket
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask
from PIL import Image
from pyvisionect.devices.enums import PacketType
from pyvisionect.packets import ControlPacket, StatusPacket
from pyvisionect.wire import Compression, DataHeader, Direction, FrameDecoder, encode_frame

from app import visionect
from app.discovery import DiscoveryCache

UUID = "36002000-1247-3830-3933-303600000000"
# What the live 32" sign reported: DisplayType 0xC205012C (falls back to the
# 4 x 1440x640 default), HardwareNameID 8, firmware 6.17.4168, 61 % battery.
STATUS_TAGS = [
    (0, 3),
    (1, 0),
    (8, 0xC205012C),
    (9, 0x7AA8C2E9),
    (10, 61),
    (12, 23),
    (13, 58),
    (15, 164),
    (16, 6),
    (17, 17),
    (18, 4168),
    (22, 8),
    (27, 30),
    (34, 3804),
    (40, 3),
    (43, 585),
    (54, 1),
]


def device_frame(uuid: str, packet_type: int, payload: bytes, packet_id: int) -> bytes:
    return encode_frame(
        DataHeader(
            device_id=visionect.uuid_bytes(uuid),
            type=packet_type,
            id=packet_id,
            length=len(payload),
        ),
        payload,
        direction=Direction.DEVICE_TO_SERVER,
        compression=Compression.NONE,
    )


def status_frame(uuid: str = UUID, packet_id: int = 1) -> bytes:
    payload = StatusPacket(records=STATUS_TAGS, sentinel=0xDEADBEEF).encode()
    return device_frame(uuid, PacketType.STATUS, payload, packet_id)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeSign:
    """A sign over a real socket: send status / ACK / NACK, collect frames."""

    def __init__(self, port: int, uuid: str = UUID) -> None:
        self.uuid = uuid
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.decoder = FrameDecoder(Direction.SERVER_TO_DEVICE)
        self.frames: list[Any] = []
        self.next_id = 1

    def status(self) -> None:
        self.sock.sendall(status_frame(self.uuid, self.next_id))
        self.next_id += 1

    def ack(self, packet_id: int) -> None:
        self.sock.sendall(
            device_frame(self.uuid, PacketType.CONTROL, ControlPacket.ack().encode(), packet_id)
        )

    def nack(self, packet_id: int) -> None:
        self.sock.sendall(
            device_frame(self.uuid, PacketType.CONTROL, ControlPacket.nack().encode(), packet_id)
        )

    def of_type(self, packet_type: int) -> list[Any]:
        return [f for f in self.frames if f.type == packet_type]

    def wait_for(self, pred: Callable[[FakeSign], bool], timeout: float = 15.0) -> bool:
        self.sock.settimeout(0.2)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred(self):
                return True
            try:
                chunk = self.sock.recv(1 << 20)
            except TimeoutError:
                continue
            if not chunk:
                return pred(self)
            self.frames.extend(self.decoder.feed(chunk))
        return pred(self)

    def images(self) -> list[Any]:
        return self.of_type(PacketType.IMAGE)

    def commands(self) -> list[Any]:
        return self.of_type(PacketType.COMMAND)

    def close(self) -> None:
        self.sock.close()


class FakeSettings:
    def __init__(self, sections: dict[str, dict[str, Any]]) -> None:
        self.sections = sections

    def get_section(self, name: str) -> dict[str, Any]:
        return self.sections.setdefault(name, {})

    def patch_section(self, name: str, values: dict[str, Any]) -> None:
        self.get_section(name).update(values)


class FakeDevice:
    def __init__(
        self, device_id: str, kind_of: str | None, panel: dict[str, Any], path: Path
    ) -> None:
        self.id = device_id
        self.kind_of = kind_of
        self.manifest: dict[str, Any] = {"panel": dict(panel)}
        self.path = path
        path.write_text(json.dumps({"id": device_id, "panel": panel}), encoding="utf-8")

    @property
    def panel(self) -> dict[str, Any]:
        return dict(self.manifest.get("panel") or {})


SIGN_PANEL = {"w": 1440, "h": 2560, "orientation": "portrait", "native_w": 1440, "native_h": 2560}


class Harness:
    """A service wired to fake devices, settings and renders."""

    def __init__(
        self, tmp_path: Path, *, uuid: str = "", app_settings: dict[str, Any] | None = None
    ) -> None:
        self.renders = tmp_path / "renders"
        self.renders.mkdir()
        self.settings = FakeSettings(
            {"devices": {"lobby": {"uuid": uuid}}, "app": dict(app_settings or {})}
        )
        kind = FakeDevice("visionect", None, SIGN_PANEL, tmp_path / "kind.json")
        self.device = FakeDevice("lobby", "visionect", SIGN_PANEL, tmp_path / "lobby.json")
        self.registry = SimpleNamespace(devices={"visionect": kind, "lobby": self.device})
        self.latest: dict[str, Any] | None = None
        self.repaints: list[str] = []
        self.app = Flask(__name__)
        self.app.config["SETTINGS_STORE"] = self.settings
        self.app.config["DISCOVERY_CACHE"] = DiscoveryCache()
        self.app.config["PUSH_MANAGER"] = SimpleNamespace(
            invalidate_and_repaint=lambda device_id, reencode=True: self.repaints.append(device_id)
        )
        self.svc = visionect.VisionectService(host="127.0.0.1", port=free_port())
        self.svc.attach(
            visionect._Context(
                app=self.app,
                devices=self.registry,  # type: ignore[arg-type]
                settings=self.settings,
                renders_dir=self.renders,
                latest_render_fn=lambda _device_id: self.latest,
            )
        )

    def render(self, digest: str, shade: int = 0) -> None:
        img = Image.new("L", (1440, 2560), 255)
        img.paste(shade, (0, 0, 1440, 400))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        (self.renders / f"{digest}.png").write_bytes(buf.getvalue())
        self.latest = {"digest": digest, "filename": f"{digest}.png", "ext": "png"}

    def push(self) -> None:
        self.svc.on_push()


@pytest.fixture
def harness(tmp_path: Path):
    h = Harness(tmp_path)
    yield h
    h.svc.stop()


def _window_around_now(sleep: bool) -> dict[str, Any]:
    now = datetime.now()
    return {
        "quiet_hours_enabled": True,
        "quiet_hours_start": (now - timedelta(hours=1)).strftime("%H:%M"),
        "quiet_hours_end": (now + timedelta(hours=1)).strftime("%H:%M"),
        "quiet_hours_sleep": sleep,
        "timezone": "system",
    }


# -- pure helpers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (UUID, UUID),
        (UUID.upper(), UUID),
        (UUID.replace("-", ""), UUID),
        ("", ""),
        ("36002000-1247", ""),
        (None, ""),
        ("zz" + UUID[2:], ""),
    ],
)
def test_normalise_uuid(raw: object, want: str) -> None:
    assert visionect.normalise_uuid(raw) == want


def test_status_heartbeat_maps_the_sign_fields() -> None:
    fields = StatusPacket(records=STATUS_TAGS, sentinel=0).fields()
    beat = visionect.status_heartbeat(fields, uuid=UUID, connect_reason="heartbeat")
    assert beat["battery_pct"] == 61
    assert beat["battery_mv"] == 3804
    assert beat["rssi"] == -58
    assert beat["temperature_c"] == 23
    assert beat["uptime_s"] == 164 * 60
    assert beat["fw_version"].startswith("6.17")
    assert beat["uuid"] == UUID


def test_configured_port_env_then_setting_then_default(monkeypatch) -> None:
    settings = FakeSettings({"app": {"visionect_port": 12000}})
    monkeypatch.delenv("TESSERAE_VISIONECT_PORT", raising=False)
    assert visionect.configured_port(settings) == 12000
    monkeypatch.setenv("TESSERAE_VISIONECT_PORT", "12001")
    assert visionect.configured_port(settings) == 12001
    monkeypatch.setenv("TESSERAE_VISIONECT_PORT", "nonsense")
    assert visionect.configured_port(FakeSettings({})) == visionect.DEFAULT_PORT


def test_artifact_path_stays_inside_the_renders_dir(tmp_path: Path) -> None:
    assert visionect.artifact_path(tmp_path, {"filename": "../../etc/passwd"}) == (
        tmp_path / "passwd"
    )
    assert visionect.artifact_path(tmp_path, {"digest": "abc", "ext": "png"}) == (
        tmp_path / "abc.png"
    )
    assert visionect.artifact_path(tmp_path, {}) is None


def test_catalog_skus_count_as_visionect() -> None:
    marker = SimpleNamespace(PROTOCOL="visionect")
    assert visionect.is_visionect_device(SimpleNamespace(id="lobby", kind_of="visionect"))
    # A Joan 6 SKU instance: its kind id differs, the shared module says so.
    assert visionect.is_visionect_device(
        SimpleNamespace(id="hall", kind_of="visionect_joan_6", module=marker)
    )
    assert not visionect.is_visionect_device(
        SimpleNamespace(id="x", kind_of="trmnl_x", module=SimpleNamespace())
    )


def test_conform_panel_adopts_the_signs_buffer(tmp_path: Path) -> None:
    default = {
        "w": 1440,
        "h": 2560,
        "orientation": "landscape",
        "native_w": 1440,
        "native_h": 2560,
    }
    device: Any = FakeDevice("joan", "visionect", default, tmp_path / "joan.json")
    assert visionect.conform_panel(device, (1024, 758), (1440, 2560))
    stored = json.loads((tmp_path / "joan.json").read_text())["panel"]
    assert (stored["native_w"], stored["native_h"]) == (1024, 758)
    assert (stored["w"], stored["h"]) == (1024, 758)  # landscape, re-seeded from defaults
    assert device.panel["native_w"] == 1024
    # Already conformed: nothing to write.
    assert not visionect.conform_panel(device, (1024, 758), (1440, 2560))


# -- quiet hours --------------------------------------------------------------


def test_sleep_minutes_inside_a_sleep_through_window(tmp_path: Path) -> None:
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    minutes = h.svc._sleep_minutes(h.device)  # type: ignore[arg-type]
    assert minutes is not None
    # Window ends an hour from now (inclusive end minute) plus the margin.
    assert 55 <= minutes <= 65


def test_no_sleep_without_sleep_through(tmp_path: Path) -> None:
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=False))
    assert h.svc._sleep_minutes(h.device) is None  # type: ignore[arg-type]


# -- end to end over a real socket ------------------------------------------


def test_unknown_sign_binds_and_gets_exactly_one_frame(harness: Harness) -> None:
    harness.render("frame-a")
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    try:
        sign.status()
        assert sign.wait_for(lambda s: len(s.images()) == 1)
        # The first status raises both DeviceConnected and StatusReceived; a
        # second status before the ACK must not resend either.
        sign.status()
        sign.wait_for(lambda s: len(s.images()) > 1, timeout=1.5)
        assert len(sign.images()) == 1
        assert all(f.device_id == visionect.uuid_bytes(UUID) for f in sign.images())
    finally:
        sign.close()
    assert harness.settings.sections["devices"]["lobby"]["uuid"] == UUID


def test_newer_frame_waits_for_the_ack_of_the_older_one(harness: Harness) -> None:
    harness.render("frame-a", shade=0)
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    try:
        sign.status()
        assert sign.wait_for(lambda s: len(s.images()) == 1)
        first = sign.images()[0]
        harness.render("frame-b", shade=128)
        harness.push()
        # One image in flight at a time: B waits for A's ACK.
        sign.wait_for(lambda s: len(s.images()) > 1, timeout=1.5)
        assert len(sign.images()) == 1
        sign.ack(first.id)
        assert sign.wait_for(lambda s: len(s.images()) == 2)
        assert sign.images()[1].id != first.id
    finally:
        sign.close()


def test_refused_frame_is_retried(harness: Harness, monkeypatch) -> None:
    monkeypatch.setattr(visionect, "NACK_RETRY_S", 0.3)
    harness.render("frame-a")
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    try:
        sign.status()
        assert sign.wait_for(lambda s: len(s.images()) == 1)
        sign.nack(sign.images()[0].id)
        assert sign.wait_for(lambda s: len(s.images()) == 2, timeout=5)
    finally:
        sign.close()


def test_sleep_only_after_the_frame_is_acked(tmp_path: Path) -> None:
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    try:
        h.render("frame-a")
        assert h.svc.start()
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            assert sign.wait_for(lambda s: len(s.images()) == 1)
            sign.wait_for(lambda s: len(s.commands()) > 0, timeout=1.5)
            assert sign.commands() == []
            sign.ack(sign.images()[0].id)
            assert sign.wait_for(lambda s: len(s.commands()) == 1)
        finally:
            sign.close()
    finally:
        h.svc.stop()


def test_refused_sleep_is_not_replayed_after_quiet_hours(tmp_path: Path) -> None:
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    try:
        assert h.svc.start()  # no render: straight to the sleep step
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            assert sign.wait_for(lambda s: len(s.commands()) == 1)
            h.settings.sections["app"]["quiet_hours_enabled"] = False
            sign.nack(sign.commands()[0].id)
            sign.status()
            sign.wait_for(lambda s: len(s.commands()) > 1, timeout=1.5)
            assert len(sign.commands()) == 1
        finally:
            sign.close()
    finally:
        h.svc.stop()


def test_sign_without_a_free_device_is_listed_as_discovered(tmp_path: Path) -> None:
    h = Harness(tmp_path, uuid="11112222-3333-4444-5555-666677778888")
    try:
        h.render("frame-a")
        assert h.svc.start()
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            sign.wait_for(lambda s: len(s.images()) > 0, timeout=1.5)
            assert sign.images() == []
        finally:
            sign.close()
        entry = h.app.config["DISCOVERY_CACHE"].get(visionect.discovery_id(UUID))
        assert entry is not None
        assert entry.kind == "visionect"  # no catalog model in this registry
        assert (entry.panel_w, entry.panel_h) == (1440, 2560)
        assert entry.parsed["uuid"] == UUID
        assert h.settings.sections["devices"]["lobby"]["uuid"] != UUID
    finally:
        h.svc.stop()


def test_a_differently_sized_sign_conforms_the_panel_and_repaints(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    try:
        # A device first set up for a 13" board, then bound to the 32" sign.
        thirteen = {
            "w": 1600,
            "h": 1200,
            "orientation": "landscape",
            "native_w": 1600,
            "native_h": 1200,
        }
        h.device.manifest["panel"] = dict(thirteen)
        h.device.path.write_text(json.dumps({"id": "lobby", "panel": thirteen}), encoding="utf-8")
        h.render("frame-a")
        assert h.svc.start()
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            deadline = time.monotonic() + 10
            while not h.repaints and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            sign.close()
        assert h.repaints == ["lobby"]
        assert (h.device.panel["native_w"], h.device.panel["native_h"]) == (1440, 2560)
    finally:
        h.svc.stop()


def test_stop_closes_a_connected_signs_socket(harness: Harness) -> None:
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    try:
        sign.status()
        sign.wait_for(lambda s: False, timeout=0.5)
        started = time.monotonic()
        harness.svc.stop()
        assert time.monotonic() - started < 5
        sign.sock.settimeout(3)
        closed = False
        try:
            while True:
                if not sign.sock.recv(65536):
                    closed = True
                    break
        except (ConnectionResetError, TimeoutError):
            closed = True
        assert closed
    finally:
        sign.close()


def test_register_moves_the_listener_when_the_port_changes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(visionect, "_SERVICE", None)
    monkeypatch.delenv("TESSERAE_VISIONECT_PORT", raising=False)
    h = Harness(tmp_path)
    push_mgr = SimpleNamespace(latest_render_for=lambda _d: None, add_listener=lambda cb: None)
    h.app.config.update(
        PUSH_MANAGER=push_mgr, DEVICE_REGISTRY=h.registry, RENDERS_DIR=h.renders, TESTING=True
    )
    h.settings.sections["app"]["visionect_port"] = 12000
    visionect.register(h.app)
    first = h.app.config["VISIONECT_SERVICE"]
    assert first.port == 12000
    assert not first.running  # never binds under pytest
    h.settings.sections["app"]["visionect_port"] = 12001
    visionect.register(h.app)
    assert h.app.config["VISIONECT_SERVICE"] is not first
    assert h.app.config["VISIONECT_SERVICE"].port == 12001
