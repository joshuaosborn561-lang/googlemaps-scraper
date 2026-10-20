"""Maps territory lock: out-of-state listings from a seeded search are dropped."""

from gmscraper import scrape, territory
from gmscraper.store import Store


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def search(self, category, zip_row):
        return [dict(r) for r in self.rows]


ROWS = [
    {"place_id": "p-ca", "name": "Synthetic Plumbing A", "state": "CA", "zip": "96001"},
    {"place_id": "p-ct", "name": "Synthetic Plumbing B", "state": "CT", "zip": "06514"},
    {"place_id": "p-ct2", "name": "Synthetic Plumbing C", "state": "Connecticut", "zip": "06510"},
    {"place_id": "p-none", "name": "Synthetic Plumbing D", "state": "", "zip": ""},
]


def test_seed_zip_state_drops_out_of_state(tmp_path):
    store = Store(tmp_path / "t.db")
    res = scrape.run(store, FakeClient(ROWS), [{"zip": "96011", "state": "CA"}],
                     ["plumber"], workers=1)
    kept = {r["place_id"] for r in store.iter_businesses()}
    assert kept == {"p-ca", "p-none"}
    assert res["dropped_out_of_territory"] == 2
    assert res["dropped_by_state"] == {"CT": 2}


def test_explicit_allowed_states_override(tmp_path):
    store = Store(tmp_path / "t.db")
    scrape.run(store, FakeClient(ROWS), [{"zip": "96011", "state": "CA"}],
               ["plumber"], workers=1, allowed_states=["CA", "CT"])
    kept = {r["place_id"] for r in store.iter_businesses()}
    assert kept == {"p-ca", "p-ct", "p-ct2", "p-none"}


def test_norm_state():
    assert territory.norm_state("ct") == "CT"
    assert territory.norm_state("New Hampshire") == "NH"
    assert territory.norm_state("Ontario") == ""
