"""comics_daily: sources, Bunny Shield, storage, refresh and render, network mocked.

Every request goes through a fake HTTPS handler installed under urllib's
real opener, so cookie handling, redirects and HTTPError bodies behave as
they do live. Fixtures are trimmed copies of the live pages (2026-10-26).
"""

from __future__ import annotations

import email.message
import gzip
import importlib.util
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.response
import zlib
from datetime import date
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient
from PIL import Image

_PLUGIN = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "_tesserae_plugins.comics_daily_test.server", _PLUGIN / "server.py"
)
assert _SPEC is not None and _SPEC.loader is not None
srv = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = srv
_SPEC.loader.exec_module(srv)
bunny = srv.bunny_shield
pages = srv.pages
images = srv.images

TODAY = date(2026, 10, 25)
# A crafted challenge, and its answer from the reference implementation the
# Visionect cookie-refresh service used (argon2-cffi hash_secret_raw, same
# parameters). Cross-checked when this test was written.
DATA_POW = "userkey-0123456789#chal-abc123#1761500000#sig"
DATA_POW_ANSWER = "170"


def _png(size: tuple[int, int] = (90, 30), mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, "white").save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(size: tuple[int, int] = (95, 110)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 200, 200)).save(buf, format="JPEG")
    return buf.getvalue()


CHALLENGE_PAGE = (
    '<html><body><p>Establishing a secure connection...</p><div id="c" data-pow="'
    + DATA_POW
    + '"></div></body></html>'
)


def _gocomics_page(slug: str, day: str, asset: str) -> str:
    y, m, d = day.split("-")
    return (
        "<html><head>"
        f'<meta property="og:image" content="https://featureassets.gocomics.com/assets/{asset}"/>'
        f'<meta property="og:title" content="Garfield by Jim Davis for October {d}, {y} | GoComics"/>'
        f'<meta property="og:url" content="https://www.gocomics.com/{slug}/{y}/{m}/{d}"/>'
        f'<link rel="canonical" href="https://www.gocomics.com/{slug}/{y}/{m}/{d}"/>'
        '<script src="/.bunny-shield/bd/bunnyprint.js"></script>'
        "</head><body></body></html>"
    )


FARSIDE_PAGE = """
<div class="tfs-content js-daily-dose">
 <div class="tfs-content__1col" data-position="0">
  <div class="card tfs-comic js-comic"><div class="card-body">
   <div class="tfs-comic__image">
    <img data-src="https://featureassets.amuniversal.com/assets/one" data-width="950"
         data-height="1123" class="img-fluid js-lazy-load" alt="ESPRESSO" src="data:x" />
   </div>
   <figure class="figure tfs-comic__caption"><figcaption class="figure-caption">
     While their owners sleep, nervous little dogs prepare for their day.
   </figcaption></figure>
  </div></div>
 </div>
 <div class="tfs-content__1col">
  <div class="card tfs-comic js-comic"><div class="card-body">
   <div class="tfs-comic__image"><img data-src="https://featureassets.amuniversal.com/assets/two" /></div>
   <figure class="figure tfs-comic__caption"><figcaption class="figure-caption"></figcaption></figure>
  </div></div>
 </div>
 <div class="tfs-content__1col">
  <div class="card tfs-comic js-comic"><div class="card-body">
   <div class="tfs-comic__image"><img data-src="https://featureassets.amuniversal.com/assets/three" /></div>
   <figure class="figure"><figcaption>&ldquo;Bummer of a birthmark, Hal.&rdquo;</figcaption></figure>
  </div></div>
 </div>
</div>
<div class="tfs-promo"><img data-src="https://www.thefarside.com/uploads/post_preview_images/x.jpg" /></div>
"""

DILBERT_PAGE = (
    '<main class="w-100 p-1"><h1 class="h4">Wednesday June 14, 1995</h1>'
    '<img class="img-fluid my-3 px-2"alt="Comic for 1995-06-14"src=https://web.archive.org/'
    "web/20230301191927im_/https://assets.amuniversal.com/a37ca4 width=1200 height=364>"
    "<nav></nav></main>"
)


class FakeWeb(urllib.request.HTTPSHandler):
    """Answers requests from a route table; records each one.

    Installed as an urllib handler, so the real opener, cookie processor and
    error handling run around it. A route value is a list of replies served
    in turn (the last repeats): ``(status, headers, body)``.
    """

    def __init__(self, routes: dict[tuple[str, str], list[tuple[int, dict[str, str], bytes]]]):
        super().__init__()
        self.routes = routes
        self.requests: list[urllib.request.Request] = []

    def https_open(self, req: urllib.request.Request) -> Any:
        self.requests.append(req)
        key = (req.get_method(), req.full_url)
        replies = self.routes.get(key)
        if not replies:
            raise urllib.error.URLError(f"no route for {key}")
        status, headers, body = replies.pop(0) if len(replies) > 1 else replies[0]
        msg = email.message.Message()
        for name, value in headers.items():
            msg[name] = value
        resp = urllib.response.addinfourl(io.BytesIO(body), msg, req.full_url, status)
        resp.msg = "OK" if status < 400 else "Error"
        return resp

    def urls(self, method: str = "GET") -> list[str]:
        return [r.full_url for r in self.requests if r.get_method() == method]


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    fake = FakeWeb({})
    real = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: real(*h, fake))
    return fake


def _html(body: str, status: int = 200, **headers: str) -> tuple[int, dict[str, str], bytes]:
    return (status, {"Content-Type": "text/html; charset=utf-8", **headers}, body.encode())


def _img(data: bytes) -> tuple[int, dict[str, str], bytes]:
    return (200, {"Content-Type": "image/png"}, data)


def _gocomics_routes(fake: FakeWeb, slug: str = "garfield") -> None:
    gc = "https://www.gocomics.com"
    fake.routes[("GET", f"{gc}/{slug}/2026/10/25")] = [
        _html(_gocomics_page(slug, "2026-10-25", "abc"), **{"Set-Cookie": "INGRESSCOOKIE=x"})
    ]
    fake.routes[("GET", "https://featureassets.gocomics.com/assets/abc")] = [_img(_png())]


# -- Bunny Shield ----------------------------------------------------------------


def test_challenge_detection() -> None:
    assert bunny.is_challenge({}, CHALLENGE_PAGE)
    assert bunny.is_challenge({"cdn-challenge": "true"}, "")
    # A normal page loads the shield's fingerprint script; that is no challenge.
    assert not bunny.is_challenge({}, _gocomics_page("garfield", "2026-10-25", "abc"))
    assert bunny.find_data_pow(CHALLENGE_PAGE) == DATA_POW
    assert bunny.find_data_pow("<html></html>") is None


def test_argon2id_matches_reference_vector() -> None:
    """cryptography's Argon2id with the challenge's parameters reproduces the
    hash argon2-cffi gave for the same inputs."""
    kdf = bunny._argon2id()(
        salt=b"userkey-0123456789", length=32, iterations=2, lanes=1, memory_cost=512
    )
    assert kdf.derive(b"chal-abc1230").hex() == (
        "a7624216086781e249f5ead85bf630b41e93cc01a8f0521798e8038978b58a91"
    )


def test_solve_finds_the_reference_answer() -> None:
    assert bunny.solve(DATA_POW) == DATA_POW_ANSWER
    assert bunny.challenge_response(DATA_POW, "170") == DATA_POW + "#170"


def test_solve_is_capped_and_rejects_garbage() -> None:
    assert bunny.solve(DATA_POW, max_iterations=5) is None
    with pytest.raises(ValueError):
        bunny.solve("short#chal")
    with pytest.raises(ValueError):
        bunny.solve("nohash")


def _cookie(name: str, value: str, expires: int | None = None) -> Cookie:
    return Cookie(0, name, value, None, False, "www.gocomics.com", False, False, "/", True,
                  True, expires, expires is None, None, None, {})  # fmt: skip


def test_session_cookie_matched_by_prefix_and_expiry() -> None:
    now = 1_800_000_000
    assert bunny.has_session([_cookie("bunny_shield_id_33498", "v")], now)
    assert not bunny.has_session([_cookie("INGRESSCOOKIE", "v")], now)
    assert not bunny.has_session([_cookie("bunny_shield", f"k#s#{now - 5}")], now)
    assert bunny.has_session([_cookie("bunny_shield", f"k#s#{now + 500}")], now)
    assert not bunny.has_session([_cookie("bunny_shield_id_1", "v", expires=now - 1)], now)


# -- page parsers ------------------------------------------------------------------


