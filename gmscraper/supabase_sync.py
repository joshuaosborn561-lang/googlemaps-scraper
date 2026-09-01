"""Push export-shaped leads into Supabase for SQL / downstream joins."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

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


def _headers(key: str, *, prefer: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def _request(
    method: str,
    url: str,
    key: str,
    *,
    body: Any = None,
    prefer: str = "return=minimal",
) -> tuple[int, str]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers=_headers(key, prefer=prefer),
        method=method,
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase {method} {url} failed ({exc.code}): {detail[:500]}") from exc


def truncate_table(table: str = "maps_leads") -> None:
    cfg = supabase_config()
    # PostgREST requires a filter; delete every row that has a place_id.
    url = f"{cfg['url']}/rest/v1/{table}?place_id=not.is.null"
    _request("DELETE", url, cfg["key"], prefer="return=minimal")


def _row_for_supabase(rec: dict[str, Any], run_label: str, synced_at: str) -> dict[str, Any]:
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
        else:
            out[col] = "" if val is None else val
    out["run_label"] = run_label or ""
    out["synced_at"] = synced_at
    return out


def upsert_rows(table: str, rows: list[dict[str, Any]]) -> int:
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
        )
        synced += len(batch)
    return synced


def sync_to_supabase(
    store,
    *,
    table: str = "maps_leads",
    icp_only: bool = False,
    with_email: bool = False,
    truncate: bool = False,
    run_label: str = "",
    client_tag: str = "",
) -> dict[str, Any]:
    """Batch-upsert leads. Returns counts only — never row payloads."""
    table = (table or "maps_leads").strip() or "maps_leads"
    label = (run_label or "").strip()
    # Touch config early so missing env fails before truncate/work.
    supabase_config()

    if truncate:
        truncate_table(table)

    synced_at = datetime.now(timezone.utc).isoformat()
    rows = [
        _row_for_supabase(rec, label, synced_at)
        for rec in export.iter_leads(
            store,
            icp_only=icp_only,
            with_email=with_email,
            order="name",
            client_tag=client_tag,
        )
    ]
    n = upsert_rows(table, rows)
    return {
        "table": table,
        "rows_synced": n,
        "rows_skipped": 0,
        "run_label": label,
    }
