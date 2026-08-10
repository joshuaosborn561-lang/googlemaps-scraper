"""Multi-client scoping, priority queue, safe auto-resume, empty project_id."""

from __future__ import annotations

import json
import time
from pathlib import Path

from gmscraper import source_binding as sb
from gmscraper.store import Store
from mcp_server import jobs
from mcp_server import server


def _reset_queue_state() -> None:
    with jobs._lock:
        jobs._wait_queue.clear()
        jobs._running_ids.clear()
        jobs._jobs.clear()
        jobs._fns.clear()
        jobs._cancel_requested.clear()
    jobs._dispatcher_wakeup.set()


def test_empty_project_id_keeps_leads_default(monkeypatch) -> None:
    monkeypatch.setenv("LEADS_SUPABASE_PROJECT_ID", "kemvxzhcxvynmoutwdrh")
    monkeypatch.setenv("LEADS_SUPABASE_URL", "https://kemvxzhcxvynmoutwdrh.supabase.co")
    monkeypatch.setenv("LEADS_SUPABASE_SERVICE_ROLE_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_URL", "https://azpapwtnrbzywlnxxecz.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "maps-key")
    binding = sb.resolve_binding(
        project_id="",  # must not wipe LEADS default
        schema="public",
        table="operators",
        key_column="operator_address",
        address_column="operator_address",
        name_column="operator_name",
    )
    assert binding.project_id == "kemvxzhcxvynmoutwdrh"
    assert "kemvxzhcxvynmoutwdrh" in binding.supabase_url


def test_default_leads_project_for_operators() -> None:
    assert (
        server._default_leads_project_id("operators", "")
        == "kemvxzhcxvynmoutwdrh"
    )
    assert server._default_leads_project_id("operators", "custom") == "custom"
    assert server._default_leads_project_id("businesses", "") == ""


def test_pending_sites_scoped_by_state(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = Store(str(db))
    store.upsert_businesses(
        [
            {
                "place_id": "nj1",
                "name": "NJ Dealer",
                "domain": "njdealer.com",
                "state": "NJ",
                "city": "Clifton",
                "main_category": "Car dealer",
                "plan_id": "carlos-plan",
                "client_tag": "basco",
            },
            {
                "place_id": "tx1",
                "name": "TX GC",
                "domain": "txgc.com",
                "state": "TX",
                "city": "Dallas",
                "main_category": "General contractor",
                "plan_id": "kyle-plan",
                "client_tag": "peterson",
            },
        ]
    )
    store.queue_sites()
    nj = store.pending_sites(state="NJ")
    assert nj == ["njdealer.com"]
    tx = store.pending_sites(client_tag="peterson")
    assert tx == ["txgc.com"]
    by_plan = store.pending_sites(plan_id="carlos-plan")
    assert by_plan == ["njdealer.com"]


def test_priority_queue_scoped_jumps_backlog(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    order: list[str] = []

    def make(name: str, delay: float = 0.2):
        def fn() -> dict:
            order.append(f"start:{name}")
            time.sleep(delay)
            order.append(f"end:{name}")
            return {"name": name}

        return fn

    # Hold the runner with a low-priority job that is already running.
    holder = jobs.start_job(
        "enrich_sites",
        make("holder", 0.35),
        meta={"limit": 200, "backlog_drain": True},
        queue_key="enrich_sites:backlog:200",
        priority=0,
        dedupe=False,
    )
    # Wait until holder is running so queue insertion is tested.
    deadline = time.time() + 3
    while time.time() < deadline and jobs.get_job(holder.id).status != "running":
        time.sleep(0.02)
    assert jobs.get_job(holder.id).status == "running"

    backlog = jobs.start_job(
        "enrich_sites",
        make("backlog"),
        meta={"limit": 200, "backlog_drain": True},
        queue_key="enrich_sites:backlog:other",
        priority=0,
        dedupe=False,
    )
    scoped = jobs.start_job(
        "enrich_sites",
        make("scoped"),
        meta={"limit": 50, "state": "NJ", "client_tag": "basco"},
        queue_key="enrich_sites:limit=50:state=NJ",
        priority=10,
        dedupe=False,
    )
    # Scoped should be ahead of backlog in the wait queue.
    with jobs._lock:
        q = list(jobs._wait_queue)
    assert q.index(scoped.id) < q.index(backlog.id)

    deadline = time.time() + 5
    while time.time() < deadline:
        if all(
            jobs.get_job(j.id).status in ("completed", "failed")
            for j in (holder, backlog, scoped)
        ):
            break
        time.sleep(0.05)
    assert "start:scoped" in order
    assert order.index("start:scoped") < order.index("start:backlog")


def test_sweep_skips_global_enrich_and_paid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
    _reset_queue_state()

    paid = jobs.Job(
        id="paidjob000001",
        kind="enrich_waterfall",
        status="interrupted",
        created_at=time.time() - 10,
        finished_at=time.time() - 5,
        meta={"max_tier": "leadmagic", "need": "email"},
    )
    jobs._persist(paid)
    global_enrich = jobs.Job(
        id="enrichglob001",
        kind="enrich_sites",
        status="interrupted",
        created_at=time.time() - 10,
        finished_at=time.time() - 5,
        meta={"limit": 0},
    )
    jobs._persist(global_enrich)
    scoped = jobs.Job(
        id="enrichscope01",
        kind="enrich_sites",
        status="interrupted",
        created_at=time.time() - 10,
        finished_at=time.time() - 5,
        meta={"limit": 50, "state": "NJ"},
    )
    jobs._persist(scoped)

    swept = jobs.sweep_orphaned_jobs()
    ids = {r["id"] for r in swept.get("jobs") or []}
    assert "enrichscope01" in ids
    assert "enrichglob001" not in ids
    assert "paidjob000001" not in ids
    paid_disk = json.loads((tmp_path / "paidjob000001.json").read_text())
    assert paid_disk["meta"].get("needs_approval") is True
    glob_disk = json.loads((tmp_path / "enrichglob001.json").read_text())
    assert glob_disk["meta"].get("resume_skipped_reason") == "global_enrich_or_backlog"


def test_classify_has_more(tmp_path: Path, monkeypatch) -> None:
    from gmscraper import classify

    db = tmp_path / "c.db"
    store = Store(str(db))
    for i in range(5):
        store.upsert_businesses(
            [
                {
                    "place_id": f"p{i}",
                    "name": f"Biz {i}",
                    "domain": f"biz{i}.com",
                    "state": "TX",
                    "main_category": "Property management company",
                }
            ]
        )
    store.queue_sites()
    for i in range(5):
        store.save_site(f"biz{i}.com", "ok", "We manage commercial properties.", [], None)

    class FakeLLM:
        model = "fake"

        def json_chat(self, *a, **k):
            return {"in_icp": True, "confidence": 0.9, "reason": "ok"}

    out = classify.run(
        store,
        FakeLLM(),
        "commercial property managers in DFW",
        workers=1,
        limit=2,
        require_geo=False,
        state="TX",
    )
    assert out["total_eligible"] == 5
    assert out["processed"] == 2
    assert out["remaining"] == 3
    assert out["has_more"] is True


def test_make_queue_key_includes_scope() -> None:
    a = jobs.make_queue_key(
        "enrich_sites", {"limit": 50, "state": "NJ", "client_tag": "basco"}
    )
    b = jobs.make_queue_key("enrich_sites", {"limit": 50})
    assert "state=NJ" in a
    assert a != b
