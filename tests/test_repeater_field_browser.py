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
def live(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[tuple[str, Flask]]:
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
    plugins_dir = tmp_path / "plugins"
    if getattr(request, "param", None) == "news":
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


@pytest.mark.parametrize("live", ["news"], indirect=True)
def test_newspaper_rows_expand_toggle_validate_and_save(live):
    from playwright.sync_api import sync_playwright

    base, app = live
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"{base}/plugins/news_front_pages/", wait_until="networkidle")
        card = page.locator("#plugin-news_front_pages")
        rows = card.locator("[data-repeater-row]")
        first = rows.first
        opener = first.locator("[data-repeater-open]")
        code = first.locator('[data-repeater-key="code"]')
        assert code.is_hidden()
        assert opener.get_attribute("aria-expanded") == "false"
        # The switch sits on the row: it never opens it, and dims it when off.
        toggle = first.locator('[data-repeater-key="enabled"]')
        assert toggle.get_attribute("aria-label") == "NY_NYT: In rotation"
        first.locator(".repeater-row-toggle").click()
        assert not toggle.is_checked()
        assert "is-off" in (first.get_attribute("class") or "")
        toggle.focus()
        page.keyboard.press("Space")
        assert toggle.is_checked()
        page.keyboard.press("Space")
        assert not toggle.is_checked()
        assert code.is_hidden()
        # The caret button opens the row from the keyboard; its fields, then
        # its move and remove buttons, are below it.
        opener.focus()
        page.keyboard.press("Enter")
        assert code.is_visible()
        assert opener.get_attribute("aria-expanded") == "true"
        assert first.locator("[data-repeater-up]").get_attribute("aria-disabled") == "true"
        code.fill("WSJ2")
        assert opener.locator("[data-repeater-title]").inner_text() == "WSJ2"
        assert toggle.get_attribute("aria-label") == "WSJ2: In rotation"
        first.locator('[data-repeater-key="css"]').fill("filter:   contrast(1.1)")
        assert opener.locator("[data-repeater-meta]").inner_text() == "filter: contrast(1.1)"
        # A click anywhere else on the row's head (here its caret) closes it.
        first.locator(".repeater-row-chev").click()
        assert code.is_hidden()
        card.locator("[data-repeater-add]").click()
        last = rows.last
        assert last.locator('[data-repeater-key="code"]').is_visible()
        assert last.locator('[data-repeater-key="code"]').evaluate(
            "el => el === document.activeElement"
        )
        last.locator("[data-repeater-open]").click()
        card.locator(".dx-save-bar-save").click()
        # Native required-field validation reveals the closed invalid row.
        assert last.locator('[data-repeater-key="code"]').is_visible()
        assert last.locator('[data-repeater-key="code"]').evaluate(
            "el => el === document.activeElement"
        )
        last.locator('[data-repeater-key="code"]').fill("NEW_TEST")
        with page.expect_navigation():
            card.locator(".dx-save-bar-save").click()
        assert "/plugins/news_front_pages/" in page.url
        assert not page.locator('[data-repeater-key="enabled"]').first.is_checked()
        assert page.locator('[data-repeater-key="code"]').first.input_value() == "WSJ2"
        browser.close()
    assert errors == []
    fields = app.config["PLUGIN_REGISTRY"].get("news_front_pages").manifest["settings"]
    papers = app.config["SETTINGS_STORE"].get_for_runtime("plugins", "news_front_pages", fields)[
        "papers"
    ]
    assert papers[0]["enabled"] is False
    assert papers[0]["css"] == "filter:   contrast(1.1)"
    assert papers[-1]["code"] == "NEW_TEST"


