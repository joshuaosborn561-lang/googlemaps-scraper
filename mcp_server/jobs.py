"""Background job runner so Claude web (5 min timeout) can start long scrapes."""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "data" / "jobs"


@dataclass
class Job:
    id: str
    kind: str
    status: str  # queued | running | completed | failed
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return asdict(self)


_lock = threading.Lock()
_jobs: dict[str, Job] = {}


def _path(job_id: str) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DIR / f"{job_id}.json"


def _persist(job: Job) -> None:
    _path(job.id).write_text(json.dumps(job.to_public(), indent=2, default=str), encoding="utf-8")


def get_job(job_id: str) -> Job:
    with _lock:
        if job_id in _jobs:
            return _jobs[job_id]
    path = _path(job_id)
    if not path.exists():
        raise ValueError(f"Unknown job_id {job_id!r}")
    data = json.loads(path.read_text(encoding="utf-8"))
    job = Job(**data)
    with _lock:
        _jobs[job_id] = job
    return job


def list_jobs(limit: int = 20) -> list[Job]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[Job] = []
    for path in files[:limit]:
        try:
            out.append(get_job(path.stem))
        except Exception:
            continue
    return out


def start_job(kind: str, fn: Callable[[], dict[str, Any]], meta: dict[str, Any] | None = None) -> Job:
    job = Job(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        status="queued",
        created_at=time.time(),
        meta=meta or {},
    )
    with _lock:
        _jobs[job.id] = job
    _persist(job)

    def worker() -> None:
        job.status = "running"
        job.started_at = time.time()
        _persist(job)
        try:
            job.result = fn() or {}
            job.status = "completed"
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.result = {"traceback": traceback.format_exc()[-4000:]}
        finally:
            job.finished_at = time.time()
            _persist(job)

    threading.Thread(target=worker, name=f"mcp-job-{job.id}", daemon=True).start()
    return job
