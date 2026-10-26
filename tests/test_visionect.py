"""app.visionect: the Visionect sign listener and its glue to the push loop.

The end-to-end tests run the real listener on a free localhost port and dial
it with a fake sign built from pyvisionect's own wire helpers: status packets
and ACK/NACKs in, image and command packets out. No hardware and no Visionect
server involved.
"""

from __future__ import annotations

import io
import json
import os
import socket
import threading
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
from pyvisionect.wire import (
    BlockHeader,
    Compression,
    DataHeader,
    Direction,
    FrameDecoder,
    ProtocolHeader,
    encode_frame,
)

from app import visionect, visionect_listener
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


def status_frame(
    uuid: str = UUID, packet_id: int = 1, tags: list[tuple[int, int]] | None = None
) -> bytes:
    payload = StatusPacket(records=tags or STATUS_TAGS, sentinel=0xDEADBEEF).encode()
    return device_frame(uuid, PacketType.STATUS, payload, packet_id)


def with_tag(tag: int, value: int) -> list[tuple[int, int]]:
    """STATUS_TAGS with one tag's value replaced."""
    return [(t, value if t == tag else v) for t, v in STATUS_TAGS]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeSign:
    """A sign over a real socket: send status / ACK / NACK, collect frames."""

    def __init__(self, port: int, uuid: str = UUID, *, rcvbuf: int | None = None) -> None:
        self.uuid = uuid
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf is not None:  # a sign that stops reading fills this up
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.settimeout(5)
        self.sock.connect(("127.0.0.1", port))
        self.decoder = FrameDecoder(Direction.SERVER_TO_DEVICE)
        self.frames: list[Any] = []
        self.next_id = 1

    @property
    def port(self) -> int:
        return int(self.sock.getsockname()[1])

    def status(self, tags: list[tuple[int, int]] | None = None) -> None:
        self.sock.sendall(status_frame(self.uuid, self.next_id, tags))
        self.next_id += 1

    def dropped(self, timeout: float = 5.0) -> bool:
        """Whether the server closes this connection within ``timeout``."""
        self.sock.settimeout(0.2)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(1 << 20)
            except TimeoutError:
                continue
            except (ConnectionResetError, BrokenPipeError):
                return True
            if not chunk:
                return True
            self.frames.extend(self.decoder.feed(chunk))
        return False

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
        self.consumed: list[str] = []
        self.busy = False  # the push pipeline is rendering for the device
        self.app = Flask(__name__)
        self.app.config["SETTINGS_STORE"] = self.settings
        self.app.config["DISCOVERY_CACHE"] = DiscoveryCache()
        self.app.config["PUSH_MANAGER"] = SimpleNamespace(
            invalidate_and_repaint=lambda device_id, reencode=True: self.repaints.append(device_id),
            consume_force_refetch=self.consumed.append,
        )
        self.svc = visionect.VisionectService(host="127.0.0.1", port=free_port())
        self.svc.attach(
            visionect._Context(
                app=self.app,
                devices=self.registry,  # type: ignore[arg-type]
                settings=self.settings,
                renders_dir=self.renders,
                latest_render_fn=lambda _device_id: self.latest,
                render_busy_fn=lambda _device_id: self.busy,
            )
        )

    def render(self, digest: str, shade: int = 0, *, noisy: bool = False) -> None:
        if noisy:  # incompressible, so the frame really is ~1.8 MB on the wire
            img = Image.frombytes("L", (1440, 2560), os.urandom(1440 * 2560))
        else:
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


def test_conform_panel_never_writes_a_deleted_device_back(tmp_path: Path) -> None:
    device: Any = FakeDevice("joan", "visionect", SIGN_PANEL, tmp_path / "joan.json")
    (tmp_path / "joan.json").unlink()  # deleted while the sign's status was in flight
    assert not visionect.conform_panel(device, (1024, 758), (1440, 2560), lambda: device)
    assert not (tmp_path / "joan.json").exists()
    # Still on disk, but no longer in the registry: also left alone.
    device = FakeDevice("joan", "visionect", SIGN_PANEL, tmp_path / "joan.json")
    before = (tmp_path / "joan.json").read_text()
    assert not visionect.conform_panel(device, (1024, 758), (1440, 2560), lambda: None)
    assert (tmp_path / "joan.json").read_text() == before