@pytest.mark.parametrize("live", ["news"], indirect=True)
def test_ai_news_settings_save_and_come_back(live):
    """AI news: the settings sit under Last run, open; an edit shows the save
    bar, and a save comes back to the settings. (The page's own controls are
    tested in plugins/news_illustrated/tests/test_admin_browser.py.)"""
    from playwright.sync_api import sync_playwright

    base, app = live
    plugin = app.config["PLUGIN_REGISTRY"].get("news_illustrated")
    (Path(plugin.data_dir) / "state.json").write_text(
        json.dumps({"last_run": {"reason": "manual", "started": 1, "finished": 2, "log": ["a"]}}),
        encoding="utf-8",
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"{base}/plugins/news_illustrated/", wait_until="networkidle")
        ids = page.evaluate(
            "[...document.querySelector('.settings-section-list').children].map((el) => el.id)"
        )
        assert ids == ["stories", "last-run", "plugin-news_illustrated"]
        form = page.locator("#plugin-news_illustrated")
        sources = form.locator('[name="sources"]')
        assert sources.is_visible()
        bar = form.locator("[data-save-bar]")
        assert bar.is_hidden()
        sources.fill("Local | https://example.test/rss")
        assert bar.is_visible()
        with page.expect_navigation():
            bar.locator(".dx-save-bar-save").click()
        page.wait_for_load_state("networkidle")
        assert page.url.endswith("#plugin-news_illustrated")
        # The log opens on its newest lines.
        page.locator("#last-run .dx-disclosure summary").click()
        assert page.locator("#last-run pre[data-log-tail]").is_visible()
        browser.close()
    assert errors == []
    fields = plugin.manifest["settings"]
    saved = app.config["SETTINGS_STORE"].get_for_runtime("plugins", plugin.id, fields)
    assert saved["sources"] == "Local | https://example.test/rss"


@pytest.mark.parametrize("live", ["news"], indirect=True)
def test_news_admin_rows_fit_desktop_and_mobile(live):
    import os

    from playwright.sync_api import sync_playwright

    base, _ = live
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for width, theme in ((1280, "light"), (390, "light"), (390, "dark")):
            page.emulate_media(color_scheme=theme)
            page.set_viewport_size({"width": width, "height": 950})
            for pid in ("news_front_pages", "news_illustrated"):
                page.goto(f"{base}/plugins/{pid}/", wait_until="networkidle")
                assert page.locator(f"#plugin-{pid}").is_visible()
                if pid == "news_front_pages":
                    rows = page.locator("[data-repeater-row]")
                    rows.first.locator("[data-repeater-open]").click()
                    box = rows.first.bounding_box()
                    form = page.locator(f"#plugin-{pid} form").bounding_box()
                    assert box["width"] >= form["width"] - 2
                    assert box["width"] > width * (0.6 if width > 640 else 0.7)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                artifacts = os.environ.get("TESSERAE_UI_ARTIFACT_DIR")
                if artifacts:
                    Path(artifacts).mkdir(parents=True, exist_ok=True)
                    page.screenshot(
                        path=str(Path(artifacts) / f"{pid}-{width}-{theme}.png"), full_page=True
                    )
        browser.close()


@pytest.mark.parametrize("live", ["news"], indirect=True)
def test_newspaper_rows_move_remove_and_discard_around_closed_rows(live):
    """An open row moves past closed neighbours and stays the open one; a
    removal hands focus to the next row's caret; Discard brings back the
    served rows, all closed."""
    from playwright.sync_api import sync_playwright

    base, _ = live
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(f"{base}/plugins/news_front_pages/", wait_until="networkidle")
        card = page.locator("#plugin-news_front_pages")
        rows = card.locator("[data-repeater-row]")

        def codes() -> list[str]:
            return [
                rows.nth(i).locator('[data-repeater-key="code"]').input_value()
                for i in range(rows.count())
            ]

        def open_codes() -> list[str]:
            return [
                rows.nth(i).locator('[data-repeater-key="code"]').input_value()
                for i in range(rows.count())
                if rows.nth(i).locator('[data-repeater-key="code"]').is_visible()
            ]

        served = codes()
        assert served == ["NY_NYT", "WSJ", "USAT", "AZ_ADS"]
        rows.nth(1).locator("[data-repeater-open]").click()
        rows.nth(1).locator("[data-repeater-down]").click()
        assert codes() == ["NY_NYT", "USAT", "WSJ", "AZ_ADS"]
        assert open_codes() == ["WSJ"]  # the neighbours stay closed
        focused = page.evaluate(
            """() => {
                 const el = document.activeElement;
                 const row = el.closest("[data-repeater-row]");
                 return [el.matches("[data-repeater-down]"),
                         row.querySelector('[data-repeater-key="code"]').value];
               }"""
        )
        assert focused == [True, "WSJ"]  # the button pressed keeps focus
        up = rows.nth(2).locator("[data-repeater-up]")
        up.click()
        page.keyboard.press("Enter")
        assert codes() == ["WSJ", "NY_NYT", "USAT", "AZ_ADS"]
        assert open_codes() == ["WSJ"]
        assert rows.first.locator("[data-repeater-up]").get_attribute("aria-disabled") == "true"
        rows.first.locator("[data-repeater-remove]").click()
        assert codes() == ["NY_NYT", "USAT", "AZ_ADS"]
        assert rows.first.locator("[data-repeater-open]").evaluate(
            "el => el === document.activeElement"
        )
        assert open_codes() == []
        card.locator(".dx-save-bar-discard").click()
        page.wait_for_timeout(50)
        assert codes() == served
        assert open_codes() == []
        assert all(
            rows.nth(i).locator("[data-repeater-open]").get_attribute("aria-expanded") == "false"
            for i in range(rows.count())
        )
        browser.close()
    assert errors == []


