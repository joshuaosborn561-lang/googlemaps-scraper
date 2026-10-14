"""Finished-job eviction, persist-load, and status after a cold restart."""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

from gmscraper.config import Settings
from mcp_server import jobs
from mcp_server import server


def _reset_queue_state() -> None:
    with jobs._lock:
        jobs._wait_queue.clear()
        jobs._running_id = None
        jobs._jobs.clear()
        jobs._fns.clear()
        jobs._cancel_requested.clear()
    jobs._dispatcher_wakeup.set()


def _finished_job(
    job_id: str,
    *,
    finished_at: float,
    result: dict | None = None,
    status: str = "completed",
    kind: str = "probe",
    error: str | None = None,
) -> jobs.Job:
    return jobs.Job(
        id=job_id,
        kind=kind,
        status=status,
        created_at=finished_at - 1,
        started_at=finished_at - 1,
        finished_at=finished_at,
        heartbeat_at=finished_at,
        error=error,
        result=result or {"ok": True, "payload": f"heavy-{job_id}"},
        meta={"queue_key": f"{kind}:{job_id}"},
        progress={"stage": status, "done": 10, "total": 10},
    )


def test_classify_workers_default_is_four() -> None:
    assert Settings().classify_workers == 4
    params = inspect.signature(server.classify_leads).parameters
    assert params["workers"].default == 4


def test_finished_job_drops_result_from_memory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    heavy = {"ok": True, "rows": list(range(500)), "note": "keep-on-disk"}
    job = jobs.start_job("probe", lambda: heavy, meta={"x": 1})
    deadline = time.time() + 5
    while time.time() < deadline:
        if jobs.get_job(job.id).status in jobs.TERMINAL_STATUSES:
            break
        time.sleep(0.05)

    got = jobs.get_job(job.id)
    assert got.status == "completed"
    assert got.result == heavy

    with jobs._lock:
        mem = jobs._jobs.get(job.id)
        assert mem is not None
        assert mem.result == {}
        assert mem.status == "completed"

    disk = json.loads((tmp_path / f"{job.id}.json").read_text())
    assert disk["result"] == heavy


def test_get_job_loads_persisted_result_after_eviction(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    payload = {"leads": 42, "csv": "/tmp/out.csv", "sample": ["a", "b"]}
    job = _finished_job("evictload001", finished_at=time.time(), result=payload)
    jobs._persist(job)
    with jobs._lock:
        jobs._jobs[job.id] = job
    jobs._offload_finished_job(job.id)

    with jobs._lock:
        assert jobs._jobs[job.id].result == {}

    got = jobs.get_job(job.id)
    assert got.result == payload
    assert got.status == "completed"
    assert got.progress.get("done") == 10


def test_caps_finished_jobs_at_50_keeps_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs, "MAX_FINISHED_IN_MEMORY", 50)
    _reset_queue_state()

    base = time.time() - 200
    for i in range(55):
        job = _finished_job(f"cap{i:010d}", finished_at=base + i, result={"i": i})
        jobs._persist(job)
        with jobs._lock:
            jobs._jobs[job.id] = job
        jobs._offload_finished_job(job.id)

    running = jobs.Job(
        id="stillrunning01",
        kind="run_leads",
        status="running",
        created_at=time.time(),
        started_at=time.time(),
        heartbeat_at=time.time(),
        result={},
        meta={"queue_key": "run_leads:live"},
    )
    jobs._persist(running)
    with jobs._lock:
        jobs._jobs[running.id] = running

    with jobs._lock:
        mem_ids = set(jobs._jobs)
        finished_ids = {
            jid for jid, j in jobs._jobs.items() if j.status in jobs.TERMINAL_STATUSES
        }
    assert running.id in mem_ids
    assert len(finished_ids) == 50
    assert "cap0000000000" not in mem_ids
    assert "cap0000000004" not in mem_ids
    assert "cap0000000054" in mem_ids

    oldest = jobs.get_job("cap0000000000")
    assert oldest.status == "completed"
    assert oldest.result == {"i": 0}
    assert jobs.get_job(running.id).status == "running"


def test_status_after_eviction_matches_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    job = _finished_job(
        "statusafter01",
        finished_at=time.time(),
        status="failed",
        error="RuntimeError: boom",
        result={"traceback": "line1\nline2"},
    )
    jobs._persist(job)
    with jobs._lock:
        jobs._jobs[job.id] = job
    jobs._offload_finished_job(job.id)
    with jobs._lock:
        jobs._jobs.pop(job.id, None)

    got = jobs.get_job(job.id)
    disk = json.loads((tmp_path / f"{job.id}.json").read_text())
    public = got.to_public()
    assert public["status"] == disk["status"] == "failed"
    assert public["error"] == disk["error"] == "RuntimeError: boom"
    assert public["result"] == disk["result"] == {"traceback": "line1\nline2"}
    assert public["kind"] == disk["kind"]
    assert public["progress"] == disk["progress"]


def test_get_job_status_tool_after_eviction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    job = _finished_job(
        "toolstatus001",
        finished_at=time.time(),
        result={"status": "completed", "leads": 3},
    )
    jobs._persist(job)
    with jobs._lock:
        jobs._jobs[job.id] = job
    jobs._offload_finished_job(job.id)
    with jobs._lock:
        jobs._jobs.pop(job.id, None)

    raw = server.get_job_status(job.id)
    payload = json.loads(raw)
    assert payload["status"] == "completed"
    assert payload["result"] == {"status": "completed", "leads": 3}
    assert payload["percent_complete"] == 100.0
    assert payload["id"] == job.id


def test_sweep_after_restart_does_not_keep_full_result(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    heavy = {"rows": list(range(200)), "stats": {"businesses": 99}}
    job = jobs.Job(
        id="orphanrestart1",
        kind="run_leads",
        status="running",
        created_at=time.time() - 120,
        started_at=time.time() - 120,
        heartbeat_at=time.time() - 120,
        result=heavy,
        meta={"queue_key": "run_leads:/tmp/plan.json", "plan_path": "/tmp/plan.json"},
        progress={"stage": "scrape", "done": 4, "total": 20},
    )
    jobs._persist(job)
    _reset_queue_state()

    out = jobs.sweep_orphaned_jobs()
    assert out["interrupted"] == 1
    assert job.id in out["job_ids"]

    with jobs._lock:
        mem = jobs._jobs.get(job.id)
        assert mem is not None
        assert mem.status == "interrupted"
        assert mem.result == {}

    got = jobs.get_job(job.id)
    assert got.status == "interrupted"
    assert "Orphaned" in (got.error or "")
    assert got.result == heavy
    assert got.progress.get("stage") == "scrape"

    disk = json.loads((tmp_path / f"{job.id}.json").read_text())
    assert disk["status"] == "interrupted"
    assert disk["result"] == heavy


def test_completed_job_survives_cold_start_without_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    payload = {"ok": True, "leads": 7}
    job = _finished_job("coldstart0001", finished_at=time.time(), result=payload)
    jobs._persist(job)
    _reset_queue_state()

    got = jobs.get_job(job.id)
    assert got.status == "completed"
    assert got.result == payload
    with jobs._lock:
        assert jobs._jobs[job.id].result == {}
