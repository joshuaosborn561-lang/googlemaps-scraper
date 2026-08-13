"""Geography targeting: explicit ZIPs, radius, exclusions."""

from __future__ import annotations

import json
from pathlib import Path

from gmscraper.brief import Plan
from gmscraper import export, zips
from gmscraper.config import DEFAULT_ZIPS


def test_explicit_zips_count():
    path = Path(DEFAULT_ZIPS)
    if not path.exists():
        zips.build(path)
    rows = zips.load_by_zips(path, "75001,75002,75006")
    assert [r["zip"] for r in rows] == ["75001", "75002", "75006"]


def test_dfw_radius_tx_zips_near_905():
    lat, lng = 32.7767, -97.0000
    rows = zips.within_radius(lat, lng, 150, states=["TX"])
    assert 880 <= len(rows) <= 930, len(rows)
    assert all(r["state"] == "TX" for r in rows)
    prefixes = {r["zip"][:3] for r in rows}
    assert "750" in prefixes and "760" in prefixes


def test_resolve_precedence_zips_beat_radius():
    path = Path(DEFAULT_ZIPS)
    if not path.exists():
        zips.build(path)
    rows, meta = zips.resolve_zip_rows(
        path,
        zips="75001,75002",
        center="Dallas, TX",
        radius_miles=150,
        states=["TX", "OK"],
    )
    assert meta["geo_mode"] == "explicit_zips"
    assert len(rows) == 2


def test_plan_excludes_roofing():
    plan = Plan.from_model(
        {
            "vertical": "commercial_gc",
            "categories": [
                "general contractor",
                "commercial contractor",
                "roofing contractor",
                "commercial roofing",
            ],
            "exclude_categories": ["roofing contractor"],
            "icp": "Commercial GCs. Exclude roofing contractors.",
            "states": ["TX"],
            "center": "Dallas, TX",
            "radius_miles": 150,
            "min_rating": 0,
            "min_reviews": 0,
            "require_website": True,
            "require_phone": True,
            "require_email": False,
            "require_owner": False,
        }
    )
    assert "roofing contractor" not in plan.categories
    assert "commercial roofing" not in plan.categories
    assert "general contractor" in plan.categories
    assert plan.states == ["TX"]
    assert plan.radius_miles == 150


def test_radius_brief_does_not_keep_neighbor_states():
    plan = Plan.from_model(
        {
            "vertical": "gc",
            "categories": ["general contractor"],
            "exclude_categories": [],
            "icp": "GCs",
            "states": ["TX", "OK"],
            "center": "Dallas, TX",
            "radius_miles": 150,
            "min_rating": 0,
            "min_reviews": 0,
            "require_website": False,
            "require_phone": False,
            "require_email": False,
            "require_owner": False,
        }
    )
    assert plan.states == ["TX"]


def test_export_columns_include_coords_and_source_zip():
    assert "latitude" in export.COLUMNS
    assert "longitude" in export.COLUMNS
    assert "source_zip" in export.COLUMNS


def test_plan_roundtrip_persists_zips(tmp_path: Path):
    from gmscraper import brief as brief_mod

    plan = Plan(
        vertical="gc",
        categories=["general contractor"],
        states=["TX"],
        zips=["75001", "75002", "75006"],
        center="Dallas, TX",
        center_lat=32.7767,
        center_lng=-97.0,
        radius_miles=150,
    )
    path = tmp_path / "plan.json"
    brief_mod.save(plan, str(path))
    loaded = brief_mod.load(str(path))
    assert loaded.zips == ["75001", "75002", "75006"]
    assert loaded.center_lat == 32.7767
    data = json.loads(path.read_text())
    assert data["zips"] == ["75001", "75002", "75006"]
