"""comics_daily, today's comics on one page: Far Side panels over full-width strips.

Ported from the Visionect comics page (a PHP cron, a cookie-refresh service
and a layout script). The sources:

* **GoComics** strips (Garfield, Calvin and Hobbes, Pearls Before Swine, any
  ``gocomics.com/<name>``), from the ``og:image`` of the day's page. The site
  sits behind Bunny Shield, so the client here keeps a cookie session in
  ``data_dir`` and solves the proof of work when the site asks (see
  ``bunny_shield.py``).
* **Dilbert**, a random archived strip a day from dilbert-viewer, a Heroku
  app that may need waking up first.
* **The Far Side** Daily Dose panels and their captions from thefarside.com.
* Any strip can instead come from a direct image address, the manual way
  round a source that is blocking downloads.

Downloads are slow (a cold dyno, a proof of work, a dozen pictures), far past
the few seconds a render gives ``fetch()``. So ``fetch()`` never downloads:
it answers from the last good pictures on disk and, when a source is due,
queues it for one background worker, reporting the work in the job tray and
as ``live_refresh_s`` so editors repaint and a push is followed up once the
new pictures land. ``render.warm_ahead_s`` starts that work ahead of a
scheduled send, and ``updates.on_schedule`` lets a placement refresh daily.

A source is due once per local day. A GoComics strip whose newest edition is
still yesterday's (the server's day can start before the strip is published)
is retried a few times later that day. A failure keeps the last good picture,
backs off, and is reported in the data so the page can say what is stale.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import importlib.util
import json
import logging
import os
import random
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.message import Message
from http.cookiejar import CookieJar, LoadError, MozillaCookieJar
from pathlib import Path
from types import ModuleType
from typing import Any

from flask import Blueprint, abort, current_app, send_file
from werkzeug.wrappers import Response

try:
    from app import jobs
except ImportError:  # an older Tesserae without the job tray
    jobs = None  # type: ignore[assignment]

try:
    from app.capabilities import CapabilityDenied
except ImportError:  # pragma: no cover - every supported host has it

    class CapabilityDenied(RuntimeError):  # type: ignore[no-redef]
        pass


_HERE = Path(__file__).resolve().parent
PLUGIN_ID = _HERE.name


def _load_sibling(name: str) -> ModuleType:
    """Load a helper module beside this file (docs/widgets.md, "Helper modules
    beside server.py"). Registered in ``sys.modules`` under a name unique to
    this widget so dataclasses inside it resolve their own module."""
    module_name = f"_tesserae_plugins.{PLUGIN_ID}.{name}"
    spec = importlib.util.spec_from_file_location(module_name, _HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


bunny_shield = _load_sibling("bunny_shield")
pages = _load_sibling("pages")
images = _load_sibling("images")

logger = logging.getLogger(__name__)

# -- sources -------------------------------------------------------------------

GOCOMICS = "https://www.gocomics.com"
FARSIDE_URL = "https://www.thefarside.com/"
DILBERT = "https://dilbert-viewer.herokuapp.com"
# The archive the viewer serves: the first and the last Dilbert strip.
DILBERT_FIRST = date(1989, 4, 16)
DILBERT_LAST = date(2023, 3, 12)
DILBERT_TRIES = 3
# GoComics: today's page, then the two days before when a day is missing.
GOCOMICS_DAYS_BACK = 3
# The warm-up page, far enough back that no strip fetch asks for it: Bunny
# Shield challenges a session that revisits an address.
WARM_DAYS_BACK = 7
FARSIDE_MAX_PANELS = 4
MAX_STRIPS = 8

DEFAULT_LABELS = {
    "garfield": "Garfield",
    "calvinandhobbes": "Calvin and Hobbes",
    "pearlsbeforeswine": "Pearls Before Swine",
    "dilbert": "Dilbert",
    "farside": "The Far Side",
    "peanuts": "Peanuts",
    "nonsequitur": "Non Sequitur",
    "bc": "B.C.",
    "doonesbury": "Doonesbury",
}

# Every request looks like the desktop browser the sites were scraped with.
# BunnyCDN answers its challenge page to anything without these two Accept
# headers, cookie or no cookie, so they go on every request, images too.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

PAGE_TIMEOUT_S = 20
IMAGE_TIMEOUT_S = 30
# A sleeping Heroku dyno takes a while to answer its first request.
WAKE_TIMEOUT_S = 45
PAGE_MAX_BYTES = 4 * 1024 * 1024
IMAGE_MAX_BYTES = 20 * 1024 * 1024

# -- refresh timing --------------------------------------------------------------

# After a failed attempt: retry soon while there is nothing to show, slowly
# while the last good picture is still up.
RETRY_MISSING_S = 15 * 60
RETRY_STALE_S = 60 * 60
# A GoComics strip still on yesterday's edition: look again this often, this
# many times a day. The newest strip shows meanwhile, so this is not urgent.
EDITION_RETRY_S = 3 * 3600
EDITION_ATTEMPTS = 5
# How long the very first render waits for pictures before painting the
# "fetching" state; the composer gives a widget six seconds in all.
FIRST_RENDER_WAIT_S = 4.0
# Editors poll, and a push is followed up, this often while work is queued.
LIVE_REFRESH_S = 8

COOKIE_FILE = "gocomics-cookies.txt"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_GOCOMICS_PAGE_RE = re.compile(r"^https?://(?:www\.)?gocomics\.com/([a-z0-9-]+)", re.IGNORECASE)


class SourceError(Exception):
    """A source could not be fetched. The message is shown to the user."""


def _allowed_hosts() -> frozenset[str] | None:
    """Hosts the manifest's ``requires:`` lets this widget reach; None for any.

    Read from plugin.json so the manual image address check and the host's
    socket-level enforcement can never disagree.
    """
    try:
        manifest = json.loads((_HERE / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    hosts = set()
    for cap in manifest.get("requires") or []:
        if isinstance(cap, str) and cap.startswith("network:"):
            hosts.add(cap.split(":", 1)[1].lower())
    return None if "*" in hosts else frozenset(hosts)


ALLOWED_HOSTS = _allowed_hosts()


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


# -- HTTP -----------------------------------------------------------------------


@dataclass
class Reply:
    status: int
    headers: Message
    body: bytes
    url: str

    def text(self) -> str:
        charset = self.headers.get_content_charset() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class Web:
    """A small browser: one opener, optionally with a cookie jar.

    Error statuses come back as replies rather than exceptions, because the
    Bunny Shield challenge arrives as a 403 whose body matters. Only a
    transport failure raises, as a :class:`SourceError` naming the host.
    """

    def __init__(self, jar: CookieJar | None = None) -> None:
        handlers = [urllib.request.HTTPCookieProcessor(jar)] if jar is not None else []
        self._opener = urllib.request.build_opener(*handlers)

    def get(
        self,
        url: str,
        *,
        timeout: float = PAGE_TIMEOUT_S,
        limit: int = PAGE_MAX_BYTES,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        request = urllib.request.Request(url, headers={**BROWSER_HEADERS, **(headers or {})})
        return self._send(request, timeout, limit)

    def post(
        self, url: str, data: bytes, *, headers: dict[str, str], timeout: float = PAGE_TIMEOUT_S
    ) -> Reply:
        request = urllib.request.Request(
            url, data=data, method="POST", headers={**BROWSER_HEADERS, **headers}
        )
        return self._send(request, timeout, PAGE_MAX_BYTES)

    def _send(self, request: urllib.request.Request, timeout: float, limit: int) -> Reply:
        host = _host(request.full_url)
        try:
            response: Any = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as err:
            response = err
        except CapabilityDenied as err:
            raise SourceError(f"{host} is not one of the sites this widget may reach.") from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise SourceError(f"Couldn't reach {host}.") from err
        try:
            with contextlib.closing(response):
                has_body = getattr(response, "fp", None) is not None
                body = response.read(limit + 1) if has_body else b""
        except (TimeoutError, OSError) as err:
            raise SourceError(f"{host} stopped answering.") from err
        if len(body) > limit:
            raise SourceError(f"{host} sent more than this widget will download.")
        headers = getattr(response, "headers", None) or Message()
        body = decode_body(body, str(headers.get("Content-Encoding") or ""), limit, host)
        status = int(getattr(response, "status", None) or getattr(response, "code", 0) or 0)
        return Reply(status, headers, body, getattr(response, "url", "") or request.full_url)


def decode_body(body: bytes, encoding: str, limit: int, host: str) -> bytes:
    """Undo a gzip or deflate ``Content-Encoding``, never past ``limit`` bytes.

    urllib sends no Accept-Encoding, but a CDN may compress anyway. The cap
    applies to the decoded size, so a small compressed answer can't expand
    into a huge one; other codings pass through unchanged.
    """
    coding = encoding.lower().strip()
    if coding in ("gzip", "x-gzip"):
        attempts = [zlib.MAX_WBITS | 16]
    elif coding == "deflate":
        # Zlib-wrapped in theory, raw in some servers' practice.
        attempts = [zlib.MAX_WBITS, -zlib.MAX_WBITS]
    else:
        return body
    for wbits in attempts:
        decoder = zlib.decompressobj(wbits)
        try:
            out = decoder.decompress(body, limit + 1)
        except zlib.error:
            continue
        if len(out) > limit or decoder.unconsumed_tail:
            raise SourceError(f"{host} sent more than this widget will download.")
        return out
    raise SourceError(f"{host} sent a damaged compressed answer.")


def _download(web: Web, url: str, what: str) -> bytes:
    """A picture's bytes, or SourceError with ``what`` named."""
    reply = web.get(url, timeout=IMAGE_TIMEOUT_S, limit=IMAGE_MAX_BYTES)
    if reply.status >= 400:
        raise SourceError(f"The picture for {what} answered HTTP {reply.status}.")
    return reply.body


# -- what a cell asks for --------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One thing to fetch. ``key`` names its record in state.json and is
    shared by every cell that shows the same strip."""

    key: str
    kind: str  # "farside" | "gocomics" | "dilbert" | "url"
    name: str  # the strip's name as written in the cell ("garfield")
    url: str = ""

    @property
    def label(self) -> str:
        return DEFAULT_LABELS.get(self.name) or self.name.replace("-", " ").title()


FARSIDE = Source(key="farside", kind="farside", name="farside")


@dataclass(frozen=True)
class Problem:
    """A line of the Strips option that can't be fetched, and why."""

    name: str
    message: str


def _source_for(name: str, url: str) -> Source | Problem:
    if url:
        host = _host(url)
        if not url.lower().startswith("https://") and not url.lower().startswith("http://"):
            return Problem(name, "That image address isn't a web address.")
        if ALLOWED_HOSTS is not None and host not in ALLOWED_HOSTS:
            return Problem(
                name,
                f"{host or 'That address'} is not one of the comic sites this widget may "
                "download from.",
            )
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        return Source(key=f"url:{digest}", kind="url", name=name, url=url)
    if name == "dilbert":
        return Source(key="dilbert", kind="dilbert", name=name)
    return Source(key=f"gocomics:{name}", kind="gocomics", name=name)


def parse_strips(text: str) -> tuple[list[Source], list[Problem]]:
    """Read the Strips option: names top to bottom, one per line (commas work
    too), each optionally followed by a direct image address that replaces the
    automatic download. A GoComics page address stands for its strip's name,
    and a line starting with ``#`` is skipped, which turns a strip off without
    losing its place."""
    entries: list[tuple[str, str]] = []
    problems: list[Problem] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        named_on_line = False
        for token in re.split(r"[\s,]+", line):
            if not token:
                continue
            page = _GOCOMICS_PAGE_RE.match(token)
            if page:
                entries.append((page.group(1).lower(), ""))
                named_on_line = True
            elif _URL_RE.match(token):
                if named_on_line and not entries[-1][1]:
                    entries[-1] = (entries[-1][0], token)
                else:
                    problems.append(Problem(token[:40], "Put a strip name before the address."))
            else:
                name = token.lower()
                if _NAME_RE.match(name):
                    entries.append((name, ""))
                    named_on_line = True
                else:
                    problems.append(Problem(token[:40], "That isn't a strip name."))
    sources: list[Source] = []
    seen: set[str] = set()
    for name, url in entries:
        if name in seen:
            continue
        seen.add(name)
        found = _source_for(name, url)
        if isinstance(found, Problem):
            problems.append(found)
        elif len(sources) < MAX_STRIPS:
            sources.append(found)
    return sources, problems


# -- fetching each source --------------------------------------------------------


@dataclass
class Fetched:
    """What a source produced: raw pictures with captions, and its date.
    ``expected`` is how many pictures a complete set has, when some of them
    may have failed to download (the Far Side's panels)."""

    pictures: list[tuple[bytes, str]]
    edition: str | None = None
    label: str | None = None
    expected: int | None = None


class GoComics:
    """A gocomics.com session that gets past Bunny Shield.

    The cookie jar persists in ``data_dir`` so most days reuse yesterday's
    session; a fresh one is set up only when no shield cookie is saved or a
    page answers with the challenge. Setting one up visits a page the strip
    fetches never ask for (the first strip a week back), solves the proof of
    work there, and confirms the page then loads. At most one proof of work
    per refresh: if the site still challenges after that, it is blocking us.
    """

    def __init__(self, cookie_file: Path, today: date) -> None:
        self._cookie_file = cookie_file
        self._jar = MozillaCookieJar()
        self._today = today
        # A session was set up in this refresh / setting one up was tried.
        self._ready = False
        self._warmed = False
        self._warm_name = ""
        with contextlib.suppress(OSError, LoadError):
            if cookie_file.is_file():
                self._jar.load(str(cookie_file), ignore_discard=True)
        self._web = Web(self._jar)

    def strip(self, name: str, label: str) -> Fetched:
        self._warm_name = self._warm_name or name
        if not self._ready and not bunny_shield.has_session(self._jar):
            if self._warmed:
                raise SourceError("GoComics is blocking automatic downloads right now.")
            self._warm()
        for back in range(GOCOMICS_DAYS_BACK):
            day = self._today - timedelta(days=back)
            reply = self._page(f"{GOCOMICS}/{name}/{day:%Y/%m/%d}")
            if reply.status == 404:
                continue
            if reply.status >= 400:
                raise SourceError(f"GoComics answered HTTP {reply.status} for {label}.")
            page = pages.parse_gocomics(reply.text(), name)
            if page is None:
                raise SourceError(f"GoComics has no strip called “{name}”.")
            raw = _download(self._web, page.image_url, label)
            self._save()
            return Fetched([(raw, "")], edition=page.date or day.isoformat(), label=page.label)
        raise SourceError(f"GoComics has no recent {label} strip.")

    def _page(self, url: str) -> Reply:
        reply = self._web.get(url)
        if not self._challenged(reply):
            return reply
        if self._warmed:
            raise SourceError("GoComics is blocking automatic downloads right now.")
        # The saved session has gone stale. Start over on a page this refresh
        # won't ask for, then ask for this one in the new session.
        logger.info("comics_daily: GoComics session expired, setting up a new one")
        self._jar.clear()
        self._warm()
        reply = self._web.get(url)
        if self._challenged(reply):
            raise SourceError("GoComics is blocking automatic downloads right now.")
        return reply

    @staticmethod
    def _challenged(reply: Reply) -> bool:
        return bunny_shield.is_challenge(reply.headers, reply.text())

    def _warm(self) -> None:
        self._warmed = True
        day = self._today - timedelta(days=WARM_DAYS_BACK)
        url = f"{GOCOMICS}/{self._warm_name}/{day:%Y/%m/%d}"
        reply = self._web.get(url)
        if not self._challenged(reply):
            if reply.status >= 400:
                raise SourceError(f"GoComics answered HTTP {reply.status}.")
            self._ready = True
            self._save()
            return
        data_pow = bunny_shield.find_data_pow(reply.text())
        if not data_pow:
            raise SourceError("GoComics changed its browser check; it can't be passed.")
        try:
            answer = bunny_shield.solve(data_pow)
        except ValueError as err:
            raise SourceError("GoComics changed its browser check; it can't be passed.") from err
        except bunny_shield.SolverUnavailable as err:
            raise SourceError(
                "GoComics wants a browser check this server can't solve: it needs "
                "cryptography 44 or newer (update Tesserae), or give the strip an "
                "image address."
            ) from err
        if answer is None:
            raise SourceError("GoComics' browser check took too long to solve.")
        verify = self._web.post(
            GOCOMICS + bunny_shield.VERIFY_PATH,
            b"{}",
            headers={
                "Content-Type": "application/json",
                bunny_shield.RESPONSE_HEADER: bunny_shield.challenge_response(data_pow, answer),
                "Origin": GOCOMICS,
                "Referer": url,
            },
        )
        if verify.status >= 400:
            raise SourceError("GoComics turned down its browser check answer.")
        if self._challenged(self._web.get(url)):
            raise SourceError("GoComics is blocking automatic downloads right now.")
        logger.info("comics_daily: passed the GoComics browser check")
        self._ready = True
        self._save()

    def _save(self) -> None:
        """Persist the session, owner-only, replacing the file atomically.
        Session cookies are kept too: the shield cookie is one."""
        self._cookie_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".cookies.", suffix=".tmp", dir=self._cookie_file.parent)
        os.close(fd)
        try:
            self._jar.save(tmp, ignore_discard=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._cookie_file)
        except OSError:
            logger.warning("comics_daily: could not save the GoComics session")
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def fetch_farside(web: Web) -> Fetched:
    reply = web.get(FARSIDE_URL)
    if bunny_shield.is_challenge(reply.headers, reply.text()):
        raise SourceError("The Far Side is blocking automatic downloads right now.")
    if reply.status >= 400:
        raise SourceError(f"thefarside.com answered HTTP {reply.status}.")
    panels = pages.parse_farside(reply.text(), FARSIDE_MAX_PANELS)
    if not panels:
        raise SourceError("No Far Side panels on thefarside.com today.")
    pictures: list[tuple[bytes, str]] = []
    failure: SourceError | None = None
    for panel in panels:
        try:
            pictures.append((_download(web, panel.image_url, "The Far Side"), panel.caption))
        except SourceError as err:
            failure = err
    if not pictures:
        raise failure or SourceError("No Far Side panels could be downloaded.")
    return Fetched(pictures, expected=len(panels))


