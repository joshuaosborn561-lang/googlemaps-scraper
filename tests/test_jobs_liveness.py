"""Background job heartbeat, stall detection, and orphan sweep."""

from __future__ import annotations

import time
from pathlib import Path

from mcp_server import jobs


def test_sweep_orphaned_marks_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    job = jobs.Job(
        id="deadbeef0123",
        kind="run_leads",
        status="running",
        created_at=time.time() - 3600,
        started_at=time.time() - 3600,
        heartbeat_at=time.time() - 3600,
    )
    jobs._persist(job)

    out = jobs.sweep_orphaned_jobs()
    assert out["interrupted"] == 1
    got = jobs.get_job(job.id)
    assert got.status == "interrupted"
    assert "Orphaned" in (got.error or "")


def test_get_job_marks_stalled_without_heartbeat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs, "STALL_SECONDS", 30)
    job = jobs.Job(
        id="stalldead0001",
        kind="scrape_maps",
        status="running",
        created_at=time.time() - 120,
        started_at=time.time() - 120,
        heartbeat_at=time.time() - 120,
    )
    jobs._persist(job)
    with jobs._lock:
        jobs._jobs[job.id] = job

    got = jobs.get_job(job.id)
    assert got.status == "stalled"
    assert "heartbeat" in (got.error or "").lower()


def test_heartbeat_keeps_job_running(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs, "STALL_SECONDS", 30)
    job = jobs.Job(
        id="alivejob00001",
        kind="run_leads",
        status="running",
        created_at=time.time() - 10,
        started_at=time.time() - 10,
        heartbeat_at=time.time() - 10,
    )
    jobs._persist(job)
    with jobs._lock:
        jobs._jobs[job.id] = job

    jobs.heartbeat(job.id, stage="scrape", done=25, total=100, last_zip="75201")
    got = jobs.get_job(job.id)
    assert got.status == "running"
    assert got.progress.get("stage") == "scrape"
    assert got.progress.get("done") == 25
    assert got.heartbeat_at is not None
    assert time.time() - got.heartbeat_at < 5


def test_start_job_completes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    job = jobs.start_job("probe", lambda: {"ok": True}, meta={"x": 1})
    deadline = time.time() + 5
    while time.time() < deadline:
        got = jobs.get_job(job.id)
        if got.status in ("completed", "failed", "stalled", "interrupted"):
            break
        time.sleep(0.05)
    got = jobs.get_job(job.id)
    assert got.status == "completed"
    assert got.result == {"ok": True}


def test_persist_is_atomic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    job = jobs.Job(
        id="atomicwrite01",
        kind="probe",
        status="running",
        created_at=time.time(),
        heartbeat_at=time.time(),
    )
    jobs._persist(job)
    path = tmp_path / "atomicwrite01.json"
    assert path.exists()
    assert path.stat().st_size > 0
    assert not list(tmp_path.glob("*.tmp"))
    loaded = jobs._load_from_disk(job.id)
    assert loaded.id == job.id
    assert loaded.status == "running"


def test_load_rejects_empty_job_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    path = tmp_path / "emptyjob00001.json"
    path.write_text("", encoding="utf-8")
    try:
        jobs._load_from_disk("emptyjob00001")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "empty" in str(exc).lower()
