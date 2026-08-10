"""Live progress fields + serial queue / dedupe for background jobs."""

from __future__ import annotations

import threading
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
        jobs._running_ids.clear()
        jobs._jobs.clear()
        jobs._fns.clear()
        jobs._cancel_requested.clear()
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


def test_classify_runs_beside_heavy_job(tmp_path: Path, monkeypatch) -> None:
    """Light kinds (classify_leads) must not wait behind a long heavy runner."""
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    monkeypatch.setenv("MCP_MAX_PARALLEL_JOBS", "2")
    _reset_queue_state()

    overlap: list[str] = []
    barrier = threading.Event()

    def heavy() -> dict:
        overlap.append("heavy_start")
        barrier.wait(timeout=2.0)
        overlap.append("heavy_end")
        return {"ok": True}

    def classify() -> dict:
        overlap.append("classify_start")
        # Prove we started while heavy is still running.
        assert "heavy_start" in overlap and "heavy_end" not in overlap
        barrier.set()
        overlap.append("classify_end")
        return {"ok": True}

    h = jobs.start_job(
        "resolve_via_serp",
        heavy,
        meta={"table": "operators"},
        queue_key="heavy:serp",
        priority=10,
    )
    # Let heavy claim the slot first.
    time.sleep(0.05)
    c = jobs.start_job(
        "classify_leads",
        classify,
        meta={"limit": 100},
        queue_key="classify:test",
        priority=20,
    )

    deadline = time.time() + 5
    while time.time() < deadline:
        if (
            jobs.get_job(h.id).status == "completed"
            and jobs.get_job(c.id).status == "completed"
        ):
            break
        time.sleep(0.05)

    assert jobs.get_job(h.id).status == "completed"
    assert jobs.get_job(c.id).status == "completed"
    assert "classify_start" in overlap
    # Classify overlapped heavy (did not wait for heavy_end first).
    assert overlap.index("classify_start") < overlap.index("heavy_end")


def test_two_heavy_jobs_still_serial(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    monkeypatch.setenv("MCP_MAX_PARALLEL_JOBS", "2")
    _reset_queue_state()

    order: list[str] = []

    def make(name: str):
        def fn() -> dict:
            order.append(f"start:{name}")
            time.sleep(0.2)
            order.append(f"end:{name}")
            return {"name": name}

        return fn

    a = jobs.start_job(
        "resolve_via_serp", make("a"), meta={}, queue_key="heavy:a", priority=10
    )
    b = jobs.start_job(
        "run_owner_lane", make("b"), meta={}, queue_key="heavy:b", priority=10
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        if (
            jobs.get_job(a.id).status == "completed"
            and jobs.get_job(b.id).status == "completed"
        ):
            break
        time.sleep(0.05)
    assert order == ["start:a", "end:a", "start:b", "end:b"]


def test_cancel_queued_job_never_starts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    started: list[str] = []

    def slow() -> dict:
        started.append("ran")
        time.sleep(0.5)
        return {"ok": True}

    # Two heavies — enrich_sites is light-parallel and would not block.
    blocker = jobs.start_job(
        "resolve_via_serp", slow, meta={"limit": 1}, queue_key="heavy:blocker"
    )
    target = jobs.start_job(
        "apify_contact_crawl",
        lambda: started.append("apify") or {"ok": True},
        meta={"domains_chars": 100},
        queue_key="apify_contact_crawl:test",
    )
    assert jobs.queue_position(target.id) == 1

    out = jobs.cancel_job(target.id, reason="user kill")
    assert out["ok"] is True
    assert out["removed_from_queue"] is True
    assert jobs.get_job(target.id).status == "cancelled"
    assert jobs.queue_position(target.id) is None

    deadline = time.time() + 5
    while time.time() < deadline:
        if jobs.get_job(blocker.id).status in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert "apify" not in started
    assert jobs.get_job(target.id).status == "cancelled"
