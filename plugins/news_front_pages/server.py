"""news_front_pages, today's newspaper front pages from Freedom Forum.

Ported from the Visionect display manager's ``newspaper`` module, which ran a
daily cron to fetch each paper's PDF, rasterised page one with ImageMagick and
round-robined the enabled papers on every frame request. Here there is no cron
and no PDF tooling: ``fetch()`` answers from the pages saved in the widget's
data dir and queues what is missing for one background download worker.

Freedom Forum publishes each paper at
``https://cdn.freedomforum.org/dfp/jpg<day-of-month>/lg/<CODE>.jpg``. Three
quirks shape the code below:

* The path names only the day of the month, so each slot is reused every
  month. Freedom Forum clears a slot a couple of days after the edition, and a
  ``Last-Modified`` well before the edition date is refused anyway, so a slot
  that wasn't cleared can't pass last month's paper off as today's.
* Editions land shortly after midnight US Eastern, so "today" is the Eastern
  date whatever the server's own time zone is. A paper that isn't out yet (or
  doesn't print that day, like the Sunday WSJ) falls back to yesterday's and
  then the day before's edition, as the old cron did.
* The ``lg`` JPEG is the largest image on offer, about 700 px wide. Only the
  PDF is bigger, and rasterising it would need a dependency a widget can't
  declare, so a full-width page on a large panel is an upscale.

How a render works:

* ``fetch()`` never touches the network. Under the state lock it picks the
  paper, validates its newest saved page, marks that page as used (its mtime)
  and returns its URL. Papers whose edition for today is missing and due are
  queued for the worker, and the data carries ``live_refresh_s`` /
  ``live_pending`` while the shown paper's download is on the way, so editors
  repaint and a push is followed up when it lands. With nothing saved yet the
  cell says it is fetching.
* One daemon thread per data dir drains a de-duplicated queue (one job per
  paper and edition day), so concurrent renders trigger one download. It runs
  in a copy of the queuing render's context, so the widget's ``requires:``
  egress allowlist still applies. Downloads happen outside the lock; writes,
  validation, deletion and pruning only under it, and a delete only removes
  the exact file version that was judged (inode, size, mtime).
* The rotation moves at most once per ``ROTATION_HOLD_S``. The host composes a
  page ahead of a scheduled send (warm-ahead) and follows sends up, all as
  ordinary non-preview renders, and gives a widget no way to tell a render
  that will be shown from one that prepares it. The hold approximates "one
  paper per shown frame": a warm-ahead, its send and the follow-ups show one
  paper. The accepted cost: two separate sends inside the hold repeat a paper,
  and a warm-ahead that leads its send by more than the hold moves on early.

Layout under ``data_dir``::

    pages/<CODE>_<YYYYMMDD>.jpg   one validated download per paper and edition
    state.json                    paper shown + since when, download attempts and failures
    directory.json                Freedom Forum's paper list, for the editor's picker

A downloaded edition is kept until it is more than ``KEEP_DAYS`` old, a newer
one exists and it has gone unused for ``PRUNE_GRACE_S``, so a paper whose
download fails keeps showing its last good page, up to ``LAST_GOOD_MAX_DAYS``.
"""

from __future__ import annotations

import contextlib
import contextvars
import html
import http.client
import io
import json
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, send_from_directory
from PIL import Image
from werkzeug.wrappers import Response

from app.plugin_http import fetch_text

try:
    from app import jobs
except ImportError:  # an older Tesserae without the job tray
    jobs = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# The folder name is the plugin id, and the id is where the host mounts the
# blueprint, so an install under another folder name still serves its pages.
PLUGIN_ID = Path(__file__).resolve().parent.name

PAGE_URL = "https://cdn.freedomforum.org/dfp/jpg{day}/lg/{code}.jpg"
SITEMAP_URL = "https://frontpages.freedomforum.org/sitemap.xml"
USER_AGENT = f"tesserae/0.1 (+{PLUGIN_ID})"

