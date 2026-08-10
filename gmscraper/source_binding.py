"""Generic Supabase source binding — no client/vertical hardcoding.

Callers pass schema/table/column names; this module validates them, ensures
writeback columns exist, and reads/patches rows via PostgREST (+ RPCs when
the target project exposes public.pp_* helpers).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib import error, parse, request

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Columns resolve_places / pipeline may write back. Added automatically if absent.
WRITEBACK_COLUMNS: dict[str, str] = {
    "domain": "text",
    "website": "text",
    "phone": "text",
    "place_id": "text",
    "latitude": "double precision",
    "longitude": "double precision",
    "confidence": "double precision",
    "candidates": "integer",
    "business_name": "text",
    "resolved": "boolean",
    "resolved_at": "timestamptz",
    "resolve_raw": "jsonb",
}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass
class SourceBinding:
    project_id: str = ""
    schema: str = ""
    table: str = ""
    key_column: str = ""
    address_column: str = ""
    name_column: str = ""
    city_column: str = ""
    domain_column: str = "domain"
    resolved_column: str = "resolved"
    confidence_column: str = "confidence"
    order_by: str = ""
    where: str = ""
    # Resolved credentials (filled by resolve_binding)
    supabase_url: str = field(default="", repr=False)
    supabase_key: str = field(default="", repr=False)


class BindingError(ValueError):
    """Clear validation / schema errors for MCP callers."""


def _require_ident(value: str, label: str) -> str:
    v = (value or "").strip()
    if not v or not _IDENT.match(v):
        raise BindingError(f"Invalid {label}: {value!r}")
    return v


def resolve_credentials(project_id: str = "") -> dict[str, str]:
    """Pick Supabase URL/key for a project ref.

    Precedence for a given project_id:
      1. SUPABASE_URL_<project_id> + SUPABASE_SERVICE_ROLE_KEY_<project_id>
      2. LEADS_* when project_id matches LEADS_SUPABASE_PROJECT_ID
      3. Default SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY / ANON_KEY
    """
    pid = (project_id or "").strip()
    leads_pid = _env("LEADS_SUPABASE_PROJECT_ID", "kemvxzhcxvynmoutwdrh")

    if pid:
        url = _env(f"SUPABASE_URL_{pid}")
        key = (
            _env(f"SUPABASE_SERVICE_ROLE_KEY_{pid}")
            or _env(f"SUPABASE_ANON_KEY_{pid}")
            or _env(f"SUPABASE_KEY_{pid}")
        )
        if not url:
            url = f"https://{pid}.supabase.co"
        if not key and pid == leads_pid:
            url = _env("LEADS_SUPABASE_URL") or url
            key = (
                _env("LEADS_SUPABASE_SERVICE_ROLE_KEY")
                or _env("LEADS_SUPABASE_ANON_KEY")
            )
        if not key:
            # Fall back to default key only if default URL is the same project.
            default_url = _env("SUPABASE_URL").rstrip("/")
            if default_url.endswith(f"{pid}.supabase.co"):
                key = _env("SUPABASE_SERVICE_ROLE_KEY") or _env("SUPABASE_ANON_KEY")
        if not key:
            raise BindingError(
                f"No Supabase key for project_id={pid!r}. Set "
                f"SUPABASE_SERVICE_ROLE_KEY_{pid} or LEADS_SUPABASE_ANON_KEY."
            )
        return {"url": url.rstrip("/"), "key": key, "project_id": pid}

    url = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_ROLE_KEY") or _env("SUPABASE_ANON_KEY")
    if not url or not key:
        raise BindingError(
            "Supabase not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY)."
        )
    # Infer project id from host when possible.
    host = parse.urlparse(url).hostname or ""
    inferred = host.split(".")[0] if host.endswith(".supabase.co") else ""
    return {"url": url, "key": key, "project_id": inferred}


def resolve_binding(**kwargs: Any) -> SourceBinding:
    """Fill a SourceBinding from kwargs + env defaults; validate identifiers."""
    defaults = {
        "project_id": _env("LEADS_SUPABASE_PROJECT_ID")
        or _env("SUPABASE_PROJECT_ID")
        or "",
        "schema": _env("SOURCE_SCHEMA", "public"),
        "table": _env("SOURCE_TABLE", ""),
        "key_column": _env("SOURCE_KEY_COLUMN", "id"),
        "address_column": _env("SOURCE_ADDRESS_COLUMN", ""),
        "name_column": _env("SOURCE_NAME_COLUMN", ""),
        "city_column": _env("SOURCE_CITY_COLUMN", ""),
        "domain_column": _env("SOURCE_DOMAIN_COLUMN", "domain"),
        "resolved_column": _env("SOURCE_RESOLVED_COLUMN", "resolved"),
        "confidence_column": _env("SOURCE_CONFIDENCE_COLUMN", "confidence"),
        "order_by": _env("SOURCE_ORDER_BY", ""),
        "where": _env("SOURCE_WHERE", ""),
    }
    # Empty strings must not wipe env defaults (auto-resume used to pass
    # project_id="" and silently target the wrong Supabase project).
    cleaned = {
        k: v
        for k, v in kwargs.items()
        if v is not None and not (isinstance(v, str) and not v.strip())
    }
    merged = {**defaults, **cleaned}
    schema = _require_ident(str(merged.get("schema") or ""), "schema")
    table = _require_ident(str(merged.get("table") or ""), "table")
    key_column = _require_ident(str(merged.get("key_column") or ""), "key_column")

    for opt in (
        "address_column", "name_column", "city_column",
        "domain_column", "resolved_column", "confidence_column",
    ):
        raw = str(merged.get(opt) or "").strip()
        if raw:
            _require_ident(raw, opt)

    if not (merged.get("address_column") or merged.get("name_column")):
        raise BindingError(
            "Provide address_column and/or name_column on the source binding."
        )

    creds = resolve_credentials(str(merged.get("project_id") or ""))
    binding = SourceBinding(
        project_id=creds["project_id"],
        schema=schema,
        table=table,
        key_column=key_column,
        address_column=str(merged.get("address_column") or "").strip(),
        name_column=str(merged.get("name_column") or "").strip(),
        city_column=str(merged.get("city_column") or "").strip(),
        domain_column=str(merged.get("domain_column") or "domain").strip(),
        resolved_column=str(merged.get("resolved_column") or "resolved").strip(),
        confidence_column=str(merged.get("confidence_column") or "confidence").strip(),
        order_by=str(merged.get("order_by") or "").strip(),
        where=str(merged.get("where") or "").strip(),
        supabase_url=creds["url"],
        supabase_key=creds["key"],
    )
    return binding


def _headers(binding: SourceBinding, *, prefer: str = "return=minimal") -> dict[str, str]:
    return {
        "apikey": binding.supabase_key,
        "Authorization": f"Bearer {binding.supabase_key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
        "Accept-Profile": binding.schema,
        "Content-Profile": binding.schema,
    }


def _request(
    binding: SourceBinding,
    method: str,
    path: str,
    *,
    body: Any = None,
    prefer: str = "return=minimal",
    profile: str | None = None,
    timeout: int = 120,
) -> tuple[int, str]:
    url = f"{binding.supabase_url}/rest/v1/{path.lstrip('/')}"
    headers = _headers(binding, prefer=prefer)
    if profile:
        headers["Accept-Profile"] = profile
        headers["Content-Profile"] = profile
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BindingError(
            f"Supabase {method} {url} failed ({exc.code}): {detail[:500]}"
        ) from exc


def rpc(binding: SourceBinding, fn: str, args: dict[str, Any]) -> Any:
    """Call a public RPC (pp_* helpers)."""
    status, text = _request(
        binding,
        "POST",
        f"rpc/{fn}",
        body=args,
        prefer="return=representation",
        profile="public",
    )
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def list_columns(binding: SourceBinding) -> set[str]:
    """Return column names for schema.table via information_schema RPC or OpenAPI.

    Falls back to selecting zero rows with Prefer: count and OpenAPI definition.
    """
    # Try OpenAPI (PostgREST) — works when schema is exposed.
    try:
        url = f"{binding.supabase_url}/rest/v1/"
        req = request.Request(
            url,
            headers={
                "apikey": binding.supabase_key,
                "Authorization": f"Bearer {binding.supabase_key}",
                "Accept": "application/openapi+json",
            },
            method="GET",
        )
        with request.urlopen(req, timeout=60) as resp:
            spec = json.loads(resp.read().decode())
        # Paths look like /table; schemas may be namespaced in definitions
        defs = spec.get("definitions") or {}
        key = binding.table
        if key in defs:
            props = (defs[key].get("properties") or {}).keys()
            return set(props)
    except Exception:
        pass

    # Fallback: select with limit 0 through SECURITY DEFINER RPC and infer from empty.
    # Use pp_select_rows with limit 0 — still returns []. Validate table exists via count.
    try:
        rpc(
            binding,
            "pp_count_rows",
            {
                "p_schema": binding.schema,
                "p_table": binding.table,
                "p_where": None,
            },
        )
    except BindingError as exc:
        raise BindingError(
            f"Missing table {binding.schema}.{binding.table}: {exc}"
        ) from exc
    return set()


def validate_binding(binding: SourceBinding) -> SourceBinding:
    """Ensure schema/table exist; clear error if not."""
    try:
        rpc(
            binding,
            "pp_count_rows",
            {
                "p_schema": binding.schema,
                "p_table": binding.table,
                "p_where": None,
            },
        )
    except BindingError as exc:
        raise BindingError(
            f"Cannot access {binding.schema}.{binding.table} on "
            f"project {binding.project_id}: {exc}"
        ) from exc
    return binding


def ensure_writeback_columns(binding: SourceBinding) -> dict[str, Any]:
    """ADD COLUMN IF NOT EXISTS for resolve writeback fields."""
    cols = {
        binding.domain_column: "text",
        binding.resolved_column: "boolean",
        binding.confidence_column: "double precision",
        "website": "text",
        "phone": "text",
        "place_id": "text",
        "latitude": "double precision",
        "longitude": "double precision",
        "candidates": "integer",
        "business_name": "text",
        "resolved_at": "timestamptz",
        "resolve_raw": "jsonb",
    }
    # Merge known defaults
    for k, v in WRITEBACK_COLUMNS.items():
        cols.setdefault(k, v)
    n = rpc(
        binding,
        "pp_ensure_columns",
        {
            "p_schema": binding.schema,
            "p_table": binding.table,
            "p_columns": cols,
        },
    )
    return {"columns_ensured": n, "columns": sorted(cols)}


def pending_where(binding: SourceBinding) -> str:
    """WHERE clause for unresolved rows + optional caller predicate."""
    parts = [
        f"({binding.resolved_column} IS NULL OR {binding.resolved_column} = false)"
    ]
    if binding.where:
        parts.append(f"({binding.where})")
    return " AND ".join(parts)


def details_only_where(binding: SourceBinding) -> str:
    """Rows with a place_id but no website — backfill Place Details only."""
    parts = [
        "place_id IS NOT NULL",
        "place_id != ''",
        "(website IS NULL OR website = '')",
    ]
    if binding.where:
        parts.append(f"({binding.where})")
    return " AND ".join(parts)


def count_pending(binding: SourceBinding, *, details_only: bool = False) -> int:
    where = details_only_where(binding) if details_only else pending_where(binding)
    n = rpc(
        binding,
        "pp_count_rows",
        {
            "p_schema": binding.schema,
            "p_table": binding.table,
            "p_where": where,
        },
    )
    return int(n or 0)


def fetch_pending(
    binding: SourceBinding,
    *,
    limit: int = 0,
    offset: int = 0,
    details_only: bool = False,
) -> list[dict[str, Any]]:
    cols = [
        binding.key_column,
        binding.resolved_column,
        binding.confidence_column,
        binding.domain_column,
    ]
    for c in (
        binding.address_column,
        binding.name_column,
        binding.city_column,
        "website",
        "phone",
        "place_id",
        "latitude",
        "longitude",
        "candidates",
        "business_name",
        "resolve_raw",
    ):
        if c and c not in cols:
            cols.append(c)
    where = details_only_where(binding) if details_only else pending_where(binding)
    rows = rpc(
        binding,
        "pp_select_rows",
        {
            "p_schema": binding.schema,
            "p_table": binding.table,
            "p_columns": cols,
            "p_where": where,
            "p_order_by": binding.order_by or binding.key_column,
            "p_limit": int(limit) if limit and limit > 0 else 100000,
            "p_offset": int(offset or 0),
        },
    )
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def patch_row(
    binding: SourceBinding,
    key_value: Any,
    patch: dict[str, Any],
) -> bool:
    """Write back fields for one row. Marks progress immediately."""
    clean = {k: v for k, v in patch.items() if _IDENT.match(k)}
    ok = rpc(
        binding,
        "pp_patch_row",
        {
            "p_schema": binding.schema,
            "p_table": binding.table,
            "p_key_column": binding.key_column,
            "p_key_value": str(key_value),
            "p_patch": clean,
        },
    )
    return bool(ok)


def with_filters(
    binding: SourceBinding,
    *,
    where: str = "",
    order_by: str = "",
    limit: int = 0,  # noqa: ARG001 — carried by callers, not binding
) -> SourceBinding:
    return replace(
        binding,
        where=where.strip() if where.strip() else binding.where,
        order_by=order_by.strip() if order_by.strip() else binding.order_by,
    )
