"""Push export-shaped leads into per-client Supabase tables.

Default path: client_tag → client_<slug>.leads (never a shared maps_leads dump
unless explicitly overridden with table= for legacy).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

from . import clients as client_reg
from . import export

BATCH_SIZE = 500

# Columns written to Supabase. Matches export_csv + run_label + synced_at.
SYNC_COLUMNS = export.COLUMNS + ["run_label", "synced_at"]


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
    cfg = supabase_config()
    url = f"{cfg['url']}/rest/v1/{table}?place_id=not.is.null"
    _request("DELETE", url, cfg["key"], prefer="return=minimal", schema=schema)


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
        elif col == "reviews":
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
) -> int:
    if not rows:
        return 0
    cfg = supabase_config()
    url = (
        f"{cfg['url']}/rest/v1/{table}"
        f"?on_conflict=place_id,run_label"
    )
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
) -> dict[str, str]:
    """Pick schema/table from client registry unless explicitly overridden."""
    tag = (client_tag or "").strip()
    explicit_table = (table or "").strip()
    explicit_schema = (schema or "").strip()

    if tag:
        client = client_reg.resolve_client(tag)
        assert client is not None
        return {
            "client_tag": client.slug,
            "schema": explicit_schema or client.supabase_schema,
            "table": explicit_table or client.leads_table,
            "fqn": (
                f"{explicit_schema or client.supabase_schema}."
                f"{explicit_table or client.leads_table}"
            ),
            "display_name": client.display_name,
        }

    # Legacy escape hatch: shared maps_leads only when no client_tag.
    if explicit_table and explicit_table != "maps_leads":
        return {
            "client_tag": "",
            "schema": explicit_schema or "public",
            "table": explicit_table,
            "fqn": f"{explicit_schema or 'public'}.{explicit_table}",
            "display_name": "",
        }
    raise ValueError(
        "client_tag is required for sync_to_supabase so each client lands in "
        "its own table (e.g. client_tag='peterson' → peterson_leads, "
        "client_tag='basco' → basco_leads). "
        "Call list_clients() for the registry."
    )


def sync_to_supabase(
    store,
    *,
    client_tag: str = "",
    table: str = "",
    schema: str = "",
    icp_only: bool = False,
    with_email: bool = False,
    truncate: bool = False,
    run_label: str = "",
    plan_id: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Batch-upsert leads into the client's Supabase table. Counts only."""
    target = resolve_sync_target(
        client_tag=client_tag, table=table, schema=schema
    )
    label = (run_label or "").strip() or target["client_tag"]
    # Touch config early so missing env fails before truncate/work.
    supabase_config()

    if truncate:
        truncate_table(target["table"], schema=target["schema"])

    synced_at = datetime.now(timezone.utc).isoformat()
    rows = [
        _row_for_supabase(
            rec, label, synced_at, client_tag=target["client_tag"]
        )
        for rec in export.iter_leads(
            store,
            icp_only=icp_only,
            with_email=with_email,
            client_tag=target["client_tag"] or None,
            plan_id=plan_id or None,
            run_id=run_id or None,
            order="name",
        )
    ]
    n = upsert_rows(target["table"], rows, schema=target["schema"])
    return {
        "client_tag": target["client_tag"],
        "display_name": target.get("display_name") or None,
        "schema": target["schema"],
        "table": target["table"],
        "fqn": target["fqn"],
        "rows_synced": n,
        "rows_skipped": 0,
        "run_label": label,
        "plan_id": plan_id or None,
        "run_id": run_id or None,
    }