# The worker's limits for one request: per socket operation, and in all, so a
# response trickling in a byte at a time can't hold the queue for long.
HTTP_TIMEOUT_S = 10.0
DOWNLOAD_BUDGET_S = 60.0
READ_CHUNK_BYTES = 64 * 1024
# choices() runs while the page editor loads, for every widget installed.
DIRECTORY_TIMEOUT_S = 4.0
# fetch() waits this long at most for the state lock, which is only ever held
# for file work (never a download).
LOCK_WAIT_S = 2.0
# Editors poll, and a push is followed up, this often while the paper a render
# waits for is next in line; later in the line the wait grows, up to the
# host's ceiling for the hint.
LIVE_REFRESH_S = 8
LIVE_REFRESH_MAX_S = 120
# One job per paper is all the queue ever needs; this only bounds a flood of
# pinned codes.
MAX_QUEUED = 32
# All of one job's requests (today, yesterday, the day before) together.
JOB_BUDGET_S = 90.0

LOOKBACK_DAYS = 3  # today, yesterday, the day before
RECHECK_S = 30 * 60  # how often to look again for an edition not out yet
KEEP_DAYS = 5
LAST_GOOD_MAX_DAYS = 30
# A Last-Modified this many days before the edition date is last month's file.
STALE_SLOT_DAYS = 3
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MIN_IMAGE_EDGE_PX = 300
MAX_TRIM_PCT = 25.0
# One paper per shown frame, approximately; see the module docstring.
ROTATION_HOLD_S = 4 * 60
# A page stays this long after its last use before pruning may delete it.
PRUNE_GRACE_S = 60 * 60

DIRECTORY_TTL_S = 7 * 24 * 60 * 60
DIRECTORY_RETRY_S = 60 * 60

PAGES_DIR = "pages"
STATE_FILE = "state.json"
DIRECTORY_FILE = "directory.json"

DEFAULT_PAPERS = "NY_NYT 3.5\nWSJ 2\nUSAT\n# AZ_ADS"

CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PAGE_FILE_RE = re.compile(r"^([A-Z0-9_-]+)_(\d{8})\.jpg$")
_SITEMAP_SLUG_RE = re.compile(r"<loc>\s*https://frontpages\.freedomforum\.org/newspapers/([^<\s]+)")

# Freedom Forum publishes on US Eastern time; see the module docstring.
_EDITION_TZ = ZoneInfo("America/New_York")

# A few well-known papers for the editor's picker when Freedom Forum's list
# can't be read. Freedom Forum's sitemap leaves some papers out (the New York
# Times among them), so these are merged into a scraped list too.
FALLBACK_DIRECTORY: dict[str, str] = {
    "NY_NYT": "The New York Times",
    "WSJ": "The Wall Street Journal",
    "USAT": "USA Today",
    "AZ_ADS": "Arizona Daily Star",
    "AZ_AR": "The Arizona Republic",
    "CA_LAT": "Los Angeles Times",
    "CA_SFC": "San Francisco Chronicle",
    "CAN_TGAM": "The Globe and Mail",
    "CAN_TS": "Toronto Star",
    "FL_MH": "The Miami Herald",
    "GA_AJC": "The Atlanta Journal-Constitution",
    "IL_CT": "Chicago Tribune",
    "MA_BG": "The Boston Globe",
    "MN_ST": "Star Tribune",
    "NY_DN": "Daily News",
    "NY_ND": "Newsday",
    "PA_PI": "The Philadelphia Inquirer",
    "TX_DMNHN": "The Dallas Morning News",
    "TX_HC": "Houston Chronicle",
    "WA_ST": "The Seattle Times",
}


def _shared(name: str, factory: Callable[[], Any]) -> Any:
    """Process-wide state that outlives a reload of this module. A reload
    imports server.py again; module globals would give the new code its own
    lock and worker registry while the old worker still writes to the same
    folder. One holder module, created once, gives every version the same."""
    holder_name = f"_tesserae_runtime.{PLUGIN_ID}"
    holder = sys.modules.setdefault(holder_name, ModuleType(holder_name))
    return holder.__dict__.setdefault(name, factory())


# Guards the pages folder and state.json: validation, deletion, pruning,
# writes, the rotation. Never held across a download.
_LOCK: threading.Lock = _shared("state_lock", threading.Lock)
# Guards the worker registry below.
_RUNS_LOCK: threading.Lock = _shared("runs_lock", threading.Lock)
# The last page each placement got under the lock, for a render that can't
# have the lock in time: (data dir, pinned code or "") -> (wall time, payload).
_LAST_CLAIMED: dict[tuple[str, str], tuple[float, dict[str, Any]]] = _shared("last_claimed", dict)

