"""Owner-segment classification for operator lane."""

from __future__ import annotations

import pytest

from gmscraper.owner_segment import (
    classify_owner_segment,
    parse_segments,
    segment_breakdown,
)


@pytest.mark.parametrize(
    "top_llc,address,expect",
    [
        ("DALLAS CITY OF", "1500 MARILLA ST, DALLAS TX", "municipal"),
        ("DALLAS ISD", "9400 N CENTRAL EXPY, DALLAS TX", "education"),
        ("PLANO ISD", "2700 W 15TH ST, PLANO TX", "education"),
        (
            "BOARD OF REG OF UNIV OF TX SYSTEM",
            "% REAL ESTATE OFFICE, 210 W 7TH ST, AUSTIN TX",
            "education",
        ),
        ("SOUTHERN METHODIST UNIVERSITY", "6425 BOAZ LN, DALLAS TX", "education"),
        ("DALLAS COLLEGE", "1601 BOTHAM JEAN BLVD, DALLAS TX", "education"),
        (
            "DALLAS COUNTY HOSPITAL DISTRICT",
            "5200 HARRY HINES BLVD, DALLAS TX",
            "healthcare",
        ),
        ("METHODIST HOSPITALS OF DALLAS", "1441 N BECKLEY, DALLAS TX", "healthcare"),
        ("DALLAS HOUSING AUTHORITY", "3939 N HAMPTON RD, DALLAS TX", "housing_authority"),
        (
            "PECOS HOUSING FINANCE CORP",
            "123 MAIN, PECOS TX",
            "housing_authority",
        ),
        ("CHI/WILDLIFE LAND LP", "100 MAIN, DALLAS TX", "private"),
        ("LIT INDUSTRIAL LTD PS", "200 MAIN, DALLAS TX", "private"),
        ("PCV LAKES LLC", "300 MAIN, DALLAS TX", "private"),
        ("DART", "1401 PACIFIC, DALLAS TX", "utility_transit"),
        ("FIRST BAPTIST CHURCH OF DALLAS", "1707 SAN JACINTO, DALLAS TX", "religious_nonprofit"),
    ],
)
def test_classify_examples(top_llc: str, address: str, expect: str) -> None:
    assert classify_owner_segment(top_llc=top_llc, operator_address=address) == expect


def test_parse_segments_default_and_all() -> None:
    assert parse_segments("") == ["private"]
    assert parse_segments("all")[0] == "municipal"
    assert "private" in parse_segments("all")
    assert parse_segments("education,healthcare") == ["education", "healthcare"]
    with pytest.raises(ValueError):
        parse_segments("roofing")


def test_segment_breakdown_sums() -> None:
    rows = [
        {"owner_segment": "private", "parcels": 2, "portfolio_value": 100},
        {"owner_segment": "private", "parcels": 1, "portfolio_value": 50},
        {"owner_segment": "education", "parcels": 10, "portfolio_value": 999},
    ]
    b = segment_breakdown(rows)
    assert b["private"]["operators"] == 2
    assert b["private"]["portfolio_value"] == 150
    assert b["education"]["operators"] == 1
