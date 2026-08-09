"""Background job runner so Claude web (5 min timeout) can start long scrapes.

Jobs persist as JSON under data/jobs/ (on the Railway volume). Workers are
in-process daemon threads, so a container restart kills them while leaving
status="running" on disk. Mitigations:

1. heartbeat_at updated while a worker is alive, plus stage progress
2. get_job / get_job_status mark stale heartbeats as status="stalled"
3. sweep_orphaned_jobs() on process start flips leftover running/queued → interrupted
4. Serial FIFO queue + queue_key dedupe so multiple Claude chats can enqueue
   work without killing or duplicating an active run
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
# Enrich/classify is CPU-heavy (html2text); allow a few minutes of ticker lag
# before declaring the worker dead so live jobs are not false-stalled.
STALL_SECONDS = 300
# Background ticker while a job runs (enrich/classify may not emit scrape ticks).
HEARTBEAT_INTERVAL_SEC = 30

ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "stalled", "interrupted"}
)


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
_fns: dict[str, Callable[[], dict[str, Any]]] = {}
_tls = threading.local()
# FIFO of job ids waiting to run. At most one worker thread executes at a time.
_wait_queue: list[str] = []
_running_id: str | None = None
_dispatcher_wakeup = threading.Event()
_dispatcher_started = False


def _path(job_id: str) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DIR / f"{job_id}.json"


def _persist(job: Job) -> None:
    """Atomic write so a crash mid-persist never leaves a 0-byte job file."""
    path = _path(job.id)
    # Unique tmp per call — heartbeats from multiple threads must not share a path.
    tmp = path.with_suffix(
        path.suffix + f".{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )
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


def make_queue_key(kind: str, meta: dict[str, Any] | None = None) -> str:
    """Stable key for dedupe across Claude chats (same work → same key)."""
    meta = meta or {}
    plan = (meta.get("plan_path") or "").strip()
    if kind in ("run_leads", "scrape_maps") and plan:
        return f"{kind}:{plan}"
    if kind == "pipeline_run":
        schema = meta.get("schema") or ""
        table = meta.get("table") or ""
        stages = meta.get("stages") or ""
        return f"pipeline_run:{schema}.{table}:{stages}"
    if kind == "enrich_waterfall":
        need = meta.get("need") or ""
        max_tier = meta.get("max_tier") or ""
        rows_chars = meta.get("rows_chars")
        fingerprint = meta.get("rows_fingerprint") or rows_chars or ""
        return f"enrich_waterfall:{need}:{max_tier}:{fingerprint}"
    if kind == "enrich_sites":
        return f"enrich_sites:limit={meta.get('limit') or 0}"
    # Unique per call when we can't safely dedupe.
    return f"{kind}:{uuid.uuid4().hex[:12]}"


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
        now = time.time()
        job.heartbeat_at = now
        merged = dict(job.progress or {})
        if stage:
            merged["stage"] = stage
        if progress:
            merged.update(progress)
        merged["updated_at"] = now
        job.progress = merged
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
            # Free the runner slot if this was the active job.
            global _running_id
            if _running_id == job.id:
                _running_id = None
                _dispatcher_wakeup.set()
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


def queue_position(job_id: str) -> int | None:
    """1-based position in the wait queue, 0 if currently running, None if N/A."""
    with _lock:
        if _running_id == job_id:
            return 0
        try:
            return _wait_queue.index(job_id) + 1
        except ValueError:
            return None


def find_active_by_queue_key(queue_key: str) -> Job | None:
    """Return queued/running job with the same queue_key (fresh heartbeat)."""
    if not queue_key:
        return None
    key = queue_key.strip()
    now = time.time()
    for job in list_jobs(limit=50):
        if job.status not in ACTIVE_STATUSES:
            continue
        if (job.meta or {}).get("queue_key") != key:
            continue
        if job.status == "running":
            hb = job.heartbeat_at or job.started_at or job.created_at
            if now - float(hb or 0) > STALL_SECONDS:
                continue
        return job
    return None


def sweep_orphaned_jobs() -> dict[str, Any]:
    """On process start, no in-process workers exist — flip leftovers to interrupted."""
    global _running_id, _wait_queue
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
    with _lock:
        _running_id = None
        _wait_queue = []
    return {"interrupted": len(flipped), "job_ids": flipped}


def _ensure_dispatcher() -> None:
    global _dispatcher_started
    with _lock:
        if _dispatcher_started:
            return
        _dispatcher_started = True
    threading.Thread(
        target=_dispatcher_loop, name="mcp-job-dispatcher", daemon=True
    ).start()


def _dispatcher_loop() -> None:
    global _running_id
    while True:
        _dispatcher_wakeup.wait(timeout=2.0)
        _dispatcher_wakeup.clear()
        job_id: str | None = None
        fn: Callable[[], dict[str, Any]] | None = None
        with _lock:
            if _running_id is not None:
                # Still busy — confirm the runner hasn't vanished.
                running = _jobs.get(_running_id)
                if running is not None and running.status in ACTIVE_STATUSES:
                    continue
                _running_id = None
            while _wait_queue:
                candidate = _wait_queue[0]
                job = _jobs.get(candidate)
                if job is None or job.status != "queued":
                    _wait_queue.pop(0)
                    _fns.pop(candidate, None)
                    continue
                fn = _fns.get(candidate)
                if fn is None:
                    # Callable lost — fail the job rather than wedging the queue.
                    job.status = "failed"
                    job.error = "Internal error: job callable missing from queue"
                    job.finished_at = time.time()
                    _wait_queue.pop(0)
                    try:
                        _persist(job)
                    except OSError:
                        pass
                    continue
                _wait_queue.pop(0)
                job_id = candidate
                _running_id = candidate
                break
        if job_id and fn is not None:
            threading.Thread(
                target=_run_worker,
                args=(job_id, fn),
                name=f"mcp-job-{job_id}",
                daemon=True,
            ).start()


def _run_worker(job_id: str, fn: Callable[[], dict[str, Any]]) -> None:
    global _running_id
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            try:
                job = _load_from_disk(job_id)
                _jobs[job_id] = job
            except ValueError:
                _running_id = None
                _dispatcher_wakeup.set()
                return
        if job.status != "queued":
            _running_id = None
            _dispatcher_wakeup.set()
            return
        job.status = "running"
        job.started_at = time.time()
        job.heartbeat_at = time.time()
        snap = Job(**asdict(job))
    _persist(snap)

    _tls.job_id = job_id
    stop_hb = threading.Event()

    def ticker() -> None:
        while not stop_hb.wait(HEARTBEAT_INTERVAL_SEC):
            try:
                heartbeat(job_id)
            except Exception:  # noqa: BLE001
                continue

    threading.Thread(
        target=ticker, name=f"mcp-job-hb-{job_id}", daemon=True
    ).start()

    try:
        result = fn() or {}
        with _lock:
            job = _jobs[job_id]
            job.result = result
            job.status = "completed"
            job.error = None
    except Exception as exc:  # noqa: BLE001
        with _lock:
            job = _jobs[job_id]
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.result = {"traceback": traceback.format_exc()[-4000:]}
    finally:
        stop_hb.set()
        with _lock:
            job = _jobs[job_id]
            job.heartbeat_at = time.time()
            job.finished_at = time.time()
            snap = Job(**asdict(job))
            if _running_id == job_id:
                _running_id = None
        try:
            _persist(snap)
        except OSError:
            pass
        _tls.job_id = ""
        with _lock:
            _fns.pop(job_id, None)
        _dispatcher_wakeup.set()


def start_job(
    kind: str,
    fn: Callable[[], dict[str, Any]],
    meta: dict[str, Any] | None = None,
    *,
    queue_key: str | None = None,
    dedupe: bool = True,
) -> Job:
    """Enqueue a background job. Never kills an existing run.

    If ``dedupe`` and a queued/running job shares ``queue_key``, return that
    job instead of spawning a duplicate. Otherwise append to the serial FIFO
    queue and return immediately with status=queued (or running once the
    dispatcher picks it up).
    """
    meta = dict(meta or {})
    key = (queue_key or meta.get("queue_key") or make_queue_key(kind, meta)).strip()
    meta["queue_key"] = key

    if dedupe:
        existing = find_active_by_queue_key(key)
        if existing is not None:
            # Annotate for the caller without mutating durable status.
            with _lock:
                prog = dict(existing.progress or {})
                prog["attached"] = True
                prog["deduped"] = True
                existing.progress = prog
                _jobs[existing.id] = existing
            try:
                _persist(existing)
            except OSError:
                pass
            return existing

    job = Job(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        status="queued",
        created_at=time.time(),
        heartbeat_at=time.time(),
        meta=meta,
        progress={"stage": "queued", "updated_at": time.time()},
    )

    _ensure_dispatcher()
    with _lock:
        _jobs[job.id] = job
        _fns[job.id] = fn
        _wait_queue.append(job.id)
        position = _wait_queue.index(job.id) + 1
        job.progress = {
            **job.progress,
            "queue_position": position,
            "queue_key": key,
        }
        snap = Job(**asdict(job))
    _persist(snap)
    _dispatcher_wakeup.set()
    return job


def live_progress(job: Job, store: Any | None = None) -> dict[str, Any]:
    """Normalized live progress for get_job_status.

    Prefer counters already written into job.progress; for Maps scrape kinds,
    refresh jobs_* / businesses from Store (same source as pipeline_stats).
    """
    prog = dict(job.progress or {})
    meta = dict(job.meta or {})
    now = time.time()
    updated_at = prog.get("updated_at") or job.heartbeat_at or job.started_at or job.created_at

    jobs_total = prog.get("jobs_total")
    jobs_done = prog.get("jobs_done")
    jobs_pending = prog.get("jobs_pending")
    businesses_found = prog.get("businesses_found")

    # Stage-local done/total (enrich / waterfall / scrape run ticks).
    stage_done = prog.get("done")
    stage_total = prog.get("total")

    if store is not None and job.kind in ("run_leads", "scrape_maps"):
        try:
            categories = meta.get("categories") or prog.get("categories_list")
            zips = meta.get("zips") or prog.get("zips_list")
            if isinstance(categories, int):
                categories = None
            if isinstance(zips, int):
                zips = None
            grid = store.grid_stats(
                categories=categories if isinstance(categories, (list, tuple)) else None,
                zips=zips if isinstance(zips, (list, tuple)) else None,
            )
            jobs_total = grid["jobs_total"]
            jobs_done = grid["jobs_done"]
            jobs_pending = grid["jobs_pending"]
            businesses_found = store.businesses_found_since(job.started_at)
        except Exception:  # noqa: BLE001
            pass
    elif store is not None and job.kind == "enrich_sites":
        try:
            stats = store.stats()
            sites_ok = int(stats.get("sites_ok") or 0)
            sites_pending = int(stats.get("sites_pending") or 0)
            domains = int(stats.get("domains") or 0)
            # Prefer stage ticks when present; else derive from sites table.
            if stage_total is None:
                jobs_total = domains or (sites_ok + sites_pending)
            if stage_done is None:
                jobs_done = sites_ok
            if jobs_pending is None:
                jobs_pending = sites_pending
            if businesses_found is None:
                businesses_found = int(stats.get("domains_with_email") or 0)
        except Exception:  # noqa: BLE001
            pass

    # Fallbacks from stage ticks when grid isn't applicable.
    if jobs_total is None and stage_total is not None:
        jobs_total = int(stage_total)
    if jobs_done is None and stage_done is not None:
        jobs_done = int(stage_done)
    if jobs_pending is None and jobs_total is not None and jobs_done is not None:
        jobs_pending = max(0, int(jobs_total) - int(jobs_done))
    if businesses_found is None:
        businesses_found = prog.get("new") or prog.get("emails") or prog.get("emails_found") or 0

    try:
        jt = int(jobs_total or 0)
        jd = int(jobs_done or 0)
        jp = int(jobs_pending if jobs_pending is not None else max(0, jt - jd))
        bf = int(businesses_found or 0)
    except (TypeError, ValueError):
        jt, jd, jp, bf = 0, 0, 0, 0

    if job.status == "completed" and jt > 0:
        jd = max(jd, jt)
        jp = 0

    percent = 0.0
    if jt > 0:
        percent = round(min(100.0, (jd / jt) * 100.0), 1)
    elif job.status == "completed":
        percent = 100.0

    eta_seconds: float | None = None
    started = job.started_at or job.created_at
    if job.status == "running" and started and jd > 0 and jp > 0:
        elapsed = max(now - float(started), 1e-3)
        # Prefer work done this run (stage_done) for rate when present.
        rate_done = int(stage_done) if stage_done is not None else jd
        # Subtract baseline done-at-start if recorded.
        baseline = int(meta.get("jobs_done_at_start") or 0)
        work_done = max(0, rate_done if stage_done is not None else (jd - baseline))
        if work_done > 0:
            rate = work_done / elapsed
            if rate > 0:
                eta_seconds = round(jp / rate, 1)

    qpos = queue_position(job.id)

    return {
        "stage": prog.get("stage") or ("queued" if job.status == "queued" else job.kind),
        "jobs_total": jt,
        "jobs_done": jd,
        "jobs_pending": jp,
        "businesses_found": bf,
        "percent_complete": percent,
        "updated_at": float(updated_at) if updated_at else None,
        "eta_seconds": eta_seconds,
        "queue_position": qpos,
        "queue_key": meta.get("queue_key"),
        "last_zip": prog.get("last_zip"),
        "last_category": prog.get("last_category"),
        "via": prog.get("via"),
        "deferred": prog.get("deferred"),
        "attached": bool(prog.get("attached") or prog.get("deduped")),
    }
