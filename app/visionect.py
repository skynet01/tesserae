"""Drive Visionect e-paper signs directly, without the Visionect Software Suite.

A Visionect sign (the 32" Place & Play, Joan, the 13" boards) is a thin
client. It dials the server address stored in its flash on TCP 11113 and
speaks first; the server pushes it pre-encoded 4-bit framebuffers. There is no
handshake and no authentication: the 16-byte UUID in the first status packet
is the sign's whole identity. The wire protocol comes from ``pyvisionect``
(MIT, a clean-room reimplementation of the vendor gateway), so this module only
glues it to Tesserae:

* One process-wide listener (``pyvisionect.io.VisionectServer``) runs on its own
  asyncio loop in a daemon thread, like ``app.embedded_broker``. It starts when
  the first ``visionect`` device exists and stops when the last one goes.
  ``register()`` re-runs on every transport rebuild, so it only re-attaches the
  push listener and refreshes references; the socket stays bound.
* Each push reads the device's renderer artifact (``trmnl_png_gray16``: a
  16-level grey PNG already fitted, flipped and dithered onto the sign's
  native 1440x2560 buffer), encodes it without further dithering, and queues
  it on the sign's ``PendingWork``. A connected sign gets it at once; a
  sleeping one when it next dials in.
* Status packets feed ``record_status_heartbeat`` so the device card, battery
  history and Home Assistant sensors work as for any other device.
* Quiet hours with "sleep through" send the sign to deep sleep until the
  window ends, after the current frame is on the wire. There is no network
  route to wake a sleeping sign early.

A sign connecting with an unknown UUID is bound to the first ``visionect``
device whose UUID field is blank. mypy --strict applies to this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask

from app import background_loops
from app.device_loader import Device, DeviceRegistry

logger = logging.getLogger(__name__)

KIND = "visionect"
DEFAULT_PORT = 11113
DEFAULT_HOST = "0.0.0.0"  # the sign dials in from the LAN
# How often the loop re-checks quiet hours for connected signs.
TICK_S = 60.0
# The longest single deep sleep we ask for. The sign accepts larger values,
# but a week-long sleep from a misconfigured window is not worth risking.
MAX_SLEEP_MINUTES = 6 * 24 * 60
# A few minutes past the window's end so the sign wakes to a new frame.
SLEEP_MARGIN_MINUTES = 2
# pyvisionect.imaging.constants Encoding.FOUR_BIT and Dithering.NONE, as
# literals so this module imports without the dependency. The renderer has
# already dithered onto the 16-grey ramp, and NONE keeps n*17 levels exact.
ENCODING_4BPP = 4
DITHERING_NONE = 1


def normalise_uuid(raw: object) -> str:
    """Canonical lower-case 8-4-4-4-12 UUID, or "" when ``raw`` isn't one."""
    text = str(raw or "").strip().lower().replace("-", "")
    if len(text) != 32:
        return ""
    try:
        bytes.fromhex(text)
    except ValueError:
        return ""
    return f"{text[0:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:32]}"


def uuid_bytes(uuid: str) -> bytes:
    return bytes.fromhex(uuid.replace("-", ""))


def status_heartbeat(fields: dict[str, Any], *, uuid: str, connect_reason: str) -> dict[str, Any]:
    """Map a decoded status packet onto Tesserae's heartbeat keys.

    ``SignalStrength`` is already negated by pyvisionect; ``DeviceUptime`` is in
    minutes; ``Firmware`` is a ``{major, minor, ...}`` dict."""
    out: dict[str, Any] = {"uuid": uuid, "connect_reason": connect_reason}

    def _int(name: str) -> int | None:
        value = fields.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    battery = _int("BatteryLevel")
    if battery is not None and 0 <= battery <= 100:
        out["battery_pct"] = battery
    mv = _int("BatteryVoltage")
    if mv is not None and 0 < mv < 10_000:
        out["battery_mv"] = mv
    rssi = _int("SignalStrength")
    if rssi is not None and -150 < rssi < 0:
        out["rssi"] = rssi
    temp = _int("AverageTemperature")
    if temp is not None and -60 < temp < 100:
        out["temperature_c"] = temp
    uptime = _int("DeviceUptime")
    if uptime is not None and uptime >= 0:
        out["uptime_s"] = uptime * 60
    fw = fields.get("Firmware")
    if isinstance(fw, dict):
        parts = [
            str(fw[k]) for k in ("major", "minor", "revision", "build") if fw.get(k) is not None
        ]
        if parts:
            out["fw_version"] = ".".join(parts)
    return out


