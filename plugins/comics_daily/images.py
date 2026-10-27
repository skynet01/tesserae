"""The last good comics on disk, and the per-source record of how they got there.

Layout inside the widget's ``data_dir``::

    state.json          per source: pictures, edition, last success / attempt, error
    log.json            the newest LOG_LINES fetches, uploads and imports, for the admin page
    gocomics-cookie-status.json  when the pasted GoComics cookies were set and how they did
    images/<name>       the pictures, named by content hash
    gocomics-cookies.txt  the Bunny Shield session (written by server.py)

A strip's manual picture (uploaded, or imported from an address on the admin
page) is a source of its own, ``manual:<strip>``, stored like a download and
never forgotten for age: it stays until the strip goes back to Automatic.

A refresh writes a new picture under a new name, then points ``state.json``
at it, then prunes, so a render reading the old state mid-refresh still
finds its files. Nothing is ever overwritten in place, which is what keeps
the last good strip on screen when a fetch fails.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from PIL import Image, ImageOps

STATE_FILE = "state.json"
LOG_FILE = "log.json"
COOKIE_STATUS_FILE = "gocomics-cookie-status.json"
IMAGE_DIR = "images"
# The admin page's log keeps this many lines.
LOG_LINES = 150
# A picture uploaded or imported by hand: at most this many bytes, and this
# many pixels once decoded (checked before decoding, so a small file that
# expands into a huge bitmap is refused rather than decoded).
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 50_000_000
MANUAL_PREFIX = "manual:"
# The widest source today is a 1200 px Dilbert; panels are 1440 px across.
# Anything larger is only bytes for the renderer to wait on.
MAX_EDGE_PX = 2400
JPEG_QUALITY = 90
# A picture no longer referenced stays this long, in case a render that read
# the previous state is still loading it.
PRUNE_GRACE_S = 3600
# A source nobody has asked for in this long is forgotten with its pictures.
FORGET_AFTER_S = 30 * 86400

_NAME_RE = re.compile(r"^[a-z0-9-]{1,80}-[0-9a-f]{12}\.(?:png|jpg)$")
_KEY_SAFE_RE = re.compile(r"[^a-z0-9]+")

_RUNTIME = f"_tesserae_plugins.{Path(__file__).resolve().parent.name}.runtime"


def shared(name: str, factory: Callable[[], Any]) -> Any:
    """Process-wide state that outlives a reload of the plugin's modules.

    An in-process widget reload imports these files again; module globals
    would then give the new code its own lock and worker registry while the
    old worker thread still writes to the same folder. Keeping them on one
    holder module, created once, gives every version the same objects.
    """
    holder = sys.modules.setdefault(_RUNTIME, ModuleType(_RUNTIME))
    return holder.__dict__.setdefault(name, factory())


# One writer at a time per process, across reloads; readers rely on the
# atomic replace.
_LOCK: threading.Lock = shared("store_lock", threading.Lock)


@dataclass(frozen=True)
class Picture:
    """A picture ready for the browser renderer."""

    data: bytes
    ext: str
    width: int
    height: int


def _flatten(img: Image.Image) -> Image.Image:
    """Bring any decoded mode down to L or RGB on white.

    Transparent pixels would otherwise show the theme's background through
    the strip, which on a dark theme is black ink on black.
    """
    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    if img.mode in ("RGBA", "LA", "PA"):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    if img.mode in ("1", "I", "I;16", "F"):
        return img.convert("L")
    if img.mode not in ("L", "RGB"):
        return img.convert("RGB")
    return img


def normalise(raw: bytes, max_edge: int = MAX_EDGE_PX) -> Picture:
    """Decode a downloaded picture and re-encode it for the renderer.

    Takes the first frame of an animation, applies EXIF orientation, flattens
    transparency, caps the size, and writes JPEG for photographic sources
    (the Far Side scans) and PNG for everything else (GIF line art keeps its
    hard edges). Raises ValueError when ``raw`` is not a picture at all,
    the usual sign of an error or challenge page served in its place.
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.width * img.height > MAX_PIXELS:
                raise ValueError("too many pixels")
            img.load()
            source_format = img.format
            out = _flatten(ImageOps.exif_transpose(img) or img)
            if max(out.size) > max_edge:
                out.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            if source_format == "JPEG":
                out.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                ext = "jpg"
            else:
                out.save(buf, format="PNG", optimize=True)
                ext = "png"
            width, height = out.size
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as err:
        raise ValueError("not a picture") from err
    if width < 1 or height < 1:
        raise ValueError("empty picture")
    return Picture(buf.getvalue(), ext, width, height)


