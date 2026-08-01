import './App.css'

function App() {
  const pipelineSteps = [
    {
      stage: '1) Plan',
      command: 'python -m gmscraper plan "<brief>" --save plans/target.json',
      purpose: 'Convert a plain-English target brief into categories, ICP, and cost estimate.',
    },
    {
      stage: '2) Scrape',
      command: 'python -m gmscraper scrape --plan plans/target.json',
      purpose: 'Collect Google Maps listings through RapidAPI Maps Data (zip x category grid).',
    },
    {
      stage: '3) Enrich',
      command: 'python -m gmscraper enrich --plan plans/target.json',
      purpose: 'Fetch company websites and extract candidate business emails.',
    },
    {
      stage: '4) Classify + Owners',
      command:
        'python -m gmscraper classify --plan plans/target.json && python -m gmscraper owners --plan plans/target.json --fallback',
      purpose: 'Score ICP fit with LLM and find owner names (optional web-search fallback).',
    },
    {
      stage: '5) Export',
      command:
        'python -m gmscraper export --out out/leads.csv --with-email --with-owner --min-rating 4.0',
      purpose: 'Produce qualified CSV leads for outreach.',
    },
  ]

  const projectAudit = [
    {
      fact: 'Actual project purpose',
      detail:
        'A lead-generation pipeline that scrapes Google Maps listings, enriches websites, classifies ICP fit, and exports CSV.',
    },
    {
      fact: 'Primary data store',
      detail: 'SQLite checkpoint database (leads.db) with resumable job stages.',
    },
    {
      fact: 'Main paid input source',
      detail: 'RapidAPI Maps Data requests for search coverage (zip x category).',
    },
    {
      fact: 'Current gap',
      detail:
        'This branch currently hosts only a UI shell; the Python scraper code is not yet wired into this frontend.',
    },
  ]

  const spendGuardrail = [
    'No paid call without explicit approval.',
    'Always estimate cost before execution.',
    'Applies to Apify and any paid external API.',
    'If estimate is unclear, call is blocked until clarified.',
  ]

  const runbookQueries = [
    'python -m gmscraper estimate --categories "dentist,orthodontist" --states CA --plan ultra',
    'python -m gmscraper probe --zip 10001 --category "dental clinic"',
    'python -m gmscraper stats --all',
    'railway logs --service railway-ui --lines 200 --json',
  ]

  const paidApis = [
    {
      name: 'RapidAPI Maps Data',
      use: 'Google Maps listing fetches during scrape stage.',
      estimateHint: 'Estimated by total request count from plan/estimate command.',
    },
    {
      name: 'OpenAI-compatible model',
      use: 'Brief planning + ICP classification + owner extraction.',
      estimateHint: 'Estimate by model choice and number of records sent to LLM stages.',
    },
    {
      name: 'Apify SERP fallback (optional)',
      use: 'Owner lookup fallback when website evidence is insufficient.',
      estimateHint: 'Estimate by fallback search count; only run with explicit approval.',
    },
  ]

  return (
    <main className="app">
      <header className="hero">
        <p className="eyebrow">Google Maps Scraper Control Panel</p>
        <h1>UI aligned to the real project: scrape, enrich, qualify, export</h1>
        <p className="subtitle">
          Repository audit summary: this project is a lead pipeline, not just a
          static Railway site. The next step is wiring these flows to a backend
          service that runs gmscraper commands.
        </p>
      </header>

      <section className="grid">
        <article className="card">
          <h2>Audit findings</h2>
          <ul className="query-list">
            {projectAudit.map((item) => (
              <li key={item.fact}>
                <h3>{item.fact}</h3>
                <p>{item.detail}</p>
              </li>
            ))}
          </ul>
        </article>

        <article className="card">
          <h2>Pipeline runbook (target UI behavior)</h2>
          <ul className="query-list">
            {pipelineSteps.map((item) => (
              <li key={item.stage}>
                <h3>{item.stage}</h3>
                <code>{item.command}</code>
                <p>{item.purpose}</p>
              </li>
            ))}
          </ul>
        </article>

        <article className="card">
          <h2>What to query while operating</h2>
          <ul className="query-list">
            {runbookQueries.map((command) => (
              <li key={command}>
                <code>{command}</code>
              </li>
            ))}
          </ul>
        </article>

        <article className="card">
          <h2>Spend approval gate (enforced)</h2>
          <p className="callout">
            Every paid call must show an estimate first and requires explicit
            user approval.
          </p>
          <ul className="query-list">
            {spendGuardrail.map((rule) => (
              <li key={rule}>
                <p>{rule}</p>
              </li>
            ))}
          </ul>
        </article>

        <article className="card">
          <h2>Paid API touchpoints</h2>
          <ul className="query-list">
            {paidApis.map((api) => (
              <li key={api.name}>
                <h3>{api.name}</h3>
                <p>{api.use}</p>
                <p>
                  <strong>Cost estimate rule:</strong> {api.estimateHint}
                </p>
              </li>
            ))}
          </ul>
        </article>
      </section>
    </main>
  )
}

export default App
