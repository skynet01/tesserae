"""The Comics admin page in a live Chromium: it fits light, dark and phone
widths, the settings fold and the strip rows open, and Import URL sends the
address as typed (the page's own script) and turns the strip manual."""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from PIL import Image

from app.main import create_app


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="Playwright Chromium not installed"
)


@pytest.fixture
def live(tmp_path: Path) -> Iterator[tuple[str, Flask]]:
    from werkzeug.serving import make_server

    plugins_dir = Path(__file__).resolve().parents[1] / "plugins"
    app = create_app(testing=False, data_root=tmp_path / "data", plugins_dir=plugins_dir)
    app.config["TESTING"] = True
    store = app.config["SETTINGS_STORE"]
    store.patch_section("app", {"onboarded": True})
    store.patch_section("auth", {"disabled": True})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", app
    finally:
        server.shutdown()
        thread.join(timeout=10)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("L", (600, 200), 255).save(buf, format="PNG")
    return buf.getvalue()


def test_comics_admin_fits_and_imports_the_typed_url(
    live: tuple[str, Flask], monkeypatch: pytest.MonkeyPatch
) -> None:
    from playwright.sync_api import sync_playwright

    from app import net_guard

    base, app = live
    asked: list[str] = []

    def fake_fetch(url: str, **kw: Any) -> tuple[bytes, str]:
        asked.append(url)
        return _png(), "image/png"

    monkeypatch.setattr(net_guard, "fetch_bytes", fake_fetch)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        for width, theme in ((1280, "light"), (390, "light"), (390, "dark")):
            page.emulate_media(color_scheme=theme)
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{base}/plugins/comics_daily/", wait_until="networkidle")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            card = page.locator("#plugin-comics_daily")
            card.locator("summary.dx-section-head").click()
            rows = card.locator("[data-repeater-row]")
            assert rows.count() == 4
            rows.nth(1).locator("[data-repeater-open]").click()
            assert rows.nth(1).locator('[data-repeater-key="image_url"]').is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # Dilbert is built in: switch it off, but it can't be removed.
        dilbert = rows.first
        assert dilbert.locator("[data-repeater-remove]").get_attribute("aria-disabled") == "true"
        assert dilbert.locator('[data-repeater-key="slug"]').get_attribute("readonly") is not None
        # Import URL takes the address as typed, after asking about the
        # unsaved change the typing made.
        page.on("dialog", lambda dialog: dialog.accept())
        url_box = rows.nth(1).locator('[data-repeater-key="image_url"]')
        url_box.fill("https://pictures.example.org/garfield.png")
        with page.expect_navigation():
            rows.nth(1).get_by_role("button", name="Import URL").click()
        page.wait_for_load_state("networkidle")
        browser.close()
    assert errors == []
    assert asked == ["https://pictures.example.org/garfield.png"]
    plugin = app.config["PLUGIN_REGISTRY"].get("comics_daily")
    rows = app.config["SETTINGS_STORE"].get_for_runtime(
        "plugins", plugin.id, plugin.manifest["settings"]
    )["strips"]
    garfield = next(r for r in rows if r["slug"] == "garfield")
    assert garfield["mode"] == "upload"
    assert garfield["image_url"] == "https://pictures.example.org/garfield.png"