@dataclass(frozen=True)
class _Context:
    app: Flask
    devices: DeviceRegistry
    settings: Any
    renders_dir: Path
    latest_render_fn: Callable[[str], dict[str, Any] | None]


class VisionectService:
    """The listener plus the glue between it and the push pipeline."""

    def __init__(self, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self.start_error: str | None = None
        self._ctx: _Context | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Any = None  # pyvisionect.io.VisionectServer
        self._store: Any = None  # pyvisionect.session.DeviceStateStore
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        # All of the below is touched on the loop thread only.
        self._queued_digest: dict[str, str] = {}  # uuid -> digest queued to the sign
        self._inflight: dict[int, tuple[str, str]] = {}  # image packet id -> (uuid, digest)
        self._acked_digest: dict[str, str] = {}  # uuid -> digest the sign acked
        self._sleep_until: dict[str, float] = {}  # uuid -> epoch the queued sleep ends
        self._warned_unbound: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._ready.is_set()

    def attach(self, ctx: _Context) -> None:
        self._ctx = ctx

    def start(self, timeout: float = 5.0) -> bool:
        """Bind the listener on a daemon thread. Returns whether it is up."""
        if self._thread is not None and self._thread.is_alive():
            return self._ready.is_set()
        self.start_error = None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="visionect-listener", daemon=True)
        self._thread.start()
        background_loops.track(self)
        # Return as soon as the bind succeeds or the thread dies (port busy),
        # not after the full timeout: register() runs on every rebuild.
        deadline = time.monotonic() + timeout
        while not self._ready.is_set() and self._thread.is_alive():
            if time.monotonic() >= deadline:
                break
            self._ready.wait(0.05)
        return self._ready.is_set()

    def stop(self, timeout: float = 5.0) -> None:
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:
            self.start_error = str(exc)
            logger.warning("visionect: listener stopped: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None
            self._ready.clear()

    async def _serve(self) -> None:
        from pyvisionect.io import VisionectServer
        from pyvisionect.session import DeviceStateStore

        self._stop_event = asyncio.Event()
        self._store = DeviceStateStore()
        self._server = VisionectServer(
            on_events=self._on_events, store=self._store, host=self.host, port=self.port
        )
        await self._server.start()
        logger.info("visionect: listening for signs on %s:%d", self.host, self.port)
        self._ready.set()
        try:
            while not self._stop_event.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=TICK_S)
                if not self._stop_event.is_set():
                    await self._tick()
        finally:
            # close() waits for live clients on 3.12 without close_clients(),
            # and a sign holds its socket open; never let shutdown hang.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.close(abort=True), timeout=3.0)

    # -- push side (called from the push thread) ---------------------------

    def on_push(self, _result: Any = None) -> None:
        """PushManager listener: hand every visionect device to the loop,
        which re-reads its latest render and queues it if it changed."""
        for device in self._visionect_devices():
            self.request_delivery(device.id)

    def request_delivery(self, device_id: str, *, force: bool = False) -> Future[None] | None:
        loop = self._loop
        if loop is None or not self.running:
            return None
        return asyncio.run_coroutine_threadsafe(self._deliver(device_id, force=force), loop)

    # -- device lookup -----------------------------------------------------

    def _visionect_devices(self) -> list[Device]:
        ctx = self._ctx
        if ctx is None:
            return []
        return [d for d in ctx.devices.devices.values() if d.kind_of == KIND]

    def _device_cfg(self, device_id: str) -> dict[str, Any]:
        ctx = self._ctx
        if ctx is None:
            return {}
        section = ctx.settings.get_section("devices") or {}
        cfg = section.get(device_id) if isinstance(section, dict) else None
        return dict(cfg) if isinstance(cfg, dict) else {}

    def device_uuid(self, device_id: str) -> str:
        return normalise_uuid(self._device_cfg(device_id).get("uuid"))

    def _device_for_uuid(self, uuid: str) -> Device | None:
        for device in self._visionect_devices():
            if self.device_uuid(device.id) == uuid:
                return device
        return None

    def _bind_unknown(self, uuid: str) -> Device | None:
        """Bind a sign we don't know to the first visionect device with no
        UUID, persisting it so the binding survives a restart."""
        ctx = self._ctx
        if ctx is None:
            return None
        for device in sorted(self._visionect_devices(), key=lambda d: d.id):
            cfg = self._device_cfg(device.id)
            if normalise_uuid(cfg.get("uuid")):
                continue
            cfg["uuid"] = uuid
            ctx.settings.patch_section("devices", {device.id: cfg})
            logger.info("visionect: bound sign %s to device %s", uuid, device.id)
            return device
        return None

    # -- loop side ---------------------------------------------------------

    async def _deliver(self, device_id: str, *, force: bool = False) -> None:
        try:
            await self._deliver_inner(device_id, force=force)
        except Exception:
            logger.exception("visionect: delivery failed for %s", device_id)

    async def _deliver_inner(self, device_id: str, *, force: bool) -> None:
        ctx = self._ctx
        uuid = self.device_uuid(device_id)
        if ctx is None or not uuid or self._store is None:
            return
        latest = ctx.latest_render_fn(device_id)
        if not latest:
            return
        digest = str(latest.get("digest") or "")
        if not digest or (not force and self._queued_digest.get(uuid) == digest):
            return
        filename = str(latest.get("filename") or f"{digest}.{latest.get('ext') or 'png'}")
        path = ctx.renders_dir / filename
        if not path.exists():
            logger.warning("visionect %s: render %s missing on disk", device_id, filename)
            return
        device_key = uuid_bytes(uuid)
        state = self._store.get(device_key)
        if state.last_status is None:
            # The panel geometry comes from the sign's own DisplayType; wait
            # for it to connect (DeviceConnected delivers then).
            return
        loop = asyncio.get_running_loop()
        frame = await loop.run_in_executor(None, _encode, path, state.panel)
        self._queued_digest[uuid] = digest
        reported = state.last_status.display_state_crc if state.last_status else None
        if reported is not None and reported == frame.state_checksum:
            # Already on the glass (a Tesserae restart, or the same frame
            # again): an e-ink redraw for nothing is a visible flash.
            logger.debug("visionect %s: sign already shows %s", device_id, digest[:12])
            return
        self._store.queue(device_key).set_image(frame, now=time.monotonic())
        logger.info("visionect %s: queued frame %s for sign %s", device_id, digest[:12], uuid)
        await self._apply(uuid)

    async def _apply(self, uuid: str) -> None:
        """Put queued work on the wire if the sign is connected. Quiet-hours
        sleep is added last so the image lands before the sign leaves."""
        if self._server is None or self._store is None:
            return
        device_key = uuid_bytes(uuid)
        conn = self._server.connection_for(device_key)
        if conn is None:
            return
        self._maybe_queue_sleep(uuid)
        sent = conn.apply_pending(now=time.monotonic())
        if not sent:
            return
        image_id = sent.get("image")
        if image_id is not None:
            self._inflight[int(image_id)] = (uuid, self._queued_digest.get(uuid, ""))
        with contextlib.suppress(KeyError):
            await self._server.flush(device_key)

    async def _tick(self) -> None:
        """Once a minute: send connected signs to sleep when their quiet
        window (with sleep-through) has started."""
        if self._server is None:
            return
        for device_key in list(self._server.connections):
            uuid = normalise_uuid(device_key.hex())
            device = self._device_for_uuid(uuid)
            if device is None or self._sleep_minutes(device) is None:
                continue
            if self._sleep_until.get(uuid, 0.0) > time.time():
                continue
            await self._apply(uuid)

    def _sleep_minutes(self, device: Device) -> int | None:
        """Minutes until this device's quiet window ends, when it is inside a
        window that asked for sleep-through; else None."""
        ctx = self._ctx
        if ctx is None:
            return None
        from app.quiet_hours import quiet_ends_at, resolve_quiet_hours
        from app.tz_resolve import app_timezone

        with ctx.app.app_context():
            window = resolve_quiet_hours(ctx.settings.get_section("app") or {}, device)
            if window is None or not window.sleep_through:
                return None
            now = datetime.now(UTC)
            ends = quiet_ends_at(window, now, app_timezone())
        if ends is None or ends <= now:
            return None
        minutes = math.ceil((ends - now).total_seconds() / 60) + SLEEP_MARGIN_MINUTES
        return max(1, min(minutes, MAX_SLEEP_MINUTES))

    def _maybe_queue_sleep(self, uuid: str) -> None:
        device = self._device_for_uuid(uuid)
        if device is None or self._store is None:
            return
        minutes = self._sleep_minutes(device)
        if minutes is None or self._sleep_until.get(uuid, 0.0) > time.time():
            return
        self._store.queue(uuid_bytes(uuid)).sleep(minutes, now=time.monotonic())
        self._sleep_until[uuid] = time.time() + minutes * 60
        logger.info("visionect: quiet hours, sign %s sleeps for %d min", uuid, minutes)

    async def _on_events(self, conn: Any, events: list[Any]) -> None:
        """pyvisionect callback, on the loop. The library doesn't catch
        exceptions from here, so the whole body is guarded."""
        try:
            for event in events:
                await self._handle_event(conn, event)
        except Exception:
            logger.exception("visionect: error handling sign events")

    async def _handle_event(self, conn: Any, event: Any) -> None:
        from pyvisionect.session import Acked, DeviceConnected, Nacked, StatusReceived

        uuid = normalise_uuid(event.uuid or (conn.device_id.hex() if conn.device_id else ""))
        if not uuid:
            return
        if isinstance(event, DeviceConnected):
            device = self._device_for_uuid(uuid) or self._bind_unknown(uuid)
            if device is None:
                if uuid not in self._warned_unbound:
                    self._warned_unbound.add(uuid)
                    logger.warning(
                        "visionect: sign %s connected but no Visionect device is free to "
                        "bind it; add one in Settings -> Devices",
                        uuid,
                    )
                return
            self._sleep_until.pop(uuid, None)
            await self._record_status(device, event.status)
            state = self._store.get(uuid_bytes(uuid)) if self._store is not None else None
            # A fresh listener (or a sign showing something else) re-asserts
            # the latest frame; otherwise just drain whatever is queued.
            if state is None or state.pushed_checksum is None or state.in_sync is False:
                await self._deliver(device.id, force=True)
            else:
                await self._apply(uuid)
        elif isinstance(event, StatusReceived):
            device = self._device_for_uuid(uuid)
            if device is not None:
                await self._record_status(device, event.status)
                await self._apply(uuid)
        elif isinstance(event, Acked):
            if self._store is not None:
                self._store.queue(uuid_bytes(uuid)).on_acked(event.packet_id)
            done = self._inflight.pop(event.packet_id, None)
            if done is not None:
                self._acked_digest[uuid] = done[1]
                logger.info("visionect: sign %s acked frame %s", uuid, done[1][:12])
        elif isinstance(event, Nacked):
            if self._store is not None:
                self._store.queue(uuid_bytes(uuid)).on_nacked(
                    event.packet_id, charging=event.charging
                )
            if self._inflight.pop(event.packet_id, None) is not None:
                # The image stays queued; it goes again on the next contact.
                level = logging.DEBUG if event.charging else logging.WARNING
                logger.log(level, "visionect: sign %s refused a frame", uuid)

    async def _record_status(self, device: Device, status: Any) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        heartbeat = status_heartbeat(
            status.fields(),
            uuid=self.device_uuid(device.id),
            connect_reason=str(status.connect_reason_name),
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _record_heartbeat, ctx.app, device, heartbeat)


