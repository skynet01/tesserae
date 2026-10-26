"""Drive Visionect e-paper signs directly, without the Visionect Software Suite.

A Visionect sign (the 32" Place & Play, Joan, the 13" boards) is a thin
client. It dials the server address stored in its flash on TCP 11113 and
speaks first; the server pushes it pre-encoded 4-bit framebuffers. There is no
handshake and no authentication: the 16-byte UUID in the first status packet
is the sign's whole identity. The wire protocol comes from ``pyvisionect``
(MIT, a clean-room reimplementation of the vendor gateway), so this module only
glues it to Tesserae:

* One process-wide listener (``pyvisionect.io.VisionectServer``) runs on its own
  asyncio loop in a daemon thread, like ``app.embedded_broker``. It runs while
  a ``visionect`` device exists. ``register()`` re-runs on every transport
  rebuild: it re-attaches the push listener, rebinds signs and restarts the
  listener only when the configured port changed.
* Each sign has a small state machine, run one pass at a time
  (``_reconcile``): encode the device's latest renderer artifact
  (``trmnl_png_gray16``, already fitted, flipped and dithered onto the
  sign's 16 greys), send it if the sign hasn't acked that frame, and only once
  it has, send a quiet-hours deep sleep. At most one image and one sleep are
  ever in flight, and both are forgotten when the sign reconnects. A NACK
  retries after a back-off.
* Status packets feed ``record_status_heartbeat``, so the device card, battery
  history and Home Assistant sensors work as for any other device.
* An unknown sign is bound to a visionect device with a blank UUID, or listed
  under Settings -> Devices as a discovered device to register.

What the sign shows is tracked from its ACKs. Its own ``DisplayStateCRC`` is
only trusted before the first ACK of a run (a Tesserae restart): firmware
6.17 reports the new value at its next status, up to 30 minutes later, so a
CRC read after a push is usually stale.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
# How often the loop re-runs every connected sign (quiet-hours sleep, retries).
TICK_S = 60.0
# Retry delays after the sign refuses a frame; a charging refusal is normal.
NACK_RETRY_S = 60.0
NACK_CHARGING_RETRY_S = 300.0
# Gap between attempts to bind the port after a failure (a timer retries).
START_RETRY_S = 30.0
# How long to hold off sleep while a size change repaints the frame.
REPAINT_WAIT_S = 300.0
# Firmware 6.17 reports the new DisplayStateCRC only at its next status,
# up to 30 minutes after the ACK; only after this long is a mismatch real.
CRC_CONVERGE_S = 40 * 60.0
# The longest single deep sleep we ask for, and the margin past the window's
# end so the sign wakes to the day's first frame.
MAX_SLEEP_MINUTES = 6 * 24 * 60
SLEEP_MARGIN_MINUTES = 2
# pyvisionect.imaging.constants Encoding.FOUR_BIT and Dithering.NONE. The
# renderer has already dithered onto the 16-grey ramp, and NONE keeps n*17
# levels exact.
ENCODING_4BPP = 4
DITHERING_NONE = 1
DISCOVERY_PREFIX = "visionect_"


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


def discovery_id(uuid: str) -> str:
    """Short, stable device id for an unregistered sign (fits DEVICE_ID_RE)."""
    return DISCOVERY_PREFIX + hashlib.blake2s(uuid.encode("ascii"), digest_size=3).hexdigest()


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


def artifact_path(renders_dir: Path, latest: dict[str, Any]) -> Path | None:
    """The renderer artifact a ``latest_render_for`` entry points at, kept
    inside ``renders_dir`` whatever the entry says."""
    digest = str(latest.get("digest") or "")
    name = str(
        latest.get("filename") or (f"{digest}.{latest.get('ext') or 'png'}" if digest else "")
    )
    name = Path(name).name
    if not name or name in (".", ".."):
        return None
    return renders_dir / name


@dataclass(frozen=True)
class _Context:
    app: Flask
    devices: DeviceRegistry
    settings: Any
    renders_dir: Path
    latest_render_fn: Callable[[str], dict[str, Any] | None]


@dataclass
class _Sign:
    """Loop-thread state for one sign UUID."""

    uuid: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dirty: bool = False
    frame: Any = None  # EncodedFrame for frame_digest
    frame_digest: str = ""
    shown_digest: str = ""  # acked by the sign during this run
    shown_checksum: int | None = None  # its state checksum (status tag 9)
    shown_at: float = 0.0  # wall clock of that ACK
    crc_corrected: bool = False  # one corrective resend per mismatch
    image_inflight: tuple[int, str, int] | None = None  # (packet id, digest, checksum)
    sleep_inflight: tuple[int, int] | None = None  # (packet id, minutes)
    retry_at: float = 0.0  # monotonic; after a refused frame
    sleep_retry_at: float = 0.0  # monotonic; after a refused sleep
    retry_handle: asyncio.TimerHandle | None = None
    repaint_until: float = 0.0  # monotonic; a size change is repainting
    conformed: bool = False  # panel conformance tried this run
    asleep_until: float = 0.0
    remote: str = ""
    connected_at: float = 0.0
    last_seen: float = 0.0
    last_error: str = ""


class VisionectService:
    """The listener plus the glue between it and the push pipeline."""

    def __init__(self, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self.start_error: str | None = None
        self.autostart = True
        self._ctx: _Context | None = None
        self._ctx_gen = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Any = None  # pyvisionect.io.VisionectServer
        self._store: Any = None  # pyvisionect.session.DeviceStateStore
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._last_start_attempt = 0.0
        # Touched on the loop thread only.
        self._signs: dict[str, _Sign] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._warned_unbound: set[str] = set()
        self._retry_timer: threading.Timer | None = None
        # Read by Flask request threads; written on the loop thread.
        self._snapshot_lock = threading.Lock()
        self._snapshot: dict[str, dict[str, Any]] = {}

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._ready.is_set()

    def attach(self, ctx: _Context) -> None:
        """Point the service at a (possibly rebuilt) app. Signs already
        connected are rebound and re-run, so a device registered while its
        sign waits gets its frame without waiting for the next check-in."""
        self._ctx = ctx
        self._ctx_gen += 1
        self._call_soon(self._rescan)

    def start(self, timeout: float = 5.0) -> bool:
        """Bind the listener on a daemon thread. Returns whether it is up."""
        if self._thread is not None and self._thread.is_alive():
            return self._ready.is_set()
        self._last_start_attempt = time.monotonic()
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

    def maybe_start(self) -> bool:
        """Start if wanted and not running, at most every START_RETRY_S, so a
        port held by the old Visionect server is retried without hammering."""
        if self.running or not self.autostart or not self._visionect_devices():
            return self.running
        if time.monotonic() - self._last_start_attempt < START_RETRY_S:
            return False
        ok = self.start()
        if not ok:
            logger.warning(
                "visionect: can't listen on port %d (%s); retrying. Is the old Visionect "
                "server still running?",
                self.port,
                self.start_error or "timed out",
            )
            self._arm_retry()
        return ok

    def _arm_retry(self) -> None:
        """Retry the bind on a timer, independent of pushes, until it works
        or no Visionect device is left."""
        timer = self._retry_timer
        if timer is not None and timer.is_alive():
            return
        timer = threading.Timer(START_RETRY_S, self._retry_fire)
        timer.daemon = True
        self._retry_timer = timer
        timer.start()

    def _retry_fire(self) -> None:
        self._retry_timer = None
        self._last_start_attempt = 0.0
        self.maybe_start()

    def stop(self, timeout: float = 5.0) -> None:
        timer, self._retry_timer = self._retry_timer, None
        if timer is not None:
            timer.cancel()
        loop, stop_event = self._loop, self._stop_event
        if loop is not None and stop_event is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
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
            logger.debug("visionect: listener stopped: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()
            self._loop = None
            self._ready.clear()
            self._signs.clear()
            with self._snapshot_lock:
                self._snapshot.clear()

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
                    self._rescan()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        """Close the listener and every sign's socket, then the tasks.

        A sign holds its socket open, and on Python < 3.13 asyncio.Server has
        no close_clients()/abort_clients(), so wait_closed() would wait for
        it forever: abort the transports ourselves first."""
        server = self._server
        writers = getattr(server, "_writers", None)
        if isinstance(writers, dict):
            for writer in list(writers.values()):
                with contextlib.suppress(Exception):
                    writer.transport.abort()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.close(abort=True), timeout=3.0)
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=3.0
                )

    def _call_soon(self, fn: Callable[[], None]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or not self.running:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(fn)

    # -- push side (called from the push thread) ---------------------------

    def on_push(self, _result: Any = None) -> None:
        """PushManager listener: re-run every bound sign on the loop, which
        re-reads its latest render and sends it if it changed."""
        if not self.running:
            self.maybe_start()
            return
        self._call_soon(self._rescan)

    # -- device lookup -----------------------------------------------------

    def _visionect_devices(self) -> list[Device]:
        ctx = self._ctx
        if ctx is None:
            return []
        return visionect_devices(ctx.devices)

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

    def _bind(self, uuid: str) -> Device | None:
        """Bind a sign we don't know to the first visionect device (by id)
        with no UUID. Persisted so the binding survives a restart; a device
        registered from the discovered banner already carries its UUID."""
        ctx = self._ctx
        if ctx is None:
            return None
        blank = [
            d
            for d in sorted(self._visionect_devices(), key=lambda d: d.id)
            if not self.device_uuid(d.id)
        ]
        if not blank:
            return None
        chosen = blank[0]
        cfg = self._device_cfg(chosen.id)
        cfg["uuid"] = uuid
        ctx.settings.patch_section("devices", {chosen.id: cfg})
        self._forget_discovery(uuid)
        logger.info("visionect: bound sign %s to device %s", uuid, chosen.id)
        return chosen

    def _announce_unknown(self, uuid: str, status: Any) -> None:
        """List an unbound sign under Settings -> Devices -> discovered."""
        ctx = self._ctx
        cache = ctx.app.config.get("DISCOVERY_CACHE") if ctx is not None else None
        if cache is None:
            return
        state = self._store.get(uuid_bytes(uuid)) if self._store is not None else None
        width, height = (canvas_size(state.panel) if state is not None else None) or (1440, 2560)
        beat = status_heartbeat(
            status.fields(), uuid=uuid, connect_reason=str(status.connect_reason_name)
        )
        payload: dict[str, Any] = {
            "kind": self._model_for((width, height)),
            "transport": "visionect",
            "uuid": uuid,
            "name": "Visionect sign",
            "panel_w": width,
            "panel_h": height,
            "ip": self._signs[uuid].remote if uuid in self._signs else "",
        }
        if beat.get("fw_version"):
            payload["fw_version"] = beat["fw_version"]
        cache.record(discovery_id(uuid), json.dumps(payload).encode("utf-8"))

    def _model_for(self, canvas: tuple[int, int]) -> str:
        """The catalog model whose native size is the sign's, when exactly
        one is (a Joan 6, the 32"); else the generic kind. Preselects the
        Register picker; the panel conforms to the sign either way."""
        ctx = self._ctx
        if ctx is None:
            return KIND
        models = [
            d.id
            for d in list(ctx.devices.devices.values())
            if d.kind_of is None
            and d.id != KIND
            and is_visionect_device(d)
            and native_size(d) == canvas
        ]
        return models[0] if len(models) == 1 else KIND

    def _forget_discovery(self, uuid: str) -> None:
        ctx = self._ctx
        cache = ctx.app.config.get("DISCOVERY_CACHE") if ctx is not None else None
        if cache is not None:
            cache.forget(discovery_id(uuid))

    # -- loop side ---------------------------------------------------------

    def _sign(self, uuid: str) -> _Sign:
        sign = self._signs.get(uuid)
        if sign is None:
            sign = _Sign(uuid=uuid)
            self._signs[uuid] = sign
        return sign

    def _rescan(self) -> None:
        """Re-run every connected sign, binding any that still need a device."""
        if self._server is None:
            return
        live = list(self._server.connections)
        for device_key in live:
            uuid = normalise_uuid(device_key.hex())
            if not uuid:
                continue
            if self._device_for_uuid(uuid) is None and self._bind(uuid) is None:
                continue
            self._schedule(uuid)
        # A sign that dropped its socket raises no event; refresh its card.
        gone = {normalise_uuid(k.hex()) for k in live}
        for uuid, sign in list(self._signs.items()):
            if uuid not in gone:
                self._publish(sign)

    def _schedule(self, uuid: str, *, delay: float = 0.0) -> None:
        sign = self._sign(uuid)
        if delay > 0:
            if sign.retry_handle is not None:
                sign.retry_handle.cancel()
            loop = asyncio.get_running_loop()
            sign.retry_handle = loop.call_later(delay, self._schedule, uuid)
            return
        if sign.lock.locked():
            sign.dirty = True
            return
        task = asyncio.get_running_loop().create_task(self._reconcile(sign))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reconcile(self, sign: _Sign) -> None:
        """One pass at a time per sign; a request during a pass re-runs it."""
        async with sign.lock:
            while True:
                sign.dirty = False
                try:
                    await self._reconcile_once(sign)
                except Exception:
                    logger.exception("visionect: reconcile failed for %s", sign.uuid)
                self._publish(sign)
                if not sign.dirty:
                    return

    async def _reconcile_once(self, sign: _Sign) -> None:
        ctx, gen = self._ctx, self._ctx_gen
        device = self._device_for_uuid(sign.uuid)
        if ctx is None or device is None or self._store is None or self._server is None:
            return
        device_key = uuid_bytes(sign.uuid)
        state = self._store.get(device_key)
        loop = asyncio.get_running_loop()

        # 0. The sign's own DisplayType is the truth about its buffer (a
        #    Joan 6 is 1024x758, the 32" is 4 x 1440x640): conform the
        #    device's native size to it and re-render before sending.
        if state.last_status is not None and not sign.conformed:
            canvas = canvas_size(state.panel)
            if canvas is not None and native_size(device) != canvas:
                sign.conformed = True  # once per run: never a repaint loop
                changed = await loop.run_in_executor(
                    None,
                    conform_panel,
                    device,
                    canvas,
                    kind_default_dims(ctx.devices, device),
                    functools.partial(ctx.devices.devices.get, device.id),
                )
                if changed:
                    sign.frame, sign.frame_digest = None, ""
                    sign.repaint_until = time.monotonic() + REPAINT_WAIT_S
                    push_mgr = ctx.app.config.get("PUSH_MANAGER")
                    if push_mgr is not None:
                        await loop.run_in_executor(
                            None,
                            functools.partial(
                                push_mgr.invalidate_and_repaint, device.id, reencode=False
                            ),
                        )
                    return  # the repaint's push re-runs us

        latest = ctx.latest_render_fn(device.id)
        digest = str((latest or {}).get("digest") or "")

        # 1. Have the latest frame encoded. Geometry comes from the sign's
        #    own DisplayType, so wait for its first status.
        if latest and digest and digest != sign.frame_digest and state.last_status is not None:
            path = artifact_path(ctx.renders_dir, latest)
            if path is None or not path.exists():
                sign.last_error = "render missing on disk"
                logger.warning("visionect %s: render %s missing on disk", device.id, path)
                return
            frame = await loop.run_in_executor(None, _encode, path, state.panel)
            current = ctx.latest_render_fn(device.id) or {}
            if (
                self._ctx_gen != gen
                or str(current.get("digest") or "") != digest
                or self._device_for_uuid(sign.uuid) is not device
            ):
                sign.dirty = True  # superseded while encoding; start over
                return
            sign.frame, sign.frame_digest = frame, digest
            sign.repaint_until = 0.0
            sign.last_error = ""
            reported = state.last_status.display_state_crc
            if not sign.shown_digest and reported == frame.state_checksum:
                # Before our first ACK this run (a restart) the sign's CRC is
                # the best evidence: it already shows this frame.
                sign.shown_digest = digest

        conn = self._server.connection_for(device_key)
        if conn is None or sign.image_inflight is not None or sign.sleep_inflight is not None:
            return

        # 2. Send the frame the sign hasn't acked, one at a time.
        if sign.frame is not None and sign.frame_digest != sign.shown_digest:
            wait = sign.retry_at - time.monotonic()
            if wait > 0:
                self._schedule(sign.uuid, delay=wait)
                return
            packet_id = int(conn.send_image(sign.frame))
            sign.image_inflight = (packet_id, sign.frame_digest, int(sign.frame.state_checksum))
            logger.info(
                "visionect %s: sending frame %s to sign %s",
                device.id,
                sign.frame_digest[:12],
                sign.uuid,
            )
            await self._flush(device_key)
            return  # the ACK re-runs us; sleep must wait for it

        # 3. Only with the latest frame on the glass: quiet-hours sleep, once
        #    (the sign takes a moment to drop the socket after acking it).
        if sign.asleep_until > time.time():
            return
        if sign.frame is None and sign.repaint_until > time.monotonic():
            return  # the size change's repaint hasn't produced a frame yet
        wait = sign.sleep_retry_at - time.monotonic()
        if wait > 0:
            self._schedule(sign.uuid, delay=wait)
            return
        minutes = self._sleep_minutes(device)
        if minutes is not None:
            packet_id = int(conn.sleep(minutes))
            sign.sleep_inflight = (packet_id, minutes)
            logger.info("visionect: quiet hours, sign %s sleeps for %d min", sign.uuid, minutes)
            await self._flush(device_key)

    async def _flush(self, device_key: bytes) -> None:
        if self._server is None:
            return
        with contextlib.suppress(KeyError, ConnectionError, OSError):
            await self._server.flush(device_key)

    def _sleep_minutes(self, device: Device) -> int | None:
        """Minutes until this device's quiet window ends, when it is inside a
        window that asked for sleep-through; else None. Recomputed every time,
        so a sleep refused at night is never replayed in the morning."""
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

    async def _on_events(self, conn: Any, events: list[Any]) -> None:
        """pyvisionect callback, on the loop. Updates state synchronously and
        schedules work as tasks: awaiting here would stall the sign's read
        loop, so its ACKs couldn't arrive. The library doesn't catch
        exceptions from here, so the whole body is guarded."""
        try:
            touched: set[str] = set()
            for event in events:
                uuid = self._handle_event(conn, event)
                if uuid:
                    touched.add(uuid)
            for uuid in touched:
                if self._device_for_uuid(uuid) is not None:
                    self._schedule(uuid)
        except Exception:
            logger.exception("visionect: error handling sign events")

    def _handle_event(self, conn: Any, event: Any) -> str:
        """Apply one event; return the UUID to re-run, or ""."""
        from pyvisionect.session import Acked, DeviceConnected, Nacked, StatusReceived

        uuid = normalise_uuid(event.uuid or (conn.device_id.hex() if conn.device_id else ""))
        if not uuid:
            return ""
        sign = self._sign(uuid)
        now = time.monotonic()
        if isinstance(event, DeviceConnected):
            # A new connection: whatever was in flight on the old one is lost.
            sign.image_inflight = None
            sign.sleep_inflight = None
            sign.asleep_until = 0.0
            sign.connected_at = time.time()
            sign.remote = self._peer(uuid)
            device = self._device_for_uuid(uuid) or self._bind(uuid)
            self._check_glass(sign, event.status)
            if device is None:
                self._announce_unknown(uuid, event.status)
                if uuid not in self._warned_unbound:
                    self._warned_unbound.add(uuid)
                    logger.warning(
                        "visionect: sign %s connected but no Visionect device is free; "
                        "it is listed under Settings -> Devices to register",
                        uuid,
                    )
                self._publish(sign)
                return ""
            return uuid
        if isinstance(event, StatusReceived):
            sign.last_seen = time.time()
            self._check_glass(sign, event.status)
            device = self._device_for_uuid(uuid)
            if device is not None:
                self._spawn(self._record_status(device, uuid, event.status))
            self._publish(sign)
            return uuid
        if isinstance(event, Acked):
            if sign.image_inflight and sign.image_inflight[0] == event.packet_id:
                sign.shown_digest = sign.image_inflight[1]
                sign.shown_checksum = sign.image_inflight[2]
                sign.shown_at = time.time()
                sign.image_inflight = None
                sign.retry_at = 0.0
                sign.last_error = ""
                logger.info("visionect: sign %s acked frame %s", uuid, sign.shown_digest[:12])
            elif sign.sleep_inflight and sign.sleep_inflight[0] == event.packet_id:
                sign.asleep_until = time.time() + sign.sleep_inflight[1] * 60
                sign.sleep_inflight = None
            return uuid
        if isinstance(event, Nacked):
            if sign.image_inflight and sign.image_inflight[0] == event.packet_id:
                sign.image_inflight = None
                delay = NACK_CHARGING_RETRY_S if event.charging else NACK_RETRY_S
                sign.retry_at = now + delay
                sign.last_error = (
                    "refused a frame (charging)" if event.charging else "refused a frame"
                )
                level = logging.DEBUG if event.charging else logging.WARNING
                logger.log(level, "visionect: sign %s refused a frame; retrying", uuid)
            elif sign.sleep_inflight and sign.sleep_inflight[0] == event.packet_id:
                # Back off like a refused frame; the duration is recomputed then.
                sign.sleep_inflight = None
                delay = NACK_CHARGING_RETRY_S if event.charging else NACK_RETRY_S
                sign.sleep_retry_at = now + delay
            return uuid
        return ""

    def _check_glass(self, sign: _Sign, status: Any) -> None:
        """Re-send when the sign reports showing something other than the
        frame it acked: its framebuffer changed while we weren't looking.
        Only once its CRC has had time to converge (firmware 6.17 reports it
        a status late), and only one corrective resend per mismatch, so a
        sign whose CRC never matches can't cause a redraw loop."""
        reported = getattr(status, "display_state_crc", None)
        if reported is None or sign.shown_checksum is None or sign.image_inflight:
            return
        if reported == sign.shown_checksum:
            sign.crc_corrected = False
            return
        if time.time() - sign.shown_at < CRC_CONVERGE_S or sign.crc_corrected:
            return
        sign.crc_corrected = True
        logger.info(
            "visionect: sign %s reports state %s, not the acked %s; re-sending",
            sign.uuid,
            reported,
            sign.shown_checksum,
        )
        sign.shown_digest = ""

    def _peer(self, uuid: str) -> str:
        writers = getattr(self._server, "_writers", None)
        writer = writers.get(uuid_bytes(uuid)) if isinstance(writers, dict) else None
        peer = writer.get_extra_info("peername") if writer is not None else None
        return str(peer[0]) if isinstance(peer, tuple) and peer else ""

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _record_status(self, device: Device, uuid: str, status: Any) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        heartbeat = status_heartbeat(
            status.fields(), uuid=uuid, connect_reason=str(status.connect_reason_name)
        )
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _record_heartbeat, ctx.app, device, heartbeat)
        except Exception:
            logger.exception("visionect: recording status for %s failed", device.id)

    # -- UI snapshot (read from request threads) ---------------------------

    def _publish(self, sign: _Sign) -> None:
        connected = (
            self._server is not None
            and self._server.connection_for(uuid_bytes(sign.uuid)) is not None
        )
        view = {
            "uuid": sign.uuid,
            "connected": connected,
            "remote": sign.remote,
            "connected_at": sign.connected_at or None,
            "last_seen": sign.last_seen or None,
            "in_sync": bool(sign.frame_digest) and sign.frame_digest == sign.shown_digest,
            "sending": sign.image_inflight is not None,
            "asleep_until": sign.asleep_until or None,
            "last_error": sign.last_error,
        }
        with self._snapshot_lock:
            self._snapshot[sign.uuid] = view

    def sign_view(self, uuid: str) -> dict[str, Any] | None:
        with self._snapshot_lock:
            view = self._snapshot.get(normalise_uuid(uuid))
            return dict(view) if view is not None else None


