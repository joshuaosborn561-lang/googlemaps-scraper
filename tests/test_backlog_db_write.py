"""MCP_BACKLOG_WORKERS + process-wide DB write semaphore."""

from __future__ import annotations

import inspect
import json
import threading
import time

from gmscraper import db_write, enrich_site, gc_sync, scrape, supabase_sync, team_contacts
from gmscraper.config import Settings
from mcp_server import server


class _FakeResp:
    status = 201

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_backlog_workers_default_is_six(monkeypatch) -> None:
    monkeypatch.delenv("MCP_BACKLOG_WORKERS", raising=False)
    assert server._backlog_workers() == 6


def test_backlog_workers_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("MCP_BACKLOG_WORKERS", "8")
    assert server._backlog_workers() == 8
    monkeypatch.setenv("MCP_BACKLOG_WORKERS", "0")
    assert server._backlog_workers() == 1
    monkeypatch.setenv("MCP_BACKLOG_WORKERS", "nope")
    assert server._backlog_workers() == 6


def test_backlog_drain_uses_env_workers_not_hardcoded_three() -> None:
    src = inspect.getsource(server._start_backlog_drain)
    assert "_backlog_workers()" in src
    assert "_enrich_sites_via_subprocess(batch, 3)" not in src
    assert '"workers": 3' not in src
    assert "backlog_workers" in src


def test_enrich_subprocess_honors_backlog_worker_count() -> None:
    assert server._enrich_subprocess_workers(6) == 6
    assert server._enrich_subprocess_workers(1) == 1
    assert server._enrich_subprocess_workers(0) == 3
    src = inspect.getsource(server._enrich_sites_via_subprocess)
    assert "min(int(workers or 3), 3)" not in src
    assert "_enrich_subprocess_workers" in src


def test_stage_thread_pool_defaults_unchanged() -> None:
    assert inspect.signature(scrape.run).parameters["workers"].default == 8
    assert inspect.signature(enrich_site.run).parameters["workers"].default == 12
    assert inspect.signature(enrich_site.crawl_team_pages).parameters["workers"].default == 12
    assert inspect.signature(team_contacts.run).parameters["workers"].default == 8
    assert Settings().classify_workers == 4
    assert inspect.signature(server.enrich_sites).parameters["workers"].default == 20
    assert inspect.signature(server.crawl_team_pages).parameters["workers"].default == 12
    assert inspect.signature(server.classify_leads).parameters["workers"].default == 4
    # User-facing unscoped SQLite enrich still caps at 3; backlog is separate.
    assert "20 if capture else 3" in inspect.getsource(server.enrich_sites)


def test_db_write_concurrency_default_is_six(monkeypatch) -> None:
    monkeypatch.delenv("DB_WRITE_CONCURRENCY", raising=False)
    db_write.reset()
    assert db_write.concurrency() == 6
    assert db_write.DEFAULT_CONCURRENCY == 6
    monkeypatch.setenv("DB_WRITE_CONCURRENCY", "4")
    db_write.reset()
    assert db_write.concurrency() == 4
    monkeypatch.setenv("DB_WRITE_CONCURRENCY", "0")
    assert db_write.concurrency() == 1
    monkeypatch.setenv("DB_WRITE_CONCURRENCY", "x")
    assert db_write.concurrency() == 6


def test_write_semaphore_shared_across_sync_modules(monkeypatch) -> None:
    monkeypatch.setenv("DB_WRITE_CONCURRENCY", "2")
    db_write.reset()

    inflight = 0
    max_inflight = 0
    lock = threading.Lock()
    gate = threading.Event()

    def fake_urlopen(_req, timeout=120):
        nonlocal inflight, max_inflight
        with lock:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        assert gate.wait(2.0)
        with lock:
            inflight -= 1
        return _FakeResp()

    monkeypatch.setattr(supabase_sync.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gc_sync.request, "urlopen", fake_urlopen)

    errors: list[BaseException] = []

    def post_sb() -> None:
        try:
            supabase_sync._request("POST", "http://example.test/rest/v1/leads", "k", body=[{"n": 1}])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def post_gc() -> None:
        try:
            gc_sync._request("POST", "companies", "k", "http://example.test", body=[{"n": 1}])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=post_sb) for _ in range(3)]
    threads.extend(threading.Thread(target=post_gc) for _ in range(3))
    for t in threads:
        t.start()

    deadline = time.time() + 2.0
    while time.time() < deadline:
        with lock:
            if max_inflight >= 2:
                break
        time.sleep(0.01)
    time.sleep(0.15)
    with lock:
        assert inflight == 2
        assert max_inflight == 2
    gate.set()
    for t in threads:
        t.join(2.0)
        assert not t.is_alive()
    assert errors == []
    assert max_inflight == 2


def test_reads_do_not_take_a_write_slot(monkeypatch) -> None:
    monkeypatch.setenv("DB_WRITE_CONCURRENCY", "1")
    db_write.reset()

    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with db_write.slot():
            held.set()
            release.wait(2.0)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(1.0)

    called = threading.Event()

    class GetResp(_FakeResp):
        status = 200

        def read(self) -> bytes:
            called.set()
            return b"[]"

    monkeypatch.setattr(supabase_sync.request, "urlopen", lambda *_a, **_k: GetResp())
    status, body = supabase_sync._request(
        "GET", "http://example.test/rest/v1/leads?select=id", "k"
    )
    assert status == 200
    assert json.loads(body or "[]") == []
    assert called.is_set()
    release.set()
    t.join(2.0)


def test_health_reports_backlog_workers_and_db_write_cap(monkeypatch) -> None:
    monkeypatch.setenv("MCP_BACKLOG_WORKERS", "7")
    monkeypatch.setenv("DB_WRITE_CONCURRENCY", "5")
    db_write.reset()
    monkeypatch.setattr(
        "gmscraper.apify_contacts.apify_token_valid", lambda *_a, **_k: False
    )
    payload = json.loads(server.health())
    assert payload["backlog_workers"] == 7
    assert payload["db_write_concurrency"] == 5
    assert "backlog_drain" in payload
