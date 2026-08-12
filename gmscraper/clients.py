"""Multi-client registry: slug → Supabase schema/tables.

Keeps Kyle (peterson) and Carlos (basco) data in separate Postgres schemas
so sync/export/enrichment never land in a shared ambiguous table.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "clients.yml"

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_ALIAS_CACHE: dict[str, str] | None = None
_CLIENTS: dict[str, "Client"] | None = None


@dataclass(frozen=True)
class Client:
    slug: str
    display_name: str
    contact_name: str = ""
    aliases: tuple[str, ...] = ()
    # Prefer public schema so PostgREST exposes tables without dashboard config.
    supabase_schema: str = "public"
    default_geo: str = ""
    notes: str = ""
    leads_table: str = ""
    contacts_table: str = ""
    companies_table: str = ""

    def __post_init__(self) -> None:
        schema = self.supabase_schema or "public"
        object.__setattr__(self, "supabase_schema", schema)
        if not self.leads_table:
            object.__setattr__(self, "leads_table", f"{self.slug}_leads")
        if not self.contacts_table:
            object.__setattr__(self, "contacts_table", f"{self.slug}_contacts")
        if not self.companies_table:
            object.__setattr__(self, "companies_table", f"{self.slug}_companies")

    @property
    def leads_fqn(self) -> str:
        return f"{self.supabase_schema}.{self.leads_table}"

    @property
    def contacts_fqn(self) -> str:
        return f"{self.supabase_schema}.{self.contacts_table}"

    @property
    def companies_fqn(self) -> str:
        return f"{self.supabase_schema}.{self.companies_table}"

    def to_public(self) -> dict[str, Any]:
        return {
            "client_tag": self.slug,
            "display_name": self.display_name,
            "contact_name": self.contact_name or None,
            "aliases": list(self.aliases),
            "supabase_schema": self.supabase_schema,
            "tables": {
                "leads": self.leads_fqn,
                "contacts": self.contacts_fqn,
                "companies": self.companies_fqn,
            },
            "default_geo": self.default_geo or None,
            "notes": self.notes or None,
        }


def normalize_slug(raw: str) -> str:
    s = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _load_raw(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or Path(os.environ.get("CLIENTS_CONFIG") or DEFAULT_CONFIG)
    if not cfg_path.exists():
        return {"clients": {}}
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {"clients": {}}
    return data


def load_clients(path: Path | None = None, *, reload: bool = False) -> dict[str, Client]:
    global _CLIENTS, _ALIAS_CACHE
    if _CLIENTS is not None and not reload:
        return _CLIENTS
    raw = _load_raw(path)
    out: dict[str, Client] = {}
    aliases: dict[str, str] = {}
    for key, body in (raw.get("clients") or {}).items():
        slug = normalize_slug(str(key))
        if not _SLUG_RE.match(slug):
            raise ValueError(f"Invalid client slug {key!r} → {slug!r}")
        body = body or {}
        alias_list = tuple(
            normalize_slug(a)
            for a in (body.get("aliases") or [])
            if normalize_slug(a)
        )
        client = Client(
            slug=slug,
            display_name=str(body.get("display_name") or slug),
            contact_name=str(body.get("contact_name") or ""),
            aliases=alias_list,
            supabase_schema=normalize_slug(
                str(body.get("supabase_schema") or "public")
            )
            or "public",
            leads_table=normalize_slug(str(body.get("leads_table") or ""))
            or f"{slug}_leads",
            contacts_table=normalize_slug(str(body.get("contacts_table") or ""))
            or f"{slug}_contacts",
            companies_table=normalize_slug(str(body.get("companies_table") or ""))
            or f"{slug}_companies",
            default_geo=str(body.get("default_geo") or ""),
            notes=str(body.get("notes") or ""),
        )
        out[slug] = client
        aliases[slug] = slug
        for a in alias_list:
            aliases[a] = slug
    _CLIENTS = out
    _ALIAS_CACHE = aliases
    return out


def resolve_client(client_tag: str, *, required: bool = True) -> Client | None:
    """Resolve a tag/alias to a registered Client."""
    load_clients()
    assert _ALIAS_CACHE is not None and _CLIENTS is not None
    key = normalize_slug(client_tag)
    if not key:
        if required:
            raise ValueError(
                "client_tag is required. Use list_clients() — e.g. "
                "'peterson' (Kyle) or 'basco' (Carlos)."
            )
        return None
    slug = _ALIAS_CACHE.get(key)
    if slug is None:
        # Allow ad-hoc slugs that look valid (auto-register shape) when not required.
        if not required and _SLUG_RE.match(key):
            return Client(slug=key, display_name=key)
        known = ", ".join(sorted(_CLIENTS)) or "(none)"
        raise ValueError(
            f"Unknown client_tag {client_tag!r}. Known: {known}. "
            "Aliases: kyle→peterson, carlos→basco."
        )
    return _CLIENTS[slug]


def list_clients_public() -> list[dict[str, Any]]:
    return [c.to_public() for c in sorted(load_clients().values(), key=lambda c: c.slug)]


# ---------------------------------------------------------------------------
# Supabase DDL / ensure
# ---------------------------------------------------------------------------

LEADS_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.{leads} (
    place_id text NOT NULL,
    run_label text NOT NULL DEFAULT '',
    name text,
    owner_name text,
    owner_title text,
    owner_source text,
    email text,
    all_emails text,
    phone text,
    website text,
    domain text,
    address text,
    city text,
    state text,
    zip text,
    source_zip text,
    rating double precision,
    reviews integer,
    main_category text,
    types text,
    latitude double precision,
    longitude double precision,
    maps_url text,
    in_icp boolean,
    icp_confidence double precision,
    icp_reason text,
    source_category text,
    permit_count integer,
    source text,
    client_tag text NOT NULL,
    plan_id text,
    run_id text,
    synced_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (place_id, run_label)
);
CREATE INDEX IF NOT EXISTS {leads}_client ON {schema}.{leads} (client_tag);
CREATE INDEX IF NOT EXISTS {leads}_domain ON {schema}.{leads} (domain);
CREATE INDEX IF NOT EXISTS {leads}_state ON {schema}.{leads} (state);
CREATE INDEX IF NOT EXISTS {leads}_in_icp ON {schema}.{leads} (in_icp);
"""

