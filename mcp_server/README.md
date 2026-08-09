# Google Maps Scraper MCP Server

Lets Claude (Desktop, Code, Cursor, **and Claude web**) run the full lead pipeline. **No login / OAuth / API-key auth on the connector.**

Claude receives a built-in playbook via server `instructions`, resource `gmscraper://playbook`, and prompts `find_leads` / `when_to_use` so it knows when to use this MCP and the exact plan → run flow.

## Claude web (claude.ai)

Live Railway URL (Streamable HTTP):

```
https://google-maps-mcp-production-88a3.up.railway.app/mcp
```

In Claude:

1. **Settings → Connectors → Add custom connector**
2. Paste the URL above
3. Leave auth empty (none)
4. Enable the connector in the chat, then ask for leads

Long scrapes run in the background — Claude should poll `get_job_status`.

Set these Railway env vars on service `google-maps-mcp` for paid runs:

- `RAPIDAPI_KEY`
- `OPENAI_API_KEY` (or `LLM_PROVIDER=ollama` + reachable Ollama)
- optional: `APIFY_TOKEN`, `MAPS_PLAN`, `MAPS_QUOTA_RESET_DAY`

## Tools

| Tool | Paid? | Notes |
|---|---|---|
| `health` | no | Keys, Maps plan, paths |
| `list_categories` | no | Built-in verticals |
| `ensure_zips` | no | Build ZIP list once |
| `pipeline_stats` | no | Local SQLite counts |
| `plan_leads` | LLM only | Brief → plan + `plan_path` + cost |
| `estimate_cost` | no | Vertical/states cost + `plan_path` |
| `probe_maps` | 1 Maps req | Schema check |
| `run_leads` | **yes** | Full pipeline; `plan_path` optional (uses latest plan) |
| `scrape_maps` | **yes** | Scrape stage only; no spend approval |
| `enrich_sites` | no | Website fetch (+ about/team pages, page_type tagged) |
| `crawl_team_pages` | no | Backfill team/about crawl on already-fetched sites |
| `extract_team_contacts` | optional LLM | Person+title → local `contacts` |
| `classify_leads` | LLM | ICP filter |
| `find_owners` | optional Apify | Team contacts + owner; paid fallback needs no approval |
| `estimate_resolve_places` | no | Read-only cost estimate for resolve_places |
| `resolve_places` | **yes** | Address/name → business writeback (no approval) |
| `estimate_apify_contact_crawl` | no | Read-only Apify cost estimate |
| `apify_contact_crawl` | Apify | Website contact crawl (no approval) |
| `parse_contacts_openai` | LLM | Parse Apify raw → people in `gc.*` (no approval) |
| `enrich_waterfall` | paid vendors | apify→AI Ark→getleads→LeadMagic (`max_tier` default leadmagic) → `gc.*` |
| `fullenrich_find_email` | FullEnrich | Last-tier email only |
| `export_csv` | no | CSV text in response (`total_matching`, `capped_at`, `csv`); optional `out_path` |
| `query_leads` | no | Paginated rows (`page` / `page_size` max 50) |
| `leads_summary` | no | Counts only (ICP / email / city / category) |
| `sample_leads` | no | Inline QA sample (random by default) |
| `sync_to_supabase` | no | Batch upsert into `maps_leads` (counts only) |
| `ingest_external_leads` | no | Insert Shovels/external rows (`source_tag`) |
| `estimate_resolve_domains` | no | Cost estimate for website lookup |
| `resolve_domains` | **yes** | Paid Maps name+city → website/domain (no approval) |
| `renormalize` | no | Re-map stored JSON |
| `remote_*` | Railway | Optional job API via `GMAPS_API_BASE` |

## Flow

1. Claude calls `plan_leads("HVAC companies in Ohio with owners and emails")`.
2. You see the estimate (requests + overage).
3. Claude calls `run_leads(plan_path=...)` — or `run_leads()` with no ids to use the latest plan.

No connector auth and no spend-approval gate.

## Setup

```bash
cd /path/to/googlemaps-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill RAPIDAPI_KEY, OPENAI_API_KEY, etc.
```

## Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "google-maps-scraper": {
      "command": "/path/to/googlemaps-scraper/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/googlemaps-scraper",
      "env": {
        "RAPIDAPI_KEY": "your-key",
        "OPENAI_API_KEY": "your-key",
        "MAPS_PLAN": "ultra",
        "GMAPS_API_BASE": "https://google-maps-scraper-production-41db.up.railway.app"
      }
    }
  }
}
```

Or rely on a local `.env` in the repo (loaded by `gmscraper.config`) and omit secrets from the MCP config.

## Claude Code / Cursor

```bash
# Claude Code
claude mcp add google-maps-scraper -- \
  /path/to/googlemaps-scraper/.venv/bin/python -m mcp_server

# Or Cursor MCP settings (stdio):
# command: .venv/bin/python
# args: ["-m", "mcp_server"]
# cwd: repo root
```

Example Cursor snippet (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "google-maps-scraper": {
      "command": "python3",
      "args": ["-m", "mcp_server"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

## Manual smoke test

```bash
python -m mcp_server
# then in another terminal with MCP inspector, or:
python - <<'PY'
import asyncio
from mcp_server.server import mcp

async def main():
    tools = await mcp.list_tools()
    print([t.name for t in tools])

asyncio.run(main())
PY
```