def test_parse_gocomics_reads_canonical_date_and_label() -> None:
    page = pages.parse_gocomics(_gocomics_page("garfield", "2026-10-24", "abc"), "garfield")
    assert page is not None
    assert page.image_url.endswith("/assets/abc")
    assert page.date == "2026-10-24"
    assert page.label == "Garfield"
    # An unknown name answers a bare page with no Open Graph tags.
    assert pages.parse_gocomics("<html><title>GoComics</title></html>", "nope") is None


def test_parse_farside_cards_only_with_captions() -> None:
    panels = pages.parse_farside(FARSIDE_PAGE)
    assert [p.image_url.rsplit("/", 1)[1] for p in panels] == ["one", "two", "three"]
    assert panels[0].caption.startswith("While their owners sleep")
    assert panels[1].caption == ""
    assert panels[2].caption == "“Bummer of a birthmark, Hal.”"
    assert len(pages.parse_farside(FARSIDE_PAGE, limit=2)) == 2


def test_parse_dilbert_minified_markup() -> None:
    strip = pages.parse_dilbert(DILBERT_PAGE)
    assert strip is not None
    assert strip.date == "1995-06-14"
    assert strip.image_url.startswith("https://web.archive.org/web/")
    assert pages.parse_dilbert("<img alt='logo' src=https://x/y.png>") is None


# -- the Strips option ---------------------------------------------------------------


def test_parse_strips_names_order_comments_and_addresses() -> None:
    text = (
        "garfield, calvinandhobbes\n"
        "# dilbert\n"
        "https://www.gocomics.com/peanuts/2026/10/20\n"
        "pearlsbeforeswine https://featureassets.gocomics.com/assets/xyz\n"
        "garfield\n"
        "bad!name\n"
        "xkcd https://imgs.xkcd.com/comics/a.png\n"
        "dilbert\n"
    )
    sources, problems = srv.parse_strips(text)
    assert [(s.kind, s.name) for s in sources] == [
        ("gocomics", "garfield"),
        ("gocomics", "calvinandhobbes"),
        ("gocomics", "peanuts"),
        ("url", "pearlsbeforeswine"),
        ("dilbert", "dilbert"),
    ]
    assert sources[3].url == "https://featureassets.gocomics.com/assets/xyz"
    assert sources[3].label == "Pearls Before Swine"
    messages = {p.name: p.message for p in problems}
    assert "bad!name" in messages
    assert "imgs.xkcd.com" in messages["xkcd"]


# -- pictures ---------------------------------------------------------------------


def test_normalise_flattens_transparency_and_keeps_jpeg() -> None:
    gif = io.BytesIO()
    frame = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    frame.save(gif, format="GIF")
    picture = images.normalise(gif.getvalue())
    assert picture.ext == "png"
    with Image.open(io.BytesIO(picture.data)) as img:
        assert img.convert("RGB").getpixel((5, 5)) == (255, 255, 255)
    assert images.normalise(_jpeg()).ext == "jpg"
    big = images.normalise(_png((3000, 600)))
    assert (big.width, big.height) == (2400, 480)
    with pytest.raises(ValueError):
        images.normalise(CHALLENGE_PAGE.encode())


