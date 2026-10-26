"""news_front_pages: the composer renders a front page at every supported
size and serves the image; plus the background downloads, fallbacks, rotation,
locking, pruning and paper-list rules in server.py. Freedom Forum is faked
throughout: the fake ``urlopen`` answers from a dict of URL → page and 404s
everything else."""

from __future__ import annotations

import contextvars
import http.client
import io
import os
import threading
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
IDLE = 2 * server.PRUNE_GRACE_S


def _jpeg(width: int = 700, height: int = 1342) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (width, height), 255).save(buf, format="JPEG", quality=70)
    return buf.getvalue()


PAGE = _jpeg()


def _url(code: str, day: int) -> str:
    return f"{CDN}/jpg{day}/lg/{code}.jpg"


class _Resp:
    """A response: its body, headers, a read error to raise, or a callback run
    on every read (to drip bytes slowly)."""

    def __init__(
        self,
        body: bytes,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
        on_read: Any = None,
    ) -> None:
        self._body = io.BytesIO(body)
        self._error = error
        self._on_read = on_read
        self.headers = headers or {}
        self.reads = 0

    def read1(self, limit: int = -1) -> bytes:
        self.reads += 1
        if self._on_read is not None:
            self._on_read()
            return self._body.read(1)
        if self._error is not None:
            raise self._error
        return self._body.read1(limit)

    def read(self, limit: int = -1) -> bytes:
        if self._error is not None:
            raise self._error
        return self._body.read(limit)

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class Clocks:
    """The wall clock (persisted times; starts at the real time so file mtimes
    line up) and the monotonic one (a download's budget), both moved by hand."""

    def __init__(self) -> None:
        self.wall = time.time()
        self.mono = 1000.0


# The fake records the value the worker thread sees here, to show the worker
# runs in a copy of the render's context (where the egress allowlist lives).
CONTEXT_PROBE: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="")


class FakeCdn:
    """Stand-in for ``urllib.request.urlopen``. ``pages`` maps a URL to a body
    (or a ``_Resp``); anything else is a 404, or a refused connection when
    ``down`` is set. With ``gate`` set, a request waits until it opens. Every
    URL asked for lands in ``calls``."""

    def __init__(self, pages: dict[str, bytes | _Resp] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []
        self.down = False
        self.gate: threading.Event | None = None
        self.contexts: list[str] = []

    def __call__(self, req: Any, timeout: float | None = None) -> _Resp:
        url = req if isinstance(req, str) else req.full_url
        self.calls.append(url)
        self.contexts.append(CONTEXT_PROBE.get())
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never opened"
        if self.down:
            raise urllib.error.URLError("connection refused")
        hit = self.pages.get(url)
        if hit is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))  # type: ignore[arg-type]
        return hit if isinstance(hit, _Resp) else _Resp(hit)


@pytest.fixture
def clocks(monkeypatch: pytest.MonkeyPatch) -> Clocks:
    c = Clocks()
    monkeypatch.setattr(server, "_wall", lambda: c.wall)
    monkeypatch.setattr(server, "_clock", lambda: c.mono)
    return c


@pytest.fixture
def cdn(monkeypatch: pytest.MonkeyPatch, clocks: Clocks, tmp_path: Path) -> Iterator[FakeCdn]:
    fake = FakeCdn()
    monkeypatch.setattr(server, "_edition_today", lambda: TODAY)
    with patch("urllib.request.urlopen", fake):
        try:
            yield fake
        finally:
            if fake.gate is not None:
                fake.gate.set()
            assert server._join_worker(tmp_path, 10)


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


def _settle(tmp_path: Path) -> None:
    assert server._join_worker(tmp_path, 10), "downloads didn't finish"


def _fetch_settled(tmp_path: Path, **kw: Any) -> dict[str, Any]:
    """A render, its downloads, then the render that shows them."""
    _fetch(tmp_path, **kw)
    _settle(tmp_path)
    return _fetch(tmp_path, **kw)


