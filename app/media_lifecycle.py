"""How a widget that keeps downloaded pictures retries and cleans up.

Three bundled widgets download pictures in the background and keep them on
disk: the newspaper front pages, the comics and the AI news cartoons. They
share one policy, so a source that is down behaves the same in each:

* **Retry with a backoff.** After a failed try the source is asked again
  30 minutes later, then after 1, 2, 4 hours, never more than 6 hours
  apart, and at most ``TRIES_PER_DAY`` times a day; the next day starts
  over. A forced fetch (Fetch now, a refresh) ignores the wait. Each widget
  logs every try.
* **Reuse the last good picture.** A failed try never replaces what is on
  disk, so the last good picture stays up (with the widget's small "not
  updated" note) until a try succeeds; the new one replaces it at once.
* **Clean up, gently.** A picture nothing refers to any more (a past
  edition, a removed strip or paper, a replaced cartoon) is deleted once it
  has gone unused for ``GRACE_S``, so a render that read the old state can
  still load it. Each widget's folder is kept under a size budget; past it
  the oldest unreferenced pictures go first. A picture still referenced
  (the one on screen) is never deleted.

mypy --strict applies to this module, see pyproject.toml.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

RETRY_FIRST_S = 30 * 60
RETRY_MAX_S = 6 * 60 * 60
TRIES_PER_DAY = 6
GRACE_S = 60 * 60
# Even over budget, a file this fresh stays: a render may be loading it.
MIN_AGE_S = 5 * 60


def retry_wait(failures: int, *, first: float = RETRY_FIRST_S, cap: float = RETRY_MAX_S) -> float:
    """Seconds to wait after the ``failures``-th failed try in a row (1, 2,
    ...): ``first`` doubling each time, at most ``cap``."""
    return float(min(first * 2 ** min(max(failures, 1) - 1, 16), cap))


def failed(
    record: dict[str, Any] | None,
    today: str,
    now: float,
    *,
    first: float = RETRY_FIRST_S,
    cap: float = RETRY_MAX_S,
) -> dict[str, Any]:
    """A source's retry record after a failed try: today's count of failed
    tries (a new day starts at one) and when it may be tried again."""
    tries = 1
    if isinstance(record, dict) and record.get("day") == today:
        count = record.get("tries")
        tries = (count if isinstance(count, int) and not isinstance(count, bool) else 0) + 1
    return {"day": today, "tries": tries, "after": now + retry_wait(tries, first=first, cap=cap)}


def may_try(
    record: dict[str, Any] | None,
    today: str,
    now: float,
    *,
    force: bool = False,
    tries_per_day: int = TRIES_PER_DAY,
) -> bool:
    """Whether a source with retry ``record`` may be tried now. No record
    (or one from another day) may; ``force`` always may."""
    if force or not isinstance(record, dict) or record.get("day") != today:
        return True
    tries, after = record.get("tries"), record.get("after")
    if not isinstance(tries, int) or isinstance(tries, bool):
        return True
    if tries >= tries_per_day:
        return False
    return not isinstance(after, (int, float)) or now >= after


def describe(
    record: dict[str, Any] | None, today: str, now: float, *, tries_per_day: int = TRIES_PER_DAY
) -> str:
    """The retry state in words for a log line or a page: "next try in 30
    min (try 2 of 6)", "no more tries today", or ""."""
    if not isinstance(record, dict) or record.get("day") != today:
        return ""
    tries = record.get("tries")
    if not isinstance(tries, int) or isinstance(tries, bool):
        return ""
    if tries >= tries_per_day:
        return "no more tries today"
    after = record.get("after")
    wait = max(0.0, float(after) - now) if isinstance(after, (int, float)) else 0.0
    minutes = round(wait / 60)
    span = f"{minutes} min" if minutes < 60 else f"{minutes / 60:.1f} h".replace(".0 h", " h")
    return f"next try in {span} (try {tries + 1} of {tries_per_day})"


def sweep(
    directory: Path,
    *,
    keep: Iterable[str],
    max_bytes: int,
    grace_s: float = GRACE_S,
    now: float | None = None,
    pattern: str = "*",
) -> list[str]:
    """Delete the files in ``directory`` matching ``pattern`` that aren't in
    ``keep`` (names) once unused for ``grace_s`` (by mtime); then, while the
    folder is over ``max_bytes``, the oldest such files at least
    ``MIN_AGE_S`` old. Files in ``keep`` are never deleted. Returns the
    names deleted."""
    now = time.time() if now is None else now
    kept = set(keep)
    files: list[tuple[float, int, Path]] = []
    total = 0
    with contextlib.suppress(OSError):
        for path in directory.glob(pattern):
            with contextlib.suppress(OSError):
                if not path.is_file():
                    continue
                stat = path.stat()
                total += stat.st_size
                if path.name not in kept:
                    files.append((stat.st_mtime, stat.st_size, path))
    files.sort(key=lambda item: item[0])
    gone: list[str] = []
    for mtime, size, path in files:
        old = now - mtime
        if old > grace_s or (total > max_bytes and old > MIN_AGE_S):
            with contextlib.suppress(OSError):
                path.unlink()
                total -= size
                gone.append(path.name)
    return gone
