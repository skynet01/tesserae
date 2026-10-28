"""The status dot's freshness bands follow a sleeping device's own cycle."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from app.main import REPO_ROOT, create_app
from app.settings._shared import STATUS_FRESH_S, STATUS_WARN_S, health_for_age


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    return create_app(testing=True, data_root=tmp_path, plugins_dir=REPO_ROOT / "plugins")


class _Telemetry:
    def __init__(self, entry: object | None) -> None:
        self._entry = entry

    def get(self, device_id: str) -> object | None:
        return self._entry


def _health(app: Flask, entry: object | None, age: float) -> str:
    app.config["DEVICE_TELEMETRY"] = _Telemetry(entry)
    with app.app_context():
        return health_for_age("frame", age)


def test_a_device_with_no_known_cycle_keeps_the_fixed_bands(app: Flask) -> None:
    assert _health(app, None, STATUS_FRESH_S) == "ok"
    assert _health(app, None, STATUS_FRESH_S + 1) == "warn"
    assert _health(app, None, STATUS_WARN_S + 1) == "stale"


def test_a_thirty_minute_sleeper_is_not_red_between_wakes(app: Flask) -> None:
    frame = SimpleNamespace(last_sleep_interval_s=1800, always_on=False)
    assert _health(app, frame, 26 * 60) == "ok"  # mid-cycle
    assert _health(app, frame, 38 * 60) == "ok"  # a little late
    assert _health(app, frame, 50 * 60) == "warn"  # missed a wake
    assert _health(app, frame, 90 * 60) == "stale"  # missed several


def test_an_always_on_device_keeps_the_fixed_bands(app: Flask) -> None:
    kiosk = SimpleNamespace(last_sleep_interval_s=1800, always_on=True)
    assert _health(app, kiosk, 10 * 60) == "stale"


def test_a_short_cycle_never_tightens_the_fixed_bands(app: Flask) -> None:
    quick = SimpleNamespace(last_sleep_interval_s=30, always_on=False)
    assert _health(app, quick, STATUS_FRESH_S) == "ok"
