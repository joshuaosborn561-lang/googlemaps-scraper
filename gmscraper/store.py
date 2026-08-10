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
    first_seen      TEXT DEFAULT CURRENT_TIMESTAMP,
    source          TEXT DEFAULT 'maps',
    external_id     TEXT,
    permit_count    INTEGER,
    plan_id         TEXT,
    run_id          TEXT,
    client_tag      TEXT
);
CREATE INDEX IF NOT EXISTS idx_biz_domain ON businesses(domain);
CREATE INDEX IF NOT EXISTS idx_biz_state  ON businesses(state);
-- Indexes on plan_id/run_id/client_tag/source/external_id are created in
-- Store._migrate so older DBs that predate those columns can ALTER TABLE first.

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
    source       TEXT,           -- website|websearch|team_page|none
    confidence   REAL,
    model        TEXT,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Multiple people per company (team-page crawl, waterfall DMs, etc.).
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id    TEXT,
    domain      TEXT,
    name        TEXT NOT NULL,
    title       TEXT,
    email       TEXT,
    source      TEXT,            -- team_page|getleads|ai_ark|leadmagic|fullenrich
    source_tier TEXT,
    confidence  REAL,
    source_url  TEXT,
    dedupe_key  TEXT NOT NULL,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_contacts_domain ON contacts(domain);
CREATE INDEX IF NOT EXISTS idx_contacts_place ON contacts(place_id);
CREATE INDEX IF NOT EXISTS idx_contacts_source ON contacts(source);

-- Per-page website text tagged by page_type (home/about/team/…).
CREATE TABLE IF NOT EXISTS site_pages (
    domain      TEXT NOT NULL,
    url         TEXT NOT NULL,
    page_type   TEXT NOT NULL,   -- home|about|team|other
    text        TEXT,
    n_chars     INTEGER DEFAULT 0,
    fetched_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (domain, url)
);
CREATE INDEX IF NOT EXISTS idx_site_pages_domain ON site_pages(domain);
CREATE INDEX IF NOT EXISTS idx_site_pages_type ON site_pages(page_type);

-- Keyed by domain like `sites`, so multi-location businesses share them.
CREATE TABLE IF NOT EXISTS emails (
    domain     TEXT NOT NULL,
    email      TEXT NOT NULL,
    source     TEXT,
    found_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (domain, email)
);
CREATE INDEX IF NOT EXISTS idx_emails_domain ON emails(domain);