def _encode(path: Path, panel: Any) -> Any:
    """Encode a renderer artifact for the sign (executor thread)."""
    from PIL import Image
    from pyvisionect.imaging import encode_frame
    from pyvisionect.imaging.panel import Panel as ImagingPanel

    imaging_panel = ImagingPanel.adapt(panel)
    with Image.open(path) as img:
        canvas = img.convert("L")
    want = (imaging_panel.canvas_width, imaging_panel.canvas_height)
    if canvas.size != want:
        canvas = canvas.resize(want)
    return encode_frame(
        canvas, panel=imaging_panel, encoding=ENCODING_4BPP, dithering=DITHERING_NONE
    )


def _record_heartbeat(app: Flask, device: Device, heartbeat: dict[str, Any]) -> None:
    events = app.config.get("EVENT_LOG")
    status_cache = app.config.get("DEVICE_STATUS")
    if events is None or status_cache is None:
        return
    from app.transport_wiring import record_status_heartbeat

    with app.app_context():
        record_status_heartbeat(
            app=app,
            device=device,
            payload=json.dumps(heartbeat).encode("utf-8"),
            status_cache=status_cache,
            event_log=events,
            event_target=f"visionect://{device.id}/status",
        )


# -- wiring ---------------------------------------------------------------

_SERVICE: VisionectService | None = None
_SERVICE_LOCK = threading.Lock()