def test_conform_panel_waits_for_other_instance_file_writers(tmp_path: Path) -> None:
    """A rename (or any other instance-file save) that runs while the sign
    conforms the panel isn't lost: both hold the instance-file lock."""
    from app.device_service import INSTANCE_FILE_LOCK

    device: Any = FakeDevice("joan", "visionect", SIGN_PANEL, tmp_path / "joan.json")
    done = threading.Event()
    with INSTANCE_FILE_LOCK:
        worker = threading.Thread(
            target=lambda: (
                visionect.conform_panel(device, (1024, 758), (1440, 2560), lambda: device),
                done.set(),
            )
        )
        worker.start()
        assert not done.wait(0.3), "conformed while another writer held the file"
        raw = json.loads(device.path.read_text())
        raw["name"] = "Desk Joan"  # the rename that was in progress
        device.path.write_text(json.dumps(raw), encoding="utf-8")
    assert done.wait(5)
    worker.join()
    stored = json.loads(device.path.read_text())
    assert stored["name"] == "Desk Joan"
    assert (stored["panel"]["native_w"], stored["panel"]["native_h"]) == (1024, 758)


def test_instance_file_writers_hold_the_instance_file_lock() -> None:
    """Every read-modify-write of an instance file (and delete) is
    serialised with the Visionect panel conformance."""
    from app import device_service, trmnl_api
    from app.settings import devices_routes

    for fn in (
        device_service.update_instance_panel,
        device_service.update_instance_gamut,
        device_service.update_instance_renderer,
        device_service.set_instance_renderer_id,
        device_service.update_advertised_gamuts,
        device_service.set_grey_prompt_dismissed,
        device_service.sync_renderer_for_firmware,
        device_service.update_instance_kind,
        device_service.update_instance_quiet_hours,
        device_service.update_instance_locale,
        device_service.update_instance_battery_offset,
        device_service.delete_instance,
        devices_routes._apply_rename,
        devices_routes._make_rest_instance,
        devices_routes.devices_set_transport,
        devices_routes.devices_regenerate_token,
    ):
        assert "INSTANCE_FILE_LOCK" in fn.__code__.co_names, fn.__name__
    assert "INSTANCE_FILE_LOCK" in trmnl_api._adopt_reported_panel.__code__.co_names


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
                    closed = True  # EOF
                    break
        except ConnectionResetError:
            closed = True
        except TimeoutError:
            closed = False  # still open: the listener left the socket behind
        assert closed
    finally:
        sign.close()


def test_register_moves_the_listener_when_the_port_changes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(visionect, "_SERVICE", None)
    monkeypatch.delenv("TESSERAE_VISIONECT_PORT", raising=False)
    h = Harness(tmp_path)
    listeners: list[Any] = []
    push_mgr = SimpleNamespace(
        latest_render_for=lambda _d: None,
        add_listener=listeners.append,
        remove_listener=listeners.remove,
    )
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
    # The retired listener is off the push manager and never restarts itself.
    assert listeners == [h.app.config["VISIONECT_SERVICE"].on_push]
    assert not first.autostart


def test_refused_sleep_backs_off(tmp_path: Path) -> None:
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    try:
        assert h.svc.start()  # no render: straight to the sleep step
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            assert sign.wait_for(lambda s: len(s.commands()) == 1)
            sign.nack(sign.commands()[0].id)
            sign.status()
            sign.wait_for(lambda s: len(s.commands()) > 1, timeout=1.5)
            assert len(sign.commands()) == 1  # no NACK/sleep ping-pong
        finally:
            sign.close()
    finally:
        h.svc.stop()


def test_sleep_waits_for_the_repaint_after_a_size_change(tmp_path: Path) -> None:
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    try:
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

        def _invalidate(device_id: str, reencode: bool = True) -> None:
            h.repaints.append(device_id)
            h.latest = None  # the real repaint drops the render until it's redone

        h.app.config["PUSH_MANAGER"] = SimpleNamespace(invalidate_and_repaint=_invalidate)
        assert h.svc.start()
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            deadline = time.monotonic() + 10
            while not h.repaints and time.monotonic() < deadline:
                time.sleep(0.05)
            assert h.repaints == ["lobby"]
            sign.status()
            sign.wait_for(lambda s: len(s.commands()) > 0, timeout=1.5)
            assert sign.commands() == []  # not asleep before the new frame
            h.render("frame-b")
            h.push()
            assert sign.wait_for(lambda s: len(s.images()) == 1)
            sign.ack(sign.images()[0].id)
            assert sign.wait_for(lambda s: len(s.commands()) == 1)
        finally:
            sign.close()
    finally:
        h.svc.stop()


