"""Live progress fields + serial queue / dedupe for background jobs."""

from __future__ import annotations

import time
from pathlib import Path

from mcp_server import jobs


def test_live_progress_from_progress_dict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    job = jobs.Job(
        id="progjob000001",
        kind="run_leads",
        status="running",
        created_at=time.time() - 100,
        started_at=time.time() - 100,
        heartbeat_at=time.time(),
        progress={
            "stage": "scrape",
            "done": 40,
            "total": 100,
            "jobs_total": 100,
            "jobs_done": 40,
            "jobs_pending": 60,
            "businesses_found": 250,
            "updated_at": time.time(),
        },
        meta={"jobs_done_at_start": 0},
    )
    live = jobs.live_progress(job)
    assert live["jobs_total"] == 100
    assert live["jobs_done"] == 40
    assert live["jobs_pending"] == 60
    assert live["businesses_found"] == 250
    assert live["percent_complete"] == 40.0
    assert live["updated_at"] is not None
    assert live["eta_seconds"] is not None
    assert live["eta_seconds"] > 0


def _reset_queue_state() -> None:
    with jobs._lock:
        jobs._wait_queue.clear()
        jobs._running_id = None
        jobs._jobs.clear()
        jobs._fns.clear()
    jobs._dispatcher_wakeup.set()


def test_queue_dedupes_same_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    started = []

    def slow() -> dict:
        started.append(1)
        time.sleep(0.4)
        return {"ok": True}

    a = jobs.start_job(
        "run_leads",
        slow,
        meta={"plan_path": "/tmp/plan-a.json"},
        queue_key="run_leads:/tmp/plan-a.json",
    )
    b = jobs.start_job(
        "run_leads",
        slow,
        meta={"plan_path": "/tmp/plan-a.json"},
        queue_key="run_leads:/tmp/plan-a.json",
    )
    assert a.id == b.id
    assert (b.progress or {}).get("attached") or (b.progress or {}).get("deduped")

    deadline = time.time() + 5
    while time.time() < deadline:
        got = jobs.get_job(a.id)
        if got.status in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert jobs.get_job(a.id).status == "completed"
    assert len(started) == 1  # only one worker


def test_queue_serializes_different_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    order: list[str] = []

    def make(name: str):
        def fn() -> dict:
            order.append(f"start:{name}")
            time.sleep(0.25)
            order.append(f"end:{name}")
            return {"name": name}

        return fn

    j1 = jobs.start_job("probe", make("a"), meta={"plan_path": "a"}, queue_key="probe:a")
    j2 = jobs.start_job("probe", make("b"), meta={"plan_path": "b"}, queue_key="probe:b")
    assert j1.id != j2.id

    deadline = time.time() + 5
    while time.time() < deadline:
        s1 = jobs.get_job(j1.id).status
        s2 = jobs.get_job(j2.id).status
        if s1 == "completed" and s2 == "completed":
            break
        time.sleep(0.05)

    assert jobs.get_job(j1.id).status == "completed"
    assert jobs.get_job(j2.id).status == "completed"
    # Serial: a fully finishes before b starts.
    assert order == ["start:a", "end:a", "start:b", "end:b"]
