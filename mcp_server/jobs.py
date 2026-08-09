"""Background job runner so Claude web (5 min timeout) can start long scrapes.

Jobs persist as JSON under data/jobs/ (on the Railway volume). Workers are
in-process daemon threads, so a container restart kills them while leaving
status="running" on disk. Mitigations:

1. heartbeat_at updated every ~60s while a worker is alive, plus scrape progress
2. get_job / get_job_status mark stale heartbeats as status="stalled"
3. sweep_orphaned_jobs() on process start flips leftover running/queued → interrupted
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "data" / "jobs"

# No heartbeat for this long → treat as dead (container restart / killed thread).
STALL_SECONDS = 180
# Background ticker while a job runs (enrich/classify may not emit scrape ticks).
HEARTBEAT_INTERVAL_SEC = 60


@dataclass
class Job:
    id: str
    kind: str
    status: str  # queued | running | completed | failed | stalled | interrupted
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    heartbeat_at: float | None = None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_tls = threading.local()


def _path(job_id: str) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DIR / f"{job_id}.json"


def _persist(job: Job) -> None:
    """Atomic write so a crash mid-persist never leaves a 0-byte job file."""
    path = _path(job.id)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    payload = json.dumps(job.to_public(), indent=2, default=str)
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def current_job_id() -> str:
    return str(getattr(_tls, "job_id", "") or "")


def _load_from_disk(job_id: str) -> Job:
    path = _path(job_id)
    if not path.exists():
        raise ValueError(f"Unknown job_id {job_id!r}")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"Corrupt empty job file for {job_id!r}")
    data = json.loads(raw)
    # Backward-compatible defaults for older job files.
    data.setdefault("heartbeat_at", None)
    data.setdefault("progress", {})
    data.setdefault("meta", {})
    data.setdefault("result", {})
    return Job(**{k: v for k, v in data.items() if k in Job.__dataclass_fields__})


def heartbeat(
    job_id: str = "",
    *,
    stage: str = "",
    **progress: Any,
) -> None:
    """Touch heartbeat_at (and optional progress) for a running job.

    Disk I/O runs outside the lock so a stuck volume write cannot freeze
    stall detection / other tickers.
    """
    jid = (job_id or current_job_id()).strip()
    if not jid:
        return
    snapshot: Job | None = None
    with _lock:
        job = _jobs.get(jid)
        if job is None:
            try:
                job = _load_from_disk(jid)
                _jobs[jid] = job
            except ValueError:
                return
        if job.status not in ("queued", "running"):
            return
        job.heartbeat_at = time.time()
        if stage:
            job.progress = {**job.progress, "stage": stage, **progress}
        elif progress:
            job.progress = {**job.progress, **progress}
        # Copy fields for persist without holding the lock during I/O.
        snapshot = Job(**asdict(job))
    if snapshot is not None:
        try:
            _persist(snapshot)
        except OSError:
            pass


def _maybe_mark_stalled(job: Job, *, persist: bool = True) -> Job:
    if job.status != "running":
        return job
    hb = job.heartbeat_at or job.started_at or job.created_at
    age = time.time() - float(hb or 0)
    if age <= STALL_SECONDS:
        return job
    job.status = "stalled"
    job.finished_at = job.finished_at or time.time()
    job.error = (
        job.error
        or (
            f"No heartbeat for {int(age)}s (threshold {STALL_SECONDS}s). "
            "Worker likely died on a container restart. Re-run run_leads / "
            "scrape_maps — Maps scrape resumes from unfinished ZIP×category pairs."
        )
    )
    if persist:
        try:
            _persist(job)
        except OSError:
            pass
    return job


def get_job(job_id: str) -> Job:
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        job = _load_from_disk(job_id)
        with _lock:
            _jobs[job_id] = job
    persist_snap: Job | None = None
    with _lock:
        before = job.status
        _maybe_mark_stalled(job, persist=False)
        if job.status != before:
            persist_snap = Job(**asdict(job))
        out = Job(**asdict(job))
    if persist_snap is not None:
        try:
            _persist(persist_snap)
        except OSError:
            pass
    return out


def list_jobs(limit: int = 20) -> list[Job]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    out: list[Job] = []
    for path in files[:limit]:
        try:
            out.append(get_job(path.stem))
        except Exception:
            continue
    return out


def sweep_orphaned_jobs() -> dict[str, Any]:
    """On process start, no in-process workers exist — flip leftovers to interrupted."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    flipped: list[str] = []
    for path in JOBS_DIR.glob("*.json"):
        try:
            job = _load_from_disk(path.stem)
        except Exception:
            continue
        if job.status not in ("queued", "running", "stalled"):
            continue
        job.status = "interrupted"
        job.finished_at = time.time()
        job.error = (
            job.error
            or (
                "Orphaned on process start (container restart killed the worker). "
                "Re-run the same plan — Maps scrape resumes from unfinished "
                "ZIP×category pairs in SQLite."
            )
        )
        with _lock:
            _jobs[job.id] = job
        _persist(job)
        flipped.append(job.id)
    return {"interrupted": len(flipped), "job_ids": flipped}


def start_job(
    kind: str,
    fn: Callable[[], dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> Job:
    job = Job(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        status="queued",
        created_at=time.time(),
        heartbeat_at=time.time(),
        meta=meta or {},
    )
    with _lock:
        _jobs[job.id] = job
    _persist(job)

    def worker() -> None:
        _tls.job_id = job.id
        with _lock:
            job.status = "running"
            job.started_at = time.time()
            job.heartbeat_at = time.time()
            snap = Job(**asdict(job))
        _persist(snap)

        stop_hb = threading.Event()

        def ticker() -> None:
            while not stop_hb.wait(HEARTBEAT_INTERVAL_SEC):
                try:
                    heartbeat(job.id)
                except Exception:  # noqa: BLE001
                    # Never let the liveness ticker die on a transient error.
                    continue

        threading.Thread(
            target=ticker, name=f"mcp-job-hb-{job.id}", daemon=True
        ).start()

        try:
            result = fn() or {}
            with _lock:
                job.result = result
                job.status = "completed"
                job.error = None
        except Exception as exc:  # noqa: BLE001
            with _lock:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.result = {"traceback": traceback.format_exc()[-4000:]}
        finally:
            stop_hb.set()
            with _lock:
                job.heartbeat_at = time.time()
                job.finished_at = time.time()
                snap = Job(**asdict(job))
            try:
                _persist(snap)
            except OSError:
                pass
            _tls.job_id = ""

    threading.Thread(target=worker, name=f"mcp-job-{job.id}", daemon=True).start()
    return job