CONTACTS_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.{contacts} (
    id bigserial PRIMARY KEY,
    domain text NOT NULL,
    first_name text,
    last_name text,
    job_title text,
    job_level text,
    email text,
    email_status text,
    cellphone text,
    linkedin_url text,
    contact_city text,
    contact_state text,
    source_tool text,
    source_tier text,
    source_url text,
    confidence double precision,
    place_id text,
    client_tag text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS {contacts}_domain_email_key
    ON {schema}.{contacts} (domain, email);
CREATE INDEX IF NOT EXISTS {contacts}_client ON {schema}.{contacts} (client_tag);
CREATE INDEX IF NOT EXISTS {contacts}_domain ON {schema}.{contacts} (domain);
"""

COMPANIES_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.{companies} (
    domain text PRIMARY KEY,
    company_name text,
    source text,
    in_shovels boolean,
    in_maps_icp boolean,
    permit_count integer,
    place text,
    address_city text,
    address_state text,
    employee_range text,
    shovels_email text,
    shovels_name text,
    dm_lookup_status text,
    email_source_tier text,
    dm_source_tier text,
    source_tier jsonb,
    website text,
    client_tag text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS {companies}_client ON {schema}.{companies} (client_tag);
"""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def supabase_admin_config() -> dict[str, str]:
    url = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_ROLE_KEY") or _env("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError(
            "Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )
    return {"url": url, "key": key, "project_id": _env("SUPABASE_PROJECT_ID")}


def ensure_sql_for_client(client: Client) -> str:
    """Return idempotent DDL for one client's tables."""
    schema = client.supabase_schema or "public"
    for name in (
        client.leads_table,
        client.contacts_table,
        client.companies_table,
        schema,
    ):
        if not _SLUG_RE.match(name):
            raise ValueError(f"Invalid identifier {name!r}")
    fmt = {
        "schema": schema,
        "leads": client.leads_table,
        "contacts": client.contacts_table,
        "companies": client.companies_table,
    }
    parts = [
        LEADS_DDL.format(**fmt),
        CONTACTS_DDL.format(**fmt),
        COMPANIES_DDL.format(**fmt),
        f"GRANT ALL ON TABLE {schema}.{client.leads_table} "
        f"TO anon, authenticated, service_role;",
        f"GRANT ALL ON TABLE {schema}.{client.contacts_table} "
        f"TO anon, authenticated, service_role;",
        f"GRANT ALL ON TABLE {schema}.{client.companies_table} "
        f"TO anon, authenticated, service_role;",
        f"GRANT ALL ON ALL SEQUENCES IN SCHEMA {schema} "
        f"TO anon, authenticated, service_role;",
        "NOTIFY pgrst, 'reload schema';",
    ]
    return "\n".join(parts)


def ensure_sql_all() -> str:
    return "\n\n".join(ensure_sql_for_client(c) for c in load_clients().values())


def ensure_client_tables(
    client_tag: str = "",
    *,
    via: str = "rest_probe",
) -> dict[str, Any]:
    """Ensure client schema/tables exist.

    Primary path: caller applies ensure_sql via Supabase SQL (MCP/migration).
    Runtime path: probe REST; if 404/PGRST205, return sql_needed for apply.
    """
    clients = (
        [resolve_client(client_tag)]
        if (client_tag or "").strip()
        else list(load_clients().values())
    )
    cfg = supabase_admin_config()
    ensured: list[dict[str, Any]] = []
    needs_sql: list[str] = []
    for client in clients:
        assert client is not None
        status = _probe_table(cfg, client.supabase_schema, client.leads_table)
        if status == "ok":
            ensured.append({**client.to_public(), "status": "ready"})
            continue
        needs_sql.append(client.slug)
        ensured.append(
            {
                **client.to_public(),
                "status": "needs_ddl",
                "probe": status,
            }
        )
    return {
        "clients": ensured,
        "needs_ddl": needs_sql,
        "apply_sql": ensure_sql_all() if needs_sql else None,
        "note": (
            "Apply apply_sql via Supabase SQL editor / migration, then expose "
            "client_* schemas in API settings (or NOTIFY pgrst). "
            "Re-call ensure_client_tables to verify."
            if needs_sql
            else "All registered client tables are reachable via PostgREST."
        ),
    }


def _probe_table(cfg: dict[str, str], schema: str, table: str) -> str:
    url = f"{cfg['url']}/rest/v1/{table}?select=place_id&limit=1"
    req = request.Request(
        url,
        headers={
            "apikey": cfg["key"],
            "Authorization": f"Bearer {cfg['key']}",
            "Accept-Profile": schema,
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            resp.read()
            return "ok"
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return f"http_{exc.code}:{detail}"
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}:{exc}"


def rest_headers(schema: str, key: str, *, prefer: str = "return=minimal") -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
        "Accept-Profile": schema,
        "Content-Profile": schema,
    }
