"""Persisted GoComics request spacing, daily attempt budget and site cooldown.

The caller supplies a lock shared across plugin reloads. All GoComics page,
image and browser-check requests use it, including manual imports. A restart,
new display or forced refresh cannot clear a wait saved on disk.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import urllib.error
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import sleep, time
from typing import Any, NoReturn

REQUEST_INTERVAL_S = 10
STRIP_RETRY_S = 6 * 3600
STRIP_ATTEMPTS = 3
COOLDOWN_MAX_S = 48 * 3600
HOSTS = frozenset(
    {
        "www.gocomics.com",
        "gocomics.com",
        "featureassets.gocomics.com",
        "assets.amuniversal.com",
        "featureassets.amuniversal.com",
    }
)
logger = logging.getLogger(__name__)


class Deferred(Exception):
    """Use saved pictures quietly until this epoch time, without another request."""

    def __init__(self, until: float) -> None:
        super().__init__("")
        self.until = until


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0


class Budget:
    def __init__(self, root: Path, lock: Any) -> None:
        self.path = root / "gocomics-limits.json"
        self.lock = lock

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.path)

    def begin_strip(self, name: str, day: str) -> None:
        """Reserve one attempt before I/O; failed or missing strips wait six hours."""
        with self.lock:
            state = self._read()
            now = time()
            until = _number(state.get("retry_at"))
            if until > now:
                raise Deferred(until)
            strips = state.get("strips")
            strips = strips if isinstance(strips, dict) else {}
            cutoff = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            strips = {
                k: v
                for k, v in strips.items()
                if isinstance(v, dict) and str(v.get("day") or "") >= cutoff
            }
            key = f"{day}:{name}"
            record = strips.get(key, {})
            attempts = int(_number(record.get("attempts")))
            until = _number(record.get("after"))
            if attempts >= STRIP_ATTEMPTS or until > now:
                raise Deferred(
                    max(until, now + STRIP_RETRY_S) if attempts >= STRIP_ATTEMPTS else until
                )
            strips[key] = {"day": day, "attempts": attempts + 1, "after": now + STRIP_RETRY_S}
            state["strips"] = strips
            self._write(state)

    def _limited(self, state: dict[str, Any], headers: Any) -> NoReturn:
        now = time()
        failures = min(16, max(0, int(_number(state.get("failures")))) + 1)
        wait = min(COOLDOWN_MAX_S, STRIP_RETRY_S * 2 ** (failures - 1))
        retry = str(headers.get("Retry-After") or "").strip() if headers else ""
        if retry.isdigit():
            retry_seconds = 0
            with contextlib.suppress(ValueError):
                retry_seconds = int(retry)
            # Beyond datetime's range, keep the site blocked indefinitely in practice.
            wait = max(wait, min(retry_seconds, 253402300799 - now))
        elif retry:
            try:
                stamp = parsedate_to_datetime(retry)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                wait = max(wait, stamp.timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
        until = now + wait
        state.update(retry_at=until, failures=failures)
        self._write(state)
        logger.warning(
            "comics_daily: GoComics HTTP 429; using saved comics until %s",
            datetime.fromtimestamp(until, UTC).isoformat(),
        )
        raise Deferred(until)

    def limited(self, headers: Any) -> NoReturn:
        """Stop on response headers alone, before reading a rate-limit body."""
        with self.lock:
            self._limited(self._read(), headers)

    def send(self, request: Callable[[], Any]) -> Any:
        """Serialize and space network requests; one 429 stops the whole site."""
        with self.lock:
            state = self._read()
            now = time()
            until = _number(state.get("retry_at"))
            if until > now:
                raise Deferred(until)
            wait = max(0.0, _number(state.get("next_request")) - now)
            if wait:
                sleep(wait)
            state["next_request"] = time() + REQUEST_INTERVAL_S
            self._write(state)
            try:
                reply = request()
            except urllib.error.HTTPError as err:
                if err.code != 429:
                    raise
                err.close()
                self._limited(state, err.headers)
            if getattr(reply, "status", None) == 429 or (
                getattr(reply, "status", None) == 403
                and b"429 Too Many Requests" in getattr(reply, "body", b"")[:4096]
            ):
                self._limited(state, reply.headers)
            return reply
