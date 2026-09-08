"""Push leads and contacts into per-client Supabase schemas.

Default path: client_tag → client_{slug}.leads (never a shared maps_leads dump
unless explicitly overridden with table= for legacy).

dataset='contacts' upserts local SQLite contacts into client_{slug}.contacts.

Source rows may be selected by the same scope filters as classify/enrich
(city/state/main_category/plan_id/run_id/source + optional radius) so
historical SQLite rows that predate client_tag stamping are reachable.
Destination rows are always stamped with the resolved client_tag.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib import error, parse, request

from . import clients as client_reg
from . import export

BATCH_SIZE = 500
PAGE_SIZE = 1000

# Columns written to Supabase. Matches export_csv + run_label + synced_at.
SYNC_COLUMNS = export.COLUMNS + ["run_label", "synced_at"]

_TEAM_SOURCES = frozenset(
    {"team_page", "heuristic", "extract_team_contacts"}
)
_OWNER_SOURCES = frozenset(
    {"website", "websearch", "find_owners", "none", "owner"}
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def supabase_config() -> dict[str, str]:
    url = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_ROLE_KEY") or _env("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError(
            "Supabase not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY) on the MCP service."
        )
    return {"url": url, "key": key}


def _headers(
    key: str,
    *,
    prefer: str,
    schema: str = "public",
) -> dict[str, str]:
    return client_reg.rest_headers(schema, key, prefer=prefer)


def _request(
    method: str,
    url: str,
    key: str,
    *,
    body: Any = None,
    prefer: str = "return=minimal",
    schema: str = "public",
) -> tuple[int, str]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers=_headers(key, prefer=prefer, schema=schema),
        method=method,
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase {method} {url} [schema={schema}] failed ({exc.code}): {detail[:500]}"
        ) from exc


def truncate_table(table: str = "leads", *, schema: str = "public") -> None:
    raise ValueError("truncate is disabled; upsert only.")


def _row_for_supabase(
    rec: dict[str, Any],
    run_label: str,
    synced_at: str,
    *,
    client_tag: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in export.COLUMNS:
        val = rec.get(col)
        if col in ("rating", "icp_confidence", "latitude", "longitude"):
            try:
                out[col] = float(val) if val not in (None, "") else None
            except (TypeError, ValueError):
                out[col] = None
        elif col in ("reviews", "permit_count"):
            try:
                out[col] = int(val) if val not in (None, "") else None
            except (TypeError, ValueError):
                out[col] = None
        elif col == "in_icp":
            # export stores yes/no strings; Supabase column is boolean.
            if val in (True, False):
                out[col] = val
            elif str(val).strip().lower() in ("yes", "true", "1"):
                out[col] = True
            elif str(val).strip().lower() in ("no", "false", "0"):
                out[col] = False
            else:
                out[col] = None
        else:
            out[col] = "" if val is None else val
    out["client_tag"] = client_tag or out.get("client_tag") or ""
    out["run_label"] = run_label or ""
    out["synced_at"] = synced_at
    return out


def upsert_rows(
    table: str,
    rows: list[dict[str, Any]],
    *,
    schema: str = "public",
    on_conflict: str = "place_id,run_label",
) -> int:
    if not rows:
        return 0
    cfg = supabase_config()
    url = f"{cfg['url']}/rest/v1/{table}?on_conflict={on_conflict}"
    synced = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        _request(
            "POST",
            url,
            cfg["key"],
            body=batch,
            prefer="resolution=merge-duplicates,return=minimal",
            schema=schema,
        )
        synced += len(batch)
    return synced


def resolve_sync_target(
    *,
    client_tag: str = "",
    table: str = "",
    schema: str = "",
    dataset: str = "",
) -> dict[str, str]:
    """Pick schema/table from client registry unless explicitly overridden."""
    tag = (client_tag or "").strip()
    explicit_table = (table or "").strip()
    explicit_schema = (schema or "").strip()
    ds = (dataset or "leads").strip().lower() or "leads"

    if tag:
        client = client_reg.resolve_client(tag)
        assert client is not None
        default_table = (
            client.contacts_table if ds in ("contacts", "contact") else client.leads_table
        )
        schema_name = explicit_schema or client.supabase_schema
        table_name = explicit_table or default_table
        return {
            "client_tag": client.slug,
            "schema": schema_name,
            "table": table_name,
            "fqn": f"{schema_name}.{table_name}",
            "display_name": client.display_name,
            "dataset": "contacts" if ds in ("contacts", "contact") else "leads",
        }

    if explicit_table and explicit_table != "maps_leads":
        return {
            "client_tag": "",
            "schema": explicit_schema or "public",
            "table": explicit_table,
            "fqn": f"{explicit_schema or 'public'}.{explicit_table}",
            "display_name": "",
            "dataset": ds,
        }
    raise ValueError(
        "client_tag is required for sync_to_supabase so each client lands in "
        "its own schema (e.g. client_tag='peterson' → client_peterson.leads, "
        "client_tag='basco' → client_basco.leads). "
        "Call list_clients() for the registry. "
        "Scope historical (untagged) SQLite rows with state/main_category/"
        "plan_id/center+radius_miles — same filters as classify_leads."
    )


def _source_scope_set(
    *,
    city: str = "",
    state: str = "",
    main_category: str = "",
    plan_id: str = "",
    run_id: str = "",
    source: str = "",
    center: str = "",
    radius_miles: float = 0.0,
    center_lat: float = 0.0,
    center_lng: float = 0.0,
) -> bool:
    """True when caller scoped by geography/plan/category (not destination tag)."""
    if any(
        (x or "").strip()
        for x in (city, state, main_category, plan_id, run_id, source, center)
    ):
        return True
    if radius_miles and (center_lat or center_lng or (center or "").strip()):
        return True
    return False


def split_name(raw: str) -> tuple[str, str]:
    """Split a single local `name` into first_name / last_name."""
    parts = [p for p in (raw or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def source_tool_for(source: str, source_tier: str = "") -> str:
    """Map local contacts.source to extract_team_contacts / find_owners."""
    raw = (source or "").strip().lower()
    tier = (source_tier or "").strip().lower()
    token = raw or tier
    if token in _TEAM_SOURCES:
        return "extract_team_contacts"
    if token in _OWNER_SOURCES:
        return "find_owners"
    return raw or "extract_team_contacts"


def natural_key(
    *,
    client_tag: str,
    domain: str,
    first_name: str,
    last_name: str,
    email: str,
) -> str | None:
    """Stable upsert key matching the unique indexes on client_*.contacts."""
    tag = (client_tag or "").strip().lower()
    dom = (domain or "").strip().lower()
    first = (first_name or "").strip().lower()
    last = (last_name or "").strip().lower()
    em = (email or "").strip().lower()
    if not tag or not dom:
        return None
    if first and last:
        return f"n|{tag}|{dom}|{first}|{last}"
    if em:
        return f"e|{tag}|{dom}|{em}"
    if first:
        return f"n|{tag}|{dom}|{first}|"
    return None


def _job_level(title: str) -> str:
    t = (title or "").lower()
    if any(k in t for k in ("ceo", "owner", "founder", "president", "principal", "partner")):
        return "C-Team"
    if "vp" in t or "vice president" in t:
        return "VP"
    if "director" in t:
        return "Director"
    if "manager" in t:
        return "Manager"
    return ""


def _verify_sql(fqn: str, dataset: str) -> str:
    if dataset == "contacts":
        return (
            f"select count(*), count(distinct domain), "
            f"count(*) filter (where email <> '') from {fqn};"
        )
    return f"select count(*) filter (where owner_name <> '') from {fqn};"


def _biz_filter_sql(
    *,
    client_tag: str | None,
    plan_id: str = "",
    run_id: str = "",
    city: str = "",
    state: str = "",
    main_category: str = "",
    source: str = "",
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if client_tag:
        clauses.append("b.client_tag = ?")
        args.append(client_tag)
    if (plan_id or "").strip():
        clauses.append("b.plan_id = ?")
        args.append(plan_id.strip())
    if (run_id or "").strip():
        clauses.append("b.run_id = ?")
        args.append(run_id.strip())
    if (city or "").strip():
        clauses.append("LOWER(b.city) = LOWER(?)")
        args.append(city.strip())
    if (state or "").strip():
        states = [s.strip().upper() for s in state.split(",") if s.strip()]
        if states:
            placeholders = ",".join("?" * len(states))
            clauses.append(f"UPPER(b.state) IN ({placeholders})")
            args.extend(states)
    if (main_category or "").strip():
        clauses.append("LOWER(COALESCE(b.main_category,'')) LIKE ?")
        args.append(f"%{main_category.strip().lower()}%")
    if (source or "").strip():
        clauses.append("COALESCE(NULLIF(b.source,''), 'maps') = ?")
        args.append(source.strip().lower())
    sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    return sql, args


def iter_local_contacts(
    store,
    *,
    client_tag: str | None,
    plan_id: str = "",
    run_id: str = "",
    city: str = "",
    state: str = "",
    main_category: str = "",
    source: str = "",
    offset: int = 0,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Page local SQLite contacts joined to one matching business."""
    extra, extra_args = _biz_filter_sql(
        client_tag=client_tag,
        plan_id=plan_id,
        run_id=run_id,
        city=city,
        state=state,
        main_category=main_category,
        source=source,
    )
    # One business per contact (prefer matching place_id, then domain).
    sql = f"""
        SELECT
            c.id AS local_id,
            c.place_id,
            c.domain,
            c.name,
            c.title,
            c.email,
            c.source,
            c.source_tier,
            c.confidence,
            c.source_url,
            b.place_id AS resolved_place_id,
            b.city AS contact_city,
            b.state AS contact_state
        FROM contacts c
        JOIN businesses b
          ON (
            (c.place_id IS NOT NULL AND c.place_id != '' AND b.place_id = c.place_id)
            OR (
              (c.place_id IS NULL OR c.place_id = '')
              AND b.domain IS NOT NULL AND b.domain != '' AND b.domain = c.domain
            )
          )
        WHERE c.domain IS NOT NULL AND c.domain != ''
          {extra}
        GROUP BY c.id
        ORDER BY c.id
    """
    args = list(extra_args)
    if limit is not None:
        sql += " LIMIT ?"
        args = list(args) + [int(limit) + int(offset or 0)]
    rows = [dict(r) for r in store.conn.execute(sql, args)]
    if offset:
        rows = rows[int(offset) :]
    if limit is not None:
        rows = rows[: int(limit)]
    return rows