def is_visionect_device(device: Any) -> bool:
    """Whether an instance or kind speaks the Visionect protocol, without a
    registry lookup: catalog SKUs share devices/visionect/device.py, which
    carries the PROTOCOL marker."""
    if getattr(device, "kind_of", None) == KIND or getattr(device, "id", None) == KIND:
        return True
    return getattr(getattr(device, "module", None), "PROTOCOL", None) == KIND


def visionect_devices(devices: DeviceRegistry) -> list[Device]:
    """Every device instance driven over the Visionect protocol."""
    return [d for d in list(devices.devices.values()) if d.kind_of and is_visionect_device(d)]


def canvas_size(panel: Any) -> tuple[int, int] | None:
    """The sign's full canvas (w, h) from its pyvisionect panel geometry."""
    from pyvisionect.imaging.panel import Panel as ImagingPanel

    try:
        imaging_panel = ImagingPanel.adapt(panel)
        return int(imaging_panel.canvas_width), int(imaging_panel.canvas_height)
    except (TypeError, ValueError):
        return None


def native_size(device: Device) -> tuple[int, int] | None:
    block = device.panel or {}
    try:
        return int(block["native_w"]), int(block["native_h"])
    except (KeyError, TypeError, ValueError):
        return None


def kind_default_dims(devices: DeviceRegistry, device: Device) -> tuple[int, int] | None:
    kind = devices.devices.get(str(device.kind_of or ""))
    block = (kind.panel if kind is not None else None) or {}
    try:
        return int(block["w"]), int(block["h"])
    except (KeyError, TypeError, ValueError):
        return None