def _seed(tmp_path: Path, name: str, body: bytes = PAGE, *, idle_s: float = 0.0) -> Path:
    """Save a page, last used ``idle_s`` seconds ago."""
    path = tmp_path / "pages" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if idle_s:
        then = time.time() - idle_s
        os.utime(path, (then, then))
    return path


def _state(tmp_path: Path) -> dict[str, Any]:
    return server._read_json(tmp_path / "state.json")


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
    module = _app_module(app)
    monkeypatch.setattr(module, "_edition_today", lambda: TODAY)
    data_dir = Path(app.config["PLUGIN_REGISTRY"].get("news_front_pages").data_dir)
    render = f"/_test/render?plugin=news_front_pages&size={size}"
    fake = FakeCdn({_url(c, 25): PAGE for c in ("NY_NYT", "WSJ", "USAT")})
    with patch("urllib.request.urlopen", fake):
        first = client.get(render)
        assert module._join_worker(data_dir, 10)
        resp = client.get(render)
    assert first.status_code == 200
    assert "pending" in first.get_data(as_text=True)
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


def _seed_today(tmp_path: Path, *codes: str) -> None:
    for code in codes:
        _seed(tmp_path, f"{code}_20261025.jpg")


def _sends(tmp_path: Path, clocks: Clocks, count: int, **kw: Any) -> list[str]:
    """``count`` panel renders, each a hold window after the last."""
    shown = []
    for _ in range(count):
        shown.append(_fetch(tmp_path, **kw)["code"])
        clocks.wall += server.ROTATION_HOLD_S
    return shown


def test_rotation_moves_one_paper_per_send(tmp_path: Path, cdn: FakeCdn, clocks: Clocks) -> None:
    _seed_today(tmp_path, "NY_NYT", "WSJ", "USAT")
    assert _sends(tmp_path, clocks, 4) == ["NY_NYT", "WSJ", "USAT", "NY_NYT"]
    assert _state(tmp_path)["current"] == "NY_NYT"
    assert cdn.calls == []


def test_warm_ahead_send_and_follow_up_show_one_paper(
    tmp_path: Path, cdn: FakeCdn, clocks: Clocks
) -> None:
    """Warm-ahead composes for a send without sending, maybe twice; the send
    and its follow-up come after. All are non-preview renders."""
    _seed_today(tmp_path, "NY_NYT", "WSJ", "USAT")
    shown = []
    for step in (0, 30, 120, 30):  # warm, warm, send, follow-up
        clocks.wall += step
        shown.append(_fetch(tmp_path)["code"])
    assert shown == ["NY_NYT"] * 4
    clocks.wall += server.ROTATION_HOLD_S
    assert _fetch(tmp_path)["code"] == "WSJ"


def test_preview_never_moves_the_rotation(tmp_path: Path, cdn: FakeCdn, clocks: Clocks) -> None:
    _seed_today(tmp_path, "NY_NYT", "WSJ", "USAT")
    assert _fetch(tmp_path)["code"] == "NY_NYT"
    assert _fetch(tmp_path, preview=True)["code"] == "NY_NYT"  # still held
    clocks.wall += server.ROTATION_HOLD_S
    assert _fetch(tmp_path, preview=True)["code"] == "WSJ"  # what the next send gets
    assert _fetch(tmp_path, preview=True)["code"] == "WSJ"
    assert _state(tmp_path)["current"] == "NY_NYT"
    assert _fetch(tmp_path)["code"] == "WSJ"


def test_rotation_skips_a_paper_without_a_page(
    tmp_path: Path, cdn: FakeCdn, clocks: Clocks
) -> None:
    _seed_today(tmp_path, "NY_NYT", "USAT")  # WSJ has nothing, and Freedom Forum 404s it
    assert _sends(tmp_path, clocks, 3) == ["NY_NYT", "USAT", "NY_NYT"]