def fetch_dilbert(web: Web, today: date) -> Fetched:
    """A strip from the archive, the same one all day: the day seeds the pick."""
    with contextlib.suppress(SourceError):
        # Wake the dyno; a slow first answer is normal and the next is quick.
        web.get(f"{DILBERT}/", timeout=WAKE_TIMEOUT_S)
    picker = random.Random(today.toordinal())
    span = (DILBERT_LAST - DILBERT_FIRST).days
    failure = SourceError("The Dilbert archive didn't return a strip.")
    for _ in range(DILBERT_TRIES):
        day = DILBERT_FIRST + timedelta(days=picker.randrange(span + 1))
        try:
            reply = web.get(f"{DILBERT}/{day.isoformat()}")
        except SourceError as err:
            failure = err
            continue
        strip = pages.parse_dilbert(reply.text()) if reply.status < 400 else None
        if strip is None:
            continue
        try:
            raw = _download(web, strip.image_url, "Dilbert")
        except SourceError as err:
            failure = err
            continue
        return Fetched([(raw, "")], edition=strip.date)
    raise failure


def fetch_image_url(web: Web, source: Source) -> Fetched:
    return Fetched([(_download(web, source.url, source.label), "")])


# -- the refresh worker ----------------------------------------------------------


def _today() -> date:
    """The local day, in the timezone set under Settings when there is one."""
    try:
        from app.tz_resolve import app_timezone

        return datetime.now(app_timezone()).date()
    except Exception:
        return datetime.now().astimezone().date()


