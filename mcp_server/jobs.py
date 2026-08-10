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
    {"completed", "failed", "stalled", "interrupted", "cancelled"}
)


@dataclass
class Job:
    id: str
    kind: str
    status: str  # queued | running | completed | failed | stalled | interrupted | cancelled
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
_cancel_requested: set[str] = set()
_tls = threading.local()
# Priority wait queue. Heavy jobs (SERP/crawl/scrape) are serial; light kinds
# (classify_leads, …) may run in parallel with one heavy job so they are not
# starved across container restarts.
_wait_queue: list[str] = []
_running_ids: set[str] = set()
_dispatcher_wakeup = threading.Event()
_dispatcher_started = False

# Kinds allowed to share the runner with one heavy job.
LIGHT_PARALLEL_KINDS = frozenset(
    {
        "classify_leads",
        "extract_team_contacts",
    }
)


def _max_parallel() -> int:
    try:
        return max(1, int(os.environ.get("MCP_MAX_PARALLEL_JOBS", "2")))
    except ValueError:
        return 2


def _is_light(kind: str) -> bool:
    return (kind or "") in LIGHT_PARALLEL_KINDS


def _can_start_locked(kind: str) -> bool:
    """Caller must hold ``_lock``. Decide if ``kind`` may start now."""
    if len(_running_ids) >= _max_parallel():
        return False
    heavy_running = 0
    for jid in _running_ids:
        j = _jobs.get(jid)
        if j is None:
            continue
        if not _is_light(j.kind):
            heavy_running += 1
    if _is_light(kind):
        return True
    return heavy_running == 0


class JobCancelled(Exception):
    """Raised inside a worker when cancel_job wins the race."""


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


def _scope_fingerprint(meta: dict[str, Any]) -> str:
    """Stable suffix for scoped enrich/classify/crawl jobs."""
    parts = []
    for key in (
        "city",
        "state",
        "main_category",
        "plan_id",
        "plan_path",
        "run_id",
        "client_tag",
        "source",
    ):
        val = str(meta.get(key) or "").strip()
        if val:
            parts.append(f"{key}={val}")
    return "|".join(parts)


def make_queue_key(kind: str, meta: dict[str, Any] | None = None) -> str:
    """Stable key for dedupe across Claude chats (same work → same key)."""
    meta = meta or {}
    plan = (meta.get("plan_path") or "").strip()
    if kind in ("run_leads", "scrape_maps") and plan:
        return f"{kind}:{plan}"
    if kind == "resolve_places":
        schema = meta.get("schema") or ""
        table = meta.get("table") or ""
        details = "details" if meta.get("details_only") else "full"
        pid = meta.get("project_id") or ""
        return f"resolve_places:{pid}:{schema}.{table}:{details}"
    if kind == "resolve_via_serp":
        schema = meta.get("schema") or ""
        table = meta.get("table") or ""
        pid = meta.get("project_id") or ""
        return f"resolve_via_serp:{pid}:{schema}.{table}"
    if kind == "resolve_addresses":
        schema = meta.get("schema") or ""
        table = meta.get("table") or ""
        pid = meta.get("project_id") or ""
        method = meta.get("method") or "auto"
        return f"resolve_addresses:{pid}:{schema}.{table}:{method}:limit={meta.get('limit') or 0}"
    if kind == "run_owner_lane":
        pid = meta.get("project_id") or ""
        states = meta.get("states") or ""
        return (
            f"run_owner_lane:{pid}:{states}:limit={meta.get('resolve_limit') or 0}:"
            f"method={meta.get('method') or 'serp'}"
        )
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
    if kind in ("enrich_sites", "crawl_team_pages", "classify_leads"):
        scope = _scope_fingerprint(meta)
        base = f"{kind}:limit={meta.get('limit') or 0}"
        if scope:
            return f"{base}:{scope}"
        return base
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
            # Free the runner slot if this was an active job.
            if job.id in _running_ids:
                _running_ids.discard(job.id)
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
        if job_id in _running_ids:
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


