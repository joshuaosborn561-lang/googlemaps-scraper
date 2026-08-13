"""Deterministic classify gates + strict prompt contract."""

from __future__ import annotations

from pathlib import Path

from gmscraper import classify
from gmscraper.store import Store


def test_strict_prompt_rejects_padding() -> None:
    assert "ONLY if it clearly matches" in classify.PROMPT
    assert "NONE of the EXCLUDE" in classify.PROMPT
    assert "never pad the list" in classify.SYSTEM.lower() or "Never pad" in classify.SYSTEM
    assert "EXCLUSIONS are hard" in classify.SYSTEM


def test_parse_exclude_categories() -> None:
    assert classify._parse_exclude_categories(
        "auto repair shop, Auto Parts Store\ntire shop"
    ) == ["auto repair shop", "auto parts store", "tire shop"]


def test_category_excluded_matches_main_and_types() -> None:
    row = {
        "main_category": "Auto repair shop",
        "types": '["Car repair and maintenance service","Auto repair shop"]',
    }
    assert classify._category_excluded(row, ["auto parts store", "auto repair"]) == (
        "auto repair"
    )
    assert classify._category_excluded(row, ["dealership"]) is None


def test_category_gate_rejects_before_llm(tmp_path: Path, monkeypatch) -> None:
    store = Store(str(tmp_path / "t.db"))
    store.upsert_businesses(
        [
            {
                "place_id": "repair1",
                "name": "Joe's Auto Repair",
                "domain": "joesrepair.example",
                "main_category": "Auto repair shop",
                "types": "[]",
                "client_tag": "basco",
            },
            {
                "place_id": "dealer1",
                "name": "Honda of Clifton",
                "domain": "honda.example",
                "main_category": "Honda dealer",
                "types": '["Car dealer"]',
                "client_tag": "basco",
            },
        ]
    )
    store.queue_sites()
    store.save_site("joesrepair.example", "ok", "we fix cars", [], None)
    store.save_site("honda.example", "ok", "new honda cars", [], None)

    calls: list[str] = []

    class FakeLLM:
        model = "test-model"

        def json_chat(self, system, prompt, schema):
            # Extract name from prompt for assertion.
            for line in prompt.splitlines():
                if line.startswith("Name:"):
                    calls.append(line.split(":", 1)[1].strip())
                    break
            return {"in_icp": True, "confidence": 0.9, "reason": "franchise dealer"}

    out = classify.run(
        store,
        FakeLLM(),  # type: ignore[arg-type]
        "INCLUDE: franchise dealers\nEXCLUDE: repair shops",
        workers=1,
        force=True,
        exclude_categories="auto repair shop",
        min_confidence=0.55,
        client_tag="basco",
    )
    assert out["category_rejected"] == 1
    assert out["in_icp"] == 1
    assert calls == ["Honda of Clifton"]
    v_repair = store.conn.execute(
        "SELECT in_icp, reason, model FROM verdicts WHERE place_id='repair1'"
    ).fetchone()
    assert int(v_repair["in_icp"]) == 0
    assert "excluded_category" in v_repair["reason"]
    assert v_repair["model"] == "category_gate"


def test_min_confidence_floor_rejects_weak_yes(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "t.db"))
    store.upsert_businesses(
        [
            {
                "place_id": "weak1",
                "name": "Maybe Motors",
                "domain": "maybe.example",
                "main_category": "Car dealer",
                "client_tag": "basco",
            }
        ]
    )
    store.queue_sites()
    store.save_site("maybe.example", "ok", "cars", [], None)

    class SoftLLM:
        model = "soft"

        def json_chat(self, system, prompt, schema):
            return {"in_icp": True, "confidence": 0.2, "reason": "unsure"}

    out = classify.run(
        store,
        SoftLLM(),  # type: ignore[arg-type]
        "INCLUDE: franchise\nEXCLUDE: used only",
        workers=1,
        force=True,
        min_confidence=0.55,
        client_tag="basco",
    )
    assert out["done"] == 1
    assert out["in_icp"] == 0
    row = store.conn.execute(
        "SELECT in_icp, confidence FROM verdicts WHERE place_id='weak1'"
    ).fetchone()
    assert int(row["in_icp"]) == 0
    assert float(row["confidence"]) == 0.2
