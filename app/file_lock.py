"""Cross-platform file locks for paid-task and budget reservations."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold the folder's lock, so one caller at a time submits or collects.
    ``fcntl`` does not exist on Windows, and importing it there stopped the
    whole server from starting, so Windows locks a byte of the file instead.
    """
    with path.open("ab") as lock:
        if sys.platform == "win32":
            import errno
            import msvcrt

            fd = lock.fileno()
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    # Gives up after about ten seconds; a picture takes longer.
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError as err:
                    if err.errno not in (errno.EACCES, errno.EDEADLK):
                        raise
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
