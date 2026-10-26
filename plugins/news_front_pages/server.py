"""news_front_pages, today's newspaper front pages from Freedom Forum.

Ported from the Visionect display manager's ``newspaper`` module, which ran a
daily cron to fetch each paper's PDF, rasterised page one with ImageMagick and
round-robined the enabled papers on every frame request. Here there is no cron
and no PDF tooling: ``fetch()`` answers from the pages saved in the widget's
data dir and queues what is missing for one background download worker.

Freedom Forum publishes each paper as a PDF at
``https://cdn.freedomforum.org/dfp/pdf<day-of-month>/<CODE>.pdf`` and as a
JPEG at ``.../dfp/jpg<day-of-month>/lg/<CODE>.jpg``. Three quirks shape the
code below:

* The path names only the day of the month, so each slot is reused every
  month. Freedom Forum clears a slot a couple of days after the edition, and a
  ``Last-Modified`` well before the edition date is refused anyway, so a slot
  that wasn't cleared can't pass last month's paper off as today's.
* Editions land shortly after midnight US Eastern, so "today" is the Eastern
  date whatever the server's own time zone is. A paper that isn't out yet (or
  doesn't print that day, like the Sunday WSJ) falls back to yesterday's and
  then the day before's edition, as the old cron did.
* The ``lg`` JPEG is only about 700 px wide: on a 1440 px panel it is an
  upscale, and body text can't be read. So the page comes from the PDF,
  page one drawn at ``RASTER_WIDTH_PX`` from its own vectors and fonts (as
  the old cron did with ImageMagick at 150 dpi). pypdfium2 does the drawing,
  in a child process with a wall-clock timeout and capped CPU time and
  address space, so a PDF that hangs or balloons costs that process, never
  the server; the PDF itself is capped at ``MAX_PDF_BYTES``. When there is
  no PDF, it can't be drawn, or pypdfium2 isn't installed, the JPEG stands
  in, and today's page is redrawn once the PDF can be had: asked for again
  on a backoff (``pdf_retry`` in state.json), a bounded number of times,
  and at once on a forced refresh. The PDF leaves ``JPEG_RESERVE_S`` of an
  edition's time for the JPEG, so a slow PDF can't starve the fallback.

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
* The ``papers`` setting is a list of rows (code, in-rotation switch, CSS),
  edited on the settings card. The rotation remembers the paper shown and
  the order it came from, never a position, so rows added, removed,
  reordered or switched off while it runs can't leave it pointing past the
  end. Each paper's CSS is sanitised here (``sanitize_css``) and applied by
  client.js to that paper's image only.

Layout under ``data_dir``::

    pages/<CODE>_<YYYYMMDD>.jpg   one validated page per paper and edition (the
                                  PDF drawn at panel width, or the JPEG)
    state.json                    paper shown, since when and the order it came from;
                                  download attempts and failures
    directory.json                Freedom Forum's paper list, for the editor's picker

A downloaded edition is kept until it is more than ``KEEP_DAYS`` old, a newer
one exists and it has gone unused for ``PRUNE_GRACE_S``, so a paper whose
download fails keeps showing its last good page, up to ``LAST_GOOD_MAX_DAYS``.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import html
import http.client
import importlib.util
import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, render_template, send_from_directory
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

PDF_URL = "https://cdn.freedomforum.org/dfp/pdf{day}/{code}.pdf"
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
# All of one job's requests (today, yesterday, the day before) together; a
# request in flight can overrun it by one socket timeout.
JOB_BUDGET_S = 90.0
# The PDF rasteriser runs in a child process (rasterise_pdf): page one only,
# at RASTER_WIDTH_PX, killed after RASTER_TIMEOUT_S of wall time, with its
# CPU time and address space capped where the platform allows.
RASTER_WIDTH_PX = 1440
RASTER_TIMEOUT_S = 45.0
# Below this much time left in the PDF's share of an edition's window, the
# page isn't drawn (the JPEG gets its turn instead).
RASTER_MIN_S = 5.0
# Of an edition's DOWNLOAD_BUDGET_S, what the PDF (download and drawing)
# leaves for the JPEG fallback, so a slow PDF can't use up the fallback's time.
JPEG_RESERVE_S = 20.0
# Today's page saved from the JPEG asks for its PDF again on a backoff
# (PDF_RETRY_S, doubling, up to PDF_RETRY_MAX_S), at most PDF_MAX_TRIES
# times an edition; a forced refresh asks at once whatever the count.
PDF_RETRY_S = 30 * 60
PDF_RETRY_MAX_S = 6 * 60 * 60
PDF_MAX_TRIES = 6
RASTER_CPU_S = 30
RASTER_MEMORY_BYTES = 1536 * 1024 * 1024
RASTER_MAX_ASPECT = 4.0
MAX_PDF_BYTES = 30 * 1024 * 1024
# A saved page narrower than this came from the JPEG fallback.
SHARP_MIN_WIDTH_PX = 1000
# Drawing runs inside the job's deadline; a request in flight can overrun it
# by one socket timeout.
JOB_WORST_S = JOB_BUDGET_S + HTTP_TIMEOUT_S
# The host follows a push up at most this many times in a row
# (FOLLOWUP_LIMIT in app/push.py); the hint is spread so they cover the wait.
HOST_FOLLOW_UPS = 8

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

# The ``papers`` setting's default, as in plugin.json. Two papers carry the
# printer's slug line above the masthead; their CSS moves the page up past it.
DEFAULT_PAPERS: list[dict[str, Any]] = [
    {"enabled": True, "code": "NY_NYT", "css": "transform: translateY(-3.5%)"},
    {"enabled": True, "code": "WSJ", "css": "transform: translateY(-2%)"},
    {"enabled": True, "code": "USAT", "css": ""},
    {"enabled": False, "code": "AZ_ADS", "css": ""},
]
# Rows read from the setting (the settings card allows as many).
MAX_PAPERS = 40

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
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
    """One row of the ``papers`` setting."""

    code: str
    enabled: bool = True  # in the rotation; a cell pinned to it shows it anyway
    css: str = ""  # sanitised declarations for this paper's page image


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
    code = str(raw or "").strip().upper() if isinstance(raw, (str, type(None))) else ""
    return code if CODE_RE.match(code) else None


def _flag(raw: Any, default: bool = True) -> bool:
    """A row's ``enabled``: a real boolean from the settings card, or one of
    the usual spellings from a hand-edited settings file."""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "on", "yes")
    if isinstance(raw, (int, float)):
        return raw != 0
    return False


def _parse_trim(raw: str) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return min(max(value, 0.0), MAX_TRIM_PCT) if math.isfinite(value) else None


def _trim_css(trim: float) -> str:
    """The old per-line trim (percent of the page height cut off the top) as
    the CSS that does the same: a translate percentage is of the element's
    own height."""
    return f"transform: translateY(-{trim:g}%)" if trim > 0 else ""


def legacy_text_rows(text: str) -> list[dict[str, Any]]:
    """Rows for the setting's old form, a textarea with ``CODE`` or
    ``CODE TRIM`` per line and ``#`` to switch a line off. Anything else (a
    ``# heading`` comment, a typo) is skipped rather than guessed at, and a
    repeated code keeps its first line, so the rows always save as they are."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
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
        rows.append({"enabled": enabled, "code": code, "css": _trim_css(trim)})
        if len(rows) >= MAX_PAPERS:
            break
    return rows