@pytest.mark.parametrize("live", ["news"], indirect=True)
def test_ai_news_invalid_field_is_shown_before_saving(live):
    """A field that fails validation stops the save and gets the focus; once
    fixed, the save goes."""
    from playwright.sync_api import sync_playwright

    base, app = live
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{base}/plugins/news_illustrated/", wait_until="networkidle")
        form = page.locator("#plugin-news_illustrated")
        words = form.locator('[name="summary_words"]')
        words.fill("5")  # under the field's minimum
        form.locator(".dx-save-bar-save").click()
        page.wait_for_timeout(200)
        assert words.evaluate("el => el === document.activeElement && !el.validity.valid")
        words.fill("60")
        with page.expect_navigation():
            form.locator(".dx-save-bar-save").click()
        browser.close()
    plugin = app.config["PLUGIN_REGISTRY"].get("news_illustrated")
    saved = app.config["SETTINGS_STORE"].get_for_runtime(
        "plugins", plugin.id, plugin.manifest["settings"]
    )
    assert saved["summary_words"] == 60


@pytest.mark.parametrize("live", ["news"], indirect=True)
def test_news_admin_pages_work_without_javascript(live):
    """No scripts: every paper's fields show (rows can't be folded), the
    page says the list needs JavaScript, and each settings card has a plain
    Save button; the AI news settings save without scripts too (the
    provider order keeps its saved value)."""
    from playwright.sync_api import sync_playwright

    base, app = live
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        # networkidle: leaving a page mid-download of a font leaves the
        # served file to the garbage collector, a ResourceWarning in pytest.
        page.goto(f"{base}/plugins/news_front_pages/", wait_until="networkidle")
        card = page.locator("#plugin-news_front_pages")
        codes = card.locator('[data-repeater-key="code"]')
        assert codes.count() == 4
        assert all(codes.nth(i).is_visible() for i in range(4))
        assert card.locator(".repeater-row-chev").first.is_hidden()
        # (Playwright's text and role engines skip <noscript>; CSS reaches it.)
        note = card.locator("noscript").first
        assert note.is_visible() and "needs JavaScript" in note.inner_html()
        assert card.locator("noscript button[type=submit]").is_visible()

        page.goto(f"{base}/plugins/news_illustrated/", wait_until="networkidle")
        ai = page.locator("#plugin-news_illustrated")
        sources = ai.locator('[name="sources"]')
        sources.fill("Local | https://example.test/rss")
        with page.expect_navigation():
            ai.locator("noscript button[type=submit]").click()
        page.wait_for_load_state("networkidle")
        context.close()
        browser.close()
    plugin = app.config["PLUGIN_REGISTRY"].get("news_illustrated")
    saved = app.config["SETTINGS_STORE"].get_for_runtime(
        "plugins", plugin.id, plugin.manifest["settings"]
    )
    assert saved["sources"] == "Local | https://example.test/rss"
    assert saved["provider_order"] == "kie, gemini, pollinations, huggingface"
