"""Claude MCP server for the Google Maps lead scraper.

Exposes planning, estimation, stage runners, full pipeline runs, and optional
Railway job history. No connector auth. Paid tools need an approval_id from
plan_leads / estimate_cost (the saved plan), nothing else.
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
)

ROOT = Path(__file__).resolve().parent.parent

mcp = MCPServer(
    "google-maps-scraper",
    instructions=(
        "Google Maps US local-business lead pipeline. No login/auth required. "
        "Call plan_leads (or estimate_cost) first, show the cost, then call "
        "run_leads with that approval_id. Nationwide without a named state — ask first."
    ),
)


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


def _plan_cost_bundle(plan, zip_limit: int | None = None) -> dict[str, Any]:
    from gmscraper import brief as brief_mod
    from gmscraper import zips
    from gmscraper.config import settings

    _ensure_zips_file()
    zip_rows = zips.load(
        str(_ensure_zips_file()),
        states=plan.states or None,
        limit=zip_limit,
    )
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
    """Check scraper config: Maps plan, whether API keys are set, zip/db paths."""
    _ensure_repo_cwd()
    settings = _settings()
    from gmscraper.config import DEFAULT_CATEGORIES, DEFAULT_DB, DEFAULT_ZIPS

    return _json(
        {
            "ok": True,
            "maps_plan": settings.maps_plan,
            "rapidapi_configured": bool(settings.rapidapi_key),
            "llm_provider": settings.llm_provider,
            "openai_configured": bool(settings.openai_api_key),
            "apify_configured": bool(settings.apify_token),
            "db": str(DEFAULT_DB),
            "zips": str(DEFAULT_ZIPS),
            "zips_ready": Path(DEFAULT_ZIPS).exists(),
            "categories_file": str(DEFAULT_CATEGORIES),
            "remote_api": _remote_base() or None,
            "auto_approve_under_usd": AUTO_APPROVE_UNDER_USD,
            "mcp_auto_approve_env": os.environ.get("GMAPS_MCP_AUTO_APPROVE", ""),
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
    """List built-in verticals and their Google Maps category lists."""
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
def plan_leads(brief: str, zip_limit: int = 0) -> str:
    """Turn a plain-English brief into a scrape plan + cost estimate (LLM only, no Maps spend).

    Always call this before run_leads. Returns an approval_id (saved plan).
    Show the estimate, then call run_leads with that approval_id. No auth.
    """
    _ensure_repo_cwd()
    brief = brief.strip()
    if len(brief) < 8:
        raise ValueError("brief is too short — describe niche + region.")

    plan = __import__("gmscraper.brief", fromlist=["make_plan"]).make_plan(_llm(), brief)
    if not plan.states:
        # Nationwide is expensive — surface a warning but still return the plan
        nationwide_warning = (
            "No US state was detected. Nationwide is 20–30x a single-state run. "
            "Ask the user which state(s) before approving spend."
        )
    else:
        nationwide_warning = None

    bundle = _plan_cost_bundle(plan, zip_limit=zip_limit or None)
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
                f"Call run_leads with approval_id={approval.id}. No auth required."
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
) -> str:
    """Estimate Maps request count and overage without scraping.

    Provide either vertical (from list_categories) or comma-separated categories,
    plus optional comma-separated state codes. Or pass brief to LLM-plan first.
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
        )
        used_brief = brief or f"{vert} in {', '.join(state_list) or 'US'}"

    bundle = _plan_cost_bundle(plan, zip_limit=zip_limit or None)
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
# Paid tools (approval gate)
# ---------------------------------------------------------------------------


def _http_mode() -> bool:
    return os.environ.get("MCP_TRANSPORT", "stdio").lower() in (
        "streamable-http",
        "http",
        "sse",
    )


def _execute_run_leads(
    approval_id: str,
    out_path: str,
    include_owner_fallback: bool,
    workers: int,
) -> dict[str, Any]:
    from gmscraper import brief as brief_mod
    from gmscraper import classify, enrich_site, export, owner, scrape, zips
    from gmscraper.config import settings
    from gmscraper.llm import default_workers
    from gmscraper.mapsdata import MapsDataClient
    from gmscraper.websearch import make_backend

    approval = require_spend_approval(
        approval_id=approval_id,
        i_approve_spend=True,  # already gated by caller
    )
    settings.require_rapidapi()
    plan = brief_mod.load(approval.plan_path)
    _ensure_zips_file()
    zip_rows = zips.load(str(_ensure_zips_file()), states=plan.states or None)
    store = _store()
    llm = _llm()
    client = MapsDataClient(settings)

    out = Path(out_path) if out_path else ROOT / "data" / "outputs" / f"{plan.vertical}-{approval.id}.csv"
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
    )
    mark_used(approval_id)
    return {
        "status": "completed",
        "leads": n,
        "csv": str(out),
        "approval_id": approval_id,
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
    approval_id: str,
    i_approve_spend: bool = True,
    out_path: str = "",
    include_owner_fallback: bool = False,
    workers: int = 8,
    background: bool = True,
) -> str:
    """Run plan → scrape → enrich → classify → owners → CSV.

    Pass approval_id from plan_leads/estimate_cost. No auth required.
    On Railway, jobs start in the background by default — poll get_job_status.
    """
    _ensure_repo_cwd()
    require_spend_approval(approval_id=approval_id, i_approve_spend=True)

    run_bg = background if background is not None else _http_mode()
    if run_bg and _http_mode():
        from mcp_server.jobs import start_job

        job = start_job(
            "run_leads",
            lambda: _execute_run_leads(
                approval_id, out_path, include_owner_fallback, workers
            ),
            meta={"approval_id": approval_id},
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
        _execute_run_leads(approval_id, out_path, include_owner_fallback, workers)
    )