def configured_port(settings: Any) -> int:
    raw: Any = os.environ.get("TESSERAE_VISIONECT_PORT")
    if not raw:
        try:
            raw = (settings.get_section("app") or {}).get("visionect_port")
        except Exception:
            raw = None
    try:
        port = int(raw) if raw not in (None, "") else DEFAULT_PORT
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def service() -> VisionectService | None:
    return _SERVICE


def _under_pytest(app: Flask) -> bool:
    return bool(app.testing or app.config.get("TESTING") or os.environ.get("PYTEST_CURRENT_TEST"))


def register(app: Flask) -> None:
    """Wire the listener to the (possibly rebuilt) push manager. Starts it
    when a visionect device exists, stops it when none does. No-op without
    a push manager (bare test setups)."""
    global _SERVICE
    push_mgr = app.config.get("PUSH_MANAGER")
    devices = app.config.get("DEVICE_REGISTRY")
    settings = app.config.get("SETTINGS_STORE")
    renders_dir = app.config.get("RENDERS_DIR")
    if push_mgr is None or devices is None or settings is None or renders_dir is None:
        return
    wanted = any(d.kind_of == KIND for d in devices.devices.values())
    with _SERVICE_LOCK:
        svc = _SERVICE
        if svc is None:
            if not wanted:
                return
            svc = VisionectService(port=configured_port(settings))
            _SERVICE = svc
        svc.attach(
            _Context(
                app=app,
                devices=devices,
                settings=settings,
                renders_dir=Path(renders_dir),
                latest_render_fn=push_mgr.latest_render_for,
            )
        )
        push_mgr.add_listener(svc.on_push)
        app.config["VISIONECT_SERVICE"] = svc
        if _under_pytest(app):
            return
        if wanted and not svc.running:
            if not svc.start():
                logger.warning(
                    "visionect: could not listen on port %d (%s); is the old Visionect "
                    "server still running?",
                    svc.port,
                    svc.start_error or "timed out",
                )
        elif not wanted and svc.running:
            svc.stop()
