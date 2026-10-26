"""app.visionect: the Visionect sign listener and its glue to the push loop.

The end-to-end tests run the real listener on a free localhost port and dial
it with a fake sign built from pyvisionect's own wire helpers: a status packet
in, an image packet out. No hardware and no Visionect server involved.
"""

from __future__ import annotations

import io
import os
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask
from PIL import Image
from pyvisionect.devices.enums import PacketType
from pyvisionect.packets import StatusPacket
from pyvisionect.wire import Compression, DataHeader, Direction, FrameDecoder, encode_frame

from app import visionect

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


def status_frame(uuid: str = UUID, packet_id: int = 1) -> bytes:
    payload = StatusPacket(records=STATUS_TAGS, sentinel=0xDEADBEEF).encode()
    return encode_frame(
        DataHeader(
            device_id=visionect.uuid_bytes(uuid),
            type=PacketType.STATUS,
            id=packet_id,
            length=len(payload),
        ),
        payload,
        direction=Direction.DEVICE_TO_SERVER,
        compression=Compression.NONE,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeSettings:
    def __init__(self, sections: dict[str, dict[str, Any]]) -> None:
        self.sections = sections

    def get_section(self, name: str) -> dict[str, Any]:
        return self.sections.setdefault(name, {})

    def patch_section(self, name: str, values: dict[str, Any]) -> None:
        self.get_section(name).update(values)


def make_device(device_id: str = "lobby", manifest: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(id=device_id, kind_of="visionect", manifest=manifest or {})


def make_context(
    tmp_path: Path, settings: FakeSettings, devices: list[Any], *, with_render: bool = True
) -> visionect._Context:
    renders = tmp_path / "renders"
    renders.mkdir()
    latest: dict[str, Any] | None = None
    if with_render:
        img = Image.new("L", (1440, 2560), 255)
        img.paste(0, (0, 0, 1440, 300))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        (renders / "abc123.png").write_bytes(buf.getvalue())
        latest = {"digest": "abc123", "filename": "abc123.png", "ext": "png"}
    app = Flask(__name__)
    app.config["SETTINGS_STORE"] = settings
    return visionect._Context(
        app=app,
        devices=SimpleNamespace(devices={d.id: d for d in devices}),  # type: ignore[arg-type]
        settings=settings,
        renders_dir=renders,
        latest_render_fn=lambda _device_id: latest,
    )


def recv_at_least(sock: socket.socket, want: int, timeout: float) -> bytes:
    sock.settimeout(0.5)
    chunks: list[bytes] = []
    got = 0
    deadline = time.monotonic() + timeout
    while got < want and time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


@pytest.fixture
def service():
    svc = visionect.VisionectService(host="127.0.0.1", port=free_port())
    yield svc
    svc.stop()


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


# -- quiet hours --------------------------------------------------------------


def _window_around_now(sleep: bool) -> dict[str, Any]:
    now = datetime.now()
    return {
        "quiet_hours_enabled": True,
        "quiet_hours_start": (now - timedelta(hours=1)).strftime("%H:%M"),
        "quiet_hours_end": (now + timedelta(hours=1)).strftime("%H:%M"),
        "quiet_hours_sleep": sleep,
        "timezone": "system",
    }


def test_sleep_minutes_inside_a_sleep_through_window(tmp_path: Path) -> None:
    settings = FakeSettings({"app": _window_around_now(sleep=True)})
    device = make_device()
    svc = visionect.VisionectService()
    svc.attach(make_context(tmp_path, settings, [device]))
    minutes = svc._sleep_minutes(device)
    assert minutes is not None
    # Window ends an hour from now (inclusive end minute) plus the margin.
    assert 55 <= minutes <= 65


def test_no_sleep_without_sleep_through(tmp_path: Path) -> None:
    settings = FakeSettings({"app": _window_around_now(sleep=False)})
    device = make_device()
    svc = visionect.VisionectService()
    svc.attach(make_context(tmp_path, settings, [device]))
    assert svc._sleep_minutes(device) is None


# -- end to end over a real socket ------------------------------------------


def test_unknown_sign_binds_and_gets_the_latest_frame(tmp_path: Path, service) -> None:
    settings = FakeSettings({"devices": {"lobby": {"uuid": ""}}})
    device = make_device()
    service.attach(make_context(tmp_path, settings, [device]))
    assert service.start()

    with socket.create_connection(("127.0.0.1", service.port), timeout=5) as sign:
        sign.sendall(status_frame())
        # A full 32" frame is two LZ4-compressed 2880x640 rectangles; even a
        # mostly white test card is tens of kilobytes on the wire.
        data = recv_at_least(sign, 4096, timeout=15)

    assert settings.sections["devices"]["lobby"]["uuid"] == UUID
    frames = FrameDecoder(Direction.SERVER_TO_DEVICE).feed(data)
    assert any(f.type == PacketType.IMAGE for f in frames), [f.type for f in frames]
    assert all(f.device_id == visionect.uuid_bytes(UUID) for f in frames)


def test_sign_with_no_free_device_is_left_alone(tmp_path: Path, service) -> None:
    settings = FakeSettings(
        {"devices": {"lobby": {"uuid": "11112222-3333-4444-5555-666677778888"}}}
    )
    service.attach(make_context(tmp_path, settings, [make_device()]))
    assert service.start()

    with socket.create_connection(("127.0.0.1", service.port), timeout=5) as sign:
        sign.sendall(status_frame())
        data = recv_at_least(sign, 4096, timeout=2)

    assert settings.sections["devices"]["lobby"]["uuid"] != UUID
    assert len(data) < 4096


def test_register_does_not_bind_under_pytest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(visionect, "_SERVICE", None)
    settings = FakeSettings({"devices": {}})
    ctx = make_context(tmp_path, settings, [make_device()])
    push_mgr = SimpleNamespace(latest_render_for=ctx.latest_render_fn, add_listener=lambda cb: None)
    app = ctx.app
    app.config.update(
        PUSH_MANAGER=push_mgr,
        DEVICE_REGISTRY=ctx.devices,
        RENDERS_DIR=ctx.renders_dir,
        TESTING=True,
    )
    assert os.environ.get("PYTEST_CURRENT_TEST")
    visionect.register(app)
    svc = app.config["VISIONECT_SERVICE"]
    assert not svc.running