def validate_upload(raw: bytes) -> Picture:
    """A picture uploaded or imported by hand, checked and converted the way
    a downloaded strip is (:func:`normalise`). Raises ValueError with a
    message for the page: too big, or not a picture."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"That picture is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    try:
        return normalise(raw)
    except ValueError as err:
        raise ValueError(
            "That isn't a picture this widget can use (PNG, JPEG, GIF or WebP, "
            f"at most {MAX_PIXELS // 1_000_000} megapixels)."
        ) from err


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class Store:
    """``state.json`` plus the picture files it points at."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir)
        self.image_dir = self.root / IMAGE_DIR
        self.state_path = self.root / STATE_FILE

    # -- state ------------------------------------------------------------

    def sources(self) -> dict[str, dict[str, Any]]:
        """Every source's record. A missing or corrupt file reads as empty,
        which only means the next render fetches again."""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        sources = data.get("sources") if isinstance(data, dict) else None
        if not isinstance(sources, dict):
            return {}
        return {k: v for k, v in sources.items() if isinstance(v, dict)}

    def update(self, key: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Read-modify-write one source's record under the store lock."""
        with _LOCK:
            sources = self.sources()
            entry = dict(sources.get(key) or {})
            change(entry)
            sources[key] = entry
            self._save(sources)
            return entry

    def _save(self, sources: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"version": 1, "sources": sources}, indent=1, sort_keys=True)
        _write_atomic(self.state_path, body.encode("utf-8"))

    def forget(self, key: str) -> None:
        """Drop one source's record (its pictures go at the next prune)."""
        with _LOCK:
            sources = self.sources()
            if sources.pop(key, None) is not None:
                self._save(sources)

    # -- the pasted GoComics cookies' status --------------------------------
    # When they were pasted, when they expire, how many, and how the last
    # GoComics session that used them did. Never the cookies themselves.

    def cookie_status(self) -> dict[str, Any]:
        try:
            data = json.loads((self.root / COOKIE_STATUS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def update_cookie_status(self, change: Callable[[dict[str, Any]], None] | None) -> None:
        """Change the status under the store lock; ``None`` forgets it."""
        with _LOCK:
            path = self.root / COOKIE_STATUS_FILE
            if change is None:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                return
            status = self.cookie_status()
            change(status)
            self.root.mkdir(parents=True, exist_ok=True)
            _write_atomic(path, json.dumps(status, indent=1).encode("utf-8"))

    # -- the log --------------------------------------------------------------

    def log(self) -> list[dict[str, Any]]:
        """The log's entries, oldest first; a missing or corrupt file is empty."""
        try:
            data = json.loads((self.root / LOG_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = data.get("log") if isinstance(data, dict) else None
        return [
            e
            for e in entries or []
            if isinstance(e, dict)
            and isinstance(e.get("at"), (int, float))
            and isinstance(e.get("text"), str)
        ]

    def append_log(self, entry: dict[str, Any]) -> None:
        """Add one entry, keeping the newest LOG_LINES."""
        with _LOCK:
            entries = [*self.log(), entry][-LOG_LINES:]
            self.root.mkdir(parents=True, exist_ok=True)
            body = json.dumps({"log": entries}, indent=1)
            _write_atomic(self.root / LOG_FILE, body.encode("utf-8"))

    # -- pictures -----------------------------------------------------------

    def add(self, key: str, picture: Picture) -> str:
        """Write ``picture`` and return its file name. Same bytes, same name,
        so an unchanged strip refetched tomorrow costs no new file."""
        self.image_dir.mkdir(parents=True, exist_ok=True)
        prefix = _KEY_SAFE_RE.sub("-", key.lower()).strip("-")[:60] or "comic"
        digest = hashlib.sha256(picture.data).hexdigest()[:12]
        name = f"{prefix}-{digest}.{picture.ext}"
        path = self.image_dir / name
        if not path.exists():
            _write_atomic(path, picture.data)
        return name

    def image_path(self, name: str) -> Path | None:
        """The file behind a served name, or None. Names are validated
        against the shape :meth:`add` writes, so no path escapes the folder."""
        if not _NAME_RE.match(name):
            return None
        path = self.image_dir / name
        return path if path.is_file() else None

    def prune(self, now: float | None = None) -> None:
        """Forget long-unused sources and delete pictures nothing points at."""
        now = time.time() if now is None else now
        with _LOCK:
            sources = self.sources()
            stale = [
                key
                for key, entry in sources.items()
                if not key.startswith(MANUAL_PREFIX)
                and now - float(entry.get("last_attempt") or 0) > FORGET_AFTER_S
            ]
            for key in stale:
                del sources[key]
            if stale:
                self._save(sources)
            keep = {
                str(pic.get("file"))
                for entry in sources.values()
                for pic in entry.get("pictures") or []
                if isinstance(pic, dict)
            }
        if not self.image_dir.is_dir():
            return
        for path in self.image_dir.iterdir():
            if path.name in keep or not path.is_file():
                continue
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > PRUNE_GRACE_S:
                    path.unlink()
