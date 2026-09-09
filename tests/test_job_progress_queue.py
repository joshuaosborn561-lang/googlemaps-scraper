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


def test_live_progress_includes_row_errors() -> None:
    job = jobs.Job(
        id="errjob00000001",
        kind="resolve_places",
        status="running",
        created_at=time.time(),
        started_at=time.time(),
        heartbeat_at=time.time(),
        progress={
            "stage": "resolve_places",
            "done": 10,
            "total": 1400,
            "errors": 10,
            "resolved": 0,
            "last_error": "BindingError: rpc/pp_patch_row failed (404)",
            "error_samples": [
                {"key": "Acme LLC", "error": "BindingError: rpc/pp_patch_row failed (404)"}
            ],
            "updated_at": time.time(),
        },
    )
    live = jobs.live_progress(job)
    assert live["errors"] == 10
    assert live["resolved"] == 0
    assert "pp_patch_row" in str(live["last_error"])
    assert live["error_samples"][0]["key"] == "Acme LLC"


def test_live_progress_flags_done_exceeding_total() -> None:
    job = jobs.Job(
        id="loopjob0000001",
        kind="resolve_places",
        status="running",
        created_at=time.time(),
        started_at=time.time(),
        heartbeat_at=time.time(),
        progress={
            "stage": "resolve_places",
            "done": 65550,
            "total": 168,
            "requests": 65550,
            "request_cap": 504,
            "stop_reason": "requests_exceed_3x_pending",
            "updated_at": time.time(),
        },
    )
    live = jobs.live_progress(job)
    assert live["done"] == 65550
    assert live["total"] == 168
    assert live["done_exceeds_total"] is True
    assert live["stop_reason"] == "requests_exceed_3x_pending"
    assert live["request_cap"] == 504


def test_is_cancel_requested_sees_running_id_without_tls() -> None:
    _reset_queue_state()
    jobs._tls.job_id = ""
    jobs._running_id = "runningjob0001"
    jobs._cancel_requested.add("runningjob0001")
    try:
        assert jobs.is_cancel_requested() is True
        assert jobs.is_cancel_requested("runningjob0001") is True
    finally:
        _reset_queue_state()


def _reset_queue_state() -> None:
    with jobs._lock:
        jobs._wait_queue.clear()
        jobs._running_id = None
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


def test_cancel_queued_job_never_starts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    started: list[str] = []

    def slow() -> dict:
        started.append("ran")
        time.sleep(0.5)
        return {"ok": True}

    blocker = jobs.start_job(
        "enrich_sites", slow, meta={"limit": 1}, queue_key="enrich_sites:limit=1"
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