def is_cancel_requested(job_id: str = "") -> bool:
    jid = (job_id or current_job_id()).strip()
    if not jid:
        return False
    with _lock:
        if jid in _cancel_requested:
            return True
        job = _jobs.get(jid)
        return bool(job and (job.meta or {}).get("cancel_requested"))


def cancel_job(job_id: str, *, reason: str = "") -> dict[str, Any]:
    """Cancel a queued or running job. Queued jobs never start.

    Running jobs are marked cancelled and asked to stop; in-flight vendor
    work (e.g. an Apify actor that already has a runId) is best-effort —
    prefer cancelling while status=queued. Cancelled jobs are never
    auto-resumed on restart.
    """
    jid = (job_id or "").strip()
    if not jid:
        raise ValueError("job_id is required")

    try:
        job = get_job(jid)
    except ValueError:
        return {"ok": False, "job_id": jid, "error": "unknown_job_id"}

    if job.status == "cancelled":
        return {
            "ok": True,
            "job_id": jid,
            "status": "cancelled",
            "already_cancelled": True,
            "kind": job.kind,
        }
    if job.status in TERMINAL_STATUSES:
        return {
            "ok": False,
            "job_id": jid,
            "status": job.status,
            "error": f"job already terminal ({job.status})",
            "kind": job.kind,
        }

    why = (reason or "Cancelled by cancel_job").strip()
    was = job.status
    with _lock:
        job = _jobs.get(jid) or job
        meta = dict(job.meta or {})
        meta["cancel_requested"] = True
        meta["auto_resume_attempted"] = True  # never reclaim
        meta["cancelled_reason"] = why
        job.meta = meta
        _cancel_requested.add(jid)

        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = time.time()
            job.error = why
            job.progress = {
                **dict(job.progress or {}),
                "stage": "cancelled",
                "updated_at": time.time(),
            }
            if jid in _wait_queue:
                _wait_queue[:] = [x for x in _wait_queue if x != jid]
            _fns.pop(jid, None)
        elif job.status == "running":
            # Soft-cancel: flag the worker; status flips when the worker exits
            # or immediately if we can claim it before work starts.
            job.progress = {
                **dict(job.progress or {}),
                "cancel_requested": True,
                "updated_at": time.time(),
            }
        snap = Job(**asdict(job))
    _persist(snap)
    _dispatcher_wakeup.set()

    # Re-read after persist for the response.
    fresh = get_job(jid)
    return {
        "ok": True,
        "job_id": jid,
        "kind": fresh.kind,
        "was_status": was,
        "status": fresh.status,
        "cancel_requested": True,
        "removed_from_queue": was == "queued",
        "note": (
            "Queued job removed; it will not start."
            if was == "queued"
            else "Running job flagged; worker will exit as cancelled when able. "
            "If an Apify actor already started, abort it separately with its runId."
        ),
        "queue_position": queue_position(jid),
    }


_RESUMABLE_KINDS = frozenset(
    {
        "run_leads",
        "scrape_maps",
        "enrich_sites",
        "resolve_places",
        "resolve_via_serp",
        "run_owner_lane",
        "pipeline_run",
        "crawl_team_pages",
        "classify_leads",
    }
)

# Kinds that spend money and must NOT auto-resume without an approval flag.
_PAID_RESUME_KINDS = frozenset(
    {
        "apify_contact_crawl",
        "enrich_waterfall",  # may hit paid tiers; resume only with approve_paid
        "find_owners",  # paid when use_paid_fallback
    }
)


