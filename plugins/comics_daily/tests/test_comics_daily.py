"""comics_daily: sources, Bunny Shield, storage, refresh and render, network mocked.

Every request goes through a fake HTTPS handler installed under urllib's
real opener, so cookie handling, redirects and HTTPError bodies behave as
they do live. Fixtures are trimmed copies of the live pages (2026-10-26).
"""

from __future__ import annotations

import email.message
import importlib.util
import io
import json
import sys
import time
import urllib.error
import urllib.request
import urllib.response
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
    kdf = bunny.Argon2id(
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
    assert "Add some strips" in out["error"]