def _contact_row_for_supabase(
    rec: dict[str, Any],
    *,
    client_tag: str,
    plan_id: str = "",
    run_id: str = "",
    run_label: str = "",
) -> dict[str, Any] | None:
    domain = (rec.get("domain") or "").strip().lower()
    raw_name = (rec.get("name") or "").strip()
    email = (rec.get("email") or "").strip().lower()
    first, last = split_name(raw_name)
    key = natural_key(
        client_tag=client_tag,
        domain=domain,
        first_name=first,
        last_name=last,
        email=email,
    )
    if not domain or not key:
        return None
    title = (rec.get("title") or "").strip()
    place_id = (
        (rec.get("place_id") or "").strip()
        or (rec.get("resolved_place_id") or "").strip()
    )
    return {
        "domain": domain,
        "first_name": first or None,
        "last_name": last or None,
        "raw_name": raw_name or None,
        "job_title": title or None,
        "job_level": _job_level(title) or None,
        "email": email or None,
        "source_tool": source_tool_for(
            rec.get("source") or "", rec.get("source_tier") or ""
        ),
        "source_tier": (rec.get("source_tier") or rec.get("source") or "") or None,
        "source_url": (rec.get("source_url") or "").strip() or None,
        "confidence": rec.get("confidence"),
        "place_id": place_id or None,
        "contact_city": (rec.get("contact_city") or "").strip() or None,
        "contact_state": (rec.get("contact_state") or "").strip() or None,
        "client_tag": client_tag,
        "natural_key": key,
    }