def _execute_scrape_maps(approval_id: str, workers: int, max_jobs: int) -> dict[str, Any]:
    from gmscraper import brief as brief_mod
    from gmscraper import scrape, zips
    from gmscraper.config import settings
    from gmscraper.mapsdata import MapsDataClient

    approval = require_spend_approval(
        approval_id=approval_id,
        i_approve_spend=True,
    )
    settings.require_rapidapi()
    plan = brief_mod.load(approval.plan_path)
    zip_rows = zips.load(str(_ensure_zips_file()), states=plan.states or None)
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
    mark_used(approval_id)
    return {"status": "completed", "result": res, "stats": store.stats()}


@mcp.tool(
    annotations=ToolAnnotations(
        title="Scrape Google Maps only",
        readOnlyHint=False,
        openWorldHint=True,
        destructiveHint=True,
    )
)
def scrape_maps(
    approval_id: str,
    i_approve_spend: bool = True,
    workers: int = 8,
    max_jobs: int = 0,
    background: bool = True,
) -> str:
    """Paid Maps scrape stage only (uses approval_id from plan/estimate). No auth."""
    _ensure_repo_cwd()
    require_spend_approval(approval_id=approval_id, i_approve_spend=True)

    if background and _http_mode():
        from mcp_server.jobs import start_job

        job = start_job(
            "scrape_maps",
            lambda: _execute_scrape_maps(approval_id, workers, max_jobs),
            meta={"approval_id": approval_id},
        )
        return _json(
            {
                "status": "started",
                "job_id": job.id,
                "message": f"Poll get_job_status with job_id={job.id}.",
            }
        )

    return _json(_execute_scrape_maps(approval_id, workers, max_jobs))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get background job status",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
def get_job_status(job_id: str) -> str:
    """Poll a background run_leads / scrape_maps job started on the HTTP server."""
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
    """Fetch website text/emails for pending domains (free, no Maps spend)."""
    _ensure_repo_cwd()
    from gmscraper import enrich_site

    store = _store()
    store.queue_sites()
    domains = store.pending_sites(limit=limit or None)
    res = enrich_site.run(store, domains, workers=workers)
    return _json({"result": res, "stats": store.stats()})


@mcp.tool(
    annotations=ToolAnnotations(
        title="Classify leads against ICP",
        readOnlyHint=False,
        openWorldHint=True,
    )
)
def classify_leads(icp: str = "", vertical: str = "", workers: int = 0) -> str:
    """LLM-classify businesses against an ICP (LLM cost only; not Maps)."""
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
    )
    return _json({"result": res, "stats": store.stats(), "llm_spend": llm.spend_line()})


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
    i_approve_spend: bool = True,
    approval_id: str = "",
    workers: int = 0,
) -> str:
    """Extract owner names from site text. Website-only is free; Apify fallback is paid.

    If use_paid_fallback=true, pass approval_id from plan_leads/estimate_cost. No auth.
    """
    _ensure_repo_cwd()
    from gmscraper import owner
    from gmscraper.config import settings
    from gmscraper.llm import default_workers
    from gmscraper.websearch import make_backend

    backend = None
    if use_paid_fallback:
        if not approval_id:
            raise ValueError(
                "Paid owner fallback needs an approval_id from plan_leads/estimate_cost."
            )
        require_spend_approval(approval_id=approval_id, i_approve_spend=True)
        backend = make_backend(settings, "")
        mark_used(approval_id)

    store = _store()
    llm = _llm()
    res = owner.run(store, llm, backend, workers=workers or default_workers(llm))
    return _json({"result": res, "stats": store.stats(), "llm_spend": llm.spend_line()})


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
    icp_only: bool = True,
) -> str:
    """Write the current DB leads to a CSV (free)."""
    _ensure_repo_cwd()
    from gmscraper import export

    out = Path(out_path) if out_path else ROOT / "data" / "outputs" / "leads-export.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    state_list = [s.strip().upper() for s in states.split(",") if s.strip()] or None
    n = export.run(
        _store(),
        str(out),
        icp_only=icp_only,
        with_owner=with_owner,
        with_email=with_email,
        min_rating=min_rating,
        min_reviews=min_reviews,
        states=state_list,
    )
    return _json({"leads": n, "csv": str(out)})


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
