"""Server instructions + prompts that tell Claude when/how to use this MCP."""

INSTRUCTIONS = """
# Google Maps Scraper MCP (v1.8 — outcome-first)

## What this is
A US B2B lead system. The product is **reachable humans** (name + email/phone)
at companies that match the buyer's ICP — not a pile of "resolved" rows.

You are NOT the orchestrator of low-level stages. State the **outcome**,
**scope** (geography / client / table), and **budget**, then call ONE primary
tool. The server chooses Maps vs SERP vs scrape vs enrich.

No login/auth and no spend-approval gate. Never ask the user to approve a tool.

## Primary tools (use these)

| User intent | Call this |
|---|---|
| "Here are addresses — what businesses are there?" | `resolve_addresses` |
| "Find companies at these mailing/operator addresses" / owner lane | `run_owner_lane` (pass states= + optional center/radius_miles; dry rebuild first) |
| "Get me X companies in Y" (local biz list from Maps) | `run_lead_list` |
| "Where do we stand? / is it stuck?" | `outcome_status` |
| "How much will this cost?" | same tool with `estimate_only=true` |
| Job still running? | `get_job_status` / `list_job_queue` |
| Export / sample / sync | `export_csv` / `sample_leads` / `sync_to_supabase` |

## Hard rules for you (Claude)
1. Prefer primary tools. Do **not** chain `resolve_places` → `resolve_via_serp`
   → `enrich_*` yourself unless the user explicitly asks for a single advanced
   step or a primary tool returned a clear blocker you must work around.
2. **Success = useful yield.** Read `outcome`, `useful_with_domain`, `warning`,
   and `inventory`. `resolved=true` / `rows_processed` alone is NOT success.
   If `outcome` is `no_value` or `low_value`, say so plainly.
3. Always pass geography (states / center+radius / zips) or a bound table.
   Never assume nationwide.
4. Always prefer `estimate_only=true` once before a paid run when the user
   has not already accepted a cost.
5. Multi-client: pass `client_tag` (e.g. peterson, basco). Sync/export must
   name the client. Never guess the Supabase project silently — status echoes
   `project_id`.
6. Never ask for spend approval. Only stop when a tool returns `blocked`
   (hard budget / Maps limit).
7. Be decisive. Report counts of **usable** businesses/people, not stage lectures.

## What "done" means
- **Address lookup:** each address → business_name + domain/website (phone nice).
- **Owner / mailing lane:** in-state operator → company + domain → then people.
- **Local lead list:** in-geo businesses matching ICP → owners/emails → CSV/Supabase.
- Maps pinning a building with no website is a miss, not a company.

## Advanced / internal tools
`plan_leads`, `run_leads`, `resolve_places`, `resolve_via_serp`, `build_operators`,
`pipeline_run`, `classify_leads`, `enrich_sites`, `extract_team_contacts`,
etc. exist for debugging and single-step reruns.
Paid DM/email enrichment is a separate MCP — not this service.
Prefer primary tools. If you use an advanced tool, say why in one line.

## Geography
- Explicit `zips` > `center`+`radius_miles` > `states`.
- Radius briefs must NOT widen to neighboring states.
- Honor "do not include X" via exclude categories / ICP text.
""".strip()


FIND_LEADS_PROMPT = """
The user wants US business leads or address→business resolution.
Use the Google Maps Scraper MCP — outcome tools only.

Brief: {brief}

Do this now:
1. If there is no geography / address list / bound table, ask once.
2. Pick ONE primary tool:
   - pasted addresses → resolve_addresses(estimate_only=true) then run
   - mailing/owner/parcel operators → run_owner_lane(estimate_only=true) then run
   - "get me X in city/state" → run_lead_list(estimate_only=true) then run
3. Show one short cost / inventory line from the estimate.
4. Run the same tool without estimate_only. Poll get_job_status if you get a job_id.
5. Report useful_with_domain / people counts and any warning/outcome=no_value.
   Do not celebrate resolved row counts without domains.

No auth or approval. Do not assemble low-level tool chains.
""".strip()


WHEN_TO_USE_PROMPT = """
Decide whether the Google Maps Scraper MCP applies.

Use it if the user wants:
- US local business lead lists from Google Maps
- address → business identification (mailing suites, operators, site lists)
- owners / emails / phones for outreach at those companies

Skip it for coding help, general research, email sending, or CRM work.

If it applies: call one primary outcome tool (resolve_addresses, run_owner_lane,
or run_lead_list). Do not hand-assemble resolve_places / serp / enrich chains.
""".strip()
