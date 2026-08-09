"""Claude MCP server for the Google Maps lead scraper.

Exposes planning, estimation, stage runners, full pipeline runs, and optional
Railway job history. No connector auth and no spend-approval gate. Paid tools
use a saved plan from plan_leads / estimate_cost (plan_path or latest plan).
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
    AUTO_APPROVE_UNDER_USD,
    create_approval,
    mark_used,
    require_spend_approval,
    resolve_plan,
)
from mcp_server.playbook import FIND_LEADS_PROMPT, INSTRUCTIONS, WHEN_TO_USE_PROMPT

ROOT = Path(__file__).resolve().parent.parent

mcp = MCPServer(
    name="google-maps-scraper",
    title="Google Maps Scraper",
    description=(
        "Build US local-business lead CSVs from Google Maps. "
        "Use when the user asks for niche + city/state leads, owners, or emails."
    ),
    instructions=INSTRUCTIONS,
    website_url="https://google-maps-mcp-production-88a3.up.railway.app/mcp",
    version="1.4.0",
)


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
        "auto_approve_under_usd": AUTO_APPROVE_UNDER_USD,
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


@mcp.tool(
    annotations=ToolAnnotations(
        title="Health / config check",
        readOnlyHint=True,
        openWorldHint=False,
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

    apify_ok = bool(settings.apify_token) and apify_token_valid(settings.apify_token)
    return _json(
        {
            "ok": True,
            "maps_plan": settings.maps_plan,
            "rapidapi_configured": bool(settings.rapidapi_key),
            "llm_provider": settings.llm_provider,
            "openai_configured": bool(settings.openai_api_key),
            "apify_configured": apify_ok,
            "apify_contact_actor": settings.apify_contact_actor,
            "apify_content_actor": settings.apify_content_actor,
            "apify_max_cost_usd": settings.apify_max_cost_usd,
            "supabase_configured": bool(supabase_url and supabase_key),
            "supabase_url": supabase_url or None,
            "db": str(DEFAULT_DB),
            "zips": str(DEFAULT_ZIPS),
            "zips_ready": Path(DEFAULT_ZIPS).exists(),
            "categories_file": str(DEFAULT_CATEGORIES),
            "remote_api": _remote_base() or None,
            "auth": "none",
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="List vertical categories",
        readOnlyHint=True,
        openWorldHint=False,
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
    annotations=ToolAnnotations(
        title="Ensure US ZIP list",
        readOnlyHint=False,
        openWorldHint=False,
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
    annotations=ToolAnnotations(
        title="Pipeline database stats",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def pipeline_stats() -> str:
    """Show what is already in the local SQLite leads database."""
    _ensure_repo_cwd()
    store = _store()
    return _json(store.stats())


@mcp.tool(
    annotations=ToolAnnotations(
        title="Plan leads from brief",
        readOnlyHint=True,
        openWorldHint=True,
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
    estimate (LLM only — no Google Maps spend yet). Returns plan_path (and an
    optional approval_id) for run_leads — no spend approval is required.

    Geography overrides (precedence: zips > center+radius_miles > states):
      zips              comma-separated 5-digit ZIPs (supports 1000+). Wins outright.
      center            "Dallas, TX" or "32.7767,-97.0000"
      radius_miles      miles from center (haversine over ZIP centroids)
      exclude_categories  comma-separated Maps categories to never scrape

    Show the user the cost summary, then call run_leads (approval_id optional).
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
    approval = create_approval(
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
            **approval.to_public(),
            **bundle,
            "nationwide_warning": nationwide_warning,
            "next_step": (
                f"Call run_leads(plan_path={str(plan_path)!r}) — "
                "no approval / auth required. approval_id is optional."
            ),
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Estimate cost for a vertical",
        readOnlyHint=True,
        openWorldHint=False,
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
    approval = create_approval(
        brief=used_brief,
        plan_path=str(plan_path),
        requests=bundle["requests"],
        estimated_overage_usd=bundle["estimated_overage_usd"],
        blocked=bundle["blocked"],
        states=plan.states,
        categories=plan.categories,
        vertical=plan.vertical,
    )
    return _json({**approval.to_public(), **bundle, "maps_plan": settings.maps_plan})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Probe Maps API (1 request)",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=False,
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
# Paid tools (no spend-approval gate — plan_path / latest plan is enough)
# ---------------------------------------------------------------------------


def _http_mode() -> bool:
    return os.environ.get("MCP_TRANSPORT", "stdio").lower() in (
        "streamable-http",
        "http",
        "sse",
    )


def _execute_run_leads(
    approval_id: str,
    plan_path: str,
    out_path: str,
    include_owner_fallback: bool,
    workers: int,
) -> dict[str, Any]:
    from gmscraper import brief as brief_mod
    from gmscraper import classify, enrich_site, export, owner, scrape
    from gmscraper.config import settings
    from gmscraper.llm import default_workers
    from gmscraper.mapsdata import MapsDataClient
    from gmscraper.websearch import make_backend

    approval = resolve_plan(approval_id=approval_id, plan_path=plan_path)
    settings.require_rapidapi()
    plan = brief_mod.load(approval.plan_path)
    _ensure_zips_file()
    zip_rows = _zip_rows_for_plan(plan)
    store = _store()
    llm = _llm()
    client = MapsDataClient(settings)

    stamp = approval.id if approval.id != "plan_path" else "run"
    out = Path(out_path) if out_path else ROOT / "data" / "outputs" / f"{plan.vertical}-{stamp}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    scrape.run(
        store,
        client,
        zip_rows,
        plan.categories,
        workers=workers,
        price_per_request=settings.price_per_request,
    )
    store.queue_sites()
    enrich_site.run(store, store.pending_sites(), workers=max(workers, 12))
    classify.run(
        store,
        llm,
        plan.icp,
        workers=default_workers(llm),
    )
    if plan.require_owner:
        backend = make_backend(settings, "") if include_owner_fallback else None
        owner.run(store, llm, backend, workers=default_workers(llm))

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
    mark_used(approval.id)
    return {
        "status": "completed",
        "leads": n,
        "csv": str(out),
        "approval_id": approval.id if approval.id != "plan_path" else None,
        "plan_path": approval.plan_path,
        "stats": store.stats(),
        "llm_spend": llm.spend_line() if hasattr(llm, "spend_line") else None,
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Run full lead pipeline",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def run_leads(
    approval_id: str = "",
    plan_path: str = "",
    out_path: str = "",
    include_owner_fallback: bool = False,
    workers: int = 8,
    background: bool = True,
    i_approve_spend: bool = True,
) -> str:
    """Execute the full lead pipeline after plan_leads. This is the main "go" tool.

    No spend approval required. Prefer plan_path from plan_leads; approval_id is
    optional. If both are omitted, the latest saved plan is used.

    On Railway/HTTP this starts a background job — poll get_job_status with the
    returned job_id until completed. Then tell the user the lead count and CSV path.
    """
    _ensure_repo_cwd()
    del i_approve_spend  # accepted for older Claude tool schemas; ignored
    resolve_plan(approval_id=approval_id, plan_path=plan_path)

    run_bg = background if background is not None else _http_mode()
    if run_bg and _http_mode():
        from mcp_server.jobs import start_job

        job = start_job(
            "run_leads",
            lambda: _execute_run_leads(
                approval_id, plan_path, out_path, include_owner_fallback, workers
            ),
            meta={"approval_id": approval_id or None, "plan_path": plan_path or None},
        )
        return _json(
            {
                "status": "started",
                "job_id": job.id,
                "message": (
                    "Pipeline started in the background. Poll get_job_status "
                    f"with job_id={job.id} until status is completed/failed."
                ),
            }
        )

    return _json(
        _execute_run_leads(
            approval_id, plan_path, out_path, include_owner_fallback, workers
        )
    )


def _execute_scrape_maps(
    approval_id: str, plan_path: str, workers: int, max_jobs: int
) -> dict[str, Any]:
    from gmscraper import brief as brief_mod
    from gmscraper import scrape
    from gmscraper.config import settings
    from gmscraper.mapsdata import MapsDataClient

    approval = resolve_plan(approval_id=approval_id, plan_path=plan_path)
    settings.require_rapidapi()
    plan = brief_mod.load(approval.plan_path)
    zip_rows = _zip_rows_for_plan(plan)
    store = _store()
    client = MapsDataClient(settings)
    res = scrape.run(
        store,
        client,
        zip_rows,
        plan.categories,
        workers=workers,
        price_per_request=settings.price_per_request,
        max_jobs=max_jobs or None,
    )
    mark_used(approval.id)
    return {
        "status": "completed",
        "result": res,
        "stats": store.stats(),
        "zip_count": len(zip_rows),
        "sample_source_zips": [r["zip"] for r in zip_rows[:20]],
        "plan_path": approval.plan_path,
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Scrape Google Maps only",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def scrape_maps(
    approval_id: str = "",
    plan_path: str = "",
    workers: int = 8,
    max_jobs: int = 0,
    background: bool = True,
    i_approve_spend: bool = True,
) -> str:
    """Paid Maps scrape stage only. No spend approval required.

    Prefer plan_path from plan_leads/estimate_cost; approval_id is optional.
    Omitting both uses the latest saved plan.
    """
    _ensure_repo_cwd()
    del i_approve_spend
    resolve_plan(approval_id=approval_id, plan_path=plan_path)

    if background and _http_mode():
        from mcp_server.jobs import start_job

        job = start_job(
            "scrape_maps",
            lambda: _execute_scrape_maps(approval_id, plan_path, workers, max_jobs),
            meta={"approval_id": approval_id or None, "plan_path": plan_path or None},
        )
        return _json(
            {
                "status": "started",
                "job_id": job.id,
                "message": f"Poll get_job_status with job_id={job.id}.",
            }
        )

    return _json(_execute_scrape_maps(approval_id, plan_path, workers, max_jobs))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get background job status",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def get_job_status(job_id: str) -> str:
    """Poll a background run_leads / scrape_maps job. Use after run_leads returns job_id."""
    from mcp_server.jobs import get_job

    return _json(get_job(job_id).to_public())


@mcp.tool(
    annotations=ToolAnnotations(
        title="List background jobs",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def list_background_jobs(limit: int = 20) -> str:
    """List recent background pipeline jobs on this MCP server."""
    from mcp_server.jobs import list_jobs

    return _json([j.to_public() for j in list_jobs(limit=limit)])


@mcp.tool(
    annotations=ToolAnnotations(
        title="Enrich business websites",
        readOnlyHint=False,
        openWorldHint=True,
    )
)
def enrich_sites(limit: int = 0, workers: int = 12) -> str:
    """Fetch website text/emails for pending domains (free, no Maps spend).

    Shallow same-domain crawl: homepage + up to 3 about/team pages
    (/about, /team, /leadership, …). Per-page text is stored with page_type.
    """
    _ensure_repo_cwd()
    from gmscraper import enrich_site

    store = _store()
    store.queue_sites()
    domains = store.pending_sites(limit=limit or None)
    res = enrich_site.run(store, domains, workers=workers)
    return _json({"result": res, "stats": store.stats()})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Crawl team/about pages",
        readOnlyHint=False,
        openWorldHint=True,
    )
)
def crawl_team_pages(
    limit: int = 0,
    workers: int = 12,
    force: bool = False,
    background: bool = True,
) -> str:
    """Re-crawl about/team pages for domains already fetched (free).

    Use after a large enrich_sites run (e.g. ~8k sites) so team-page text is
    tagged by page_type. force=true re-crawls even if team pages exist.
    On HTTP transport defaults to a background job — poll get_job_status.
    """
    _ensure_repo_cwd()
    from gmscraper import enrich_site

    store = _store()

    def _run() -> dict[str, Any]:
        res = enrich_site.crawl_team_pages(
            store,
            limit=limit or None,
            workers=workers,
            force=force,
        )
        return {"result": res, "stats": store.stats()}

    if background and _http_mode():
        from mcp_server.jobs import start_job

        job = start_job("crawl_team_pages", _run, meta={"limit": limit, "force": force})
        return _json(
            {
                "job_id": job.id,
                "status": job.status,
                "message": f"Poll get_job_status with job_id={job.id}.",
            }
        )
    return _json(_run())


@mcp.tool(
    annotations=ToolAnnotations(
        title="Extract team-page contacts",
        readOnlyHint=False,
        openWorldHint=False,
    )
)
def extract_team_contacts(
    limit: int = 0,
    workers: int = 8,
    icp_only: bool = False,
    use_llm: bool = False,
    target_titles: str = "",
    background: bool = True,
) -> str:
    """Parse person+title pairs from team/about page text into contacts.

    Writes to local contacts. Also fills empty owners.
    Prefer use_llm=true — heuristic path invents title/company "names".
    target_titles = comma-separated roles to prefer (vertical-agnostic default
    when empty: owner, founder, president, principal, partner, chief, …).
    """
    _ensure_repo_cwd()
    from gmscraper import team_contacts

    store = _store()
    titles = [t.strip() for t in (target_titles or "").split(",") if t.strip()]

    def _run() -> dict[str, Any]:
        llm = _llm() if use_llm else None
        res = team_contacts.run(
            store,
            limit=limit or None,
            workers=workers,
            icp_only=icp_only,
            use_llm=use_llm,
            llm=llm,
            target_titles=titles or None,
        )
        out: dict[str, Any] = {"result": res, "stats": store.stats()}
        if llm:
            out["llm_spend"] = llm.spend_line()
        return out

    if background and _http_mode():
        from mcp_server.jobs import start_job

        job = start_job(
            "extract_team_contacts",
            _run,
            meta={
                "limit": limit,
                "icp_only": icp_only,
                "use_llm": use_llm,
                "target_titles": titles or None,
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
    annotations=ToolAnnotations(
        title="Ingest external leads",
        readOnlyHint=False,
        openWorldHint=False,
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
    annotations=ToolAnnotations(
        title="Resolve places (address/name → business)",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
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
    background: bool = True,
) -> str:
    """Turn source-table rows into business identities via Maps.

    Generic source binding: pass schema/table/columns — no hardcoded vertical.
    strategy = 'address' | 'name' | 'address_then_name'.
    Writes domain/phone/place_id/confidence/resolved per row as each completes
    (resumable). Low-confidence multi-tenant hits are stored but do not overwrite
    stronger values. estimate_only returns request/cost projection and starts nothing.
    Response is counts only.
    """
    _ensure_repo_cwd()
    from gmscraper import resolve_places as rp

    def _run() -> dict[str, Any]:
        return rp.run(
            schema=schema,
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
            project_id=project_id or "",
        )

    if background and _http_mode() and not estimate_only:
        from mcp_server.jobs import start_job

        job = start_job(
            "resolve_places",
            _run,
            meta={
                "schema": schema,
                "table": table,
                "limit": limit,
                "strategy": strategy,
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
    annotations=ToolAnnotations(
        title="Pipeline: resolve → enrich → extract → contacts",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
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
        )

    if background and _http_mode() and not estimate_only:
        from mcp_server.jobs import start_job

        job = start_job(
            "pipeline_run",
            _run,
            meta={
                "schema": schema,
                "table": table,
                "stages": stages,
                "max_tier": max_tier,
                "limit": limit,
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
    annotations=ToolAnnotations(
        title="Estimate domain resolve cost",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def estimate_resolve_domains(source: str = "", limit: int = 0) -> str:
    """Estimate paid Maps cost to find websites for businesses missing a domain.

    Only ~28% of typical Shovels rows have websites; classify needs site text.
    Returns plan_path / optional approval_id for resolve_domains. One Maps
    request per business. No spend approval required.
    """
    _ensure_repo_cwd()
    from gmscraper import resolve_domains
    from mcp_server.approvals import create_approval

    store = _store()
    est = resolve_domains.estimate(store, source=source, limit=limit)
    plan_path = resolve_domains.save_resolve_plan(est, source, limit)
    approval = create_approval(
        brief=f"resolve_domains source={source or '*'} limit={limit or 'all'}",
        plan_path=str(plan_path),
        requests=int(est["requests"]),
        estimated_overage_usd=est["estimated_overage_usd"],
        blocked=bool(est["blocked"]),
        states=[],
        categories=["resolve_domains"],
        vertical="resolve_domains",
    )
    public = approval.to_public()
    public.update(est)
    public["instruction"] = (
        "Call resolve_domains("
        + (f"source={source!r}, " if source else "")
        + (f"limit={limit}, " if limit else "")
        + "plan_path=… or omit ids). No approval required."
    )
    return _json(public)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Resolve missing domains via Maps",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def resolve_domains(
    approval_id: str = "",
    plan_path: str = "",
    source: str = "",
    limit: int = 0,
    force: bool = False,
    workers: int = 4,
    i_approve_spend: bool = True,
) -> str:
    """Paid Maps name+city lookup to fill website/domain on ingested rows.

    No spend approval required. approval_id / plan_path are optional — omit both
    to use the latest estimate_resolve_domains plan, or pass source/limit directly.
    After resolve, call enrich_sites then classify_leads(source=...).
    """
    _ensure_repo_cwd()
    from gmscraper import resolve_domains as resolve_mod

    del i_approve_spend
    store = _store()
    src = source
    lim = limit
    approval = None
    if approval_id or plan_path:
        approval = resolve_plan(approval_id=approval_id, plan_path=plan_path)
        try:
            plan = json.loads(Path(approval.plan_path).read_text(encoding="utf-8"))
            src = src or (plan.get("source") or "")
            if not lim:
                lim = int(plan.get("limit") or 0)
        except Exception:
            pass
    elif not source and not limit:
        # Fall back to latest plan if caller passed nothing.
        try:
            approval = resolve_plan()
            plan = json.loads(Path(approval.plan_path).read_text(encoding="utf-8"))
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
    if approval:
        mark_used(approval.id)
    next_src = src or "…"
    return _json(
        {
            "result": res,
            "approval_id": approval.id if approval and approval.id != "plan_path" else None,
            "stats": store.stats(),
            "next": (
                "Call enrich_sites for new domains, then "
                f"classify_leads(source={next_src!r}, icp=…)."
            ),
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Classify leads against ICP",
        readOnlyHint=False,
        openWorldHint=True,
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
) -> str:
    """LLM-classify businesses against an ICP (LLM cost only; not Maps).

    Only businesses with fetched website text are eligible by default. Scope
    with source (e.g. 'shovels'), re-run with force=true, and cap with limit.
    When nothing is eligible, result.reason explains why.
    """
    _ensure_repo_cwd()
    from gmscraper import classify
    from gmscraper.cli import pick_vertical
    from gmscraper.config import DEFAULT_CATEGORIES
    from gmscraper.llm import default_workers

    if not icp and vertical:
        icp, _ = pick_vertical(DEFAULT_CATEGORIES, vertical)
    if not icp:
        raise ValueError("Provide icp text or a known vertical.")
    store = _store()
    llm = _llm()
    res = classify.run(
        store,
        llm,
        icp,
        workers=workers or default_workers(llm),
        source=source,
        force=force,
        limit=limit or None,
        include_no_site=include_no_site,
    )
    out: dict[str, Any] = {
        "result": res,
        "stats": store.stats(),
        "llm_spend": llm.spend_line(),
    }
    if res.get("reason"):
        out["reason"] = res["reason"]
    return _json(out)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Find owner names",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def find_owners(
    use_paid_fallback: bool = False,
    workers: int = 0,
    i_approve_spend: bool = True,
    approval_id: str = "",
) -> str:
    """Extract owners + team contacts from site text.

    First pulls person+title pairs from team/about pages into `contacts`
    (source=team_page). Then LLM single-owner extraction. Website-only is free;
    Apify fallback is paid but needs no approval — set use_paid_fallback=true.
    """
    _ensure_repo_cwd()
    from gmscraper import owner
    from gmscraper.config import settings
    from gmscraper.llm import default_workers
    from gmscraper.websearch import make_backend

    del i_approve_spend, approval_id  # legacy Claude schema args; ignored

    backend = None
    if use_paid_fallback:
        backend = make_backend(settings, "")

    store = _store()
    llm = _llm()
    res = owner.run(store, llm, backend, workers=workers or default_workers(llm))
    return _json({"result": res, "stats": store.stats(), "llm_spend": llm.spend_line()})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Apify contact crawl",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def apify_contact_crawl(
    domains: str = "",
    source: str = "",
    limit: int = 0,
    max_pages_per_site: int = 5,
    verify_emails: bool = False,
    use_proxy: bool = True,
    estimate_only: bool = False,
    run_label: str = "",
    background: bool = True,
) -> str:
    """Run automation-lab/website-contact-finder; persist raw items locally.

    Pass domains as comma-separated hosts/URLs, or source='maps_no_owner' /
    'icp_no_owner' to select from local SQLite. Always estimates cost first;
    refuses when estimate exceeds APIFY_MAX_COST_USD. estimate_only=True
    returns the estimate and starts nothing.

    Response is counts + run_id only — never row payloads.
    """
    _ensure_repo_cwd()
    from gmscraper import apify_contacts

    store = _store()

    def _run() -> dict[str, Any]:
        return apify_contacts.crawl(
            store,
            domains=domains or "",
            source=source or "",
            limit=int(limit or 0),
            max_pages_per_site=int(max_pages_per_site or 5),
            verify_emails=bool(verify_emails),
            use_proxy=bool(use_proxy),
            estimate_only=bool(estimate_only),
            run_label=run_label or "",
        )

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
    annotations=ToolAnnotations(
        title="Parse Apify contacts via OpenAI",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
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
    annotations=ToolAnnotations(
        title="FullEnrich find email",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
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
    annotations=ToolAnnotations(
        title="FullEnrich find email (bulk)",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
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
    annotations=ToolAnnotations(
        title="Enrich waterfall → Supabase gc.*",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def enrich_waterfall(
    rows: str,
    need: str = "both",
    max_tier: str = "leadmagic",
    run_apify: bool = True,
    background: bool = True,
) -> str:
    """Walk apify → getleads → AI Ark → LeadMagic → FullEnrich; write to gc.*.

    `rows` = JSON list of {domain, first_name?, last_name?, company_name?, ...}.
    need = 'email' | 'dm' | 'both'.
    max_tier = 'apify' | 'getleads' | 'aiark' | 'leadmagic' | 'fullenrich'
    (default 'leadmagic' — FullEnrich never runs unless explicitly requested).

    Apify+OpenAI is a discovery tier for domains with no known person and runs
    before paid person lookups. Stops at first success per field. Records
    source_tier for hit-rate math. Response is counts only.
    """
    _ensure_repo_cwd()
    from gmscraper import waterfall as wf

    need_norm = (need or "both").strip().lower()
    if need_norm not in ("email", "dm", "both"):
        raise ValueError("need must be 'email', 'dm', or 'both'")
    max_tier_n = wf.normalize_max_tier(max_tier)

    store = _store()

    def _run() -> dict[str, Any]:
        return wf.enrich_waterfall(
            rows,
            need=need_norm,  # type: ignore[arg-type]
            store=store,
            write_supabase=True,
            max_tier=max_tier_n,
            run_apify=bool(run_apify),
        )

    if background and _http_mode() and len(rows or "") > 2000:
        from mcp_server.jobs import start_job

        job = start_job(
            "enrich_waterfall",
            _run,
            meta={
                "need": need_norm,
                "max_tier": max_tier_n,
                "rows_chars": len(rows or ""),
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
    annotations=ToolAnnotations(
        title="Export leads CSV",
        readOnlyHint=True,
        openWorldHint=False,
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
) -> str:
    """Return matching leads as CSV text in the response (free).

    Shape matches Property Owners pmf_shovels_contractors_export_csv:
      { total_matching, capped_at: 5000, csv: "<text>", ... }

    Caps at 5000 rows. Large responses may be spilled to a local file by the
    MCP client harness. Optional out_path also writes a full CSV on disk.
    clean=true (default) drops placeholder / agency emails. icp_reason is
    opt-in via include_reason. Blank cities are backfilled from address.
    """
    _ensure_repo_cwd()
    from gmscraper import export

    state_list = [s.strip().upper() for s in states.split(",") if s.strip()] or None
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
        backfill_cities=True,
    )
    return _json(payload)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Query leads (paginated)",
        readOnlyHint=True,
        openWorldHint=False,
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
    annotations=ToolAnnotations(
        title="Leads summary (counts)",
        readOnlyHint=True,
        openWorldHint=False,
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
    annotations=ToolAnnotations(
        title="Sample leads for QA",
        readOnlyHint=True,
        openWorldHint=False,
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
    annotations=ToolAnnotations(
        title="Sync leads to Supabase",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def sync_to_supabase(
    table: str = "maps_leads",
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
    background: bool = True,
) -> str:
    """Batch-upsert into Supabase. Counts only — never echoes rows.

    dataset='' (default): upsert local leads into `table` (maps_leads) on
    (place_id, run_label).

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

    result = supabase_sync.sync_to_supabase(
        _store(),
        table=table or "maps_leads",
        icp_only=icp_only,
        with_email=with_email,
        truncate=truncate,
        run_label=run_label,
    )
    return _json(result)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Renormalize stored Maps JSON",
        readOnlyHint=False,
        openWorldHint=False,
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
    annotations=ToolAnnotations(
        title="Remote API health",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def remote_health() -> str:
    """Ping the Railway-hosted scraper API (requires GMAPS_API_BASE)."""
    return _json(_remote_json("GET", "/api/health"))


@mcp.tool(
    annotations=ToolAnnotations(
        title="List remote scrape jobs",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def list_remote_jobs() -> str:
    """List jobs stored in the Railway/Supabase-backed API."""
    data = _remote_json("GET", "/api/jobs")
    return _json(data)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Create remote scrape job",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def create_remote_job(
    prompt: str,
    i_approve_spend: bool = True,
    approve_maps: bool = True,
    approve_llm: bool = True,
    approve_apify: bool = True,
    tags: str = "",
) -> str:
    """Create a job on the Railway UI API. No MCP auth — approvals are sent as true."""
    del i_approve_spend  # accepted for older Claude tool schemas
    body = {
        "prompt": prompt,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "approvals": {
            "maps": approve_maps,
            "llm": approve_llm,
            "apify": approve_apify,
        },
    }
    return _json(_remote_json("POST", "/api/jobs", body))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Download remote job CSV",
        readOnlyHint=True,
        openWorldHint=True,
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


@mcp.custom_route("/health", methods=["GET"])
async def health_live(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "google-maps-scraper-mcp",
            "transport": "streamable-http",
            "mcp_path": "/mcp",
            "claude_web": (
                "Add this connector URL in Claude → Settings → Connectors: "
                "https://<your-host>/mcp"
            ),
        }
    )


def main() -> None:
    """stdio for local Claude Desktop; streamable-http for Railway / Claude web."""
    _ensure_repo_cwd()
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
