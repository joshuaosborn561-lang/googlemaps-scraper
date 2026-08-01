import { useMemo, useState } from 'react'
import './App.css'

type ParsedPlan = {
  categories: string[]
  states: string[]
  minRating: number
  minReviews: number
  needsOwners: boolean
  needsEmail: boolean
  usesFallback: boolean
  leadTarget: number
}

const STATE_CODES = [
  'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DC', 'DE', 'FL', 'GA', 'HI', 'IA',
  'ID', 'IL', 'IN', 'KS', 'KY', 'LA', 'MA', 'MD', 'ME', 'MI', 'MN', 'MO', 'MS',
  'MT', 'NC', 'ND', 'NE', 'NH', 'NJ', 'NM', 'NV', 'NY', 'OH', 'OK', 'OR', 'PA',
  'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VA', 'VT', 'WA', 'WI', 'WV', 'WY',
]

const CATEGORY_HINTS: Array<{ token: string; categories: string[] }> = [
  { token: 'roof', categories: ['roofing contractor', 'roof repair'] },
  { token: 'hvac', categories: ['hvac contractor', 'air conditioning contractor'] },
  { token: 'dental', categories: ['dentist', 'dental clinic'] },
  { token: 'chiro', categories: ['chiropractor'] },
  { token: 'medspa', categories: ['medical spa'] },
  { token: 'gym', categories: ['gym', 'fitness center'] },
  { token: 'plumb', categories: ['plumber'] },
  { token: 'funeral', categories: ['funeral home', 'cremation service'] },
  { token: 'lawyer', categories: ['law firm', 'personal injury attorney'] },
  { token: 'auto', categories: ['auto repair shop'] },
]

function parsePrompt(prompt: string): ParsedPlan {
  const lower = prompt.toLowerCase()
  const matchedCategories = CATEGORY_HINTS.flatMap((entry) =>
    lower.includes(entry.token) ? entry.categories : [],
  )
  const categories = [...new Set(matchedCategories)].slice(0, 6)
  const stateMatches = STATE_CODES.filter((code) =>
    new RegExp(`\\b${code.toLowerCase()}\\b`).test(lower),
  )
  const ratingMatch = lower.match(/(\d(?:\.\d)?)\+?\s*star/)
  const reviewsMatch = lower.match(/(\d+)\+?\s*review/)
  const leadsMatch = lower.match(/(\d{2,6})\s*(lead|prospect|record)/)

  return {
    categories: categories.length > 0 ? categories : ['local business'],
    states: stateMatches.length > 0 ? stateMatches : ['US'],
    minRating: ratingMatch ? Number(ratingMatch[1]) : 4.0,
    minReviews: reviewsMatch ? Number(reviewsMatch[1]) : 15,
    needsOwners: /(owner|founder|ceo)/.test(lower),
    needsEmail: /(email|inbox|contact)/.test(lower) || true,
    usesFallback: /(fallback|web search|apify)/.test(lower) || /(owner)/.test(lower),
    leadTarget: leadsMatch ? Number(leadsMatch[1]) : 500,
  }
}

function formatUsd(value: number): string {
  return `$${value.toFixed(2)}`
}