-- Raw Apify contact-info-scraper dataset items (one row per item).
CREATE TABLE IF NOT EXISTS apify_contact_raw (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    domain      TEXT,
    url         TEXT,
    raw_json    TEXT NOT NULL,
    run_label   TEXT,
    fetched_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_apify_contact_raw_run ON apify_contact_raw(run_id);
CREATE INDEX IF NOT EXISTS idx_apify_contact_raw_domain ON apify_contact_raw(domain);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


_BIZ_EXTRA_COLS = (
    ("source", "TEXT DEFAULT 'maps'"),
    ("external_id", "TEXT"),
    ("permit_count", "INTEGER"),
    # Multi-client scoping: stamp the plan/run that created the row.
    ("plan_id", "TEXT"),
    ("run_id", "TEXT"),
    ("client_tag", "TEXT"),
)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        with self.conn as c:
            c.executescript(SCHEMA)
            self._migrate(c)

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

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after the first schema shipped."""
        existing = {
            r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()
        }
        for name, decl in _BIZ_EXTRA_COLS:
            if name not in existing:
                conn.execute(f"ALTER TABLE businesses ADD COLUMN {name} {decl}")
        # Backfill legacy Maps rows so source filters stay interpretable.
        conn.execute(
            "UPDATE businesses SET source = 'maps' "
            "WHERE source IS NULL OR source = ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_biz_source ON businesses(source)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_biz_external_id ON businesses(external_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_biz_plan ON businesses(plan_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_biz_run ON businesses(run_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_biz_client ON businesses(client_tag)"
        )
        # Ensure tables added after the first schema ship exist on old volumes.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                place_id    TEXT,
                domain      TEXT,
                name        TEXT NOT NULL,
                title       TEXT,
                email       TEXT,
                source      TEXT,
                source_tier TEXT,
                confidence  REAL,
                source_url  TEXT,
                dedupe_key  TEXT NOT NULL,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (dedupe_key)
            );
            CREATE INDEX IF NOT EXISTS idx_contacts_domain ON contacts(domain);
            CREATE INDEX IF NOT EXISTS idx_contacts_place ON contacts(place_id);
            CREATE INDEX IF NOT EXISTS idx_contacts_source ON contacts(source);
            CREATE TABLE IF NOT EXISTS site_pages (
                domain      TEXT NOT NULL,
                url         TEXT NOT NULL,
                page_type   TEXT NOT NULL,
                text        TEXT,
                n_chars     INTEGER DEFAULT 0,
                fetched_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (domain, url)
            );
            CREATE INDEX IF NOT EXISTS idx_site_pages_domain ON site_pages(domain);
            CREATE INDEX IF NOT EXISTS idx_site_pages_type ON site_pages(page_type);
            CREATE TABLE IF NOT EXISTS apify_contact_raw (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                domain      TEXT,
                url         TEXT,
                raw_json    TEXT NOT NULL,
                run_label   TEXT,
                fetched_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_apify_contact_raw_run
                ON apify_contact_raw(run_id);
            CREATE INDEX IF NOT EXISTS idx_apify_contact_raw_domain
                ON apify_contact_raw(domain);
            """
        )

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
                r.get("source") or "maps",
                r.get("external_id"),
                r.get("permit_count"),
                r.get("plan_id") or None,
                r.get("run_id") or None,
                r.get("client_tag") or None,
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
                    longitude, maps_url, source_zip, source_category, raw_json,
                    source, external_id, permit_count, plan_id, run_id, client_tag)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                payload,
            )
            # Stamp plan/run/client on existing rows when a new scrape re-sees them.
            for r in rows:
                if not r.get("place_id"):
                    continue
                if not (r.get("plan_id") or r.get("run_id") or r.get("client_tag")):
                    continue
                c.execute(
                    """UPDATE businesses SET
                         plan_id = COALESCE(?, plan_id),
                         run_id = COALESCE(?, run_id),
                         client_tag = COALESCE(?, client_tag)
                       WHERE place_id = ?""",
                    (
                        r.get("plan_id") or None,
                        r.get("run_id") or None,
                        r.get("client_tag") or None,
                        r.get("place_id"),
                    ),
                )
            return cur.rowcount or 0

    def get_business(self, place_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM businesses WHERE place_id = ?", (place_id,)
        ).fetchone()

    def find_by_domain(self, domain: str) -> sqlite3.Row | None:
        if not domain:
            return None
        return self.conn.execute(
            "SELECT * FROM businesses WHERE domain = ? LIMIT 1", (domain,)
        ).fetchone()

    def find_by_external_id(self, source: str, external_id: str) -> sqlite3.Row | None:
        if not external_id:
            return None
        return self.conn.execute(
            "SELECT * FROM businesses WHERE source = ? AND external_id = ? LIMIT 1",
            (source, str(external_id)),
        ).fetchone()

    def insert_business(self, row: dict[str, Any]) -> bool:
        """Insert one business row. Returns False if place_id already exists."""
        if not row.get("place_id"):
            return False
        with self.conn as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO businesses
                   (place_id, name, address, city, state, zip, phone, website,
                    domain, rating, reviews, main_category, types, latitude,
                    longitude, maps_url, source_zip, source_category, raw_json,
                    source, external_id, permit_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row.get("place_id"),
                    row.get("name"),
                    row.get("address"),
                    row.get("city"),
                    row.get("state"),
                    row.get("zip"),
                    row.get("phone"),
                    row.get("website"),
                    row.get("domain"),
                    row.get("rating"),
                    row.get("reviews"),
                    row.get("main_category"),
                    json.dumps(row.get("types") or []),
                    row.get("latitude"),
                    row.get("longitude"),
                    row.get("maps_url"),
                    row.get("source_zip"),
                    row.get("source_category"),
                    json.dumps(row.get("raw") or {}, ensure_ascii=False),
                    row.get("source") or "maps",
                    row.get("external_id"),
                    row.get("permit_count"),
                ),
            )
            return bool(cur.rowcount)

    def update_business_fields(self, place_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "name", "address", "city", "state", "zip", "phone", "website",
            "domain", "rating", "reviews", "main_category", "types",
            "latitude", "longitude", "maps_url", "source_zip", "source_category",
            "raw_json", "source", "external_id", "permit_count",
        }
        cols, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "types" and not isinstance(v, str):
                v = json.dumps(v or [])
            if k == "raw_json" and not isinstance(v, str):
                v = json.dumps(v or {}, ensure_ascii=False)
            cols.append(f"{k}=?")
            vals.append(v)
        if not cols:
            return
        vals.append(place_id)
        with self.conn as c:
            c.execute(
                f"UPDATE businesses SET {', '.join(cols)} WHERE place_id=?",
                vals,
            )

    def iter_businesses(self, where: str = "", args: Sequence = ()) -> Iterator[sqlite3.Row]:
        sql = "SELECT * FROM businesses"
        if where:
            sql += f" WHERE {where}"
        yield from self.conn.execute(sql, args)

    def email_bucket(self, place_id: str, domain: str | None = None) -> str:
        """Key used in the emails table for this business."""
        d = (domain or "").strip().lower()
        if d:
            return d
        return f"ext:{place_id}"

    def emails_for_business(self, place_id: str, domain: str | None = None) -> list[str]:
        keys = []
        d = (domain or "").strip().lower()
        if d:
            keys.append(d)
        keys.append(f"ext:{place_id}")
        out: list[str] = []
        seen: set[str] = set()
        for key in keys:
            for r in self.conn.execute(
                "SELECT email FROM emails WHERE domain = ? ORDER BY email", (key,)
            ):
                e = r["email"]
                if e not in seen:
                    seen.add(e)
                    out.append(e)
        return out

    # ---------------------------------------------------------------- sites

    def queue_sites(self) -> int:
        with self.conn as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO sites (domain)
                   SELECT DISTINCT domain FROM businesses
                   WHERE domain IS NOT NULL AND domain != ''"""
            )
            return cur.rowcount or 0

    def pending_sites(
        self,
        limit: int | None = None,
        *,
        city: str = "",
        state: str = "",
        main_category: str = "",
        plan_id: str = "",
        run_id: str = "",
        client_tag: str = "",
        source: str = "",
    ) -> list[str]:
        """Pending site domains, optionally scoped to matching businesses.

        state/main_category accept comma-separated lists. main_category matches
        with SQL LIKE (%term%) so 'dealer' catches 'Car dealer' / 'Toyota dealer'.
        """
        scoped = any(
            [
                city.strip(),
                state.strip(),
                main_category.strip(),
                plan_id.strip(),
                run_id.strip(),
                client_tag.strip(),
                source.strip(),
            ]
        )
        if not scoped:
            sql = "SELECT domain FROM sites WHERE status = 'pending'"
            if limit:
                sql += f" LIMIT {int(limit)}"
            return [r["domain"] for r in self.conn.execute(sql)]

        clauses = ["s.status = 'pending'", "b.domain IS NOT NULL", "b.domain != ''"]
        args: list[Any] = []
        if city.strip():
            clauses.append("lower(b.city) = lower(?)")
            args.append(city.strip())
        if state.strip():
            states = [s.strip().upper() for s in state.split(",") if s.strip()]
            if states:
                placeholders = ",".join("?" for _ in states)
                clauses.append(f"upper(COALESCE(b.state,'')) IN ({placeholders})")
                args.extend(states)
        if main_category.strip():
            cats = [c.strip() for c in main_category.split(",") if c.strip()]
            if cats:
                ors = " OR ".join(
                    "lower(COALESCE(b.main_category,'')) LIKE ?" for _ in cats
                )
                clauses.append(f"({ors})")
                args.extend(f"%{c.lower()}%" for c in cats)
        if plan_id.strip():
            clauses.append("b.plan_id = ?")
            args.append(plan_id.strip())
        if run_id.strip():
            clauses.append("b.run_id = ?")
            args.append(run_id.strip())
        if client_tag.strip():
            clauses.append("b.client_tag = ?")
            args.append(client_tag.strip())
        if source.strip():
            clauses.append("COALESCE(NULLIF(b.source,''), 'maps') = ?")
            args.append(source.strip().lower())

        sql = (
            "SELECT DISTINCT s.domain FROM sites s "
            "JOIN businesses b ON b.domain = s.domain WHERE "
            + " AND ".join(clauses)
            + " ORDER BY s.domain"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["domain"] for r in self.conn.execute(sql, args)]

    def save_site(
        self,
        domain: str,
        status: str,
        text: str = "",
        pages: Sequence[Any] = (),
        error: str | None = None,
    ) -> None:
        # pages may be URL strings or {url, page_type} dicts.
        page_urls: list[str] = []
        for p in pages:
            if isinstance(p, dict):
                page_urls.append(str(p.get("url") or ""))
            else:
                page_urls.append(str(p))
        page_urls = [u for u in page_urls if u]
        with self.conn as c:
            c.execute(
                """UPDATE sites SET status=?, text=?, pages=?, n_chars=?, error=?,
                   fetched_at=datetime('now') WHERE domain=?""",
                (status, text, json.dumps(page_urls), len(text), error, domain),
            )

    def save_site_pages(
        self,
        domain: str,
        pages: Sequence[dict[str, Any]],
    ) -> int:
        """Upsert per-page text tagged by page_type."""
        rows = []
        for p in pages:
            url = (p.get("url") or "").strip()
            if not url:
                continue
            text = p.get("text") or ""
            rows.append(
                (
                    domain,
                    url,
                    (p.get("page_type") or "other").strip() or "other",
                    text,
                    len(text),
                )
            )
        if not rows:
            return 0
        with self.conn as c:
            c.executemany(
                """INSERT INTO site_pages (domain, url, page_type, text, n_chars, fetched_at)
                   VALUES (?,?,?,?,?,datetime('now'))
                   ON CONFLICT(domain, url) DO UPDATE SET
                     page_type=excluded.page_type, text=excluded.text,
                     n_chars=excluded.n_chars, fetched_at=excluded.fetched_at""",
                rows,
            )
        return len(rows)

    def get_site_text(self, domain: str) -> str | None:
        row = self.conn.execute(
            "SELECT text FROM sites WHERE domain=? AND status='ok'", (domain,)
        ).fetchone()
        return row["text"] if row else None

    def get_site_pages(
        self,
        domain: str,
        page_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if page_types:
            placeholders = ",".join("?" * len(page_types))
            sql = (
                f"SELECT domain, url, page_type, text, n_chars FROM site_pages "
                f"WHERE domain=? AND page_type IN ({placeholders}) ORDER BY page_type, url"
            )
            args: list[Any] = [domain, *page_types]
        else:
            sql = (
                "SELECT domain, url, page_type, text, n_chars FROM site_pages "
                "WHERE domain=? ORDER BY page_type, url"
            )
            args = [domain]
        return [dict(r) for r in self.conn.execute(sql, args)]

    def _business_scope_clauses(
        self,
        *,
        city: str = "",
        state: str = "",
        main_category: str = "",
        plan_id: str = "",
        run_id: str = "",
        client_tag: str = "",
        source: str = "",
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if city.strip():
            clauses.append("lower(b.city) = lower(?)")
            args.append(city.strip())
        if state.strip():
            states = [s.strip().upper() for s in state.split(",") if s.strip()]
            if states:
                placeholders = ",".join("?" for _ in states)
                clauses.append(f"upper(COALESCE(b.state,'')) IN ({placeholders})")
                args.extend(states)
        if main_category.strip():
            cats = [c.strip() for c in main_category.split(",") if c.strip()]
            if cats:
                ors = " OR ".join(
                    "lower(COALESCE(b.main_category,'')) LIKE ?" for _ in cats
                )
                clauses.append(f"({ors})")
                args.extend(f"%{c.lower()}%" for c in cats)
        if plan_id.strip():
            clauses.append("b.plan_id = ?")
            args.append(plan_id.strip())
        if run_id.strip():
            clauses.append("b.run_id = ?")
            args.append(run_id.strip())
        if client_tag.strip():
            clauses.append("b.client_tag = ?")
            args.append(client_tag.strip())
        if source.strip():
            clauses.append("COALESCE(NULLIF(b.source,''), 'maps') = ?")
            args.append(source.strip().lower())
        return clauses, args

    def domains_with_ok_sites(
        self,
        limit: int | None = None,
        *,
        city: str = "",
        state: str = "",
        main_category: str = "",
        plan_id: str = "",
        run_id: str = "",
        client_tag: str = "",
        source: str = "",
    ) -> list[str]:
        scope, args = self._business_scope_clauses(
            city=city,
            state=state,
            main_category=main_category,
            plan_id=plan_id,
            run_id=run_id,
            client_tag=client_tag,
            source=source,
        )
        if not scope:
            sql = "SELECT domain FROM sites WHERE status='ok' ORDER BY domain"
            if limit:
                sql += f" LIMIT {int(limit)}"
            return [r["domain"] for r in self.conn.execute(sql)]
        sql = (
            "SELECT DISTINCT s.domain FROM sites s "
            "JOIN businesses b ON b.domain = s.domain "
            "WHERE s.status='ok' AND "
            + " AND ".join(scope)
            + " ORDER BY s.domain"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["domain"] for r in self.conn.execute(sql, args)]

    def domains_needing_team_crawl(
        self,
        limit: int | None = None,
        *,
        city: str = "",
        state: str = "",
        main_category: str = "",
        plan_id: str = "",
        run_id: str = "",
        client_tag: str = "",
        source: str = "",
    ) -> list[str]:
        """OK sites that have no team/about page rows yet."""
        scope, args = self._business_scope_clauses(
            city=city,
            state=state,
            main_category=main_category,
            plan_id=plan_id,
            run_id=run_id,
            client_tag=client_tag,
            source=source,
        )
        base = """
            SELECT DISTINCT s.domain FROM sites s
            {join}
            WHERE s.status = 'ok'
              AND NOT EXISTS (
                SELECT 1 FROM site_pages p
                WHERE p.domain = s.domain AND p.page_type IN ('team', 'about')
              )
              {extra}
            ORDER BY s.domain
        """
        if scope:
            sql = base.format(
                join="JOIN businesses b ON b.domain = s.domain",
                extra="AND " + " AND ".join(scope),
            )
            if limit:
                sql += f" LIMIT {int(limit)}"
            return [r["domain"] for r in self.conn.execute(sql, args)]
        sql = base.format(join="", extra="")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["domain"] for r in self.conn.execute(sql)]

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

    def clear_verdict(self, place_id: str) -> None:
        with self.conn as c:
            c.execute("DELETE FROM verdicts WHERE place_id = ?", (place_id,))

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

    @staticmethod
    def _contact_dedupe_key(
        domain: str, name: str, title: str = "", source_url: str = ""
    ) -> str:
        return "|".join(
            [
                (domain or "").strip().lower(),
                (name or "").strip().lower(),
                (title or "").strip().lower(),
                (source_url or "").strip().lower(),
            ]
        )

    def save_contact(
        self,
        *,
        name: str,
        domain: str = "",
        place_id: str = "",
        title: str = "",
        email: str = "",
        source: str = "team_page",
        source_tier: str = "",
        confidence: float = 0.0,
        source_url: str = "",
    ) -> bool:
        """Upsert one contact. Returns True when a new row was inserted."""
        name = (name or "").strip()
        if not name:
            return False
        domain = (domain or "").strip().lower()
        title = (title or "").strip()
        email = (email or "").strip().lower()
        source_url = (source_url or "").strip()
        key = self._contact_dedupe_key(domain, name, title, source_url)
        with self.conn as c:
            cur = c.execute(
                """INSERT INTO contacts
                   (place_id, domain, name, title, email, source, source_tier,
                    confidence, source_url, dedupe_key, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(dedupe_key) DO UPDATE SET
                     place_id=COALESCE(NULLIF(excluded.place_id,''), contacts.place_id),
                     email=COALESCE(NULLIF(excluded.email,''), contacts.email),
                     source=excluded.source,
                     source_tier=COALESCE(NULLIF(excluded.source_tier,''), contacts.source_tier),
                     confidence=MAX(contacts.confidence, excluded.confidence),
                     updated_at=excluded.updated_at""",
                (
                    place_id or None,
                    domain or None,
                    name,
                    title or None,
                    email or None,
                    source,
                    source_tier or source,
                    float(confidence or 0.0),
                    source_url or None,
                    key,
                ),
            )
            return bool(cur.rowcount)

    def save_contacts(self, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        for r in rows:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            if self.save_contact(
                name=name,
                domain=r.get("domain") or "",
                place_id=r.get("place_id") or "",
                title=r.get("title") or "",
                email=r.get("email") or "",
                source=r.get("source") or "team_page",
                source_tier=r.get("source_tier") or "",
                confidence=float(r.get("confidence") or 0.0),
                source_url=r.get("source_url") or "",
            ):
                n += 1
        return n

    def contacts_for_domain(self, domain: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM contacts WHERE domain=? ORDER BY confidence DESC, name",
                ((domain or "").strip().lower(),),
            )
        ]

    # ------------------------------------------------------- apify contact raw

    def save_apify_contact_raw(
        self,
        run_id: str,
        items: Iterable[dict[str, Any]],
        *,
        run_label: str = "",
    ) -> int:
        """Persist Apify dataset items. Returns rows written."""
        from urllib.parse import urlsplit

        def _domain(item: dict[str, Any]) -> str:
            for key in (
                "domain", "website", "url", "inputUrl", "startUrl", "loadedUrl"
            ):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    if "://" in val or "/" in val:
                        host = (urlsplit(val if "://" in val else f"https://{val}")
                                .hostname or "").lower()
                        return host[4:] if host.startswith("www.") else host
                    return val.strip().lower().lstrip("www.")
            return ""

        def _url(item: dict[str, Any], domain: str) -> str:
            for key in ("url", "loadedUrl", "startUrl", "inputUrl", "website"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            return f"https://{domain}" if domain else ""

        n = 0
        label = (run_label or "").strip() or None
        with self.conn as c:
            for item in items:
                if not isinstance(item, dict):
                    continue
                domain = _domain(item)
                url = _url(item, domain)
                c.execute(
                    """INSERT INTO apify_contact_raw
                       (run_id, domain, url, raw_json, run_label, fetched_at)
                       VALUES (?,?,?,?,?,datetime('now'))""",
                    (
                        run_id,
                        domain or None,
                        url or None,
                        json.dumps(item, ensure_ascii=False),
                        label,
                    ),
                )
                n += 1
        return n

    def apify_contact_raw_rows(
        self,
        *,
        run_id: str = "",
        source: str = "",
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Load raw Apify items for parse_contacts_openai."""
        clauses: list[str] = []
        params: list[Any] = []
        if run_id.strip():
            clauses.append("run_id = ?")
            params.append(run_id.strip())
        if source.strip():
            clauses.append("run_label = ?")
            params.append(source.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, run_id, domain, url, raw_json, run_label, fetched_at "
            f"FROM apify_contact_raw{where} ORDER BY id"
        )
        if limit and limit > 0:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, params)]

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

    def requests_since(self, since_iso: str) -> int:
        """Searches billed since a date — how much of the plan quota is gone.

        Errored jobs count: the provider bills the request, not the useful
        result.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('done','error') "
            "AND updated_at >= ?",
            (since_iso,),
        ).fetchone()
        return row[0] if row else 0

    def requests_this_cycle(self, reset_day: int = 1) -> int:
        from .config import cycle_start

        return self.requests_since(cycle_start(reset_day))

    # ---------------------------------------------------------------- stats

    def grid_stats(
        self,
        *,
        categories: Sequence[str] | None = None,
        zips: Sequence[str] | None = None,
    ) -> dict[str, int]:
        """ZIP×category scrape-grid counters, optionally scoped to a plan."""
        filters: list[str] = []
        params: list[Any] = []
        if categories:
            cats = [c for c in categories if c]
            if cats:
                filters.append(f"category IN ({','.join('?' for _ in cats)})")
                params.extend(cats)
        if zips:
            zs = [z for z in zips if z]
            if zs:
                filters.append(f"zip IN ({','.join('?' for _ in zs)})")
                params.extend(zs)

        def count(status: str | None = None, *, not_status: str | None = None) -> int:
            parts = list(filters)
            p = list(params)
            if status is not None:
                parts.append("status = ?")
                p.append(status)
            if not_status is not None:
                parts.append("status != ?")
                p.append(not_status)
            where = f" WHERE {' AND '.join(parts)}" if parts else ""
            row = self.conn.execute(
                f"SELECT COUNT(*) FROM jobs{where}", p
            ).fetchone()
            return int(row[0] if row else 0)

        return {
            "jobs_total": count(),
            "jobs_done": count("done"),
            "jobs_error": count("error"),
            "jobs_pending": count("pending"),
            "jobs_not_done": count(not_status="done"),
        }

    def businesses_found_since(self, started_at: float | None) -> int:
        """Count businesses first_seen at/after a unix timestamp (job start)."""
        if not started_at:
            return int(
                self.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
            )
        # first_seen is SQLite CURRENT_TIMESTAMP text; compare via unixepoch.
        row = self.conn.execute(
            "SELECT COUNT(*) FROM businesses "
            "WHERE unixepoch(first_seen) >= ?",
            (int(started_at),),
        ).fetchone()
        return int(row[0] if row else 0)

    def stats(self) -> dict[str, Any]:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        businesses = q("SELECT COUNT(*) FROM businesses")
        # Eligible for classify: has a domain with successfully fetched site text.
        eligible = q(
            """SELECT COUNT(*) FROM businesses b
               WHERE b.domain IS NOT NULL AND b.domain != ''
                 AND EXISTS (
                   SELECT 1 FROM sites s
                   WHERE s.domain = b.domain AND s.status = 'ok'
                 )"""
        )
        classified = q("SELECT COUNT(*) FROM verdicts")
        unclassifiable = max(0, businesses - eligible)
        pct = round((classified / eligible) * 100.0, 1) if eligible else 0.0
        by_source = {
            (r["source"] or "maps"): r["n"]
            for r in self.conn.execute(
                "SELECT COALESCE(NULLIF(source,''), 'maps') AS source, COUNT(*) AS n "
                "FROM businesses GROUP BY 1 ORDER BY n DESC"
            )
        }
        grid = self.grid_stats()
        return {
            "jobs_total": grid["jobs_total"],
            "jobs_done": grid["jobs_done"],
            "jobs_error": grid["jobs_error"],
            "jobs_pending": grid["jobs_pending"],
            "businesses": businesses,
            "businesses_by_source": by_source,
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
            "classified": classified,
            "classifiable_with_site": eligible,
            "unclassifiable_no_site": unclassifiable,
            "classified_pct_of_eligible": pct,
            "in_icp": q("SELECT COUNT(*) FROM verdicts WHERE in_icp=1"),
            "owners_found": q(
                "SELECT COUNT(*) FROM owners WHERE owner_name IS NOT NULL AND owner_name!=''"
            ),
            "contacts_found": q(
                "SELECT COUNT(*) FROM contacts WHERE name IS NOT NULL AND name!=''"
            ),
            "contacts_team_page": q(
                "SELECT COUNT(*) FROM contacts WHERE source='team_page'"
            ),
            "site_pages": q("SELECT COUNT(*) FROM site_pages"),
            "site_pages_team": q(
                "SELECT COUNT(*) FROM site_pages WHERE page_type='team'"
            ),
            "needs_domain_resolve": q(
                """SELECT COUNT(*) FROM businesses
                   WHERE (domain IS NULL OR domain = '')
                     AND name IS NOT NULL AND name != ''"""
            ),
        }
