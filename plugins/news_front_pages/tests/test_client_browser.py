"""client.js in a live Chromium, through the host's own /_test/render page:
a page's PDF is drawn with the vendored PDF.js onto a canvas at the cell's
pixel width (times the device pixel ratio), and the JPEG stands in when the
PDF is broken or takes too long.

The PDF is one this file writes (vector shapes, no fonts), so the tests need
no network. Set ``NEWS_FRONT_PAGES_LIVE_PDF=1`` to also draw today's New York
Times PDF, downloaded from Freedom Forum when the test starts."""

from __future__ import annotations

import io
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from flask import Flask
from PIL import Image

from app.main import create_app

REPO_ROOT = Path(os.environ.get("TESSERAE_TEST_ROOT", Path(__file__).resolve().parents[3]))
PLUGIN = "news_front_pages"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(
                os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", p.chromium.executable_path)
            ).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="Playwright Chromium not installed"
)


def vector_pdf(width_pt: int = 612, height_pt: int = 1188) -> bytes:
    """A one-page PDF drawn with vector shapes only: white, a black band
    across the top tenth, and a hairline grid below it."""
    band = height_pt // 10
    ops = [f"0 g 0 {height_pt - band} {width_pt} {band} re f", "0 G 1 w"]
    for x in range(36, width_pt, 36):
        ops.append(f"{x} 36 m {x} {height_pt - band - 36} l S")
    content = "\n".join(ops).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt} {height_pt}] "
        "/Contents 4 0 R /Resources << >> >>".encode("ascii"),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def _jpeg(width: int = 700, height: int = 1359) -> bytes:
    img = Image.new("L", (width, height), 255)
    img.paste(0, (0, 0, width, height // 10))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


@pytest.fixture
def live(tmp_path: Path) -> Iterator[tuple[str, Flask]]:
    from werkzeug.serving import make_server

    app = create_app(
        testing=False,
        data_root=tmp_path / "data",
        plugins_dir=Path(os.environ.get("TESSERAE_TEST_PLUGINS", REPO_ROOT / "plugins")),
    )
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


def _no_network(req: Any, timeout: float | None = None) -> Any:
    url = req if isinstance(req, str) else req.full_url
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))  # type: ignore[arg-type]


def _edition(app: Flask, pdf: bytes | None, jpeg: bytes | None = None) -> Path:
    """Today's NY_NYT edition saved (the JPEG, and ``pdf`` beside it), and
    the paper list cut to NY_NYT, so a render downloads nothing."""
    plugin = app.config["PLUGIN_REGISTRY"].get(PLUGIN)
    module = plugin.server_module
    data_dir = Path(plugin.data_dir)
    module.save_config(
        data_dir,
        {"papers": [{"enabled": True, "code": "NY_NYT", "css": ""}], "small_screens": "top"},
    )
    page = Path(module.edition_path(data_dir / "pages", "NY_NYT", module._edition_today()))
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(jpeg or _jpeg())
    if pdf is not None:
        module.pdf_path(page).write_bytes(pdf)
    return page


# What the render left in the widget's shadow root.
_INSPECT = """() => {
  const cell = document.querySelector('.cell[data-plugin="news_front_pages"]');
  const host = cell.querySelector('.cell-content') || cell;
  const root = host.shadowRoot;
  const canvas = root.querySelector('canvas.page');
  const img = root.querySelector('img.page');
  const out = {cellW: host.getBoundingClientRect().width};
  if (img) out.img = {src: img.getAttribute('src'), w: img.naturalWidth, complete: img.complete};
  if (canvas) {
    const ctx = canvas.getContext('2d');
    const luma = (x, y) => {
      const d = ctx.getImageData(Math.floor(x), Math.floor(y), 1, 1).data;
      return Math.round((d[0] + d[1] + d[2]) / 3);
    };
    let dark = 0;
    const row = ctx.getImageData(0, Math.floor(canvas.height / 2), canvas.width, 1).data;
    for (let i = 0; i < row.length; i += 4) if (row[i] < 128) dark += 1;
    out.canvas = {
      w: canvas.width, h: canvas.height,
      cssW: canvas.getBoundingClientRect().width,
      visibility: getComputedStyle(canvas).visibility,
      ms: Number(canvas.dataset.drawnMs),
      band: luma(canvas.width / 2, 4),
      paper: luma(canvas.width / 2 + 8, canvas.height * 0.97),
      darkInRow: dark,
    };
  }
  return out;
}"""