def legacy_rows(name: str, value: Any) -> list[dict[str, Any]] | None:
    """Host hook (app/repeater.py): the settings card shows a ``papers``
    value saved before the setting became rows as rows; saving stores them."""
    if name != "papers" or not isinstance(value, str):
        return None
    return legacy_text_rows(value)


def _rows(value: Any) -> list[dict[str, Any]]:
    """The setting's rows, whichever form it was saved in: the list the
    settings card stores, the same as a JSON string, or the old text."""
    if isinstance(value, str):
        if not value.lstrip().startswith("["):
            return legacy_text_rows(value)
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def parse_papers(value: Any) -> list[Paper]:
    """The ``papers`` setting as papers, in order. A row without a valid
    code (blank, spaces, ``../x``) is skipped, and so are keys a row
    doesn't know. A code listed twice uses its first row that is in the
    rotation (else its first row), at the first row's place."""
    papers: list[Paper] = []
    at: dict[str, int] = {}
    for row in _rows(value)[:MAX_PAPERS]:
        code = clean_code(row.get("code"))
        if code is None:
            continue
        paper = Paper(code, _flag(row.get("enabled")), sanitize_css(row.get("css")))
        if code not in at:
            at[code] = len(papers)
            papers.append(paper)
        elif paper.enabled and not papers[at[code]].enabled:
            papers[at[code]] = paper
    return papers


# ----- per-paper CSS ---------------------------------------------------------
#
# Each paper's CSS is a list of declarations applied to that paper's page
# image and nothing else: client.js sets them as the <img>'s inline style
# (``style.cssText``), so there is no selector, no rule and no stylesheet for
# them to escape from, and other papers and widgets never see them. The page
# is drawn by the headless Chromium that composes the panel's frame, so they
# are also cut down here to a small, closed vocabulary before they reach it:
#
# * Only the characters CSS declarations of that vocabulary need. Anything
#   else (``<``, ``>``, braces, quotes, backslash escapes, ``@``, ``#``,
#   ``&``, non-ASCII, control characters) drops the paper's CSS entirely,
#   since past that point there is no telling where a declaration ends.
#   Comments are removed first; an unclosed one drops it all too.
# * Each declaration is ``property: value``. The property must be in
#   ``_CSS_PROPERTIES``; every function in the value must be in
#   ``_CSS_FUNCTION_CONTEXT`` (no url(), image-set(), var(), attr(), env(),
#   calc(), expression(), skew()), at most one level deep; every number must
#   carry a unit its context allows and stay inside that context's bound
#   (``_css_limits``: moves within half the page, sizes within twice the
#   cell); every scale (the scale property and each of the transform's
#   scale functions, at its largest factor, rotations notwithstanding)
#   multiplies into one bound of ``CSS_MAX_SCALE``.
#   A declaration that fails is dropped on its own; of two for the same
#   property, the last one counts, as in CSS.
# * No position, z-index or anything else that could put the page outside
#   its cell; nothing that loads anything. The ``translate`` property is
#   client.js's, to put the page on whole pixels; ``transform: translate()``
#   does what a paper needs.

CSS_MAX_CHARS = 1000
CSS_MAX_DECLARATIONS = 12
CSS_MAX_VALUE_CHARS = 200
CSS_MAX_FUNCTIONS = 6
# How far the page may be enlarged: every scale in its CSS multiplied
# together at its largest factor (see _css_value).
CSS_MAX_SCALE = 3.0

_CSS_PROPERTIES = frozenset(
    {
        "filter",
        "opacity",
        "transform",
        "transform-origin",
        "scale",
        "rotate",
        "object-fit",
        "object-position",
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "width",
        "height",
        "max-width",
        "max-height",
        "min-width",
        "min-height",
        "clip-path",
        "image-rendering",
    }
)
# Every number is read in a context, which bounds it: the function it sits
# in, else its property. A number with no context (``transform: 5px``) is
# refused. Skew isn't offered: near 90deg it stretches the page without
# bound, and a newspaper page has no use for it.
_CSS_FUNCTION_CONTEXT: dict[str, str] = {
    "translate": "move",
    "translatex": "move",
    "translatey": "move",
    "scale": "scale",
    "scalex": "scale",
    "scaley": "scale",
    "rotate": "angle",
    "hue-rotate": "angle",
    "brightness": "tone",
    "contrast": "tone",
    "grayscale": "tone",
    "invert": "tone",
    "opacity": "tone",
    "saturate": "tone",
    "sepia": "tone",
    "inset": "place",
}
_CSS_PROPERTY_CONTEXT: dict[str, str] = {
    "scale": "scale",
    "rotate": "angle",
    "opacity": "tone",
    "margin": "move",
    "margin-top": "move",
    "margin-right": "move",
    "margin-bottom": "move",
    "margin-left": "move",
    "width": "size",
    "height": "size",
    "max-width": "size",
    "max-height": "size",
    "min-width": "size",
    "min-height": "size",
    "object-position": "place",
    "transform-origin": "place",
}
_CSS_VIEWPORT_UNITS = ("vw", "vh", "vmin", "vmax")