def conform_panel(
    device: Device,
    canvas: tuple[int, int],
    defaults: tuple[int, int] | None,
    current: Callable[[], Device | None] | None = None,
) -> bool:
    """Set the device's native buffer to the sign's canvas (executor thread).

    Same shape as the TRMNL size adoption: the instance file's panel block
    gets ``native_w / native_h``, and a composition canvas still at the
    kind's defaults (or the old buffer's size) is re-seeded to the new one,
    ordered for the stored orientation. Unlike TRMNL this isn't one-shot:
    the bound sign's DisplayType is authoritative. Returns True when it
    wrote.

    The read and the write happen under the instance-file lock every
    instance-file writer holds (a rename, a panel save, a delete), so
    neither side drops the other's change, and the file is replaced
    atomically, so readers never see half-written JSON and only the size
    keys change. ``current`` returns the registry's device object now, which
    a reload may have replaced; it is patched too. A device deleted, or
    reloaded from another file, since the caller looked it up is left alone
    rather than written back to life."""
    from app.device_service import INSTANCE_FILE_LOCK

    with INSTANCE_FILE_LOCK:
        live = current() if current is not None else device
        if live is None or live.path != device.path or not device.path.exists():
            return False
        return _write_conformed_panel(device, live, canvas, defaults)


def _write_conformed_panel(
    device: Device, live: Device, canvas: tuple[int, int], defaults: tuple[int, int] | None
) -> bool:
    width, height = canvas
    try:
        raw = json.loads(device.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    block = dict(raw.get("panel") or {})
    old_native: tuple[int, int] | None = None
    with contextlib.suppress(KeyError, TypeError, ValueError):
        old_native = (int(block["native_w"]), int(block["native_h"]))
    if old_native == canvas:
        return False
    try:
        cur = (int(block["w"]), int(block["h"]))
    except (KeyError, TypeError, ValueError):
        cur = None
    seeds = {defaults} | ({old_native, old_native[::-1]} if old_native else set())
    if cur is None or cur in seeds:
        short, long = min(width, height), max(width, height)
        portrait = str(block.get("orientation") or "").lower().startswith("portrait")
        block["w"], block["h"] = (short, long) if portrait else (long, short)
    block["native_w"], block["native_h"] = width, height
    raw["panel"] = block
    tmp = device.path.with_name(f".{device.path.name}.visionect.tmp")
    try:
        tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, device.path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    device.manifest["panel"] = dict(block)
    if live is not device:
        live.manifest["panel"] = dict(block)
    logger.info(
        "visionect: device %s now uses the sign's %dx%d buffer (canvas %sx%s)",
        device.id,
        width,
        height,
        block.get("w"),
        block.get("h"),
    )
    return True


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
    when a visionect device exists, stops it when none does, and moves it
    when the configured port changed. No-op without a push manager (bare
    test setups)."""
    global _SERVICE
    push_mgr = app.config.get("PUSH_MANAGER")
    devices = app.config.get("DEVICE_REGISTRY")
    settings = app.config.get("SETTINGS_STORE")
    renders_dir = app.config.get("RENDERS_DIR")
    if push_mgr is None or devices is None or settings is None or renders_dir is None:
        return
    wanted = bool(visionect_devices(devices))
    port = configured_port(settings)
    with _SERVICE_LOCK:
        svc = _SERVICE
        if svc is not None and svc.port != port:
            svc.stop()
            svc = None
        if svc is None:
            if not wanted:
                _SERVICE = None
                return
            svc = VisionectService(port=port)
            _SERVICE = svc
        svc.autostart = not _under_pytest(app)
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
        if not svc.autostart:
            return
        if wanted and not svc.running:
            svc._last_start_attempt = 0.0  # a rebuild is an explicit retry
            svc.maybe_start()
        elif not wanted and svc.running:
            svc.stop()
