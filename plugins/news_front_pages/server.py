"""news_front_pages, today's newspaper front pages from Freedom Forum.

Ported from the Visionect display manager's ``newspaper`` module, which ran a
daily cron to fetch each paper's PDF, rasterised page one with ImageMagick and
round-robined the enabled papers on every frame request. Here the download
happens inside ``fetch()``, on demand, and the rotation cursor lives in the
widget's data dir, so there is no cron and no PDF tooling to install.

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

Layout under ``data_dir``::

    pages/<CODE>_<YYYYMMDD>.jpg   one validated download per paper and edition
    state.json                    rotation cursor + when each paper was last looked for
    directory.json                Freedom Forum's paper list, for the editor's picker

A downloaded edition is kept until it is more than ``KEEP_DAYS`` old and a
newer one exists, so a paper whose download fails keeps showing its last good
page, up to ``LAST_GOOD_MAX_DAYS``.
"""

from __future__ import annotations

import contextlib
import html
import io
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, send_from_directory
from PIL import Image
from werkzeug.wrappers import Response

from app.plugin_http import fetch_text

logger = logging.getLogger(__name__)

# The folder name is the plugin id, and the id is where the host mounts the
# blueprint, so an install under another folder name still serves its pages.
PLUGIN_ID = Path(__file__).resolve().parent.name

PAGE_URL = "https://cdn.freedomforum.org/dfp/jpg{day}/lg/{code}.jpg"
SITEMAP_URL = "https://frontpages.freedomforum.org/sitemap.xml"
USER_AGENT = f"tesserae/0.1 (+{PLUGIN_ID})"

# The composer gives a widget's fetch() about six seconds; one slow request
# must not spend all of it.
HTTP_TIMEOUT_S = 5.0
# choices() runs while the page editor loads, for every widget installed.
DIRECTORY_TIMEOUT_S = 4.0

LOOKBACK_DAYS = 3  # today, yesterday, the day before
RECHECK_S = 30 * 60  # how often to look again for an edition not out yet
KEEP_DAYS = 5
LAST_GOOD_MAX_DAYS = 30
# A Last-Modified this many days before the edition date is last month's file.
STALE_SLOT_DAYS = 3
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MIN_IMAGE_EDGE_PX = 300
MAX_TRIM_PCT = 25.0
TEMP_MAX_AGE_S = 60 * 60

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

# Serialises rotation and downloads: two renders at once would otherwise both
# read the same cursor and show the same paper.
_LOCK = threading.Lock()


class CdnUnreachable(Exception):
    """Freedom Forum didn't answer (network error, 403, 5xx). Unlike a 404,
    which only means one edition isn't out, this stops further downloads for
    the rest of the render so one outage costs one timeout, not nine."""


@dataclass(frozen=True)
class Paper:
    """One line of the ``papers`` setting."""

    code: str
    enabled: bool = True
    trim: float = 0.0  # percent of the page height cut off the top


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
    """Write via a temp file and a rename, so a render never reads half a file."""
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


# ----- downloading -----------------------------------------------------------


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


def is_page_image(body: bytes) -> bool:
    """True for a complete JPEG or PNG big enough to be a front page. The CDN
    answers a missing paper with a 404 page, but a proxy or a captive portal
    can hand back HTML with a 200, and a cut-off download decodes as a page
    with a grey bottom; neither may replace a good page. ``load()`` rather
    than ``verify()``: for a JPEG, only a full decode notices truncation."""
    try:
        with Image.open(io.BytesIO(body)) as img:
            fmt, (width, height) = img.format, img.size
            img.load()
    except Exception:  # Pillow raises a zoo of types for a broken file
        return False
    return fmt in ("JPEG", "PNG") and min(width, height) >= MIN_IMAGE_EDGE_PX