def _existing_natural_keys(
    schema: str, table: str, keys: list[str]
) -> set[str]:
    if not keys:
        return set()
    cfg = supabase_config()
    found: set[str] = set()
    for i in range(0, len(keys), 200):
        chunk = keys[i : i + 200]
        quoted = ",".join(f'"{k}"' for k in chunk)
        url = (
            f"{cfg['url']}/rest/v1/{table}"
            f"?select=natural_key&natural_key=in.({quoted})"
        )
        try:
            _status, body = _request(
                "GET", url, cfg["key"], prefer="return=representation", schema=schema
            )
        except RuntimeError:
            return set()
        try:
            rows = json.loads(body or "[]")
        except json.JSONDecodeError:
            rows = []
        for row in rows:
            if isinstance(row, dict) and row.get("natural_key"):
                found.add(str(row["natural_key"]))
    return found


def sync_contacts(
    store,
    *,
    client_tag: str,
    table: str = "",
    schema: str = "",
    run_label: str = "",
    plan_id: str = "",
    run_id: str = "",
    city: str = "",
    state: str = "",
    main_category: str = "",
    source: str = "",
    center: str = "",
    radius_miles: float = 0.0,
    center_lat: float = 0.0,
    center_lng: float = 0.0,
    cursor: int = 0,
    page_size: int = PAGE_SIZE,
    max_pages: int = 0,
) -> dict[str, Any]:
    """Upsert local contacts into client_{tag}.contacts. Counts only."""
    target = resolve_sync_target(
        client_tag=client_tag, table=table, schema=schema, dataset="contacts"
    )
    supabase_config()
    scoped = _source_scope_set(
        city=city,
        state=state,
        main_category=main_category,
        plan_id=plan_id,
        run_id=run_id,
        source=source,
        center=center,
        radius_miles=float(radius_miles or 0),
        center_lat=float(center_lat or 0),
        center_lng=float(center_lng or 0),
    )
    source_client_tag = None if scoped else (target["client_tag"] or None)
    label = (run_label or "").strip() or target["client_tag"]
    page = max(1, int(page_size or PAGE_SIZE))
    offset = max(0, int(cursor or 0))
    pages = 0
    synced = 0
    updated = 0
    skipped = 0
    has_more = True
    last_offset = offset

    while has_more:
        if max_pages and pages >= max_pages:
            break
        raw_rows = iter_local_contacts(
            store,
            client_tag=source_client_tag,
            plan_id=plan_id,
            run_id=run_id,
            city=city,
            state=state,
            main_category=main_category,
            source=source,
            offset=offset,
            limit=page,
        )
        if not raw_rows:
            has_more = False
            break
        mapped: list[dict[str, Any]] = []
        for rec in raw_rows:
            row = _contact_row_for_supabase(
                rec,
                client_tag=target["client_tag"],
                plan_id=plan_id,
                run_id=run_id,
                run_label=label,
            )
            if row is None:
                skipped += 1
                continue
            mapped.append(row)
        existing = _existing_natural_keys(
            target["schema"], target["table"], [r["natural_key"] for r in mapped]
        )
        updated += sum(1 for r in mapped if r["natural_key"] in existing)
        synced += upsert_rows(
            target["table"],
            mapped,
            schema=target["schema"],
            on_conflict="natural_key",
        )
        pages += 1
        offset += len(raw_rows)
        last_offset = offset
        has_more = len(raw_rows) >= page

    return {
        "dataset": "contacts",
        "client_tag": target["client_tag"],
        "display_name": target.get("display_name") or None,
        "schema": target["schema"],
        "table": target["table"],
        "fqn": target["fqn"],
        "rows_synced": synced,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "has_more": has_more,
        "resume_token": str(last_offset),
        "cursor": last_offset,
        "page_size": page,
        "pages": pages,
        "run_label": label,
        "plan_id": plan_id or None,
        "run_id": run_id or None,
        "verify_sql": _verify_sql(target["fqn"], "contacts"),
        "scope": {
            "city": city or None,
            "state": state or None,
            "main_category": main_category or None,
            "source": source or None,
            "center": center or None,
            "radius_miles": float(radius_miles) if radius_miles else None,
            "source_filtered_by_client_tag": not scoped,
        },
    }