# Clocks, as module names so tests can drive them: wall time is persisted
# (rotation, rechecks, file ages), the monotonic one times a download.
_wall = time.time
_clock = time.monotonic


class CdnUnreachable(Exception):
    """Freedom Forum didn't answer (network error, 403, 5xx, a cut-off or too
    slow body). Unlike a 404, which only means one edition isn't out, this
    ends the job: the older editions would fail the same way."""


@dataclass(frozen=True)
class Paper:
    """One line of the ``papers`` setting."""

    code: str
    enabled: bool = True
    trim: float = 0.0  # percent of the page height cut off the top


@dataclass(frozen=True)
class Page:
    """A saved edition that decoded as a front page."""

    path: Path
    day: date
    width: int
    height: int


# ----- settings --------------------------------------------------------------


def clean_code(raw: Any) -> str | None:
    """A paper code as the CDN spells it (upper case), or None if it isn't one.
    Freedom Forum's own page URLs carry the code in lower case, so a code
    copied from there is accepted too."""
    code = str(raw or "").strip().upper()
    return code if CODE_RE.match(code) else None


def _parse_trim(raw: str) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return min(max(value, 0.0), MAX_TRIM_PCT) if math.isfinite(value) else None


def parse_papers(text: Any) -> list[Paper]:
    """Parse the ``papers`` setting: ``CODE`` or ``CODE TRIM`` per line, ``#``
    to disable a line. Anything else (a ``# heading`` comment, a typo) is
    skipped rather than guessed at, and a repeated code keeps its first line."""
    papers: list[Paper] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        enabled = not line.startswith("#")
        parts = line.lstrip("#").split()
        if not parts or len(parts) > 2:
            continue
        code = clean_code(parts[0])
        trim = _parse_trim(parts[1]) if len(parts) == 2 else 0.0
        if code is None or trim is None or code in seen:
            continue
        seen.add(code)
        papers.append(Paper(code, enabled, trim))
    return papers


# ----- small file helpers ----------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write via a temp file and a rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_json(path: Path, data: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        _write_atomic(path, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))


