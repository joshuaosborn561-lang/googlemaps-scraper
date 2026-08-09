# Google Maps Scraper UI (Railway)

For Claude chat access to the same pipeline (no spend-approval gate), use the
MCP server at [`../mcp_server/README.md`](../mcp_server/README.md).

This UI is built for:

- entering a **natural-language lead-gen prompt**
- reviewing parsed scrape scope + projected cost
- starting the run immediately (no approval checkbox)

Core workflow:

1. Plan lead campaign from a brief
2. Scrape Google Maps listings
3. Enrich websites and emails
4. Classify ICP fit and extract owner data
5. Export qualified CSV leads

## What this UI does

- Accepts a natural-language scraping brief
- Parses scope (categories, states, rating/review gates)
- Estimates paid usage (Maps, LLM, optional Apify fallback)
- Queues backend job execution (`gmscraper run`) — no spend gate
- Persists job history and allows CSV redownload by job

## Run locally

```bash
npm install
npm run dev
```

## Deploy to Railway

From this project directory:

```bash
railway up
```

Railway will guide auth/sign-up if needed, create project/service if missing, and deploy.

## Core pipeline commands (copy/paste)

```bash
python -m gmscraper plan "<brief>" --save plans/target.json
python -m gmscraper scrape --plan plans/target.json
python -m gmscraper enrich --plan plans/target.json
python -m gmscraper classify --plan plans/target.json
python -m gmscraper owners --plan plans/target.json --fallback
python -m gmscraper export --out out/leads.csv --with-email --with-owner
```

## Operating queries

```bash
python -m gmscraper estimate --categories "dentist,orthodontist" --states CA --plan ultra
python -m gmscraper probe --zip 10001 --category "dental clinic"
python -m gmscraper stats --all
railway logs --service railway-ui --lines 200 --json
```

## Cost estimate (informational)

Before paid API calls, the UI shows a written cost estimate, then starts when
you click **Start scrape**. There is no approval checkbox or spend gate.

## Supabase integration (primary persistence)

Supabase is the source of truth for history and downloads:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_INGEST_SECRET`

Tables:
- `scrape_jobs` (job history + tags + spend estimates)
- `scrape_leads` (lead rows + tags + structured fields)
- `scrape_exports` (CSV content for re-download)

API behavior:
- `GET /api/jobs` reads from Supabase (survives Railway restarts/refreshes)
- `POST /api/jobs` creates a job and starts execution
- `GET /api/jobs/:id/file` returns the CSV export
