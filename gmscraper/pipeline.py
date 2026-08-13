"""Generic resolve → enrich → extract pipeline (maps + site crawl).

Paid DM/email enrichment lives in a separate service.
Every stage is optional. Stages skip work that already exists. Counts only.
"""

from __future__ import annotations

import json
from typing import Any

from . import enrich_site, resolve_places, source_binding as sb, team_contacts
from .config import settings
from .llm import make_llm
from .store import Store


def _parse_stages(stages: str) -> list[str]:
    allowed = {"resolve", "enrich", "extract"}
    parts = [p.strip().lower() for p in (stages or "").split(",") if p.strip()]
    out = [p for p in parts if p in allowed]
    if not out:
        raise ValueError(
            f"stages must include one of {sorted(allowed)}; got {stages!r}"
        )
    return out


def run(
    store: Store,
    *,
    schema: str,
    table: str,
    key_column: str,
    stages: str = "resolve,enrich,extract",
    address_column: str = "",
    name_column: str = "",
    city_column: str = "",
    where: str = "",
    order_by: str = "",
    limit: int = 0,
    use_llm: bool = True,
    estimate_only: bool = False,
    project_id: str = "",
    strategy: str = "address",
    min_confidence: float = 0.6,
    target_titles: str = "",
    workers: int = 8,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    stage_list = _parse_stages(stages)
    out: dict[str, Any] = {
        "stages": stage_list,
        "estimate_only": bool(estimate_only),
        "per_stage": {},
        "cumulative_cost_usd": 0.0,
        "note": (
            "Paid DM/email enrichment is a separate MCP. "
            "After extract, export domains and enrich there."
        ),
    }

    def _tick(stage: str, **extra: Any) -> None:
        if on_progress is None:
            return
        try:
            on_progress(stage=stage, **extra)
        except Exception:  # noqa: BLE001
            pass

    _tick(
        "pipeline",
        done=0,
        total=len(stage_list),
        jobs_total=len(stage_list),
        jobs_done=0,
        jobs_pending=len(stage_list),
        businesses_found=0,
        stages=stage_list,
    )

    binding = None
    if any(s in stage_list for s in ("resolve", "enrich", "extract")):
        binding = sb.resolve_binding(
            project_id=project_id,
            schema=schema,
            table=table,
            key_column=key_column,
            address_column=address_column,
            name_column=name_column,
            city_column=city_column,
            where=where,
            order_by=order_by,
        )
        if "resolve" in stage_list:
            sb.validate_binding(binding)
            sb.ensure_writeback_columns(binding)

    # ---- resolve ----
    if "resolve" in stage_list:
        _tick("resolve", done=0, total=1, jobs_total=len(stage_list), jobs_done=0)
        res = resolve_places.run(
            schema=schema,
            table=table,
            key_column=key_column,
            address_column=address_column,
            name_column=name_column,
            city_column=city_column,
            where=where,
            order_by=order_by,
            limit=limit,
            strategy=strategy,
            min_confidence=min_confidence,
            workers=workers,
            estimate_only=estimate_only,
            project_id=project_id,
        )
        out["per_stage"]["resolve"] = {
            k: res.get(k)
            for k in (
                "pending_rows", "requests", "estimated_overage_usd", "blocked",
                "started", "resolved", "low_confidence", "no_match", "errors", "rows",
            )
            if k in res
        }
        cost = float(res.get("estimated_overage_usd") or 0)
        out["cumulative_cost_usd"] = round(out["cumulative_cost_usd"] + cost, 4)
        done_stages = 1
        _tick(
            "resolve",
            done=1,
            total=1,
            jobs_total=len(stage_list),
            jobs_done=done_stages,
            jobs_pending=max(0, len(stage_list) - done_stages),
            businesses_found=int(res.get("resolved") or 0),
        )
        if estimate_only or res.get("blocked"):
            out["started"] = False
            out["blocked"] = bool(res.get("blocked"))
            return out

    if estimate_only:
        # Still report remaining stage intents without running them.
        for s in stage_list:
            if s != "resolve":
                out["per_stage"].setdefault(s, {"skipped": True, "reason": "estimate_only"})
        out["started"] = False
        return out

    # Domains available after resolve (or already on the table).
    domains: list[str] = []
    if binding and any(s in stage_list for s in ("enrich", "extract")):
        rows = sb.rpc(
            binding,
            "pp_select_rows",
            {
                "p_schema": binding.schema,
                "p_table": binding.table,
                "p_columns": [binding.key_column, binding.domain_column, "business_name"],
                "p_where": (
                    f"{binding.domain_column} IS NOT NULL AND {binding.domain_column} != ''"
                    + (f" AND ({binding.where})" if binding.where else "")
                ),
                "p_order_by": binding.order_by or binding.key_column,
                "p_limit": int(limit) if limit and limit > 0 else 100000,
                "p_offset": 0,
            },
        )
        if isinstance(rows, list):
            seen: set[str] = set()
            for r in rows:
                d = str((r or {}).get(binding.domain_column) or "").strip().lower()
                if d and d not in seen:
                    seen.add(d)
                    domains.append(d)

    # ---- enrich (site crawl into local SQLite) ----
    stages_done = sum(1 for s in ("resolve",) if s in stage_list and s in out["per_stage"])
    if "enrich" in stage_list:
        _tick(
            "enrich",
            done=0,
            total=max(len(domains), 1),
            jobs_total=len(stage_list),
            jobs_done=stages_done,
            jobs_pending=max(0, len(stage_list) - stages_done),
            businesses_found=len(domains),
        )
        if not domains:
            out["per_stage"]["enrich"] = {"domains": 0, "skipped": True, "reason": "no_domains"}
        else:
            # Ensure local businesses exist so queue_sites / crawl can run.
            with store.conn as c:
                for d in domains:
                    c.execute(
                        """INSERT INTO businesses (place_id, name, domain, website, source)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(place_id) DO UPDATE SET
                             domain=excluded.domain,
                             website=COALESCE(NULLIF(excluded.website,''), businesses.website)""",
                        (f"pipe:{d}", d, d, f"https://{d}", "pipeline"),
                    )
            store.queue_sites()
            pending = [s for s in store.pending_sites() if s in domains]
            if pending:
                enrich_res = enrich_site.run(
                    store,
                    pending,
                    workers=max(1, min(int(workers or 8), 3)),
                    on_progress=lambda **p: _tick("enrich", **p),
                )
            else:
                enrich_res = {"fetched": 0, "skipped": len(domains)}
            # Always try team/about backfill for these domains.
            team_res = enrich_site.crawl_team_pages(
                store, domains=domains, workers=max(1, min(int(workers or 8), 3)), force=False
            )
            out["per_stage"]["enrich"] = {
                "domains": len(domains),
                "site_fetch": enrich_res,
                "team_crawl": team_res,
                "cost_usd": 0.0,
            }
        stages_done += 1
        _tick(
            "enrich",
            done=1,
            total=1,
            jobs_total=len(stage_list),
            jobs_done=stages_done,
            jobs_pending=max(0, len(stage_list) - stages_done),
            businesses_found=len(domains),
        )

    # ---- extract people ----
    if "extract" in stage_list:
        _tick(
            "extract",
            jobs_total=len(stage_list),
            jobs_done=stages_done,
            jobs_pending=max(0, len(stage_list) - stages_done),
            businesses_found=len(domains),
        )
        if not domains:
            out["per_stage"]["extract"] = {"domains": 0, "skipped": True, "reason": "no_domains"}
        else:
            llm = make_llm(settings) if use_llm else None
            titles = [t.strip() for t in (target_titles or "").split(",") if t.strip()]
            ext = team_contacts.run(
                store,
                domains=domains,
                workers=workers,
                use_llm=use_llm,
                llm=llm,
                target_titles=titles or None,
            )
            out["per_stage"]["extract"] = {**ext, "use_llm": use_llm, "cost_usd": 0.0}
        stages_done += 1
        _tick(
            "extract",
            done=1,
            total=1,
            jobs_total=len(stage_list),
            jobs_done=stages_done,
            jobs_pending=max(0, len(stage_list) - stages_done),
            businesses_found=len(domains),
        )


    out["started"] = True
    out["domains"] = len(domains)
    return out
