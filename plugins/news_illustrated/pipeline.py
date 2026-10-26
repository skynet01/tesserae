"""The daily edition: one illustrated story per source, made in the background.

For each source: the feed's newest item, the article text (or the feed's
description), a Groq summary, then an illustration from the provider
cascade. A story is replaced only as a whole. When the feed cannot be read,
or no provider paints a picture, the previous story stays, title, summary
and picture together, so a new headline never sits on an unrelated image.

Making an edition takes minutes and costs money, so it never runs inside a
render. ``fetch()`` asks :func:`due` (cheap: two small JSON reads) and, on a
settled render, :meth:`Runner.start` hands the work to a daemon thread and
returns at once; the render shows the stories already on disk. The widget's
page starts a run by hand, and its progress shows in the job tray.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from . import feeds, painters, store, summarise

try:
    from app import jobs
except ImportError:  # a Tesserae without the job tray
    jobs = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLUGIN_ID = "news_illustrated"
LABEL = "News, AI Illustrated"
DEFAULT_SOURCES = (
    "Tech | https://hnrss.org/frontpage?points=100\n"
    "Business | https://feeds.bbci.co.uk/news/business/rss.xml\n"
    "Global | https://feeds.bbci.co.uk/news/world/rss.xml"
)
DEFAULT_RUN_AT = "06:05"
DEFAULT_WORDS = 70
# A day's edition is attempted at most this often, this far apart, when a
# run makes no story at all (the network was down at run time, say).
MAX_ATTEMPTS = 2
RETRY_AFTER_S = 2 * 3600
LOG_LINES = 200


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    label: str
    feed: str

    @property
    def key(self) -> str:
        """Stable id: the label as a slug plus a hash of the feed, so a story
        survives reordering but not pointing the label at another feed."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.label.lower()).strip("-")[:24] or "news"
        digest = hashlib.sha1(self.feed.encode("utf-8"), usedforsecurity=False).hexdigest()
        return f"{slug}-{digest[:8]}"


@dataclass(frozen=True)
class Config:
    sources: tuple[Source, ...]
    groq_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    summary_words: int = DEFAULT_WORDS
    summary_prompt: str = ""
    run_at: str = DEFAULT_RUN_AT
    paint: painters.Settings = field(default_factory=lambda: painters.Settings(keys={}))

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> Config:
        def get(name: str, default: str = "") -> str:
            return str(settings.get(name) or "").strip() or default

        keys = {name: get(f"{name}_api_key") for name in painters.PROVIDERS}
        return cls(
            # Absent is the default list; saved blank means no sources at all.
            sources=parse_sources(
                DEFAULT_SOURCES if settings.get("sources") is None else str(settings["sources"])
            ),
            groq_key=get("groq_api_key"),
            groq_model=get("groq_model", "openai/gpt-oss-120b"),
            summary_words=_whole(settings.get("summary_words"), DEFAULT_WORDS, 10, 300),
            summary_prompt=get("summary_prompt"),
            run_at=parse_run_at(get("run_at", DEFAULT_RUN_AT)),
            paint=painters.Settings(
                keys=keys,
                order=parse_order(get("provider_order")),
                kie_model=get("kie_model", "google/nano-banana"),
                gemini_model=get("gemini_model", "gemini-2.5-flash-image"),
                pollinations_model=get("pollinations_model", "flux"),
                huggingface_model=get("huggingface_model", "black-forest-labs/FLUX.1-schnell"),
                image_prompt=get("image_prompt"),
            ),
        )

    @property
    def painters_ready(self) -> list[str]:
        """The providers in the cascade that have a key."""
        return [p for p in self.paint.order if self.paint.keys.get(p)]


def parse_sources(raw: str) -> tuple[Source, ...]:
    """One source per line, ``Label | feed URL``; a bare URL is labelled News.
    Blank lines, ``#`` comments and anything that is not an http(s) URL are skipped."""
    out: list[Source] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, url = line.rpartition("|")
        if not sep:
            label, url = "News", line
        label, url = " ".join(label.split())[:40] or "News", url.strip()
        if url.startswith(("http://", "https://")):
            out.append(Source(label, url))
    return tuple(out)