def _identity(path: Path) -> tuple[int, int, int] | None:
    """Which version of a file this is: inode, size and mtime."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def _unlink_if_same(path: Path, identity: tuple[int, int, int] | None) -> bool:
    """Delete ``path`` only if it is still the version that was judged. A file
    replaced since (a new download renamed over it) is left alone. Callers
    hold ``_LOCK``, so within this process nothing can slip in between."""
    if identity is None or _identity(path) != identity:
        return False
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    return True


# ----- downloading (worker thread only) --------------------------------------


def _edition_today() -> date:
    return datetime.now(_EDITION_TZ).date()


def _slot_is_stale(last_modified: str | None, day: date) -> bool:
    if not last_modified:
        return False
    try:
        modified = parsedate_to_datetime(last_modified)
    except (TypeError, ValueError):
        return False
    return modified.date() < day - timedelta(days=STALE_SLOT_DAYS)


def page_size(body: bytes) -> tuple[int, int] | None:
    """The pixel size of a complete JPEG or PNG big enough to be a front page,
    or None. The CDN answers a missing paper with a 404 page, but a proxy or a
    captive portal can hand back HTML with a 200, and a cut-off download
    decodes as a page with a grey bottom; neither may stand in for a good page.
    ``load()`` rather than ``verify()``: for a JPEG, only a full decode notices
    truncation."""
    try:
        with Image.open(io.BytesIO(body)) as img:
            fmt, size = img.format, img.size
            img.load()
    except Exception:  # Pillow raises a zoo of types for a broken file
        return None
    return size if fmt in ("JPEG", "PNG") and min(size) >= MIN_IMAGE_EDGE_PX else None


def _read_capped(resp: Any, deadline: float) -> bytes:
    """The body, stopping one byte past ``MAX_IMAGE_BYTES`` and at ``deadline``.
    ``read1`` returns whatever one socket read brings, where ``read(n)`` would
    wait for all n bytes, so the deadline is checked at least once per
    ``HTTP_TIMEOUT_S`` however slowly the bytes arrive."""
    read = getattr(resp, "read1", None) or resp.read
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_IMAGE_BYTES:
        if _clock() >= deadline:
            raise CdnUnreachable("download too slow")
        chunk = read(min(READ_CHUNK_BYTES, MAX_IMAGE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def download_edition(code: str, day: date, deadline: float | None = None) -> bytes | None:
    """The ``code`` front page for ``day``, validated, or None when that
    edition isn't there. Raises :class:`CdnUnreachable` when the CDN fails or
    ``deadline`` (on ``_clock``; at most ``DOWNLOAD_BUDGET_S`` away) passes."""
    deadline = min(deadline or math.inf, _clock() + DOWNLOAD_BUDGET_S)
    if deadline <= _clock():
        raise CdnUnreachable("out of time")
    url = PAGE_URL.format(day=day.day, code=code)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg,image/*;q=0.8"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
            body = _read_capped(resp, deadline)
            headers = getattr(resp, "headers", None)
            last_modified = headers.get("Last-Modified") if headers is not None else None
    except urllib.error.HTTPError as err:
        err.close()
        if err.code == 404:
            return None
        raise CdnUnreachable(f"HTTP {err.code}") from err
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as err:
        # HTTPException covers a response cut off mid-body (IncompleteRead).
        raise CdnUnreachable(str(getattr(err, "reason", None) or err)) from err
    if len(body) > MAX_IMAGE_BYTES or _slot_is_stale(last_modified, day):
        logger.info("%s: %s for %s refused (oversized or stale slot)", PLUGIN_ID, code, day)
        return None
    if page_size(body) is None:
        logger.info("%s: %s for %s is not an image", PLUGIN_ID, code, day)
        return None
    return body


# ----- the pages on disk (callers hold _LOCK) --------------------------------


def _page_day(name: str) -> tuple[str, date] | None:
    match = _PAGE_FILE_RE.match(name)
    if match is None:
        return None
    try:
        return match.group(1), datetime.strptime(match.group(2), "%Y%m%d").date()
    except ValueError:
        return None


def edition_path(pages: Path, code: str, day: date) -> Path:
    return pages / f"{code}_{day:%Y%m%d}.jpg"


def newest_page(pages: Path, code: str, today: date) -> Page | None:
    """The paper's newest saved edition that decodes and isn't too old to show.
    A file that doesn't decode (zero bytes, damaged) is deleted on the way, so
    it can neither hide an older good page nor stand in for today's and block
    its download; only that exact version is deleted, never a replacement."""
    oldest = today - timedelta(days=LAST_GOOD_MAX_DAYS)
    dated: list[tuple[date, Path]] = []
    for path in pages.glob(f"{code}_*.jpg"):
        parsed = _page_day(path.name)
        if parsed is not None and parsed[0] == code and oldest <= parsed[1] <= today:
            dated.append((parsed[1], path))
    for day, path in sorted(dated, reverse=True):
        identity = _identity(path)
        try:
            size = page_size(path.read_bytes())
        except OSError:
            continue
        if size is not None:
            return Page(path, day, *size)
        if _unlink_if_same(path, identity):
            logger.warning("%s: removed unreadable %s", PLUGIN_ID, path.name)
    return None


def _claim(page: Page) -> bool:
    """Mark ``page`` as just used, so pruning leaves it for ``PRUNE_GRACE_S``.
    Only a claimed page's URL is ever handed out."""
    try:
        os.utime(page.path)
    except OSError:
        return False
    return True


def prune(pages: Path, today: date, now: float) -> None:
    """Drop editions past ``KEEP_DAYS`` unless they are a paper's newest (its
    last good page), everything past ``LAST_GOOD_MAX_DAYS``, and temp files an
    interrupted write left behind; each only once it has gone unused for
    ``PRUNE_GRACE_S``, since a renderer may still be loading a page a render
    handed out a moment ago."""
    found: list[tuple[Path, str, date]] = []
    newest: dict[str, date] = {}
    for path in pages.glob("*.jpg"):
        parsed = _page_day(path.name)
        if parsed is not None:
            found.append((path, *parsed))
            newest[parsed[0]] = max(parsed[1], newest.get(parsed[0], parsed[1]))
    doomed = list(pages.glob(".*.tmp"))
    for path, code, day in found:
        age = (today - day).days
        if age > LAST_GOOD_MAX_DAYS or (age > KEEP_DAYS and day != newest[code]):
            doomed.append(path)
    for path in doomed:
        identity = _identity(path)
        if identity is not None and now - identity[2] / 1e9 > PRUNE_GRACE_S:
            _unlink_if_same(path, identity)


