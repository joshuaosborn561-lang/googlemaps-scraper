"""Stage 2: fan out over the (zip x category) grid."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Sequence

from .mapsdata import AuthError, MapsDataClient, MapsDataError
from .store import Store

ProgressCb = Callable[[dict[str, Any]], None]


class Progress:
    def __init__(
        self,
        total: int,
        price_per_request: float,
        on_progress: ProgressCb | None = None,
        heartbeat_every: int = 25,
    ):
        self.total = total
        self.done = 0
        self.errors = 0
        self.new_rows = 0
        self.requests = 0
        self.price = price_per_request
        self.start = time.monotonic()
        self.lock = threading.Lock()
        self.on_progress = on_progress
        self.heartbeat_every = max(1, int(heartbeat_every or 25))
        self.last_zip = ""
        self.last_category = ""

    def tick(
        self,
        new_rows: int,
        error: bool,
        *,
        zip_code: str = "",
        category: str = "",
    ) -> None:
        with self.lock:
            self.done += 1
            self.requests += 1
            self.new_rows += new_rows
            self.errors += int(error)
            if zip_code:
                self.last_zip = zip_code
            if category:
                self.last_category = category
            if self.done % self.heartbeat_every == 0 or self.done == self.total:
                self._render()
                self._emit()

    def _emit(self) -> None:
        if not self.on_progress:
            return
        try:
            self.on_progress(
                {
                    "stage": "scrape",
                    "done": self.done,
                    "total": self.total,
                    "new": self.new_rows,
                    "errors": self.errors,
                    "last_zip": self.last_zip,
                    "last_category": self.last_category,
                    "requests": self.requests,
                }
            )
        except Exception:  # noqa: BLE001 — never kill scrape for heartbeat I/O
            pass

    def _render(self) -> None:
        elapsed = max(time.monotonic() - self.start, 1e-6)
        rate = self.done / elapsed
        remaining = (self.total - self.done) / rate if rate else 0
        sys.stderr.write(
            f"\r  {self.done:,}/{self.total:,} searches | "
            f"{self.new_rows:,} new businesses | {self.errors:,} errors | "
            f"{rate:.1f}/s | ~{remaining / 60:.0f}m left | "
            f"~${self.requests * self.price:.2f} spent   "
        )
        sys.stderr.flush()

    def finish(self) -> None:
        self._render()
        sys.stderr.write("\n")


def run(
    store: Store,
    client: MapsDataClient,
    zip_rows: Sequence[dict[str, str]],
    categories: Sequence[str],
    workers: int = 8,
    price_per_request: float = 0.0,
    max_jobs: int | None = None,
    on_progress: ProgressCb | None = None,
    heartbeat_every: int = 25,
) -> dict[str, int]:
    """Scrape every pending (zip, category) pair. Safe to re-run after a kill.

    Completed pairs stay status='done' in SQLite, so a re-run skips them and
    resumes from the unfinished grid (ZIP×category).
    """
    by_zip = {r["zip"]: r for r in zip_rows}
    store.queue_jobs(list(by_zip), list(categories))

    pending = [
        (z, c) for z, c in store.pending_jobs(limit=max_jobs) if z in by_zip
    ]
    if not pending:
        print("Nothing pending -- every (zip, category) pair is already done.")
        if on_progress:
            try:
                on_progress(
                    {
                        "stage": "scrape",
                        "done": 0,
                        "total": 0,
                        "new": 0,
                        "errors": 0,
                        "resumed": True,
                        "pending": 0,
                    }
                )
            except Exception:  # noqa: BLE001
                pass
        return {"done": 0, "new": 0, "errors": 0, "pending_at_start": 0}

    print(
        f"{len(pending):,} searches pending "
        f"({len(by_zip):,} zips x {len(categories)} categories), "
        f"{workers} workers, est. ${len(pending) * price_per_request:,.2f}"
    )
    prog = Progress(
        len(pending),
        price_per_request,
        on_progress=on_progress,
        heartbeat_every=heartbeat_every,
    )
    stop = threading.Event()

    def work(job: tuple[str, str]) -> None:
        zip_code, category = job
        if stop.is_set():
            return
        try:
            rows = client.search(category, by_zip[zip_code])
            new = store.upsert_businesses(rows)
            store.finish_job(zip_code, category, len(rows))
            prog.tick(new, False, zip_code=zip_code, category=category)
        except AuthError as exc:
            stop.set()
            store.finish_job(zip_code, category, 0, str(exc))
            prog.tick(0, True, zip_code=zip_code, category=category)
            raise
        except (MapsDataError, Exception) as exc:  # noqa: BLE001 - keep the run alive
            store.finish_job(zip_code, category, 0, f"{type(exc).__name__}: {exc}")
            prog.tick(0, True, zip_code=zip_code, category=category)

    auth_failure: Exception | None = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, j) for j in pending]
        try:
            for f in as_completed(futures):
                exc = f.exception()
                if isinstance(exc, AuthError) and auth_failure is None:
                    auth_failure = exc
        except KeyboardInterrupt:
            stop.set()
            print("\nInterrupted -- progress is checkpointed, re-run to continue.")
            for f in futures:
                f.cancel()

    prog.finish()
    if auth_failure:
        raise SystemExit(f"\nAborted: {auth_failure}")
    return {
        "done": prog.done,
        "new": prog.new_rows,
        "errors": prog.errors,
        "pending_at_start": len(pending),
    }