def _css_limits(context: str) -> Mapping[str, float]:
    """The largest magnitude a number may have in ``context``, per unit
    ("" is unitless). A unit not listed is refused. Moves stay within half
    the page, sizes within twice the cell; a bare 0 is the only unitless
    length."""
    if context == "move":
        return {"": 0, "%": 50, "px": 1000, "em": 50, "rem": 50} | dict.fromkeys(
            _CSS_VIEWPORT_UNITS, 50
        )
    if context == "size":
        return {"": 0, "%": 200, "px": 3000, "em": 100, "rem": 100} | dict.fromkeys(
            _CSS_VIEWPORT_UNITS, 200
        )
    if context == "place":
        return {"": 0, "%": 100, "px": 3000, "em": 100, "rem": 100} | dict.fromkeys(
            _CSS_VIEWPORT_UNITS, 100
        )
    if context == "angle":
        return {"": 0, "deg": 360, "turn": 1, "rad": 6.3, "grad": 400}
    if context == "tone":
        return {"": 4, "%": 400}
    if context == "scale":
        return {"": CSS_MAX_SCALE, "%": 100 * CSS_MAX_SCALE}
    return {}


_CSS_CHARS_RE = re.compile(r"^[A-Za-z0-9 \t\r\n.,:;%()+\-!/*]*$")
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
# Matched against a stripped chunk, so the value needs no lazy trailing
# whitespace match (which goes quadratic on a long run of spaces).
_CSS_DECLARATION_RE = re.compile(r"^([A-Za-z-]+)\s*:\s*(.+)$", re.S)
_CSS_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<func>[A-Za-z][A-Za-z0-9-]*)\(
    | (?P<close>\))
    | (?P<comma>,)
    | (?P<num>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?P<unit>%|[A-Za-z]+)?
    | (?P<ident>-?[A-Za-z][A-Za-z0-9-]*)
    """,
    re.X,
)


def _scale_factor(name: str, factors: list[float]) -> float | None:
    """How much one scale (a ``scale*()`` function's arguments, or the
    scale property's values) can enlarge the page: its largest factor, and
    never less than 1, since shrinking one axis doesn't make up for
    growing another. None for a third (z) value or no value at all."""
    expected = (1,) if name in ("scalex", "scaley") else (1, 2)
    if len(factors) not in expected:
        return None
    return max(1.0, *factors)


def _css_value(prop: str, value: str) -> float | None:
    """Check one declaration's value; how much it can enlarge the page (1
    for not at all) when it's allowed, else None. The bound is
    deliberately coarse: every scale in the value multiplies in at its
    largest factor, whatever lies between (a rotate turns one axis's scale
    into the other's, so per-axis sums can't be trusted)."""
    if len(value) > CSS_MAX_VALUE_CHARS:
        return None
    pos, functions, growth = 0, 0, 1.0
    current: str | None = None  # the function we're inside, if any
    factors: list[float] = []  # scale values seen in the current scope
    while pos < len(value):
        match = _CSS_TOKEN_RE.match(value, pos)
        if match is None:
            return None
        pos = match.end()
        if match.group("func"):
            name = match.group("func").lower()
            functions += 1
            if current is not None or name not in _CSS_FUNCTION_CONTEXT:
                return None
            if functions > CSS_MAX_FUNCTIONS:
                return None
            current, factors = name, []
        elif match.group("close"):
            if current is None:
                return None
            if _CSS_FUNCTION_CONTEXT[current] == "scale":
                factor = _scale_factor(current, factors)
                if factor is None:
                    return None
                growth *= factor
            current, factors = None, []
        elif match.group("num"):
            context = (
                _CSS_FUNCTION_CONTEXT[current]
                if current is not None
                else _CSS_PROPERTY_CONTEXT.get(prop)
            )
            unit = (match.group("unit") or "").lower()
            limit = _css_limits(context or "").get(unit)
            try:
                number = float(match.group("num"))
            except ValueError:
                return None
            if limit is None or not math.isfinite(number) or abs(number) > limit:
                return None
            if context == "scale":
                factors.append(abs(number) / 100 if unit == "%" else abs(number))
    if current is not None:
        return None
    if prop == "scale" and factors:
        factor = _scale_factor("scale", factors)
        if factor is None:
            return None
        growth *= factor
    return growth if growth <= CSS_MAX_SCALE else None


def _without_important(value: str) -> str:
    head, bang, tail = value.rpartition("!")
    return head.rstrip() if bang and tail.strip().lower() == "important" else value


def sanitize_css(raw: Any) -> str:
    """``raw`` cut down to the declarations it's safe to give one paper's
    page image, re-serialised as ``prop: value; prop: value``; "" when
    nothing survives. See the notes above for what survives."""
    if not isinstance(raw, str) or len(raw) > CSS_MAX_CHARS:
        return ""
    return _sanitize_css(raw)


@functools.lru_cache(maxsize=256)
def _sanitize_css(raw: str) -> str:
    """sanitize_css for a string, remembered: every render reads every
    paper's CSS, and it only changes when the settings do."""
    if not _CSS_CHARS_RE.match(raw):
        return ""
    text = _CSS_COMMENT_RE.sub(" ", raw)
    if "/*" in text or "*/" in text:
        return ""
    # The last declaration of a property is the one that applies, as in CSS.
    kept: dict[str, tuple[str, float]] = {}
    for chunk in text.split(";"):
        match = _CSS_DECLARATION_RE.match(chunk.strip())
        if match is None:
            continue
        prop = match.group(1).lower()
        value = _without_important(match.group(2))
        scale = _css_value(prop, value) if prop in _CSS_PROPERTIES and value else None
        if scale is not None:
            kept.pop(prop, None)
            kept[prop] = (" ".join(value.split()), scale)
    # The scale property and the transform's scale functions multiply: past
    # the bound together, the scale property goes.
    if (
        "scale" in kept
        and "transform" in kept
        and kept["scale"][1] * kept["transform"][1] > CSS_MAX_SCALE
    ):
        del kept["scale"]
    return "; ".join(
        f"{prop}: {value}" for prop, (value, _) in list(kept.items())[:CSS_MAX_DECLARATIONS]
    )


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


