"""Claude MCP server for the Google Maps lead scraper.

v1.8 outcome-first: prefer resolve_addresses / run_owner_lane / run_lead_list /
outcome_status. Low-level stage tools remain for advanced/debug use.
No connector auth and no spend-approval gate.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from mcp_server.approvals import (
    resolve_plan,
    save_plan_record,
)
from mcp_server.playbook import FIND_LEADS_PROMPT, INSTRUCTIONS, WHEN_TO_USE_PROMPT

ROOT = Path(__file__).resolve().parent.parent

mcp = MCPServer(
    name="google-maps-scraper",
    title="Google Maps Scraper",
    description=(
        "Outcome-first US B2B lead MCP: resolve addresses to businesses, "
        "owner/mailing lanes, and local Maps lead lists. Prefer "
        "resolve_addresses / run_owner_lane / run_lead_list / outcome_status."
    ),
    instructions=INSTRUCTIONS,
    website_url="https://google-maps-mcp-production-88a3.up.railway.app/mcp",
    # Bump when annotations/schemas change so Claude refreshes its tool cache.
    version="1.8.0",
)


def _ann(
    title: str,
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> ToolAnnotations:
    """Build fully-specified tool annotations (no None defaults).

    Claude's connector treats a missing destructiveHint as confirmation-gated
    ("No approval received"). Always set all four hints explicitly.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def _iter_registered_tools() -> list[Any]:
    """Sync access to registered tools (list_tools() is async)."""
    return list(mcp._tool_manager.list_tools())


def _dump_tool_annotations() -> None:
    """Print every tool's annotation set at startup (deploy-log drift check)."""
    try:
        tools = _iter_registered_tools()
    except Exception as exc:  # noqa: BLE001
        print(f"tool annotation dump failed: {exc}", flush=True)
        return
    print(f"MCP tools ({len(tools)}) annotations:", flush=True)
    for t in sorted(tools, key=lambda x: x.name):
        ann = getattr(t, "annotations", None)
        if ann is None:
            print(f"  {t.name}: annotations=<none>", flush=True)
            continue
        dump = (
            ann.model_dump()
            if hasattr(ann, "model_dump")
            else {
                "title": getattr(ann, "title", None),
                "read_only_hint": getattr(ann, "read_only_hint", None),
                "destructive_hint": getattr(ann, "destructive_hint", None),
                "idempotent_hint": getattr(ann, "idempotent_hint", None),
                "open_world_hint": getattr(ann, "open_world_hint", None),
            }
        )
        ro = dump.get("read_only_hint", dump.get("readOnlyHint"))
        dest = dump.get("destructive_hint", dump.get("destructiveHint"))
        idem = dump.get("idempotent_hint", dump.get("idempotentHint"))
        ow = dump.get("open_world_hint", dump.get("openWorldHint"))
        title = dump.get("title")
        missing = [
            k
            for k, v in (
                ("readOnlyHint", ro),
                ("destructiveHint", dest),
                ("idempotentHint", idem),
                ("openWorldHint", ow),
            )
            if v is None
        ]
        flag = f" MISSING={missing}" if missing else ""
        print(
            f"  {t.name}: title={title!r} readOnly={ro} destructive={dest} "
            f"idempotent={idem} openWorld={ow}{flag}",
            flush=True,
        )