def is_due(source: Source, record: dict[str, Any] | None, today: str, now: float) -> bool:
    """Whether ``source`` should be fetched now, given its record."""
    if not record:
        return True
    last_attempt = float(record.get("last_attempt") or 0)
    if record.get("fetched_on") == today:
        edition = record.get("edition")
        return bool(
            source.kind == "gocomics"
            and edition
            and edition < today
            and record.get("attempted_on") == today
            and int(record.get("attempts") or 0) < EDITION_ATTEMPTS
            and now - last_attempt >= EDITION_RETRY_S
        )
    backoff = RETRY_STALE_S if record.get("pictures") else RETRY_MISSING_S
    return now - last_attempt >= backoff


@dataclass
class _Run:
    """One background refresh: a queue a render can add to while it runs.

    Queue entries carry a ``force`` flag (``?fresh=1``), which skips the
    due checks. ``completed`` remembers what this run already fetched, so a
    render working from a snapshot taken before that can't queue it again.
    """

    data_dir: Path
    today: date
    queue: list[tuple[Source, bool]] = field(default_factory=list)
    active: Source | None = None
    completed: set[str] = field(default_factory=set)
    done: bool = False
    finished: int = 0
    failed: int = 0
    thread: threading.Thread | None = None

    def keys(self) -> set[str]:
        keys = {s.key for s, _ in self.queue}
        if self.active is not None:
            keys.add(self.active.key)
        return keys

    def alive(self) -> bool:
        return not self.done and self.thread is not None and self.thread.is_alive()


