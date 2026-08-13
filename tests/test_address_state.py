"""State extraction for operator mailing filter (OOS leak fix)."""

from __future__ import annotations

from gmscraper.address_state import extract_state, is_in_states


def test_comma_between_state_and_zip_oos_leak_cases() -> None:
    """Prior filter only caught ', ST ZIP'; these 'ST, ZIP' forms leaked."""
    cases = {
        "BOSTON MA, 02109": "MA",
        "NASHVILLE TN, 37203": "TN",
        "ATLANTA GA, 30309": "GA",
        "CHICAGO IL, 60601": "IL",
        "INDIANAPOLIS IN, 46204": "IN",
        "CONSHOHOCKEN PA, 19428": "PA",
        "SOME LLC, BOSTON MA, 02109": "MA",
    }
    for addr, expect in cases.items():
        assert extract_state(addr) == expect, addr
        assert not is_in_states(addr, {"TX"}), addr


def test_comma_before_state_and_plain_zip() -> None:
    assert extract_state("123 Main St, Dallas, TX 75201") == "TX"
    assert extract_state("123 Main St, Dallas TX 75201") == "TX"
    assert extract_state("DALLAS TX, 75201") == "TX"
    assert extract_state("PO BOX 1, AUSTIN, TX, 78701") == "TX"
    assert is_in_states("3102 MAPLE AVE STE 500, DALLAS TX 75201", {"TX"})


def test_full_state_name() -> None:
    assert extract_state("100 MAIN ST, BOSTON MASSACHUSETTS 02116") == "MA"
    assert extract_state("AUSTIN TEXAS 78701") == "TX"
    assert not is_in_states("100 MAIN ST, BOSTON MASSACHUSETTS 02116", {"TX"})


def test_street_suffix_st_not_south_dakota() -> None:
    # House number + ST street suffix before a bare zip must not yield SD.
    assert extract_state("123 MAIN ST, 02109") == ""
    # Real TX address with street ST still resolves via trailing state+zip.
    assert extract_state("123 MAIN ST, DALLAS TX 75201") == "TX"


def test_empty_and_unparseable() -> None:
    assert extract_state("") == ""
    assert extract_state("NO STATE HERE") == ""
    assert is_in_states("", {"TX"}) is False