def download_edition(code: str, day: date) -> bytes | None:
    """The ``code`` front page for ``day``, validated, or None when that
    edition isn't there. Raises :class:`CdnUnreachable` when the CDN fails."""
    url = PAGE_URL.format(day=day.day, code=code)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg,image/*;q=0.8"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
            body: bytes = resp.read(MAX_IMAGE_BYTES + 1)
            headers = getattr(resp, "headers", None)
            last_modified = headers.get("Last-Modified") if headers is not None else None
    except urllib.error.HTTPError as err:
        err.close()
        if err.code == 404:
            return None
        raise CdnUnreachable(f"HTTP {err.code}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise CdnUnreachable(str(getattr(err, "reason", err))) from err
    if len(body) > MAX_IMAGE_BYTES or _slot_is_stale(last_modified, day):
        logger.info("%s: %s for %s refused (oversized or stale slot)", PLUGIN_ID, code, day)
        return None
    if not is_page_image(body):
        logger.info("%s: %s for %s is not an image", PLUGIN_ID, code, day)
        return None
    return body


# ----- the shelf: pages on disk + state --------------------------------------


def _page_day(name: str) -> tuple[str, date] | None:
    match = _PAGE_FILE_RE.match(name)
    if match is None:
        return None
    try:
        return match.group(1), datetime.strptime(match.group(2), "%Y%m%d").date()
    except ValueError:
        return None


class Shelf:
    """The downloaded pages and the rotation state, for one fetch() call."""

    def __init__(self, data_dir: Path, *, today: date, now: float, fresh: bool = False) -> None:
        self.pages = data_dir / PAGES_DIR
        self.state_path = data_dir / STATE_FILE
        self.today = today
        self.now = now
        self.fresh = fresh
        self.state = _read_json(self.state_path)
        self.offline = False
        self.downloaded = False
        self._dirty = False

    @property
    def last_shown(self) -> str | None:
        last = self.state.get("last")
        return last if isinstance(last, str) else None

    def mark_shown(self, code: str) -> None:
        if self.state.get("last") != code:
            self.state["last"] = code
            self._dirty = True

    def edition_path(self, code: str, day: date) -> Path:
        return self.pages / f"{code}_{day:%Y%m%d}.jpg"

    def newest(self, code: str) -> tuple[Path, date] | None:
        """The paper's newest edition on disk, if it isn't too old to show."""
        oldest = self.today - timedelta(days=LAST_GOOD_MAX_DAYS)
        best: tuple[Path, date] | None = None
        for path in self.pages.glob(f"{code}_*.jpg"):
            parsed = _page_day(path.name)
            if parsed is None or parsed[0] != code or not oldest <= parsed[1] <= self.today:
                continue
            if best is None or parsed[1] > best[1]:
                best = (path, parsed[1])
        return best

    def page_for(self, code: str) -> tuple[Path, date] | None:
        """Today's edition if it is (or can now be) on disk, else the newest one kept."""
        if not self.edition_path(code, self.today).is_file() and self._due(code):
            self._fetch_newest(code)
        return self.newest(code)

    def _due(self, code: str) -> bool:
        if self.offline:
            return False
        if self.fresh:
            return True
        checked = self.state.get("checked")
        last = checked.get(code) if isinstance(checked, dict) else None
        return not isinstance(last, (int, float)) or self.now - last >= RECHECK_S

    def _fetch_newest(self, code: str) -> None:
        checked = self.state.get("checked")
        if not isinstance(checked, dict):
            checked = self.state["checked"] = {}
        checked[code] = self.now
        self._dirty = True
        for back in range(LOOKBACK_DAYS):
            day = self.today - timedelta(days=back)
            path = self.edition_path(code, day)
            if path.is_file():
                return  # nothing older can beat an edition already kept
            try:
                body = download_edition(code, day)
            except CdnUnreachable as err:
                logger.warning("%s: Freedom Forum unreachable (%s)", PLUGIN_ID, err)
                self.offline = True
                return
            if body is not None:
                try:
                    _write_atomic(path, body)
                except OSError as err:
                    logger.warning("%s: could not save %s: %s", PLUGIN_ID, path.name, err)
                    return
                self.downloaded = True
                return

    def prune(self) -> None:
        """Drop editions past ``KEEP_DAYS`` unless they are a paper's newest
        (its last good page), everything past ``LAST_GOOD_MAX_DAYS``, and temp
        files an interrupted write left behind."""
        found: list[tuple[Path, str, date]] = []
        newest: dict[str, date] = {}
        for path in self.pages.glob("*.jpg"):
            parsed = _page_day(path.name)
            if parsed is None:
                continue
            code, day = parsed
            found.append((path, code, day))
            newest[code] = max(day, newest.get(code, day))
        for path, code, day in found:
            age = (self.today - day).days
            if age > LAST_GOOD_MAX_DAYS or (age > KEEP_DAYS and day != newest[code]):
                with contextlib.suppress(OSError):
                    path.unlink()
        for tmp in self.pages.glob(".*.tmp"):
            with contextlib.suppress(OSError):
                if self.now - tmp.stat().st_mtime > TEMP_MAX_AGE_S:
                    tmp.unlink()

    def close(self) -> None:
        if self.downloaded:
            self.prune()
        if self._dirty:
            _write_json(self.state_path, self.state)


def rotation(enabled: list[str], last: str | None) -> list[str]:
    """Enabled codes in the order this render tries them: the paper after the
    one shown last, then on round the list. A paper missing its page is
    skipped by the caller, so one dead code doesn't stall the rotation."""
    start = enabled.index(last) + 1 if last in enabled else 0
    return enabled[start:] + enabled[:start]


# ----- widget contract: fetch ------------------------------------------------


def _page_size(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:  # a page that won't open still renders, just without a trim
        return 0, 0


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

    with _LOCK:
        shelf = Shelf(
            Path(ctx["data_dir"]),
            today=_edition_today(),
            now=time.time(),
            fresh=bool(ctx.get("fresh")),
        )
        order = candidates if pinned else rotation(candidates, shelf.last_shown)
        found: tuple[str, Path, date] | None = None
        for code in order:
            page = shelf.page_for(code)
            if page is not None:
                found = (code, *page)
                break
        # Only a render headed for a panel moves the rotation on. A preview
        # (the editor, the dashboards list) shows the paper the panel gets next.
        if found and not pinned and not ctx.get("preview"):
            shelf.mark_shown(found[0])
        shelf.close()

    if found is None:
        if shelf.offline:
            return {"error": "Couldn't reach Freedom Forum, and no front page is saved yet."}
        which = f"{pinned} front page" if pinned else "front page from the paper list"
        return {"error": f"No {which} in the last {LOOKBACK_DAYS} days."}
    code, path, day = found
    width, height = _page_size(path)
    trims = {p.code: p.trim for p in papers}
    return {
        "url": f"/plugins/{PLUGIN_ID}/page/{path.name}",
        "code": code,
        "date": day.isoformat(),
        "width": width,
        "height": height,
        "trim": trims.get(code, 0.0),
    }


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