def parse_order(raw: str) -> tuple[str, ...]:
    """Provider names separated by commas or spaces, unknown ones dropped;
    the default order when nothing usable is left."""
    names = [n for n in re.split(r"[\s,>]+", raw.lower()) if n in painters.PROVIDERS]
    return tuple(dict.fromkeys(names)) or painters.PROVIDERS


def parse_run_at(raw: str) -> str:
    """``HH:MM`` (24-hour), or the default when it is not one."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", raw or "")
    if match and int(match[1]) < 24 and int(match[2]) < 60:
        return f"{int(match[1]):02d}:{match[2]}"
    return DEFAULT_RUN_AT


# ----------------------------------------------------------------------
# When a run is due
# ----------------------------------------------------------------------


def edition(now: datetime, run_at: str) -> str:
    """The edition a moment belongs to: today's from ``run_at`` on, else
    yesterday's. An ISO date."""
    hour, minute = (int(p) for p in run_at.split(":"))
    day = now.date() if (now.hour, now.minute) >= (hour, minute) else now.date() - timedelta(1)
    return day.isoformat()


def due(data_dir: Path, run_at: str, now: datetime) -> bool:
    """True when a settled render should start a run: there are no stories
    yet, or today's edition has not been made (nor tried too often)."""
    state = store.load_state(data_dir)
    current = edition(now, run_at)
    if state.get("edition") == current and store.load_stories(data_dir):
        return False
    tries = [float(t) for t in (state.get("attempts") or {}).get(current, [])]
    if len(tries) >= MAX_ATTEMPTS:
        return False
    return not tries or now.timestamp() - max(tries) >= RETRY_AFTER_S


# ----------------------------------------------------------------------
# One story
# ----------------------------------------------------------------------


@dataclass
class Outcome:
    """What a run did, for the log, the tray and the widget's page."""

    made: int = 0
    unchanged: int = 0
    kept: int = 0
    missing: int = 0

    def count(self, what: str) -> None:
        if what not in ("made", "unchanged", "kept", "missing"):
            raise ValueError(f"unknown story outcome {what!r}")
        setattr(self, what, getattr(self, what) + 1)

    def sentence(self) -> str:
        bits = [f"{self.made} new"]
        if self.unchanged:
            bits.append(f"{self.unchanged} unchanged")
        if self.kept:
            bits.append(f"{self.kept} kept from before")
        if self.missing:
            bits.append(f"{self.missing} missing")
        return ", ".join(bits)


def make_story(
    source: Source,
    previous: dict[str, Any] | None,
    cfg: Config,
    data_dir: Path,
    *,
    today: date,
    force: bool,
    log: Callable[[str], None],
    stopped: Callable[[], bool],
    budget: Callable[[], painters.Budget] = painters.Budget,
) -> tuple[dict[str, Any] | None, str]:
    """(the story to show for ``source``, what happened: one of the
    :class:`Outcome` counters, made / unchanged / kept / missing)."""
    item = feeds.newest_item(source.feed, log)
    if item is None:
        log("Feed failed, keeping the previous story")
        return previous, "kept" if previous else "missing"
    log(f"Title: {item.title}")
    if (
        not force
        and previous
        and previous.get("image")
        and previous.get("url") == item.link
        and previous.get("title") == item.title
    ):
        log("Same story as before, keeping its picture")
        return previous, "unchanged"
    if stopped():
        return previous, "kept" if previous else "missing"
    found = feeds.article(item, log)
    log(f"Summarising {len(found.text)} characters from the {found.origin}")
    summary = summarise.summarise(
        item.title,
        found.text,
        api_key=cfg.groq_key,
        model=cfg.groq_model,
        words=cfg.summary_words,
        template=cfg.summary_prompt,
        log=log,
    )
    if stopped():
        return previous, "kept" if previous else "missing"
    painting = painters.paint(
        item.title, summary, cfg.paint, budget=budget(), log=log, stopped=stopped
    )
    story = {
        "source_key": source.key,
        "source": source.label,
        "title": item.title,
        "summary": summary,
        "url": item.link if item.link.startswith(("http://", "https://")) else "",
        "date": today.isoformat(),
        "made": int(time.time()),
        "image": "",
        "provider": "",
    }
    if painting is None:
        if previous and previous.get("image"):
            # Never pair the new headline with an unrelated old picture.
            log("No picture, keeping the whole previous story")
            return previous, "kept"
        log("No picture and nothing earlier with one, saving the story as text only")
        return story, "made"
    story["image"] = store.save_image(data_dir, source.key, painting.jpeg)
    story["provider"] = painting.provider
    return story, "made"


