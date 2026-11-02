"""Data for one device's battery chart card.

The Device batteries page renders one card per device, and the device page's
Power section embeds the same card for a single device. Both go through
:func:`build_battery_chart`, which returns everything the
``templates/_battery_chart.html`` macros need (see the docstring there) so no
caller re-derives series, wake dots, stats or whether predictions apply.

Wake dots. A "wake" is a moment the device checked in. Two sources feed it:

* every battery sample is a heartbeat, so each one is a wake labelled
  "Check-in" by default (the steady heartbeat the event log deliberately
  skips, see ``transport_wiring.status_changed_meaningfully``);
* ``type="device"`` event-log rows that carry a reason (Visionect
  ``connect_reason``, a REST client's ``wake_reason`` / ``reset_reason``, or a
  detected abnormal reset in ``diag.reset``) name the reason of the heartbeat
  they were written for. The row is matched to the nearest sample within
  :data:`MATCH_TOLERANCE_S`; one with no sample nearby is added as its own wake
  with the line's value interpolated at that instant.

The event log only gets a row when the status changed, so a steady "heartbeat"
or "timer" wake carries no row and stays "Check-in". The raw reason is mapped to
a short label by :func:`reason_label`; an unmapped reason shows its raw text.

mypy --strict applies to this module, see pyproject.toml.
"""

from __future__ import annotations

import bisect
import re
import time
from collections.abc import Iterable, Sequence
from typing import Any

from app import visionect
from app.battery_offset import apply_to_pct
from app.state.battery_history import BatteryHistory, BatteryRow, Prediction
from app.state.event_log import EventLog, EventRow

# At most this many dots per chart. The window is cut into this many equal time
# buckets and each keeps one wake, so a device on a 1-minute cadence costs the
# same as one on an hourly cadence.
MAX_WAKE_DOTS: int = 120
# An event-log row is written just after the sample of the same heartbeat; this
# is how far apart the two may be and still count as one wake.
MATCH_TOLERANCE_S: float = 120.0
DEFAULT_LABEL: str = "Check-in"
# Fields of a parsed heartbeat that name why the device woke or connected, in
# priority order. ``connect_reason`` is Visionect's; the others are what REST
# firmware sends (unknown fields ride along in ``parsed``).
REASON_KEYS: tuple[str, ...] = (
    "connect_reason",
    "wake_reason",
    "wakeup_reason",
    "wake_cause",
    "reset_reason",
    "boot_reason",
)
# Longest raw reason kept, so a garbage field can't bloat the page.
MAX_REASON_CHARS: int = 48

# Normalised reason token -> label. Tokens are lowercased with runs of spaces,
# dashes and slashes folded to "_". Visionect's names come from pyvisionect's
# CONNECT_REASON table; the ESP-IDF ones from its wake-cause / reset-reason names.
_LABELS: dict[str, str] = {
    **dict.fromkeys(
        ("heartbeat", "timer", "timer_wake", "rtc", "deepsleep", "deep_sleep", "scheduled"),
        DEFAULT_LABEL,
    ),
    **dict.fromkeys(("unknown", "n/a", "na", "none", "undefined"), DEFAULT_LABEL),
    "wakeup": "Wake-up",
    "wake": "Wake-up",
    **dict.fromkeys(("button", "ext0", "ext1", "gpio", "gpio_wake", "ext_button"), "Button"),
    **dict.fromkeys(("touch", "touchpad"), "Touch"),
    **dict.fromkeys(("by_server_request", "server_request", "server"), "Server request"),
    "server_changes": "Server changes",
    **dict.fromkeys(("wifi", "wifi_reconnect", "wi_fi_reconnect", "reconnect"), "Wi-Fi reconnect"),
    **dict.fromkeys(("connection_error", "connection_driver_error"), "Reconnect after error"),
    **dict.fromkeys(("poweron", "power_on", "power_on_reset", "por", "cold_boot"), "Power on"),
    **dict.fromkeys(("reboot", "restart", "sw", "software", "software_reset"), "Reboot"),
    "brownout": "Brownout",
    **dict.fromkeys(("panic", "exception", "crash"), "Crash"),
    **dict.fromkeys(("watchdog", "wdt", "int_wdt", "task_wdt"), "Watchdog reset"),
    "logs_full": "Logs full",
    "command_line": "Command line",
    "protocol_error": "Protocol error",
    "display_driver_failed": "Display driver failed",
}
_TOKEN_SEP = re.compile(r"[\s\-/]+")


