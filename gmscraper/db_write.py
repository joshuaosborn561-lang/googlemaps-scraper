"""Process-wide cap on concurrent Supabase write HTTP calls.

The project pool is about 60 connections and is shared with other services.
Stage thread pools (scrape / enrich / classify) stay independent; only
``supabase_sync`` and ``gc_sync`` writes take a slot here.

Override with ``DB_WRITE_CONCURRENCY`` (default 6).
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

DEFAULT_CONCURRENCY = 6

_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

_lock = threading.Lock()
_sem: threading.Semaphore | None = None
_sem_limit: int | None = None


def concurrency() -> int:
    raw = os.environ.get("DB_WRITE_CONCURRENCY", str(DEFAULT_CONCURRENCY))
    try:
        n = int(raw) if str(raw).strip() else DEFAULT_CONCURRENCY
    except (TypeError, ValueError):
        n = DEFAULT_CONCURRENCY
    return max(1, n)


def reset() -> None:
    """Drop the cached semaphore. Tests call this after changing the env."""
    global _sem, _sem_limit
    with _lock:
        _sem = None
        _sem_limit = None


def _semaphore() -> threading.Semaphore:
    global _sem, _sem_limit
    n = concurrency()
    with _lock:
        if _sem is None or _sem_limit != n:
            _sem = threading.Semaphore(n)
            _sem_limit = n
        return _sem


def is_write_method(method: str) -> bool:
    return (method or "").upper() in _WRITE_METHODS


@contextmanager
def slot(*, write: bool = True) -> Iterator[None]:
    """Hold one write permit for the duration of a Supabase mutating call.

    Reads (``write=False``) pass through without taking a slot.
    """
    if not write:
        yield
        return
    sem = _semaphore()
    sem.acquire()
    try:
        yield
    finally:
        sem.release()