# ----------------------------------------------------------------------
# A whole edition
# ----------------------------------------------------------------------


def run_edition(
    cfg: Config,
    data_dir: Path,
    *,
    tz: tzinfo | None = None,
    force: bool = False,
    stopped: Callable[[], bool] = lambda: False,
    log: Callable[[str], None] = logger.info,
    progress: Callable[[int, int, str], None] = lambda done, total, label: None,
    budget: Callable[[], painters.Budget] = painters.Budget,
) -> Outcome:
    """Make one story per source, saving after each so panels see new
    stories as they land and a crash loses at most the one under way."""
    data_dir = Path(data_dir)
    today = datetime.now(tz).date()
    before = {s.get("source_key"): s for s in store.load_stories(data_dir)}
    shown: dict[str, dict[str, Any]] = {}
    outcome = Outcome()
    total = len(cfg.sources)
    for n, source in enumerate(cfg.sources, start=1):
        if stopped():
            log("Stopped")
            break
        store.refresh_lock(data_dir)
        progress(n - 1, total, source.label)
        log(f"Story {n} of {total}: {source.label}")
        previous = before.get(source.key)
        story, what = make_story(
            source,
            previous,
            cfg,
            data_dir,
            today=today,
            force=force,
            log=log,
            stopped=stopped,
            budget=budget,
        )
        outcome.count(what)
        if story is not None:
            shown[source.key] = story
        # Sources not reached yet keep their stories until their turn.
        pending = {s.key: before[s.key] for s in cfg.sources[n:] if s.key in before}
        ordered = [
            (shown | pending)[s.key] for s in cfg.sources if s.key in shown or s.key in pending
        ]
        store.save_stories(data_dir, ordered)
    store.prune_images(
        data_dir, {str(s.get("image")) for s in store.load_stories(data_dir) if s.get("image")}
    )
    progress(total, total, "")
    return outcome


# ----------------------------------------------------------------------
# The background runner
# ----------------------------------------------------------------------


@dataclass
class _Run:
    data_dir: Path
    stop: threading.Event
    total: int
    done: int = 0
    label: str = ""


