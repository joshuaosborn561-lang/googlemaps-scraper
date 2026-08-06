# Google Maps Scraper

US local-business lead pipeline: plan → scrape Google Maps → enrich → classify → owners → CSV.

## Ways to use it

1. **Claude MCP** (recommended in chat) — say what you want; approve spend with “yes”  
   See [`mcp_server/README.md`](mcp_server/README.md)
2. **CLI** — `python -m gmscraper plan "…"` then `run`  
   See [`CLAUDE.md`](CLAUDE.md)
3. **Railway UI** — browser wizard with the same approval gate  
   See [`railway-ui/README.md`](railway-ui/README.md) · live: https://google-maps-scraper-production-41db.up.railway.app

## Quick MCP setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # RAPIDAPI_KEY, OPENAI_API_KEY, MAPS_PLAN, …
python -m mcp_server
```

Point Claude Desktop / Cursor at that process (examples in `mcp_server/`).