def test_a_held_paper_that_lost_its_page_moves_on(
    tmp_path: Path, cdn: FakeCdn, clocks: Clocks
) -> None:
    _seed_today(tmp_path, "NY_NYT", "WSJ")
    assert _fetch(tmp_path)["code"] == "NY_NYT"
    (tmp_path / "pages" / "NY_NYT_20261025.jpg").unlink()
    clocks.wall += 10
    assert _fetch(tmp_path)["code"] == "WSJ"
    assert _state(tmp_path)["current"] == "WSJ"


def test_disabled_papers_are_left_out(tmp_path: Path, cdn: FakeCdn, clocks: Clocks) -> None:
    _seed_today(tmp_path, "NY_NYT", "WSJ")
    assert _sends(tmp_path, clocks, 2, papers="# NY_NYT\nWSJ") == ["WSJ", "WSJ"]


def test_rotation_and_plan_helpers() -> None:
    assert server.rotation(["A", "B", "C"], "B") == ["C", "A", "B"]
    assert server.rotation(["A", "B", "C"], "C") == ["A", "B", "C"]
    assert server.rotation(["A", "B", "C"], "GONE") == ["A", "B", "C"]
    held = {"current": "B", "since": 1000.0}
    assert server.plan(["A", "B", "C"], held, 1010.0) == (["B", "C", "A"], "B")
    later = 1000.0 + server.ROTATION_HOLD_S
    assert server.plan(["A", "B", "C"], held, later) == (["C", "A", "B"], None)
    assert server.plan(["A", "C"], held, 1010.0) == (["A", "C"], None)


