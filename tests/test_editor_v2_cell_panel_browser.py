"""Dashboard editor v2: every bundled widget's settings panel has one shape.

A dashboard with one cell per bundled widget; each row is opened at desktop
and phone width and checked:

- the cell form's children are only the known blocks (description, a
  choices note, the main options, the collapsible sections, the footer), with
  no loose text between them (#: "Checking capture status..." once sat
  between "More Dashboard settings" and "Look of this cell");
- status text lives in the footer's status slot, nowhere else;
- the footer reads: status line, then "Empty this cell" (link, left), then the
  buttons, Done (primary) last, in DOM order and on screen;
- "More <widget> settings" counts the fields it shows.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from app.main import REPO_ROOT, create_app
from app.state.page_store import Cell, Page, Panel

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
pytestmark = [
    pytest.mark.skipif(not _HAS_CHROMIUM, reason="Playwright Chromium not installed"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]

PAGE_ID = "every-widget"

# Direct children a cell form may have (hidden inputs and <noscript> aside),
# in the order they must come.
ALLOWED_BLOCKS = [
    "p.ev2-wsub",
    "p.ev2-note",
    "div.ev2-opts:not(.ev2-opts--tail)",
    "details.ev2-adv",
    "div.ev2-cv-foot",
]

CHECK_JS = """
(allowed) => {
  const out = [];
  const forms = [...document.querySelectorAll('.ev2-cl[data-widget] [data-ev2-cell-form]')];
  for (const form of forms) {
    const id = form.dataset.cellId;
    const plugin = form.querySelector('[data-ev2-plugin]').value;
    EV2.widgets.openRow(id);
    const bad = (msg) => out.push(`${plugin}: ${msg}`);
    const body = form.closest('.ev2-cl-b');
    if (body.hidden) bad('row did not open');

    // 1. Only known blocks, in order, no loose text.
    let rank = 0;
    for (const n of form.childNodes) {
      if (n.nodeType === Node.TEXT_NODE) {
        if (n.textContent.trim()) bad(`loose text "${n.textContent.trim()}"`);
        continue;
      }
      if (n.nodeType !== Node.ELEMENT_NODE) continue;
      if (n.matches('input[type=hidden], noscript')) continue;
      const r = allowed.findIndex((sel) => n.matches(sel));
      if (r < 0) bad(`unexpected block <${n.tagName.toLowerCase()} class="${n.className}">`);
      else if (r < rank) bad(`${allowed[r]} comes after ${allowed[rank]}`);
      else rank = r;
    }
    const blocks = [...form.children].filter((el) => !el.matches('input[type=hidden], noscript'));
    if (!blocks.length || !blocks[blocks.length - 1].matches('.ev2-cv-foot'))
      bad('footer is not the last block');

    // 2. Status text only in the footer slot.
    const foot = form.querySelector(':scope > .ev2-cv-foot');
    const slot = foot && foot.querySelector(':scope > [data-ev2-cell-status]');
    if (!slot) bad('no status slot in the footer');
    else {
      if (slot.getAttribute('role') !== 'status') bad('status slot has no role=status');
      if (slot.getAttribute('aria-live') !== 'polite') bad('status slot is not aria-live=polite');
    }
    form.querySelectorAll('[role=status], [data-capture-status], [data-ev2-status]').forEach((el) => {
      if (el !== slot) bad(`status element outside the footer slot: ${el.outerHTML.slice(0, 80)}`);
    });

    // 3. Footer order: status, link, buttons, Done last; screen agrees.
    if (foot) {
      const kids = [...foot.children].filter((el) => !el.matches('.sp'));
      if (kids[0] !== slot) bad('status slot is not first in the footer');
      const done = foot.querySelector('[data-ev2-done]');
      const empty = foot.querySelector('[data-ev2-empty]');
      if (kids[kids.length - 1] !== done) bad('Done is not last in the footer');
      if (!empty) bad('no "Empty this cell"');
      else {
        const order = kids.indexOf(empty);
        const firstButton = kids.findIndex((el) => el.matches('button:not(.ev2-link), .ev2-mv'));
        if (order > firstButton) bad('"Empty this cell" comes after a button');
        const e = empty.getBoundingClientRect();
        const d = done.getBoundingClientRect();
        if (!(e.right <= d.left + 1 || e.bottom <= d.top + 1))
          bad('"Empty this cell" is not left of (or above) Done on screen');
      }
      if (slot && slot.textContent.trim()) {
        const s = slot.getBoundingClientRect();
        const d = done.getBoundingClientRect();
        if (s.bottom > d.top + 1) bad('status line is not above the footer actions');
      }
    }

    // 4. "More settings" counts what it shows.
    form.querySelectorAll('details[data-opt-more]').forEach((more) => {
      const list = more.querySelector('[data-opt-more-list]');
      const shown = [...list.children].filter((el) => !el.hidden && el.type !== 'hidden').length;
      const count = Number(more.querySelector('[data-opt-more-count]').textContent);
      if (count !== shown) bad(`More settings says ${count}, shows ${shown}`);
      if (!more.hidden && shown === 0) bad('More settings shown with nothing in it');
    });
  }
  return {checked: forms.length, problems: out};
}
"""


@pytest.fixture(scope="module")
def pw() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Flask, list[str]]]:
    from werkzeug.serving import make_server

    app: Flask = create_app(
        testing=False,
        data_root=tmp_path_factory.mktemp("cellpanel") / "data",
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
        devices_dir=REPO_ROOT / "devices",
    )
    app.config["TESTING"] = True
    settings = app.config["SETTINGS_STORE"]
    settings.patch_section("app", {"onboarded": True})
    settings.patch_section("auth", {"disabled": True})
    widgets = sorted(app.config["PLUGIN_REGISTRY"].widgets(), key=lambda p: p.id)
    cols, w, h = 6, 300, 240
    cells = [
        Cell(id=f"w{i:02d}", plugin=p.id, x=(i % cols) * w, y=(i // cols) * h, w=w, h=h)
        for i, p in enumerate(widgets)
    ]
    rows = (len(cells) + cols - 1) // cols
    app.config["PAGE_STORE"].save(
        Page(id=PAGE_ID, name="Every widget", panel=Panel(w=cols * w, h=rows * h), cells=cells)
    )
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", app, [p.id for p in widgets]
    finally:
        srv.shutdown()
        thread.join(timeout=10)


def _open(pw: Any, base: str, width: int) -> tuple[Any, Any, list[str]]:
    browser = pw.chromium.launch(executable_path=_EXE) if _EXE else pw.chromium.launch()
    page = browser.new_page(viewport={"width": width, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/pages/{PAGE_ID}", wait_until="domcontentloaded")
    page.wait_for_function("() => window.EV2 && EV2.widgets && EV2.cellStatus")
    page.evaluate("() => EV2.steps && EV2.steps.open('widgets')")
    return browser, page, errors


@pytest.mark.parametrize("width", [1440, 390])
def test_every_widget_panel_has_one_shape(pw: Any, server: Any, width: int) -> None:
    base, _app, widget_ids = server
    browser, page, errors = _open(pw, base, width)
    try:
        result = page.evaluate(CHECK_JS, ALLOWED_BLOCKS)
        assert result["checked"] == len(widget_ids)
        assert result["problems"] == []
        assert errors == []
    finally:
        browser.close()


def test_capture_status_lands_in_the_footer(pw: Any, server: Any) -> None:
    """ha_dashboard's capture status is filled from the preview's cell data,
    in the footer slot, and nothing about it sits between the sections."""
    base, _app, widget_ids = server
    cell_id = f"w{widget_ids.index('ha_dashboard'):02d}"
    browser, page, errors = _open(pw, base, 1440)
    try:
        page.evaluate("(id) => EV2.widgets.openRow(id)", cell_id)
        slot = page.locator(
            f'[data-ev2-cell-form][data-cell-id="{cell_id}"] [data-ev2-cell-status]'
        )
        assert slot.get_attribute("data-ev2-status-kind") == "capture"
        # No Home Assistant here: the capture reports why it failed.
        page.wait_for_function(
            """(id) => {
              const s = document.querySelector(
                `[data-ev2-cell-form][data-cell-id="${id}"] [data-ev2-cell-status]`);
              return s && s.dataset.tone === 'error' && /Home Assistant/.test(s.textContent);
            }""",
            arg=cell_id,
            timeout=20000,
        )
        assert slot.inner_text().startswith("Capture failed:")
        form_text = page.locator(f'[data-ev2-cell-form][data-cell-id="{cell_id}"]')
        assert "Checking capture status" not in form_text.inner_text()
        assert errors == []
    finally:
        browser.close()


def test_status_api_sets_and_clears(pw: Any, server: Any) -> None:
    base, _app, _ids = server
    browser, page, errors = _open(pw, base, 1440)
    try:
        page.evaluate("() => EV2.widgets.openRow('w00')")
        slot = page.locator('[data-ev2-cell-form][data-cell-id="w00"] [data-ev2-cell-status]')
        assert slot.bounding_box()["height"] <= 1  # empty: no room taken
        page.evaluate("() => EV2.cellStatus.set('w00', 'Connection failed', 'error')")
        assert slot.inner_text() == "Connection failed"
        assert slot.get_attribute("data-tone") == "error"
        assert slot.bounding_box()["height"] > 10
        page.evaluate("() => EV2.cellStatus.clear('w00')")
        assert slot.inner_text() == ""
        assert slot.get_attribute("data-tone") is None
        assert errors == []
    finally:
        browser.close()
