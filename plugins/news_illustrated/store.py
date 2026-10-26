"""Everything the widget keeps in its data dir, written atomically.

* ``stories.json``: the stories a render shows, one per source, in source order.
* ``images/<source>-<hash>.jpg``: one illustration per story. The name carries
  a hash of the bytes, so a new picture never overwrites the one a panel may
  be loading, and its URL can be cached forever.
* ``state.json``: the last run (for the widget's page) and which day's
  edition is done.
* ``cursor.json``: which story each placement showed last, for rotation.
* ``run.lock``: held while a run is going, so two never overlap (a second
  server process, or a widget reloaded in place with the old run still going).

A render only ever reads these; the run is the only writer of stories and
images.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

STORIES = "stories.json"
STATE = "state.json"
CURSOR = "cursor.json"
LOCK = "run.lock"
IMAGES = "images"
IMAGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,80}\.jpg")
# A rendering placement moves to the next story at most this often, so the
# renders around one send (a warm-ahead, the send, its follow-ups) agree.
ROTATE_MIN_S = 240
# Placements not seen for this long are forgotten.
CURSOR_KEEP_S = 30 * 86400
# A run refreshes the lock before every story; one this stale has died.
LOCK_STALE_S = 15 * 60

_CURSOR_LOCK = threading.Lock()


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file and a rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    write_atomic(path, json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))


# ----------------------------------------------------------------------
# Stories and their pictures
# ----------------------------------------------------------------------


def load_stories(data_dir: Path) -> list[dict[str, Any]]:
    got = read_json(Path(data_dir) / STORIES, {})
    stories = got.get("stories") if isinstance(got, dict) else None
    if not isinstance(stories, list):
        return []
    return [s for s in stories if isinstance(s, dict) and s.get("title")]


def save_stories(data_dir: Path, stories: list[dict[str, Any]]) -> None:
    write_json(Path(data_dir) / STORIES, {"stories": stories, "saved": int(time.time())})


def save_image(data_dir: Path, source_key: str, jpeg: bytes) -> str:
    """Keep a picture; returns its file name."""
    digest = hashlib.sha1(jpeg, usedforsecurity=False).hexdigest()[:12]
    name = f"{source_key}-{digest}.jpg"
    write_atomic(Path(data_dir) / IMAGES / name, jpeg)
    return name


def image_path(data_dir: Path, name: str) -> Path | None:
    """The picture called ``name``, if that is a name this widget writes and it exists."""
    if not IMAGE_NAME.fullmatch(name):
        return None
    path = Path(data_dir) / IMAGES / name
    return path if path.is_file() else None


def prune_images(data_dir: Path, keep: set[str]) -> int:
    """Delete pictures no story uses any more. How many went."""
    gone = 0
    with contextlib.suppress(OSError):
        for path in (Path(data_dir) / IMAGES).glob("*.jpg"):
            if path.name not in keep:
                with contextlib.suppress(OSError):
                    path.unlink()
                    gone += 1
    return gone


# ----------------------------------------------------------------------
# Run state
# ----------------------------------------------------------------------


def load_state(data_dir: Path) -> dict[str, Any]:
    got = read_json(Path(data_dir) / STATE, {})
    return got if isinstance(got, dict) else {}


def save_state(data_dir: Path, state: dict[str, Any]) -> None:
    write_json(Path(data_dir) / STATE, state)


# ----------------------------------------------------------------------
# Rotation
# ----------------------------------------------------------------------


def pick(data_dir: Path, placement: str, count: int, *, advance: bool, now: float) -> int:
    """The story index a placement shows now. With ``advance`` the placement
    moves on to the next story, at most once per ROTATE_MIN_S."""
    if count <= 0:
        return 0
    path = Path(data_dir) / CURSOR
    with _CURSOR_LOCK:
        got = read_json(path, {})
        cursors = got if isinstance(got, dict) else {}
        mine = cursors.get(placement)
        index = int(mine.get("i", -1)) if isinstance(mine, dict) else -1
        at = float(mine.get("at", 0)) if isinstance(mine, dict) else 0.0
        if index < 0 or (advance and now - at >= ROTATE_MIN_S):
            index += 1
            at = now
            cursors = {
                k: v
                for k, v in cursors.items()
                if isinstance(v, dict) and now - float(v.get("at", 0)) < CURSOR_KEEP_S
            }
            cursors[placement] = {"i": index % count, "at": at}
            with contextlib.suppress(OSError):
                write_json(path, cursors)
        return index % count


# ----------------------------------------------------------------------
# The run lock
# ----------------------------------------------------------------------


def take_lock(data_dir: Path, now: float | None = None) -> bool:
    """Take the run lock; False when a live run holds it. A lock left by a
    run that died (not refreshed for LOCK_STALE_S) is taken over."""
    path = Path(data_dir) / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    for _attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue  # released meanwhile; try again
            if age < LOCK_STALE_S:
                return False
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def refresh_lock(data_dir: Path) -> None:
    with contextlib.suppress(OSError):
        os.utime(Path(data_dir) / LOCK)


def release_lock(data_dir: Path) -> None:
    with contextlib.suppress(OSError):
        (Path(data_dir) / LOCK).unlink()


def lock_held(data_dir: Path, now: float | None = None) -> bool:
    """True while a live run (in this process or another) holds the lock."""
    now = time.time() if now is None else now
    try:
        return now - (Path(data_dir) / LOCK).stat().st_mtime < LOCK_STALE_S
    except OSError:
        return False