def test_pinned_paper_ignores_the_rotation(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed_today(tmp_path, "NY_NYT", "TX_HC")
    data = _fetch(tmp_path, paper="tx_hc")
    assert (data["code"], data["trim"]) == ("TX_HC", 0.0)
    assert "current" not in _state(tmp_path)
    assert _fetch(tmp_path)["code"] == "NY_NYT"


def test_bad_pinned_code_and_empty_list_are_errors(tmp_path: Path, cdn: FakeCdn) -> None:
    assert "isn't a Freedom Forum paper code" in _fetch(tmp_path, paper="../x")["error"]
    assert "No papers to show" in _fetch(tmp_path, papers="# NY_NYT")["error"]
    assert cdn.calls == []


def test_payload_carries_size_and_trim(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed_today(tmp_path, "NY_NYT")
    assert _fetch(tmp_path, papers="NY_NYT 3.5") == {
        "url": "/plugins/news_front_pages/page/NY_NYT_20261025.jpg",
        "code": "NY_NYT",
        "date": "2026-10-25",
        "width": 700,
        "height": 1342,
        "trim": 3.5,
    }
    assert cdn.calls == []


# ----- background downloads --------------------------------------------------


def test_first_render_says_fetching_then_shows_the_page(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.pages = {_url("NY_NYT", 25): PAGE}
    first = _fetch(tmp_path, paper="NY_NYT")
    assert first == {"pending": True, "live_refresh_s": server.LIVE_REFRESH_S, "live_pending": 1}
    _settle(tmp_path)
    assert _fetch(tmp_path, paper="NY_NYT")["date"] == "2026-10-25"


def test_a_slow_download_never_holds_up_a_render(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "NY_NYT_20261024.jpg")
    cdn.gate = threading.Event()  # the CDN answers only when the test says so
    cdn.pages = {_url("NY_NYT", 25): PAGE}
    start = time.monotonic()
    first = _fetch(tmp_path, paper="NY_NYT")
    second = _fetch(tmp_path, paper="NY_NYT")
    assert time.monotonic() - start < 1.0
    assert first["date"] == second["date"] == "2026-10-24"
    assert first["live_refresh_s"] == server.LIVE_REFRESH_S
    cdn.gate.set()
    _settle(tmp_path)
    assert _fetch(tmp_path, paper="NY_NYT")["date"] == "2026-10-25"


def test_a_dripping_download_gives_up_at_its_budget(cdn: FakeCdn, clocks: Clocks) -> None:
    def tick() -> None:
        clocks.mono += 1.0  # a byte a second

    drip = _Resp(PAGE, on_read=tick)
    cdn.pages = {_url("NY_NYT", 25): drip}
    with pytest.raises(server.CdnUnreachable):
        server.download_edition("NY_NYT", TODAY)
    assert drip.reads <= server.DOWNLOAD_BUDGET_S + 1


def test_concurrent_renders_trigger_one_download(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.gate = threading.Event()
    cdn.pages = {_url("NY_NYT", 25): PAGE}
    renders = [
        threading.Thread(target=_fetch, args=(tmp_path,), kwargs={"paper": "NY_NYT"})
        for _ in range(6)
    ]
    for thread in renders:
        thread.start()
    for thread in renders:
        thread.join(5)
    _fetch(tmp_path, paper="NY_NYT", fresh=True)  # even a forced one joins the queued job
    cdn.gate.set()
    _settle(tmp_path)
    assert cdn.calls == [_url("NY_NYT", 25)]


def test_downloads_run_in_the_render_context(tmp_path: Path, cdn: FakeCdn) -> None:
    """The host's egress allowlist lives in the render's context; the worker
    must carry it."""
    token = CONTEXT_PROBE.set("render scope")
    try:
        _fetch(tmp_path, paper="NY_NYT")
    finally:
        CONTEXT_PROBE.reset(token)
    _settle(tmp_path)
    assert cdn.contexts and set(cdn.contexts) == {"render scope"}


def test_falls_back_across_a_month_boundary(
    tmp_path: Path, cdn: FakeCdn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_edition_today", lambda: date(2026, 11, 1))
    cdn.pages = {_url("WSJ", 31): PAGE}
    assert _fetch_settled(tmp_path, paper="WSJ")["date"] == "2026-10-31"
    assert cdn.calls == [_url("WSJ", 1), _url("WSJ", 31)]


def test_gives_up_after_three_days(tmp_path: Path, cdn: FakeCdn) -> None:
    data = _fetch_settled(tmp_path, paper="WSJ")
    assert data["error"] == "No WSJ front page in the last 3 days."
    assert cdn.calls == [_url("WSJ", 25), _url("WSJ", 24), _url("WSJ", 23)]


def test_todays_page_on_disk_needs_no_network(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "NY_NYT_20261025.jpg")
    data = _fetch(tmp_path, paper="NY_NYT")
    assert data["date"] == "2026-10-25"
    assert "live_refresh_s" not in data
    assert cdn.calls == []


def test_an_older_kept_edition_ends_the_lookback(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "NY_NYT_20261024.jpg")
    assert _fetch_settled(tmp_path, paper="NY_NYT")["date"] == "2026-10-24"
    assert cdn.calls == [_url("NY_NYT", 25)]


def test_keeps_the_last_good_page_when_freedom_forum_is_down(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "WSJ_20261020.jpg")
    cdn.down = True
    assert _fetch_settled(tmp_path, paper="WSJ")["date"] == "2026-10-20"
    assert len(cdn.calls) == 1  # an outage ends the job


def test_outage_with_nothing_saved_says_so(tmp_path: Path, cdn: FakeCdn) -> None:
    cdn.down = True
    data = _fetch_settled(tmp_path)
    assert data["error"].startswith("Couldn't reach Freedom Forum")
    assert len(cdn.calls) == 3  # one try per paper, not three days each


def test_a_refused_egress_is_recorded_like_an_outage(
    tmp_path: Path, cdn: FakeCdn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host's allowlist raises its own error type; the job still ends
    cleanly, recorded, and the next paper's job still runs."""

    def denied(code: str, day: date) -> bytes | None:
        raise RuntimeError(f"Capability denied: {code}")

    monkeypatch.setattr(server, "download_edition", denied)
    data = _fetch_settled(tmp_path)
    assert data["error"].startswith("Couldn't reach Freedom Forum")
    assert set(_state(tmp_path)["failed"]) == {"NY_NYT", "WSJ", "USAT"}


def test_a_response_cut_off_mid_body_keeps_the_last_good_page(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "NY_NYT_20261024.jpg")
    cut = _Resp(b"", error=http.client.IncompleteRead(b"partial", 600_000))
    cdn.pages = {_url("NY_NYT", 25): cut}
    assert _fetch_settled(tmp_path, paper="NY_NYT")["date"] == "2026-10-24"
    assert len(cdn.calls) == 1
    assert "NY_NYT" in _state(tmp_path)["checked"]


def test_a_missing_edition_is_rechecked_after_a_while(
    tmp_path: Path, cdn: FakeCdn, clocks: Clocks
) -> None:
    _seed(tmp_path, "NY_NYT_20261024.jpg")
    _fetch_settled(tmp_path, paper="NY_NYT")
    _fetch_settled(tmp_path, paper="NY_NYT")
    assert len(cdn.calls) == 1
    _fetch_settled(tmp_path, paper="NY_NYT", fresh=True)
    assert len(cdn.calls) == 2
    clocks.wall += server.RECHECK_S
    cdn.pages = {_url("NY_NYT", 25): PAGE}
    assert _fetch_settled(tmp_path, paper="NY_NYT")["date"] == "2026-10-25"


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
    assert "error" in _fetch_settled(tmp_path, paper="NY_NYT")
    assert not list((tmp_path / "pages").glob("*.jpg"))


def test_refuses_last_months_file_in_a_reused_slot(tmp_path: Path, cdn: FakeCdn) -> None:
    stale = _Resp(PAGE, {"Last-Modified": "Fri, 25 Sep 2026 05:06:06 GMT"})
    fresh = _Resp(PAGE, {"Last-Modified": "Sat, 24 Oct 2026 08:54:04 GMT"})
    cdn.pages = {_url("WSJ", 25): stale, _url("WSJ", 24): fresh}
    assert _fetch_settled(tmp_path, paper="WSJ")["date"] == "2026-10-24"


# ----- the lock and handed-out pages -----------------------------------------


def test_a_handed_out_page_is_claimed(tmp_path: Path, cdn: FakeCdn) -> None:
    page = _seed(tmp_path, "WSJ_20261024.jpg", idle_s=IDLE)
    _fetch(tmp_path, paper="WSJ")
    assert time.time() - page.stat().st_mtime < 60


def test_a_busy_lock_answers_with_the_last_claimed_page(
    tmp_path: Path, cdn: FakeCdn, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, "NY_NYT_20261025.jpg")
    claimed = _fetch(tmp_path, paper="NY_NYT")
    monkeypatch.setattr(server, "LOCK_WAIT_S", 0.05)
    with server._LOCK:
        assert _fetch(tmp_path, paper="NY_NYT") == claimed
        # A placement that has had nothing claimed gets no URL at all.
        unclaimed = _fetch(tmp_path)
    assert unclaimed == {"pending": True, "live_refresh_s": server.LIVE_REFRESH_S}


def test_a_page_just_handed_out_survives_a_newer_download(tmp_path: Path, cdn: FakeCdn) -> None:
    """One render serves a 10-day-old last good page; a moment later a newer
    edition lands and pruning runs. The renderer still loading the old
    page's URL must not get a 404."""
    old = _seed(tmp_path, "WSJ_20261015.jpg", idle_s=IDLE)
    cdn.down = True
    assert _fetch_settled(tmp_path, paper="WSJ")["url"].endswith("/WSJ_20261015.jpg")
    cdn.down = False
    cdn.pages = {_url("WSJ", 25): PAGE, _url("USAT", 25): PAGE}
    assert _fetch_settled(tmp_path, paper="WSJ", fresh=True)["date"] == "2026-10-25"
    assert old.exists()
    then = time.time() - IDLE
    os.utime(old, (then, then))
    _fetch_settled(tmp_path, paper="USAT")  # the next download prunes it
    assert not old.exists()


def test_prunes_old_editions_but_keeps_each_papers_last_good(tmp_path: Path, cdn: FakeCdn) -> None:
    keep = [
        _seed(tmp_path, "NY_NYT_20261021.jpg", idle_s=IDLE),  # within KEEP_DAYS
        _seed(tmp_path, "WSJ_20261010.jpg", idle_s=IDLE),  # old, but WSJ's newest
        _seed(tmp_path, "NY_NYT_20261013.jpg"),  # old and superseded, but just used
    ]
    gone = [
        _seed(tmp_path, "NY_NYT_20261012.jpg", idle_s=IDLE),  # old and superseded
        _seed(tmp_path, "AZ_ADS_20260901.jpg", idle_s=IDLE),  # past LAST_GOOD_MAX_DAYS
        _seed(tmp_path, ".NY_NYT_20261025.jpg.abc.tmp", idle_s=IDLE),  # interrupted write
    ]
    cdn.pages = {_url("NY_NYT", 25): PAGE}
    _fetch_settled(tmp_path, paper="NY_NYT")
    assert all(p.exists() for p in keep)
    assert not any(p.exists() for p in gone)


def test_too_old_a_page_is_not_shown(tmp_path: Path, cdn: FakeCdn) -> None:
    _seed(tmp_path, "WSJ_20260901.jpg")
    cdn.down = True
    assert "error" in _fetch_settled(tmp_path, paper="WSJ")


# ----- damaged saved pages ---------------------------------------------------


@pytest.mark.parametrize("body", [b"", PAGE[:2000]], ids=["empty", "damaged"])
def test_a_damaged_saved_page_falls_back_to_a_good_one(
    tmp_path: Path, cdn: FakeCdn, body: bytes
) -> None:
    broken = _seed(tmp_path, "NY_NYT_20261025.jpg", body)
    _seed(tmp_path, "NY_NYT_20261024.jpg")
    cdn.down = True
    data = _fetch(tmp_path, paper="NY_NYT")
    assert (data["date"], data["width"], data["height"]) == ("2026-10-24", 700, 1342)
    assert not broken.exists()


def test_a_damaged_saved_page_is_downloaded_again(
    tmp_path: Path, cdn: FakeCdn, clocks: Clocks
) -> None:
    broken = _seed(tmp_path, "NY_NYT_20261025.jpg", b"")
    server._write_json(tmp_path / "state.json", {"checked": {"NY_NYT": clocks.wall}})
    cdn.pages = {_url("NY_NYT", 25): PAGE}
    data = _fetch_settled(tmp_path, paper="NY_NYT", fresh=True)
    assert (data["date"], data["width"]) == ("2026-10-25", 700)
    assert broken.read_bytes() == PAGE


def test_deleting_refuses_a_replaced_file(tmp_path: Path) -> None:
    path = _seed(tmp_path, "NY_NYT_20261025.jpg", b"")
    judged = server._identity(path)
    server._write_atomic(path, PAGE)  # a download renames over it
    assert not server._unlink_if_same(path, judged)
    assert path.read_bytes() == PAGE


def test_validation_keeps_a_replacement_written_while_it_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed(tmp_path, "NY_NYT_20261025.jpg", b"")
    real = server.page_size

    def replaced_meanwhile(body: bytes) -> tuple[int, int] | None:
        if not body:
            server._write_atomic(path, PAGE)  # another writer, mid-judgement
        return real(body)

    monkeypatch.setattr(server, "page_size", replaced_meanwhile)
    server.newest_page(tmp_path / "pages", "NY_NYT", TODAY)
    assert path.read_bytes() == PAGE


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