# Kept outside this module (see images.shared) so a reload of the plugin
# finds the worker already running instead of starting a second one.
_RUNS: dict[str, _Run] = images.shared("runs", dict)
_RUNS_LOCK: threading.Lock = images.shared("runs_lock", threading.Lock)


def _enqueue(data_dir: Path, sources: list[Source], today: date, *, force: bool = False) -> _Run:
    """Queue ``sources`` on the running refresh, starting one if none runs.

    A run whose thread has died without finishing is replaced, not reused.
    The worker runs in a copy of the caller's context, so the widget's
    capability scope (its ``requires:`` hosts) still applies off-thread.
    """
    with _RUNS_LOCK:
        run = _RUNS.get(str(data_dir))
        start = run is None or not run.alive()
        if start:
            run = _Run(data_dir=data_dir, today=today)
            _RUNS[str(data_dir)] = run
        assert run is not None
        known = run.keys() | (set() if force else run.completed)
        run.queue.extend((s, force) for s in sources if s.key not in known)
        if start:
            context = contextvars.copy_context()
            run.thread = threading.Thread(
                target=context.run, args=(_work, run), name="comics-daily-refresh", daemon=True
            )
            run.thread.start()
    return run


def _running_keys(data_dir: Path) -> set[str]:
    with _RUNS_LOCK:
        run = _RUNS.get(str(data_dir))
        return run.keys() if run is not None and run.alive() else set()