def test_disconnected_sign_stops_reading_as_connected(harness: Harness) -> None:
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    sign.status()
    deadline = time.monotonic() + 5
    while not (harness.svc.sign_view(UUID) or {}).get("connected"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    sign.close()
    time.sleep(0.3)
    harness.svc._call_soon(harness.svc._rescan)
    deadline = time.monotonic() + 5
    while (harness.svc.sign_view(UUID) or {}).get("connected"):
        assert time.monotonic() < deadline, "still reads as connected"
        time.sleep(0.05)


def test_glass_check_resends_once_after_the_crc_has_had_time() -> None:
    svc = visionect.VisionectService()
    sign = visionect._Sign(uuid=UUID)
    sign.shown_digest, sign.shown_checksum = "frame-a", 111
    status = SimpleNamespace(display_state_crc=222)
    sign.shown_at = time.time()
    svc._check_glass(sign, status)
    assert sign.shown_digest == "frame-a"  # too soon: 6.17 reports a status late
    sign.shown_at = time.time() - visionect.CRC_CONVERGE_S - 1
    svc._check_glass(sign, status)
    assert sign.shown_digest == ""  # one corrective resend
    sign.shown_digest, sign.shown_at = "frame-a", time.time() - visionect.CRC_CONVERGE_S - 1
    svc._check_glass(sign, status)
    assert sign.shown_digest == "frame-a"  # never a loop
    svc._check_glass(sign, SimpleNamespace(display_state_crc=111))
    assert not sign.crc_corrected  # a match re-arms the check


def test_a_slow_stop_never_lets_a_new_run_share_its_state(harness: Harness, monkeypatch) -> None:
    """stop() gives up joining after its timeout. The old run's teardown must
    not then wipe a new run's loop, readiness, signs and snapshot: start()
    refuses until that thread is gone, and a run only clears its own."""
    import asyncio

    real_shutdown = harness.svc._shutdown

    async def _slow_shutdown(server: Any) -> None:
        await asyncio.sleep(1.0)
        await real_shutdown(server)

    monkeypatch.setattr(harness.svc, "_shutdown", _slow_shutdown)
    assert harness.svc.start()
    old_thread = harness.svc._thread
    harness.svc.stop(timeout=0.1)
    assert old_thread is not None and old_thread.is_alive()
    assert not harness.svc.running
    assert not harness.svc.start()  # refused while the old run winds down
    assert "still shutting down" in (harness.svc.start_error or "")
    listeners = [t for t in threading.enumerate() if t.name == "visionect-listener"]
    assert listeners == [old_thread]
    old_thread.join(5)
    monkeypatch.setattr(harness.svc, "_shutdown", real_shutdown)
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    try:
        sign.status()
        deadline = time.monotonic() + 5
        while not (harness.svc.sign_view(UUID) or {}).get("connected"):
            assert time.monotonic() < deadline, "the new run lost its state"
            time.sleep(0.05)
        assert harness.svc.running
    finally:
        sign.close()


def test_concurrent_starts_run_one_listener(harness: Harness) -> None:
    results: list[bool] = []
    starters = [
        threading.Thread(target=lambda: results.append(harness.svc.start())) for _ in range(4)
    ]
    for t in starters:
        t.start()
    for t in starters:
        t.join(10)
    assert results == [True] * 4
    assert len([t for t in threading.enumerate() if t.name == "visionect-listener"]) == 1


def test_a_retry_armed_before_stop_does_not_restart_the_listener(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(visionect, "START_RETRY_S", 0.2)
    h = Harness(tmp_path)
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", h.svc.port))
    blocker.listen()
    try:
        assert not h.svc.maybe_start()  # arms the retry timer
        gen = h.svc._lifecycle_gen
        h.svc.stop()
    finally:
        blocker.close()
    h.svc._retry_fire(gen)  # a timer that fired just as stop() ran
    time.sleep(0.5)
    assert not h.svc.running
    h.svc.stop()


def test_busy_port_is_retried_on_a_timer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(visionect, "START_RETRY_S", 0.3)
    h = Harness(tmp_path)
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", h.svc.port))
    blocker.listen()
    try:
        assert not h.svc.maybe_start()  # port held, as by the old VSS
        assert h.svc.start_error
    finally:
        blocker.close()
    try:
        deadline = time.monotonic() + 5
        while not h.svc.running:
            assert time.monotonic() < deadline, "listener never retried"
            time.sleep(0.05)
    finally:
        h.svc.stop()


# -- untrusted LAN traffic ----------------------------------------------------


def _closed_soon(sock: socket.socket, timeout: float = 3.0) -> bool:
    """Whether the server drops ``sock`` within ``timeout``."""
    sock.settimeout(timeout)
    try:
        while True:
            if not sock.recv(65536):
                return True
    except (ConnectionResetError, BrokenPipeError):
        return True
    except TimeoutError:
        return False


def _raw(port: int) -> socket.socket:
    return socket.create_connection(("127.0.0.1", port), timeout=5)


def test_the_listener_hooks_pyvisionect_where_it_expects() -> None:
    """GuardedServer overrides two private methods; a pyvisionect release that
    renames or reshapes them must fail here, not silently drop the bounds."""
    import inspect

    from pyvisionect.io import VisionectServer

    for name, params in (
        ("_handle_client", ["self", "reader", "writer"]),
        ("_run_timers", ["self", "conn", "writer"]),
    ):
        method = getattr(VisionectServer, name)
        assert inspect.iscoroutinefunction(method), name
        assert list(inspect.signature(method).parameters) == params, name


def test_a_decompression_bomb_never_reaches_the_decoder(harness: Harness, monkeypatch) -> None:
    import pyvisionect.wire.framing as framing

    def _boom(*_a: Any, **_k: Any) -> bytes:
        raise AssertionError("the bomb reached pyvisionect's block decoder")

    monkeypatch.setattr(framing, "decode_blocks", _boom)
    assert harness.svc.start()
    # Two blocks declared, so the library's one total-size check passes; the
    # first block's one LZ4 byte "inflates" to 2 GiB.
    body = BlockHeader(0, 1, 1, 1 << 31, 0).pack() + b"\x00"
    frame = ProtocolHeader(version=3, compression=Compression.LZ4, length=len(body)).pack() + body
    sock = _raw(harness.svc.port)
    try:
        sock.sendall(frame)
        assert _closed_soon(sock)
    finally:
        sock.close()
    assert harness.svc._server.rejected_frames == 1


def test_a_huge_frame_is_refused_from_its_header(harness: Harness) -> None:
    assert harness.svc.start()
    sock = _raw(harness.svc.port)
    try:
        # Claims 50 MB; the library would buffer all of it before deciding.
        sock.sendall(ProtocolHeader(version=3, length=50_000_000).pack() + b"\0" * 1000)
        assert _closed_soon(sock)
    finally:
        sock.close()
    assert harness.svc._server.rejected_frames == 1


def test_a_small_compressed_frame_still_reaches_the_library(harness: Harness) -> None:
    harness.render("frame-a")
    assert harness.svc.start()
    payload = StatusPacket(records=STATUS_TAGS, sentinel=0xDEADBEEF).encode()
    frame = encode_frame(
        DataHeader(
            device_id=visionect.uuid_bytes(UUID),
            type=PacketType.STATUS,
            id=1,
            length=len(payload),
        ),
        payload,
        direction=Direction.DEVICE_TO_SERVER,
        compression=Compression.LZ4,
    )
    sign = FakeSign(harness.svc.port)
    try:
        for i in range(len(frame)):  # one byte at a time: frames reassemble
            sign.sock.sendall(frame[i : i + 1])
        assert sign.wait_for(lambda s: len(s.images()) == 1)
    finally:
        sign.close()


def test_a_connection_that_never_identifies_is_dropped_on_time(
    harness: Harness, monkeypatch
) -> None:
    """The library's identify timeout is per read, and stops applying once a
    frame carries a device id even if it never yields a valid status. The
    listener's deadline is for the whole connection."""
    monkeypatch.setattr(visionect_listener, "IDENTIFY_TIMEOUT_S", 0.6)
    assert harness.svc.start()
    assert harness.svc._server.identify_timeout == 0.6
    sock = _raw(harness.svc.port)
    try:
        # A status-typed frame whose payload doesn't decode: the library
        # registers the UUID but no sign ever identifies.
        sock.sendall(device_frame(UUID, PacketType.STATUS, b"\x01\x02\x03", 1))
        started = time.monotonic()
        assert _closed_soon(sock, timeout=5)
        assert time.monotonic() - started < 3
    finally:
        sock.close()
    assert harness.svc._server.identify_timeouts == 1


def test_a_sign_is_not_dropped_by_the_identify_deadline(harness: Harness, monkeypatch) -> None:
    monkeypatch.setattr(visionect_listener, "IDENTIFY_TIMEOUT_S", 0.4)
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    try:
        sign.status()
        assert not _closed_soon(sign.sock, timeout=1.0)
    finally:
        sign.close()


def test_the_listener_caps_connections_per_address(harness: Harness, monkeypatch) -> None:
    monkeypatch.setattr(visionect_listener, "MAX_CONNECTIONS_PER_PEER", 2)
    assert harness.svc.start()
    socks = [_raw(harness.svc.port) for _ in range(2)]
    try:
        time.sleep(0.2)
        extra = _raw(harness.svc.port)
        socks.append(extra)
        assert _closed_soon(extra)
        assert not _closed_soon(socks[0], timeout=0.3)
    finally:
        for sock in socks:
            sock.close()
    assert harness.svc._server.refused_connections == 1


def test_a_timer_task_write_error_does_not_escape() -> None:
    """pyvisionect awaits the timer task suppressing only CancelledError, so
    an error out of it would skip the connection's removal and close."""
    import asyncio

    aborted: list[bool] = []

    class _Writer:
        transport = SimpleNamespace(abort=lambda: aborted.append(True), is_closing=lambda: True)

        def get_extra_info(self, _name: str) -> Any:
            return ("10.0.0.9", 1234)

        def write(self, _data: bytes) -> None:
            pass

        async def drain(self) -> None:
            raise ConnectionResetError("Connection lost")

    conn = SimpleNamespace(closed=False, advance=lambda _now: [], drain=lambda: b"\x01")

    async def _run() -> None:
        server = visionect_listener.GuardedServer(on_events=lambda c, e: None)
        await asyncio.wait_for(server._run_timers(conn, _Writer()), timeout=5)

    asyncio.run(_run())  # returns instead of raising ConnectionResetError
    assert aborted == [True]


# -- delivery: deadlines, stuck writes, whose connection it is -----------------


def _fake_addresses(monkeypatch, by_port: dict[int, str]) -> None:
    """Make connections from these local ports look like they come from
    these addresses (every test connection is 127.0.0.1)."""
    real = visionect_listener.peer_ip

    def _peer_ip(writer: Any) -> str:
        peer = writer.get_extra_info("peername") if writer is not None else None
        if isinstance(peer, tuple) and len(peer) > 1 and peer[1] in by_port:
            return by_port[peer[1]]
        return real(writer)

    monkeypatch.setattr(visionect_listener, "peer_ip", _peer_ip)


def _wait(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_an_unacked_frame_expires_and_the_latest_is_sent_on_redial(
    harness: Harness, monkeypatch
) -> None:
    """A lost ACK used to block every later frame, the sleep and the glass
    check until the sign re-dialed, which a plugged-in sign may not do for
    days."""
    monkeypatch.setattr(visionect, "IMAGE_ACK_TIMEOUT_S", 0.5)
    harness.render("frame-a")
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    try:
        sign.status()
        assert sign.wait_for(lambda s: len(s.images()) == 1)
        harness.render("frame-b", shade=128)
        harness.push()  # waits behind the unanswered frame A
        assert sign.dropped(timeout=5), "the connection outlived the ACK deadline"
    finally:
        sign.close()
    assert "never confirmed the frame" in (harness.svc.sign_view(UUID) or {}).get("last_error", "")
    again = FakeSign(harness.svc.port)
    try:
        again.status()
        assert again.wait_for(lambda s: len(s.images()) == 1)
        again.ack(again.images()[0].id)
        assert _wait(lambda: (harness.svc.sign_view(UUID) or {}).get("in_sync") is True)
    finally:
        again.close()
    sign_state = harness.svc._signs[UUID]
    assert sign_state.shown_digest == "frame-b"


def test_an_unacked_sleep_expires(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(visionect, "SLEEP_ACK_TIMEOUT_S", 0.5)
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    try:
        assert h.svc.start()  # no render: straight to the sleep step
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            assert sign.wait_for(lambda s: len(s.commands()) == 1)
            assert sign.dropped(timeout=5)
        finally:
            sign.close()
    finally:
        h.svc.stop()


def test_a_write_that_never_drains_is_dropped(harness: Harness, monkeypatch) -> None:
    monkeypatch.setattr(visionect, "WRITE_TIMEOUT_S", 0.5)
    harness.render("frame-a", noisy=True)
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port, rcvbuf=4096)
    try:
        sign.status()  # and then never reads the 1.8 MB frame
        assert _wait(
            lambda: (
                "stopped taking data" in (harness.svc.sign_view(UUID) or {}).get("last_error", "")
            )
        )
        assert not (harness.svc.sign_view(UUID) or {}).get("connected")
    finally:
        sign.close()


def test_a_redial_supersedes_a_connection_stuck_in_a_write(harness: Harness) -> None:
    """Sol's blocked-writer probe: the pass writing to a half-open socket held
    the sign's lock, so the sign's new connection was never served."""
    harness.render("frame-a", noisy=True)
    assert harness.svc.start()
    stuck = FakeSign(harness.svc.port, rcvbuf=4096)
    try:
        stuck.status()  # the frame's write to this socket blocks
        assert _wait(lambda: (harness.svc.sign_view(UUID) or {}).get("sending") is True)
        time.sleep(0.3)
        fresh = FakeSign(harness.svc.port)
        try:
            fresh.status()
            assert fresh.wait_for(lambda s: len(s.images()) == 1, timeout=10)
            assert stuck.dropped(timeout=5)
        finally:
            fresh.close()
    finally:
        stuck.close()


def test_another_address_cannot_take_over_a_bound_sign(tmp_path: Path, monkeypatch) -> None:
    h = Harness(tmp_path, uuid=UUID)
    try:
        h.render("frame-a")
        assert h.svc.start()
        sign = FakeSign(h.svc.port)
        intruder: FakeSign | None = None
        try:
            sign.status()
            assert sign.wait_for(lambda s: len(s.images()) == 1)
            sign.ack(sign.images()[0].id)
            intruder = FakeSign(h.svc.port)
            _fake_addresses(monkeypatch, {sign.port: "10.0.0.5", intruder.port: "10.0.0.66"})
            # (The sign's address was read before the patch; set it to match.)
            h.svc._signs[UUID].remote = "10.0.0.5"
            # Claims the sign's UUID, with a Joan 6's DisplayType.
            intruder.status(with_tag(8, 0x6))
            assert intruder.dropped(timeout=5)
            view = h.svc.sign_view(UUID) or {}
            assert (view.get("refused") or {}).get("remote") == "10.0.0.66"
            assert view.get("connected") and view.get("remote") == "10.0.0.5"
            # The panel isn't conformed to the intruder's geometry, and the
            # next frame still goes to the sign.
            h.render("frame-b", shade=128)
            h.push()
            assert sign.wait_for(lambda s: len(s.images()) == 2)
            assert (h.device.panel["native_w"], h.device.panel["native_h"]) == (1440, 2560)
            assert intruder.images() == []
        finally:
            sign.close()
            if intruder is not None:
                intruder.close()
    finally:
        h.svc.stop()


def test_a_silent_old_connection_is_probed_so_a_moved_sign_gets_back_in(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(visionect, "PROBE_QUIET_S", 0.0)
    monkeypatch.setattr(visionect, "PROBE_TIMEOUT_S", 0.5)
    h = Harness(tmp_path, uuid=UUID)
    try:
        assert h.svc.start()
        old = FakeSign(h.svc.port)  # half-open: never answers again
        old.status()
        assert _wait(lambda: (h.svc.sign_view(UUID) or {}).get("connected") is True)
        moved = FakeSign(h.svc.port)
        _fake_addresses(monkeypatch, {old.port: "10.0.0.5", moved.port: "10.0.0.77"})
        h.svc._signs[UUID].remote = "10.0.0.5"
        moved.status()
        assert moved.dropped(timeout=5)  # refused while the old one may live
        assert old.wait_for(lambda s: len(s.commands()) == 1)  # asked for a status
        assert old.dropped(timeout=5)  # no answer: dropped
        back = FakeSign(h.svc.port)
        _fake_addresses(monkeypatch, {back.port: "10.0.0.77"})
        back.status()
        assert _wait(lambda: (h.svc.sign_view(UUID) or {}).get("remote") == "10.0.0.77")
        assert not back.dropped(timeout=0.5)
        for s in (old, moved, back):
            s.close()
    finally:
        h.svc.stop()


def test_a_redial_from_the_same_address_replaces_the_old_connection(harness: Harness) -> None:
    assert harness.svc.start()
    first = FakeSign(harness.svc.port)
    second: FakeSign | None = None
    try:
        first.status()
        assert _wait(lambda: (harness.svc.sign_view(UUID) or {}).get("connected") is True)
        second = FakeSign(harness.svc.port)
        second.status()
        assert first.dropped(timeout=5)
        assert not second.dropped(timeout=0.5)
    finally:
        first.close()
        if second is not None:
            second.close()


def test_a_closing_socket_reads_as_disconnected() -> None:
    """pyvisionect can leave a dead connection registered; its closing
    transport is what tells."""
    svc = visionect.VisionectService()
    sign = visionect._Sign(uuid=UUID)
    sign.conn = SimpleNamespace(closed=False)
    sign.writer = SimpleNamespace(transport=SimpleNamespace(is_closing=lambda: True))
    assert not svc._alive(sign)
    svc._publish(sign)
    assert (svc.sign_view(UUID) or {}).get("connected") is False
    sign.writer = SimpleNamespace(transport=SimpleNamespace(is_closing=lambda: False))
    assert svc._alive(sign)


# -- sleep vs renders, the glass check, resends ---------------------------------


def test_sleep_waits_for_a_render_in_progress(tmp_path: Path, monkeypatch) -> None:
    """Frame A acked, render B under way as the window starts: the sign used
    to sleep all night on A."""
    monkeypatch.setattr(visionect, "RENDER_HOLD_POLL_S", 0.1)
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    try:
        h.render("frame-a")
        h.busy = True
        assert h.svc.start()
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            assert sign.wait_for(lambda s: len(s.images()) == 1)
            sign.ack(sign.images()[0].id)
            sign.wait_for(lambda s: len(s.commands()) > 0, timeout=1.5)
            assert sign.commands() == []  # B is still rendering
            h.render("frame-b", shade=128)
            h.busy = False
            h.push()
            assert sign.wait_for(lambda s: len(s.images()) == 2)
            sign.wait_for(lambda s: len(s.commands()) > 0, timeout=1.0)
            assert sign.commands() == []  # and B isn't acked yet
            sign.ack(sign.images()[1].id)
            assert sign.wait_for(lambda s: len(s.commands()) == 1)
        finally:
            sign.close()
    finally:
        h.svc.stop()


def test_sleep_waits_for_a_render_even_with_no_frame_yet(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(visionect, "RENDER_HOLD_POLL_S", 0.1)
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    try:
        h.busy = True  # latest_render_for() is None, but a render is coming
        assert h.svc.start()
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            sign.wait_for(lambda s: len(s.commands()) > 0, timeout=1.5)
            assert sign.commands() == []
            h.render("frame-a")
            h.busy = False
            h.push()
            assert sign.wait_for(lambda s: len(s.images()) == 1)
            sign.ack(sign.images()[0].id)
            assert sign.wait_for(lambda s: len(s.commands()) == 1)
        finally:
            sign.close()
    finally:
        h.svc.stop()


def test_a_render_that_never_finishes_holds_the_sleep_only_so_long(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(visionect, "RENDER_HOLD_POLL_S", 0.1)
    monkeypatch.setattr(visionect, "RENDER_HOLD_MAX_S", 0.6)
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    try:
        h.busy = True
        assert h.svc.start()
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            assert sign.wait_for(lambda s: len(s.commands()) == 1, timeout=5)
        finally:
            sign.close()
    finally:
        h.svc.stop()


@pytest.mark.parametrize("failure", ["missing", "encode"])
def test_an_unreadable_render_is_noted_once_and_the_sign_still_sleeps(
    tmp_path: Path, monkeypatch, caplog, failure: str
) -> None:
    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    try:
        h.render("frame-a")
        assert h.svc.start()
        sign = FakeSign(h.svc.port)
        try:
            sign.status()
            assert sign.wait_for(lambda s: len(s.images()) == 1)
            if failure == "missing":
                h.latest = {"digest": "frame-gone", "filename": "frame-gone.png", "ext": "png"}
            else:
                h.render("frame-b", shade=128)

                def _broken(*_a: Any) -> Any:
                    raise ValueError("bad PNG")

                monkeypatch.setattr(visionect, "_encode", _broken)
            caplog.clear()
            sign.ack(sign.images()[0].id)
            # The sign keeps frame A and sleeps on it.
            assert sign.wait_for(lambda s: len(s.commands()) == 1)
            for _ in range(3):  # later passes don't retry or log it again
                h.push()
                sign.status()
            sign.wait_for(lambda s: len(s.images()) > 1, timeout=1.0)
            assert len(sign.images()) == 1
        finally:
            sign.close()
        notes = [r for r in caplog.records if "can't send render" in r.getMessage()]
        assert len(notes) == 1
        view = h.svc.sign_view(UUID) or {}
        assert "couldn't be read" in view.get("last_error", "")
    finally:
        h.svc.stop()


def test_a_frame_adopted_after_a_restart_keeps_its_glass_check(harness: Harness) -> None:
    """At startup a frame the sign already shows (its CRC matches) is
    adopted without a resend; its checksum must arm the glass check too."""
    harness.render("frame-a")
    status = StatusPacket(records=STATUS_TAGS, sentinel=0)
    path = harness.renders / "frame-a.png"
    checksum = int(visionect._encode(path, visionect.sign_panel(status)).state_checksum)
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    try:
        sign.status(with_tag(9, checksum))
        assert _wait(lambda: (harness.svc.sign_view(UUID) or {}).get("in_sync") is True)
        state = harness.svc._signs[UUID]
        assert state.shown_checksum == checksum and state.shown_at > 0
        assert sign.images() == []  # adopted, not resent
        # Much later the sign reports something else: one corrective resend.
        state.shown_at = time.time() - visionect.CRC_CONVERGE_S - 1
        sign.status(with_tag(9, checksum ^ 1))
        assert sign.wait_for(lambda s: len(s.images()) == 1)
    finally:
        sign.close()


def test_an_ack_rearms_the_corrective_resend() -> None:
    from pyvisionect.packets import ControlPacket as Control
    from pyvisionect.session import Acked

    svc = visionect.VisionectService()
    sign = svc._sign(UUID)
    conn = SimpleNamespace(device_id=visionect.uuid_bytes(UUID), closed=False)
    sign.conn = conn
    sign.crc_corrected = True  # spent on the previous frame
    sign.inflight = visionect._Inflight(
        kind="image", packet_id=7, conn=conn, writer=None, digest="frame-b", checksum=5
    )
    event = Acked(packet_id=7, control=Control.ack(), device_id=visionect.uuid_bytes(UUID))
    assert svc._handle_event(conn, event) == UUID
    assert sign.shown_digest == "frame-b" and sign.shown_checksum == 5
    assert not sign.crc_corrected


def test_one_sleep_lasts_at_most_a_day(tmp_path: Path, monkeypatch) -> None:
    from app import quiet_hours

    h = Harness(tmp_path, app_settings=_window_around_now(sleep=True))
    monkeypatch.setattr(
        quiet_hours,
        "quiet_ends_at",
        lambda _window, now, _tz: now + timedelta(days=5),  # a long "away" window
    )
    assert h.svc._sleep_minutes(h.device) == 24 * 60  # type: ignore[arg-type]


def test_a_history_resend_repaints_the_frame_once(harness: Harness) -> None:
    from pyvisionect.packets import ImagePacket

    harness.render("frame-a")
    assert harness.svc.start()
    sign = FakeSign(harness.svc.port)
    try:
        sign.status()
        assert sign.wait_for(lambda s: len(s.images()) == 1)
        sign.ack(sign.images()[0].id)
        assert _wait(lambda: (harness.svc.sign_view(UUID) or {}).get("in_sync") is True)
        normal = ImagePacket.decode(sign.images()[0].payload).checksum
        # History -> resend: the same frame, flagged for a one-shot refetch.
        harness.latest = {**harness.latest, "force_refetch": True, "timestamp": 1000.0}
        harness.push()
        assert sign.wait_for(lambda s: len(s.images()) == 2)
        resent = ImagePacket.decode(sign.images()[1].payload).checksum
        assert resent != normal  # a checksum the sign can't hold: it repaints
        sign.ack(sign.images()[1].id)
        assert _wait(lambda: harness.consumed == ["lobby"])
        assert harness.svc._signs[UUID].shown_checksum == resent
        harness.push()  # the flag is still set, but that resend was honoured
        sign.wait_for(lambda s: len(s.images()) > 2, timeout=1.0)
        assert len(sign.images()) == 2
        harness.latest = {**harness.latest, "timestamp": 2000.0}  # another resend
        harness.push()
        assert sign.wait_for(lambda s: len(s.images()) == 3)
    finally:
        sign.close()