class Runner:
    """At most one run per data dir, on a daemon thread. The run lock in the
    data dir keeps a second process (or a reloaded copy of this module) from
    starting another alongside it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[Path, _Run] = {}

    def running(self, data_dir: Path) -> bool:
        """True while a run is going, here or in another process."""
        with self._lock:
            if Path(data_dir) in self._runs:
                return True
        return store.lock_held(data_dir)

    def pending(self, data_dir: Path) -> int:
        """Stories this process's run has still to make; 1 for a run elsewhere."""
        with self._lock:
            run = self._runs.get(Path(data_dir))
        if run is not None:
            return max(1, run.total - run.done)
        return 1 if store.lock_held(data_dir) else 0

    def stop(self, data_dir: Path) -> bool:
        with self._lock:
            run = self._runs.get(Path(data_dir))
        if run is None:
            return False
        run.stop.set()
        return True

    def start(
        self,
        cfg: Config,
        data_dir: Path,
        *,
        tz: tzinfo | None,
        reason: str,
        force: bool = False,
    ) -> bool:
        """Start a run unless one is going. Returns at once."""
        data_dir = Path(data_dir)
        if not cfg.sources:
            return False
        with self._lock:
            if data_dir in self._runs or not store.take_lock(data_dir):
                return False
            run = _Run(data_dir, threading.Event(), len(cfg.sources))
            self._runs[data_dir] = run
        _note_attempt(data_dir, datetime.now(tz), cfg.run_at, reason)
        # The copied context carries the widget's capability scope into the
        # thread, so the run is held to the hosts the manifest declares.
        context = contextvars.copy_context()
        thread = threading.Thread(
            target=context.run,
            args=(self._work, cfg, run, tz, reason, force),
            name=f"{PLUGIN_ID}-run",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                self._runs.pop(data_dir, None)
            store.release_lock(data_dir)
            raise
        return True

    def _work(self, cfg: Config, run: _Run, tz: tzinfo | None, reason: str, force: bool) -> None:
        lines: list[str] = []
        started = time.time()

        def log(line: str) -> None:
            lines.append(f"{time.strftime('%H:%M:%S')} {line}")
            del lines[:-LOG_LINES]
            logger.info("%s: %s", PLUGIN_ID, line)

        def progress(done: int, total: int, label: str) -> None:
            run.done, run.label = done, label
            _tray_report(run, total)

        outcome: Outcome | None = None
        failure = ""
        try:
            log(f"Run started ({reason})")
            outcome = run_edition(
                cfg,
                run.data_dir,
                tz=tz,
                force=force,
                stopped=run.stop.is_set,
                log=log,
                progress=progress,
            )
            log(f"Run finished: {outcome.sentence()}")
        except Exception as err:  # a bug must not leave the lock held
            logger.exception("%s: run failed", PLUGIN_ID)
            failure = f"{type(err).__name__}: {str(err)[:200]}"
            log(f"Run failed: {failure}")
        finally:
            try:
                _record_run(run.data_dir, cfg, tz, reason, started, outcome, failure, lines, run)
            finally:
                with self._lock:
                    self._runs.pop(run.data_dir, None)
                store.release_lock(run.data_dir)
                _tray_close(run, outcome, failure)


RUNNER = Runner()


def _note_attempt(data_dir: Path, now: datetime, run_at: str, reason: str) -> None:
    state = store.load_state(data_dir)
    current = edition(now, run_at)
    attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
    # Only the current edition's attempts matter; older ones are dropped.
    tries = [float(t) for t in (attempts or {}).get(current, [])]
    if reason != "manual":
        tries.append(now.timestamp())
    state["attempts"] = {current: tries}
    store.save_state(data_dir, state)


def _record_run(
    data_dir: Path,
    cfg: Config,
    tz: tzinfo | None,
    reason: str,
    started: float,
    outcome: Outcome | None,
    failure: str,
    lines: list[str],
    run: _Run,
) -> None:
    state = store.load_state(data_dir)
    finished_edition = edition(datetime.now(tz), cfg.run_at)
    if outcome is not None and not run.stop.is_set() and (outcome.made or outcome.unchanged):
        state["edition"] = finished_edition
    state["last_run"] = {
        "reason": reason,
        "started": int(started),
        "finished": int(time.time()),
        "outcome": failure or (outcome.sentence() if outcome else ""),
        "stopped": run.stop.is_set(),
        "failed": bool(failure),
        "log": lines,
    }
    try:
        store.save_state(data_dir, state)
    except OSError:
        logger.exception("%s: could not save the run state", PLUGIN_ID)


def _tray_report(run: _Run, total: int) -> None:
    if jobs is None:
        return
    try:
        detail = f"Story {min(run.done + 1, total)} of {total}"
        jobs.report(
            PLUGIN_ID,
            "run",
            "Making the AI news",
            progress=run.done / total if total else None,
            detail=f"{detail}: {run.label}" if run.label else detail,
            label=LABEL,
            href=f"/plugins/{PLUGIN_ID}/",
            stop=run.stop.set,
        )
    except Exception:
        logger.debug("%s: job tray report failed", PLUGIN_ID, exc_info=True)


def _tray_close(run: _Run, outcome: Outcome | None, failure: str) -> None:
    if jobs is None:
        return
    try:
        if failure:
            jobs.fail(PLUGIN_ID, "run", failure)
        elif run.stop.is_set():
            jobs.cancel(PLUGIN_ID, "run", "Stopped")
        else:
            jobs.finish(PLUGIN_ID, "run", outcome.sentence() if outcome else "")
    except Exception:
        logger.debug("%s: job tray close failed", PLUGIN_ID, exc_info=True)


def _whole(value: Any, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(float(str(value)))))
    except (TypeError, ValueError):
        return default
