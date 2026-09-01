"""sample_leads / classify gap reporting / export helpers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from gmscraper import classify, export
from gmscraper.store import Store


def _seed(store: Store) -> None:
    with store.conn as c:
        c.execute(
            """INSERT INTO businesses
               (place_id, name, city, state, domain, website, main_category, types,
                latitude, longitude, source_zip, source_category)
               VALUES
               ('p1','Acme GC','Dallas','TX','acme.example','https://acme.example',
                'general contractor','["general contractor"]',32.7,-96.8,'75001','general contractor'),
               ('p2','No Site LLC','Dallas','TX',NULL,NULL,
                'general contractor','[]',32.7,-96.8,'75001','general contractor'),
               ('p3','Roof Co','Dallas','TX','roof.example','https://roof.example',
                'roofing contractor','["roofing contractor"]',32.7,-96.8,'75002','roofing contractor')"""
        )
        c.execute(
            "INSERT INTO sites (domain, status, text, pages, n_chars) VALUES "
            "('acme.example','ok','We are a commercial GC. Contact info@acme.example','[]',40),"
            "('roof.example','ok','Roofing specialists','[]',20)"
        )
        c.execute(
            "INSERT INTO emails (domain, email, source) VALUES "
            "('acme.example','info@acme.example','website'),"
            "('acme.example','bob@acme.example','website')"
        )
        c.execute(
            """INSERT INTO verdicts (place_id, in_icp, confidence, reason, model)
               VALUES ('p1', 1, 0.9, 'commercial GC mentioned', 'test'),
                      ('p3', 0, 0.8, 'roofing exclusion', 'test')"""
        )
    store.backfill_legacy_verdicts()


def test_sample_leads_icp_with_email(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    rows = export.sample_leads(
        store, limit=20, icp_only=True, with_email=True, order="random",
        client_tag="basco",
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "Acme GC"
    assert rows[0]["email"]
    assert rows[0]["icp_reason"]
    assert rows[0]["in_icp"] == "yes"


def test_stats_unclassifiable_gap(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    stats = store.stats()
    assert stats["businesses"] == 3
    assert stats["classifiable_with_site"] == 2
    assert stats["unclassifiable_no_site"] == 1
    assert stats["classified"] == 2
    assert stats["classified_pct_of_eligible"] == 100.0


def test_classify_empty_reason(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)

    class DummyLLM:
        model = "dummy"

        def json_chat(self, *a, **k):  # pragma: no cover
            raise AssertionError("should not call LLM when nothing eligible")

    res = classify.run(
        store, DummyLLM(), "commercial general contractors", client_tag="basco"
    )
    assert res["done"] == 0
    assert "nothing eligible" in res["reason"]
    assert "already" in res["reason"] or "no site text" in res["reason"]


def test_iter_leads_columns(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.db")
    _seed(store)
    rows = export.fetch_leads(store, icp_only=True, with_email=True, client_tag="basco")
    assert len(rows) == 1
    for col in export.COLUMNS:
        assert col in rows[0]
