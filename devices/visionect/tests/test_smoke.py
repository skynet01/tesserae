"""visionect smoke tests: the kind loads, heartbeats decode leniently and
the UUID config is validated."""

from __future__ import annotations

import json

import pytest

from app.device_loader import discover
from app.main import REPO_ROOT


@pytest.fixture
def visionect(tmp_path):
    registry = discover(
        REPO_ROOT / "devices",
        schema_path=REPO_ROOT / "schema" / "device.schema.json",
        data_root=tmp_path,
    )
    assert registry.errors == [], registry.errors
    d = registry.get("visionect")
    assert d is not None
    return d


def test_kind_is_a_push_device_on_the_grey_renderer(visionect) -> None:
    assert visionect.manifest["transport"] == "push"
    assert visionect.renderer_ids == ["trmnl_png_gray16"]
    assert visionect.manifest["panel"]["native_w"] == 1440
    assert visionect.manifest["panel"]["native_h"] == 2560


def test_parse_status_keeps_numeric_readings(visionect) -> None:
    beat = {
        "battery_pct": 61,
        "battery_mv": 3804,
        "rssi": -58,
        "temperature_c": 23,
        "uptime_s": 9840,
        "fw_version": "6.17.4168",
        "uuid": "36002000-1247-3830-3933-303600000000",
        "battery_pct_bogus": "x",
    }
    parsed = visionect.parse_status(json.dumps(beat).encode("utf-8"))
    assert parsed["battery_pct"] == 61
    assert parsed["rssi"] == -58
    assert parsed["fw_version"] == "6.17.4168"
    assert "battery_pct_bogus" not in parsed


def test_parse_status_tolerates_garbage(visionect) -> None:
    assert visionect.parse_status(b"") == {}
    assert "error" in visionect.parse_status(b"not json")
    assert "error" in visionect.parse_status(b"[1, 2]")


@pytest.mark.parametrize(
    ("uuid", "ok"),
    [
        ("", True),
        ("36002000-1247-3830-3933-303600000000", True),
        ("36002000124738303933303600000000", True),
        ("36002000-1247", False),
        ("zz002000-1247-3830-3933-303600000000", False),
    ],
)
def test_validate_config_checks_the_uuid(visionect, uuid: str, ok: bool) -> None:
    assert visionect.validate_config({"uuid": uuid})[0] is ok
