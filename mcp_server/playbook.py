"""Server instructions + prompts that tell Claude when/how to use this MCP."""

INSTRUCTIONS = """
# Google Maps Scraper MCP

## What this is
A US local-business lead generator. The user describes who they want
(e.g. "HVAC companies in Ohio with owner names and emails") and this MCP
turns that into a downloadable CSV of businesses from Google Maps.

Pipeline: plan → scrape Google Maps → enrich websites/emails → classify ICP fit
→ find owners → export CSV.

No login/auth on this connector. It is already connected when tools are available.

## When to use this MCP
Use these tools when the user wants:
- local business leads / prospect lists
- Google Maps scrapes for a niche + city/state
- owner names, emails, phones for outreach
- "get me X companies in Y"
- funeral homes, HVAC, dentists, med spas, gyms, lawyers, etc. in a region

Do NOT use this for:
- general web search, coding, email sending, CRM updates
- non-US markets (this pipeline is US ZIP / state based)
- one-off questions that don't need a lead list

## Default flow (every time)
1. If no US state/region/radius is named → ASK which state(s) or city+radius.
   Never assume nationwide.
2. Call `plan_leads` with their brief. For radius briefs ("within 150 miles of
   Dallas"), pass `center` + `radius_miles` when you already know them, or let
   the planner extract them. Prefer explicit `zips` when you have a ZIP list.
3. Show a short cost summary: requests, estimated overage $, zip_count, region.
4. Cost rules:
   - estimated overage ≤ $5 OR $0 inside quota → proceed to run (tell them the number)
   - estimated overage > $5 → ask once for a yes, then run
   - BLOCKED → stop; tell them to upgrade MAPS_PLAN
5. Call `run_leads(approval_id=...)` with the id from step 2.
6. On the Railway/HTTP server, runs are background jobs. Poll `get_job_status`
   until completed/failed. Then QA with `sample_leads`, and sync with
   `sync_to_supabase(run_label=...)` so results are queryable in SQL.
7. Deliver the outcome: how many leads, sample quality notes, Supabase table /
   run_label, email/owner coverage. Do not dump flags or stage lectures.

## Geography (important)
- Explicit `zips` beats `center`+`radius_miles`, which beats `states`.
- Radius briefs must NOT widen to neighboring states (DFW ≠ TX+OK).
- Honor "do not include X" via `exclude_categories` — never scrape competitor niches
  the brief ruled out (e.g. roofing for a GC prospecting list).
- Exports include `latitude`, `longitude`, and `source_zip`.

## Tool cheat sheet
| User intent | Tool |
|---|---|
| "get me X in Y" / full list | `plan_leads` → `run_leads` |
| "how much would X cost?" | `plan_leads` or `estimate_cost` (stop before run) |
| "what verticals exist?" | `list_categories` |
| "is the API working?" | `probe_maps` (1 paid Maps request) |
| "re-run classify only" / tighten ICP | `classify_leads` (no re-scrape) |
| "pull emails from sites" | `enrich_sites` |
| "find owners" | `find_owners` (Apify fallback only if they want paid web lookup) |
| "export what we have" | `export_csv` (CSV text in response; capped 5000; clean=true) |
| "browse / page through leads" | `query_leads` (page_size max 50) |
| "how many leads / breakdown" | `leads_summary` (counts only) |
| "show me some rows" / QA | `sample_leads` (random sample; never rely on disk path alone) |
| "put results in Supabase / SQL" | `sync_to_supabase` (counts only; use run_label) |
| "load Shovels / external CSV rows" | `ingest_external_leads` (set source_tag; counts only) |
| "these rows have no website" | `estimate_resolve_domains` → `resolve_domains` → `enrich_sites` |
| "classify only shovels / re-run" | `classify_leads(source=…, force=…, limit=…)` |
| "job status?" | `get_job_status` / `list_background_jobs` |
| "history on the website?" | `list_remote_jobs` / `download_remote_csv` |
| config check | `health` |

## Hard rules
- Always `plan_leads` (or `estimate_cost`) before any paid scrape/`run_leads`.
- Prefer state-level pilots for a new vertical before offering nationwide.
- Do not invent an approval_id — only use one returned by plan/estimate.
- Do not re-scrape to fix field mapping; use `renormalize` after alias fixes.
- Maps scrape costs money; enrich/classify/export (without Apify fallback) do not.
- Be decisive. User wants the CSV, not a menu of options.
""".strip()


FIND_LEADS_PROMPT = """
The user wants local US business leads. Use the Google Maps Scraper MCP.

Brief: {brief}

Do this now:
1. If the brief has no US state/region/radius, ask which state(s) or city+radius —
   do not run nationwide blindly.
2. Call plan_leads with the brief. For "within N miles of City", pass center +
   radius_miles when known. Pass exclude_categories for "do not include …".
   Prefer explicit zips when you already have a ZIP list.
3. Show one short cost line (requests + est. overage + zip_count + region).
4. If overage ≤ $5 (or $0 in quota), call run_leads with the returned approval_id.
   If overage > $5, ask once for confirmation, then run_leads.
5. If run_leads returns a job_id, poll get_job_status until done.
6. Report lead count, CSV path, and a brief sample / coverage summary.

No auth is required. Do not ask the user for API keys or connector setup.
""".strip()


WHEN_TO_USE_PROMPT = """
Decide whether the Google Maps Scraper MCP applies.

Use it if the user wants a list of US local businesses / leads from Google Maps
(niche + geography, optionally owners/emails).

Skip it if they want coding help, general research, email campaigns, or anything
outside building a local-business lead CSV.

If it applies, follow the find_leads flow: plan_leads → show cost → run_leads.
""".strip()