function App() {
  const [prompt, setPrompt] = useState(
    'Find 1200 dental and orthodontic clinics in CA and AZ with 4.2+ stars, at least 30 reviews, include owner and email.',
  )
  const [approvedMaps, setApprovedMaps] = useState(false)
  const [approvedLlm, setApprovedLlm] = useState(false)
  const [approvedApify, setApprovedApify] = useState(false)
  const [activity, setActivity] = useState<string[]>([
    'Ready: enter a natural-language scrape brief.',
  ])

  const parsed = useMemo(() => parsePrompt(prompt), [prompt])

  const estimates = useMemo(() => {
    const zipEstimate = Math.max(120, parsed.states.length * 340)
    const requestEstimate = zipEstimate * parsed.categories.length
    const mapsCost = requestEstimate * 0.0005
    const llmRecords = Math.min(parsed.leadTarget, requestEstimate * 0.32)
    const llmCost = llmRecords * 0.002
    const apifySearches = parsed.usesFallback ? Math.ceil(llmRecords * 0.18) : 0
    const apifyCost = apifySearches * 0.0005

    return {
      zipEstimate,
      requestEstimate,
      mapsCost,
      llmRecords,
      llmCost,
      apifySearches,
      apifyCost,
      total: mapsCost + llmCost + apifyCost,
    }
  }, [parsed])

  const planFile = 'plans/ui-generated.json'
  const commandPlan = `python -m gmscraper plan "${prompt.replace(/"/g, "'")}" --save ${planFile}`
  const commandRun = `python -m gmscraper run --plan ${planFile} --out out/leads.csv --yes`
  const commandExport =
    'python -m gmscraper export --out out/leads.csv --with-email --with-owner --min-rating 4.0'

  const requiresApifyApproval = parsed.usesFallback
  const canQueue =
    approvedMaps &&
    approvedLlm &&
    (!requiresApifyApproval || approvedApify) &&
    prompt.trim().length > 20

  function generatePlan() {
    setActivity((prev) => [
      `Plan generated for ${parsed.categories.join(', ')} in ${parsed.states.join(', ')}.`,
      `Estimated ${estimates.requestEstimate.toLocaleString()} Maps requests (${formatUsd(estimates.mapsCost)}).`,
      ...prev,
    ])
  }

  function queueRun() {
    if (!canQueue) return
    setActivity((prev) => [
      `Queued run with approval gate passed. Expected total spend: ${formatUsd(estimates.total)}.`,
      `Run command: ${commandRun}`,
      ...prev,
    ])
  }

  return (
    <main className="app">
      <header className="hero">
        <p className="eyebrow">Google Maps Scraper</p>
        <h1>From natural-language prompt to scrape run plan</h1>
        <p className="subtitle">
          Describe the lead target in plain English. The UI parses scope, estimates
          spend, and blocks paid actions until explicit approval is checked.
        </p>
      </header>

      <section className="layout">
        <article className="card">
          <h2>1) Prompt</h2>
          <label htmlFor="brief">What should we scrape?</label>
          <textarea
            id="brief"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            rows={6}
          />
          <div className="actions">
            <button type="button" onClick={generatePlan}>
              Generate plan
            </button>
          </div>
          <p className="hint">No spend happens in this step.</p>
        </article>

        <article className="card">
          <h2>2) Parsed plan</h2>
          <ul className="kv">
            <li><span>Categories</span><strong>{parsed.categories.join(', ')}</strong></li>
            <li><span>States</span><strong>{parsed.states.join(', ')}</strong></li>
            <li><span>Min rating</span><strong>{parsed.minRating.toFixed(1)}+</strong></li>
            <li><span>Min reviews</span><strong>{parsed.minReviews}+</strong></li>
            <li><span>Lead target</span><strong>{parsed.leadTarget.toLocaleString()}</strong></li>
            <li><span>Owner required</span><strong>{parsed.needsOwners ? 'Yes' : 'No'}</strong></li>
            <li><span>Email required</span><strong>{parsed.needsEmail ? 'Yes' : 'No'}</strong></li>
          </ul>
          <code>{commandPlan}</code>
        </article>

        <article className="card">
          <h2>3) Cost estimate</h2>
          <ul className="kv">
            <li><span>ZIPs scanned (est.)</span><strong>{estimates.zipEstimate.toLocaleString()}</strong></li>
            <li><span>Maps requests (est.)</span><strong>{estimates.requestEstimate.toLocaleString()}</strong></li>
            <li><span>RapidAPI Maps (est.)</span><strong>{formatUsd(estimates.mapsCost)}</strong></li>
            <li><span>LLM records (est.)</span><strong>{Math.round(estimates.llmRecords).toLocaleString()}</strong></li>
            <li><span>LLM cost (est.)</span><strong>{formatUsd(estimates.llmCost)}</strong></li>
            <li><span>Apify fallback searches</span><strong>{estimates.apifySearches.toLocaleString()}</strong></li>
            <li><span>Apify cost (est.)</span><strong>{formatUsd(estimates.apifyCost)}</strong></li>
            <li className="total"><span>Total projected spend</span><strong>{formatUsd(estimates.total)}</strong></li>
          </ul>
        </article>

        <article className="card">
          <h2>4) Approval gate</h2>
          <p className="callout">Paid actions remain blocked until each required approval is checked.</p>
          <label className="check">
            <input type="checkbox" checked={approvedMaps} onChange={(e) => setApprovedMaps(e.target.checked)} />
            Approve RapidAPI Maps spend ({formatUsd(estimates.mapsCost)})
          </label>
          <label className="check">
            <input type="checkbox" checked={approvedLlm} onChange={(e) => setApprovedLlm(e.target.checked)} />
            Approve LLM spend ({formatUsd(estimates.llmCost)})
          </label>
          {requiresApifyApproval && (
            <label className="check">
              <input type="checkbox" checked={approvedApify} onChange={(e) => setApprovedApify(e.target.checked)} />
              Approve Apify fallback spend ({formatUsd(estimates.apifyCost)})
            </label>
          )}
          <div className="actions">
            <button type="button" onClick={queueRun} disabled={!canQueue}>
              Queue scrape run
            </button>
          </div>
        </article>

        <article className="card span-2">
          <h2>5) Run commands</h2>
          <code>{commandRun}</code>
          <code>{commandExport}</code>
          <p className="hint">
            Next backend step: wire these actions to an API route that executes gmscraper and streams logs.
          </p>
        </article>

        <article className="card span-2">
          <h2>Activity log</h2>
          <ul className="log">
            {activity.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </article>
      </section>
    </main>
  )
}

export default App