def _report(run: _Run, source: Source) -> None:
    if jobs is None:
        return
    total = run.finished + len(run.queue) + 1
    with contextlib.suppress(Exception):
        jobs.report(
            PLUGIN_ID,
            "refresh",
            "Fetching today's comics",
            progress=run.finished / total,
            detail=source.label,
            label="Comics",
        )


def _finish_job(run: _Run, crashed: bool) -> None:
    if jobs is None:
        return
    with contextlib.suppress(Exception):
        summary = f"{run.finished - run.failed} of {run.finished} updated"
        if crashed or (run.failed and run.failed == run.finished):
            jobs.fail(PLUGIN_ID, "refresh", summary)
        else:
            jobs.finish(PLUGIN_ID, "refresh", summary)


def _work(run: _Run) -> None:
    """Drain the run's queue, one source at a time.

    Whatever happens, the run ends marked done with nothing active, so the
    next render starts a fresh worker rather than waiting on a dead one.
    """
    store = images.Store(run.data_dir)
    gocomics: GoComics | None = None
    web = Web()
    crashed = False
    try:
        while True:
            with _RUNS_LOCK:
                if not run.queue:
                    run.active = None
                    run.done = True
                    break
                source, force = run.queue.pop(0)
                run.active = source
            # The render that queued this read the state before earlier work
            # in this run landed; ask the state again, now.
            record = store.sources().get(source.key)
            if not force and not is_due(source, record, run.today.isoformat(), time.time()):
                continue
            _report(run, source)
            if source.kind == "gocomics" and gocomics is None:
                gocomics = GoComics(run.data_dir / COOKIE_FILE, run.today)
            if not refresh_source(store, source, run.today, web=web, gocomics=gocomics):
                run.failed += 1
            run.finished += 1
            run.completed.add(source.key)
    except Exception:
        crashed = True
        logger.exception("comics_daily: the refresh worker stopped")
    finally:
        with _RUNS_LOCK:
            run.active = None
            run.done = True
        with contextlib.suppress(Exception):
            store.prune()
        _finish_job(run, crashed)


