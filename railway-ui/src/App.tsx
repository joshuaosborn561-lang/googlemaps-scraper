import { useEffect, useMemo, useState } from 'react'
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

type JobRecord = {
  id: string
  prompt: string
  tags: string[]
  status: 'queued' | 'running' | 'completed' | 'failed'
  createdAt: string
  finishedAt: string | null
  estimate: {
    requestEstimate: number
    mapsCost: number
    llmCost: number
    apifyCost: number
    total: number
  }
  approvals: {
    maps: boolean
    llm: boolean
    apify: boolean
  }
  downloadUrl: string | null
  error: string | null
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
  const [step, setStep] = useState<1 | 2 | 3>(1)
  const [prompt, setPrompt] = useState(
    'Find 1200 dental and orthodontic clinics in CA and AZ with 4.2+ stars, at least 30 reviews, include owner and email.',
  )
  const [tagsInput, setTagsInput] = useState('dental, high-value, west-coast')
  const [approvedMaps, setApprovedMaps] = useState(false)
  const [approvedLlm, setApprovedLlm] = useState(false)
  const [approvedApify, setApprovedApify] = useState(false)
  const [confirmedPlan, setConfirmedPlan] = useState(false)
  const [jobs, setJobs] = useState<JobRecord[]>([])
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [activity, setActivity] = useState<string[]>(['Ready for a new lead-gen prompt.'])
  const [backendState, setBackendState] = useState('Checking backend...')

  const parsed = useMemo(() => parsePrompt(prompt), [prompt])
  const tags = useMemo(
    () =>
      tagsInput
        .split(',')
        .map((tag) => tag.trim())
        .filter((tag) => tag.length > 0),
    [tagsInput],
  )

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

  const requiresApifyApproval = parsed.usesFallback
  const canQueue =
    confirmedPlan &&
    approvedMaps &&
    approvedLlm &&
    (!requiresApifyApproval || approvedApify) &&
    prompt.trim().length > 20

  async function loadJobs(): Promise<void> {
    try {
      const response = await fetch('/api/jobs')
      if (!response.ok) throw new Error(`Failed to load jobs (${response.status})`)
      const data: JobRecord[] = await response.json()
      setJobs(data)
      const health = await fetch('/api/health')
      if (health.ok) {
        const payload = await health.json()
        setBackendState(
          payload.supabaseConfigured
            ? 'Backend + Supabase connected'
            : 'Backend connected (local history)',
        )
      } else {
        setBackendState('Backend connected')
      }
    } catch {
      setBackendState('Backend unavailable in this session')
    }
  }

  async function queueRun(): Promise<void> {
    if (!canQueue) return
    setIsSubmitting(true)
    try {
      const response = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt,
          tags,
          approvals: {
            maps: approvedMaps,
            llm: approvedLlm,
            apify: approvedApify,
          },
        }),
      })

      if (!response.ok) {
        const failed = await response.json()
        throw new Error(failed.error ?? `Failed with status ${response.status}`)
      }

      const created: JobRecord = await response.json()
      setJobs((prev) => [created, ...prev])
      setActivity((prev) => [
        `Job ${created.id} queued. Expected spend ${formatUsd(created.estimate.total)}.`,
        ...prev,
      ])
      setApprovedMaps(false)
      setApprovedLlm(false)
      setApprovedApify(false)
      setConfirmedPlan(false)
      setStep(1)
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error'
      setActivity((prev) => [`Queue failed: ${message}`, ...prev])
    } finally {
      setIsSubmitting(false)
    }
  }

  useEffect(() => {
    void loadJobs()
    const timer = setInterval(() => {
      void loadJobs()
    }, 5000)
    return () => clearInterval(timer)
  }, [])

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <p className="brand">Google Maps Scraper</p>
          <p className="muted">Natural-language lead generation workflow</p>
        </div>
        <p className="status-pill">{backendState}</p>
      </header>

      <section className="hero-band">
        <p className="eyebrow">Lead infrastructure setup</p>
        <h1>From scrape brief to downloadable leads.</h1>
        <p className="subtitle">
          A guided workflow for planning Maps scrapes, approving paid usage, running jobs,
          and re-downloading previous exports anytime.
        </p>
      </section>

      <section className="workflow">
        <div className="panel">
          <div className="panel-head">
            <h2>New scrape job</h2>
            <p>Step {step} of 3</p>
          </div>

          <ol className="steps">
            <li className={step === 1 ? 'active' : ''}>1 Prompt</li>
            <li className={step === 2 ? 'active' : ''}>2 Plan</li>
            <li className={step === 3 ? 'active' : ''}>3 Review</li>
          </ol>

          {step === 1 && (
            <div className="form-block">
              <label htmlFor="brief">What should we scrape? Required</label>
              <textarea
                id="brief"
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                rows={7}
              />
              <label htmlFor="tags">Tags</label>
              <input
                id="tags"
                value={tagsInput}
                onChange={(event) => setTagsInput(event.target.value)}
                placeholder="dental, q3-campaign, california"
              />
              <p className="hint">No spend happens in this step.</p>
              <div className="actions">
                <button type="button" onClick={() => setStep(2)} disabled={prompt.trim().length < 20}>
                  Continue
                </button>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="form-block">
              <ul className="kv">
                <li><span>Categories</span><strong>{parsed.categories.join(', ')}</strong></li>
                <li><span>States</span><strong>{parsed.states.join(', ')}</strong></li>
                <li><span>Min rating</span><strong>{parsed.minRating.toFixed(1)}+</strong></li>
                <li><span>Min reviews</span><strong>{parsed.minReviews}+</strong></li>
                <li><span>Lead target</span><strong>{parsed.leadTarget.toLocaleString()}</strong></li>
                <li><span>Owner / email</span><strong>{parsed.needsOwners ? 'Yes' : 'Optional'} / {parsed.needsEmail ? 'Yes' : 'No'}</strong></li>
              </ul>
              <div className="actions split">
                <button type="button" className="ghost" onClick={() => setStep(1)}>Back</button>
                <button type="button" onClick={() => setStep(3)}>Continue</button>
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="form-block">
              <p className="callout">
                Creating this job does not spend money until you approve the paid actions below.
              </p>
              <ul className="kv">
                <li><span>Maps requests (est.)</span><strong>{estimates.requestEstimate.toLocaleString()}</strong></li>
                <li><span>RapidAPI Maps</span><strong>{formatUsd(estimates.mapsCost)}</strong></li>
                <li><span>LLM cost</span><strong>{formatUsd(estimates.llmCost)}</strong></li>
                <li><span>Apify fallback</span><strong>{formatUsd(estimates.apifyCost)}</strong></li>
                <li className="total"><span>Total projected</span><strong>{formatUsd(estimates.total)}</strong></li>
              </ul>

              <label className="check">
                <input type="checkbox" checked={confirmedPlan} onChange={(e) => setConfirmedPlan(e.target.checked)} />
                The scrape details and cost estimate look correct
              </label>
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

              <div className="actions split">
                <button type="button" className="ghost" onClick={() => setStep(2)}>Back</button>
                <button type="button" onClick={() => void queueRun()} disabled={!canQueue || isSubmitting}>
                  {isSubmitting ? 'Starting…' : 'Create scrape job'}
                </button>
              </div>
            </div>
          )}
        </div>

        <aside className="summary">
          <h3>Live summary</h3>
          <ul className="kv">
            <li><span>Prompt</span><strong>{prompt.slice(0, 48)}{prompt.length > 48 ? '…' : ''}</strong></li>
            <li><span>Tags</span><strong>{tags.join(', ') || '—'}</strong></li>
            <li><span>Categories</span><strong>{parsed.categories.join(', ')}</strong></li>
            <li><span>States</span><strong>{parsed.states.join(', ')}</strong></li>
            <li><span>Projected spend</span><strong>{formatUsd(estimates.total)}</strong></li>
          </ul>
          <p className="hint">
            The workflow pauses before paid APIs run. Nothing is charged until you explicitly approve.
          </p>
        </aside>
      </section>

      <section className="panel history-panel">
        <div className="panel-head">
          <h2>Job history + downloads</h2>
          <p>Re-download completed CSV exports anytime</p>
        </div>
        <table className="history">
          <thead>
            <tr>
              <th>Created</th>
              <th>Status</th>
              <th>Tags</th>
              <th>Est. cost</th>
              <th>File</th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 ? (
              <tr>
                <td colSpan={5}>No jobs yet.</td>
              </tr>
            ) : (
              jobs.map((job) => (
                <tr key={job.id}>
                  <td>{new Date(job.createdAt).toLocaleString()}</td>
                  <td><span className={`status ${job.status}`}>{job.status}</span></td>
                  <td>{job.tags.join(', ') || '-'}</td>
                  <td>{formatUsd(job.estimate.total)}</td>
                  <td>
                    {job.downloadUrl ? (
                      <a href={job.downloadUrl}>Download CSV</a>
                    ) : (
                      job.error ?? '-'
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Activity log</h2>
        </div>
        <ul className="log">
          {activity.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>
    </main>
  )
}

export default App
