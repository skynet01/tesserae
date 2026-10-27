"""The Comics admin page in a live Chromium: it fits light, dark and phone
widths, the strip rows open, and Import URL sends the
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


def test_discard_brings_rows_back_with_their_extras(live: tuple[str, Flask]) -> None:
    """Discard restores the rows as served: each strip's upload tools and
    status, and Dilbert still can't be removed."""
    from playwright.sync_api import sync_playwright

    base, _ = live
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{base}/plugins/comics_daily/", wait_until="networkidle")
        card = page.locator("#plugin-comics_daily")
        rows = card.locator("[data-repeater-row]")
        slugs = lambda: [  # noqa: E731
            rows.nth(i).locator('[data-repeater-key="slug"]').input_value()
            for i in range(rows.count())
        ]
        served = slugs()
        rows.nth(1).locator("[data-repeater-open]").click()
        rows.nth(1).locator("[data-repeater-up]").click()  # Garfield above Dilbert
        rows.nth(2).locator("[data-repeater-open]").click()
        rows.nth(2).locator("[data-repeater-remove]").click()
        assert slugs() != served
        card.locator(".dx-save-bar-discard").click()
        page.wait_for_timeout(100)
        assert slugs() == served
        dilbert = rows.first
        assert dilbert.get_attribute("data-repeater-fixed") is not None
        assert dilbert.locator("[data-repeater-remove]").get_attribute("aria-disabled") == "true"
        assert rows.nth(1).locator('input[type="file"][form="cx-upload-garfield"]').count() == 1
        assert rows.nth(1).locator(".cx-row-status").count() == 1
        # And the restored rows still work.
        rows.nth(1).locator("[data-repeater-open]").click()
        assert rows.nth(1).locator('[data-repeater-key="image_url"]').is_visible()
        browser.close()


def test_far_side_panels_save_through_the_one_form(live: tuple[str, Flask]) -> None:
    """Picking a panel count shows the save bar and saves with the rest;
    typing cookies doesn't count as a settings change; the log opens on
    its newest line."""
    from playwright.sync_api import sync_playwright

    base, app = live
    plugin = app.config["PLUGIN_REGISTRY"].get("comics_daily")
    store = plugin.server_module.images.Store(Path(plugin.data_dir))
    for n in range(40):
        store.append_log({"at": 1.0e9 + n, "source": "Garfield", "outcome": "ok", "text": f"n{n}"})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{base}/plugins/comics_daily/", wait_until="networkidle")
        bar = page.locator("[data-save-bar]")
        log = page.locator(".cx-log")
        assert log.evaluate("el => el.scrollHeight - el.scrollTop - el.clientHeight < 2")
        page.locator("#cookies details").evaluate("d => { d.open = true; }")
        page.locator("#cx-cookies").fill("session=1")
        assert bar.is_hidden()
        page.locator("#farside").get_by_text("2", exact=True).click()
        assert bar.is_visible()
        with page.expect_navigation():
            bar.locator(".dx-save-bar-save").click()
        page.wait_for_load_state("networkidle")
        assert page.locator('input[name="far_side_count"][value="2"]').is_checked()
        browser.close()
    values = app.config["SETTINGS_STORE"].get_for_runtime(
        "plugins", plugin.id, plugin.manifest["settings"]
    )
    assert values["far_side_count"] == 2
    assert [r["slug"] for r in values["strips"]][:2] == ["dilbert", "garfield"]


def _seed_strip(app: Flask) -> None:
    import datetime as dt
    import time as _time

    plugin = app.config["PLUGIN_REGISTRY"].get("comics_daily")
    module = plugin.server_module
    store = module.images.Store(Path(plugin.data_dir))
    buf = io.BytesIO()
    Image.new("L", (1200, 380), 255).save(buf, format="PNG")
    picture = module.images.normalise(buf.getvalue())
    name = store.add("gocomics:garfield", picture)
    today = module._today().isoformat()
    store.update(
        "gocomics:garfield",
        lambda e: e.update(
            pictures=[{"file": name, "w": 1200, "h": 380}],
            edition=today,
            fetched_on=today,
            last_success=_time.time(),
            last_attempt=_time.time(),
            error=None,
        ),
    )
    fields = plugin.manifest["settings"]
    with app.app_context():
        values = app.config["SETTINGS_STORE"].get_for_runtime("plugins", plugin.id, fields)
        values["far_side"] = False
        values["strips"] = [
            {"enabled": True, "label": "", "slug": "dilbert", "mode": "upload", "image_url": ""},
            {"enabled": True, "label": "", "slug": "garfield", "mode": "auto", "image_url": ""},
        ]
        values["strips"][0]["enabled"] = False
        app.config["SETTINGS_STORE"].update_for_namespace("plugins", plugin.id, values, fields)
    del dt


def test_a_sliver_of_a_cell_says_it_is_too_small_rather_than_clip(
    live: tuple[str, Flask],
) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    _seed_strip(app)
    probe = """() => {
      const host = [...document.querySelectorAll('*')].find(
        (el) => el.shadowRoot && el.shadowRoot.querySelector('.rows'));
      const sr = host && host.shadowRoot;
      if (!sr) return null;
      const img = [...sr.querySelectorAll('.row')].find((r) => !r.hidden);
      const box = sr.querySelector('.rows').getBoundingClientRect();
      const r = img ? img.getBoundingClientRect() : null;
      return {
        small: sr.querySelector('.too-small')?.textContent || '',
        inside: r ? (r.top >= box.top - 1 && r.bottom <= box.bottom + 1) : null,
      };
    }"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 800, "height": 300})
        page.goto(f"{base}/_test/render?plugin=comics_daily&w=800&h=300", wait_until="networkidle")
        page.wait_for_timeout(1800)
        normal = page.evaluate(probe)
        page.set_viewport_size({"width": 800, "height": 50})
        page.goto(f"{base}/_test/render?plugin=comics_daily&w=800&h=50", wait_until="networkidle")
        page.wait_for_timeout(1800)
        sliver = page.evaluate(probe)
        browser.close()
    assert normal == {"small": "", "inside": True}
    assert sliver["small"] == "Too small a space for a comic."