def _fetch_source(source: Source, today: date, web: Web, gocomics: GoComics | None) -> Fetched:
    if source.kind == "farside":
        return fetch_farside(web)
    if source.kind == "dilbert":
        return fetch_dilbert(web, today)
    if source.kind == "url":
        return fetch_image_url(web, source)
    if gocomics is None:
        raise SourceError("GoComics session missing.")
    return gocomics.strip(source.name, source.label)


def refresh_source(
    store: Any, source: Source, today: date, *, web: Web, gocomics: GoComics | None
) -> bool:
    """Fetch one source and record the outcome; True when it fully updated.

    The previous pictures stay unless a complete new set was stored, so a
    failure never blanks the page and a half-downloaded Far Side row never
    replaces a whole one. Storage failures (a full disk) are logged and
    reported as a failed source, never raised into the worker.
    """
    today_iso = today.isoformat()
    started = time.time()

    def attempt(entry: dict[str, Any]) -> None:
        if entry.get("attempted_on") != today_iso:
            entry["attempted_on"] = today_iso
            entry["attempts"] = 0
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["last_attempt"] = started

    try:
        store.update(source.key, attempt)
    except Exception:
        logger.exception("comics_daily: could not record an attempt at %s", source.key)
        return False
    try:
        fetched = _fetch_source(source, today, web, gocomics)
        stored = _store_pictures(store, source, fetched)
    except SourceError as err:
        outcome: Callable[[dict[str, Any]], None] = _failed(str(err))
        complete = False
    except Exception:
        logger.exception("comics_daily: fetching %s failed", source.key)
        outcome = _failed(f"Couldn't update {source.label}.")
        complete = False
    else:
        expected = max(fetched.expected or 0, len(fetched.pictures))
        complete = len(stored) >= expected
        outcome = _succeeded(source, fetched, stored, expected, today_iso)
    try:
        entry = store.update(source.key, outcome)
    except Exception:
        logger.exception("comics_daily: could not record the outcome for %s", source.key)
        return False
    if complete:
        logger.info("comics_daily: updated %s (%s)", source.key, fetched.edition or today_iso)
    else:
        logger.warning("comics_daily: %s not updated: %s", source.key, entry.get("error"))
    return complete


