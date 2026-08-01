# Google Maps Scraper UI (Railway)

This UI now reflects the repository's real target workflow and is explicitly built for:

- entering a **natural-language lead-gen prompt**
- reviewing parsed scrape scope + projected cost
- approving paid actions before run commands

Core workflow:

1. Plan lead campaign from a brief
2. Scrape Google Maps listings
3. Enrich websites and emails
4. Classify ICP fit and extract owner data
5. Export qualified CSV leads

Current state: this frontend is deployed and aligned to that flow; it now provides
a prompt-driven planning UX. The Python `gmscraper` backend still needs HTTP wiring
for one-click execution from the UI.

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

## What the audit found

- Project purpose is a **lead-generation pipeline** for US local businesses.
- Main paid source is **RapidAPI Maps Data**.
- LLM stages handle planning, ICP classification, and owner extraction.
- Export output is CSV leads with filters (email/owner/rating/reviews).

## Spend approval gate (required)

Before any paid API call (RapidAPI, OpenAI-compatible endpoints, Apify fallback), this workflow requires:

1. A written cost estimate
2. Explicit user approval
3. Only then execution

No exceptions for "small" calls.

## Railway start command used by this app

`npm start` runs:

```bash
vite preview --host 0.0.0.0 --port ${PORT:-4173}
```

That matches Railway's runtime requirements (bind to host + provided port).