def sweep_orphaned_jobs() -> dict[str, Any]:
    """On process start, no in-process workers exist — flip leftovers to interrupted.

    Also returns previously interrupted resumable jobs that have not yet been
    auto-resume-attempted (so a deploy that ships auto-resume can reclaim work
    killed by earlier restarts).
    """
    global _wait_queue
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    flipped: list[str] = []
    records: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    now = time.time()

    def _record(job: Job, *, mark_attempt: bool = False) -> None:
        meta = dict(job.meta or {})
        key = str(meta.get("queue_key") or make_queue_key(job.kind, meta))
        if key in seen_keys:
            return
        seen_keys.add(key)
        if mark_attempt:
            meta["auto_resume_attempted"] = True
            job.meta = meta
            with _lock:
                _jobs[job.id] = job
            _persist(job)
        records.append(
            {
                "id": job.id,
                "kind": job.kind,
                "meta": meta,
                "progress": dict(job.progress or {}),
            }
        )

    for path in JOBS_DIR.glob("*.json"):
        try:
            job = _load_from_disk(path.stem)
        except Exception:
            continue
        if job.status not in ("queued", "running", "stalled"):
            continue
        job.status = "interrupted"
        job.finished_at = now
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
        _record(job, mark_attempt=True)

    # Reclaim interrupted jobs from prior boots that never got auto-resume.
    max_age = float(os.environ.get("MCP_AUTO_RESUME_MAX_AGE_SEC", str(7 * 86400)))
    for path in JOBS_DIR.glob("*.json"):
        try:
            job = _load_from_disk(path.stem)
        except Exception:
            continue
        if job.status != "interrupted":
            continue
        meta = dict(job.meta or {})
        if (
            meta.get("auto_resume_attempted")
            or meta.get("auto_resumed_from")
            or meta.get("cancel_requested")
            or meta.get("cancelled_reason")
            or meta.get("needs_approval")
        ):
            continue
        # Never reclaim paid work unless the original call set approve_paid.
        if job.kind in _PAID_RESUME_KINDS and not meta.get("approve_paid"):
            meta["needs_approval"] = True
            meta["auto_resume_attempted"] = True
            job.meta = meta
            job.error = (
                (job.error or "")
                + " | Not auto-resumed: paid job requires approve_paid=true"
            ).strip(" |")
            with _lock:
                _jobs[job.id] = job
            _persist(job)
            continue
        if job.kind not in _RESUMABLE_KINDS:
            continue
        # Global enrich_sites:limit=0 / backlog drain starves scoped work —
        # do not reclaim them; callers re-queue with scope when needed.
        if job.kind == "enrich_sites":
            scoped = bool(_scope_fingerprint(meta))
            if meta.get("backlog_drain") or (
                int(meta.get("limit") or 0) == 0 and not scoped
            ):
                meta["auto_resume_attempted"] = True
                meta["resume_skipped_reason"] = "global_enrich_or_backlog"
                job.meta = meta
                with _lock:
                    _jobs[job.id] = job
                _persist(job)
                continue
        finished = float(job.finished_at or job.heartbeat_at or job.created_at or 0)
        if finished and (now - finished) > max_age:
            continue
        _record(job, mark_attempt=True)

    with _lock:
        _running_ids.clear()
        _wait_queue = []
        _fns.clear()
    return {
        "interrupted": len(flipped),
        "job_ids": flipped,
        "jobs": records,
        "reclaim_candidates": len(records),
    }


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
    while True:
        _dispatcher_wakeup.wait(timeout=2.0)
        _dispatcher_wakeup.clear()
        to_start: list[tuple[str, Callable[[], dict[str, Any]]]] = []
        with _lock:
            # Drop slots whose workers vanished without clearing themselves.
            for jid in list(_running_ids):
                running = _jobs.get(jid)
                if running is None or running.status not in ACTIVE_STATUSES:
                    _running_ids.discard(jid)

            i = 0
            while i < len(_wait_queue):
                if len(_running_ids) >= _max_parallel():
                    break
                candidate = _wait_queue[i]
                job = _jobs.get(candidate)
                if job is None or job.status != "queued":
                    _wait_queue.pop(i)
                    _fns.pop(candidate, None)
                    continue
                fn = _fns.get(candidate)
                if fn is None:
                    job.status = "failed"
                    job.error = "Internal error: job callable missing from queue"
                    job.finished_at = time.time()
                    _wait_queue.pop(i)
                    try:
                        _persist(job)
                    except OSError:
                        pass
                    continue
                if not _can_start_locked(job.kind):
                    i += 1
                    continue
                _wait_queue.pop(i)
                _running_ids.add(candidate)
                to_start.append((candidate, fn))
                # do not increment i — next item shifted into place
        for job_id, fn in to_start:
            threading.Thread(
                target=_run_worker,
                args=(job_id, fn),
                name=f"mcp-job-{job_id}",
                daemon=True,
            ).start()