def _failed(message: str) -> Callable[[dict[str, Any]], None]:
    return lambda entry: entry.update(error=message)


def _succeeded(
    source: Source, fetched: Fetched, stored: list[dict[str, Any]], expected: int, today: str
) -> Callable[[dict[str, Any]], None]:
    def record(entry: dict[str, Any]) -> None:
        if len(stored) < expected:
            # Keep the last complete set; a partial one only stands in when
            # there is nothing else. Not marked fetched today, so it retries.
            if not entry.get("pictures"):
                entry.update(pictures=stored, edition=fetched.edition)
            entry["error"] = f"Only {len(stored)} of {expected} pictures came through."
            return
        entry.update(
            pictures=stored,
            edition=fetched.edition,
            fetched_on=today,
            last_success=time.time(),
            error=None,
        )
        if fetched.label:
            entry["label"] = fetched.label

    return record


def _store_pictures(store: Any, source: Source, fetched: Fetched) -> list[dict[str, Any]]:
    """Convert and keep each fetched picture; skip any that isn't one.
    Raises SourceError when none survive, which leaves the old pictures up."""
    stored = []
    for raw, caption in fetched.pictures:
        try:
            picture = images.normalise(raw)
        except ValueError:
            continue
        name = store.add(source.key, picture)
        stored.append({"file": name, "w": picture.width, "h": picture.height, "caption": caption})
    if not stored:
        raise SourceError(f"The download for {source.label} wasn't a picture.")
    return stored


# -- widget entry point ----------------------------------------------------------


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return default if value is None else bool(value)


