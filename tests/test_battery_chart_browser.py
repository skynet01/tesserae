"""In a live Chromium: the battery chart's tooltip reads in light and dark, in
both UI designs; wake dots show their reason on hover and from the keyboard.

Set ``BATTERY_SHOTS=/some/dir`` to also save screenshots of each combination."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from app.main import REPO_ROOT, create_app

_EXE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(_EXE or p.chromium.executable_path).exists()
    except Exception:
        return False


def _launch(p: Any) -> Any:
    return p.chromium.launch(executable_path=_EXE) if _EXE else p.chromium.launch()


_HAS_CHROMIUM = _chromium_available()
if not _HAS_CHROMIUM and os.environ.get("TESSERAE_REQUIRE_BROWSER_TESTS") == "1":
    raise RuntimeError(
        "Playwright Chromium is required; run playwright install --with-deps chromium"
    )
pytestmark = pytest.mark.skipif(not _HAS_CHROMIUM, reason="Playwright Chromium not installed")


def _seed(app: Flask, data_root: Path) -> None:
    client = app.test_client()
    for device_id, kind in (
        ("hall_frame", "visionect_place_and_play_32"),
        ("attic_esp", "esp32_client"),
    ):
        client.post(
            "/settings/devices/add",
            data={"id": device_id, "kind": kind, "name": device_id.replace("_", " ").title()},
        )
    store = app.config["BATTERY_HISTORY"]
    log = app.config["EVENT_LOG"]
    now = time.time()
    reasons = {20: "wakeup", 14: "by server request", 8: "reboot"}
    for device_id in ("hall_frame", "attic_esp"):
        for i in range(48):  # a sample every 30 minutes over the last day
            store.record(device_id, pct=92 - i // 2, timestamp=now - (48 - i) * 1800)
    # The event-log rows the status handler writes when the reason changes,
    # moved back to the time of the sample they belong to.
    for idx, reason in reasons.items():
        log.record(
            type="device",
            source="hall_frame",
            target="rest://hall_frame/status",
            status="ok",
            extra={"parsed": {"connect_reason": reason}},
        )
        with contextlib.closing(sqlite3.connect(data_root / "core" / "events.db")) as conn, conn:
            conn.execute(
                "UPDATE events SET timestamp = ? WHERE id = (SELECT MAX(id) FROM events)",
                (now - (48 - idx) * 1800 + 2,),
            )


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Flask]]:
    from werkzeug.serving import make_server

    data_root = tmp_path / "data"
    app: Flask = create_app(
        testing=False,
        data_root=data_root,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
        devices_dir=REPO_ROOT / "devices",
    )
    app.config["TESTING"] = True
    store = app.config["SETTINGS_STORE"]
    store.patch_section("app", {"onboarded": True, "timezone": "UTC"})
    store.patch_section("auth", {"disabled": True})
    _seed(app, data_root)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", app
    finally:
        server.shutdown()
        thread.join(timeout=10)


# The Visionect card's chart (the one seeded with wake reasons).
CANVAS = "article[data-lf-text$=' hall_frame'] canvas.battery-chart"

# Tooltip colours as RGB triples + the contrast ratio between box and text.
_TOOLTIP_JS = """() => {
  const canvas = document.querySelector(__CANVAS__);
  const tip = Chart.getChart(canvas).options.plugins.tooltip;
  const c = document.createElement('canvas').getContext('2d');
  const rgb = (v) => { c.fillStyle = '#000'; c.fillStyle = v; c.fillRect(0, 0, 1, 1);
    return Array.from(c.getImageData(0, 0, 1, 1).data).slice(0, 3); };
  const lum = ([r, g, b]) => { const f = (x) => { x /= 255;
    return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const ratio = (a, b) => { const [hi, lo] = [lum(rgb(a)), lum(rgb(b))].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05); };
  const page = getComputedStyle(canvas.closest('.dx-battery-card')).backgroundColor;
  return { body: ratio(tip.backgroundColor, tip.bodyColor),
           title: ratio(tip.backgroundColor, tip.titleColor),
           edge: ratio(tip.borderColor, page) };
}""".replace("__CANVAS__", repr(CANVAS))

# Where wake dot ``i`` of the first chart is, in page coordinates.
_DOT_JS = """(label) => {
  const canvas = document.querySelector(__CANVAS__);
  const wakes = JSON.parse(canvas.dataset.wakes);
  const i = wakes.findIndex((w) => w.label === label);
  if (i < 0) return null;
  const el = Chart.getChart(canvas).getDatasetMeta(1).data[i];
  const r = canvas.getBoundingClientRect();
  return { x: r.left + el.x, y: r.top + el.y };
}""".replace("__CANVAS__", repr(CANVAS))

_TIP_TEXT_JS = """() => {
  const t = Chart.getChart(document.querySelector(__CANVAS__)).tooltip;
  return { opacity: t.opacity, body: t.body.flatMap((b) => b.lines), title: t.title };
}""".replace("__CANVAS__", repr(CANVAS))


def _open(page: Any, base: str, app: Flask, *, ui: str, theme: str) -> None:
    app.config["SETTINGS_STORE"].patch_section("app", {"ui_design": ui})
    page.goto(f"{base}/devices/battery?window=1")
    page.evaluate(
        "(t) => { const r = document.documentElement;"
        " if (t === 'dark') r.setAttribute('data-theme', 'dark');"
        " else r.removeAttribute('data-theme'); }",
        theme,
    )
    page.wait_for_selector("canvas.battery-chart")
    page.wait_for_timeout(300)  # the theme observer restyles on the next frame


@pytest.mark.parametrize("ui", ["classic", "paper"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_tooltip_is_readable_and_dots_explain_themselves(
    served: tuple[str, Flask], ui: str, theme: str
) -> None:
    from playwright.sync_api import sync_playwright

    base, app = served
    shots = os.environ.get("BATTERY_SHOTS")
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={"width": 1100, "height": 1500})
        _open(page, base, app, ui=ui, theme=theme)
        assert page.evaluate("document.documentElement.dataset.ui || 'classic'") == (
            "paper" if ui == "paper" else "classic"
        )

        # Readable: text on the box clears WCAG AA, and its border stands off the card.
        ratios = page.evaluate(_TOOLTIP_JS)
        assert ratios["body"] >= 4.5, ratios
        assert ratios["title"] >= 3.0, ratios
        assert ratios["edge"] >= 1.2, ratios

        # Hovering a dot names its reason.
        spot = page.evaluate(_DOT_JS, "Server request")
        assert spot is not None
        page.mouse.move(spot["x"], spot["y"])
        page.wait_for_timeout(150)
        tip = page.evaluate(_TIP_TEXT_JS)
        assert tip["opacity"] > 0
        assert "Server request" in tip["body"], tip
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / f"batteries-{ui}-{theme}.png"), full_page=True)

        # And from the keyboard: focus the chart, step back one wake.
        page.mouse.move(2, 2)
        page.evaluate(f"document.querySelector({CANVAS!r}).focus()")
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(150)
        tip = page.evaluate(_TIP_TEXT_JS)
        assert tip["opacity"] > 0
        assert any("%" in line for line in tip["body"])
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        assert page.evaluate(_TIP_TEXT_JS)["opacity"] == 0
        browser.close()


def test_visionect_card_has_no_prediction_tiles_in_the_browser(served: tuple[str, Flask]) -> None:
    from playwright.sync_api import sync_playwright

    base, app = served
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page()
        _open(page, base, app, ui="paper", theme="light")
        labels = page.evaluate(
            """() => Object.fromEntries([...document.querySelectorAll('.dx-battery-card')].map(
                 (c) => [c.dataset.lfText.split(' ').pop(),
                         [...c.querySelectorAll('.dx-stat-label')].map((l) => l.textContent.trim())]))"""
        )
        browser.close()
    assert labels["hall_frame"] == ["Samples"]
    assert "Drain rate" in labels["attic_esp"]