# ----- rotation --------------------------------------------------------------


def rotation(enabled: list[str], last: str | None) -> list[str]:
    """Enabled codes from the one after ``last`` on round the list, ``last`` at the end."""
    start = enabled.index(last) + 1 if last in enabled else 0
    return enabled[start:] + enabled[:start]


def plan(enabled: list[str], state: dict[str, Any], now: float) -> tuple[list[str], str | None]:
    """The codes this render tries, in order, and the paper being held, if any.
    Inside ``ROTATION_HOLD_S`` of the last move the current paper comes first;
    after it, the next one does. The caller skips a paper with no page, so one
    dead code doesn't stall the rotation."""
    current = state.get("current")
    current = current if isinstance(current, str) else None
    since = state.get("since")
    order = rotation(enabled, current)
    held = isinstance(since, (int, float)) and 0 <= now - since < ROTATION_HOLD_S
    if current in enabled and held:
        return [current, *order[:-1]], current
    return order, None


def _section(state: dict[str, Any], name: str) -> dict[str, Any]:
    value = state.get(name)
    return dict(value) if isinstance(value, dict) else {}


def _recently_checked(state: dict[str, Any], code: str, now: float) -> bool:
    last = _section(state, "checked").get(code)
    return isinstance(last, (int, float)) and 0 <= now - last < RECHECK_S


# ----- the download worker ---------------------------------------------------


@dataclass(frozen=True)
class _Job:
    """Fetch the newest edition of ``code`` as of ``today``."""

    code: str
    today: date
    force: bool = False  # ?fresh=1: skip the recheck wait


@dataclass
class _Run:
    """One worker thread and its queue, for one data dir. Renders add to the
    queue while it runs; it ends when the queue is empty, marking itself done
    in the same locked step, so a render that queues after that starts a new
    worker instead of handing work to one that is leaving."""

    data_dir: Path
    queue: list[_Job] = field(default_factory=list)
    active: _Job | None = None
    done: bool = False
    thread: threading.Thread | None = None

    def line(self) -> list[str]:
        """Codes in the order they will be worked on, the running one first."""
        head = [self.active.code] if self.active is not None else []
        return head + [job.code for job in self.queue]

    def alive(self) -> bool:
        return not self.done and self.thread is not None and self.thread.is_alive()

    def add(self, job: _Job, *, first: bool) -> None:
        """Queue ``job``, at most once per paper: a paper already waiting
        keeps its place (or moves to the front for ``first``), taking the
        later day and the force flag. A paper being downloaded now for the
        same day isn't queued again."""
        active = self.active
        if active is not None and active.code == job.code and active.today >= job.today:
            return
        for index, queued in enumerate(self.queue):
            if queued.code == job.code:
                job = _Job(job.code, max(job.today, queued.today), job.force or queued.force)
                del self.queue[index]
                self.queue.insert(0 if first else index, job)
                return
        if len(self.queue) >= MAX_QUEUED:
            return  # a later render queues it again
        if first:
            self.queue.insert(0, job)
        else:
            self.queue.append(job)


_RUNS: dict[str, _Run] = _shared("runs", dict)


def _enqueue(
    data_dir: Path, codes: list[str], today: date, *, force: bool, first: str | None = None
) -> None:
    """Queue a job per code, ``first`` (the paper this render is waiting
    for) at the front, starting the worker if none runs. The worker runs in a
    copy of this render's context, so the capability scope (the
    ``requires:`` hosts) applies to its downloads too."""
    with _RUNS_LOCK:
        run = _RUNS.get(str(data_dir))
        start = run is None or not run.alive()
        if start:
            run = _RUNS[str(data_dir)] = _Run(data_dir)
        assert run is not None
        for code in codes:
            if code != first:
                run.add(_Job(code, today, force), first=False)
        if first in codes:
            run.add(_Job(first, today, force), first=True)
        if start and run.queue:
            context = contextvars.copy_context()
            run.thread = threading.Thread(
                target=context.run, args=(_work, run), name=f"{PLUGIN_ID}-downloads", daemon=True
            )
            run.thread.start()


