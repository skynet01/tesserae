"""The repeater settings field in a live Chromium: rows drawn on load, Add,
Remove, Move up / down, the unsaved-changes bar, Discard rebuilding the
served rows, and Save storing what the rows show. The row behaviour lives in
static/components.js (attachRepeaters), which no rendered-HTML assertion
reaches."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

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

SETTING: dict[str, Any] = {
    "name": "items",
    "type": "repeater",
    "label": "Items",
    "add_label": "Add item",
    "row_label": "Item",
    "max_rows": 3,
    "fields": [
        {"name": "on", "type": "boolean", "label": "On", "default": True},
        {"name": "code", "type": "string", "label": "Code", "required": True},
        {"name": "note", "type": "textarea", "label": "Note"},
        {"name": "n", "type": "number", "label": "N"},
    ],
    "default": [
        {"on": True, "code": "A", "note": "first", "n": 1},
        {"on": False, "code": "B", "note": "", "n": None},
    ],
}


@pytest.fixture
def live(tmp_path: Path) -> Iterator[tuple[str, Flask]]:
    from werkzeug.serving import make_server

    folder = tmp_path / "plugins" / "rep_demo"
    folder.mkdir(parents=True)
    manifest = {
        "tesserae_compat": "1.x",
        "name": "Rep Demo",
        "version": "0.0.1",
        "kind": "widget",
        "supports": {"sizes": ["sm"]},
        "settings": [SETTING],
    }
    (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "client.js").write_text("export default function () {}\n", encoding="utf-8")
    app = create_app(testing=False, data_root=tmp_path / "data", plugins_dir=tmp_path / "plugins")
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


def _stored(app: Flask) -> Any:
    store = app.config["SETTINGS_STORE"]
    return store.get_for_runtime("plugins", "rep_demo", [SETTING])["items"]


def test_rows_add_remove_move_discard_and_save(live: tuple[str, Flask]) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/settings/plugins", wait_until="networkidle")
        card = page.locator("#plugin-rep_demo")
        rows = card.locator("[data-repeater-row]")
        storage = card.locator("[data-repeater-storage]")
        bar = card.locator("[data-save-bar]")

        def codes() -> list[str]:
            return [
                rows.nth(i).locator('[data-repeater-key="code"]').input_value()
                for i in range(rows.count())
            ]

        def stored_codes() -> list[str]:
            return [r["code"] for r in json.loads(storage.input_value())]

        assert codes() == ["A", "B"]
        assert rows.first.locator("[data-repeater-up]").get_attribute("aria-disabled") == "true"
        assert bar.is_hidden()

        # Add a row: it takes the sub-field defaults, gets focus and fresh ids.
        card.locator("[data-repeater-add]").click()
        assert rows.count() == 3
        new_code = rows.nth(2).locator('[data-repeater-key="code"]')
        assert new_code.evaluate("el => el === document.activeElement")
        new_code.fill("C")
        assert rows.nth(2).locator('[data-repeater-key="on"]').is_checked()
        ids = card.locator("[data-repeater-list] [id]").evaluate_all("els => els.map(e => e.id)")
        assert len(ids) == len(set(ids))
        assert stored_codes() == ["A", "B", "C"]
        assert bar.is_visible()  # structural edits mark the card unsaved
        # max_rows is 3: the add button is now off and does nothing.
        assert card.locator("[data-repeater-add]").get_attribute("aria-disabled") == "true"
        card.locator("[data-repeater-add]").evaluate("el => el.click()")
        assert rows.count() == 3

        # Move C up twice: it ends first, and the button keeps focus.
        up = rows.nth(2).locator("[data-repeater-up]")
        up.click()
        assert codes() == ["A", "C", "B"]
        page.keyboard.press("Enter")
        assert codes() == ["C", "A", "B"]
        assert stored_codes() == ["C", "A", "B"]
        rows.first.locator("[data-repeater-down]").click()
        assert codes() == ["A", "C", "B"]

        # Remove B.
        rows.nth(2).locator("[data-repeater-remove]").click()
        assert codes() == ["A", "C"]
        assert stored_codes() == ["A", "C"]

        # Discard brings back the rows the page was served with.
        card.locator(".dx-save-bar-discard").click()
        page.wait_for_timeout(50)
        assert codes() == ["A", "B"]
        assert stored_codes() == ["A", "B"]
        assert not rows.nth(1).locator('[data-repeater-key="on"]').is_checked()
        assert rows.first.locator('[data-repeater-key="note"]').input_value() == "first"

        # Edit and save: what the rows show is what gets stored.
        rows.nth(1).locator('[data-repeater-key="code"]').fill("B2")
        rows.nth(1).locator('[data-repeater-key="n"]').fill("7")
        rows.first.locator("[data-repeater-remove]").click()
        with page.expect_navigation():
            card.locator(".dx-save-bar-save").click()
        page.wait_for_load_state("networkidle")
        after = page.locator("#plugin-rep_demo [data-repeater-row]")
        assert after.count() == 1
        browser.close()

    assert errors == []
    assert _stored(app) == [{"on": False, "code": "B2", "note": "", "n": 7}]


def test_a_blank_required_code_blocks_the_save_in_the_browser(live: tuple[str, Flask]) -> None:
    from playwright.sync_api import sync_playwright

    base, app = live
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{base}/settings/plugins", wait_until="networkidle")
        card = page.locator("#plugin-rep_demo")
        card.locator("[data-repeater-add]").click()
        card.locator(".dx-save-bar-save").click()
        page.wait_for_timeout(200)
        invalid = card.locator('[data-repeater-key="code"]:invalid').count()
        browser.close()

    assert invalid == 1
    assert _stored(app) == SETTING["default"]


ACCEPTED_PATTERNS = [
    "[A-Za-z0-9_\\-]+",
    "[A-Z]{2}_[A-Z0-9]+",
    "#[0-9a-fA-F]{6}",
    "\\d{3}-\\d{4}",
    "[^ ]*",
    "v\\.\\d?",
    "x.y",
    "[\\w.]{1,8}",
    "a/b",
    "[a\\-z]+",
    "\\(\\d+\\)",
    "[\\/]",
    "é+",
]
SAMPLES = [
    "",
    "a",
    "A_9-z",
    "AB_X1",
    "#00ffAA",
    "555-1234",
    "no spaces",
    "v.1",
    "xzy",
    "a.b_c",
    "a/b",
    "a-z",
    "(42)",
    "/",
    "éé",
    "١٢٣-٤٥٦٧",
    "١",
    "ab١",
]


def test_the_browser_reads_every_accepted_pattern_the_same_way() -> None:
    """The input's pattern attribute compiles with the v flag. Every pattern
    the server accepts compiles there too, and matches exactly what the
    server's own matcher matches, Unicode digits included; patterns with
    Unicode digits in a count are refused here as they are there."""
    from playwright.sync_api import sync_playwright

    from app import repeater

    expected = {
        p: [repeater._pattern_matches(repeater._compile_pattern(p), t) for t in SAMPLES]
        for p in ACCEPTED_PATTERNS
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        got = page.evaluate(
            """([patterns, samples]) => Object.fromEntries(patterns.map((p) => {
                 const re = new RegExp(`^(?:${p})$`, "v");
                 return [p, samples.map((s) => re.test(s))];
               }))""",
            [ACCEPTED_PATTERNS, SAMPLES],
        )
        refused = page.evaluate(
            """(patterns) => patterns.map((p) => {
                 try { new RegExp(`^(?:${p})$`, "v"); return false; } catch { return true; }
               })""",
            ["a{١}", "a{１}"],
        )
        browser.close()
    assert got == expected
    assert refused == [True, True]
    for pattern in ("a{١}", "a{１}"):
        assert repeater.pattern_error(pattern) is not None
