"""SQLite state.

Everything the pipeline knows lives here so that any stage can be killed
mid-run and picked up again.  Three ideas carry the design:

* ``jobs`` is a checkpoint table -- one row per (zip, category).  A restart
  only queues the rows that are not ``done``.
* ``businesses`` is keyed by the provider's place id, so the heavy overlap
  between neighbouring ZIPs collapses into one row instead of twenty.
* every row keeps the provider's untouched JSON in ``raw_json``.  If a field
  turns out to live under a name the normaliser does not know yet, the data
  is still on disk and ``renormalize`` can re-read it without re-scraping.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    zip         TEXT NOT NULL,
    category    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|done|error
    n_results   INTEGER DEFAULT 0,
    error       TEXT,
    updated_at  TEXT,
    PRIMARY KEY (zip, category)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS businesses (
    place_id        TEXT PRIMARY KEY,
    name            TEXT,
    address         TEXT,
    city            TEXT,
    state           TEXT,
    zip             TEXT,
    phone           TEXT,
    website         TEXT,
    domain          TEXT,
    rating          REAL,
    reviews         INTEGER,
    main_category   TEXT,
    types           TEXT,
    latitude        REAL,
    longitude       REAL,
    maps_url        TEXT,
    source_zip      TEXT,
    source_category TEXT,
    raw_json        TEXT,
    first_seen      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_biz_domain ON businesses(domain);
CREATE INDEX IF NOT EXISTS idx_biz_state  ON businesses(state);

-- One row per domain, not per business: franchises and multi-location shops
-- share a website and there is no reason to fetch or read it twice.
CREATE TABLE IF NOT EXISTS sites (
    domain      TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|ok|error|skipped
    pages       TEXT,
    text        TEXT,
    n_chars     INTEGER DEFAULT 0,
    error       TEXT,
    fetched_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status);

CREATE TABLE IF NOT EXISTS verdicts (
    place_id    TEXT PRIMARY KEY,
    in_icp      INTEGER,
    confidence  REAL,
    reason      TEXT,
    model       TEXT,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_verdicts_icp ON verdicts(in_icp);

CREATE TABLE IF NOT EXISTS owners (
    place_id     TEXT PRIMARY KEY,
    owner_name   TEXT,
    owner_title  TEXT,
    source       TEXT,           -- website|websearch|none
    confidence   REAL,
    model        TEXT,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Keyed by domain like `sites`, so multi-location businesses share them.
CREATE TABLE IF NOT EXISTS emails (
    domain     TEXT NOT NULL,
    email      TEXT NOT NULL,
    source     TEXT,
    found_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (domain, email)
);
CREATE INDEX IF NOT EXISTS idx_emails_domain ON emails(domain);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        with self.conn as c:
            c.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        """One connection per thread -- SQLite objects are not shareable."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=60000")
            self._local.conn = conn
        return conn

    # ---------------------------------------------------------------- jobs

    def queue_jobs(self, zips: Sequence[str], categories: Sequence[str]) -> int:
        """Insert the (zip, category) grid. Existing rows keep their status."""
        rows = [(z, c) for c in categories for z in zips]
        with self.conn as c:
            c.executemany(
                "INSERT OR IGNORE INTO jobs (zip, category) VALUES (?, ?)", rows
            )
        return len(rows)

    def pending_jobs(self, limit: int | None = None) -> list[tuple[str, str]]:
        sql = "SELECT zip, category FROM jobs WHERE status != 'done' ORDER BY category, zip"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(r["zip"], r["category"]) for r in self.conn.execute(sql)]

    def finish_job(
        self, zip_code: str, category: str, n: int, error: str | None = None
    ) -> None:
        with self.conn as c:
            c.execute(
                "UPDATE jobs SET status=?, n_results=?, error=?, "
                "updated_at=datetime('now') WHERE zip=? AND category=?",
                ("error" if error else "done", n, error, zip_code, category),
            )

    # ----------------------------------------------------------- businesses

    def upsert_businesses(self, rows: Iterable[dict[str, Any]]) -> int:
        """Insert new businesses; ignore ones already seen under another ZIP."""
        payload = [
            (
                r.get("place_id"),
                r.get("name"),
                r.get("address"),
                r.get("city"),
                r.get("state"),
                r.get("zip"),
                r.get("phone"),
                r.get("website"),
                r.get("domain"),
                r.get("rating"),
                r.get("reviews"),
                r.get("main_category"),
                json.dumps(r.get("types") or []),
                r.get("latitude"),
                r.get("longitude"),
                r.get("maps_url"),
                r.get("source_zip"),
                r.get("source_category"),
                json.dumps(r.get("raw") or {}, ensure_ascii=False),
            )
            for r in rows
            if r.get("place_id")
        ]
        if not payload:
            return 0
        with self.conn as c:
            cur = c.executemany(
                """INSERT OR IGNORE INTO businesses
                   (place_id, name, address, city, state, zip, phone, website,
                    domain, rating, reviews, main_category, types, latitude,
                    longitude, maps_url, source_zip, source_category, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
            return cur.rowcount or 0

    def iter_businesses(self, where: str = "", args: Sequence = ()) -> Iterator[sqlite3.Row]:
        sql = "SELECT * FROM businesses"
        if where:
            sql += f" WHERE {where}"
        yield from self.conn.execute(sql, args)

    # ---------------------------------------------------------------- sites

    def queue_sites(self) -> int:
        with self.conn as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO sites (domain)
                   SELECT DISTINCT domain FROM businesses
                   WHERE domain IS NOT NULL AND domain != ''"""
            )
            return cur.rowcount or 0

    def pending_sites(self, limit: int | None = None) -> list[str]:
        sql = "SELECT domain FROM sites WHERE status = 'pending'"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["domain"] for r in self.conn.execute(sql)]

    def save_site(
        self,
        domain: str,
        status: str,
        text: str = "",
        pages: Sequence[str] = (),
        error: str | None = None,
    ) -> None:
        with self.conn as c:
            c.execute(
                """UPDATE sites SET status=?, text=?, pages=?, n_chars=?, error=?,
                   fetched_at=datetime('now') WHERE domain=?""",
                (status, text, json.dumps(list(pages)), len(text), error, domain),
            )

    def get_site_text(self, domain: str) -> str | None:
        row = self.conn.execute(
            "SELECT text FROM sites WHERE domain=? AND status='ok'", (domain,)
        ).fetchone()
        return row["text"] if row else None

    # --------------------------------------------------------------- emails

    def save_emails(
        self, domain: str, addresses: Iterable[str], source: str = "website"
    ) -> int:
        rows = [(domain, e, source) for e in sorted(set(addresses)) if e]
        if not rows:
            return 0
        with self.conn as c:
            cur = c.executemany(
                "INSERT OR IGNORE INTO emails (domain, email, source) VALUES (?,?,?)",
                rows,
            )
            return cur.rowcount or 0

    def emails_by_domain(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.conn.execute("SELECT domain, email FROM emails ORDER BY domain"):
            out.setdefault(r["domain"], []).append(r["email"])
        return out

    # ------------------------------------------------------ verdicts/owners

    def save_verdict(
        self,
        place_id: str,
        in_icp: bool | None,
        confidence: float,
        reason: str,
        model: str,
    ) -> None:
        with self.conn as c:
            c.execute(
                """INSERT INTO verdicts (place_id, in_icp, confidence, reason, model,
                                         updated_at)
                   VALUES (?,?,?,?,?,datetime('now'))
                   ON CONFLICT(place_id) DO UPDATE SET
                     in_icp=excluded.in_icp, confidence=excluded.confidence,
                     reason=excluded.reason, model=excluded.model,
                     updated_at=excluded.updated_at""",
                (place_id, None if in_icp is None else int(in_icp), confidence, reason, model),
            )

    def save_owner(
        self,
        place_id: str,
        name: str | None,
        title: str | None,
        source: str,
        confidence: float,
        model: str,
    ) -> None:
        with self.conn as c:
            c.execute(
                """INSERT INTO owners (place_id, owner_name, owner_title, source,
                                       confidence, model, updated_at)
                   VALUES (?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(place_id) DO UPDATE SET
                     owner_name=excluded.owner_name, owner_title=excluded.owner_title,
                     source=excluded.source, confidence=excluded.confidence,
                     model=excluded.model, updated_at=excluded.updated_at""",
                (place_id, name, title, source, confidence, model),
            )

    # ----------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        with self.conn as c:
            c.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def requests_this_month(self) -> int:
        """Searches billed since the 1st — how much of the plan quota is gone.

        Errored jobs count: RapidAPI bills the request, not the useful result.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('done','error') "
            "AND updated_at >= date('now','start of month')"
        ).fetchone()
        return row[0] if row else 0

    # ---------------------------------------------------------------- stats

    def stats(self) -> dict[str, Any]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "jobs_total": q("SELECT COUNT(*) FROM jobs"),
            "jobs_done": q("SELECT COUNT(*) FROM jobs WHERE status='done'"),
            "jobs_error": q("SELECT COUNT(*) FROM jobs WHERE status='error'"),
            "jobs_pending": q("SELECT COUNT(*) FROM jobs WHERE status='pending'"),
            "businesses": q("SELECT COUNT(*) FROM businesses"),
            "with_website": q(
                "SELECT COUNT(*) FROM businesses WHERE domain IS NOT NULL AND domain!=''"
            ),
            "with_phone": q(
                "SELECT COUNT(*) FROM businesses WHERE phone IS NOT NULL AND phone!=''"
            ),
            "domains": q("SELECT COUNT(*) FROM sites"),
            "sites_ok": q("SELECT COUNT(*) FROM sites WHERE status='ok'"),
            "sites_pending": q("SELECT COUNT(*) FROM sites WHERE status='pending'"),
            "emails": q("SELECT COUNT(*) FROM emails"),
            "domains_with_email": q("SELECT COUNT(DISTINCT domain) FROM emails"),
            "classified": q("SELECT COUNT(*) FROM verdicts"),
            "in_icp": q("SELECT COUNT(*) FROM verdicts WHERE in_icp=1"),
            "owners_found": q(
                "SELECT COUNT(*) FROM owners WHERE owner_name IS NOT NULL AND owner_name!=''"
            ),
        }