def _line(data_dir: Path) -> list[str]:
    """Codes queued or being downloaded, in the order they'll be done."""
    with _RUNS_LOCK:
        run = _RUNS.get(str(data_dir))
        return run.line() if run is not None and run.alive() else []


def _join_worker(data_dir: Path, timeout: float) -> bool:
    """Wait for the data dir's worker to finish; True when it has (tests)."""
    with _RUNS_LOCK:
        run = _RUNS.get(str(data_dir))
        thread = run.thread if run is not None else None
    if thread is not None:
        thread.join(timeout)
    return thread is None or not thread.is_alive()


def _report(job: _Job, waiting: int) -> None:
    if jobs is None:
        return
    with contextlib.suppress(Exception):
        jobs.report(
            PLUGIN_ID,
            "download",
            "Fetching front pages",
            progress=None,
            detail=f"{job.code}, {waiting} more" if waiting else job.code,
            label="Front pages",
        )


def _work(run: _Run) -> None:
    """Drain the run's queue, one job at a time. Whatever happens, the run
    ends marked done with nothing active, so the next render starts a fresh
    worker rather than waiting on a dead one."""
    try:
        while True:
            with _RUNS_LOCK:
                if not run.queue:
                    run.done = True  # in the same step as the empty check
                    break
                job = run.queue.pop(0)
                run.active = job
                waiting = len(run.queue)
            _report(job, waiting)
            _refresh(run.data_dir, job)
            with _RUNS_LOCK:
                run.active = None
    except Exception:
        logger.exception("%s: the download worker stopped", PLUGIN_ID)
    finally:
        with _RUNS_LOCK:
            run.active = None
            run.done = True
        if jobs is not None:
            with contextlib.suppress(Exception):
                jobs.finish(PLUGIN_ID, "download")


def _refresh(data_dir: Path, job: _Job) -> None:
    """One job: look at the state again (the render that queued it read it
    before earlier jobs landed), download outside the lock, save under it."""
    pages = data_dir / PAGES_DIR
    state_path = data_dir / STATE_FILE
    with _LOCK:
        state = _read_json(state_path)
        if not job.force and _recently_checked(state, job.code, _wall()):
            return
        have = newest_page(pages, job.code, job.today)
    if have is not None and have.day >= job.today:
        return
    saved: tuple[date, bytes] | None = None
    failure = "missing"
    deadline = _clock() + JOB_BUDGET_S
    for back in range(LOOKBACK_DAYS):
        day = job.today - timedelta(days=back)
        if have is not None and day <= have.day:
            break  # nothing older can beat an edition already kept
        try:
            body = download_edition(job.code, day, deadline)
        except Exception as err:  # CdnUnreachable, or the host refusing the egress
            logger.warning("%s: Freedom Forum unreachable (%s)", PLUGIN_ID, err)
            failure = "unreachable"
            break
        if body is not None:
            saved = (day, body)
            break
    now = _wall()
    with _LOCK:
        if saved is not None:
            try:
                _write_atomic(edition_path(pages, job.code, saved[0]), saved[1])
            except OSError as err:
                logger.warning("%s: could not save %s: %s", PLUGIN_ID, job.code, err)
                saved = None
            else:
                prune(pages, job.today, now)
        state = _read_json(state_path)
        state["checked"] = {
            code: when
            for code, when in {**_section(state, "checked"), job.code: now}.items()
            if isinstance(when, (int, float)) and now - when < RECHECK_S
        }
        failed = _section(state, "failed")
        failed.pop(job.code, None)
        if saved is None and have is None:
            failed[job.code] = failure
        state["failed"] = failed
        _write_json(state_path, state)


# ----- widget contract: fetch ------------------------------------------------


def _fallback(key: tuple[str, str], now: float) -> dict[str, Any]:
    """For a render that couldn't have the lock in time: the last page this
    placement got under the lock, if it's recent enough that pruning can't
    have taken it, else the fetching state, retried shortly."""
    last = _LAST_CLAIMED.get(key)
    if last is not None and 0 <= now - last[0] < PRUNE_GRACE_S / 2:
        return dict(last[1])
    return {"pending": True, "live_refresh_s": LIVE_REFRESH_S}


