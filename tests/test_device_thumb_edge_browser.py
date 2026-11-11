"""The device page's thumbnail of the latest picture shows no strip of its own
background along one edge.

A 1 px border takes 2 px off both sides of the box, so a box sized by the
panel's ratio holds a picture of that same ratio only if the border is kept out
of the ratio; otherwise ``object-fit: contain`` leaves a sub-pixel gap that
paints the (white) image background as a light line next to the border, which
shows on a dark picture."""

from __future__ import annotations

import io
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask

from tests.device_page_kinds import _push, build_app

_EXE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(_EXE or p.chromium.executable_path).exists()
    except Exception:
        return False


_HAS_CHROMIUM = _chromium_available()
if not _HAS_CHROMIUM and os.environ.get("TESSERAE_REQUIRE_BROWSER_TESTS") == "1":
    raise RuntimeError(
        "Playwright Chromium is required; run playwright install --with-deps chromium"
    )
pytestmark = pytest.mark.skipif(not _HAS_CHROMIUM, reason="Playwright Chromium not installed")

#: A dark picture, so any light pixel inside the card is the background.
_DARK = (16, 16, 16)


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Flask]]:
    from werkzeug.serving import make_server

    app = build_app(tmp_path_factory.mktemp("device-thumb-edge"), live=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", app
    finally:
        server.shutdown()
        thread.join(timeout=10)


def _light_pixels_inside(png: bytes, inset: int, corner: int) -> list[tuple[int, int, int]]:
    """White pixels (the image background) inside the card, leaving out the
    1 px border ring (``inset`` device pixels, the screenshot may reach a pixel
    past a card at a fractional offset) and the rounded corners."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as shot:
        im = shot.convert("L")
    w, h = im.size
    bad = []
    for y in range(inset, h - inset):
        for x in range(inset, w - inset):
            if (x < corner or x >= w - corner) and (y < corner or y >= h - corner):
                continue
            value = im.getpixel((x, y))
            if value > 245:
                bad.append((x, y, value))
    return bad


@pytest.mark.parametrize("ui", ["paper", "classic"])
@pytest.mark.parametrize(("width", "scale"), [(1440, 1), (1440, 2), (1100, 1), (390, 1), (390, 3)])
def test_card_thumbnail_has_no_light_edge(
    live: tuple[str, Flask], width: int, scale: int, ui: str
) -> None:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    base, app = live
    app.config["SETTINGS_STORE"].patch_section("app", {"ui_design": ui})
    _push(app, "study", "D1", "compDark")
    buf = io.BytesIO()
    Image.new("RGB", (800, 480), _DARK).save(buf, format="PNG")
    (app.config["RENDERS_DIR"] / "compDark.png").write_bytes(buf.getvalue())
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_EXE) if _EXE else p.chromium.launch()
        try:
            ctx = browser.new_context(
                viewport={"width": width, "height": 900}, device_scale_factor=scale
            )
            pg = ctx.new_page()
            pg.goto(f"{base}/settings/devices/study", wait_until="load")
            pg.wait_for_function(
                "() => { const i = document.querySelector('.dx-now-thumb img');"
                " return i && i.complete && i.naturalWidth > 0; }"
            )
            thumb = pg.locator(".dx-now-thumb")
            assert "w=800" in pg.get_attribute(".dx-now-thumb img", "src")
            # The picture fills the box it is in: no letterbox gap of a pixel.
            gap = pg.evaluate(
                "() => { const i = document.querySelector('.dx-now-thumb img');"
                " const r = i.getBoundingClientRect();"
                " const natural = i.naturalWidth / i.naturalHeight;"
                " return Math.abs(r.width - r.height * natural); }"
            )
            assert gap < 0.5, (width, scale, ui, gap)
            bad = _light_pixels_inside(thumb.screenshot(), inset=scale + 1, corner=5 * scale)
            assert not bad, (width, scale, ui, bad[:6])
        finally:
            browser.close()
