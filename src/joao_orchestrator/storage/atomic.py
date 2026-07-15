"""Atomic filesystem writes (migrated from V1.4.0 state.py).

Write-to-tmp + os.replace + fsync. Used by all artifact persistence so a crash
never leaves a partial file.

This module is the SINGLE home for durability primitives (m2 closure): no
other module should re-implement tmp/fsync/replace or append+fsync. A
macOS-compatible inter-process advisory lock (:class:`FileLock`) is provided
here so state writers can serialize read-modify-write across processes without
any new dependency (``fcntl`` is stdlib).
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


def _unique_tmp(path: Path) -> Path:
    """A unique temp path beside ``path`` (never a shared fixed .tmp name).

    A fixed ``<name>.tmp`` collides across concurrent writers and across a
    crashed prior run; a per-call random suffix makes every writer use its own
    temp file so two concurrent atomic writes cannot clobber each other's temp.
    """
    return path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(path)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        # Clean up our own temp if we crashed before the replace.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: dict) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True))


def append_line(path: Path, line: str) -> None:
    """Atomic-ish append: open for append, write+flush+fsync. For events.jsonl.

    This is the SINGLE fsync-append primitive (m2): callers that need an
    append-only audited line must route through here rather than re-implementing
    ``open(..., 'a')`` + ``flush`` + ``os.fsync``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = line if line.endswith("\n") else line + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# Inter-process advisory lock (macOS-compatible, stdlib fcntl)
# ---------------------------------------------------------------------------

try:
    import fcntl
    _HAVE_FCNTL = hasattr(fcntl, "LOCK_EX") and hasattr(fcntl, "LOCK_UN") \
        and hasattr(fcntl, "LOCK_NB")
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False


class LockAcquireError(RuntimeError):
    """Raised when an inter-process lock cannot be acquired within the timeout."""


class FileLock:
    """A macOS/POSIX advisory inter-process file lock (stdlib ``fcntl``).

    Used to serialize read-modify-write across cooperating processes (e.g. the
    project-state store). It is advisory: only processes that take this lock
    are serialized. The lock file is *not* the data file — it is a dedicated
    sibling ``<name>.lock`` so a crash never corrupts the data file's replace.

    Usage::

        with FileLock(state_path, timeout=5.0):
            ...  # read-modify-write, serialized across processes

    On non-POSIX platforms (no fcntl) the lock degrades to a best-effort
    in-process threading lock so the API is always usable; concurrent
    cross-process writes are then not serialized, which is documented via
    :attr:`cross_process`.
    """

    def __init__(self, target: Path, *, timeout: float = 5.0,
                 poll_interval: float = 0.05):
        self.target = Path(target)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._lock_path = self.target.with_name(self.target.name + ".lock")
        self._fh = None
        self._owned = False
        self.cross_process = _HAVE_FCNTL

    def acquire(self) -> None:
        import time
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT + O_RDWR: create if absent, never truncate an existing lock.
        fd = os.open(self._lock_path,
                     os.O_CREAT | os.O_RDWR, 0o600)
        self._fh = open(fd, closefd=True)
        deadline = time.monotonic() + self.timeout
        if _HAVE_FCNTL:
            # Try non-blocking; poll until the timeout expires.
            while True:
                try:
                    fcntl.flock(self._fh.fileno(),
                                fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._owned = True
                    return
                except OSError:
                    if time.monotonic() >= deadline:
                        self._fh.close()
                        self._fh = None
                        raise LockAcquireError(
                            f"could not acquire lock {self._lock_path} within "
                            f"{self.timeout}s")
                    time.sleep(self.poll_interval)
        else:  # pragma: no cover - non-POSIX fallback
            import threading
            self._thread_lock = FileLock._thread_locks.setdefault(  # type: ignore[attr-defined]
                str(self._lock_path), threading.Lock())
            while True:
                if self._thread_lock.acquire(False):
                    self._owned = True
                    return
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise LockAcquireError(
                        f"could not acquire lock {self._lock_path} within "
                        f"{self.timeout}s")
                time.sleep(self.poll_interval)

    def release(self) -> None:
        if not self._owned:
            return
        try:
            if _HAVE_FCNTL and self._fh is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._owned = False
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


# Module-level threading-lock registry for the non-POSIX fallback. POSIX builds
# never touch this; it exists so FileLock is always constructible.
import threading  # noqa: E402
FileLock._thread_locks = {}  # type: ignore[attr-defined]