def _missing_error(codes: list[str], state: dict[str, Any], pinned: str | None) -> dict[str, Any]:
    failed = _section(state, "failed")
    if codes and all(failed.get(code) == "unreachable" for code in codes):
        return {"error": "Couldn't reach Freedom Forum, and no front page is saved yet."}
    which = f"{pinned} front page" if pinned else "front page from the paper list"
    return {"error": f"No {which} in the last {LOOKBACK_DAYS} days."}


def _wait_hint(position: int) -> dict[str, Any]:
    """Refresh hints for a render waiting on the job at ``position`` in the
    worker's line (0 = downloading now). The host follows a push up a limited
    number of times, so the wait grows with the jobs ahead instead of
    spending those follow-ups on a fixed short interval."""
    seconds = min(LIVE_REFRESH_MAX_S, LIVE_REFRESH_S * (position + 1))
    return {"live_refresh_s": seconds, "live_pending": position + 1}


@dataclass
class _Selection:
    """What one look at the saved pages found, under the lock."""

    chosen: tuple[str, Page] | None
    state: dict[str, Any]
    due: list[str]  # papers whose edition for today is missing and due
    wanted: str | None  # the due paper this render would show once it lands


def _select(
    data_dir: Path,
    candidates: list[str],
    *,
    pinned: str | None,
    today: date,
    now: float,
    force: bool,
    preview: bool,
) -> _Selection | None:
    """Pick the paper and claim its newest page, all under the lock; None
    when the lock can't be had within ``LOCK_WAIT_S``."""
    pages = data_dir / PAGES_DIR
    state_path = data_dir / STATE_FILE
    if not _LOCK.acquire(timeout=LOCK_WAIT_S):
        return None
    try:
        state = _read_json(state_path)
        order, held = (candidates, None) if pinned else plan(candidates, state, now)
        chosen: tuple[str, Page] | None = None
        for code in order:
            page = newest_page(pages, code, today)
            if page is not None and _claim(page):
                chosen = (code, page)
                break
        # A preview (the editor, the dashboards list) shows what the panel gets
        # next but never moves the rotation; repeating the held paper doesn't either.
        if chosen and not pinned and not preview and chosen[0] != held:
            state["current"], state["since"] = chosen[0], now
            _write_json(state_path, state)
        due = [
            code
            for code in candidates
            if not edition_path(pages, code, today).is_file()
            and (force or not _recently_checked(state, code, now))
        ]
    finally:
        _LOCK.release()
    if chosen is not None:
        wanted = chosen[0] if chosen[0] in due else None
    else:
        wanted = next((code for code in order if code in due), None)
    return _Selection(chosen, state, due, wanted)


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    papers = parse_papers(settings.get("papers", DEFAULT_PAPERS))
    pinned_raw = str(options.get("paper") or "").strip()
    pinned = clean_code(pinned_raw) if pinned_raw else None
    if pinned_raw and pinned is None:
        return {"error": f"'{pinned_raw}' isn't a Freedom Forum paper code."}
    candidates = [pinned] if pinned else [p.code for p in papers if p.enabled]
    if not candidates:
        return {
            "error": "No papers to show. Add paper codes under Settings → Widgets → News, Front Pages."
        }

    data_dir = Path(ctx["data_dir"])
    today = _edition_today()
    now = _wall()
    force = bool(ctx.get("fresh"))
    key = (str(data_dir), pinned or "")

    def select() -> _Selection | None:
        return _select(
            data_dir,
            candidates,
            pinned=pinned,
            today=today,
            now=now,
            force=force,
            preview=bool(ctx.get("preview")),
        )

    picked = select()
    if picked is None:
        logger.info("%s: state busy, answering from the last page handed out", PLUGIN_ID)
        return _fallback(key, now)
    if picked.due:
        _enqueue(data_dir, picked.due, today, force=force, first=picked.wanted)
        line = _line(data_dir)
        if any(code not in line for code in picked.due):
            # A download finished between queueing it and now: look again,
            # rather than answer from the snapshot taken before it landed.
            picked = select() or picked
    line = _line(data_dir)

    if picked.chosen is None:
        waiting = [line.index(code) for code in candidates if code in line]
        if waiting:
            return {"pending": True, **_wait_hint(min(waiting))}
        return _missing_error(candidates, picked.state, pinned)

    code, page = picked.chosen
    trims = {p.code: p.trim for p in papers}
    payload: dict[str, Any] = {
        "url": f"/plugins/{PLUGIN_ID}/page/{page.path.name}",
        "code": code,
        "date": page.day.isoformat(),
        "width": page.width,
        "height": page.height,
        "trim": trims.get(code, 0.0),
    }
    _LAST_CLAIMED[key] = (now, payload)
    if code in line:
        # Only the shown paper's download changes this frame.
        return {**payload, **_wait_hint(line.index(code))}
    return payload