def _render(
    base: str,
    *,
    scale: float = 1,
    query: str = "size=lg",
    vw: int = 1200,
    vh: int = 800,
    logs: list[str] | None = None,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        page = browser.new_page(viewport={"width": vw, "height": vh}, device_scale_factor=scale)
        if logs is not None:
            page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
        page.goto(f"{base}/_test/render?plugin={PLUGIN}&{query}", wait_until="load")
        page.wait_for_function("window.__tesseraeComposed === true", timeout=30_000)
        result: dict[str, Any] = page.evaluate(_INSPECT)
        if "img" in result and not result["img"]["complete"]:
            page.wait_for_function(
                """() => {
                  const c = document.querySelector('.cell[data-plugin="news_front_pages"]');
                  const h = c.querySelector('.cell-content') || c;
                  const i = h.shadowRoot.querySelector('img.page');
                  return i && i.complete;
                }""",
                timeout=10_000,
            )
            result = page.evaluate(_INSPECT)
        browser.close()
    return result


def test_the_pdf_is_drawn_at_the_cells_width(live: tuple[str, Flask]) -> None:
    base, app = live
    _edition(app, vector_pdf())
    logs: list[str] = []
    with patch("urllib.request.urlopen", _no_network):
        got = _render(base, logs=logs)
    canvas = got["canvas"]
    assert "img" not in got  # the JPEG never came into it
    assert canvas["w"] == round(got["cellW"]) == 1200  # one canvas pixel per screen pixel
    assert canvas["h"] == round(1200 * 1188 / 612)
    assert canvas["cssW"] == 1200
    assert canvas["visibility"] == "visible"
    assert canvas["band"] < 40 and canvas["paper"] > 215  # drawn, not blank
    assert canvas["darkInRow"] >= 10  # the hairlines
    assert canvas["ms"] > 0
    assert not [line for line in logs if line.startswith("error")]


def test_the_canvas_follows_the_device_pixel_ratio(live: tuple[str, Flask]) -> None:
    base, app = live
    _edition(app, vector_pdf())
    with patch("urllib.request.urlopen", _no_network):
        got = _render(base, scale=2)
    assert got["canvas"]["cssW"] == 1200
    assert got["canvas"]["w"] == 2400


def test_a_broken_pdf_falls_back_to_the_jpeg(live: tuple[str, Flask]) -> None:
    base, app = live
    _edition(app, b"%PDF-1.7\nthis is not a PDF at all\n%%EOF\n")
    logs: list[str] = []
    with patch("urllib.request.urlopen", _no_network):
        got = _render(base, logs=logs)
    assert "img" not in got
    assert got["canvas"]["w"] == 700  # the fallback JPEG, processed at its own size
    assert got["canvas"]["band"] < 40 and got["canvas"]["paper"] > 215
    assert any("drew the JPEG, the PDF failed" in line for line in logs)


def test_a_missing_pdf_falls_back_to_the_jpeg(live: tuple[str, Flask]) -> None:
    """The PDF went between the render's data and the browser asking for it
    (pruned, say): a 404, so the JPEG."""
    base, app = live
    _edition(app, vector_pdf())
    module = app.config["PLUGIN_REGISTRY"].get(PLUGIN).server_module
    real = module._pdf_beside

    def gone_after_the_look(path: Path) -> Path | None:
        found: Path | None = real(path)
        if found is not None:
            found.unlink()
        return found

    with (
        patch("urllib.request.urlopen", _no_network),
        patch.object(module, "_pdf_beside", gone_after_the_look),
    ):
        got = _render(base)
    assert "img" not in got
    assert got["canvas"]["w"] == 700
    assert got["canvas"]["band"] < 40 and got["canvas"]["paper"] > 215


def test_a_pdf_over_its_budget_gives_up(live: tuple[str, Flask]) -> None:
    """drawPdf stops at its budget and says so; render() then shows the JPEG."""
    from playwright.sync_api import sync_playwright

    base, app = live
    page_file = _edition(app, vector_pdf())
    pdf_url = f"/plugins/{PLUGIN}/page/{page_file.with_suffix('.pdf').name}"
    with patch("urllib.request.urlopen", _no_network), sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        page = browser.new_page()
        page.goto(f"{base}/_test/render?plugin={PLUGIN}&size=lg", wait_until="load")
        page.wait_for_function("window.__tesseraeComposed === true", timeout=30_000)
        outcome = page.evaluate(
            """async (url) => {
              const m = await import('/plugins/news_front_pages/client.js');
              const c = document.createElement('canvas');
              document.body.append(c);
              try { await m.drawPdf(c, url, 1); return 'drawn'; }
              catch (err) { return String(err.message); }
            }""",
            pdf_url,
        )
        browser.close()
    assert outcome == "took longer than 1 ms"


def _todays_nyt_pdf() -> bytes:
    """Today's (or the latest of the last three days') New York Times PDF."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("America/New_York")).date()
    for back in range(3):
        day = today - timedelta(days=back)
        url = f"https://cdn.freedomforum.org/dfp/pdf{day.day}/NY_NYT.pdf"
        request = urllib.request.Request(
            url, headers={"User-Agent": "tesserae/0.1 (+news_front_pages)"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return bytes(resp.read())
        except urllib.error.HTTPError:
            continue
    pytest.skip("no New York Times PDF in the last three days")


@pytest.mark.skipif(
    os.environ.get("NEWS_FRONT_PAGES_LIVE_PDF") != "1", reason="set NEWS_FRONT_PAGES_LIVE_PDF=1"
)
def test_a_real_front_page_is_drawn_on_the_big_portrait_panel(live: tuple[str, Flask]) -> None:
    body = _todays_nyt_pdf()
    base, app = live
    _edition(app, body)
    with patch("urllib.request.urlopen", _no_network):
        got = _render(base, query="size=lg&w=1440&h=2560", vw=1440, vh=2560)
    canvas = got["canvas"]
    assert canvas["w"] == 1440 and canvas["visibility"] == "visible"
    assert canvas["darkInRow"] > 50  # text and pictures across the middle of the page
    print(f"NY_NYT PDF ({len(body) // 1024} KB) drawn 1440 px wide in {canvas['ms']} ms")