def is_battery_predictable(device: Any) -> bool:
    """Whether drain / reaches-X predictions are shown for ``device``.

    A Visionect frame shows the chart, the percentage and the sample count only.
    ``None`` (no registry entry) is predictable."""
    return not visionect.is_visionect_device(device)


def reason_label(raw: str | None) -> str:
    """Short readable label for a raw wake / connect reason.

    Known reasons map through :data:`_LABELS`; an empty or uninformative one
    ("unknown", "n/a") is a plain check-in; anything else shows as reported."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_LABEL
    token = _TOKEN_SEP.sub("_", text.lower())
    return _LABELS.get(token, text[:MAX_REASON_CHARS])


def event_reason(extra: dict[str, Any]) -> str | None:
    """The wake / connect reason an event-log row names, or ``None``.

    Looks at the parsed heartbeat first (:data:`REASON_KEYS`), then at an
    abnormal-reset report (``diag.reset``: brownout, panic, watchdog)."""
    parsed = extra.get("parsed")
    if isinstance(parsed, dict):
        for key in REASON_KEYS:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:MAX_REASON_CHARS]
    diag = extra.get("diag")
    if isinstance(diag, dict):
        reset = diag.get("reset")
        if isinstance(reset, str) and reset.strip():
            return reset.strip()[:MAX_REASON_CHARS]
    return None


def extract_wakes(
    series: Sequence[dict[str, Any]], events: Iterable[EventRow]
) -> list[dict[str, Any]]:
    """Every wake in the window, oldest first, before thinning.

    ``series`` is the chart line (``{t_ms, pct}``, oldest first); each point is a
    check-in wake. ``events`` are the device's status-heartbeat rows; each one
    that names a reason sets the label of the nearest wake within
    :data:`MATCH_TOLERANCE_S`, or becomes a wake of its own. Returns
    ``{t_ms, pct, label, reason}`` dicts (``reason`` is the raw text, ``None``
    for a plain check-in). Empty when there is no line to put dots on."""
    if not series:
        return []
    wakes: list[dict[str, Any]] = [
        {"t_ms": p["t_ms"], "pct": p["pct"], "label": DEFAULT_LABEL, "reason": None} for p in series
    ]
    times = [w["t_ms"] for w in wakes]
    tol_ms = MATCH_TOLERANCE_S * 1000.0
    extras: list[dict[str, Any]] = []
    for event in events:
        raw = event_reason(event.extra)
        if raw is None:
            continue
        label = reason_label(raw)
        t_ms = int(event.timestamp * 1000)
        idx = _nearest(times, t_ms)
        if idx is not None and abs(times[idx] - t_ms) <= tol_ms:
            target = wakes[idx]
            # A routine label never overwrites a reason already matched.
            if label != DEFAULT_LABEL or target["reason"] is None:
                target.update(label=label, reason=raw)
            continue
        extras.append(
            {"t_ms": t_ms, "pct": _interpolate(series, t_ms), "label": label, "reason": raw}
        )
    if extras:
        wakes.extend(extras)
        wakes.sort(key=lambda w: w["t_ms"])
    return wakes


def thin_wakes(
    wakes: Sequence[dict[str, Any]],
    *,
    start_ms: int,
    end_ms: int,
    max_dots: int = MAX_WAKE_DOTS,
) -> list[dict[str, Any]]:
    """Cap ``wakes`` at ``max_dots`` by keeping one per equal time bucket.

    A bucket keeps its last wake, except that a notable one (any label other
    than a plain check-in) beats routine check-ins in the same bucket, so a
    Power on does not vanish behind the heartbeat that follows it. Fewer wakes
    than ``max_dots`` pass through untouched."""
    if len(wakes) <= max_dots or max_dots < 1:
        return list(wakes)
    span = max(end_ms - start_ms, 1)
    kept: dict[int, dict[str, Any]] = {}
    for wake in wakes:
        bucket = min(max((wake["t_ms"] - start_ms) * max_dots // span, 0), max_dots - 1)
        held = kept.get(bucket)
        if held is not None and held["label"] != DEFAULT_LABEL and wake["label"] == DEFAULT_LABEL:
            continue
        kept[bucket] = wake
    return [kept[b] for b in sorted(kept)]


def prediction_dict(prediction: Prediction | None) -> dict[str, Any] | None:
    """The prediction fields the templates and JSON endpoint use."""
    if prediction is None:
        return None
    return {
        "slope_per_day": prediction.slope_per_day,
        "is_charging": prediction.is_charging,
        "charge_rate_per_day": prediction.charge_rate_per_day,
        "days_to_20pct": prediction.days_to_20pct,
        "days_to_empty": prediction.days_to_empty,
        "days_to_full": prediction.days_to_full,
        "samples": prediction.samples,
    }


def tone_for(pct: int | None) -> str:
    """``critical`` (10% or less), ``low`` (30% or less) or ``ok``."""
    if pct is not None and pct <= 10:
        return "critical"
    if pct is not None and pct <= 30:
        return "low"
    return "ok"


def load_wake_events(
    event_log: EventLog | None, device_ids: Sequence[str], *, window_days: float
) -> dict[str, list[EventRow]]:
    """Status-heartbeat events for ``device_ids`` over the chart window, in one
    query. Pass the result to :func:`build_battery_chart` as ``events`` when
    rendering several cards."""
    if event_log is None or not device_ids:
        return {}
    since = time.time() - window_days * 86400.0
    return event_log.device_status_events(list(device_ids), since=since)


def build_battery_chart(
    device_id: str,
    *,
    history: BatteryHistory,
    window_days: int,
    name: str | None = None,
    device: Any = None,
    current: dict[str, Any] | None = None,
    mv_offset: int = 0,
    pct_offset: int = 0,
    event_log: EventLog | None = None,
    events: Sequence[EventRow] | None = None,
) -> dict[str, Any]:
    """Everything one device's battery card needs.

    ``events`` are the device's pre-fetched status rows (from
    :func:`load_wake_events`, one query for a whole page); when ``None`` and an
    ``event_log`` is given, they are fetched here for this one device. The
    series carries the **offset-adjusted** percent so the chart matches the
    dashboard's current-battery readout (``BatteryHistory.recent`` returns raw
    rows; the offset is layered here only).

    Returns::

        {
          "id": str, "name": str,
          "current": {...},            # live reading, as passed in
          "pct": int | None, "tone": "ok" | "low" | "critical",
          "window_days": int,
          "samples": int,
          "series": [{"t_ms": int, "pct": int}, ...],            # oldest first
          "wakes": [{"t_ms", "pct", "label", "reason"}, ...],     # thinned
          "predictions_apply": bool,   # False for a Visionect frame
          "prediction": {...} | None,  # always None when predictions don't apply
        }
    """
    rows: list[BatteryRow] = history.recent(device_id, window_days=window_days)
    series: list[dict[str, Any]] = []
    for row in rows:
        adj = apply_to_pct(row.pct, mv_offset, pct_offset, raw_mv=row.battery_mv)
        series.append(
            {"t_ms": int(row.timestamp * 1000), "pct": adj if adj is not None else row.pct}
        )
    if events is None:
        events = load_wake_events(event_log, [device_id], window_days=window_days).get(
            device_id, []
        )
    now_ms = int(time.time() * 1000)
    wakes = thin_wakes(
        extract_wakes(series, events),
        start_ms=now_ms - window_days * 86_400_000,
        end_ms=now_ms,
    )
    applies = is_battery_predictable(device)
    pct = (current or {}).get("pct")
    return {
        "id": device_id,
        "name": name or device_id,
        "current": current or {},
        "pct": pct,
        "tone": tone_for(pct),
        "window_days": window_days,
        "samples": len(rows),
        "series": series,
        "wakes": wakes,
        "predictions_apply": applies,
        "prediction": (
            prediction_dict(history.predict(device_id, window_days=window_days))
            if applies
            else None
        ),
    }


def _nearest(times: Sequence[int], t_ms: int) -> int | None:
    """Index of the value in sorted ``times`` closest to ``t_ms``."""
    if not times:
        return None
    i = bisect.bisect_left(times, t_ms)
    if i == 0:
        return 0
    if i == len(times):
        return len(times) - 1
    return i if times[i] - t_ms < t_ms - times[i - 1] else i - 1


def _interpolate(series: Sequence[dict[str, Any]], t_ms: int) -> int:
    """The line's percent at ``t_ms``, clamped to the first / last sample."""
    times = [p["t_ms"] for p in series]
    i = bisect.bisect_left(times, t_ms)
    if i == 0:
        return int(series[0]["pct"])
    if i == len(series):
        return int(series[-1]["pct"])
    a, b = series[i - 1], series[i]
    span = b["t_ms"] - a["t_ms"]
    if span <= 0:
        return int(b["pct"])
    frac = (t_ms - a["t_ms"]) / span
    value: int = round(a["pct"] + (b["pct"] - a["pct"]) * frac)
    return value