def iter_local_owners(store) -> list[dict[str, Any]]:
    """Every local owner with a name — not gated by plan/city/icp."""
    rows = store.conn.execute(
        """
        SELECT place_id, owner_name, owner_title, source
        FROM owners
        WHERE owner_name IS NOT NULL AND TRIM(owner_name) != ''
        ORDER BY place_id
        """
    )
    return [dict(r) for r in rows]


def backfill_lead_owners(
    store,
    *,
    schema: str,
    table: str,
) -> int:
    """PATCH owner_name/title onto existing client leads by place_id.

    Ignores plan/city/icp scope so owners on historical rows are not dropped.
    """
    owners = iter_local_owners(store)
    if not owners:
        return 0
    cfg = supabase_config()
    payload = [
        {
            "place_id": r["place_id"],
            "owner_name": (r.get("owner_name") or "").strip(),
            "owner_title": (r.get("owner_title") or "").strip() or None,
            "owner_source": (r.get("source") or "").strip() or None,
        }
        for r in owners
        if (r.get("place_id") or "").strip() and (r.get("owner_name") or "").strip()
    ]
    if not payload:
        return 0

    # Prefer a single RPC; fall back to per-place PATCH.
    try:
        url = f"{cfg['url']}/rest/v1/rpc/sync_client_lead_owners"
        _request(
            "POST",
            url,
            cfg["key"],
            body={"p_schema": schema, "p_rows": payload},
            prefer="return=representation",
            schema="public",
        )
        return len(payload)
    except RuntimeError:
        updated = 0
        for row in payload:
            pid = parse.quote(row["place_id"], safe="")
            url = f"{cfg['url']}/rest/v1/{table}?place_id=eq.{pid}"
            _request(
                "PATCH",
                url,
                cfg["key"],
                body={
                    "owner_name": row["owner_name"],
                    "owner_title": row["owner_title"],
                    "owner_source": row["owner_source"],
                },
                prefer="return=minimal",
                schema=schema,
            )
            updated += 1
        return updated