def _count(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return default


def _image_url(name: str) -> str:
    return f"/plugins/{PLUGIN_ID}/img/{name}"


def _pictures(record: dict[str, Any], store: Any) -> list[dict[str, Any]]:
    """The record's pictures whose files are still on disk."""
    out = []
    for pic in record.get("pictures") or []:
        if not isinstance(pic, dict):
            continue
        name = str(pic.get("file") or "")
        w, h = int(pic.get("w") or 0), int(pic.get("h") or 0)
        if w > 0 and h > 0 and store.image_path(name) is not None:
            out.append({"src": _image_url(name), "w": w, "h": h, "caption": pic.get("caption", "")})
    return out


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    store = images.Store(Path(ctx["data_dir"]))
    strips, problems = parse_strips(str(options.get("strips") or ""))
    show_farside = _bool(options.get("far_side"), True)
    wanted = ([FARSIDE] if show_farside else []) + strips
    if not wanted:
        if problems:
            return {"error": f"{problems[0].name}: {problems[0].message}"}
        return {"error": "Add some strips, or turn on the Far Side, in the cell editor."}

    today = _today()
    today_iso = today.isoformat()
    now = time.time()
    records = store.sources()
    force = bool(ctx.get("fresh"))
    due = [s for s in wanted if force or is_due(s, records.get(s.key), today_iso, now)]
    run = _enqueue(store.root, due, today, force=force) if due else None
    if run is not None and not any(_pictures(records.get(s.key) or {}, store) for s in wanted):
        # Nothing to show yet: give a quick source the chance to land in this
        # render rather than painting "fetching" and waiting for the repaint.
        if run.thread is not None:
            run.thread.join(FIRST_RENDER_WAIT_S)
        records = store.sources()
    busy = _running_keys(store.root)
    return _payload(wanted, problems, records, busy, options, store, today_iso)


def freshness(
    source: Source, record: dict[str, Any], has_pictures: bool, busy: bool, today: str
) -> tuple[str, str | None]:
    """How current a source's pictures are, and the date a note should show.

    ``ok``, ``stale`` (old pictures up), ``missing`` (none, and the last try
    failed) or ``pending``. A GoComics strip is judged by its own edition,
    not by whether today's request went through: a page that keeps answering
    with an old strip is stale once it is more than a day behind, or a day
    behind with today's retries spent. Archived sources (Dilbert) and image
    addresses carry no such date and go by the last success.
    """
    error = record.get("error")
    if not has_pictures:
        return ("missing" if error and not busy else "pending"), None
    edition = record.get("edition")
    if source.kind == "gocomics" and isinstance(edition, str) and edition < today:
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        spent = (
            record.get("attempted_on") == today
            and int(record.get("attempts") or 0) >= EDITION_ATTEMPTS
        )
        if edition < yesterday or spent or (error and record.get("fetched_on") != today):
            return "stale", edition
    if error and record.get("fetched_on") != today:
        return "stale", record.get("fetched_on")
    return "ok", None


def _payload(
    wanted: list[Source],
    problems: list[Problem],
    records: dict[str, dict[str, Any]],
    busy: set[str],
    options: dict[str, Any],
    store: Any,
    today: str,
) -> dict[str, Any]:
    farside: list[dict[str, Any]] = []
    strips: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for source in wanted:
        record = records.get(source.key) or {}
        pictures = _pictures(record, store)
        label = str(record.get("label") or source.label)
        error = record.get("error")
        state, since = freshness(source, record, bool(pictures), source.key in busy, today)
        sources.append(
            {
                "id": source.name,
                "label": label,
                "state": state,
                "updating": source.key in busy,
                "edition": record.get("edition"),
                "updated_on": record.get("fetched_on"),
                "message": error or "",
            }
        )
        if state in ("stale", "missing"):
            notes.append(
                {
                    "label": label,
                    "state": state,
                    "since": since,
                    "message": error or "",
                }
            )
        if source.kind == "farside":
            count = _count(options.get("far_side_count"), FARSIDE_MAX_PANELS, 1, FARSIDE_MAX_PANELS)
            farside = pictures[:count]
        elif pictures:
            first = pictures[0]
            strips.append(
                {
                    "id": source.name,
                    "label": label,
                    "src": first["src"],
                    "w": first["w"],
                    "h": first["h"],
                    "date": record.get("edition"),
                }
            )
    for problem in problems:
        notes.append({"label": problem.name, "state": "invalid", "message": problem.message})

    pending = [s for s in sources if s["updating"]]
    payload: dict[str, Any] = {
        "farside": farside,
        "strips": strips,
        "sources": sources,
        "notes": notes,
        "updating": bool(pending),
        "today": today,
    }
    if pending:
        payload["live_refresh_s"] = LIVE_REFRESH_S
        payload["live_pending"] = len(pending)
    elif not farside and not strips:
        failed = [s["message"] for s in sources if s["state"] == "missing" and s["message"]]
        if failed:
            payload["error"] = "Couldn't fetch any comics yet. " + failed[0]
    return payload


# -- serving the pictures --------------------------------------------------------

# Read-only, so the loopback renderer may load the pictures with no session
# (docs/widgets.md, "Serving your own media").
RENDER_SAFE_ENDPOINTS: tuple[str, ...] = (f"{PLUGIN_ID}_media.image",)


def _store() -> Any:
    plugin = current_app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID)
    if plugin is None:
        abort(404)
    return images.Store(Path(plugin.data_dir))


def blueprint() -> Blueprint:
    bp = Blueprint(f"{PLUGIN_ID}_media", __name__)

    @bp.get("/img/<name>")
    def image(name: str) -> Response:
        path = _store().image_path(name)
        if path is None:
            abort(404)
        response = send_file(path, conditional=True, max_age=365 * 86400)
        # Names are content hashes: a new strip is a new address.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    return bp