def _short(err: object) -> str:
    """An error as one short line for the log: what Freedom Forum or the
    network said. Nothing here carries a secret (the widget has none)."""
    text = " ".join(str(err).split()) or type(err).__name__
    return text if len(text) <= 120 else text[:119] + "\u2026"


def _kb(size: int) -> str:
    return (
        f"{size / 1024 / 1024:.1f} MB"
        if size >= 1024 * 1024
        else f"{max(1, round(size / 1024))} KB"
    )


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


def _read_capped(resp: Any, deadline: float, cap: int = MAX_IMAGE_BYTES) -> bytes:
    """The body, stopping one byte past ``cap`` and at ``deadline``.
    ``read1`` returns whatever one socket read brings, where ``read(n)`` would
    wait for all n bytes, so the deadline is checked at least once per
    ``HTTP_TIMEOUT_S`` however slowly the bytes arrive."""
    read = getattr(resp, "read1", None) or resp.read
    chunks: list[bytes] = []
    total = 0
    while total <= cap:
        if _clock() >= deadline:
            raise CdnUnreachable("download too slow")
        chunk = read(min(READ_CHUNK_BYTES, cap + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _get(url: str, deadline: float, cap: int, accept: str) -> tuple[bytes, str | None] | None:
    """The body (at most ``cap`` + 1 bytes) and its Last-Modified, or None on
    a 404. Raises :class:`CdnUnreachable` when the CDN fails or ``deadline``
    (on ``_clock``; at most ``DOWNLOAD_BUDGET_S`` away) passes."""
    deadline = min(deadline, _clock() + DOWNLOAD_BUDGET_S)
    if deadline <= _clock():
        raise CdnUnreachable("out of time")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
            body = _read_capped(resp, deadline, cap)
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
    return body, last_modified


# Runs in a child process (``python -I -c``), so a PDF that hangs, crashes
# or balloons the rasteriser takes down that process, never the server. It
# caps its own address space and CPU time first (where ``resource`` exists:
# Linux and macOS), reads the PDF from stdin and writes page one as a JPEG,
# ``argv[1]`` pixels wide, to stdout. Exit 3 is a PDF it won't draw.
_RASTER_CHILD = r"""
import io
import sys

width, memory, cpu, max_aspect = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
try:
    import resource
except ImportError:
    resource = None
if resource is not None:
    for name, value in (("RLIMIT_AS", memory), ("RLIMIT_CPU", cpu)):
        limit = getattr(resource, name, None)
        if limit is not None:
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass
import pypdfium2
document = pypdfium2.PdfDocument(sys.stdin.buffer.read())
if len(document) < 1:
    sys.exit(3)
page = document[0]
w, h = page.get_size()
if not (w > 0 and h > 0) or h / w > max_aspect or w / h > max_aspect:
    sys.exit(3)
image = page.render(scale=width / w).to_pil().convert("RGB")
out = io.BytesIO()
image.save(out, "JPEG", quality=90, subsampling=0, optimize=True)
sys.stdout.buffer.write(out.getvalue())
"""


@functools.cache
def rasteriser_available() -> bool:
    """Whether pypdfium2 is installed (a core dependency, but an older
    install may not have it yet). Looked up, not imported: only the child
    process ever loads it."""
    return importlib.util.find_spec("pypdfium2") is not None


def rasterise_pdf(
    body: bytes, width: int = RASTER_WIDTH_PX, timeout: float = RASTER_TIMEOUT_S
) -> bytes | None:
    """Page one of the PDF ``body`` as a JPEG ``width`` pixels wide, drawn
    at that width from the PDF's own vectors and fonts (never an upscaled
    bitmap), or None when it can't be drawn: no rasteriser, a broken or odd
    PDF, the child over its time or memory. Blocks for at most ``timeout``
    (and never more than ``RASTER_TIMEOUT_S``); the worker thread is the
    only caller."""
    timeout = min(timeout, RASTER_TIMEOUT_S)
    if not rasteriser_available() or timeout <= 0:
        return None
    command = [
        sys.executable,
        "-I",
        "-c",
        _RASTER_CHILD,
        str(width),
        str(RASTER_MEMORY_BYTES),
        str(RASTER_CPU_S),
        str(RASTER_MAX_ASPECT),
    ]
    try:
        done = subprocess.run(
            command, input=body, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        logger.warning("%s: PDF rasteriser timed out", PLUGIN_ID)
        return None
    except (OSError, ValueError) as err:
        logger.warning("%s: PDF rasteriser didn't start: %s", PLUGIN_ID, err)
        return None
    if done.returncode != 0:
        detail = done.stderr[-300:].decode("utf-8", "replace").strip()
        logger.warning("%s: PDF rasteriser failed (%s) %s", PLUGIN_ID, done.returncode, detail)
        return None
    size = page_size(done.stdout)
    if size is None or abs(size[0] - width) > 2:
        logger.warning("%s: PDF rasteriser returned no usable page", PLUGIN_ID)
        return None
    return done.stdout


@dataclass
class Trace:
    """What one ``download_edition`` call did, for the worker's log: where
    the page it returned came from, and why the PDF or the JPEG wasn't used
    when it wasn't."""

    source: str = ""  # "PDF" or "JPG"
    pdf: str = ""
    jpg: str = ""
    no_rasteriser: bool = False  # pypdfium2 missing: the JPEG is all there is


def _pdf_edition(
    code: str, day: date, deadline: float, *, quiet: bool, trace: Trace
) -> bytes | None:
    """The ``code`` front page for ``day`` drawn from its PDF, or None when
    there's no PDF or it can't be drawn. With ``quiet`` a CDN failure is a
    None too, and the JPEG fallback after it decides whether Freedom Forum
    is reachable; without, it raises :class:`CdnUnreachable`."""
    try:
        got = _get(
            PDF_URL.format(day=day.day, code=code), deadline, MAX_PDF_BYTES, "application/pdf"
        )
    except CdnUnreachable as err:
        trace.pdf = f"unreachable ({_short(err)})"
        if not quiet:
            raise
        logger.info("%s: %s PDF for %s unavailable (%s)", PLUGIN_ID, code, day, err)
        return None
    if got is None:
        trace.pdf = "not out yet"
        return None
    body, last_modified = got
    if len(body) > MAX_PDF_BYTES or _slot_is_stale(last_modified, day):
        trace.pdf = "refused (too big, or last month's file)"
        logger.info("%s: %s PDF for %s refused (oversized or stale slot)", PLUGIN_ID, code, day)
        return None
    if not body.startswith(b"%PDF"):
        trace.pdf = "not a PDF"
        logger.info("%s: %s PDF for %s is not a PDF", PLUGIN_ID, code, day)
        return None
    left = deadline - _clock()
    if left < RASTER_MIN_S:
        trace.pdf = "no time left to draw it"
        logger.info("%s: no time left to draw the %s PDF for %s", PLUGIN_ID, code, day)
        return None
    page = rasterise_pdf(body, timeout=left)
    if page is None:
        trace.pdf = f"didn't draw ({_kb(len(body))})"
    else:
        trace.source = "PDF"
    return page


def download_edition(
    code: str,
    day: date,
    deadline: float | None = None,
    *,
    pdf_only: bool = False,
    trace: Trace | None = None,
) -> bytes | None:
    """The ``code`` front page for ``day``, validated, or None when that
    edition isn't there. The PDF comes first, drawn at panel width; the
    ``lg`` JPEG (about 700 px wide) is the fallback, skipped when
    ``pdf_only``. The two share one ``DOWNLOAD_BUDGET_S`` window (and
    ``deadline``, on ``_clock``), of which the PDF, download and drawing
    together, leaves ``JPEG_RESERVE_S`` for the JPEG. Raises
    :class:`CdnUnreachable` when the CDN fails or the time runs out.
    ``trace``, when given, is filled in for the worker's log."""
    trace = trace if trace is not None else Trace()
    deadline = min(deadline if deadline is not None else math.inf, _clock() + DOWNLOAD_BUDGET_S)
    if deadline <= _clock():
        raise CdnUnreachable("out of time")
    if not rasteriser_available():
        trace.no_rasteriser = True
        if pdf_only:
            return None
    elif pdf_only:
        return _pdf_edition(code, day, deadline, quiet=False, trace=trace)
    elif deadline - JPEG_RESERVE_S > _clock():
        sharp = _pdf_edition(code, day, deadline - JPEG_RESERVE_S, quiet=True, trace=trace)
        if sharp is not None:
            return sharp
    else:
        trace.pdf = "skipped, no time left"
    got = _get(
        PAGE_URL.format(day=day.day, code=code),
        deadline,
        MAX_IMAGE_BYTES,
        "image/jpeg,image/*;q=0.8",
    )
    if got is None:
        trace.jpg = "not out yet"
        return None
    body, last_modified = got
    if len(body) > MAX_IMAGE_BYTES or _slot_is_stale(last_modified, day):
        trace.jpg = "refused (too big, or last month's file)"
        logger.info("%s: %s for %s refused (oversized or stale slot)", PLUGIN_ID, code, day)
        return None
    if page_size(body) is None:
        trace.jpg = "not an image"
        logger.info("%s: %s for %s is not an image", PLUGIN_ID, code, day)
        return None
    trace.source = "JPG"
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


def _saved_width(path: Path) -> int | None:
    """A saved page's width from its header alone (no decode), or None."""
    try:
        with Image.open(path) as img:
            return int(img.size[0])
    except Exception:  # missing, half-written, not an image
        return None


def _pdf_retry(state: dict[str, Any], code: str, day: date) -> tuple[int, float]:
    """How many times the PDF of ``code``'s ``day`` edition has been asked
    for and failed, and when it may be asked for again (wall time)."""
    entry = _section(state, "pdf_retry").get(code)
    if not isinstance(entry, dict) or entry.get("day") != day.isoformat():
        return 0, 0.0
    tries, after = entry.get("tries"), entry.get("after")
    if isinstance(tries, bool) or not isinstance(tries, int) or not isinstance(after, (int, float)):
        return 0, 0.0
    return tries, float(after)


def _wants_sharper(
    code: str, day: date, width: int | None, state: dict[str, Any], now: float, force: bool
) -> bool:
    """Whether the saved ``day`` page of ``code``, ``width`` pixels wide, is
    the blurry JPEG fallback and its PDF may be asked for now. A PDF that
    wasn't there (not out yet, say) or didn't draw is asked for again on a
    backoff, up to ``PDF_MAX_TRIES`` times an edition; a forced refresh asks
    at once. Pages saved before the PDF path existed are of this kind too,
    so today's are redrawn; older ones age out."""
    if width is None or width >= SHARP_MIN_WIDTH_PX or not rasteriser_available():
        return False
    if force:
        return True
    tries, after = _pdf_retry(state, code, day)
    return tries < PDF_MAX_TRIES and now >= after


# ----- rotation --------------------------------------------------------------


def rotation(enabled: list[str], last: str | None, previous: list[str] | None = None) -> list[str]:
    """Enabled codes from the one after ``last`` on round the list, ``last``
    at the end. The rotation remembers papers, not a position, so an edited
    list can't leave it pointing past the end. When ``last`` has left the
    list (removed, switched off, its code edited), the rotation resumes at
    the first paper still listed that came after it in ``previous``, the
    order it was shown from, rather than starting over."""
    if last in enabled:
        start = enabled.index(last) + 1
    elif previous and last in previous:
        at = previous.index(last)
        after = previous[at + 1 :] + previous[:at]
        resume = next((code for code in after if code in enabled), None)
        start = enabled.index(resume) if resume is not None else 0
    else:
        start = 0
    return enabled[start:] + enabled[:start]


def _codes(raw: Any) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    return [code for code in raw if isinstance(code, str)][:MAX_PAPERS]


def plan(enabled: list[str], state: dict[str, Any], now: float) -> tuple[list[str], str | None]:
    """The codes this render tries, in order, and the paper being held, if any.
    Inside ``ROTATION_HOLD_S`` of the last move the current paper comes first;
    after it, the next one does. The caller skips a paper with no page, so one
    dead code doesn't stall the rotation."""
    current = state.get("current")
    current = current if isinstance(current, str) else None
    since = state.get("since")
    order = rotation(enabled, current, _codes(state.get("order")))
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
    active_since: float = 0.0  # on _clock
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
            if not first:
                return  # a later render queues it again
            self.queue.pop()  # the job that would run last makes room
        if first:
            self.queue.insert(0, job)
        else:
            self.queue.append(job)


# Keyed by this file's revision: a reload after an edit gets a fresh registry
# rather than live runs of an older ``_Run`` it may not be able to drive. An
# older revision's worker finishes its own queue and exits; writes from both
# go through the one shared state lock.
_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
_RUNS: dict[str, _Run] = _shared(f"runs.{_REVISION}", dict)


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


def _line(data_dir: Path) -> tuple[list[str], float | None]:
    """Codes queued or being downloaded, in the order they'll be done, and
    the running job's remaining budget (None when none is running yet)."""
    with _RUNS_LOCK:
        run = _RUNS.get(str(data_dir))
        if run is None or not run.alive():
            return [], None
        left = None if run.active is None else JOB_WORST_S - (_clock() - run.active_since)
        return run.line(), left


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
                run.active_since = _clock()
                waiting = len(run.queue)
            _report(job, waiting)
            _refresh(run.data_dir, job)
            with _RUNS_LOCK:
                run.active = None
    except Exception as err:
        logger.exception("%s: the download worker stopped", PLUGIN_ID)
        _log_stop(run.data_dir, err)
    finally:
        with _RUNS_LOCK:
            run.active = None
            run.done = True
        if jobs is not None:
            with contextlib.suppress(Exception):
                jobs.finish(PLUGIN_ID, "download")


def _refresh(data_dir: Path, job: _Job) -> None:
    """One job: look at the state again (the render that queued it read it
    before earlier jobs landed), download outside the lock, save under it.
    Today's page already saved as the blurry JPEG fallback is redrawn from
    the PDF when that may now work (``_wants_sharper``). What it asked for
    and got goes to the log the admin page shows (``state["log"]``)."""
    pages = data_dir / PAGES_DIR
    state_path = data_dir / STATE_FILE
    with _LOCK:
        state = _read_json(state_path)
        if not job.force and _recently_checked(state, job.code, _wall()):
            return
        have = newest_page(pages, job.code, job.today)
    current = have is not None and have.day >= job.today
    sharpen = (
        current
        and have is not None
        and _wants_sharper(job.code, have.day, have.width, state, _wall(), job.force)
    )
    if current and not sharpen:
        return
    notes: list[tuple[float, str]] = []

    def note(text: str) -> None:
        notes.append((_wall(), text))

    if sharpen and have is not None:
        tries = _pdf_retry(state, job.code, have.day)[0]
        which = f"try {tries + 1} of {PDF_MAX_TRIES}" if tries < PDF_MAX_TRIES else "a refresh"
        note(f"{have.day}: saved from the JPG, asking for the PDF again ({which})")
    saved: tuple[date, bytes] | None = None
    saved_how: tuple[Trace, float] | None = None
    failure = "missing"
    deadline = _clock() + JOB_BUDGET_S
    for back in range(LOOKBACK_DAYS):
        day = job.today - timedelta(days=back)
        if have is not None and (day < have.day or (day == have.day and not sharpen)):
            break  # nothing older can beat an edition already kept
        trace = Trace()
        started = _clock()
        try:
            body = download_edition(job.code, day, deadline, pdf_only=sharpen, trace=trace)
        except Exception as err:  # CdnUnreachable, or the host refusing the egress
            logger.warning("%s: Freedom Forum unreachable (%s)", PLUGIN_ID, err)
            note(f"{day}: Freedom Forum unreachable ({_short(err)})")
            failure = "unreachable"
            break
        if body is not None:
            saved = (day, body)
            saved_how = (trace, max(0.0, _clock() - started))
            break
        note(f"{day}: {_not_found(trace, pdf_only=sharpen)}")
    size = page_size(saved[1]) if saved is not None else None
    # The edition left on its JPEG by this job, whose PDF is asked for again
    # later: a JPEG saved in its place, or a redraw that came to nothing
    # (no PDF yet, one that didn't draw, an outage).
    blurry_day: date | None = None
    sharp_day: date | None = None
    if saved is not None and rasteriser_available():
        if size is not None and size[0] < SHARP_MIN_WIDTH_PX:
            blurry_day = saved[0]
        else:
            sharp_day = saved[0]
    elif sharpen and saved is None and have is not None:
        blurry_day = have.day
    now = _wall()
    with _LOCK:
        if saved is not None:
            try:
                _write_atomic(edition_path(pages, job.code, saved[0]), saved[1])
            except OSError as err:
                logger.warning("%s: could not save %s: %s", PLUGIN_ID, job.code, err)
                note(f"{saved[0]}: downloaded, but couldn't be saved ({_short(err)})")
                saved = None
                blurry_day = sharp_day = None
            else:
                prune(pages, job.today, now)
                note(f"{saved[0]}: {_saved_line(saved[1], size, saved_how, redraw=sharpen)}")
        if saved is None and not sharpen:
            if have is not None:
                note(f"keeping the {have.day} page")
            elif failure == "missing":
                note(f"no page in the last {LOOKBACK_DAYS} days")
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
        oldest = (job.today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        retry = {
            code: entry
            for code, entry in _section(state, "pdf_retry").items()
            if isinstance(entry, dict)
            and isinstance(entry.get("day"), str)
            and entry["day"] >= oldest
        }
        if blurry_day is not None:
            tries = _pdf_retry(state, job.code, blurry_day)[0] + 1
            wait = min(PDF_RETRY_S * 2 ** min(tries - 1, 16), PDF_RETRY_MAX_S)
            retry[job.code] = {"day": blurry_day.isoformat(), "tries": tries, "after": now + wait}
            if tries < PDF_MAX_TRIES:
                note(f"{blurry_day}: PDF asked for again in {_duration(wait)}")
            else:
                note(f"{blurry_day}: no more PDF tries for this edition, keeping the JPG")
        elif sharp_day is not None:
            retry.pop(job.code, None)
        state["pdf_retry"] = retry
        state.pop("jpeg_only", None)  # this file's earlier, give-up-for-the-day record
        _append_log(state, job.code, notes)
        _write_json(state_path, state)


# ----- the worker's log --------------------------------------------------------
#
# The admin page shows what the worker did, newest last: per paper, the
# editions it asked for, where each saved page came from (the PDF drawn at
# panel width, or the JPEG fallback), its size and how long it took, the PDF
# retries and the failures. The lines live in state.json, at most LOG_LINES
# of them. They hold paper codes, dates and what Freedom Forum or the network
# answered; the widget has no secrets to leak into them.

LOG_LINES = 150


def _not_found(trace: Trace, *, pdf_only: bool) -> str:
    if pdf_only:
        return f"PDF {trace.pdf or 'not drawn'}"
    if trace.pdf in ("", "not out yet") and trace.jpg == "not out yet":
        return "not out yet"
    parts = [f"PDF {trace.pdf}" if trace.pdf else "", f"JPG {trace.jpg}" if trace.jpg else ""]
    return ", ".join(part for part in parts if part) or "not found"


def _saved_line(
    body: bytes, size: tuple[int, int] | None, how: tuple[Trace, float] | None, *, redraw: bool
) -> str:
    trace, took = how if how is not None else (Trace(), 0.0)
    source = trace.source or ("PDF" if size and size[0] >= SHARP_MIN_WIDTH_PX else "JPG")
    dims = f"{size[0]}\u00d7{size[1]} px, " if size else ""
    line = f"{'redrawn from the PDF' if redraw else source}, {dims}{_kb(len(body))} in {took:.1f} s"
    if source == "JPG" and trace.pdf:
        line += f" (PDF {trace.pdf})"
    elif source == "JPG" and trace.no_rasteriser:
        line += " (no PDF drawing here: pypdfium2 isn't installed)"
    return line


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes / 60:g} h" if minutes % 60 == 0 else f"{minutes // 60} h {minutes % 60} min"


def _log_entries(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [
        entry
        for entry in raw
        if isinstance(entry, dict)
        and isinstance(entry.get("at"), (int, float))
        and isinstance(entry.get("code"), str)
        and isinstance(entry.get("text"), str)
    ]


def _append_log(state: dict[str, Any], code: str, notes: list[tuple[float, str]]) -> None:
    """Add ``notes`` to the state's log for ``code``, keeping the newest
    ``LOG_LINES``. Callers hold ``_LOCK``."""
    if not notes:
        return
    entries = _log_entries(state.get("log"))
    entries += [{"at": at, "code": code, "text": text} for at, text in notes]
    state["log"] = entries[-LOG_LINES:]


def _log_stop(data_dir: Path, err: BaseException) -> None:
    """Note in the log that the worker stopped on an error (a bug)."""
    with contextlib.suppress(Exception), _LOCK:
        state = _read_json(data_dir / STATE_FILE)
        _append_log(state, "", [(_wall(), f"The download worker stopped: {type(err).__name__}")])
        _write_json(data_dir / STATE_FILE, state)


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


def _wait_hint(position: int, active_left: float | None) -> dict[str, Any]:
    """Refresh hints for a render waiting on the job at ``position`` in the
    worker's line (0 = first; the running job when ``active_left`` is set).
    The host follows a push up only ``HOST_FOLLOW_UPS`` times, so the hint is
    the worst-case wait (the running job's remaining budget plus a full
    budget per job before ours) spread over them, never under
    ``LIVE_REFRESH_S`` nor over the host's ceiling."""
    if active_left is None:
        worst = (position + 1) * JOB_WORST_S
    else:
        worst = max(0.0, active_left) + position * JOB_WORST_S
    seconds = min(LIVE_REFRESH_MAX_S, max(LIVE_REFRESH_S, math.ceil(worst / HOST_FOLLOW_UPS)))
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
            state["order"] = list(candidates)  # where to resume if the list changes
            _write_json(state_path, state)
        due = [
            code
            for code in candidates
            if (force or not _recently_checked(state, code, now))
            and (
                not edition_path(pages, code, today).is_file()
                or _wants_sharper(
                    code,
                    today,
                    _saved_width(edition_path(pages, code, today)),
                    state,
                    now,
                    force,
                )
            )
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
        return {"error": f"'{pinned_raw[:60]}' isn't a Freedom Forum paper code."}
    candidates = [pinned] if pinned else [p.code for p in papers if p.enabled]
    if not candidates:
        where = "under Widgets → Admin pages → Newspapers"
        if papers:
            return {"error": f"Every paper in the list is switched off. Turn one on {where}."}
        return {"error": f"No papers to show. Add paper codes {where}."}

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
    # One look at the queue decides the answer. A job this render queued that
    # is already off it finished after the pages were read: read them again,
    # rather than answer from the snapshot taken before it landed.
    line, active_left = _line(data_dir)
    if any(code not in line for code in picked.due):
        picked = select() or picked

    if picked.chosen is None:
        waiting = [line.index(code) for code in candidates if code in line]
        if waiting:
            return {"pending": True, **_wait_hint(min(waiting), active_left)}
        return _missing_error(candidates, picked.state, pinned)

    code, page = picked.chosen
    styles = {p.code: p.css for p in papers}
    payload: dict[str, Any] = {
        # The width tells a JPEG-fallback page from its sharp redraw, which
        # replaces it under the same name: a browser holding the first (the
        # route lets it keep a page a day) fetches the second.
        "url": f"/plugins/{PLUGIN_ID}/page/{page.path.name}?w={page.width}",
        "code": code,
        "date": page.day.isoformat(),
        "width": page.width,
        "height": page.height,
        # This paper's own CSS, sanitised (see sanitize_css); a pinned paper
        # uses its row's CSS too, whether or not the row is in the rotation.
        "css": styles.get(code, ""),
    }
    _LAST_CLAIMED[key] = (now, payload)
    if code in line:
        # Only the shown paper's download changes this frame.
        return {**payload, **_wait_hint(line.index(code), active_left)}
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
    papers = parse_papers(_settings().get("papers", DEFAULT_PAPERS))
    listed = [p.code for p in papers]
    off = {p.code for p in papers if not p.enabled}
    rest = sorted((c for c in names if c not in listed), key=lambda c: names[c].casefold())
    for code in [*listed, *rest]:
        label = f"{names[code]} ({code})" if code in names else code
        if code in off:
            label += ", not in the rotation"
        rows.append({"value": code, "label": label})
    return rows


# ----- the admin page ----------------------------------------------------------


def _timezone() -> tzinfo | None:
    """The app's time zone (Settings, else the host's); None is the host's."""
    try:
        from app.tz_resolve import app_timezone

        return app_timezone()
    except Exception:
        return None


def downloads_view(data_dir: Path, tz: tzinfo | None) -> dict[str, Any]:
    """The admin page's Downloads card: what the worker is doing now, the
    PDF retries it has waiting and the papers it has no page for (one
    sentence each), and its log, oldest line first."""

    def when(ts: float, fmt: str) -> str:
        try:
            return datetime.fromtimestamp(ts, tz).strftime(fmt)
        except (OverflowError, OSError, ValueError):  # a hand-edited state file
            return "?"

    state = _read_json(data_dir / STATE_FILE)
    entries = _log_entries(state.get("log"))
    status: list[str] = []
    line, _ = _line(data_dir)
    if line:
        more = f", {len(line) - 1} more waiting" if len(line) > 1 else ""
        status.append(f"Downloading {line[0]} now{more}.")
    elif entries:
        status.append(f"Last activity {when(entries[-1]['at'], '%b %d, %H:%M')}.")
    else:
        status.append("Nothing downloaded yet: a render that needs a page asks for it.")
    retries, spent = [], []
    for code, entry in sorted(_section(state, "pdf_retry").items()):
        if not isinstance(entry, dict):
            continue
        tries, after = entry.get("tries"), entry.get("after")
        if isinstance(tries, bool) or not isinstance(tries, int):
            continue
        if not isinstance(after, (int, float)):
            continue
        if tries < PDF_MAX_TRIES:
            retries.append(
                f"{code} (try {tries + 1} of {PDF_MAX_TRIES}, after {when(after, '%H:%M')})"
            )
        else:
            spent.append(code)
    if retries:
        status.append(f"Waiting to ask for the PDF again: {', '.join(retries)}.")
    if spent:
        status.append(f"Out of PDF tries, keeping the JPG: {', '.join(spent)}.")
    reasons = {
        "unreachable": "Freedom Forum unreachable",
        "missing": f"none in the last {LOOKBACK_DAYS} days",
    }
    failed = [
        f"{code} ({reasons.get(str(why), str(why))})"
        for code, why in sorted(_section(state, "failed").items())
    ]
    if failed:
        status.append(f"No page yet for {', '.join(failed)}.")
    width = max((len(e["code"]) for e in entries), default=0)
    log = [
        f"{when(e['at'], '%b %d %H:%M:%S')}  {e['code'] or '-':<{width}}  {e['text']}"
        for e in entries
    ]
    return {"status": status, "log": log}


# ----- page images -----------------------------------------------------------


# The widget's <img> points here and the panel render fetches it over loopback
# with no session (see app/auth.py). Read-only: it serves files this module
# wrote, by a name that has to match the page-file pattern.
RENDER_SAFE_ENDPOINTS: tuple[str, ...] = (f"{PLUGIN_ID}.serve_page",)


# Settings and controls belong together under Widgets > Admin pages > Newspapers.
ADMIN_SETTINGS = True


def blueprint() -> Blueprint:
    bp = Blueprint(PLUGIN_ID, __name__, template_folder="templates")

    @bp.get("/")
    def index() -> str:
        from app.plugin_admin import settings_section

        plugin = _plugin()
        if plugin is None:
            abort(404)
        papers = next(
            (f for f in plugin.manifest.get("settings", []) if f.get("name") == "papers"), {}
        )
        section = settings_section(
            plugin,
            title="Papers",
            blurb=papers.get("help", ""),
            icon="newspaper",
            repeater_layout="list",
        )
        return render_template(
            "news_front_pages/index.html",
            section=section,
            downloads=downloads_view(Path(plugin.data_dir), _timezone()),
        )

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
