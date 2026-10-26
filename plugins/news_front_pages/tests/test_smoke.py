"""news_front_pages: the composer renders a front page at every supported
size and serves the image; plus the download, fallback, rotation, pruning and
paper-list rules in server.py. Freedom Forum is faked throughout: the fake
``urlopen`` answers from a dict of URL → page and 404s everything else."""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient
from PIL import Image

from plugins.news_front_pages import server

TODAY = date(2026, 10, 25)
CDN = "https://cdn.freedomforum.org/dfp"


def _jpeg(width: int = 700, height: int = 1342) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (width, height), 255).save(buf, format="JPEG", quality=70)
    return buf.getvalue()


PAGE = _jpeg()


def _url(code: str, day: int) -> str:
    return f"{CDN}/jpg{day}/lg/{code}.jpg"


class _Resp:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeCdn:
    """Stand-in for ``urllib.request.urlopen``. ``pages`` maps a URL to a body
    (or a ``_Resp`` for custom headers); anything else is a 404, or a refused
    connection when ``down`` is set. Every URL asked for lands in ``calls``."""

    def __init__(self, pages: dict[str, bytes | _Resp] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []
        self.down = False

    def __call__(self, req: Any, timeout: float | None = None) -> _Resp:
        url = req if isinstance(req, str) else req.full_url
        self.calls.append(url)
        if self.down:
            raise urllib.error.URLError("connection refused")
        hit = self.pages.get(url)
        if hit is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        return hit if isinstance(hit, _Resp) else _Resp(hit)


@pytest.fixture
def cdn(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeCdn]:
    fake = FakeCdn()
    monkeypatch.setattr(server, "_edition_today", lambda: TODAY)
    with patch("urllib.request.urlopen", fake):
        yield fake


def _fetch(
    tmp_path: Path,
    *,
    paper: str = "",
    papers: str | None = None,
    preview: bool = False,
    fresh: bool = False,
) -> dict[str, Any]:
    settings = {} if papers is None else {"papers": papers}
    ctx = {"data_dir": str(tmp_path), "preview": preview, "fresh": fresh}
    return server.fetch({"paper": paper}, settings, ctx=ctx)


def _seed(tmp_path: Path, name: str, body: bytes = PAGE) -> Path:
    path = tmp_path / "pages" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


# ----- rendering through the app ---------------------------------------------


def _app_module(app: Flask) -> ModuleType:
    """The loader imports server.py under its own module name, so the app's copy
    is patched separately from the one this file imports."""
    module = app.config["PLUGIN_REGISTRY"].get("news_front_pages").server_module
    assert module is not None
    return module  # type: ignore[no-any-return]


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_widget_renders(
    app: Flask, client: FlaskClient, monkeypatch: pytest.MonkeyPatch, size: str
) -> None:
    monkeypatch.setattr(_app_module(app), "_edition_today", lambda: TODAY)
    fake = FakeCdn({_url("NY_NYT", 25): PAGE})
    with patch("urllib.request.urlopen", fake):
        resp = client.get(f"/_test/render?plugin=news_front_pages&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-plugin="news_front_pages"' in body
    assert "/plugins/news_front_pages/page/NY_NYT_20261025.jpg" in body

    image = client.get("/plugins/news_front_pages/page/NY_NYT_20261025.jpg")
    assert image.status_code == 200
    assert image.mimetype == "image/jpeg"
    assert image.data == PAGE
    image.close()


def test_page_route_serves_only_page_files(app: Flask, client: FlaskClient) -> None:
    pages = Path(app.config["PLUGIN_REGISTRY"].get("news_front_pages").data_dir) / "pages"
    pages.mkdir(parents=True)
    (pages / "notes.jpg").write_bytes(PAGE)
    assert client.get("/plugins/news_front_pages/page/notes.jpg").status_code == 404
    assert client.get("/plugins/news_front_pages/page/state.json").status_code == 404
    assert client.get("/plugins/news_front_pages/page/WSJ_20261025.jpg").status_code == 404


def test_page_route_is_render_safe(app: Flask) -> None:
    assert "news_front_pages.serve_page" in app.config["RENDER_SAFE_ENDPOINTS"]


# ----- the papers setting ----------------------------------------------------


def test_parse_papers_defaults() -> None:
    assert server.parse_papers(server.DEFAULT_PAPERS) == [
        server.Paper("NY_NYT", True, 3.5),
        server.Paper("WSJ", True, 2.0),
        server.Paper("USAT", True, 0.0),
        server.Paper("AZ_ADS", False, 0.0),
    ]


def test_parse_papers_skips_what_it_cannot_read() -> None:
    text = "# My papers\nny_nyt\n\nbad code!\nWSJ x\nUSAT 80\nNY_NYT 4\n#  tx_hc 1.5\nBRA^PR"
    assert server.parse_papers(text) == [
        server.Paper("NY_NYT", True, 0.0),
        server.Paper("USAT", True, server.MAX_TRIM_PCT),
        server.Paper("TX_HC", False, 1.5),
    ]


# ----- rotation --------------------------------------------------------------


def test_rotation_advances_one_paper_per_render(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.pages = {_url(c, 25): PAGE for c in ("NY_NYT", "WSJ", "USAT")}
    shown = [_fetch(tmp_path)["code"] for _ in range(4)]
    assert shown == ["NY_NYT", "WSJ", "USAT", "NY_NYT"]
    assert json.loads((tmp_path / "state.json").read_text())["last"] == "NY_NYT"


def test_preview_shows_the_next_paper_without_advancing(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.pages = {_url(c, 25): PAGE for c in ("NY_NYT", "WSJ", "USAT")}
    assert _fetch(tmp_path)["code"] == "NY_NYT"
    assert _fetch(tmp_path, preview=True)["code"] == "WSJ"
    assert _fetch(tmp_path, preview=True)["code"] == "WSJ"
    assert _fetch(tmp_path)["code"] == "WSJ"


def test_rotation_skips_a_paper_without_a_page(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.pages = {_url("NY_NYT", 25): PAGE, _url("USAT", 25): PAGE}  # no WSJ at all
    assert [_fetch(tmp_path)["code"] for _ in range(3)] == ["NY_NYT", "USAT", "NY_NYT"]


def test_disabled_papers_are_left_out(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.pages = {_url(c, 25): PAGE for c in ("NY_NYT", "WSJ")}
    papers = "# NY_NYT\nWSJ"
    assert [_fetch(tmp_path, papers=papers)["code"] for _ in range(2)] == ["WSJ", "WSJ"]


def test_rotation_order_helper() -> None:
    assert server.rotation(["A", "B", "C"], "B") == ["C", "A", "B"]
    assert server.rotation(["A", "B", "C"], "C") == ["A", "B", "C"]
    assert server.rotation(["A", "B", "C"], "GONE") == ["A", "B", "C"]


def test_pinned_paper_ignores_the_rotation(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.pages = {_url("NY_NYT", 25): PAGE, _url("TX_HC", 25): PAGE}
    data = _fetch(tmp_path, paper="tx_hc")
    assert data["code"] == "TX_HC"
    assert data["trim"] == 0.0
    assert "last" not in server._read_json(tmp_path / "state.json")
    assert _fetch(tmp_path)["code"] == "NY_NYT"


def test_bad_pinned_code_and_empty_list_are_errors(tmp_path: Path, cdn: FakeCdn) -> None:
    assert "isn't a Freedom Forum paper code" in _fetch(tmp_path, paper="../x")["error"]
    assert "No papers to show" in _fetch(tmp_path, papers="# NY_NYT")["error"]
    assert cdn.calls == []


def test_payload_carries_size_and_trim(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.pages = {_url("NY_NYT", 25): PAGE}
    data = _fetch(tmp_path)
    assert data == {
        "url": "/plugins/news_front_pages/page/NY_NYT_20261025.jpg",
        "code": "NY_NYT",
        "date": "2026-10-25",
        "width": 700,
        "height": 1342,
        "trim": 3.5,
    }


# ----- downloading and falling back ------------------------------------------


def test_falls_back_across_a_month_boundary(
    tmp_path: Path, cdn: FakeCdn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_edition_today", lambda: date(2026, 11, 1))
    cdn.pages = {_url("WSJ", 31): PAGE}
    data = _fetch(tmp_path, paper="WSJ")
    assert data["date"] == "2026-10-31"
    assert cdn.calls == [_url("WSJ", 1), _url("WSJ", 31)]
    assert (tmp_path / "pages" / "WSJ_20261031.jpg").is_file()


def test_gives_up_after_three_days(tmp_path: Path, cdn: FakeCdn) -> None:
    data = _fetch(tmp_path, paper="WSJ")
    assert data["error"] == "No WSJ front page in the last 3 days."
    assert cdn.calls == [_url("WSJ", 25), _url("WSJ", 24), _url("WSJ", 23)]


def test_todays_page_on_disk_needs_no_network(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "NY_NYT_20261025.jpg")
    assert _fetch(tmp_path, paper="NY_NYT")["date"] == "2026-10-25"
    assert cdn.calls == []


def test_an_older_kept_edition_ends_the_lookback(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "NY_NYT_20261024.jpg")
    assert _fetch(tmp_path, paper="NY_NYT")["date"] == "2026-10-24"
    assert cdn.calls == [_url("NY_NYT", 25)]


def test_keeps_the_last_good_page_when_freedom_forum_is_down(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "WSJ_20261020.jpg")
    cdn.down = True
    data = _fetch(tmp_path, paper="WSJ")
    assert data["date"] == "2026-10-20"
    assert len(cdn.calls) == 1  # an outage stops the lookback


def test_outage_with_nothing_saved_says_so(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.down = True
    data = _fetch(tmp_path)
    assert data["error"].startswith("Couldn't reach Freedom Forum")
    assert len(cdn.calls) == 1  # and isn't retried for every paper in the list


def test_a_missing_edition_is_rechecked_after_a_while(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "NY_NYT_20261024.jpg")
    _fetch(tmp_path, paper="NY_NYT")
    _fetch(tmp_path, paper="NY_NYT")
    assert len(cdn.calls) == 1
    _fetch(tmp_path, paper="NY_NYT", fresh=True)
    assert len(cdn.calls) == 2
    state = json.loads((tmp_path / "state.json").read_text())
    state["checked"]["NY_NYT"] -= server.RECHECK_S
    (tmp_path / "state.json").write_text(json.dumps(state))
    cdn.pages = {_url("NY_NYT", 25): PAGE}
    assert _fetch(tmp_path, paper="NY_NYT")["date"] == "2026-10-25"


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Access denied</html>",
        PAGE[: len(PAGE) // 2],  # cut off mid-download
        _jpeg(120, 200),  # a thumbnail, not a page
    ],
    ids=["html", "truncated", "tiny"],
)
def test_refuses_what_is_not_a_front_page(tmp_path: Path, cdn: FakeCdn, body: bytes) -> None:
    cdn.pages = {_url("NY_NYT", 25): body}
    assert "error" in _fetch(tmp_path, paper="NY_NYT")
    assert not list((tmp_path / "pages").glob("*.jpg"))


def test_refuses_last_months_file_in_a_reused_slot(tmp_path: Path, cdn: FakeCdn) -> None:
    stale = _Resp(PAGE, {"Last-Modified": "Fri, 25 Sep 2026 05:06:06 GMT"})
    fresh = _Resp(PAGE, {"Last-Modified": "Sat, 24 Oct 2026 08:54:04 GMT"})
    cdn.pages = {_url("WSJ", 25): stale, _url("WSJ", 24): fresh}
    assert _fetch(tmp_path, paper="WSJ")["date"] == "2026-10-24"


# ----- pruning ---------------------------------------------------------------


def test_prunes_old_editions_but_keeps_each_papers_last_good(tmp_path: Path, cdn: FakeCdn) -> None:
    keep = [
        _seed(tmp_path, "NY_NYT_20261021.jpg"),  # within KEEP_DAYS
        _seed(tmp_path, "WSJ_20261010.jpg"),  # old, but WSJ's newest
    ]
    gone = [
        _seed(tmp_path, "NY_NYT_20261012.jpg"),  # old and superseded
        _seed(tmp_path, "AZ_ADS_20260901.jpg"),  # past LAST_GOOD_MAX_DAYS
    ]
    tmp = _seed(tmp_path, ".NY_NYT_20261025.jpg.abc.tmp")
    old = time.time() - 2 * server.TEMP_MAX_AGE_S
    os.utime(tmp, (old, old))
    cdn.pages = {_url("NY_NYT", 25): PAGE}
    _fetch(tmp_path, paper="NY_NYT")
    assert all(p.exists() for p in keep)
    assert not any(p.exists() for p in gone)
    assert not tmp.exists()
    assert (tmp_path / "pages" / "NY_NYT_20261025.jpg").exists()


def test_too_old_a_page_is_not_shown(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "WSJ_20260901.jpg")
    cdn.down = True
    assert "error" in _fetch(tmp_path, paper="WSJ")


# ----- the editor's paper picker ---------------------------------------------

_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://frontpages.freedomforum.org/</loc></url>
<url><loc>https://frontpages.freedomforum.org/newspapers/ia_dr-The_Des_Moines_Register</loc></url>
<url><loc>https://frontpages.freedomforum.org/newspapers/ny_wjny-World_Journal_-_New_York</loc></url>
<url><loc>https://frontpages.freedomforum.org/newspapers/bra^pr_ddc-Di%C3%A1rio_dos_Campos</loc></url>
<url><loc>https://frontpages.freedomforum.org/newspapers/al_td-Times&amp;Daily</loc></url>
</urlset>"""


def _choices(app: Flask) -> list[dict[str, str]]:
    with app.app_context():
        rows: list[dict[str, str]] = _app_module(app).choices("papers")
    return rows


def test_choices_list_rotate_then_the_paper_list_then_freedom_forum(app: Flask) -> None:
    fake = FakeCdn({server.SITEMAP_URL: _SITEMAP.encode()})
    with patch("urllib.request.urlopen", fake):
        rows = _choices(app)
        again = _choices(app)
    assert fake.calls == [server.SITEMAP_URL]  # cached after the first read
    assert rows == again
    values = [r["value"] for r in rows]
    assert values[:5] == ["", "NY_NYT", "WSJ", "USAT", "AZ_ADS"]
    labels = {r["value"]: r["label"] for r in rows}
    assert labels["NY_NYT"] == "The New York Times (NY_NYT)"
    assert labels["IA_DR"] == "The Des Moines Register (IA_DR)"
    assert labels["NY_WJNY"] == "World Journal - New York (NY_WJNY)"
    assert labels["AL_TD"] == "Times&Daily (AL_TD)"
    assert not any("DDC" in v for v in values)


def test_choices_fall_back_when_freedom_forum_is_down(app: Flask) -> None:
    fake = FakeCdn()
    fake.down = True
    with patch("urllib.request.urlopen", fake):
        rows = _choices(app)
        _choices(app)
    assert len(fake.calls) == 1  # a failed read isn't retried on the next editor load
    values = {r["value"] for r in rows}
    assert set(server.FALLBACK_DIRECTORY) <= values


def test_choices_ignores_other_names() -> None:
    assert server.choices("other") == []