def sync_to_supabase(
    store,
    *,
    client_tag: str = "",
    table: str = "",
    schema: str = "",
    dataset: str = "",
    icp_only: bool = False,
    with_email: bool = False,
    truncate: bool = False,
    run_label: str = "",
    plan_id: str = "",
    run_id: str = "",
    city: str = "",
    state: str = "",
    main_category: str = "",
    source: str = "",
    center: str = "",
    radius_miles: float = 0.0,
    center_lat: float = 0.0,
    center_lng: float = 0.0,
    cursor: int = 0,
    page_size: int = PAGE_SIZE,
    max_pages: int = 0,
) -> dict[str, Any]:
    """Batch-upsert into the client's Supabase table. Counts only.

    client_tag selects the destination schema/table and stamps every row.
    dataset='contacts' writes client_{tag}.contacts. Default writes leads
    and always backfills owner_name/owner_title from the local owners table
    (not gated by plan_id / city / icp_only).
    """
    ds = (dataset or "").strip().lower()
    if truncate:
        raise ValueError("truncate is disabled; upsert only.")
    if ds in ("contacts", "contact"):
        return sync_contacts(
            store,
            client_tag=client_tag,
            table=table,
            schema=schema,
            run_label=run_label,
            plan_id=plan_id,
            run_id=run_id,
            city=city,
            state=state,
            main_category=main_category,
            source=source,
            center=center,
            radius_miles=radius_miles,
            center_lat=center_lat,
            center_lng=center_lng,
            cursor=cursor,
            page_size=page_size,
            max_pages=max_pages,
        )

    target = resolve_sync_target(
        client_tag=client_tag, table=table, schema=schema, dataset="leads"
    )
    label = (run_label or "").strip() or target["client_tag"]
    supabase_config()

    scoped = _source_scope_set(
        city=city,
        state=state,
        main_category=main_category,
        plan_id=plan_id,
        run_id=run_id,
        source=source,
        center=center,
        radius_miles=float(radius_miles or 0),
        center_lat=float(center_lat or 0),
        center_lng=float(center_lng or 0),
    )
    source_client_tag = None if scoped else (target["client_tag"] or None)

    synced_at = datetime.now(timezone.utc).isoformat()
    page = max(1, int(page_size or PAGE_SIZE))
    offset = max(0, int(cursor or 0))
    pages = 0
    synced = 0
    has_more = True
    last_offset = offset

    while has_more:
        if max_pages and pages >= max_pages:
            break
        batch = [
            _row_for_supabase(
                rec, label, synced_at, client_tag=target["client_tag"]
            )
            for rec in export.iter_leads(
                store,
                icp_only=icp_only,
                with_email=with_email,
                client_tag=source_client_tag,
                plan_id=plan_id or None,
                run_id=run_id or None,
                city=city or None,
                state=state or None,
                main_category=main_category or None,
                source=source or None,
                center=center or None,
                radius_miles=float(radius_miles) if radius_miles else None,
                center_lat=float(center_lat) if center_lat else None,
                center_lng=float(center_lng) if center_lng else None,
                order="name",
                limit=page,
                offset=offset,
            )
        ]
        if not batch:
            has_more = False
            break
        synced += upsert_rows(target["table"], batch, schema=target["schema"])
        pages += 1
        offset += len(batch)
        last_offset = offset
        has_more = len(batch) >= page

    owners_updated = backfill_lead_owners(
        store, schema=target["schema"], table=target["table"]
    )

    return {
        "dataset": "leads",
        "client_tag": target["client_tag"],
        "display_name": target.get("display_name") or None,
        "schema": target["schema"],
        "table": target["table"],
        "fqn": target["fqn"],
        "rows_synced": synced,
        "rows_updated": owners_updated,
        "rows_skipped": 0,
        "has_more": has_more,
        "resume_token": str(last_offset),
        "cursor": last_offset,
        "page_size": page,
        "pages": pages,
        "owners_backfilled": owners_updated,
        "run_label": label,
        "plan_id": plan_id or None,
        "run_id": run_id or None,
        "verify_sql": _verify_sql(target["fqn"], "leads"),
        "scope": {
            "city": city or None,
            "state": state or None,
            "main_category": main_category or None,
            "source": source or None,
            "center": center or None,
            "radius_miles": float(radius_miles) if radius_miles else None,
            "center_lat": float(center_lat) if center_lat else None,
            "center_lng": float(center_lng) if center_lng else None,
            "source_filtered_by_client_tag": not scoped,
        },
    }