def _run_worker(job_id: str, fn: Callable[[], dict[str, Any]]) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            try:
                job = _load_from_disk(job_id)
                _jobs[job_id] = job
            except ValueError:
                _running_ids.discard(job_id)
                _dispatcher_wakeup.set()
                return
        # Cancelled / non-queued jobs must never start work.
        if job.status != "queued" or job_id in _cancel_requested or (
            job.meta or {}
        ).get("cancel_requested"):
            if job.status == "queued":
                job.status = "cancelled"
                job.error = (job.meta or {}).get("cancelled_reason") or (
                    "Cancelled by cancel_job"
                )
                job.finished_at = time.time()
                try:
                    _persist(job)
                except OSError:
                    pass
            _running_ids.discard(job_id)
            _fns.pop(job_id, None)
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
        if is_cancel_requested(job_id):
            raise JobCancelled("Cancelled by cancel_job")
        result = fn() or {}
        with _lock:
            job = _jobs[job_id]
            if job_id in _cancel_requested or (job.meta or {}).get(
                "cancel_requested"
            ):
                job.status = "cancelled"
                job.error = (job.meta or {}).get("cancelled_reason") or (
                    "Cancelled by cancel_job"
                )
                job.result = result if isinstance(result, dict) else {}
            else:
                job.result = result
                job.status = "completed"
                job.error = None
    except JobCancelled as exc:
        with _lock:
            job = _jobs[job_id]
            job.status = "cancelled"
            job.error = str(exc) or "Cancelled by cancel_job"
            job.result = {}
    except Exception as exc:  # noqa: BLE001
        with _lock:
            job = _jobs[job_id]
            if job_id in _cancel_requested or (job.meta or {}).get(
                "cancel_requested"
            ):
                job.status = "cancelled"
                job.error = (job.meta or {}).get("cancelled_reason") or (
                    f"Cancelled by cancel_job ({type(exc).__name__}: {exc})"
                )
            else:
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
            _running_ids.discard(job_id)
            _cancel_requested.discard(job_id)
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
    priority: int | None = None,
) -> Job:
    """Enqueue a background job. Never kills an existing run.

    If ``dedupe`` and a queued/running job shares ``queue_key``, return that
    job instead of spawning a duplicate. Otherwise insert into the wait
    queue by priority (higher first; same priority is FIFO) and return
    immediately with status=queued.

    Priority guidance:
      20   classify_leads (light; jumps ahead + may run beside one heavy)
      10+  scoped client / resolve / scrape work
       5   interactive unscoped user jobs
       0   backlog drain / auto housekeeping
    """
    meta = dict(meta or {})
    key = (queue_key or meta.get("queue_key") or make_queue_key(kind, meta)).strip()
    meta["queue_key"] = key
    if priority is None:
        if meta.get("backlog_drain"):
            priority = 0
        elif kind == "classify_leads":
            priority = 20
        elif _scope_fingerprint(meta) or kind in (
            "resolve_places",
            "run_leads",
            "scrape_maps",
        ):
            priority = 10
        else:
            priority = 5
    meta["priority"] = int(priority)
    meta["parallel_class"] = "light" if _is_light(kind) else "heavy"

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
        # Higher priority jumps ahead of lower-priority queued work.
        insert_at = len(_wait_queue)
        for i, jid in enumerate(_wait_queue):
            other = _jobs.get(jid)
            other_pri = int((other.meta or {}).get("priority") or 0) if other else 0
            if int(priority) > other_pri:
                insert_at = i
                break
        _wait_queue.insert(insert_at, job.id)
        position = _wait_queue.index(job.id) + 1
        job.progress = {
            **job.progress,
            "queue_position": position,
            "queue_key": key,
            "priority": int(priority),
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
            # Subprocess enrich only heartbeats liveness — always prefer sites table.
            jobs_total = domains or (sites_ok + sites_pending) or int(jobs_total or 0)
            jobs_done = sites_ok
            jobs_pending = sites_pending
            businesses_found = int(stats.get("businesses") or 0)
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
