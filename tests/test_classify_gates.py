"""Classify is a short yes/no checklist plus cheap category skips."""

from __future__ import annotations

from pathlib import Path

from gmscraper import classify
from gmscraper.store import Store


def test_prompt_is_simple_checklist() -> None:
    assert "every question is yes" in classify.PROMPT
    assert "QUESTIONS:" in classify.PROMPT
    assert "Do not invent extra rules" in classify.SYSTEM
    assert "EXCLUDE" not in classify.PROMPT
    assert "obviously not" not in classify.PROMPT
    assert "car dealership" not in classify.SYSTEM.lower()
    assert "Honda" not in classify.PROMPT


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


def test_category_gate_skips_repair_before_llm(tmp_path: Path) -> None:
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
            for line in prompt.splitlines():
                if line.startswith("Name:"):
                    calls.append(line.split(":", 1)[1].strip())
                    break
            return {"in_icp": True, "confidence": 0.9, "reason": "Honda dealer"}

    icp = (
        "1. Is this a car dealership?\n"
        "2. Is it one of these brands: Honda, Toyota?"
    )
    out = classify.run(
        store,
        FakeLLM(),  # type: ignore[arg-type]
        icp,
        workers=1,
        force=True,
        exclude_categories="auto repair shop",
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


def test_min_confidence_only_applies_when_set(tmp_path: Path) -> None:
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
            return {"in_icp": True, "confidence": 0.2, "reason": "looks like a dealer"}

    out = classify.run(
        store,
        SoftLLM(),  # type: ignore[arg-type]
        "1. Is this a car dealership?",
        workers=1,
        force=True,
        client_tag="basco",
    )
    assert out["in_icp"] == 1

    out2 = classify.run(
        store,
        SoftLLM(),  # type: ignore[arg-type]
        "1. Is this a car dealership?",
        workers=1,
        force=True,
        min_confidence=0.55,
        client_tag="basco",
    )
    assert out2["in_icp"] == 0