# ----- widget contract: choices ---------------------------------------------


def _plugin() -> Any:
    return current_app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID)


def _settings() -> dict[str, Any]:
    plugin = _plugin()
    store = current_app.config.get("SETTINGS_STORE")
    if plugin is None or store is None:
        return {}
    result: dict[str, Any] = store.get_for_runtime(
        "plugins", PLUGIN_ID, plugin.manifest.get("settings", [])
    )
    return result


def scrape_directory() -> dict[str, str]:
    """Code → name for every paper in Freedom Forum's sitemap. Its paper URLs
    read ``/newspapers/<code>-<Name_With_Underscores>``."""
    text = fetch_text(SITEMAP_URL, headers={"User-Agent": USER_AGENT}, timeout=DIRECTORY_TIMEOUT_S)
    out: dict[str, str] = {}
    for slug in _SITEMAP_SLUG_RE.findall(text):
        raw_code, _, raw_name = urllib.parse.unquote(html.unescape(slug)).partition("-")
        code = clean_code(raw_code)
        name = " ".join(raw_name.replace("_", " ").split())
        if code and name:
            out[code] = name
    return out


def directory(data_dir: Path) -> dict[str, str]:
    """Freedom Forum's paper list, cached for a week. A failed read is retried
    after an hour, so a Freedom Forum outage doesn't slow every editor load."""
    path = data_dir / DIRECTORY_FILE
    cached = _read_json(path)
    kept = cached.get("papers")
    papers: dict[str, str] = kept if isinstance(kept, dict) else {}
    fetched_at = cached.get("fetched_at")
    age = time.time() - fetched_at if isinstance(fetched_at, (int, float)) else None
    ttl = DIRECTORY_TTL_S if cached.get("ok") else DIRECTORY_RETRY_S
    if age is None or not 0 <= age < ttl:
        try:
            scraped = scrape_directory()
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            logger.info("%s: paper list unavailable: %s", PLUGIN_ID, err)
            scraped = {}
        if scraped:
            papers = scraped
        _write_json(path, {"fetched_at": time.time(), "ok": bool(scraped), "papers": papers})
    return {**FALLBACK_DIRECTORY, **papers}


def choices(name: str) -> list[dict[str, str]]:
    if name != "papers":
        return []
    plugin = _plugin()
    names = directory(Path(plugin.data_dir)) if plugin is not None else dict(FALLBACK_DIRECTORY)
    rows = [{"value": "", "label": "Rotate through the paper list"}]
    listed = [p.code for p in parse_papers(_settings().get("papers", DEFAULT_PAPERS))]
    rest = sorted((c for c in names if c not in listed), key=lambda c: names[c].casefold())
    for code in [*listed, *rest]:
        label = f"{names[code]} ({code})" if code in names else code
        rows.append({"value": code, "label": label})
    return rows


# ----- page images -----------------------------------------------------------


# The widget's <img> points here and the panel render fetches it over loopback
# with no session (see app/auth.py). Read-only: it serves files this module
# wrote, by a name that has to match the page-file pattern.
RENDER_SAFE_ENDPOINTS: tuple[str, ...] = (f"{PLUGIN_ID}.serve_page",)


def blueprint() -> Blueprint:
    bp = Blueprint(PLUGIN_ID, __name__)

    @bp.get("/page/<name>")
    def serve_page(name: str) -> Response:
        plugin = _plugin()
        if plugin is None or _page_day(name) is None:
            abort(404)
        # Each edition has its own file name, so a day's cache is safe.
        return send_from_directory(
            Path(plugin.data_dir) / PAGES_DIR, name, mimetype="image/jpeg", max_age=86400
        )

    return bp