def test_store_names_are_validated(tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    name = store.add("gocomics:garfield", images.normalise(_png()))
    assert store.image_path(name) == tmp_path / "images" / name
    assert store.image_path("../state.json") is None
    assert store.image_path("x.png") is None


# -- GoComics through the shield ---------------------------------------------------


def test_gocomics_solves_challenge_on_warm_page_and_keeps_cookies(
    web: FakeWeb, tmp_path: Path
) -> None:
    gc = "https://www.gocomics.com"
    warm = f"{gc}/garfield/2026/10/18"
    web.routes[("GET", warm)] = [
        _html(CHALLENGE_PAGE, status=403),
        _html("<html>real page</html>"),
    ]
    web.routes[("POST", gc + "/.bunny-shield/verify-pow")] = [
        (200, {"Set-Cookie": "bunny_shield_id_33498=s3cret; Path=/"}, b"{}")
    ]
    _gocomics_routes(web)
    cookie_file = tmp_path / srv.COOKIE_FILE

    fetched = srv.GoComics(cookie_file, TODAY).strip("garfield", "Garfield")

    assert fetched.edition == "2026-10-25"
    assert fetched.label == "Garfield"
    asset = "https://featureassets.gocomics.com/assets/abc"
    assert web.urls() == [warm, warm, f"{gc}/garfield/2026/10/25", asset]
    (verify,) = [r for r in web.requests if r.get_method() == "POST"]
    assert verify.get_header("Bunnyshield-challenge-response") == f"{DATA_POW}#{DATA_POW_ANSWER}"
    # Every request carries the Accept headers BunnyCDN insists on.
    for req in web.requests:
        assert req.get_header("Accept", "").startswith("text/html")
        assert req.get_header("Accept-language") == "en-US,en;q=0.5"
    # The strip page was asked for once, with the new session's cookie.
    strip_req = web.requests[3]
    assert "bunny_shield_id_33498=s3cret" in (strip_req.get_header("Cookie") or "")
    assert cookie_file.stat().st_mode & 0o777 == 0o600
    jar = MozillaCookieJar()
    jar.load(str(cookie_file), ignore_discard=True)
    assert bunny.has_session(jar)

    # Next day: the saved session is reused, no warm-up, no proof of work.
    web.requests.clear()
    _gocomics_routes(web, "calvinandhobbes")
    srv.GoComics(cookie_file, TODAY).strip("calvinandhobbes", "Calvin and Hobbes")
    assert web.urls() == [f"{gc}/calvinandhobbes/2026/10/25", asset]
    assert web.urls("POST") == []


def test_gocomics_stale_session_rewarms_once_then_reports_blocked(
    web: FakeWeb, tmp_path: Path
) -> None:
    gc = "https://www.gocomics.com"
    cookie_file = tmp_path / srv.COOKIE_FILE
    jar = MozillaCookieJar()
    jar.set_cookie(_cookie("bunny_shield_id_1", "old"))
    jar.save(str(cookie_file), ignore_discard=True)
    web.routes[("GET", f"{gc}/garfield/2026/10/25")] = [_html(CHALLENGE_PAGE, status=403)]
    web.routes[("GET", f"{gc}/garfield/2026/10/18")] = [_html(CHALLENGE_PAGE, status=403)]
    web.routes[("POST", gc + "/.bunny-shield/verify-pow")] = [(403, {}, b"no")]

    with pytest.raises(srv.SourceError, match="turned down"):
        srv.GoComics(cookie_file, TODAY).strip("garfield", "Garfield")
    # One proof of work attempt, on the warm-up page, not the strip page.
    assert len(web.urls("POST")) == 1
    assert web.urls()[1] == f"{gc}/garfield/2026/10/18"


# -- the other sources ---------------------------------------------------------------


def test_farside_and_dilbert_fetchers(web: FakeWeb) -> None:
    web.routes[("GET", srv.FARSIDE_URL)] = [_html(FARSIDE_PAGE)]
    for name in ("one", "two", "three"):
        web.routes[("GET", f"https://featureassets.amuniversal.com/assets/{name}")] = [
            _img(_jpeg())
        ]
    fetched = srv.fetch_farside(srv.Web())
    assert [caption[:5] for _, caption in fetched.pictures] == ["While", "", "“Bumm"]

    web.routes[("GET", f"{srv.DILBERT}/")] = [_html("<html>awake</html>")]
    picker = srv.random.Random(TODAY.toordinal())
    day = srv.DILBERT_FIRST + srv.timedelta(
        days=picker.randrange((srv.DILBERT_LAST - srv.DILBERT_FIRST).days + 1)
    )
    web.routes[("GET", f"{srv.DILBERT}/{day.isoformat()}")] = [_html(DILBERT_PAGE)]
    web.routes[("GET", pages.parse_dilbert(DILBERT_PAGE).image_url)] = [_img(_png((120, 36)))]
    dilbert = srv.fetch_dilbert(srv.Web(), TODAY)
    assert dilbert.edition == "1995-06-14"


# -- refresh bookkeeping ---------------------------------------------------------------


def test_is_due() -> None:
    go = srv.Source("gocomics:garfield", "gocomics", "garfield")
    now = 1_000_000.0
    today = "2026-10-25"
    assert srv.is_due(go, None, today, now)
    done = {"fetched_on": today, "edition": today, "last_attempt": now - 10, "pictures": [1]}
    assert not srv.is_due(go, done, today, now)
    lagging = dict(done, edition="2026-10-24", attempted_on=today, attempts=1)
    assert not srv.is_due(go, lagging, today, now)
    assert srv.is_due(go, dict(lagging, last_attempt=now - srv.EDITION_RETRY_S), today, now)
    assert not srv.is_due(
        go, dict(lagging, last_attempt=0, attempts=srv.EDITION_ATTEMPTS), today, now
    )
    yesterday = dict(done, fetched_on="2026-10-24", last_attempt=now - 120)
    assert not srv.is_due(go, yesterday, today, now)  # failed two minutes ago: back off
    assert srv.is_due(go, dict(yesterday, last_attempt=now - srv.RETRY_STALE_S), today, now)


def test_failed_refresh_keeps_last_good_and_reports_stale(web: FakeWeb, tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    source = srv.parse_strips("garfield https://featureassets.gocomics.com/assets/m")[0][0]
    web.routes[("GET", source.url)] = [_img(_png())]
    assert srv.refresh_source(store, source, date(2026, 10, 24), web=srv.Web(), gocomics=None)
    first = store.sources()[source.key]["pictures"]

    web.routes[("GET", source.url)] = [_html(CHALLENGE_PAGE)]
    assert not srv.refresh_source(store, source, TODAY, web=srv.Web(), gocomics=None)
    record = store.sources()[source.key]
    assert record["pictures"] == first
    assert "wasn't a picture" in record["error"]

    payload = srv._payload([source], [], store.sources(), set(), {}, store, TODAY.isoformat())
    assert payload["strips"][0]["src"].endswith(first[0]["file"])
    assert payload["notes"] == [
        {"label": "Garfield", "state": "stale", "since": "2026-10-24", "message": record["error"]}
    ]
    assert "error" not in payload


# -- the widget end to end -----------------------------------------------------------------


def _all_routes(fake: FakeWeb) -> None:
    gc = "https://www.gocomics.com"
    fake.routes[("GET", srv.FARSIDE_URL)] = [_html(FARSIDE_PAGE)]
    for name in ("one", "two", "three"):
        fake.routes[("GET", f"https://featureassets.amuniversal.com/assets/{name}")] = [
            _img(_jpeg())
        ]
    fake.routes[("GET", f"{gc}/garfield/2026/10/18")] = [
        _html("<html>ok</html>", **{"Set-Cookie": "bunny_shield_id_9=v; Path=/"})
    ]
    _gocomics_routes(fake)


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_widget_renders(
    client: FlaskClient, web: FakeWeb, monkeypatch: pytest.MonkeyPatch, size: str
) -> None:
    _all_routes(web)
    monkeypatch.setattr(srv, "_today", lambda: TODAY)
    plugin = client.application.config["PLUGIN_REGISTRY"].get("comics_daily")
    monkeypatch.setattr(plugin.server_module, "_today", lambda: TODAY)
    resp = client.get(f"/_test/render?plugin=comics_daily&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-plugin="comics_daily"' in body
    assert "/plugins/comics_daily/img/gocomics-garfield-" in body
    assert "nervous little dogs" in body

    # Pictures are served read-only from data_dir.
    data_dir = Path(plugin.data_dir)
    state = json.loads((data_dir / "state.json").read_text())
    name = state["sources"]["gocomics:garfield"]["pictures"][0]["file"]
    with client.get(f"/plugins/comics_daily/img/{name}") as img:
        assert img.status_code == 200 and img.data[:4] == b"\x89PNG"
    with client.get("/plugins/comics_daily/img/..%2Fstate.json") as missing:
        assert missing.status_code == 404


def test_fetch_answers_from_disk_while_refresh_runs(
    web: FakeWeb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "_today", lambda: TODAY)
    monkeypatch.setattr(srv, "FIRST_RENDER_WAIT_S", 0.0)
    release = srv.threading.Event()
    real_work = srv._work
    monkeypatch.setattr(srv, "_work", lambda run: (release.wait(5), real_work(run)))
    _all_routes(web)
    options = {"strips": "garfield", "far_side": False}

    first = srv.fetch(options, {}, ctx={"data_dir": str(tmp_path)})
    assert first["strips"] == []
    assert first["updating"] and first["live_refresh_s"] == srv.LIVE_REFRESH_S
    assert first["live_pending"] == 1

    release.set()
    run = srv._RUNS[str(tmp_path)]
    assert run.thread is not None
    run.thread.join(5)
    second = srv.fetch(options, {}, ctx={"data_dir": str(tmp_path)})
    assert len(second["strips"]) == 1 and "live_refresh_s" not in second
    started = time.time()
    srv.fetch(options, {}, ctx={"data_dir": str(tmp_path)})
    assert time.time() - started < 1  # nothing due: no new work, no waiting


def test_no_sources_is_an_error(tmp_path: Path) -> None:
    out = srv.fetch({"strips": "", "far_side": False}, {}, ctx={"data_dir": str(tmp_path)})
    assert "Turn on a strip or the Far Side" in out["error"]


# -- review regressions ------------------------------------------------------------


def _load(name: str, file: str = "server.py") -> Any:
    spec = importlib.util.spec_from_file_location(name, _PLUGIN / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _wait_done(data_dir: Path) -> Any:
    run = srv._RUNS[str(data_dir)]
    assert run.thread is not None
    run.thread.join(5)
    assert run.done and run.active is None
    return run


def test_storage_failure_is_contained(
    web: FakeWeb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk while recording fails the source, never the worker."""
    store = images.Store(tmp_path)
    source = srv.parse_strips("garfield https://featureassets.gocomics.com/assets/m")[0][0]

    def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "update", broken)
    assert not srv.refresh_source(store, source, TODAY, web=srv.Web(), gocomics=None)


def test_crashed_worker_ends_its_run_and_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "refresh_source", explode)
    source = srv.Source("url:x", "url", "x", "https://featureassets.gocomics.com/assets/x")
    first = srv._enqueue(tmp_path, [source], TODAY)
    _wait_done(tmp_path)
    assert srv._running_keys(tmp_path) == set()

    # A run left "not done" by a thread that died anyway is not reused.
    first.done = False
    first.active = source
    monkeypatch.setattr(srv, "refresh_source", lambda *a, **k: True)
    second = srv._enqueue(tmp_path, [source], TODAY)
    assert second is not first
    _wait_done(tmp_path)


def test_without_argon2id_only_gocomics_challenges_fail(
    web: FakeWeb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older cryptography (no Argon2id) still loads the widget."""
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.kdf.argon2", None)
    old = _load("_comics_daily_test_old_crypto.bunny_shield", "bunny_shield.py")
    assert old.is_challenge({}, CHALLENGE_PAGE)
    with pytest.raises(old.SolverUnavailable):
        old.solve(DATA_POW)

    monkeypatch.setattr(srv, "bunny_shield", old)
    gc = "https://www.gocomics.com"
    web.routes[("GET", f"{gc}/garfield/2026/10/18")] = [_html(CHALLENGE_PAGE, status=403)]
    with pytest.raises(srv.SourceError, match="cryptography 44"):
        srv.GoComics(tmp_path / srv.COOKIE_FILE, TODAY).strip("garfield", "Garfield")


def test_reloaded_module_shares_the_running_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    again = _load("_comics_daily_test_reload.server")
    assert again._RUNS is srv._RUNS
    assert again._RUNS_LOCK is srv._RUNS_LOCK
    assert again.images._LOCK is images._LOCK

    gate = threading.Event()
    calls: list[str] = []

    def slow(_store: Any, source: Any, *_a: Any, **_k: Any) -> bool:
        gate.wait(5)
        calls.append(source.key)
        return True

    monkeypatch.setattr(srv, "refresh_source", slow)
    a = srv.Source("url:a", "url", "a", "https://featureassets.gocomics.com/assets/a")
    b = again.Source("url:b", "url", "b", "https://featureassets.gocomics.com/assets/b")
    run = srv._enqueue(tmp_path, [a], TODAY)
    # The reloaded code queues onto the old worker instead of starting another.
    assert again._enqueue(tmp_path, [b], TODAY) is run
    assert run.keys() == {"url:a", "url:b"}
    gate.set()
    _wait_done(tmp_path)
    assert calls == ["url:a", "url:b"]
    assert again._running_keys(tmp_path) == set()


def test_finished_source_is_not_fetched_again_from_a_stale_snapshot(
    web: FakeWeb, tmp_path: Path
) -> None:
    store = images.Store(tmp_path)
    source = srv.parse_strips("garfield https://featureassets.gocomics.com/assets/m")[0][0]
    web.routes[("GET", source.url)] = [_img(_png())]
    assert srv.refresh_source(store, source, TODAY, web=srv.Web(), gocomics=None)
    web.requests.clear()
    before = store.sources()[source.key]

    # A render that read the state before that landed queues it again.
    srv._enqueue(tmp_path, [source], TODAY)
    _wait_done(tmp_path)
    assert web.requests == []
    assert store.sources()[source.key]["attempts"] == before["attempts"]

    # And a live run that already did it won't take it back.
    run = srv._Run(data_dir=tmp_path, today=TODAY, completed={source.key})
    gate = threading.Event()
    run.thread = threading.Thread(target=gate.wait, args=(5,), daemon=True)
    run.thread.start()
    srv._RUNS[str(tmp_path)] = run
    try:
        assert srv._enqueue(tmp_path, [source], TODAY) is run
        assert run.queue == []
        srv._enqueue(tmp_path, [source], TODAY, force=True)
        assert [s.key for s, _ in run.queue] == [source.key]
    finally:
        run.queue.clear()
        gate.set()
        run.thread.join(5)
        run.done = True


def test_partial_far_side_keeps_the_complete_set(web: FakeWeb, tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    web.routes[("GET", srv.FARSIDE_URL)] = [_html(FARSIDE_PAGE)]
    for name in ("one", "two", "three"):
        web.routes[("GET", f"https://featureassets.amuniversal.com/assets/{name}")] = [
            _img(_jpeg((95 + len(name), 110)))
        ]
    yesterday = date(2026, 10, 24)
    assert srv.refresh_source(store, srv.FARSIDE, yesterday, web=srv.Web(), gocomics=None)
    complete = store.sources()["farside"]["pictures"]
    assert len(complete) == 3

    web.routes[("GET", "https://featureassets.amuniversal.com/assets/two")] = [(503, {}, b"")]
    web.routes[("GET", "https://featureassets.amuniversal.com/assets/three")] = [
        _html("<html>not a picture</html>")
    ]
    assert not srv.refresh_source(store, srv.FARSIDE, TODAY, web=srv.Web(), gocomics=None)
    record = store.sources()["farside"]
    assert record["pictures"] == complete
    assert record["fetched_on"] == "2026-10-24"
    assert record["error"] == "Only 1 of 3 pictures came through."
    # Not marked done for today, so it is tried again after the back-off.
    assert srv.is_due(srv.FARSIDE, record, "2026-10-25", time.time() + srv.RETRY_STALE_S)

    # With nothing better on disk, the partial set stands in.
    fresh = images.Store(tmp_path / "fresh")
    assert not srv.refresh_source(fresh, srv.FARSIDE, TODAY, web=srv.Web(), gocomics=None)
    assert len(fresh.sources()["farside"]["pictures"]) == 1


def test_gocomics_freshness_follows_the_edition() -> None:
    go = srv.Source("gocomics:garfield", "gocomics", "garfield")
    today = "2026-10-25"
    pics = True
    ok_today = {"fetched_on": today, "edition": today}
    assert srv.freshness(go, ok_today, pics, False, today, today) == ("ok", None)
    # Yesterday's strip before today's is due (published is still yesterday).
    lagging = {"fetched_on": today, "edition": "2026-10-24", "attempted_on": today, "attempts": 1}
    assert srv.freshness(go, lagging, pics, False, today, "2026-10-24") == ("ok", None)
    # Once today's is due it is late, however few attempts were made.
    assert srv.freshness(go, lagging, pics, False, today, today) == ("stale", "2026-10-24")
    # The request "worked" today but the site keeps serving an old strip.
    old = dict(lagging, edition="2026-10-20")
    assert srv.freshness(go, old, pics, False, today, "2026-10-24") == ("stale", "2026-10-20")
    # Archived Dilbert and image addresses carry old dates by design.
    dilbert = srv.Source("dilbert", "dilbert", "dilbert")
    assert srv.freshness(dilbert, dict(old), pics, False, today, today) == ("ok", None)
    url = srv.Source("url:x", "url", "garfield", "https://featureassets.gocomics.com/assets/x")
    assert srv.freshness(url, dict(old), pics, False, today, today) == ("ok", None)
    assert srv.freshness(go, {"error": "x"}, False, False, today) == ("missing", None)
    assert srv.freshness(go, {}, False, True, today) == ("pending", None)


def test_published_edition_uses_eastern_midnight_plus_grace() -> None:
    from datetime import datetime, timedelta, timezone

    def at(hour: int, offset: int, day: int) -> datetime:
        return datetime(2026, 10, day, hour, 15, tzinfo=timezone(timedelta(hours=offset)))

    # Phoenix 06:15: New York is past 03:00 on the 26th, so the 26th is due.
    assert srv.published_edition(at(6, -7, 26), "2026-10-26") == "2026-10-26"
    # Phoenix 21:15 on the 25th: New York is just past midnight, within the
    # grace, and the local day is still the 25th anyway.
    assert srv.published_edition(at(21, -7, 25), "2026-10-25") == "2026-10-25"
    # Berlin 07:15 on the 26th: New York's 26th is not three hours old yet.
    assert srv.published_edition(at(7, 1, 26), "2026-10-26") == "2026-10-25"
    # Melbourne 09:15 on the 26th is still the 25th in New York.
    assert srv.published_edition(at(9, 11, 26), "2026-10-26") == "2026-10-25"
    # Honolulu late on the 25th: never ahead of the local day.
    assert srv.published_edition(at(22, -10, 25), "2026-10-25") == "2026-10-25"


def test_partial_set_gives_way_to_a_fuller_one(web: FakeWeb, tmp_path: Path) -> None:
    store = images.Store(tmp_path)
    base = "https://featureassets.amuniversal.com/assets/"
    web.routes[("GET", srv.FARSIDE_URL)] = [_html(FARSIDE_PAGE)]

    def serve(*good: str) -> None:
        for name in ("one", "two", "three"):
            reply = _img(_jpeg((95 + len(name), 110))) if name in good else (503, {}, b"")
            web.routes[("GET", base + name)] = [reply]

    def refresh() -> bool:
        return srv.refresh_source(store, srv.FARSIDE, TODAY, web=srv.Web(), gocomics=None)

    serve("one")
    assert not refresh()
    assert len(store.sources()["farside"]["pictures"]) == 1
    serve("one", "two")
    assert not refresh()
    record = store.sources()["farside"]
    assert len(record["pictures"]) == 2
    assert record["error"] == "Only 2 of 3 pictures came through."
    serve("one", "two", "three")
    assert refresh()
    assert len(store.sources()["farside"]["pictures"]) == 3
    assert store.sources()["farside"]["partial"] is False
    serve("one", "two")
    assert not refresh()
    assert len(store.sources()["farside"]["pictures"]) == 3


def test_decoded_size_is_capped() -> None:
    big = b"a" * 10_000
    with pytest.raises(srv.SourceError, match="more than"):
        srv.decode_body(gzip.compress(big), "gzip", 100, "example.org")
    assert srv.decode_body(gzip.compress(b"hello"), "gzip", 100, "example.org") == b"hello"
    raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    deflated = raw.compress(b"hello") + raw.flush()
    assert srv.decode_body(deflated, "deflate", 100, "example.org") == b"hello"
    assert srv.decode_body(b"plain", "", 100, "example.org") == b"plain"
    with pytest.raises(srv.SourceError, match="damaged"):
        srv.decode_body(b"not gzip", "gzip", 100, "example.org")


def test_gzip_members_trailers_and_cumulative_cap() -> None:
    two = gzip.compress(b"hello") + gzip.compress(b"world")
    assert srv.decode_body(two, "gzip", 100, "example.org") == b"helloworld"
    with pytest.raises(srv.SourceError, match="more than"):
        srv.decode_body(gzip.compress(b"a" * 60) * 2, "gzip", 100, "example.org")
    # A stream cut before its CRC/length trailer is damaged, not short.
    with pytest.raises(srv.SourceError, match="damaged"):
        srv.decode_body(gzip.compress(b"hello")[:-4], "gzip", 100, "example.org")
    with pytest.raises(srv.SourceError, match="damaged"):
        srv.decode_body(zlib.compress(b"hello") + b"junk", "deflate", 100, "example.org")


# -- the settings, the admin page and its actions --------------------------------

GC = "https://www.gocomics.com"
LEGACY = {
    "far_side": False,
    "strips": "peanuts\n# garfield\ndilbert https://featureassets.gocomics.com/assets/x",
    "gap": 20,
}


def _app_module(client: FlaskClient) -> Any:
    return client.application.config["PLUGIN_REGISTRY"].get("comics_daily").server_module


def _plugin(client: FlaskClient) -> Any:
    return client.application.config["PLUGIN_REGISTRY"].get("comics_daily")


def _settings(client: FlaskClient) -> dict[str, Any]:
    plugin = _plugin(client)
    store = client.application.config["SETTINGS_STORE"]
    return store.get_for_runtime("plugins", plugin.id, plugin.manifest["settings"])


def _saved_raw(client: FlaskClient) -> Any:
    return (client.application.config["SETTINGS_STORE"].get_section("plugins") or {}).get(
        "comics_daily"
    )


def _store(client: FlaskClient) -> Any:
    return _app_module(client).images.Store(Path(_plugin(client).data_dir))


def _add_comics_page(client: FlaskClient, options: dict[str, Any]) -> None:
    from app.state.page_store import Cell, Page

    page = Page(
        id="comics-page",
        name="Comics",
        cells=[Cell(id="c1", x=0, y=0, w=1, h=1, plugin="comics_daily", options=options)],
    )
    client.application.config["PAGE_STORE"].save(page)


def _messages(client: FlaskClient) -> list[str]:
    with client.session_transaction() as session:
        return [text for _, text in session.get("_flashes", [])]


def test_legacy_options_become_rows() -> None:
    rows = srv.legacy_strip_rows(LEGACY["strips"])
    assert rows == [
        {"enabled": True, "label": "Peanuts", "slug": "peanuts", "mode": "auto", "image_url": ""},
        {
            "enabled": False,
            "label": "Garfield",
            "slug": "garfield",
            "mode": "auto",
            "image_url": "",
        },
        {
            "enabled": True,
            "label": "Dilbert",
            "slug": "dilbert",
            "mode": "url",
            "image_url": "https://featureassets.gocomics.com/assets/x",
        },
    ]
    assert srv.customised(LEGACY)
    assert not srv.customised(dict(srv.LEGACY_DEFAULTS))
    assert not srv.customised({"strips": "garfield calvinandhobbes\npearlsbeforeswine dilbert"})
    assert not srv.customised({})


def test_settings_decide_what_shows_and_the_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {
        "far_side": False,
        "far_side_count": 9,
        "gap": 12,
        "strips": [
            {"enabled": True, "label": "Odie's", "slug": "garfield", "mode": "auto"},
            {"enabled": False, "slug": "peanuts"},
            {"enabled": True, "slug": "dilbert", "mode": "upload"},
            {"enabled": True, "slug": "nonsequitur", "mode": "url", "image_url": ""},
            {"enabled": True, "slug": "Bad Name!"},
        ],
    }
    config = srv.config_from_settings(settings)
    assert config.far_side_count == 4 and config.gap == 12
    wanted, problems = srv.sources_for(config)
    assert [(s.key, s.label) for s in wanted] == [
        ("gocomics:garfield", "Odie's"),
        ("manual:dilbert", "Dilbert"),
    ]
    assert [p.message for p in problems] == ["Give it an image URL first."]
    queued: list[tuple[list[str], str | None]] = []
    # No network: the render's work is captured, not started.
    monkeypatch.setattr(
        srv,
        "_enqueue",
        lambda root, sources, today, force=False, pasted=None: queued.append(
            ([s.key for s in sources], pasted)
        ),
    )
    out = srv.fetch({"strips": "peanuts"}, settings, ctx={"data_dir": str(tmp_path)})
    assert out["layout"]["far_side"] is False and out["layout"]["gap"] == 12
    # The cell's own options are ignored when settings are given.
    assert queued == [(["gocomics:garfield"], "")]  # no pasted cookies without a host
    # The manual strip has no picture yet: noted, never fetched.
    notes = {note["label"]: note for note in out["notes"]}
    assert notes["Dilbert"]["state"] == "missing"
    assert notes["Dilbert"]["message"] == "No picture uploaded yet."


def test_settings_are_seeded_from_the_first_customised_cell(client: FlaskClient) -> None:
    _add_comics_page(client, LEGACY)
    assert _saved_raw(client) is None
    page = client.get("/plugins/comics_daily/")
    assert page.status_code == 200
    settings = _settings(client)
    assert [r["slug"] for r in settings["strips"]] == ["peanuts", "garfield", "dilbert"]
    assert settings["strips"][1]["enabled"] is False
    assert settings["far_side"] is False and settings["gap"] == 20
    # From now on the settings rule: a cell's own options are ignored.
    module = _app_module(client)
    with client.application.test_request_context():
        config = module.resolve_config({"strips": "bc", "far_side": True}, {})
    assert [s.slug for s in config.strips] == ["peanuts", "garfield", "dilbert"]
    assert config.far_side is False


def test_untouched_cells_seed_the_defaults(client: FlaskClient) -> None:
    _add_comics_page(client, dict(srv.LEGACY_DEFAULTS))
    client.get("/plugins/comics_daily/")
    settings = _settings(client)
    assert [r["slug"] for r in settings["strips"]] == [
        "dilbert",
        "garfield",
        "calvinandhobbes",
        "pearlsbeforeswine",
    ]
    assert settings["far_side"] is True
    assert (settings["gap"], settings["gap_min"], settings["gap_max"]) == (32, 6, 48)


def test_a_render_seeds_the_settings_too(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _app_module(client)
    monkeypatch.setattr(module, "_enqueue", lambda *a, **k: None)
    with client.application.test_request_context():
        out = module.fetch(LEGACY, {}, ctx={"data_dir": _plugin(client).data_dir})
    assert out["layout"]["far_side"] is False  # seeded from the cell rendered
    assert [r["slug"] for r in _settings(client)["strips"]] == ["peanuts", "garfield", "dilbert"]


def test_settings_save_through_the_settings_form(client: FlaskClient) -> None:
    client.get("/plugins/comics_daily/")
    rows = [
        {"enabled": True, "label": "", "slug": "garfield", "mode": "auto", "image_url": ""},
        {"enabled": True, "label": "Dilbert", "slug": "dilbert", "mode": "auto", "image_url": ""},
    ]
    resp = client.post(
        "/settings/plugin-comics_daily",
        data={
            "strips": json.dumps(rows),
            "far_side": "on",
            "far_side_count": "2",
            "gap": "40",
            "gap_min": "6",
            "gap_max": "48",
        },
    )
    assert resp.status_code == 302
    assert resp.location == "/plugins/comics_daily/#plugin-comics_daily"
    settings = _settings(client)
    assert [r["slug"] for r in settings["strips"]] == ["garfield", "dilbert"]
    assert settings["far_side_count"] == 2 and settings["captions"] is False
    # A bad name is refused and nothing changes.
    bad = [{"enabled": True, "label": "", "slug": "Not a name", "mode": "auto", "image_url": ""}]
    client.post("/settings/plugin-comics_daily", data={"strips": json.dumps(bad)})
    assert [r["slug"] for r in _settings(client)["strips"]] == ["garfield", "dilbert"]


def test_admin_page_lists_strips_with_status_and_tools(client: FlaskClient) -> None:
    client.get("/plugins/comics_daily/")
    store = _store(client)
    module = _app_module(client)
    picture = module.images.normalise(_png((300, 100)))
    name = store.add("gocomics:garfield", picture)
    store.update(
        "gocomics:garfield",
        lambda e: e.update(
            pictures=[{"file": name, "w": 300, "h": 100, "caption": ""}],
            edition="2026-10-20",
            fetched_on="2026-10-20",
            last_success=1.0,
            error="GoComics is blocking automatic downloads right now.",
        ),
    )
    body = client.get("/plugins/comics_daily/").get_data(as_text=True)
    assert "Comics, Tesserae</title>" in body
    card = body[body.index('id="plugin-comics_daily"') :]
    assert card.count("data-repeater-fixed") == 1  # Dilbert can't be removed
    assert "Blocked" in card and "Couldn&#39;t refresh automatically" in card
    assert 'form="cx-upload-garfield"' in card and 'form="cx-import-garfield"' in card
    assert 'form="cx-add"' in card and "data-repeater-add" not in card
    assert 'id="cx-upload-garfield"' in body and 'enctype="multipart/form-data"' in body
    today = body[body.index('id="today"') :]
    assert f"/plugins/comics_daily/img/{name}" in today
    assert "needs a look: Garfield (blocked)" in today
    nav = body[: body.index("<main")]
    assert re.search(r'href="/plugins/comics_daily/">\s*<i[^>]*></i>Comics\s*</a>', nav)


@pytest.mark.parametrize(
    ("typed", "slug"),
    [
        ("https://www.gocomics.com/peanuts", "peanuts"),
        ("gocomics.com/Peanuts/2026/10/25", "peanuts"),
        ("  nonsequitur ", "nonsequitur"),
        ("https://example.com/peanuts", None),
        ("../etc", None),
        ("", None),
    ],
)
def test_parse_strip_input(typed: str, slug: str | None) -> None:
    assert srv.parse_strip_input(typed) == slug


def _peanuts_routes(fake: FakeWeb, body: str | None = None, status: int = 200) -> None:
    _all_routes(fake)
    page = body if body is not None else _gocomics_page("peanuts", "2026-10-25", "pnts")
    fake.routes[("GET", f"{GC}/peanuts/2026/10/25")] = [_html(page, status)]
    fake.routes[("GET", "https://featureassets.gocomics.com/assets/pnts")] = [_img(_png())]


def test_add_a_gocomics_strip_checks_it_and_fetches_it(
    client: FlaskClient, web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_app_module(client), "_today", lambda: TODAY)
    client.get("/plugins/comics_daily/")
    _peanuts_routes(web)
    resp = client.post("/plugins/comics_daily/strips/add", data={"strip": f"{GC}/peanuts"})
    assert resp.location == "/plugins/comics_daily/#plugin-comics_daily"
    rows = _settings(client)["strips"]
    assert rows[-1] == {
        "enabled": True,
        "label": "Garfield",  # the page's own name for it (the fixture's title)
        "slug": "peanuts",
        "mode": "auto",
        "image_url": "",
    }
    record = _store(client).sources()["gocomics:peanuts"]
    assert record["fetched_on"] == "2026-10-25" and record["pictures"]
    log = [(e["outcome"], e["source"]) for e in _store(client).log()]
    assert ("ok", "Garfield") in log and ("added", "Garfield") in log
    # Again: already there.
    client.post("/plugins/comics_daily/strips/add", data={"strip": "peanuts"})
    assert len(_settings(client)["strips"]) == len(rows)
    assert any("already in the list" in m for m in _messages(client))


def test_add_refuses_a_name_gocomics_does_not_have(
    client: FlaskClient, web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_app_module(client), "_today", lambda: TODAY)
    client.get("/plugins/comics_daily/")
    before = _settings(client)["strips"]
    # A page that isn't a strip page.
    _peanuts_routes(web, body="<html><head></head></html>")
    client.post("/plugins/comics_daily/strips/add", data={"strip": "peanuts"})
    assert _settings(client)["strips"] == before
    assert any("no strip called" in m for m in _messages(client))
    # Every date 404s and so does the strip's own page.
    _all_routes(web)
    for day in ("25", "24", "23"):
        web.routes[("GET", f"{GC}/nosuchstrip/2026/10/{day}")] = [_html("gone", 404)]
    web.routes[("GET", f"{GC}/nosuchstrip")] = [_html("gone", 404)]
    client.post("/plugins/comics_daily/strips/add", data={"strip": "nosuchstrip"})
    assert _settings(client)["strips"] == before
    # Not a name at all.
    client.post("/plugins/comics_daily/strips/add", data={"strip": "<script>"})
    assert any("isn't a GoComics address" in m for m in _messages(client))


def test_add_keeps_a_strip_it_could_not_fetch_yet(
    client: FlaskClient, web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_app_module(client), "_today", lambda: TODAY)
    client.get("/plugins/comics_daily/")
    _all_routes(web)  # no route for peanuts: GoComics unreachable for it
    client.post("/plugins/comics_daily/strips/add", data={"strip": "peanuts"})
    assert _settings(client)["strips"][-1]["slug"] == "peanuts"
    assert any("couldn't be fetched yet" in m for m in _messages(client))
    assert _store(client).sources()["gocomics:peanuts"]["error"]


def test_add_stops_at_the_most_strips(client: FlaskClient) -> None:
    client.get("/plugins/comics_daily/")
    rows = [{"enabled": True, "label": "", "slug": "dilbert", "mode": "auto", "image_url": ""}] + [
        {"enabled": True, "label": "", "slug": f"strip{i}", "mode": "auto", "image_url": ""}
        for i in range(srv.MAX_STRIPS - 1)
    ]
    client.post("/settings/plugin-comics_daily", data={"strips": json.dumps(rows)})
    client.post("/plugins/comics_daily/strips/add", data={"strip": "peanuts"})
    assert len(_settings(client)["strips"]) == srv.MAX_STRIPS
    assert any("at most" in m for m in _messages(client))


def test_upload_makes_a_manual_strip_until_switched_back(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _app_module(client)
    monkeypatch.setattr(module, "_today", lambda: TODAY)
    client.get("/plugins/comics_daily/")
    data = {"file": (io.BytesIO(_png((640, 200), "RGBA")), "garfield.png")}
    resp = client.post(
        "/plugins/comics_daily/strips/garfield/upload",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    row = next(r for r in _settings(client)["strips"] if r["slug"] == "garfield")
    assert row["mode"] == "upload"
    record = _store(client).sources()["manual:garfield"]
    assert record["origin"] == "uploaded" and record["pictures"][0]["w"] == 640
    # Every cell now shows the manual picture, and never fetches it.
    queued: list[str] = []
    monkeypatch.setattr(
        module, "_enqueue", lambda root, sources, *a, **k: queued.extend(s.key for s in sources)
    )
    with client.application.test_request_context():
        out = module.fetch({}, {}, ctx={"data_dir": _plugin(client).data_dir})
    assert "gocomics:garfield" not in queued and "manual:garfield" not in queued
    strip = next(s for s in out["strips"] if s["id"] == "garfield")
    assert strip["src"].startswith("/plugins/comics_daily/img/manual-garfield-")
    assert ("uploaded", "Garfield") in [(e["outcome"], e["source"]) for e in _store(client).log()]
    # A later day it still shows, with a warning on the admin page.
    monkeypatch.setattr(module, "_today", lambda: date(2026, 10, 27))
    body = client.get("/plugins/comics_daily/").get_data(as_text=True)
    assert "This picture is from Oct 25" in body
    # Old manual pictures are never pruned for age.
    _store(client).prune(now=time.time() + 400 * 86400)
    assert "manual:garfield" in _store(client).sources()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "Choose a picture"),
        (b"<html>not a picture</html>", "isn't a picture"),
        # Over the cap (lowered here: werkzeug's test client leaves a request
        # body over 500 KB in an unclosed temp file).
        (b"x" * 2000, "That picture is over"),
    ],
)
def test_upload_refuses_what_is_not_a_usable_picture(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, payload: bytes, message: str
) -> None:
    monkeypatch.setattr(_app_module(client).images, "MAX_UPLOAD_BYTES", 1000)
    client.get("/plugins/comics_daily/")
    data = {"file": (io.BytesIO(payload), "x.png" if payload else "")}
    client.post(
        "/plugins/comics_daily/strips/garfield/upload",
        data=data,
        content_type="multipart/form-data",
    )
    assert any(message in m for m in _messages(client))
    assert "manual:garfield" not in _store(client).sources()
    assert next(r for r in _settings(client)["strips"] if r["slug"] == "garfield")["mode"] == "auto"


def test_upload_refuses_too_many_pixels(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_app_module(client).images, "MAX_PIXELS", 1000)
    client.get("/plugins/comics_daily/")
    data = {"file": (io.BytesIO(_png((100, 100))), "big.png")}
    client.post(
        "/plugins/comics_daily/strips/garfield/upload",
        data=data,
        content_type="multipart/form-data",
    )
    assert any("isn't a picture this widget can use" in m for m in _messages(client))
    assert client.post("/plugins/comics_daily/strips/nosuch/upload").status_code == 404


def test_import_fetches_through_the_net_guard(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import net_guard

    asked: list[tuple[str, dict[str, Any]]] = []

    def fake_fetch(url: str, **kw: Any) -> tuple[bytes, str]:
        asked.append((url, kw))
        return _jpeg((800, 250)), "image/jpeg"

    client.get("/plugins/comics_daily/")
    monkeypatch.setattr(net_guard, "fetch_bytes", fake_fetch)
    url = "https://pictures.example.org/today.jpg"
    client.post("/plugins/comics_daily/strips/calvinandhobbes/import", data={"url": url})
    assert asked[0][0] == url
    assert asked[0][1]["allow_local"] is False
    assert asked[0][1]["max_bytes"] == _app_module(client).images.MAX_UPLOAD_BYTES
    row = next(r for r in _settings(client)["strips"] if r["slug"] == "calvinandhobbes")
    assert (row["mode"], row["image_url"]) == ("upload", url)
    record = _store(client).sources()["manual:calvinandhobbes"]
    assert record["origin"] == "imported from pictures.example.org"
    assert record["pictures"][0]["file"].endswith(".jpg")


def test_import_refuses_private_addresses_and_reports_failures(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.get("/plugins/comics_daily/")
    # The real guard: a loopback address is refused before any request.
    client.post(
        "/plugins/comics_daily/strips/garfield/import", data={"url": "http://127.0.0.1/x.png"}
    )
    assert any("isn't on the public web" in m for m in _messages(client))
    client.post("/plugins/comics_daily/strips/garfield/import", data={"url": "ftp://x/y.png"})
    assert any("has to start with https://" in m for m in _messages(client))
    client.post("/plugins/comics_daily/strips/garfield/import", data={"url": ""})
    assert any("Image URL first" in m for m in _messages(client))
    from app import net_guard

    def down(url: str, **kw: Any) -> tuple[bytes, str]:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(net_guard, "fetch_bytes", down)
    client.post(
        "/plugins/comics_daily/strips/garfield/import", data={"url": "https://example.org/x.png"}
    )
    assert any("connection refused" in m for m in _messages(client))
    assert "manual:garfield" not in _store(client).sources()


def test_fetch_now_queues_every_fetched_source_forced(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _app_module(client)
    queued: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(
        module,
        "_enqueue",
        lambda root, sources, today, force=False, **_: queued.append(
            ([s.key for s in sources], force)
        ),
    )
    client.get("/plugins/comics_daily/")
    rows = _settings(client)["strips"]
    rows[1]["mode"] = "upload"  # Garfield: a manual strip is never fetched
    client.post(
        "/settings/plugin-comics_daily", data={"strips": json.dumps(rows), "far_side": "on"}
    )
    resp = client.post("/plugins/comics_daily/fetch")
    assert resp.location == "/plugins/comics_daily/#today"
    assert queued == [
        (["farside", "dilbert", "gocomics:calvinandhobbes", "gocomics:pearlsbeforeswine"], True)
    ]
    assert _store(client).log()[-1]["source"] == "Fetch now"


def test_the_log_records_each_fetch(
    web: FakeWeb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "_today", lambda: TODAY)
    store = images.Store(tmp_path)
    _all_routes(web)
    gocomics = srv.GoComics(tmp_path / srv.COOKIE_FILE, TODAY)
    garfield = srv.Source(key="gocomics:garfield", kind="gocomics", name="garfield")
    assert srv.refresh_source(store, garfield, TODAY, web=srv.Web(), gocomics=gocomics)
    assert srv.refresh_source(store, srv.FARSIDE, TODAY, web=srv.Web(), gocomics=None)
    url = srv.Source(
        key="url:x", kind="url", name="x", url="https://featureassets.gocomics.com/nope"
    )
    assert not srv.refresh_source(store, url, TODAY, web=srv.Web(), gocomics=None)
    log = store.log()
    assert [(e["source"], e["outcome"]) for e in log] == [
        ("Garfield", "ok"),
        ("The Far Side", "ok"),
        ("X", "error"),
    ]
    assert re.fullmatch(r"2026-10-25, 1 picture, \d+ KB in \d+\.\d s", log[0]["text"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\d, 3 pictures, \d+ KB in \d+\.\d s", log[1]["text"])
    assert log[2]["text"].startswith("Couldn't reach featureassets.gocomics.com.")
    for i in range(images.LOG_LINES + 3):
        store.append_log({"at": float(i), "source": "x", "outcome": "ok", "text": str(i)})
    assert len(store.log()) == images.LOG_LINES
    assert store.log()[-1]["text"] == str(images.LOG_LINES + 2)


def test_the_log_tells_blocked_from_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = images.Store(tmp_path)
    source = srv.Source(key="gocomics:garfield", kind="gocomics", name="garfield")
    blocked = srv.SourceError("GoComics is blocking automatic downloads right now.")
    srv.record_fetch(store, source, blocked, TODAY, time.time())
    old = srv.Fetched([(_png(), "")], edition="2026-10-20")
    monkeypatch.setattr(srv, "published_edition", lambda now, today: "2026-10-25")
    srv.record_fetch(store, source, old, TODAY, time.time())
    assert [e["outcome"] for e in store.log()] == ["blocked", "stale"]
    assert store.log()[1]["text"].startswith("still the 2026-10-20 strip, 1 picture")


def test_the_admin_page_shows_the_log(client: FlaskClient) -> None:
    client.get("/plugins/comics_daily/")
    _store(client).append_log(
        {"at": time.time(), "source": "Garfield", "outcome": "blocked", "text": "nope <b>"}
    )
    body = client.get("/plugins/comics_daily/").get_data(as_text=True)
    log = body[body.index('id="log"') :]
    assert "data-log-tail" in log
    assert re.search(r"Garfield\s+BLOCKED\s+nope &lt;b&gt;", log)
    # The settings sit right above the log, folded.
    settings = body.index('id="plugin-comics_daily"')
    assert body.index('id="today"') < settings < body.index('id="log"')
    assert '<details class="dx-section-fold" data-fold>' in body[settings:]


# -- GoComics cookies pasted on the Comics page --------------------------------

cookies_mod = srv.cookies
SHIELD = "bunny_shield_id_9=abc#sig#1893456000"


@pytest.mark.parametrize(
    "paste",
    [
        # Netscape cookies.txt (Get cookies.txt Locally, Copy All)
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.gocomics.com\tTRUE\t/\tTRUE\t1893456000\tbunny_shield_id_9\tabc#sig#1893456000\n"
        ".gocomics.com\tTRUE\t/\tFALSE\t0\tsession\txyz\n"
        ".example.com\tTRUE\t/\tFALSE\t0\tother\tnope\n",
        # A JSON export
        json.dumps(
            [
                {
                    "name": "bunny_shield_id_9",
                    "value": "abc#sig#1893456000",
                    "domain": ".gocomics.com",
                },
                {"name": "session", "value": "xyz", "domain": "www.gocomics.com"},
                {"name": "other", "value": "nope", "domain": "example.com"},
            ]
        ),
        # A Cookie header
        "Cookie: bunny_shield_id_9=abc#sig#1893456000; session=xyz",
    ],
    ids=["netscape", "json", "header"],
)
def test_cookie_paste_formats(paste: str) -> None:
    pasted = cookies_mod.parse_paste(paste)
    assert pasted.header() == "bunny_shield_id_9=abc#sig#1893456000; session=xyz"
    assert pasted.expires_at == 1893456000


@pytest.mark.parametrize(
    ("paste", "message"),
    [
        ("", "Paste the cookies first"),
        ("session=xyz", "No bunny_shield cookie"),
        ("bunny_shield_x=a\r\nInjected: yes", "isn't a name=value cookie"),
        ('bunny_shield_x="a b"', "characters a cookie can't hold"),
        ("[1, 2", "doesn't read as JSON"),
        ('{"cookies": 3}', "isn't a list of cookies"),
        ("x" * 30000, "far more than"),
    ],
)
def test_cookie_paste_refusals(paste: str, message: str) -> None:
    with pytest.raises(cookies_mod.PasteError, match=message):
        cookies_mod.parse_paste(paste)


def test_pasted_cookies_skip_the_check_and_stay_out_of_the_session_file(
    web: FakeWeb, tmp_path: Path
) -> None:
    _gocomics_routes(web)
    gc = srv.GoComics(tmp_path / srv.COOKIE_FILE, TODAY, pasted=SHIELD + "; session=xyz")
    fetched = gc.strip("garfield", "Garfield")
    assert fetched.edition == "2026-10-25"
    sent = [r for r in web.requests if "gocomics.com/garfield/" in r.full_url]
    assert len(sent) == 1  # no warm-up page: the pasted session was used
    assert "bunny_shield_id_9=abc#sig#1893456000" in sent[0].get_header("Cookie")
    assert gc.pasted_result == "worked"
    saved = (tmp_path / srv.COOKIE_FILE).read_text()
    assert "abc#sig" not in saved and "xyz" not in saved


def test_turned_away_cookies_fall_back_to_the_solver(
    web: FakeWeb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_routes(web)
    page = web.routes[("GET", f"{GC}/garfield/2026/10/25")][0]
    web.routes[("GET", f"{GC}/garfield/2026/10/25")] = [_html(CHALLENGE_PAGE, 403), page]
    gc = srv.GoComics(tmp_path / srv.COOKIE_FILE, TODAY, pasted=SHIELD)
    assert gc.strip("garfield", "Garfield").edition == "2026-10-25"
    assert gc.pasted_result == "turned away"
    assert f"{GC}/garfield/2026/10/18" in web.urls()  # set up its own session


def test_cookie_card_saves_encrypted_never_echoes_and_clears(client: FlaskClient) -> None:
    client.get("/plugins/comics_daily/")
    secret = "bunny_shield_id_9=topsecret#sig#1893456000; session=hush"
    client.post("/plugins/comics_daily/cookies", data={"cookies": secret})
    assert any("Saved 2 GoComics cookies" in m for m in _messages(client))
    module = _app_module(client)
    with client.application.test_request_context():
        assert module.saved_cookie_header() == secret
    disk = Path(client.application.config["SETTINGS_STORE"]._path).read_text()
    assert "gocomics_cookies_secret" in disk and "topsecret" not in disk
    body = client.get("/plugins/comics_daily/").get_data(as_text=True)
    assert "topsecret" not in body and "hush" not in body
    card = body[body.index('id="cookies"') :]
    assert "Set</span>" in card and "2 cookies" in card and "Clear" in card
    assert "topsecret" not in json.dumps(_store(client).log())
    # A failed paste changes nothing.
    client.post("/plugins/comics_daily/cookies", data={"cookies": "nope=1"})
    with client.application.test_request_context():
        assert module.saved_cookie_header() == secret
    # How the last session did shows on the card.
    _store(client).update_cookie_status(
        lambda st: st.update(result="turned away", used_at=time.time())
    )
    card = client.get("/plugins/comics_daily/").get_data(as_text=True)
    assert "Turned away" in card and "Paste fresh ones" in card
    client.post("/plugins/comics_daily/cookies/clear")
    with client.application.test_request_context():
        assert module.saved_cookie_header() == ""
    body = client.get("/plugins/comics_daily/").get_data(as_text=True)
    assert "Not set" in body[body.index('id="cookies"') :]


def test_renders_and_fetch_now_hand_the_cookies_to_the_worker(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _app_module(client)
    handed: list[Any] = []
    monkeypatch.setattr(
        module,
        "_enqueue",
        lambda root, sources, today, force=False, pasted=None: handed.append(pasted),
    )
    client.get("/plugins/comics_daily/")
    client.post("/plugins/comics_daily/cookies", data={"cookies": SHIELD})
    client.post("/plugins/comics_daily/fetch")
    with client.application.test_request_context():
        module.fetch({}, {}, ctx={"data_dir": _plugin(client).data_dir})
    assert handed == [SHIELD, SHIELD]


def test_an_oversized_upload_is_refused_before_the_form_is_read(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _app_module(client)
    monkeypatch.setattr(module.images, "MAX_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(module, "UPLOAD_OVERHEAD_BYTES", 1000)
    client.get("/plugins/comics_daily/")
    parsed: list[bool] = []
    request_class = client.application.request_class
    real = request_class.files

    monkeypatch.setattr(
        request_class,
        "files",
        property(lambda self: parsed.append(True) or real.__get__(self, request_class)),
    )
    data = {"file": (io.BytesIO(b"x" * 20_000), "big.png")}
    client.post(
        "/plugins/comics_daily/strips/garfield/upload",
        data=data,
        content_type="multipart/form-data",
    )
    assert parsed == []  # never parsed
    assert any("That picture is over" in m for m in _messages(client))
    assert "manual:garfield" not in _store(client).sources()


@pytest.mark.parametrize("fmt", ["PNG", "GIF", "WEBP", "JPEG"])
def test_picture_sizes_are_read_from_the_header(fmt: str) -> None:
    buf = io.BytesIO()
    Image.new("RGB", (321, 123), "white").save(buf, format=fmt)
    assert images.header_size(buf.getvalue()) == (321, 123)


def test_a_huge_picture_is_refused_before_pillow_opens_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WebP claiming a 16384 x 16384 canvas: refused on its header, so
    Pillow never sets up a decoder (or a canvas) for it."""
    canvas = (16383).to_bytes(3, "little") * 2
    body = b"VP8X" + (10).to_bytes(4, "little") + b"\x10\x00\x00\x00" + canvas
    webp = b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body
    assert images.header_size(webp) == (16384, 16384)
    opened: list[bytes] = []
    monkeypatch.setattr(images.Image, "open", lambda *a, **k: opened.append(b"") or None)
    with pytest.raises(ValueError, match="too many pixels"):
        images.normalise(webp)
    with pytest.raises(ValueError):
        images.normalise(b"BM" + b"\x00" * 64)  # a BMP: not one of the four
    assert opened == []


def _save_strips(client: FlaskClient, slugs: list[str]) -> None:
    rows = [
        {"enabled": True, "label": "", "slug": slug, "mode": "auto", "image_url": ""}
        for slug in slugs
    ]
    resp = client.post("/settings/plugin-comics_daily", data={"strips": json.dumps(rows)})
    assert resp.status_code == 302


def test_seeding_never_overwrites_a_save_that_got_in_first(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _app_module(client)

    def save_meanwhile() -> None:
        # Another request saves the settings while this one is seeding.
        _save_strips(client, ["dilbert", "peanuts"])
        return None

    monkeypatch.setattr(module, "_first_cell_options", save_meanwhile)
    with client.application.test_request_context():
        settings = module.ensure_settings()
    assert [r["slug"] for r in settings["strips"]] == ["dilbert", "peanuts"]
    assert [r["slug"] for r in _settings(client)["strips"]] == ["dilbert", "peanuts"]


def test_an_import_does_not_bring_back_a_strip_removed_meanwhile(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import net_guard

    client.get("/plugins/comics_daily/")

    def slow_download(url: str, **kw: Any) -> tuple[bytes, str]:
        # Garfield is removed in another tab while the picture downloads.
        _save_strips(client, ["dilbert", "calvinandhobbes"])
        return _png((300, 100)), "image/png"

    monkeypatch.setattr(net_guard, "fetch_bytes", slow_download)
    client.post(
        "/plugins/comics_daily/strips/garfield/import", data={"url": "https://example.org/g.png"}
    )
    assert [r["slug"] for r in _settings(client)["strips"]] == ["dilbert", "calvinandhobbes"]


def test_patch_for_namespace_is_one_step(app: Any) -> None:
    store = app.config["SETTINGS_STORE"]
    fields = [{"name": "n", "type": "number", "default": 0}]
    for _ in range(5):
        store.patch_for_namespace("plugins", "toy", fields, lambda cur, stored: {"n": cur["n"] + 1})
    assert store.get_for_runtime("plugins", "toy", fields)["n"] == 5
    seen: list[set[str]] = []
    store.patch_for_namespace("plugins", "toy", fields, lambda cur, stored: seen.append(stored))
    assert seen == [{"n"}]


@pytest.mark.parametrize(
    "rows",
    [
        [{"slug": "garfield"}],  # Dilbert left out
        [{"slug": "dilbert2"}, {"slug": "garfield"}],  # Dilbert renamed
    ],
)
def test_dilbert_is_kept_by_every_save_path(
    client: FlaskClient, rows: list[dict[str, Any]]
) -> None:
    client.get("/plugins/comics_daily/")
    before = _settings(client)["strips"]
    full = [{"enabled": True, "label": "", "mode": "auto", "image_url": "", **r} for r in rows]
    client.post("/settings/plugin-comics_daily", data={"strips": json.dumps(full)})
    assert _settings(client)["strips"] == before
    assert any("built in" in m for m in _messages(client))
    # The plugin's own writes go through the same check.
    module = _app_module(client)
    from app.repeater import RepeaterValueError

    with client.application.test_request_context(), pytest.raises(RepeaterValueError):
        module._patch_rows(_plugin(client), lambda current: full)
    assert _settings(client)["strips"] == before


def test_legacy_rows_always_carry_dilbert() -> None:
    rows = srv.legacy_strip_rows("garfield\npeanuts")
    assert rows[-1] == {
        "enabled": False,
        "label": "Dilbert",
        "slug": "dilbert",
        "mode": "auto",
        "image_url": "",
    }


@pytest.mark.parametrize(
    "url", ["https://[", "https://exa mple.org/x.png", "ftp://x/y.png", "nope"]
)
def test_a_malformed_image_url_is_refused_on_save_and_harmless_if_stored(
    client: FlaskClient, url: str
) -> None:
    client.get("/plugins/comics_daily/")
    rows = json.loads(json.dumps(_settings(client)["strips"]))  # a copy, not the store's
    rows[1].update(mode="url", image_url=url)
    client.post("/settings/plugin-comics_daily", data={"strips": json.dumps(rows)})
    assert next(r for r in _settings(client)["strips"] if r["slug"] == "garfield")["mode"] == "auto"
    assert any("must be a web address" in m for m in _messages(client))
    # One stored before this check (a hand-edited file) is a row problem,
    # not a crash, on the page and in a render.
    store = client.application.config["SETTINGS_STORE"]
    store.patch_section("plugins", {"comics_daily": {**_saved_raw(client), "strips": rows}})
    assert client.get("/plugins/comics_daily/").status_code == 200
    module = _app_module(client)
    with client.application.test_request_context():
        config = module.resolve_config({}, {})
    wanted, problems = module.sources_for(config)
    assert "gocomics:garfield" not in [s.key for s in wanted]
    assert [p.message for p in problems] == ["That image address isn't a web address."]