async def _tool_call_log_middleware(ctx: Any, call_next: Any) -> Any:
    """Log every inbound tools/call so client-side refusals are distinguishable."""
    method = getattr(ctx, "method", None) or ""
    if method != "tools/call":
        return await call_next(ctx)
    params = getattr(ctx, "params", None) or {}
    if isinstance(params, dict):
        name = params.get("name") or "?"
        args = params.get("arguments") or {}
    else:
        name = getattr(params, "name", None) or "?"
        args = getattr(params, "arguments", None) or {}
    arg_keys = sorted(args.keys()) if isinstance(args, dict) else []
    req_id = getattr(ctx, "request_id", None)
    print(
        f"tools/call inbound name={name!r} request_id={req_id!r} arg_keys={arg_keys}",
        flush=True,
    )
    try:
        result = await call_next(ctx)
    except Exception as exc:  # noqa: BLE001
        print(
            f"tools/call FAILED name={name!r} request_id={req_id!r} "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        raise
    is_err = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    print(
        f"tools/call done name={name!r} request_id={req_id!r} is_error={is_err}",
        flush=True,
    )
    return result


# Observe every tools/call that reaches the server (client-side gates never hit this).
mcp.middleware.append(_tool_call_log_middleware)



@mcp.resource(
    "gmscraper://playbook",
    name="playbook",
    title="How to use Google Maps Scraper",
    description="When to use this MCP and the exact plan → run workflow.",
    mime_type="text/markdown",
)
def playbook_resource() -> str:
    return INSTRUCTIONS


@mcp.prompt(
    name="find_leads",
    title="Find local business leads",
    description=(
        "Run the full Google Maps lead flow for a plain-English brief "
        "(plan → cost → run → CSV)."
    ),
)
def find_leads_prompt(brief: str = "local businesses in a US state") -> str:
    return FIND_LEADS_PROMPT.format(brief=brief.strip() or "local businesses in a US state")


@mcp.prompt(
    name="when_to_use",
    title="When to use this scraper",
    description="Decide whether the Google Maps Scraper MCP applies to the user's request.",
)
def when_to_use_prompt() -> str:
    return WHEN_TO_USE_PROMPT


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _ensure_repo_cwd() -> None:
    os.chdir(ROOT)


def _settings():
    from gmscraper.config import settings

    return settings


def _store(db: str | None = None):
    from gmscraper.config import DEFAULT_DB
    from gmscraper.store import Store

    return Store(db or str(DEFAULT_DB))


def _llm(model: str = "", provider: str = ""):
    from gmscraper.config import settings
    from gmscraper.llm import make_llm

    return make_llm(settings, model=model, provider=provider)


def _load_verticals():
    from gmscraper.cli import load_verticals
    from gmscraper.config import DEFAULT_CATEGORIES

    return load_verticals(DEFAULT_CATEGORIES)


def _ensure_zips_file() -> Path:
    from gmscraper import zips
    from gmscraper.config import DEFAULT_ZIPS

    path = Path(DEFAULT_ZIPS)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        zips.build(str(path))
    return path


def _parse_overage(cost_text_lines: list[str]) -> tuple[float | None, bool]:
    """Return (overage_usd, blocked) from brief.cost_lines output."""
    blocked = False
    overage: float | None = 0.0
    for line in cost_text_lines:
        if "BLOCKED" in line:
            blocked = True
            overage = None
            break
        match = re.search(r"est\. cost\s+\$([0-9,.]+)\s+overage", line)
        if match:
            overage = float(match.group(1).replace(",", ""))
        elif "fits inside this cycle's quota" in line:
            overage = 0.0
    return overage, blocked


def _apply_geo_overrides(
    plan,
    *,
    zips: str = "",
    center: str = "",
    radius_miles: float = 0.0,
    exclude_categories: str = "",
) -> dict[str, Any]:
    """Apply MCP overrides onto a Plan and resolve ZIP rows.

    Precedence: explicit zips > center+radius > states.
    Persists the resolved ZIP list onto plan.zips so run_leads uses it verbatim.
    """
    from gmscraper import brief as brief_mod
    from gmscraper import zips as zips_mod

    extras = brief_mod._parse_exclude_list(exclude_categories)
    plan.apply_exclusions(extras)

    if zips.strip():
        plan.zips = zips_mod.parse_zip_list(zips)
        # Explicit list wins — clear radius so resolve uses zips.
        # Keep center/radius on the plan only as metadata if caller also sent them.
    elif center.strip() and radius_miles > 0:
        plan.center = center.strip()
        plan.radius_miles = float(radius_miles)
    elif plan.center and plan.radius_miles > 0:
        pass  # from LLM planner
    else:
        plan.radius_miles = float(plan.radius_miles or 0) or 0.0

    # Resolve center coordinates when we have a radius brief/override.
    if not plan.zips and plan.center and plan.radius_miles > 0:
        lat, lng, label = zips_mod.parse_center(plan.center)
        plan.center = label
        plan.center_lat = lat
        plan.center_lng = lng

    zip_path = str(_ensure_zips_file())
    zip_rows, geo_meta = zips_mod.resolve_zip_rows(
        zip_path,
        zips=plan.zips or None,
        center=plan.center or None,
        radius_miles=plan.radius_miles or None,
        center_lat=plan.center_lat,
        center_lng=plan.center_lng,
        states=plan.states or None,
    )

    # Persist the exact ZIP list so run_leads scrapes only these.
    plan.zips = [r["zip"] for r in zip_rows]
    if geo_meta.get("center_lat") is not None:
        plan.center_lat = geo_meta["center_lat"]
        plan.center_lng = geo_meta["center_lng"]
    if geo_meta.get("center"):
        plan.center = geo_meta["center"]
    if geo_meta.get("radius_miles"):
        plan.radius_miles = float(geo_meta["radius_miles"])

    return {"zip_rows": zip_rows, "geo_meta": geo_meta}


def _plan_cost_bundle(
    plan,
    zip_limit: int | None = None,
    *,
    zips: str = "",
    center: str = "",
    radius_miles: float = 0.0,
    exclude_categories: str = "",
) -> dict[str, Any]:
    from gmscraper import brief as brief_mod
    from gmscraper.config import settings

    resolved = _apply_geo_overrides(
        plan,
        zips=zips,
        center=center,
        radius_miles=radius_miles,
        exclude_categories=exclude_categories,
    )
    zip_rows = resolved["zip_rows"]
    if zip_limit:
        zip_rows = zip_rows[:zip_limit]
        plan.zips = [r["zip"] for r in zip_rows]

    store = _store()
    used = store.requests_this_cycle(settings.quota_reset_day)
    requests = len(zip_rows) * len(plan.categories)
    cost_lines = brief_mod.cost_lines(requests, settings.plan, used)
    overage, blocked = _parse_overage(cost_lines)
    description = plan.describe(len(zip_rows), settings.plan, used)
    return {
        "plan": plan.__dict__,
        "zip_count": len(zip_rows),
        "requests": requests,
        "description": description,
        "cost_lines": cost_lines,
        "estimated_overage_usd": overage,
        "blocked": blocked,
        "maps_plan": settings.maps_plan,
        "quota_used": used,
        "geo": resolved["geo_meta"],
        "center": plan.center,
        "center_lat": plan.center_lat,
        "center_lng": plan.center_lng,
        "radius_miles": plan.radius_miles,
        "exclude_categories": plan.exclude_categories,
        "sample_zips": plan.zips[:12],
    }


def _save_plan(plan, brief: str) -> Path:
    from gmscraper import brief as brief_mod

    plans_dir = ROOT / "data" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", plan.vertical.lower()).strip("-") or "plan"
    path = plans_dir / f"{slug}-{int(__import__('time').time())}.json"
    brief_mod.save(plan, str(path))
    # stash brief alongside for auditing
    path.with_suffix(".brief.txt").write_text(brief, encoding="utf-8")
    return path


def _zip_rows_for_plan(plan) -> list[dict[str, str]]:
    """Load ZIP rows for a saved plan (explicit list wins)."""
    from gmscraper import zips as zips_mod

    zip_path = str(_ensure_zips_file())
    rows, _ = zips_mod.resolve_zip_rows(
        zip_path,
        zips=plan.zips or None,
        center=plan.center or None,
        radius_miles=plan.radius_miles or None,
        center_lat=plan.center_lat,
        center_lng=plan.center_lng,
        states=plan.states or None,
    )
    return rows


def _remote_base() -> str:
    return os.environ.get("GMAPS_API_BASE", "").rstrip("/")


def _remote_json(method: str, path: str, body: dict | None = None) -> Any:
    base = _remote_base()
    if not base:
        raise ValueError(
            "GMAPS_API_BASE is not set. Point it at the Railway app, e.g. "
            "https://google-maps-scraper-production-41db.up.railway.app"
        )
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Remote API {exc.code}: {detail}") from exc


# ---------------------------------------------------------------------------
# Read-only / free tools
# ---------------------------------------------------------------------------


def _supabase_project_ref(url: str = "") -> str | None:
    raw = (url or os.environ.get("SUPABASE_URL") or "").strip()
    if not raw:
        return None
    # https://<ref>.supabase.co → ref
    try:
        host = raw.split("://", 1)[-1].split("/", 1)[0]
        if host.endswith(".supabase.co"):
            return host.split(".")[0] or None
    except Exception:  # noqa: BLE001
        return None
    return None


@mcp.tool(
    annotations=_ann(
        'Health / config check',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def health() -> str:
    """Check whether this lead-scraper MCP is ready (Maps plan, API keys, paths).

    Call first if a paid run fails or you're unsure keys are configured.
    """
    _ensure_repo_cwd()
    settings = _settings()
    from gmscraper.config import DEFAULT_CATEGORIES, DEFAULT_DB, DEFAULT_ZIPS

    supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
    supabase_key = bool(
        (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        or (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    )
    from gmscraper.apify_contacts import apify_token_valid

    apify_token_set = bool(settings.apify_token)
    apify_token_ok = apify_token_set and apify_token_valid(settings.apify_token)
    project_ref = _supabase_project_ref(supabase_url)
    leads_ref = (os.environ.get("LEADS_SUPABASE_PROJECT_ID") or "").strip() or None
    return _json(
        {
            "ok": True,
            "maps_plan": settings.maps_plan,
            "rapidapi_configured": bool(settings.rapidapi_key),
            "llm_provider": settings.llm_provider,
            "openai_configured": bool(settings.openai_api_key),
            "apify_token_set": apify_token_set,
            "apify_token_valid": apify_token_ok,
            "apify_configured": apify_token_ok,
            "apify_contact_actor": settings.apify_contact_actor,
            "apify_content_actor": settings.apify_content_actor,
            "apify_max_cost_usd": settings.apify_max_cost_usd,
            "supabase_configured": bool(supabase_url and supabase_key),
            "supabase_url": supabase_url or None,
            "supabase_project_ref": project_ref,
            "leads_supabase_project_id": leads_ref,
            "auto_resume": os.environ.get("MCP_AUTO_RESUME", "true").lower()
            not in ("0", "false", "no"),
            "backlog_drain": os.environ.get("MCP_BACKLOG_DRAIN", "true").lower()
            not in ("0", "false", "no"),
            "db": str(DEFAULT_DB),
            "zips": str(DEFAULT_ZIPS),
            "zips_ready": Path(DEFAULT_ZIPS).exists(),
            "categories_file": str(DEFAULT_CATEGORIES),
            "remote_api": _remote_base() or None,
            "auth": "none",
            "mcp_version": "1.8.0",
            "primary_tools": [
                "resolve_addresses",
                "run_owner_lane",
                "run_lead_list",
                "outcome_status",
            ],
        }
    )


# ---------------------------------------------------------------------------
# Primary outcome tools (v1.8) — Claude should prefer these over stage tools.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=_ann(
        'Resolve addresses → businesses',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def resolve_addresses(
    addresses: str = "",
    schema: str = "",
    table: str = "",
    key_column: str = "",
    address_column: str = "",
    name_column: str = "",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    project_id: str = "",
    method: str = "auto",
    limit: int = 0,
    min_confidence: float = 0.35,
    estimate_only: bool = False,
    background: bool = True,
) -> str:
    """PRIMARY: Find the business at each address (name, website/domain, phone).

    Pass ``addresses`` as a newline-separated list or JSON array for ad-hoc
    lookups ("here are these addresses — what businesses are there?").
    OR bind a Supabase table (schema/table/key_column/address_column).

    method=auto uses Maps then SERP for building-only / empty misses (recommended).
    method=serp / maps forces one engine. Returns outcome + useful_with_domain;
    outcome=no_value means nothing usable was written — say so to the user.
    Prefer estimate_only=true once before a paid run. Counts + results (ad-hoc).
    """
    _ensure_repo_cwd()
    from gmscraper import outcomes as oc
    from mcp_server.errors import tool_error_from_exception

    resolved_project = _default_leads_project_id(table, project_id)
    resolved_schema = _default_source_schema(table, schema) if table else schema

    def _run() -> dict[str, Any]:
        try:
            return oc.resolve_addresses(
                addresses=addresses,
                schema=resolved_schema,
                table=table,
                key_column=key_column,
                address_column=address_column,
                name_column=name_column,
                city_column=city_column,
                where=where,
                order_by=order_by,
                project_id=resolved_project,
                method=method,
                limit=int(limit or 0),
                min_confidence=float(min_confidence or 0.35),
                estimate_only=bool(estimate_only),
                on_progress=lambda **p: _job_progress(**p),
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error_from_exception(exc)

    # Ad-hoc lists are usually small — run sync unless background forced with table.
    ad_hoc = bool((addresses or "").strip()) and not table
    if background and _http_mode() and not estimate_only and not ad_hoc:
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        meta = {
            "kind": "resolve_addresses",
            "table": table,
            "schema": resolved_schema,
            "method": method,
            "limit": limit,
            "project_id": resolved_project,
        }
        before = find_active_by_queue_key(make_queue_key("resolve_addresses", meta))
        job = start_job("resolve_addresses", _run, meta=meta, priority=10)
        return _json(
            _started_response(job, attached=before is not None and before.id == job.id)
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Owner / mailing-operator lane',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def run_owner_lane(
    states: str = "TX",
    rebuild_operators: bool = False,
    operators_dry_run: bool = True,
    min_parcels: int = 1,
    center: str = "",
    radius_miles: float = 0.0,
    owner_segments: str = "private",
    resolve_limit: int = 0,
    method: str = "serp",
    min_confidence: float = 0.35,
    project_id: str = "",
    estimate_only: bool = False,
    background: bool = True,
) -> str:
    """PRIMARY: Parcel mailing addresses → real operator companies (any market).

    Collapses shell LLCs by mailing, drops OOS mailings, classifies each
    operator into owner_segment (municipal / education / healthcare /
    housing_authority / utility_transit / religious_nonprofit / private /
    unclassified). Rebuild stores ALL segments; resolve spends only on
    owner_segments= (default 'private'). Use 'education' or 'all' etc. later
    without rebuilding. Scope buildings with center + radius_miles.

    Dry rebuild returns segments= breakdown + top_by_segment before confirm.
    """
    _ensure_repo_cwd()
    from gmscraper import outcomes as oc
    from mcp_server.errors import tool_error_from_exception

    resolved_project = _default_leads_project_id("operators", project_id)

    def _run() -> dict[str, Any]:
        try:
            return oc.run_owner_lane(
                states=states or "TX",
                rebuild_operators=bool(rebuild_operators),
                operators_dry_run=bool(operators_dry_run),
                min_parcels=int(float(min_parcels or 1)),
                center=center or "",
                radius_miles=float(radius_miles or 0),
                owner_segments=owner_segments or "private",
                resolve_limit=int(resolve_limit or 0),
                method=method or "serp",
                min_confidence=float(min_confidence or 0.35),
                project_id=resolved_project,
                estimate_only=bool(estimate_only),
                on_progress=lambda **p: _job_progress(**p),
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error_from_exception(exc)

    # Dry rebuilds are sync-friendly (no spend) — avoid background job opacity.
    dry_rebuild = bool(rebuild_operators) and (
        bool(operators_dry_run) or bool(estimate_only)
    )
    if background and _http_mode() and not estimate_only and not dry_rebuild:
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        meta = {
            "states": states,
            "rebuild_operators": rebuild_operators,
            "operators_dry_run": bool(operators_dry_run),
            "center": center,
            "radius_miles": radius_miles,
            "min_parcels": min_parcels,
            "owner_segments": owner_segments,
            "resolve_limit": resolve_limit,
            "method": method,
            "min_confidence": min_confidence,
            "project_id": resolved_project,
        }
        before = find_active_by_queue_key(make_queue_key("run_owner_lane", meta))
        job = start_job("run_owner_lane", _run, meta=meta, priority=10)
        return _json(
            _started_response(job, attached=before is not None and before.id == job.id)
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Local Maps lead list (plan→scrape)',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def run_lead_list(
    brief: str = "",
    client_tag: str = "",
    center: str = "",
    radius_miles: float = 0.0,
    states: str = "",
    zips: str = "",
    vertical: str = "",
    estimate_only: bool = False,
    background: bool = True,
) -> str:
    """PRIMARY: Build a local-business lead list from Google Maps for a niche+geo.

    One-shot wrapper: plans from the brief (or explicit geo/vertical), returns
    cost when estimate_only=true, otherwise starts the scrape pipeline. Pass
    client_tag for multi-client isolation (peterson, basco, …).

    For address→business on a pasted list, use resolve_addresses instead.
    For parcel/mailing owners, use run_owner_lane instead.
    """
    _ensure_repo_cwd()
    from mcp_server.errors import tool_error_from_exception

    text = (brief or "").strip()
    if not text:
        bits = [b for b in (vertical, states or center) if b]
        text = " in ".join(bits) if bits else ""
    if states and states.upper() not in text.upper():
        text = f"{text} in {states}".strip()
    if len(text) < 8:
        return _json(
            {
                "started": False,
                "outcome": "nothing_to_do",
                "warning": "Pass brief= (niche + geography) or vertical= + states=/center=.",
            }
        )

    try:
        plan_raw = plan_leads(
            brief=text,
            zips=zips,
            center=center,
            radius_miles=float(radius_miles or 0),
        )
    except Exception as exc:  # noqa: BLE001
        return _json(tool_error_from_exception(exc))

    if estimate_only:
        try:
            plan_obj = json.loads(plan_raw) if isinstance(plan_raw, str) else plan_raw
        except json.JSONDecodeError:
            return plan_raw if isinstance(plan_raw, str) else _json(plan_raw)
        if isinstance(plan_obj, dict):
            plan_obj = {
                **plan_obj,
                "estimate_only": True,
                "started": False,
                "primary_tool": "run_lead_list",
                "client_tag": client_tag or None,
            }
            return _json(plan_obj)
        return plan_raw if isinstance(plan_raw, str) else _json(plan_raw)

    try:
        plan_obj = json.loads(plan_raw) if isinstance(plan_raw, str) else plan_raw
    except json.JSONDecodeError:
        return plan_raw if isinstance(plan_raw, str) else _json(plan_raw)

    if isinstance(plan_obj, dict) and (
        plan_obj.get("blocked") or str(plan_obj.get("status") or "").upper() == "BLOCKED"
    ):
        return _json(
            {
                "started": False,
                "blocked": True,
                "outcome": "blocked",
                "warning": "Plan blocked by Maps hard limit — see plan details.",
                "plan": plan_obj,
            }
        )

    plan_path = ""
    if isinstance(plan_obj, dict):
        plan_path = str(plan_obj.get("plan_path") or plan_obj.get("path") or "")

    return run_leads(
        plan_path=plan_path,
        background=background,
        client_tag=client_tag,
    )


@mcp.tool(
    annotations=_ann(
        'Outcome status / inventory',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def outcome_status(
    scope: str = "operators",
    client_tag: str = "",
    project_id: str = "",
    schema: str = "",
    table: str = "",
) -> str:
    """PRIMARY: Honest inventory — useful domains/names, not just resolved flags.

    scope='operators' (default) reports owner-lane truth: with_domain,
    building_name_no_web, pending_for_serp, cost to finish, project_id.
    Use whenever the user asks "where are we" or something looks stuck.
    """
    _ensure_repo_cwd()
    from gmscraper import outcomes as oc
    from mcp_server.errors import tool_error_from_exception

    try:
        return _json(
            oc.status(
                scope=scope,
                client_tag=client_tag,
                project_id=project_id or _default_leads_project_id(table or "operators", ""),
                schema=schema,
                table=table,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _json(tool_error_from_exception(exc))


@mcp.tool(
    annotations=_ann(
        'List vertical categories',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def list_categories() -> str:
    """List built-in industry verticals (hvac, dental, funeral, …) and Maps categories.

    Use when the user asks what niches are supported, or before estimate_cost(vertical=…).
    """
    _ensure_repo_cwd()
    data = _load_verticals()
    out = []
    for name, block in sorted(data.items()):
        cats = (block or {}).get("categories") or []
        out.append(
            {
                "vertical": name,
                "category_count": len(cats),
                "categories": cats,
                "icp": " ".join(str((block or {}).get("icp", "")).split()),
            }
        )
    return _json(out)


@mcp.tool(
    annotations=_ann(
        'Ensure US ZIP list',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def ensure_zips() -> str:
    """Build the offline US ZIP list if missing (free, ~2s)."""
    _ensure_repo_cwd()
    path = _ensure_zips_file()
    from gmscraper import zips

    rows = zips.load(str(path))
    return _json({"path": str(path), "zip_count": len(rows)})


@mcp.tool(
    annotations=_ann(
        'Pipeline database stats',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def pipeline_stats(
    city: str = "",
    state: str = "",
    main_category: str = "",
    plan_path: str = "",
    plan_id: str = "",
    run_id: str = "",
    client_tag: str = "",
    source: str = "",
) -> str:
    """Show what is already in the local SQLite leads database.

    Optional filters (city/state/main_category/plan_path/plan_id/run_id/
    client_tag/source) return a scoped business count so one client's scrape
    is distinguishable from the global cumulative totals.
    """
    _ensure_repo_cwd()
    store = _store()
    out: dict[str, Any] = dict(store.stats())
    scope = _normalize_scope(
        city=city,
        state=state,
        main_category=main_category,
        plan_path=plan_path,
        plan_id=plan_id,
        run_id=run_id,
        client_tag=client_tag,
        source=source,
    )
    if _scope_is_set(scope) or scope.get("plan_path"):
        clauses, args = store._business_scope_clauses(
            city=scope["city"],
            state=scope["state"],
            main_category=scope["main_category"],
            plan_id=scope["plan_id"],
            run_id=scope["run_id"],
            client_tag=scope["client_tag"],
            source=scope["source"],
        )
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        scoped_n = store.conn.execute(
            f"SELECT COUNT(*) FROM businesses b{where}", args
        ).fetchone()[0]
        pending = store.pending_sites(
            city=scope["city"],
            state=scope["state"],
            main_category=scope["main_category"],
            plan_id=scope["plan_id"],
            run_id=scope["run_id"],
            client_tag=scope["client_tag"],
            source=scope["source"],
        )
        out["scope"] = scope
        out["scoped_businesses"] = int(scoped_n)
        out["scoped_pending_sites"] = len(pending)
    return _json(out)


@mcp.tool(
    annotations=_ann(
        'Plan leads from brief',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def plan_leads(
    brief: str,
    zip_limit: int = 0,
    zips: str = "",
    center: str = "",
    radius_miles: float = 0.0,
    exclude_categories: str = "",
) -> str:
    """REQUIRED first step for any new lead request.

    Turns a plain-English brief into categories, geography, ICP, and a cost
    estimate (LLM only — no Google Maps spend yet). Returns plan_path for
    run_leads. No approval gate.

    Geography overrides (precedence: zips > center+radius_miles > states):
      zips              comma-separated 5-digit ZIPs (supports 1000+). Wins outright.
      center            "Dallas, TX" or "32.7767,-97.0000"
      radius_miles      miles from center (haversine over ZIP centroids)
      exclude_categories  comma-separated Maps categories to never scrape

    Show the cost summary, then call run_leads(plan_path=...) or run_leads().
    """
    _ensure_repo_cwd()
    brief = brief.strip()
    if len(brief) < 8:
        raise ValueError("brief is too short — describe niche + region.")

    plan = __import__("gmscraper.brief", fromlist=["make_plan"]).make_plan(_llm(), brief)

    # Parameter overrides beat the LLM when provided.
    if center.strip() and radius_miles > 0:
        plan.center = center.strip()
        plan.radius_miles = float(radius_miles)
    if exclude_categories.strip():
        pass  # applied inside _plan_cost_bundle

    has_geo = bool(zips.strip()) or (plan.center and plan.radius_miles > 0) or bool(plan.states)
    if not has_geo:
        nationwide_warning = (
            "No US state / radius / ZIP list was detected. Nationwide is 20–30x "
            "a single-state run. Ask the user which state(s) or pass zips/center."
        )
    else:
        nationwide_warning = None

    bundle = _plan_cost_bundle(
        plan,
        zip_limit=zip_limit or None,
        zips=zips,
        center=center,
        radius_miles=radius_miles,
        exclude_categories=exclude_categories,
    )
    plan_path = _save_plan(plan, brief)
    record = save_plan_record(
        brief=brief,
        plan_path=str(plan_path),
        requests=bundle["requests"],
        estimated_overage_usd=bundle["estimated_overage_usd"],
        blocked=bundle["blocked"],
        states=plan.states,
        categories=plan.categories,
        vertical=plan.vertical,
    )
    return _json(
        {
            **record.to_public(),
            **bundle,
            "nationwide_warning": nationwide_warning,
            "next_step": (
                f"Call run_leads(plan_path={str(plan_path)!r}) or run_leads()."
            ),
        }
    )


@mcp.tool(
    annotations=_ann(
        'Estimate cost for a vertical',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def estimate_cost(
    vertical: str = "",
    states: str = "",
    categories: str = "",
    zip_limit: int = 0,
    brief: str = "",
    zips: str = "",
    center: str = "",
    radius_miles: float = 0.0,
    exclude_categories: str = "",
) -> str:
    """Price a scrape without running it. Use for "how much would this cost?"

    Prefer plan_leads for open-ended briefs. Geography overrides match plan_leads:
    zips > center+radius_miles > states. exclude_categories drops scrape categories.
    """
    _ensure_repo_cwd()
    from gmscraper import brief as brief_mod
    from gmscraper.brief import Plan
    from gmscraper.cli import pick_vertical
    from gmscraper.config import DEFAULT_CATEGORIES, settings

    if brief.strip():
        plan = brief_mod.make_plan(_llm(), brief.strip())
        used_brief = brief.strip()
    else:
        state_list = [s.strip().upper() for s in states.split(",") if s.strip()]
        if categories.strip():
            cats = [c.strip() for c in categories.split(",") if c.strip()]
            icp = ""
            vert = "custom"
        elif vertical.strip():
            icp, cats = pick_vertical(DEFAULT_CATEGORIES, vertical.strip())
            vert = vertical.strip()
        else:
            raise ValueError("Provide vertical, categories, or brief.")
        plan = Plan(
            vertical=vert,
            categories=cats,
            icp=icp,
            states=state_list,
            center=center.strip(),
            radius_miles=float(radius_miles or 0),
        )
        used_brief = brief or f"{vert} in {', '.join(state_list) or center or 'US'}"

    if center.strip() and radius_miles > 0:
        plan.center = center.strip()
        plan.radius_miles = float(radius_miles)

    bundle = _plan_cost_bundle(
        plan,
        zip_limit=zip_limit or None,
        zips=zips,
        center=center,
        radius_miles=radius_miles,
        exclude_categories=exclude_categories,
    )
    plan_path = _save_plan(plan, used_brief)
    record = save_plan_record(
        brief=used_brief,
        plan_path=str(plan_path),
        requests=bundle["requests"],
        estimated_overage_usd=bundle["estimated_overage_usd"],
        blocked=bundle["blocked"],
        states=plan.states,
        categories=plan.categories,
        vertical=plan.vertical,
    )
    return _json({**record.to_public(), **bundle, "maps_plan": settings.maps_plan})


@mcp.tool(
    annotations=_ann(
        'Probe Maps API (1 request)',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def probe_maps(zip_code: str = "10001", category: str = "hvac contractor") -> str:
    """Make one live RapidAPI Maps request and return normalized sample fields.

    Costs a single Maps request. Use before first scrape or after .env changes.
    """
    _ensure_repo_cwd()
    from gmscraper import mapsdata, zips
    from gmscraper.config import settings
    from gmscraper.mapsdata import MapsDataClient

    settings.require_rapidapi()
    _ensure_zips_file()
    rows = zips.load(str(_ensure_zips_file()))
    row = next((r for r in rows if r["zip"] == zip_code), None) or rows[0]
    client = MapsDataClient(settings)
    payload = client.raw_search(category, row)
    items = mapsdata.extract_list(payload)
    sample = items[0] if items else payload
    norm = mapsdata.normalize(items[0], row["zip"], category) if items else None
    if norm:
        norm.pop("raw", None)
    return _json(
        {
            "zip": row["zip"],
            "category": category,
            "result_count": len(items),
            "normalized_sample": norm,
            "raw_sample_keys": list(sample.keys()) if isinstance(sample, dict) else type(sample).__name__,
        }
    )


# ---------------------------------------------------------------------------
# Paid tools — plan_path / latest plan only. No approval gates.
# ---------------------------------------------------------------------------


def _http_mode() -> bool:
    return os.environ.get("MCP_TRANSPORT", "stdio").lower() in (
        "streamable-http",
        "http",
        "sse",
    )


def _job_progress(stage: str, **extra: Any) -> None:
    """Best-effort heartbeat into the current background job (if any)."""
    try:
        from mcp_server.jobs import heartbeat

        heartbeat(stage=stage, **extra)
    except Exception:  # noqa: BLE001
        pass


def _plan_id_from_path(plan_path: str) -> str:
    """Stable plan_id stamped onto business rows (filename stem)."""
    p = (plan_path or "").strip()
    if not p:
        return ""
    return Path(p).stem


def _normalize_scope(
    *,
    city: str = "",
    state: str = "",
    main_category: str = "",
    plan_path: str = "",
    plan_id: str = "",
    run_id: str = "",
    client_tag: str = "",
    source: str = "",
) -> dict[str, str]:
    """Normalize optional row-scope filters for enrich/classify/crawl."""
    pid = (plan_id or "").strip() or _plan_id_from_path(plan_path)
    return {
        "city": (city or "").strip(),
        "state": (state or "").strip(),
        "main_category": (main_category or "").strip(),
        "plan_path": (plan_path or "").strip(),
        "plan_id": pid,
        "run_id": (run_id or "").strip(),
        "client_tag": (client_tag or "").strip(),
        "source": (source or "").strip(),
    }


def _scope_is_set(scope: dict[str, str]) -> bool:
    return any(
        scope.get(k)
        for k in (
            "city",
            "state",
            "main_category",
            "plan_id",
            "run_id",
            "client_tag",
            "source",
        )
    )


def _default_leads_project_id(table: str = "", project_id: str = "") -> str:
    """Operators / parcel tables live on the LEADS Supabase project."""
    if (project_id or "").strip():
        return project_id.strip()
    t = (table or "").strip().lower()
    if t in ("operators", "parcels", "contractors", "permits"):
        return (
            os.environ.get("LEADS_SUPABASE_PROJECT_ID", "").strip()
            or "kemvxzhcxvynmoutwdrh"
        )
    return ""


def _default_source_schema(table: str = "", schema: str = "") -> str:
    """Parcel/operator tables ship in permit_parcel, not public."""
    if (schema or "").strip():
        return schema.strip()
    t = (table or "").strip().lower()
    if t in ("operators", "parcels"):
        return "permit_parcel"
    return "public"


def _started_response(job: Any, *, attached: bool = False) -> dict[str, Any]:
    """Uniform start/queue/attach payload for background tools."""
    from mcp_server.jobs import live_progress, queue_position

    pos = queue_position(job.id)
    status = "already_running" if attached or (job.progress or {}).get("attached") else (
        "started" if job.status == "running" else "queued"
    )
    if job.status == "queued" and not attached:
        status = "queued"
    msg = {
        "already_running": (
            f"Identical work already active as job_id={job.id}. "
            "Attached — poll get_job_status (does not start a duplicate)."
        ),
        "queued": (
            f"Queued behind other jobs (position {pos}). "
            f"Poll get_job_status with job_id={job.id}."
        ),
        "started": (
            f"Pipeline started in the background. Poll get_job_status "
            f"with job_id={job.id} until completed/failed/stalled/interrupted."
        ),
    }[status]
    return {
        "status": status,
        "job_id": job.id,
        "queue_position": pos,
        "queue_key": (job.meta or {}).get("queue_key"),
        "live": live_progress(job),
        "message": msg,
    }


def _execute_run_leads(
    plan_path: str,
    out_path: str,
    include_owner_fallback: bool,
    workers: int,
    client_tag: str = "",
) -> dict[str, Any]:
    from gmscraper import brief as brief_mod
    from gmscraper import classify, enrich_site, export, owner, scrape
    from gmscraper import clients as client_reg
    from gmscraper.config import settings
    from gmscraper.llm import default_workers
    from gmscraper.mapsdata import MapsDataClient
    from gmscraper.websearch import make_backend

    record = resolve_plan(plan_path=plan_path)
    settings.require_rapidapi()
    plan = brief_mod.load(record.plan_path)
    _ensure_zips_file()
    zip_rows = _zip_rows_for_plan(plan)
    store = _store()
    llm = _llm()
    client = MapsDataClient(settings)

    stamp = record.id if record.id != "plan_path" else "run"
    out = Path(out_path) if out_path else ROOT / "data" / "outputs" / f"{plan.vertical}-{stamp}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Maps scrape resumes automatically: SQLite jobs table skips status='done'.
    cats = list(plan.categories)
    zips = [r["zip"] for r in zip_rows]
    grid0 = store.grid_stats(categories=cats, zips=zips)
    _job_progress(
        "scrape",
        zip_count=len(zip_rows),
        categories=len(cats),
        categories_list=cats,
        zips_list=zips[:50],  # sample for status; full scope via meta
        jobs_total=grid0["jobs_total"] or len(zips) * len(cats),
        jobs_done=grid0["jobs_done"],
        jobs_pending=grid0["jobs_pending"] or len(zips) * len(cats),
        businesses_found=0,
        jobs_done_at_start=grid0["jobs_done"],
    )

    from mcp_server.jobs import current_job_id, get_job

    started_at = None
    try:
        jid = current_job_id()
        if jid:
            started_at = get_job(jid).started_at
    except Exception:  # noqa: BLE001
        started_at = None

    def _on_scrape_progress(p: dict[str, Any]) -> None:
        grid = store.grid_stats(categories=cats, zips=zips)
        bf = store.businesses_found_since(started_at)
        _job_progress(
            **p,
            jobs_total=grid["jobs_total"],
            jobs_done=grid["jobs_done"],
            jobs_pending=grid["jobs_pending"],
            businesses_found=bf,
        )

    tag_plan = _plan_id_from_path(record.plan_path)
    tag_run = current_job_id() or ""
    if (client_tag or "").strip():
        resolved = client_reg.resolve_client(client_tag)
        tag_client = resolved.slug if resolved else client_reg.normalize_slug(client_tag)
    else:
        # Prefer a known client alias inside the vertical string; else leave blank
        # so rows are not mis-tagged as a bogus client.
        tag_client = ""
        for cand in (plan.vertical or "", getattr(plan, "icp", "") or ""):
            try:
                hit = client_reg.resolve_client(cand.split()[0], required=False)
                if hit:
                    tag_client = hit.slug
                    break
            except Exception:  # noqa: BLE001
                continue
    scrape_res = scrape.run(
        store,
        client,
        zip_rows,
        plan.categories,
        workers=workers,
        price_per_request=settings.price_per_request,
        on_progress=_on_scrape_progress,
        heartbeat_every=10,
        plan_id=tag_plan,
        run_id=tag_run,
        client_tag=tag_client,
    )

    _job_progress("enrich")
    store.queue_sites()
    pending = store.pending_sites()
    _job_progress("enrich", done=0, total=len(pending))
    # Large site-fetch runs are GIL-heavy (html2text) and wedge the MCP HTTP
    # loop if done in-process. Defer to enrich_sites so heartbeats/status stay live.
    defer_threshold = 500
    if len(pending) > defer_threshold:
        _job_progress(
            "enrich_deferred",
            done=0,
            total=len(pending),
            deferred=True,
            reason=f"pending_sites>{defer_threshold}",
        )
        return {
            "plan_path": record.plan_path,
            "scrape": scrape_res,
            "enrich": "deferred",
            "pending_sites": len(pending),
            "csv": None,
            "rows": 0,
            "message": (
                f"Maps scrape finished. {len(pending):,} domains still need "
                "enrich_sites — call that next (then classify_leads / "
                "find_owners / export_csv). Large enrich is deferred so the "
                "MCP stays responsive."
            ),
        }

    enrich_site.run(
        store,
        pending,
        workers=max(1, min(int(workers or 8), 3)),
        on_progress=lambda **p: _job_progress("enrich", **p),
    )

    _job_progress("classify")
    classify.run(
        store,
        llm,
        plan.icp,
        workers=default_workers(llm),
    )
    if plan.require_owner:
        _job_progress("owners")
        backend = make_backend(settings, "") if include_owner_fallback else None
        owner.run(store, llm, backend, workers=default_workers(llm))

    _job_progress("export")
    n = export.run(
        store,
        str(out),
        icp_only=True,
        with_owner=plan.require_owner,
        with_phone=plan.require_phone,
        with_website=plan.require_website,
        with_email=plan.require_email,
        min_rating=plan.min_rating,
        min_reviews=plan.min_reviews,
        states=plan.states or None,
        center=plan.center or None,
        radius_miles=plan.radius_miles or None,
        center_lat=plan.center_lat,
        center_lng=plan.center_lng,
    )
    return {
        "status": "completed",
        "leads": n,
        "csv": str(out),
        "plan_path": record.plan_path,
        "scrape": scrape_res,
        "stats": store.stats(),
        "llm_spend": llm.spend_line() if hasattr(llm, "spend_line") else None,
        "resume_note": (
            "Maps scrape is checkpointed per ZIP×category; re-running the same "
            "plan skips finished pairs."
        ),
    }


@mcp.tool(
    annotations=_ann(
        'Run full lead pipeline',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def run_leads(
    plan_path: str = "",
    out_path: str = "",
    include_owner_fallback: bool = False,
    workers: int = 8,
    background: bool = True,
    client_tag: str = "",
) -> str:
    """Execute the full lead pipeline after plan_leads. This is the main "go" tool.

    Pass plan_path from plan_leads, or omit it to use the latest saved plan.
    Pass client_tag ('peterson' / 'basco') so scraped rows are stamped for
    that client's Supabase tables (peterson_* / basco_*).

    On Railway/HTTP this starts a background job — poll get_job_status with the
    returned job_id until completed/failed/stalled/interrupted. Maps scrape
    resumes from unfinished ZIP×category pairs if the worker dies mid-run;
    re-call run_leads with the same plan_path to continue.
    """
    _ensure_repo_cwd()
    resolve_plan(plan_path=plan_path)

    run_bg = background if background is not None else _http_mode()
    if run_bg and _http_mode():
        from gmscraper import brief as brief_mod
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        record = resolve_plan(plan_path=plan_path)
        plan = brief_mod.load(record.plan_path)
        meta = {
            "plan_path": record.plan_path,
            "resumable_scrape": True,
            "categories": list(plan.categories),
            "client_tag": client_tag or None,
        }
        before = find_active_by_queue_key(make_queue_key("run_leads", meta))
        job = start_job(
            "run_leads",
            lambda: _execute_run_leads(
                plan_path, out_path, include_owner_fallback, workers, client_tag
            ),
            meta=meta,
        )
        attached = before is not None and before.id == job.id
        out = _started_response(job, attached=attached)
        out["resume_note"] = (
            "If stalled/interrupted, re-call run_leads with the same "
            "plan_path — Maps scrape resumes from unfinished ZIP×category pairs. "
            "Identical plan_path while a job is live attaches (no duplicate)."
        )
        return _json(out)

    return _json(
        _execute_run_leads(
            plan_path, out_path, include_owner_fallback, workers, client_tag
        )
    )


def _execute_scrape_maps(
    plan_path: str,
    workers: int,
    max_jobs: int,
    client_tag: str = "",
) -> dict[str, Any]:
    from gmscraper import brief as brief_mod
    from gmscraper import clients as client_reg
    from gmscraper import scrape
    from gmscraper.config import settings
    from gmscraper.mapsdata import MapsDataClient
    from mcp_server.jobs import current_job_id

    record = resolve_plan(plan_path=plan_path)
    settings.require_rapidapi()
    plan = brief_mod.load(record.plan_path)
    zip_rows = _zip_rows_for_plan(plan)
    store = _store()
    client = MapsDataClient(settings)
    tag_plan = _plan_id_from_path(record.plan_path)
    tag_run = current_job_id() or ""
    if (client_tag or "").strip():
        resolved = client_reg.resolve_client(client_tag)
        tag_client = resolved.slug if resolved else client_reg.normalize_slug(client_tag)
    else:
        tag_client = ""
    res = scrape.run(
        store,
        client,
        zip_rows,
        plan.categories,
        workers=workers,
        price_per_request=settings.price_per_request,
        max_jobs=max_jobs or None,
        on_progress=lambda p: _job_progress(**p),
        plan_id=tag_plan,
        run_id=tag_run,
        client_tag=tag_client,
    )
    return {
        "status": "completed",
        "result": res,
        "stats": store.stats(),
        "zip_count": len(zip_rows),
        "sample_source_zips": [r["zip"] for r in zip_rows[:20]],
        "plan_path": record.plan_path,
        "plan_id": tag_plan,
        "run_id": tag_run,
        "client_tag": tag_client or None,
        "resume_note": (
            "Re-run scrape_maps with the same plan to continue unfinished pairs."
        ),
    }


@mcp.tool(
    annotations=_ann(
        'Scrape Google Maps only',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def scrape_maps(
    plan_path: str = "",
    workers: int = 8,
    max_jobs: int = 0,
    background: bool = True,
    client_tag: str = "",
) -> str:
    """Paid Maps scrape stage only. Pass plan_path or omit for latest plan.

    Pass client_tag ('peterson' / 'basco') to stamp rows for that client.
    """
    _ensure_repo_cwd()
    resolve_plan(plan_path=plan_path)

    if background and _http_mode():
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        record = resolve_plan(plan_path=plan_path)
        meta = {"plan_path": record.plan_path, "client_tag": client_tag or None}
        before = find_active_by_queue_key(make_queue_key("scrape_maps", meta))
        job = start_job(
            "scrape_maps",
            lambda: _execute_scrape_maps(plan_path, workers, max_jobs, client_tag),
            meta=meta,
        )
        return _json(
            _started_response(job, attached=before is not None and before.id == job.id)
        )

    return _json(_execute_scrape_maps(plan_path, workers, max_jobs, client_tag))


@mcp.tool(
    annotations=_ann(
        'Get background job status',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def get_job_status(job_id: str) -> str:
    """Poll a background job. Use after run_leads / pipeline_run / enrich_* return job_id.

    status may be queued|running|completed|failed|stalled|interrupted|cancelled.
    Always includes `live` progress: jobs_total, jobs_done, jobs_pending,
    businesses_found, percent_complete, updated_at, eta_seconds, queue_position.
    stalled = no heartbeat for ~5 minutes. interrupted = orphaned on process boot.
    cancelled = removed via cancel_job (never auto-resumed).
    Same plan_path / queue_key re-calls attach instead of duplicating.
    """
    from mcp_server.jobs import STALL_SECONDS, get_job, live_progress, queue_position

    job = get_job(job_id)
    public = job.to_public()
    store = None
    try:
        if job.kind in (
            "run_leads",
            "scrape_maps",
            "enrich_sites",
            "classify_leads",
            "resolve_places",
        ):
            store = _store()
    except Exception:  # noqa: BLE001
        store = None
    live = live_progress(job, store=store)
    public["live"] = live
    # Flatten the key counters at top level for easy Claude polling.
    for key in (
        "jobs_total",
        "jobs_done",
        "jobs_pending",
        "businesses_found",
        "percent_complete",
        "updated_at",
        "eta_seconds",
        "queue_position",
        "stage",
    ):
        public[key] = live.get(key)
    public["queue_position"] = queue_position(job.id)
    if job.status in ("stalled", "interrupted"):
        public["next_step"] = (
            "Re-call the same tool with the same plan_path / table args. "
            "Identical queue_key attaches to a live job; finished ZIP×category "
            "pairs are skipped automatically for Maps scrapes."
        )
    elif job.status == "running":
        public["liveness"] = {
            "heartbeat_at": job.heartbeat_at,
            "stall_after_seconds": STALL_SECONDS,
            "progress": job.progress,
            "live": live,
        }
    elif job.status == "queued":
        public["next_step"] = (
            f"Waiting in queue (position {public['queue_position']}). "
            "Do not start a duplicate — keep polling this job_id. "
            "Call cancel_job to remove it from the queue."
        )
    elif job.status == "cancelled":
        public["next_step"] = "Job cancelled. It will not auto-resume on restart."
    return _json(public)


@mcp.tool(
    annotations=_ann(
        'List background jobs',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def list_background_jobs(limit: int = 20) -> str:
    """List recent background pipeline jobs on this MCP server."""
    from mcp_server.jobs import list_jobs, live_progress, queue_position

    out = []
    for j in list_jobs(limit=limit):
        row = j.to_public()
        row["queue_position"] = queue_position(j.id)
        row["live"] = live_progress(j)
        out.append(row)
    return _json(out)


@mcp.tool(
    annotations=_ann(
        'List job queue',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def list_job_queue() -> str:
    """Show the serial background queue: running job + waiting jobs in order.

    Multiple Claude chats can enqueue work safely. Same queue_key attaches to
    the existing job instead of duplicating. Nothing is killed when a new job
    is queued — it waits its turn.
    """
    from mcp_server.jobs import list_jobs, live_progress, queue_position

    jobs = list_jobs(limit=50)
    running = [j for j in jobs if j.status == "running"]
    queued = sorted(
        [j for j in jobs if j.status == "queued"],
        key=lambda j: queue_position(j.id) or 9999,
    )

    def _row(j: Any) -> dict[str, Any]:
        return {
            "job_id": j.id,
            "kind": j.kind,
            "status": j.status,
            "queue_position": queue_position(j.id),
            "queue_key": (j.meta or {}).get("queue_key"),
            "live": live_progress(j),
            "meta": j.meta,
        }

    return _json(
        {
            "running": [_row(j) for j in running],
            "queued": [_row(j) for j in queued],
            "note": (
                "Re-calling a tool with the same plan_path / table / rows "
                "attaches to the active job_id — it does not start a duplicate "
                "or kill the current run. Use cancel_job(job_id) to remove a "
                "queued job before it starts."
            ),
        }
    )


@mcp.tool(
    annotations=_ann(
        'Cancel background job',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def cancel_job(job_id: str, reason: str = "") -> str:
    """Cancel a queued or running background job by job_id.

    Queued jobs are removed immediately and never start (use this to kill an
    Apify crawl still waiting behind enrich). Running jobs are flagged; the
    worker exits as cancelled when able. Cancelled jobs are never auto-resumed
    on container restart. Does not call Apify abort — if an actor runId already
    exists, abort that separately.
    """
    from mcp_server.jobs import cancel_job as _cancel

    return _json(_cancel(job_id, reason=reason or ""))


def _enrich_sites_via_subprocess(limit: int, workers: int) -> dict[str, Any]:
    """Run site enrich in a child process so html2text cannot wedge uvicorn.

    The parent job thread blocks in wait() (GIL released); the heartbeat ticker
    and HTTP event loop keep running in this process.
    """
    import subprocess
    import sys
    import time

    store = _store()
    store.queue_sites()
    domains = store.pending_sites(limit=limit or None)
    total = len(domains)
    _job_progress("enrich", done=0, total=total, via="subprocess")
    if total == 0:
        return {"result": {"ok": 0, "error": 0, "skipped": 0}, "stats": store.stats(), "domains": 0}

    w = max(1, min(int(workers or 3), 3))
    cmd = [sys.executable, "-m", "gmscraper", "enrich", "--workers", str(w)]
    if limit:
        cmd.extend(["--limit", str(int(limit))])
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"enrich-{int(time.time())}.log"
    with log_path.open("w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
        while proc.poll() is None:
            _job_progress("enrich", total=total, via="subprocess", pid=proc.pid)
            time.sleep(15)
        code = proc.wait()
    if code != 0:
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8")[-2000:]
        except OSError:
            pass
        raise RuntimeError(f"enrich subprocess exited {code}: {tail}")
    return {
        "result": {"exit_code": code, "log": str(log_path)},
        "stats": store.stats(),
        "domains": total,
        "workers": w,
    }


@mcp.tool(
    annotations=_ann(
        'Enrich business websites',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def enrich_sites(
    limit: int = 0,
    workers: int = 3,
    background: bool = True,
    city: str = "",
    state: str = "",
    main_category: str = "",
    plan_path: str = "",
    plan_id: str = "",
    run_id: str = "",
    client_tag: str = "",
    source: str = "",
) -> str:
    """Fetch website text/emails for pending domains (free, no Maps spend).

    Shallow same-domain crawl: homepage + up to 3 about/team pages
    (/about, /team, /leadership, …). Per-page text is stored with page_type.

    Scope with city/state/main_category/plan_path/plan_id/run_id/client_tag/
    source so one client's rows can be enriched without draining another
    client's global backlog. Scoped runs jump the queue (priority > backlog).

    On Railway/HTTP this defaults to a background job — poll get_job_status.
    Unscoped work runs in a subprocess so the MCP HTTP loop stays responsive.
    """
    _ensure_repo_cwd()
    scope = _normalize_scope(
        city=city,
        state=state,
        main_category=main_category,
        plan_path=plan_path,
        plan_id=plan_id,
        run_id=run_id,
        client_tag=client_tag,
        source=source,
    )
    scoped = _scope_is_set(scope)

    def _run() -> dict[str, Any]:
        # Subprocess CLI has no scope filters — run scoped work in-process.
        if _http_mode() and not scoped:
            return _enrich_sites_via_subprocess(limit, workers)
        from gmscraper import enrich_site

        store = _store()
        store.queue_sites()
        domains = store.pending_sites(
            limit=limit or None,
            city=scope["city"],
            state=scope["state"],
            main_category=scope["main_category"],
            plan_id=scope["plan_id"],
            run_id=scope["run_id"],
            client_tag=scope["client_tag"],
            source=scope["source"],
        )
        _job_progress("enrich", done=0, total=len(domains), **{
            k: v for k, v in scope.items() if v
        })
        res = enrich_site.run(
            store,
            domains,
            workers=max(1, min(int(workers or 3), 3)),
            on_progress=lambda **p: _job_progress("enrich", **p),
        )
        return {
            "result": res,
            "stats": store.stats(),
            "domains": len(domains),
            "scope": scope,
        }

    if _http_mode() and background:
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        meta = {
            "limit": limit,
            "workers": max(1, min(int(workers or 3), 3)),
            **scope,
        }
        before = find_active_by_queue_key(make_queue_key("enrich_sites", meta))
        job = start_job(
            "enrich_sites",
            _run,
            meta=meta,
            priority=10 if scoped else 5,
        )
        return _json(
            _started_response(job, attached=before is not None and before.id == job.id)
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Crawl team/about pages',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def crawl_team_pages(
    limit: int = 0,
    workers: int = 12,
    force: bool = False,
    background: bool = True,
    city: str = "",
    state: str = "",
    main_category: str = "",
    plan_path: str = "",
    plan_id: str = "",
    run_id: str = "",
    client_tag: str = "",
    source: str = "",
) -> str:
    """Re-crawl about/team pages for domains already fetched (free).

    Use after a large enrich_sites run (e.g. ~8k sites) so team-page text is
    tagged by page_type. force=true re-crawls even if team pages exist.
    Scope with city/state/main_category/plan_path/plan_id/run_id/client_tag/
    source for multi-client isolation.
    On HTTP transport defaults to a background job — poll get_job_status.
    """
    _ensure_repo_cwd()
    from gmscraper import enrich_site

    store = _store()
    scope = _normalize_scope(
        city=city,
        state=state,
        main_category=main_category,
        plan_path=plan_path,
        plan_id=plan_id,
        run_id=run_id,
        client_tag=client_tag,
        source=source,
    )
    scoped = _scope_is_set(scope)

    def _run() -> dict[str, Any]:
        domains = (
            store.domains_with_ok_sites(
                limit=limit or None,
                city=scope["city"],
                state=scope["state"],
                main_category=scope["main_category"],
                plan_id=scope["plan_id"],
                run_id=scope["run_id"],
                client_tag=scope["client_tag"],
                source=scope["source"],
            )
            if force
            else store.domains_needing_team_crawl(
                limit=limit or None,
                city=scope["city"],
                state=scope["state"],
                main_category=scope["main_category"],
                plan_id=scope["plan_id"],
                run_id=scope["run_id"],
                client_tag=scope["client_tag"],
                source=scope["source"],
            )
        )
        res = enrich_site.crawl_team_pages(
            store,
            domains=domains,
            workers=workers,
            force=force,
        )
        return {"result": res, "stats": store.stats(), "scope": scope, "domains": len(domains)}

    if background and _http_mode():
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        meta = {"limit": limit, "force": force, **scope}
        before = find_active_by_queue_key(make_queue_key("crawl_team_pages", meta))
        job = start_job(
            "crawl_team_pages",
            _run,
            meta=meta,
            priority=10 if scoped else 5,
        )
        return _json(
            _started_response(job, attached=before is not None and before.id == job.id)
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Extract team-page contacts',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def extract_team_contacts(
    limit: int = 0,
    workers: int = 8,
    icp_only: bool = False,
    use_llm: bool = True,
    target_titles: str = "",
    background: bool = True,
    city: str = "",
    state: str = "",
    main_category: str = "",
    plan_path: str = "",
    plan_id: str = "",
    run_id: str = "",
    client_tag: str = "",
    source: str = "",
) -> str:
    """Parse person+title pairs from team/about page text into contacts.

    Writes to local contacts. Also fills empty owners.
    Defaults to use_llm=true — heuristic path invents title/company "names".
    target_titles = comma-separated roles to prefer (vertical-agnostic default
    when empty: owner, founder, president, principal, partner, chief, …).

    Scope with city/state/main_category/plan_path/plan_id/run_id/client_tag/
    source so a Lane-2 client pull does not LLM-extract the whole DB.
    """
    _ensure_repo_cwd()
    from gmscraper import team_contacts

    scope = _normalize_scope(
        city=city,
        state=state,
        main_category=main_category,
        plan_path=plan_path,
        plan_id=plan_id,
        run_id=run_id,
        client_tag=client_tag,
        source=source,
    )
    scoped = _scope_is_set(scope)
    titles = [t.strip() for t in (target_titles or "").split(",") if t.strip()]

    def _run() -> dict[str, Any]:
        store = _store()
        llm = _llm() if use_llm else None
        domains = None
        if scoped:
            # Prefer domains that already have team/about pages crawled.
            domains = store.domains_with_ok_sites(
                limit=limit or None,
                city=scope["city"],
                state=scope["state"],
                main_category=scope["main_category"],
                plan_id=scope["plan_id"],
                run_id=scope["run_id"],
                client_tag=scope["client_tag"],
                source=scope["source"],
            )
        res = team_contacts.run(
            store,
            limit=limit or None,
            workers=workers,
            icp_only=icp_only,
            use_llm=use_llm,
            llm=llm,
            domains=domains,
            target_titles=titles or None,
        )
        out: dict[str, Any] = {
            "result": res,
            "stats": store.stats(),
            "scope": scope,
            "domains": len(domains) if domains is not None else res.get("domains"),
        }
        if llm:
            out["llm_spend"] = llm.spend_line()
        return out

    if background and _http_mode():
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        meta = {
            "limit": limit,
            "icp_only": icp_only,
            "use_llm": use_llm,
            "target_titles": titles or None,
            **scope,
        }
        before = find_active_by_queue_key(make_queue_key("extract_team_contacts", meta))
        job = start_job(
            "extract_team_contacts",
            _run,
            meta=meta,
            priority=10 if scoped else 5,
        )
        return _json(
            _started_response(
                job, attached=before is not None and before.id == job.id
            )
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Ingest external leads',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def ingest_external_leads(
    rows: str,
    source_tag: str = "shovels",
    dedupe_on: str = "domain",
) -> str:
    """Insert external lead rows (e.g. Shovels CSV/JSON) into the local DB.

    `rows` is a JSON list of objects. Shovels mapping:
      business_name→name, website→domain, primary_email/email→emails,
      address_city/state→city/state, primary_phone→phone, name→owner_name,
      permit_count + id kept on the row. source is set from source_tag.

    Email cells may be comma-separated; primary email prefers an address whose
    domain matches website (typo hosts like yhaoo.com / gmail.comp lose).
    Returns counts only — never echoes rows.
    """
    _ensure_repo_cwd()
    from gmscraper import ingest

    result = ingest.run(
        _store(),
        rows,
        source_tag=source_tag or "shovels",
        dedupe_on=dedupe_on or "domain",
    )
    result["stats"] = _store().stats()
    return _json(result)


@mcp.tool(
    annotations=_ann(
        'Estimate resolve_places cost',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def estimate_resolve_places(
    schema: str,
    table: str,
    key_column: str,
    address_column: str = "",
    name_column: str = "",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    limit: int = 0,
    project_id: str = "",
    details_only: bool = False,
) -> str:
    """Read-only Maps cost estimate for resolve_places. No spend, no schema writes.

    details_only=True estimates 1 request/row for place_id-without-website backfill.
    Prefer this over resolve_places(estimate_only=true). No approval required.
    """
    _ensure_repo_cwd()
    from gmscraper import resolve_places as rp

    return _json(
        rp.run(
            schema=schema,
            table=table,
            key_column=key_column,
            address_column=address_column,
            name_column=name_column,
            city_column=city_column,
            where=where,
            order_by=order_by,
            limit=int(limit or 0),
            estimate_only=True,
            details_only=bool(details_only),
            project_id=project_id or "",
        )
    )


@mcp.tool(
    annotations=_ann(
        'Resolve places (address/name → business)',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def resolve_places(
    schema: str,
    table: str,
    key_column: str,
    address_column: str = "",
    name_column: str = "",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    limit: int = 0,
    strategy: str = "address",
    min_confidence: float = 0.6,
    workers: int = 8,
    project_id: str = "",
    estimate_only: bool = False,
    details_only: bool = False,
    background: bool = True,
) -> str:
    """[Advanced] Prefer PRIMARY tool `resolve_addresses` (method=auto|maps).

    Turn source-table rows into business identities via Maps.
    Generic source binding: pass schema/table/columns — no hardcoded vertical.
    For a cost check prefer estimate_resolve_places (read-only). Counts only.
    """
    _ensure_repo_cwd()
    from gmscraper import resolve_places as rp
    from mcp_server.errors import tool_error_from_exception

    resolved_project = _default_leads_project_id(table, project_id)
    resolved_schema = _default_source_schema(table, schema)

    def _run() -> dict[str, Any]:
        try:
            return rp.run(
                schema=resolved_schema,
                table=table,
                key_column=key_column,
                address_column=address_column,
                name_column=name_column,
                city_column=city_column,
                where=where,
                order_by=order_by,
                limit=int(limit or 0),
                strategy=strategy or "address",
                min_confidence=float(min_confidence or 0.6),
                workers=int(workers or 8),
                estimate_only=bool(estimate_only),
                details_only=bool(details_only),
                project_id=resolved_project,
                on_progress=lambda **p: _job_progress(**p),
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error_from_exception(exc)

    if background and _http_mode() and not estimate_only:
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        meta = {
            "schema": resolved_schema,
            "table": table,
            "key_column": key_column,
            "address_column": address_column,
            "name_column": name_column,
            "city_column": city_column,
            "where": where,
            "order_by": order_by,
            "limit": limit,
            "strategy": strategy,
            "min_confidence": min_confidence,
            "workers": workers,
            "project_id": resolved_project,
            "details_only": bool(details_only),
        }
        before = find_active_by_queue_key(make_queue_key("resolve_places", meta))
        job = start_job("resolve_places", _run, meta=meta, priority=10)
        return _json(
            _started_response(job, attached=before is not None and before.id == job.id)
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Estimate resolve_via_serp cost',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def estimate_resolve_via_serp(
    schema: str = "permit_parcel",
    table: str = "operators",
    key_column: str = "operator_address",
    address_column: str = "operator_address",
    name_column: str = "operator_name",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    limit: int = 0,
    project_id: str = "",
) -> str:
    """Read-only Apify SERP cost estimate + Lane-3 inventory. No spend.

    Pricing model: $0.001 actor start per batch of ≤100 queries + $0.0045/SERP.
    Returns an inventory truth table (with_domain, building_name_no_web,
    pending_for_serp). Maps resolved=true does NOT mean useful — pending is
    rows missing domain+website that have not been tried via SERP.
    """
    _ensure_repo_cwd()
    from gmscraper import resolve_serp as rs
    from mcp_server.errors import tool_error_from_exception

    resolved_project = _default_leads_project_id(table, project_id)
    resolved_schema = _default_source_schema(table, schema)
    resolved_order = order_by or (
        "portfolio_value DESC NULLS LAST" if table == "operators" else ""
    )
    try:
        return _json(
            rs.run(
                schema=resolved_schema,
                table=table,
                key_column=key_column,
                address_column=address_column,
                name_column=name_column,
                city_column=city_column,
                where=where,
                order_by=resolved_order,
                limit=int(limit or 0),
                project_id=resolved_project,
                estimate_only=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _json(tool_error_from_exception(exc))


@mcp.tool(
    annotations=_ann(
        'Resolve via Google SERP (Apify + OpenAI)',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def resolve_via_serp(
    schema: str = "permit_parcel",
    table: str = "operators",
    key_column: str = "operator_address",
    address_column: str = "operator_address",
    name_column: str = "operator_name",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    limit: int = 0,
    min_confidence: float = 0.35,
    project_id: str = "",
    estimate_only: bool = False,
    batch_size: int = 100,
    background: bool = True,
) -> str:
    """[Advanced] Prefer PRIMARY `resolve_addresses` or `run_owner_lane`.

    Resolve source rows via Google SERP when Maps returns a building pin.

    Eligibility is missing domain+website and not yet tried via SERP — NOT
    resolved=false. Maps often stamps resolved=true while writing the building
    street address as business_name with no website; those rows stay eligible.
    Operators default to portfolio_value DESC so the highest-value mailings run
    first. Batches into apify/google-search-scraper (100/query, $0.0045/SERP +
    $0.001 start), OpenAI-parses organics, writes operator_name/business_name/
    domain/website/phone/confidence. Result includes outcome/useful_rate —
    a run that touches many rows but writes 0 domains reports outcome=no_value.

    Proven on "3102 MAPLE AVE STE 500, DALLAS TX" → Weitzman. Prefer
    estimate_resolve_via_serp first (shows inventory + cost). Counts only.
    """
    _ensure_repo_cwd()
    from gmscraper import resolve_serp as rs
    from mcp_server.errors import tool_error_from_exception

    resolved_project = _default_leads_project_id(table, project_id)
    resolved_schema = _default_source_schema(table, schema)
    resolved_order = order_by or (
        "portfolio_value DESC NULLS LAST" if table == "operators" else ""
    )

    def _run() -> dict[str, Any]:
        try:
            return rs.run(
                schema=resolved_schema,
                table=table,
                key_column=key_column,
                address_column=address_column,
                name_column=name_column,
                city_column=city_column,
                where=where,
                order_by=resolved_order,
                limit=int(limit or 0),
                min_confidence=float(min_confidence or 0.35),
                project_id=resolved_project,
                estimate_only=bool(estimate_only),
                batch_size=int(batch_size or 100),
                on_progress=lambda **p: _job_progress(**p),
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error_from_exception(exc)

    if background and _http_mode() and not estimate_only:
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        meta = {
            "schema": resolved_schema,
            "table": table,
            "key_column": key_column,
            "address_column": address_column,
            "name_column": name_column,
            "city_column": city_column,
            "where": where,
            "order_by": resolved_order,
            "limit": limit,
            "min_confidence": min_confidence,
            "batch_size": int(batch_size or 100),
            "project_id": resolved_project,
        }
        before = find_active_by_queue_key(make_queue_key("resolve_via_serp", meta))
        job = start_job("resolve_via_serp", _run, meta=meta, priority=10)
        return _json(
            _started_response(job, attached=before is not None and before.id == job.id)
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Build operators from parcels (in-state filter)',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def build_operators(
    states: str = "TX",
    dry_run: bool = True,
    min_parcels: int = 1,
    center: str = "",
    radius_miles: float = 0.0,
    project_id: str = "",
    background: bool = True,
) -> str:
    """[Advanced] Prefer PRIMARY `run_owner_lane(rebuild_operators=true, …)`.

    Rebuild permit_parcel.operators from parcels. In-state mailing filter +
    optional center/radius_miles on parcel ZIPs (buildings in market).
    dry_run=true (default): counts + top sample, no truncate.
    """
    _ensure_repo_cwd()
    from gmscraper import operators as ops
    from mcp_server.errors import tool_error_from_exception

    resolved_project = _default_leads_project_id("operators", project_id)

    def _run() -> dict[str, Any]:
        try:
            return ops.build_operators(
                states=states or "TX",
                project_id=resolved_project,
                dry_run=bool(dry_run),
                min_parcels=int(float(min_parcels or 1)),
                center=center or "",
                radius_miles=float(radius_miles or 0),
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error_from_exception(exc)

    if background and _http_mode() and not dry_run:
        from mcp_server.jobs import start_job

        job = start_job(
            "build_operators",
            _run,
            meta={
                "states": states,
                "dry_run": False,
                "min_parcels": min_parcels,
                "project_id": resolved_project,
            },
            priority=10,
        )
        return _json(
            {
                "job_id": job.id,
                "status": job.status,
                "message": f"Poll get_job_status with job_id={job.id}.",
            }
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Pipeline: resolve → enrich → extract → contacts',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def pipeline_run(
    schema: str,
    table: str,
    key_column: str,
    stages: str = "resolve,enrich,extract,contacts",
    address_column: str = "",
    name_column: str = "",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    limit: int = 0,
    max_tier: str = "getleads",
    use_llm: bool = True,
    strategy: str = "address",
    min_confidence: float = 0.6,
    target_titles: str = "",
    project_id: str = "",
    workers: int = 8,
    estimate_only: bool = False,
    background: bool = True,
) -> str:
    """Chain optional stages from a raw list to contactable people.

    stages = comma list of resolve,enrich,extract,contacts.
    max_tier caps the contacts waterfall (default getleads).
    target_titles steers LLM extraction (comma-separated).
    Returns per-stage counts + cumulative cost. Never returns rows.
    """
    _ensure_repo_cwd()
    from gmscraper import pipeline as pipe

    store = _store()

    def _run() -> dict[str, Any]:
        return pipe.run(
            store,
            schema=schema,
            table=table,
            key_column=key_column,
            stages=stages,
            address_column=address_column,
            name_column=name_column,
            city_column=city_column,
            where=where,
            order_by=order_by,
            limit=int(limit or 0),
            max_tier=max_tier or "getleads",
            use_llm=bool(use_llm),
            estimate_only=bool(estimate_only),
            project_id=project_id or "",
            strategy=strategy or "address",
            min_confidence=float(min_confidence or 0.6),
            target_titles=target_titles or "",
            workers=int(workers or 8),
            on_progress=lambda **p: _job_progress(**p),
        )

    if background and _http_mode() and not estimate_only:
        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        meta = {
            "schema": schema,
            "table": table,
            "key_column": key_column,
            "address_column": address_column,
            "name_column": name_column,
            "city_column": city_column,
            "where": where,
            "order_by": order_by,
            "stages": stages,
            "max_tier": max_tier,
            "limit": limit,
            "use_llm": bool(use_llm),
            "strategy": strategy,
            "min_confidence": float(min_confidence or 0.6),
            "target_titles": target_titles or "",
            "project_id": project_id or "",
            "workers": int(workers or 8),
        }
        before = find_active_by_queue_key(make_queue_key("pipeline_run", meta))
        job = start_job("pipeline_run", _run, meta=meta)
        return _json(
            _started_response(job, attached=before is not None and before.id == job.id)
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Estimate domain resolve cost',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def estimate_resolve_domains(source: str = "", limit: int = 0) -> str:
    """Estimate paid Maps cost to find websites for businesses missing a domain.

    Only ~28% of typical Shovels rows have websites; classify needs site text.
    Returns plan_path for resolve_domains. One Maps request per business.
    No approval required.
    """
    _ensure_repo_cwd()
    from gmscraper import resolve_domains

    store = _store()
    est = resolve_domains.estimate(store, source=source, limit=limit)
    plan_path = resolve_domains.save_resolve_plan(est, source, limit)
    record = save_plan_record(
        brief=f"resolve_domains source={source or '*'} limit={limit or 'all'}",
        plan_path=str(plan_path),
        requests=int(est["requests"]),
        estimated_overage_usd=est["estimated_overage_usd"],
        blocked=bool(est["blocked"]),
        states=[],
        categories=["resolve_domains"],
        vertical="resolve_domains",
    )
    public = record.to_public()
    public.update(est)
    public["instruction"] = (
        "Call resolve_domains("
        + (f"source={source!r}, " if source else "")
        + (f"limit={limit}, " if limit else "")
        + "). No approval required."
    )
    return _json(public)


@mcp.tool(
    annotations=_ann(
        'Resolve missing domains via Maps',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def resolve_domains(
    plan_path: str = "",
    source: str = "",
    limit: int = 0,
    force: bool = False,
    workers: int = 4,
) -> str:
    """Paid Maps name+city lookup to fill website/domain on ingested rows.

    Pass source/limit directly, or omit to use the latest estimate plan.
    No approval required. After resolve, call enrich_sites then classify_leads.
    """
    _ensure_repo_cwd()
    from gmscraper import resolve_domains as resolve_mod

    store = _store()
    src = source
    lim = limit
    if plan_path:
        record = resolve_plan(plan_path=plan_path)
        try:
            plan = json.loads(Path(record.plan_path).read_text(encoding="utf-8"))
            src = src or (plan.get("source") or "")
            if not lim:
                lim = int(plan.get("limit") or 0)
        except Exception:
            pass
    elif not source and not limit:
        try:
            record = resolve_plan()
            plan = json.loads(Path(record.plan_path).read_text(encoding="utf-8"))
            src = plan.get("source") or ""
            lim = int(plan.get("limit") or 0)
        except ValueError:
            pass

    res = resolve_mod.run(
        store,
        source=src,
        limit=lim,
        force=force,
        workers=max(1, workers),
    )
    next_src = src or "…"
    return _json(
        {
            "result": res,
            "stats": store.stats(),
            "next": (
                "Call enrich_sites for new domains, then "
                f"classify_leads(source={next_src!r}, icp=…)."
            ),
        }
    )


@mcp.tool(
    annotations=_ann(
        'Classify leads against ICP',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def classify_leads(
    icp: str = "",
    vertical: str = "",
    workers: int = 0,
    source: str = "",
    force: bool = False,
    limit: int = 0,
    include_no_site: bool = False,
    center: str = "",
    radius_miles: float = 0.0,
    center_lat: float = 0.0,
    center_lng: float = 0.0,
    require_geo: bool = True,
    background: bool = True,
    city: str = "",
    state: str = "",
    main_category: str = "",
    plan_path: str = "",
    plan_id: str = "",
    run_id: str = "",
    client_tag: str = "",
) -> str:
    """LLM-classify businesses against an ICP (LLM cost only; not Maps).

    Only businesses with fetched website text are eligible by default. Scope
    with source/city/state/main_category/plan_path/plan_id/run_id/client_tag,
    re-run with force=true, and cap with limit. Foreground batches that hit
    the timeout return has_more=true + remaining instead of a generic error.

    On Railway/HTTP, background=true (default) returns job_id immediately and
    classifies on the worker — use limit=0 to drain all eligible rows.

    Geography is a free deterministic gate applied BEFORE the LLM. Default
    require_geo=true — pass center + radius_miles (or center_lat/center_lng).
    Out-of-radius rows are saved as in_icp=false with reason outside_radius.
    Pass require_geo=false for ICPs with no geographic constraint.
    When nothing is eligible, result.reason explains why.
    """
    _ensure_repo_cwd()
    from mcp_server.errors import tool_error_from_exception

    scope = _normalize_scope(
        city=city,
        state=state,
        main_category=main_category,
        plan_path=plan_path,
        plan_id=plan_id,
        run_id=run_id,
        client_tag=client_tag,
        source=source,
    )

    def _run() -> dict[str, Any]:
        from gmscraper import classify
        from gmscraper.cli import pick_vertical
        from gmscraper.config import DEFAULT_CATEGORIES
        from gmscraper.llm import default_workers
        from mcp_server.jobs import current_job_id, heartbeat as _hb

        text = icp
        if not text and vertical:
            text, _ = pick_vertical(DEFAULT_CATEGORIES, vertical)
        if not text:
            raise ValueError("Provide icp text or a known vertical.")
        # Capture job id on the worker thread — classify pool threads have no TLS.
        jid = current_job_id()

        def _prog(**p: Any) -> None:
            if jid:
                _hb(jid, **p)

        store = _store()
        llm = _llm()
        res = classify.run(
            store,
            llm,
            text,
            workers=workers or default_workers(llm),
            source=scope["source"],
            force=force,
            limit=limit or None,
            include_no_site=include_no_site,
            center=center or "",
            radius_miles=float(radius_miles or 0),
            center_lat=float(center_lat) if center_lat else None,
            center_lng=float(center_lng) if center_lng else None,
            require_geo=bool(require_geo),
            city=scope["city"],
            state=scope["state"],
            main_category=scope["main_category"],
            plan_id=scope["plan_id"],
            run_id=scope["run_id"],
            client_tag=scope["client_tag"],
            on_progress=_prog,
        )
        out: dict[str, Any] = {
            "result": res,
            "stats": store.stats(),
            "llm_spend": llm.spend_line(),
            "scope": scope,
            "has_more": bool(res.get("has_more")),
            "remaining": res.get("remaining"),
            "total_eligible": res.get("total_eligible"),
        }
        if res.get("reason"):
            out["reason"] = res["reason"]
        return out

    try:
        if background and _http_mode():
            from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

            meta = {
                "icp": (icp or vertical or "")[:200],
                "vertical": vertical,
                "force": force,
                "limit": limit,
                "include_no_site": include_no_site,
                "center": center,
                "radius_miles": radius_miles,
                "center_lat": center_lat,
                "center_lng": center_lng,
                "require_geo": require_geo,
                "workers": workers,
                **scope,
            }
            before = find_active_by_queue_key(make_queue_key("classify_leads", meta))
            # Priority 20 + light parallel slot so classify is not starved by
            # long SERP / crawl / owner-lane jobs across container restarts.
            job = start_job("classify_leads", _run, meta=meta, priority=20)
            return _json(
                _started_response(
                    job, attached=before is not None and before.id == job.id
                )
            )
        return _json(_run())
    except Exception as exc:  # noqa: BLE001
        return _json(tool_error_from_exception(exc))


@mcp.tool(
    annotations=_ann(
        'Find owner names',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def find_owners(
    use_paid_fallback: bool = False,
    workers: int = 0,
) -> str:
    """Extract owners + team contacts from site text.

    First pulls person+title pairs from team/about pages into `contacts`
    (source=team_page). Then LLM single-owner extraction. Website-only is free;
    Apify fallback is paid — set use_paid_fallback=true. No approval required.
    """
    _ensure_repo_cwd()
    from gmscraper import owner
    from gmscraper.config import settings
    from gmscraper.llm import default_workers
    from gmscraper.websearch import make_backend

    backend = None
    if use_paid_fallback:
        backend = make_backend(settings, "")

    store = _store()
    llm = _llm()
    res = owner.run(store, llm, backend, workers=workers or default_workers(llm))
    return _json({"result": res, "stats": store.stats(), "llm_spend": llm.spend_line()})


@mcp.tool(
    annotations=_ann(
        'Estimate Apify contact crawl cost',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def estimate_apify_contact_crawl(
    domains: str = "",
    source: str = "",
    limit: int = 0,
    verify_emails: bool = False,
) -> str:
    """Read-only Apify cost estimate. No crawl starts. No approval required.

    Prefer this over apify_contact_crawl(estimate_only=true). Then call
    apify_contact_crawl to run.
    """
    _ensure_repo_cwd()
    from gmscraper import apify_contacts

    return _json(
        apify_contacts.crawl(
            _store(),
            domains=domains or "",
            source=source or "",
            limit=int(limit or 0),
            verify_emails=bool(verify_emails),
            estimate_only=True,
        )
    )


@mcp.tool(
    annotations=_ann(
        'Apify contact crawl',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def apify_contact_crawl(
    domains: str = "",
    source: str = "",
    limit: int = 0,
    max_pages_per_site: int = 3,
    verify_emails: bool = False,
    use_proxy: bool = True,
    estimate_only: bool = False,
    run_label: str = "",
    background: bool = True,
) -> str:
    """Run vdrmota/contact-info-scraper; persist raw items locally.

    Pass domains as comma-separated hosts/URLs, or source='maps_no_owner' /
    'icp_no_owner' to select from local SQLite. Prefer estimate_apify_contact_crawl
    for cost checks. Refuses when estimate exceeds APIFY_MAX_COST_USD.
    Default max_pages_per_site=3. Paid leadsEnrichment/social/email-verify
    add-ons are never enabled. Failures return typed ToolError JSON.
    """
    _ensure_repo_cwd()
    from gmscraper import apify_contacts
    from mcp_server.errors import tool_error_from_exception

    store = _store()

    def _run() -> dict[str, Any]:
        try:
            return apify_contacts.crawl(
                store,
                domains=domains or "",
                source=source or "",
                limit=int(limit or 0),
                max_pages_per_site=int(max_pages_per_site or 3),
                verify_emails=bool(verify_emails),
                use_proxy=bool(use_proxy),
                estimate_only=bool(estimate_only),
                run_label=run_label or "",
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error_from_exception(exc)

    # Long live runs go to background; estimates stay sync.
    if (
        background
        and _http_mode()
        and not estimate_only
        and (domains or source)
    ):
        from mcp_server.jobs import start_job

        job = start_job(
            "apify_contact_crawl",
            _run,
            meta={
                "limit": limit,
                "source": source or None,
                "domains_chars": len(domains or ""),
                "estimate_only": False,
            },
        )
        return _json(
            {
                "job_id": job.id,
                "status": job.status,
                "message": f"Poll get_job_status with job_id={job.id}.",
            }
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Parse Apify contacts via OpenAI',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def parse_contacts_openai(
    run_id: str = "",
    source: str = "",
    limit: int = 0,
    model: str = "gpt-4o-mini",
    workers: int = 8,
    background: bool = True,
) -> str:
    """Extract real people from Apify crawl text with OpenAI; write to gc.*.

    Rejects job titles and company names in name fields. Never invents emails.
    Writes gc.companies / gc.contacts server-side. Response is counts only.
    No approval / spend confirmation required.
    """
    _ensure_repo_cwd()
    from gmscraper import apify_contacts

    store = _store()

    def _run() -> dict[str, Any]:
        return apify_contacts.parse_contacts_openai(
            store,
            run_id=run_id or "",
            source=source or "",
            limit=int(limit or 0),
            model=model or "gpt-4o-mini",
            workers=int(workers or 8),
        )

    if background and _http_mode():
        from mcp_server.jobs import start_job

        job = start_job(
            "parse_contacts_openai",
            _run,
            meta={"run_id": run_id or None, "source": source or None, "limit": limit},
        )
        return _json(
            {
                "job_id": job.id,
                "status": job.status,
                "message": f"Poll get_job_status with job_id={job.id}.",
            }
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'FullEnrich find email',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def fullenrich_find_email(
    first_name: str,
    last_name: str,
    domain: str,
    company_name: str = "",
) -> str:
    """FullEnrich email lookup (last tier). 1 credit on work-email hit, 0 on miss.

    Call only after earlier waterfall tiers miss — or use enrich_waterfall
    with max_tier='fullenrich'. Default waterfall max_tier is leadmagic.
    Requires FULLENRICH_API_KEY.
    """
    _ensure_repo_cwd()
    from gmscraper.vendors.fullenrich import FullEnrichClient

    client = FullEnrichClient()
    if not client.enabled:
        raise ValueError("FULLENRICH_API_KEY is not set on this MCP service.")
    hit = client.find_email(first_name, last_name, domain, company_name or domain)
    return _json(
        {
            "email": hit.email if hit else None,
            "status": hit.status if hit else "not_found",
            "source_tier": "fullenrich",
            "credits_used": client.credits_used,
        }
    )


@mcp.tool(
    annotations=_ann(
        'FullEnrich find email (bulk)',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def fullenrich_find_email_bulk(rows: str) -> str:
    """Bulk FullEnrich email lookup. `rows` = JSON list of
    {first_name, last_name, domain, company_name?}. Max 100.

    Returns counts + per-row email/status only (no raw vendor payloads).
    """
    _ensure_repo_cwd()
    from gmscraper.vendors.fullenrich import FullEnrichClient

    client = FullEnrichClient()
    if not client.enabled:
        raise ValueError("FULLENRICH_API_KEY is not set on this MCP service.")
    try:
        parsed = json.loads(rows) if isinstance(rows, str) else rows
    except json.JSONDecodeError as exc:
        raise ValueError("rows must be a JSON list") from exc
    if not isinstance(parsed, list):
        raise ValueError("rows must be a JSON list")
    hits = client.find_email_bulk(
        [r for r in parsed if isinstance(r, dict)][:100]
    )
    results = [
        {
            "email": h.email if h else None,
            "status": h.status if h else "not_found",
            "source_tier": "fullenrich" if h else None,
        }
        for h in hits
    ]
    return _json(
        {
            "rows": len(results),
            "found": sum(1 for r in results if r["email"]),
            "credits_used": client.credits_used,
            "results": results,
        }
    )


@mcp.tool(
    annotations=_ann(
        "Debug echo (annotation probe)",
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def debug_echo(message: str = "ping") -> str:
    """Return the input string. Same annotations as enrich_waterfall.

    If this tool is refused with 'No approval received' while other tools work,
    the gate is annotations/registration — not the enrich_waterfall body.
    If this succeeds, call enrich_waterfall / classify_leads next.
    """
    return _json(
        {
            "ok": True,
            "echo": message,
            "server_version": mcp.version,
            "note": "Reached MCP tool body — client approval gate did not block.",
        }
    )


@mcp.tool(
    annotations=_ann(
        'Enrich waterfall → Supabase gc.*',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def enrich_waterfall(
    rows: str,
    need: str = "both",
    max_tier: str = "leadmagic",
    run_apify: bool = True,
    background: bool = True,
    client_tag: str = "",
    target_titles: str = "",
    require_title_match: bool = True,
) -> str:
    """Walk site/team crawl → AI Ark → getleads → LeadMagic → FullEnrich.

    Pass client_tag ('peterson' / 'basco') so contacts write to
    {slug}_contacts / {slug}_companies. Omitting client_tag falls back to
    the legacy shared gc.* schema (discouraged for new runs).

    `rows` = JSON list of {domain, first_name?, last_name?, company_name?, ...}.
    need = 'email' | 'dm' | 'both'.
    max_tier = 'apify' | 'aiark' | 'getleads' | 'leadmagic' | 'fullenrich'
    (default 'leadmagic' — FullEnrich never runs unless explicitly requested).

    target_titles = comma-separated roles to rank/reject against for need=dm.
    For client_tag=basco defaults to Service Director → Fixed Ops → Service
    Manager → Warranty Manager → GM / Dealer Principal. Without titles,
    loose DM hints still reject non-DM staff (porter, clerk, …).
    require_title_match=false accepts the first usable person regardless of title.
    Response is counts only.
    """
    _ensure_repo_cwd()
    from gmscraper import waterfall as wf

    need_norm = (need or "both").strip().lower()
    if need_norm not in ("email", "dm", "both"):
        raise ValueError("need must be 'email', 'dm', or 'both'")
    max_tier_n = wf.normalize_max_tier(max_tier)

    store = _store()

    def _run() -> dict[str, Any]:
        from mcp_server.errors import tool_error_from_exception

        try:
            return wf.enrich_waterfall(
                rows,
                need=need_norm,  # type: ignore[arg-type]
                store=store,
                write_supabase=True,
                max_tier=max_tier_n,
                run_apify=bool(run_apify),
                on_progress=lambda **p: _job_progress("enrich_waterfall", **p),
                client_tag=client_tag,
                target_titles=target_titles or None,
                require_title_match=bool(require_title_match),
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error_from_exception(exc)

    if background and _http_mode() and len(rows or "") > 2000:
        import hashlib

        from mcp_server.jobs import find_active_by_queue_key, make_queue_key, start_job

        meta = {
            "need": need_norm,
            "max_tier": max_tier_n,
            "client_tag": client_tag or None,
            "target_titles": (target_titles or "")[:200] or None,
            "rows_chars": len(rows or ""),
            "rows_fingerprint": hashlib.sha1((rows or "").encode()).hexdigest()[:16],
        }
        before = find_active_by_queue_key(make_queue_key("enrich_waterfall", meta))
        job = start_job("enrich_waterfall", _run, meta=meta)
        return _json(
            _started_response(job, attached=before is not None and before.id == job.id)
        )
    return _json(_run())


@mcp.tool(
    annotations=_ann(
        'Export leads CSV',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def export_csv(
    out_path: str = "",
    with_email: bool = True,
    with_owner: bool = False,
    min_rating: float = 0.0,
    min_reviews: int = 0,
    states: str = "",
    city: str = "",
    state: str = "",
    q: str = "",
    icp_only: bool = True,
    center: str = "",
    radius_miles: float = 0.0,
    include_reason: bool = False,
    clean: bool = True,
    client_tag: str = "",
    plan_id: str = "",
    run_id: str = "",
) -> str:
    """Return matching leads as CSV text in the response (free).

    Pass client_tag to export one client's rows only (peterson / basco).
    Caps at 5000 rows. clean=true drops placeholder / agency emails.
    """
    _ensure_repo_cwd()
    from gmscraper import export

    state_list = [s.strip().upper() for s in states.split(",") if s.strip()] or None
    # Resolve aliases so export_csv(client_tag='kyle') works.
    tag = ""
    if (client_tag or "").strip():
        from gmscraper import clients as client_reg

        resolved = client_reg.resolve_client(client_tag)
        tag = resolved.slug if resolved else client_tag.strip()
    payload = export.export_payload(
        _store(),
        icp_only=icp_only,
        with_owner=with_owner,
        with_email=with_email,
        min_rating=min_rating,
        min_reviews=min_reviews,
        states=state_list,
        city=city or None,
        state=state or None,
        q=q or None,
        center=center or None,
        radius_miles=radius_miles or None,
        include_reason=include_reason,
        clean=clean,
        out_path=out_path or None,
        client_tag=tag or None,
        plan_id=plan_id or None,
        run_id=run_id or None,
        backfill_cities=True,
    )
    return _json(payload)


@mcp.tool(
    annotations=_ann(
        'Query leads (paginated)',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def query_leads(
    q: str = "",
    city: str = "",
    state: str = "",
    icp_only: bool = False,
    with_email: bool = False,
    with_owner: bool = False,
    min_permits: int = 0,
    page: int = 1,
    page_size: int = 50,
    clean: bool = True,
    include_reason: bool = False,
) -> str:
    """Paginated lead rows for browsing / joins (free).

    Mirrors Property Owners pmf_shovels_contractors_query.
    Returns { total, page, page_size, total_pages, items }.
    page_size max 50. clean=true drops placeholder / agency emails.
    """
    _ensure_repo_cwd()
    from gmscraper import export

    return _json(
        export.query_leads(
            _store(),
            q=q,
            city=city,
            state=state,
            icp_only=icp_only,
            with_email=with_email,
            with_owner=with_owner,
            min_permits=min_permits,
            page=page,
            page_size=page_size,
            clean=clean,
            include_reason=include_reason,
            backfill_cities=True,
        )
    )


@mcp.tool(
    annotations=_ann(
        'Leads summary (counts)',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def leads_summary(
    icp_only: bool = False,
    source: str = "",
    clean: bool = True,
) -> str:
    """Aggregate counts only — no row payloads (free).

    Returns total businesses, in_icp, with_email/phone/website, unique domains,
    classified vs unclassified, and in_icp breakdowns by city and main_category.
    """
    _ensure_repo_cwd()
    from gmscraper import export

    return _json(
        export.leads_summary(
            _store(),
            icp_only=icp_only,
            source=source,
            clean=clean,
            backfill_cities=True,
        )
    )


@mcp.tool(
    annotations=_ann(
        'Sample leads for QA',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def sample_leads(
    limit: int = 20,
    icp_only: bool = False,
    with_email: bool = False,
    city: str = "",
    order: str = "random",
) -> str:
    """Return a small inline sample of lead rows for quality checks.

    Prefer order=random (default) so you do not only see the first few ZIPs.
    Caps at 100 rows. Useful subset only — not a full export.
    """
    _ensure_repo_cwd()
    from gmscraper import export

    ord_norm = (order or "random").strip().lower()
    if ord_norm not in ("random", "recent"):
        raise ValueError("order must be 'random' or 'recent'")
    rows = export.sample_leads(
        _store(),
        limit=limit,
        icp_only=icp_only,
        with_email=with_email,
        city=city,
        order=ord_norm,  # type: ignore[arg-type]
    )
    return _json(
        {
            "count": len(rows),
            "order": ord_norm,
            "icp_only": icp_only,
            "with_email": with_email,
            "rows": rows,
        }
    )


@mcp.tool(
    annotations=_ann(
        'List registered clients',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def list_clients() -> str:
    """Show registered clients and their dedicated Supabase tables.

    peterson → public.peterson_{leads,contacts,companies} (Kyle / Roofs by Peterson)
    basco    → public.basco_{leads,contacts,companies} (Carlos / Basco Warranty)
    Always pass client_tag on scrape/sync/enrich so rows never mix.
    """
    from gmscraper import clients as client_reg

    return _json(
        {
            "clients": client_reg.list_clients_public(),
            "usage": {
                "scrape": "run_leads(..., client_tag='peterson'|'basco')",
                "sync": (
                    "sync_to_supabase(client_tag='peterson'|'basco', "
                    "state=..., main_category=..., center=..., radius_miles=...)"
                ),
                "enrich": "enrich_waterfall(..., client_tag='peterson'|'basco')",
                "aliases": "kyle→peterson, carlos→basco",
            },
        }
    )


@mcp.tool(
    annotations=_ann(
        'Ensure client Supabase tables',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def ensure_client_tables(client_tag: str = "") -> str:
    """Probe (and report DDL for) per-client Supabase schemas.

    Creates nothing itself when PostgREST cannot DDL — returns apply_sql to
    run once in the Supabase SQL editor. Pass client_tag to check one client,
    or omit to check all registered clients.
    """
    _ensure_repo_cwd()
    from gmscraper import clients as client_reg
    from mcp_server.errors import tool_error_from_exception

    try:
        return _json(client_reg.ensure_client_tables(client_tag))
    except Exception as exc:  # noqa: BLE001
        return _json(tool_error_from_exception(exc))


@mcp.tool(
    annotations=_ann(
        'Sync leads to Supabase',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def sync_to_supabase(
    client_tag: str = "",
    table: str = "",
    schema: str = "",
    dataset: str = "",
    county: str = "",
    cursor: int = 0,
    page_size: int = 1000,
    max_pages: int = 0,
    project_id: str = "",
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
    background: bool = True,
) -> str:
    """Batch-upsert into the client's Supabase table. Counts only.

    dataset='' (default): requires client_tag ('peterson' or 'basco').
    Writes to {slug}_leads (never a shared maps_leads dump). Destination
    rows are stamped with that client_tag.

    Scope historical (untagged) SQLite rows with the same filters as
    classify_leads / enrich_sites: city, state (comma-separated OK),
    main_category, plan_id, run_id, source, and optional center+radius_miles.
    Examples:
      sync_to_supabase(client_tag='peterson', state='TX',
                       center='Dallas, TX', radius_miles=60)
      sync_to_supabase(client_tag='basco', state='NJ,NY,CT',
                       main_category='dealer')
    When any of those source scopes are set, SQLite is NOT filtered by
    client_tag (pre-tagging rows become reachable). With no source scope,
    SQLite is filtered by client_tag as before.

    dataset='parcels': page scrape_leads → permit_parcel.parcels with cursor
    pagination. Honour `county`. Upsert on natural key (county, account_id).
    Returns has_more + resume_token (cursor) when more pages remain.
    """
    _ensure_repo_cwd()
    ds = (dataset or "").strip().lower()

    if ds in ("parcels", "parcel"):
        from gmscraper import parcels_sync

        def _run_parcels() -> dict[str, Any]:
            return parcels_sync.sync_parcels(
                county=county or "",
                project_id=project_id or "",
                page_size=int(page_size or 1000),
                max_pages=int(max_pages or 0),
                cursor=int(cursor or 0),
            )

        if background and _http_mode():
            from mcp_server.jobs import start_job

            job = start_job(
                "sync_parcels",
                _run_parcels,
                meta={"county": county or None, "cursor": cursor},
            )
            return _json(
                {
                    "job_id": job.id,
                    "status": job.status,
                    "message": f"Poll get_job_status with job_id={job.id}.",
                }
            )
        return _json(_run_parcels())

    from gmscraper import supabase_sync
    from mcp_server.errors import tool_error_from_exception

    try:
        result = supabase_sync.sync_to_supabase(
            _store(),
            client_tag=client_tag,
            table=table,
            schema=schema,
            icp_only=icp_only,
            with_email=with_email,
            truncate=truncate,
            run_label=run_label,
            plan_id=plan_id,
            run_id=run_id,
            city=city,
            state=state,
            main_category=main_category,
            source=source,
            center=center,
            radius_miles=float(radius_miles or 0),
            center_lat=float(center_lat or 0),
            center_lng=float(center_lng or 0),
        )
        return _json(result)
    except Exception as exc:  # noqa: BLE001
        return _json(tool_error_from_exception(exc))


@mcp.tool(
    annotations=_ann(
        'Renormalize stored Maps JSON',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
)
def renormalize() -> str:
    """Re-map stored raw Maps JSON after ALIAS fixes (0 API calls)."""
    _ensure_repo_cwd()
    import argparse

    from gmscraper.cli import cmd_renormalize
    from gmscraper.config import DEFAULT_DB

    store = _store()
    cmd_renormalize(argparse.Namespace(db=str(DEFAULT_DB)))
    return _json({"status": "ok", "stats": store.stats()})


# ---------------------------------------------------------------------------
# Optional Railway remote job API
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=_ann(
        'Remote API health',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def remote_health() -> str:
    """Ping the Railway-hosted scraper API (requires GMAPS_API_BASE)."""
    return _json(_remote_json("GET", "/api/health"))


@mcp.tool(
    annotations=_ann(
        'List remote scrape jobs',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def list_remote_jobs() -> str:
    """List jobs stored in the Railway/Supabase-backed API."""
    data = _remote_json("GET", "/api/jobs")
    return _json(data)


@mcp.tool(
    annotations=_ann(
        'Create remote scrape job',
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def create_remote_job(
    prompt: str,
    tags: str = "",
) -> str:
    """Create a job on the Railway UI API. No spend-approval args — always proceeds."""
    body = {
        "prompt": prompt,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "approvals": {"maps": True, "llm": True, "apify": True},
    }
    return _json(_remote_json("POST", "/api/jobs", body))


@mcp.tool(
    annotations=_ann(
        'Download remote job CSV',
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
)
def download_remote_csv(job_id: str, out_path: str = "") -> str:
    """Download a completed remote job CSV to disk."""
    base = _remote_base()
    if not base:
        raise ValueError("GMAPS_API_BASE is not set.")
    url = f"{base}/api/jobs/{job_id}/file"
    out = Path(out_path) if out_path else ROOT / "data" / "outputs" / f"remote-{job_id}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=120) as resp:
        content = resp.read()
    out.write_bytes(content)
    return _json({"job_id": job_id, "bytes": len(content), "csv": str(out)})


from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse


@mcp.custom_route("/", methods=["GET"])
async def root_page(_request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "Google Maps Scraper MCP\n"
        "Claude web connector URL: /mcp\n"
        "Health: /health\n"
    )


def _health_payload() -> dict[str, Any]:
    """HTTP probe payload — mirrors key fields from the health MCP tool."""
    try:
        settings = _settings()
        from gmscraper.apify_contacts import apify_token_valid

        supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
        apify_set = bool(settings.apify_token)
        apify_ok = apify_set and apify_token_valid(settings.apify_token)
        return {
            "ok": True,
            "service": "google-maps-scraper-mcp",
            "version": "1.8.0",
            "transport": "streamable-http",
            "mcp_path": "/mcp",
            "primary_tools": [
                "resolve_addresses",
                "run_owner_lane",
                "run_lead_list",
                "outcome_status",
            ],
            "supabase_project_ref": _supabase_project_ref(supabase_url),
            "apify_token_set": apify_set,
            "apify_token_valid": apify_ok,
            "apify_configured": apify_ok,
            "apify_contact_actor": settings.apify_contact_actor,
            "auto_resume": os.environ.get("MCP_AUTO_RESUME", "true").lower()
            not in ("0", "false", "no"),
            "claude_web": (
                "Add this connector URL in Claude → Settings → Connectors: "
                "https://<your-host>/mcp"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "service": "google-maps-scraper-mcp",
            "error": f"{type(exc).__name__}: {exc}",
        }


@mcp.custom_route("/health", methods=["GET"])
async def health_live(_request: Request) -> JSONResponse:
    return JSONResponse(_health_payload())


@mcp.custom_route("/api/health", methods=["GET"])
async def health_live_api(_request: Request) -> JSONResponse:
    """Alias for platforms that probe /api/health."""
    return JSONResponse(_health_payload())


def _auto_resume_orphans(swept: dict[str, Any]) -> list[str]:
    """Re-enqueue resumable interrupted jobs from stored meta (no human action).

    Replays the original argument payload from job.meta. Paid kinds and global
    enrich_sites:limit=0 / backlog_drain are filtered in sweep_orphaned_jobs.
    """
    from mcp_server.jobs import make_queue_key, start_job

    auto = os.environ.get("MCP_AUTO_RESUME", "true").lower() not in (
        "0",
        "false",
        "no",
    )
    if not auto:
        return []
    resumed: list[str] = []
    for rec in swept.get("jobs") or []:
        kind = rec.get("kind") or ""
        meta = dict(rec.get("meta") or {})
        if meta.get("needs_approval") or meta.get("resume_skipped_reason"):
            print(
                f"Auto-resume skipped {kind}/{rec.get('id')}: "
                f"{meta.get('resume_skipped_reason') or 'needs_approval'}",
                flush=True,
            )
            continue
        try:
            if kind == "run_leads" and meta.get("plan_path"):
                plan_path = meta["plan_path"]
                workers = int(meta.get("workers") or 8)
                ctag = str(meta.get("client_tag") or "")
                job = start_job(
                    "run_leads",
                    lambda p=plan_path, w=workers, c=ctag: _execute_run_leads(
                        p, "", True, w, c
                    ),
                    meta={**meta, "auto_resumed_from": rec.get("id")},
                    queue_key=make_queue_key("run_leads", meta),
                    priority=int(meta.get("priority") or 10),
                )
                resumed.append(job.id)
            elif kind == "scrape_maps" and meta.get("plan_path"):
                plan_path = meta["plan_path"]
                workers = int(meta.get("workers") or 8)
                max_jobs = int(meta.get("max_jobs") or 0)
                ctag = str(meta.get("client_tag") or "")
                job = start_job(
                    "scrape_maps",
                    lambda p=plan_path, w=workers, m=max_jobs, c=ctag: _execute_scrape_maps(
                        p, w, m, c
                    ),
                    meta={**meta, "auto_resumed_from": rec.get("id")},
                    queue_key=make_queue_key("scrape_maps", meta),
                    priority=int(meta.get("priority") or 10),
                )
                resumed.append(job.id)
            elif kind == "enrich_sites":
                from gmscraper import enrich_site

                m = dict(meta)
                scope = _normalize_scope(
                    city=str(m.get("city") or ""),
                    state=str(m.get("state") or ""),
                    main_category=str(m.get("main_category") or ""),
                    plan_path=str(m.get("plan_path") or ""),
                    plan_id=str(m.get("plan_id") or ""),
                    run_id=str(m.get("run_id") or ""),
                    client_tag=str(m.get("client_tag") or ""),
                    source=str(m.get("source") or ""),
                )
                lim = int(m.get("limit") or 0)
                workers = int(m.get("workers") or 3)

                def _enrich(
                    sc=scope, lim=lim, workers=workers
                ) -> dict[str, Any]:
                    store = _store()
                    store.queue_sites()
                    if _http_mode() and not _scope_is_set(sc):
                        return _enrich_sites_via_subprocess(lim, workers)
                    pending = store.pending_sites(
                        limit=lim or None,
                        city=sc["city"],
                        state=sc["state"],
                        main_category=sc["main_category"],
                        plan_id=sc["plan_id"],
                        run_id=sc["run_id"],
                        client_tag=sc["client_tag"],
                        source=sc["source"],
                    )
                    res = enrich_site.run(
                        store,
                        pending,
                        workers=max(1, min(workers, 3)),
                        on_progress=lambda **p: _job_progress("enrich", **p),
                    )
                    return {
                        "result": res,
                        "stats": store.stats(),
                        "domains": len(pending),
                        "scope": sc,
                    }

                job = start_job(
                    "enrich_sites",
                    _enrich,
                    meta={**meta, **scope, "auto_resumed_from": rec.get("id")},
                    queue_key=make_queue_key("enrich_sites", {**meta, **scope}),
                    priority=int(meta.get("priority") or (10 if _scope_is_set(scope) else 5)),
                )
                resumed.append(job.id)
            elif kind == "classify_leads":
                m = dict(meta)
                # Never re-force on resume — force=true wipes verdicts and
                # restarts the starvation loop after every container restart.
                m["force"] = False

                def _classify(mm=m) -> dict[str, Any]:
                    from gmscraper import classify
                    from gmscraper.llm import default_workers
                    from mcp_server.jobs import current_job_id, heartbeat as _hb

                    store = _store()
                    llm = _llm()
                    text = (mm.get("icp") or "").strip()
                    if not text:
                        raise ValueError("classify_leads resume missing icp")
                    jid = current_job_id()

                    def _prog(**p: Any) -> None:
                        if jid:
                            _hb(jid, **p)

                    res = classify.run(
                        store,
                        llm,
                        text,
                        workers=int(mm.get("workers") or 0) or default_workers(llm),
                        source=str(mm.get("source") or ""),
                        force=False,
                        limit=int(mm.get("limit") or 0) or None,
                        include_no_site=bool(mm.get("include_no_site")),
                        center=str(mm.get("center") or ""),
                        radius_miles=float(mm.get("radius_miles") or 0),
                        center_lat=float(mm["center_lat"])
                        if mm.get("center_lat")
                        else None,
                        center_lng=float(mm["center_lng"])
                        if mm.get("center_lng")
                        else None,
                        require_geo=bool(mm.get("require_geo", True)),
                        city=str(mm.get("city") or ""),
                        state=str(mm.get("state") or ""),
                        main_category=str(mm.get("main_category") or ""),
                        plan_id=str(mm.get("plan_id") or ""),
                        run_id=str(mm.get("run_id") or ""),
                        client_tag=str(mm.get("client_tag") or ""),
                        on_progress=_prog,
                    )
                    return {
                        "result": res,
                        "stats": store.stats(),
                        "llm_spend": llm.spend_line(),
                        "has_more": bool(res.get("has_more")),
                        "remaining": res.get("remaining"),
                    }

                job = start_job(
                    "classify_leads",
                    _classify,
                    meta={**meta, "force": False, "auto_resumed_from": rec.get("id")},
                    queue_key=make_queue_key("classify_leads", meta),
                    priority=int(meta.get("priority") or 20),
                )
                resumed.append(job.id)
            elif kind == "run_owner_lane":
                from gmscraper import outcomes as oc

                m = dict(meta)
                # Interrupted paid runs were never dry — default dry_run false on resume.
                dry = bool(m.get("operators_dry_run", False))

                def _owner(mm=m, dry=dry) -> dict[str, Any]:
                    return oc.run_owner_lane(
                        states=str(mm.get("states") or "TX"),
                        rebuild_operators=bool(mm.get("rebuild_operators")),
                        operators_dry_run=dry,
                        min_parcels=int(mm.get("min_parcels") or 1),
                        center=str(mm.get("center") or ""),
                        radius_miles=float(mm.get("radius_miles") or 0),
                        owner_segments=str(mm.get("owner_segments") or "private"),
                        resolve_limit=int(mm.get("resolve_limit") or 0),
                        method=str(mm.get("method") or "serp"),
                        min_confidence=float(mm.get("min_confidence") or 0.35),
                        project_id=str(mm.get("project_id") or ""),
                        estimate_only=False,
                        on_progress=lambda **p: _job_progress(**p),
                    )

                job = start_job(
                    "run_owner_lane",
                    _owner,
                    meta={
                        **meta,
                        "operators_dry_run": dry,
                        "auto_resumed_from": rec.get("id"),
                    },
                    queue_key=make_queue_key("run_owner_lane", meta),
                    priority=int(meta.get("priority") or 10),
                )
                resumed.append(job.id)
            elif kind == "resolve_places" and meta.get("table"):
                from gmscraper import resolve_places as rp

                m = dict(meta)
                table = m.get("table") or ""
                pid = _default_leads_project_id(table, str(m.get("project_id") or ""))
                schema = _default_source_schema(table, str(m.get("schema") or ""))
                m["project_id"] = pid
                m["schema"] = schema

                def _resolve(mm=m) -> dict[str, Any]:
                    return rp.run(
                        schema=mm.get("schema") or "",
                        table=mm.get("table") or "",
                        key_column=mm.get("key_column") or "id",
                        address_column=mm.get("address_column") or "",
                        name_column=mm.get("name_column") or "",
                        city_column=mm.get("city_column") or "",
                        where=mm.get("where") or "",
                        order_by=mm.get("order_by") or "",
                        limit=int(mm.get("limit") or 0),
                        strategy=mm.get("strategy") or "address",
                        min_confidence=float(mm.get("min_confidence") or 0.6),
                        workers=int(mm.get("workers") or 8),
                        details_only=bool(mm.get("details_only")),
                        project_id=mm.get("project_id") or "",
                        on_progress=lambda **p: _job_progress(**p),
                    )

                job = start_job(
                    "resolve_places",
                    _resolve,
                    meta={**meta, "project_id": pid, "auto_resumed_from": rec.get("id")},
                    queue_key=make_queue_key(
                        "resolve_places", {**meta, "project_id": pid}
                    ),
                    priority=int(meta.get("priority") or 10),
                )
                resumed.append(job.id)
            elif kind == "resolve_via_serp" and meta.get("table"):
                from gmscraper import resolve_serp as rs

                m = dict(meta)
                table = m.get("table") or ""
                pid = _default_leads_project_id(table, str(m.get("project_id") or ""))
                schema = _default_source_schema(table, str(m.get("schema") or ""))
                m["project_id"] = pid
                m["schema"] = schema

                def _resolve_serp(mm=m) -> dict[str, Any]:
                    tbl = mm.get("table") or ""
                    order = mm.get("order_by") or (
                        "portfolio_value DESC NULLS LAST"
                        if tbl == "operators"
                        else ""
                    )
                    return rs.run(
                        schema=mm.get("schema") or "",
                        table=tbl,
                        key_column=mm.get("key_column") or "operator_address",
                        address_column=mm.get("address_column") or "",
                        name_column=mm.get("name_column") or "",
                        city_column=mm.get("city_column") or "",
                        where=mm.get("where") or "",
                        order_by=order,
                        limit=int(mm.get("limit") or 0),
                        min_confidence=float(mm.get("min_confidence") or 0.35),
                        batch_size=int(mm.get("batch_size") or 100),
                        project_id=mm.get("project_id") or "",
                        on_progress=lambda **p: _job_progress(**p),
                    )

                job = start_job(
                    "resolve_via_serp",
                    _resolve_serp,
                    meta={**meta, "project_id": pid, "auto_resumed_from": rec.get("id")},
                    queue_key=make_queue_key(
                        "resolve_via_serp", {**meta, "project_id": pid}
                    ),
                    priority=int(meta.get("priority") or 10),
                )
                resumed.append(job.id)
            elif kind == "pipeline_run" and meta.get("schema") and meta.get("table"):
                from gmscraper import pipeline as pipe

                m = dict(meta)
                # Infer binding fields when older jobs omitted them.
                table = m.get("table") or ""
                key_col = m.get("key_column") or (
                    "operator_address" if table == "operators" else "id"
                )
                addr_col = m.get("address_column") or (
                    "operator_address" if table == "operators" else ""
                )
                name_col = m.get("name_column") or (
                    "operator_name" if table == "operators" else ""
                )
                pid = _default_leads_project_id(table, str(m.get("project_id") or ""))

                def _pipe(
                    mm=m,
                    key_col=key_col,
                    addr_col=addr_col,
                    name_col=name_col,
                    table=table,
                    pid=pid,
                ) -> dict[str, Any]:
                    return pipe.run(
                        _store(),
                        schema=mm.get("schema") or "",
                        table=table,
                        key_column=key_col,
                        address_column=addr_col,
                        name_column=name_col,
                        city_column=mm.get("city_column") or "",
                        where=mm.get("where") or "",
                        order_by=mm.get("order_by") or "",
                        stages=mm.get("stages") or "resolve,enrich,extract,contacts",
                        max_tier=mm.get("max_tier") or "getleads",
                        limit=int(mm.get("limit") or 0),
                        use_llm=bool(mm.get("use_llm", True)),
                        strategy=mm.get("strategy") or "address",
                        min_confidence=float(mm.get("min_confidence") or 0.6),
                        target_titles=mm.get("target_titles") or "",
                        project_id=pid,
                        workers=int(mm.get("workers") or 8),
                        on_progress=lambda **p: _job_progress(**p),
                    )

                job = start_job(
                    "pipeline_run",
                    _pipe,
                    meta={
                        **meta,
                        "key_column": key_col,
                        "address_column": addr_col,
                        "name_column": name_col,
                        "project_id": pid,
                        "auto_resumed_from": rec.get("id"),
                    },
                    queue_key=make_queue_key("pipeline_run", meta),
                    priority=int(meta.get("priority") or 10),
                )
                resumed.append(job.id)
        except Exception as exc:  # noqa: BLE001
            print(f"Auto-resume skipped for {kind}/{rec.get('id')}: {exc}", flush=True)
    return resumed


def _start_backlog_drain() -> None:
    """Background loop that drains pending sites / team pages when idle."""
    enabled = os.environ.get("MCP_BACKLOG_DRAIN", "true").lower() not in (
        "0",
        "false",
        "no",
    )
    if not enabled or not _http_mode():
        return
    interval = int(os.environ.get("MCP_BACKLOG_DRAIN_SEC", "300") or 300)
    batch = int(os.environ.get("MCP_BACKLOG_BATCH", "200") or 200)
    team_workers = int(os.environ.get("MCP_BACKLOG_TEAM_WORKERS", "4") or 4)

    def loop() -> None:
        import time

        from mcp_server.jobs import list_jobs, start_job

        while True:
            time.sleep(max(60, interval))
            try:
                active = [
                    j
                    for j in list_jobs(limit=20)
                    if j.status in ("queued", "running")
                ]
                if active:
                    continue
                store = _store()
                store.queue_sites()
                pending = store.pending_sites(limit=batch)
                if pending:
                    print(
                        f"Backlog drain: queueing enrich_sites for {len(pending)} domains",
                        flush=True,
                    )
                    start_job(
                        "enrich_sites",
                        lambda: _enrich_sites_via_subprocess(batch, 3),
                        meta={"limit": batch, "workers": 3, "backlog_drain": True},
                        queue_key=f"enrich_sites:backlog:{batch}",
                        dedupe=True,
                        priority=0,
                    )
                    continue

                # After site fetch drains, crawl team/about pages + extract.
                need_team = store.domains_needing_team_crawl(limit=batch)
                if not need_team:
                    continue
                print(
                    f"Backlog drain: queueing crawl_team_pages for {len(need_team)} domains",
                    flush=True,
                )

                def _team() -> dict[str, Any]:
                    from gmscraper import enrich_site, team_contacts

                    s = _store()
                    crawl = enrich_site.crawl_team_pages(
                        s, limit=batch, workers=team_workers
                    )
                    extract = team_contacts.run(
                        s, use_llm=True, workers=2, limit=batch
                    )
                    return {
                        "crawl": crawl,
                        "extract": extract,
                        "stats": s.stats(),
                    }

                start_job(
                    "crawl_team_pages",
                    _team,
                    meta={
                        "limit": batch,
                        "workers": team_workers,
                        "backlog_drain": True,
                    },
                    queue_key=f"crawl_team_pages:backlog:{batch}",
                    dedupe=True,
                    priority=0,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"Backlog drain tick failed: {exc}", flush=True)

    import threading

    threading.Thread(target=loop, name="mcp-backlog-drain", daemon=True).start()


def main() -> None:
    """stdio for local Claude Desktop; streamable-http for Railway / Claude web."""
    _ensure_repo_cwd()
    print(f"MCP server version {mcp.version}", flush=True)
    _dump_tool_annotations()

    # Validate Apify token at boot — env-var presence alone is not enough.
    try:
        from gmscraper.apify_contacts import apify_token_valid
        from gmscraper.config import settings as _cfg

        if _cfg.apify_token:
            ok = apify_token_valid(_cfg.apify_token)
            print(
                f"Apify token startup check: {'valid' if ok else 'INVALID'}",
                flush=True,
            )
            if not ok:
                print(
                    "WARNING: APIFY_TOKEN set but GET /v2/users/me failed — "
                    "apify_contact_crawl will return missing_or_invalid_credential.",
                    flush=True,
                )
        else:
            print("Apify token startup check: not configured", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Apify token startup check skipped: {exc}", flush=True)

    # Container restarts kill in-process workers; flip leftovers so they are
    # not stuck as status=running forever, then auto-requeue when possible.
    try:
        from mcp_server.jobs import sweep_orphaned_jobs

        swept = sweep_orphaned_jobs()
        if swept.get("interrupted"):
            print(
                f"Marked {swept['interrupted']} orphaned background job(s) "
                f"as interrupted: {', '.join(swept.get('job_ids') or [])}",
                flush=True,
            )
            resumed = _auto_resume_orphans(swept)
            if resumed:
                print(
                    f"Auto-resumed {len(resumed)} job(s): {', '.join(resumed)}",
                    flush=True,
                )
    except Exception as exc:  # noqa: BLE001
        print(f"Job orphan sweep skipped: {exc}", flush=True)

    try:
        _start_backlog_drain()
    except Exception as exc:  # noqa: BLE001
        print(f"Backlog drain not started: {exc}", flush=True)

    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    if transport in ("streamable-http", "http"):
        from mcp.server.transport_security import TransportSecuritySettings

        # Must use mcp.run() so StreamableHTTP session manager lifespan starts.
        # Claude web connects from Anthropic's cloud (not the browser), so CORS
        # is not required — only a public HTTPS /mcp endpoint.
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            streamable_http_path="/mcp",
            stateless_http=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
        return

    if transport == "sse":
        mcp.run(transport="sse", host=host, port=port)
        return

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
