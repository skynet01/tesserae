"""Battery chart component: wake extraction, reason labels, thinning, and the
Visionect no-predictions rule (``app.battery_chart``)."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import battery_chart as bc
from app.state.battery_history import BatteryHistory
from app.state.event_log import EventLog, EventRow

NOW = time.time()


def _event(ts: float, extra: dict[str, Any], *, source: str = "d1") -> EventRow:
    return EventRow(
        id=1,
        type="device",
        timestamp=ts,
        source=source,
        target=f"rest://{source}/status",
        status="ok",
        digest=None,
        error=None,
        duration_s=0.0,
        extra=extra,
    )


def _series(*offsets_s: float, pct: int = 80) -> list[dict[str, Any]]:
    return [{"t_ms": int((NOW + o) * 1000), "pct": pct - i} for i, o in enumerate(offsets_s)]


# -- reasons -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("heartbeat", "Check-in"),
        ("timer", "Check-in"),
        ("", "Check-in"),
        (None, "Check-in"),
        ("unknown", "Check-in"),
        ("by server request", "Server request"),
        ("wakeup", "Wake-up"),
        ("reboot", "Reboot"),
        ("connection error", "Reconnect after error"),
        ("ext0", "Button"),
        ("POWERON", "Power on"),
        ("brownout", "Brownout"),
        ("task_wdt", "Watchdog reset"),
        ("wifi-reconnect", "Wi-Fi reconnect"),
        ("moon phase", "moon phase"),
    ],
)
def test_reason_label(raw: str | None, label: str) -> None:
    assert bc.reason_label(raw) == label


def test_unknown_reason_is_truncated_not_dropped() -> None:
    assert bc.reason_label("x" * 200) == "x" * bc.MAX_REASON_CHARS


def test_event_reason_reads_known_keys_in_priority_order() -> None:
    assert bc.event_reason({"parsed": {"connect_reason": "wakeup", "wake_reason": "timer"}}) == (
        "wakeup"
    )
    assert bc.event_reason({"parsed": {"wake_reason": "ext0"}}) == "ext0"
    assert bc.event_reason({"parsed": {"reset_reason": "poweron"}}) == "poweron"
    assert bc.event_reason({"diag": {"id": 1, "reset": "brownout"}}) == "brownout"


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"parsed": {"battery_pct": 50}},
        {"parsed": {"connect_reason": 3}},
        {"parsed": {"wake_reason": "  "}},
        {"diag": {"id": 1, "paint_error": "refresh_timeout"}},
        {"ota": {"state": "idle"}},
    ],
)
def test_event_reason_none_when_row_names_no_reason(extra: dict[str, Any]) -> None:
    assert bc.event_reason(extra) is None


# -- wake extraction -----------------------------------------------------


def test_every_sample_is_a_check_in_wake() -> None:
    series = _series(-300, -200, -100)
    wakes = bc.extract_wakes(series, [])
    assert [w["t_ms"] for w in wakes] == [p["t_ms"] for p in series]
    assert {w["label"] for w in wakes} == {"Check-in"}
    assert all(w["reason"] is None for w in wakes)


def test_no_series_means_no_wakes() -> None:
    event = _event(NOW, {"parsed": {"connect_reason": "wakeup"}})
    assert bc.extract_wakes([], [event]) == []


def test_event_reason_labels_the_matching_sample() -> None:
    series = _series(-300, -200, -100)
    # Written a couple of seconds after the middle sample, as in production.
    event = _event(NOW - 198, {"parsed": {"connect_reason": "by server request"}})
    wakes = bc.extract_wakes(series, [event])
    assert len(wakes) == 3
    assert [w["label"] for w in wakes] == ["Check-in", "Server request", "Check-in"]
    assert wakes[1]["reason"] == "by server request"


def test_event_without_nearby_sample_becomes_its_own_wake_on_the_line() -> None:
    series = [
        {"t_ms": int((NOW - 1000) * 1000), "pct": 80},
        {"t_ms": int((NOW - 200) * 1000), "pct": 60},
    ]
    event = _event(NOW - 600, {"diag": {"id": 7, "reset": "brownout"}})
    wakes = bc.extract_wakes(series, [event])
    assert [w["label"] for w in wakes] == ["Check-in", "Brownout", "Check-in"]
    mid = wakes[1]
    assert mid["t_ms"] == int((NOW - 600) * 1000)
    assert mid["pct"] == 70  # halfway down the line between 80 and 60


def test_routine_event_does_not_overwrite_a_specific_reason() -> None:
    series = _series(-100)
    events = [
        _event(NOW - 99, {"parsed": {"connect_reason": "reboot"}}),
        _event(NOW - 98, {"parsed": {"connect_reason": "heartbeat"}}),
    ]
    wakes = bc.extract_wakes(series, events)
    assert wakes[0]["label"] == "Reboot"


def test_event_rows_without_a_reason_are_ignored() -> None:
    series = _series(-100)
    wakes = bc.extract_wakes(series, [_event(NOW - 100, {"parsed": {"battery_pct": 50}})])
    assert len(wakes) == 1 and wakes[0]["label"] == "Check-in"


# -- thinning ------------------------------------------------------------


def _wake(t_ms: int, label: str = "Check-in") -> dict[str, Any]:
    return {"t_ms": t_ms, "pct": 50, "label": label, "reason": None}


def test_thin_passes_small_lists_through() -> None:
    wakes = [_wake(i * 1000) for i in range(10)]
    assert bc.thin_wakes(wakes, start_ms=0, end_ms=10_000, max_dots=20) == wakes


def test_thin_caps_dot_count_and_keeps_the_last_of_each_bucket() -> None:
    wakes = [_wake(i * 1000) for i in range(1000)]
    out = bc.thin_wakes(wakes, start_ms=0, end_ms=1_000_000, max_dots=50)
    assert len(out) == 50
    assert out[0]["t_ms"] == 19_000  # last of the first 20-wake bucket
    assert out[-1]["t_ms"] == 999_000
    assert [w["t_ms"] for w in out] == sorted(w["t_ms"] for w in out)


def test_thin_keeps_a_notable_wake_over_later_routine_ones() -> None:
    wakes = [_wake(i * 1000) for i in range(100)]
    wakes[3] = _wake(3000, "Power on")
    out = bc.thin_wakes(wakes, start_ms=0, end_ms=100_000, max_dots=5)
    assert len(out) == 5
    assert "Power on" in {w["label"] for w in out}


def test_thin_clamps_wakes_outside_the_window() -> None:
    wakes = [_wake(-5000)] + [_wake(i * 1000) for i in range(100)] + [_wake(500_000)]
    out = bc.thin_wakes(wakes, start_ms=0, end_ms=100_000, max_dots=10)
    assert len(out) <= 10


# -- builder -------------------------------------------------------------


@pytest.fixture
def history(tmp_path: Path) -> BatteryHistory:
    return BatteryHistory(tmp_path / "battery.db")


def _fill(history: BatteryHistory, device_id: str, n: int = 12) -> None:
    for i in range(n):
        history.record(device_id, pct=100 - i, timestamp=NOW - (n - i) * 3600)


def test_builder_shape_for_a_normal_device(history: BatteryHistory) -> None:
    _fill(history, "d1")
    card = bc.build_battery_chart(
        "d1", history=history, window_days=7, name="Hall", current={"pct": 25}
    )
    assert card["id"] == "d1" and card["name"] == "Hall"
    assert card["pct"] == 25 and card["tone"] == "low"
    assert card["samples"] == 12 == len(card["series"]) == len(card["wakes"])
    assert card["predictions_apply"] is True
    assert card["prediction"] is not None
    assert set(card["series"][0]) == {"t_ms", "pct"}
    assert set(card["wakes"][0]) == {"t_ms", "pct", "label", "reason"}


def test_builder_applies_offset_to_series_and_wakes(history: BatteryHistory) -> None:
    history.record("d1", pct=50, timestamp=NOW - 60)
    card = bc.build_battery_chart("d1", history=history, window_days=1, pct_offset=10)
    assert card["series"][0]["pct"] == 60
    assert card["wakes"][0]["pct"] == 60


def test_builder_fetches_events_for_one_device(history: BatteryHistory, tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    history.record("d1", pct=70, timestamp=time.time())
    log.record(
        type="device",
        source="d1",
        target="rest://d1/status",
        status="ok",
        extra={"parsed": {"wake_reason": "ext0"}},
    )
    card = bc.build_battery_chart("d1", history=history, window_days=1, event_log=log)
    assert [w["label"] for w in card["wakes"]] == ["Button"]


def test_builder_with_no_samples(history: BatteryHistory) -> None:
    card = bc.build_battery_chart("d1", history=history, window_days=7)
    assert card["series"] == [] and card["wakes"] == [] and card["samples"] == 0
    assert card["pct"] is None and card["tone"] == "ok"


def test_visionect_devices_get_no_predictions(history: BatteryHistory) -> None:
    _fill(history, "frame")
    visionect_instance = SimpleNamespace(id="frame", kind_of="visionect", module=None)
    card = bc.build_battery_chart(
        "frame", history=history, window_days=7, device=visionect_instance
    )
    assert card["predictions_apply"] is False
    assert card["prediction"] is None
    # The chart data is still there.
    assert card["samples"] == 12 and len(card["series"]) == 12


def test_other_devices_keep_predictions(history: BatteryHistory) -> None:
    other = SimpleNamespace(id="e1", kind_of="esp32_client", module=SimpleNamespace(PROTOCOL="x"))
    assert bc.is_battery_predictable(other) is True
    assert bc.is_battery_predictable(None) is True


def test_load_wake_events_is_one_batched_window_bounded_query(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    for dev in ("a", "b", "c"):
        log.record(
            type="device",
            source=dev,
            target=f"rest://{dev}/status",
            status="ok",
            extra={"parsed": {"connect_reason": "wakeup"}},
        )
    log.record(
        type="device", source="a", target="client_log", status="info", extra={"client_log": True}
    )
    log.record(type="push", source="a", target="page", status="sent")
    out = bc.load_wake_events(log, ["a", "b"], window_days=1)
    assert set(out) == {"a", "b"}
    assert len(out["a"]) == 1  # no client-log row, no push row
    # Bounded by the chart window: age "c" past it and it drops out.
    with contextlib.closing(sqlite3.connect(tmp_path / "events.db")) as conn, conn:
        conn.execute("UPDATE events SET timestamp = ? WHERE source = 'c'", (NOW - 10 * 86400,))
    assert "c" not in bc.load_wake_events(log, ["a", "b", "c"], window_days=1)
    assert "c" in bc.load_wake_events(log, ["a", "b", "c"], window_days=30)
    assert bc.load_wake_events(None, ["a"], window_days=1) == {}
    assert bc.load_wake_events(log, [], window_days=1) == {}
