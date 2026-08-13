"""build_operators in-state aggregation (OOS mailing exclusion)."""

from __future__ import annotations

from gmscraper.operators import _aggregate_parcels, _as_int


def test_as_int_tolerates_float_strings() -> None:
    assert _as_int(2) == 2
    assert _as_int(2.0) == 2
    assert _as_int("2") == 2
    assert _as_int(None, 1) == 1


def test_aggregate_drops_oos_city_st_comma_zip() -> None:
    parcels = [
        {
            "mailing_address": "100 MAIN ST, DALLAS TX 75201",
            "owner_name": "TX HOLDINGS LLC",
            "county": "Dallas",
            "assessed_value": 5_000_000,
            "parcel_address": "100 Main St Dallas",
        },
        {
            "mailing_address": "BOSTON MA, 02109",
            "owner_name": "EAST COAST LLC",
            "county": "Dallas",
            "assessed_value": 50_000_000,
            "parcel_address": "Big Dallas Tower",
        },
        {
            "mailing_address": "NASHVILLE TN, 37203",
            "owner_name": "MUSIC ROW LP",
            "county": "Harris",
            "assessed_value": 20_000_000,
            "parcel_address": "Houston Site",
        },
        {
            "mailing_address": "ATLANTA GA, 30309",
            "owner_name": "PEACH LLC",
            "county": "Travis",
            "assessed_value": 10_000_000,
            "parcel_address": "Austin Site",
        },
        {
            "mailing_address": "CHICAGO IL, 60601",
            "owner_name": "WINDY LLC",
            "county": "Dallas",
            "assessed_value": 8_000_000,
            "parcel_address": "Dallas Site",
        },
        {
            "mailing_address": "INDIANAPOLIS IN, 46204",
            "owner_name": "HOOSIER LLC",
            "county": "Dallas",
            "assessed_value": 7_000_000,
            "parcel_address": "Dallas Site 2",
        },
        {
            "mailing_address": "CONSHOHOCKEN PA, 19428",
            "owner_name": "PA HOLDCO",
            "county": "Dallas",
            "assessed_value": 6_000_000,
            "parcel_address": "Dallas Site 3",
        },
        {
            "mailing_address": "200 CONGRESS AVE, AUSTIN TEXAS 78701",
            "owner_name": "AUSTIN TX LLC",
            "county": "Travis",
            "assessed_value": 1_000_000,
            "parcel_address": "200 Congress",
        },
    ]
    rows, stats = _aggregate_parcels(parcels, allowed_states={"TX"})
    assert stats["parcels_oos"] == 6
    assert stats["parcels_kept"] == 2
    assert stats["operators"] == 2
    addrs = {r["operator_address"] for r in rows}
    assert any("DALLAS" in a.upper() for a in addrs)
    assert any("AUSTIN" in a.upper() for a in addrs)
    assert not any("BOSTON" in a.upper() for a in addrs)
    assert not any("NASHVILLE" in a.upper() for a in addrs)
    # Top by portfolio among kept is Dallas mailing.
    assert rows[0]["portfolio_value"] == 5_000_000


def test_aggregate_radius_zip_and_min_parcels() -> None:
    parcels = [
        {
            "mailing_address": "100 MAIN ST, DALLAS TX 75201",
            "owner_name": "A LLC",
            "county": "Dallas",
            "assessed_value": 1_000_000,
            "parcel_address": "P1",
            "zip": "75201",
        },
        {
            "mailing_address": "100 MAIN ST, DALLAS TX 75201",
            "owner_name": "B LLC",
            "county": "Dallas",
            "assessed_value": 2_000_000,
            "parcel_address": "P2",
            "zip": "75201",
        },
        {
            "mailing_address": "200 CONGRESS, AUSTIN TX 78701",
            "owner_name": "C LLC",
            "county": "Travis",
            "assessed_value": 9_000_000,
            "parcel_address": "P3",
            "zip": "78701",
        },
    ]
    rows, stats = _aggregate_parcels(
        parcels, allowed_states={"TX"}, zip_allow={"75201"}
    )
    assert stats["parcels_outside_radius"] == 1
    assert stats["parcels_kept"] == 2
    assert len(rows) == 1
    assert rows[0]["parcels"] == 2
    assert rows[0]["portfolio_value"] == 3_000_000
    # min_parcels=2 keeps this operator
    kept = [r for r in rows if r["parcels"] >= 2]
    assert len(kept) == 1
