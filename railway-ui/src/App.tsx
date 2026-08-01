import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'

type Intent = {
  city: string
  niche: string
  maxLeads: number
  enrichment: boolean
  includeOwners: boolean
  includeClassification: boolean
}

type CostEstimate = {
  currency: string
  estimatedUsd: number
  breakdown: Array<{ item: string; usd: number }>
  notes: string[]
}

type JobRecord = {
  id: string
  createdAt: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  prompt: string
  intent: Intent
  costEstimate: CostEstimate
  approvedSpendUsd: number
  command: string
  outputPath: string | null
  error: string | null
  startedAt: string | null
  finishedAt: string | null
}

type HistoryResponse = {
  jobs: JobRecord[]
  supabaseConfigured: boolean
  historyMode: string
  persistence: string
}

const EXAMPLES = [
  'Find med spa leads in Austin with owners and emails, max 25',
  'Scrape dental clinics in Miami, max 40, no owners',
  'Get roofing companies in Dallas with enrichment, limit 30',
]

function parsePrompt(prompt: string): Intent {
  const text = prompt.trim()
  const lower = text.toLowerCase()

  const cityMatch =
    lower.match(/\bin\s+([a-z][a-z\s.'-]{1,40}?)(?:\s+(?:with|and|for|that|who|max|limit|only)\b|[.,]|$)/i) ||
    lower.match(/\b(?:around|near)\s+([a-z][a-z\s.'-]{1,40}?)(?:\s+(?:with|and|for|that|who|max|limit|only)\b|[.,]|$)/i)

  let city = cityMatch?.[1]?.trim() || 'Austin'
  city = city.replace(/\b(tx|texas|ca|california|ny|new york|fl|florida)\b/gi, '').trim() || city

  let niche = text
  if (cityMatch) niche = niche.replace(new RegExp(cityMatch[0], 'i'), ' ')
  niche = niche
    .replace(/\b(find|get|scrape|pull|show|list|me|please|leads?|business(?:es)?|companies|owners?|emails?|phones?)\b/gi, ' ')
    .replace(/\b(with|and|for|that|who|max|limit|only|enriched?|enrichment|classification|owners?)\b/gi, ' ')
    .replace(/\b\d+\b/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  if (!niche || niche.length < 3) niche = 'med spas'

  const maxMatch = lower.match(/\b(?:max|limit|only|top)\s+(\d{1,4})\b/) || lower.match(/\b(\d{1,4})\s+leads?\b/)
  const maxLeads = Math.min(Math.max(Number(maxMatch?.[1] || 25), 1), 500)

  return {
    city,
    niche,
    maxLeads,
    enrichment: !/\bno enrich/i.test(lower),
    includeOwners: !/\bno owners?\b/i.test(lower),
    includeClassification: !/\bno classif/i.test(lower),
  }
}

function estimateCost(intent: Intent): CostEstimate {
  const mapsCost = Math.max(0.08, intent.maxLeads * 0.012)
  const enrichmentCost = intent.enrichment ? intent.maxLeads * 0.01 : 0
  const classificationCost = intent.includeClassification ? intent.maxLeads * 0.004 : 0
  const ownerCost = intent.includeOwners ? intent.maxLeads * 0.006 : 0
  const estimatedUsd = Number((mapsCost + enrichmentCost + classificationCost + ownerCost).toFixed(2))

  return {
    currency: 'USD',
    estimatedUsd,
    breakdown: [
      { item: 'Google Maps scrape (RapidAPI Maps Data)', usd: Number(mapsCost.toFixed(2)) },
      { item: 'Website enrichment', usd: Number(enrichmentCost.toFixed(2)) },
      { item: 'Lead classification', usd: Number(classificationCost.toFixed(2)) },
      { item: 'Owner discovery', usd: Number(ownerCost.toFixed(2)) },
    ].filter((row) => row.usd > 0),
    notes: [
      'Estimate only. Actual spend depends on API pricing and result volume.',
      'No paid call is made until you explicitly approve this estimate.',
    ],
  }
}

function statusClass(status: JobRecord['status']) {
  if (status === 'succeeded') return 'completed'
  if (status === 'failed') return 'failed'
  return 'awaiting'
}

function statusLabel(status: JobRecord['status']) {
  if (status === 'queued') return 'Queued'
  if (status === 'running') return 'Running'
  if (status === 'succeeded') return 'Ready'
  return 'Failed'
}

function App() {
  const [step, setStep] = useState(1)
  const [prompt, setPrompt] = useState(EXAMPLES[0])
  const [approved, setApproved] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [jobs, setJobs] = useState<JobRecord[]>([])
  const [supabaseConfigured, setSupabaseConfigured] = useState(false)
  const [historyMode, setHistoryMode] = useState('unknown')
  const [activeJobId, setActiveJobId] = useState<string | null>(null)

  const intent = useMemo(() => parsePrompt(prompt), [prompt])
  const estimate = useMemo(() => estimateCost(intent), [intent])
  const activeJob = jobs.find((job) => job.id === activeJobId) || null

  async function refreshHistory() {
    const response = await fetch('/api/jobs')
    if (!response.ok) throw new Error('Could not load scrape history')
    const data = (await response.json()) as HistoryResponse
    setJobs(data.jobs || [])
    setSupabaseConfigured(Boolean(data.supabaseConfigured))
    setHistoryMode(data.historyMode || 'unknown')
  }

  useEffect(() => {
    refreshHistory().catch((err: Error) => setError(err.message))
  }, [])

  useEffect(() => {
    if (!activeJob) return
    if (activeJob.status === 'succeeded' || activeJob.status === 'failed') return
    const timer = window.setInterval(() => {
      refreshHistory().catch(() => undefined)
    }, 2500)
    return () => window.clearInterval(timer)
  }, [activeJob?.id, activeJob?.status])

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)

    if (!approved) {
      setError('Approve the estimated spend before starting the scrape.')
      return
    }

    setSubmitting(true)
    try {
      const response = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt,
          intent,
          costEstimate: estimate,
          approvedSpendUsd: estimate.estimatedUsd,
          approved: true,
        }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.error || 'Failed to create scrape job')
      setActiveJobId(data.job.id)
      await refreshHistory()
      setStep(3)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected error')
    } finally {
      setSubmitting(false)
    }
  }

  const wizardTitle =
    step === 1 ? 'What leads should we find?' : step === 2 ? 'Review the scrape plan' : 'Scrape status'

  return (
    <>
      <header className="topbar">
        <a className="wordmark" href="/" aria-label="Google Maps Scraper home">
          <span className="wordmark-mark" aria-hidden="true">
            G
          </span>
          <span>Google Maps Scraper</span>
        </a>
        <span className="safety-badge">
          <i /> Human approval required before spend
        </span>
      </header>

      <main className="shell">
        <section className="hero">
          <p className="eyebrow">Lead generation console</p>
          <h1>From a plain-English brief to downloadable leads.</h1>
          <p>
            Describe the niche and city, review the paid-API estimate, then approve before anything
            runs. History and CSV exports stay in Supabase.
          </p>
        </section>

        <div className="layout">
          <div className="primary">
            <section className="panel wizard-panel">
              <div className="panel-heading">
                <div>
                  <p className="eyebrow">New scrape</p>
                  <h2>{wizardTitle}</h2>
                </div>
                <span className="step-count">Step {step} of 3</span>
              </div>

              <ol className="wizard-progress" aria-label="Scrape setup progress">
                <li className={step === 1 ? 'active' : step > 1 ? 'complete' : ''}>
                  <span>{step > 1 ? '✓' : '1'}</span>Brief
                </li>
                <li className={step === 2 ? 'active' : step > 2 ? 'complete' : ''}>
                  <span>{step > 2 ? '✓' : '2'}</span>Plan
                </li>
                <li className={step === 3 ? 'active' : ''}>
                  <span>3</span>Run
                </li>
              </ol>

              {step === 1 ? (
                <div className="wizard-step">
                  <div className="field">
                    <label htmlFor="lead-prompt">
                      Lead request <b>Required</b>
                    </label>
                    <textarea
                      id="lead-prompt"
                      value={prompt}
                      onChange={(event) => {
                        setPrompt(event.target.value)
                        setApproved(false)
                      }}
                      rows={5}
                      placeholder="Find HVAC companies in Denver with owner emails, max 40"
                    />
                    <small>Include niche, city, and optional lead count. We parse the rest.</small>
                  </div>

                  <div className="example-row">
                    {EXAMPLES.map((example) => (
                      <button
                        key={example}
                        type="button"
                        className="example-chip"
                        onClick={() => {
                          setPrompt(example)
                          setApproved(false)
                        }}
                      >
                        {example}
                      </button>
                    ))}
                  </div>

                  <div className="wizard-actions single">
                    <span />
                    <button type="button" onClick={() => setStep(2)}>
                      Continue to plan
                    </button>
                  </div>
                </div>
              ) : null}

              {step === 2 ? (
                <form className="wizard-step" onSubmit={onSubmit}>
                  <div className="plan-preview" aria-live="polite">
                    <div>
                      <span>{intent.maxLeads}</span>
                      <small>max leads</small>
                    </div>
                    <div>
                      <span>${estimate.estimatedUsd.toFixed(2)}</span>
                      <small>est. spend</small>
                    </div>
                    <div>
                      <span>{intent.enrichment ? 'On' : 'Off'}</span>
                      <small>enrichment</small>
                    </div>
                  </div>

                  <div className="review-card">
                    <div className="review-row">
                      <span>City</span>
                      <strong>{intent.city}</strong>
                    </div>
                    <div className="review-row">
                      <span>Niche</span>
                      <strong>{intent.niche}</strong>
                    </div>
                    <div className="review-row">
                      <span>Owners</span>
                      <strong>{intent.includeOwners ? 'Include' : 'Skip'}</strong>
                    </div>
                    <div className="review-row">
                      <span>Classification</span>
                      <strong>{intent.includeClassification ? 'Include' : 'Skip'}</strong>
                    </div>
                    <div className="review-row">
                      <span>Request</span>
                      <strong>{prompt}</strong>
                    </div>
                  </div>

                  <div className="approval-callout">
                    <span className="lock">$</span>
                    <div>
                      <strong>
                        Estimated spend: ${estimate.estimatedUsd.toFixed(2)} {estimate.currency}
                      </strong>
                      <p>
                        {estimate.breakdown.map((row) => `${row.item}: $${row.usd.toFixed(2)}`).join(' · ')}
                      </p>
                    </div>
                  </div>

                  <label className="choice-row confirmation">
                    <input
                      type="checkbox"
                      checked={approved}
                      onChange={(event) => setApproved(event.target.checked)}
                    />
                    <span>
                      <strong>I approve this paid-API estimate</strong>
                      <small>
                        Nothing is charged until you check this box and start the scrape. Cap:{' '}
                        ${estimate.estimatedUsd.toFixed(2)}.
                      </small>
                    </span>
                  </label>

                  {error ? <p className="form-message">{error}</p> : null}

                  <div className="wizard-actions">
                    <button type="button" className="button secondary" onClick={() => setStep(1)}>
                      Back
                    </button>
                    <span />
                    <button type="submit" disabled={!approved || submitting}>
                      {submitting ? 'Starting…' : 'Approve & start scrape'}
                    </button>
                  </div>
                </form>
              ) : null}

              {step === 3 ? (
                <div className="wizard-step">
                  {activeJob ? (
                    <>
                      <div className="job-head">
                        <div>
                          <p className="eyebrow">Active job</p>
                          <h2>
                            {activeJob.intent.niche} · {activeJob.intent.city}
                          </h2>
                        </div>
                        <span className={`status ${statusClass(activeJob.status)}`}>
                          {statusLabel(activeJob.status)}
                        </span>
                      </div>

                      <div className="review-card">
                        <div className="review-row">
                          <span>Job ID</span>
                          <strong>
                            <code>{activeJob.id}</code>
                          </strong>
                        </div>
                        <div className="review-row">
                          <span>Approved</span>
                          <strong>${activeJob.approvedSpendUsd.toFixed(2)}</strong>
                        </div>
                        <div className="review-row">
                          <span>Request</span>
                          <strong>{activeJob.prompt}</strong>
                        </div>
                      </div>

                      {activeJob.error ? <p className="form-message">{activeJob.error}</p> : null}

                      {activeJob.status === 'succeeded' ? (
                        <div className="info-callout" style={{ marginTop: '1.25rem' }}>
                          <span>✓</span>
                          <p>CSV is ready. Download stays available from Supabase history.</p>
                        </div>
                      ) : null}

                      <div className="wizard-actions">
                        <button
                          type="button"
                          className="button secondary"
                          onClick={() => {
                            setApproved(false)
                            setActiveJobId(null)
                            setError(null)
                            setStep(1)
                          }}
                        >
                          New scrape
                        </button>
                        <span />
                        {activeJob.status === 'succeeded' ? (
                          <a className="button" href={`/api/jobs/${activeJob.id}/file`}>
                            Download CSV
                          </a>
                        ) : (
                          <button type="button" className="button secondary" onClick={() => refreshHistory()}>
                            Refresh status
                          </button>
                        )}
                      </div>
                    </>
                  ) : (
                    <>
                      <p className="empty-state">No active job yet. Approve a plan to start one.</p>
                      <div className="wizard-actions single">
                        <span />
                        <button type="button" onClick={() => setStep(1)}>
                          Start a scrape
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ) : null}
            </section>

            <section className="panel">
              <div className="jobs-head">
                <div>
                  <p className="eyebrow">Durable history</p>
                  <h2>All scrape jobs</h2>
                </div>
                <span className="step-count">{jobs.length} stored</span>
              </div>

              {jobs.length === 0 ? (
                <p className="empty-state">Approved scrapes appear here with download links.</p>
              ) : (
                <div className="table-wrap">
                  <table className="history-table">
                    <thead>
                      <tr>
                        <th>When</th>
                        <th>Request</th>
                        <th>Status</th>
                        <th>Est.</th>
                        <th>File</th>
                      </tr>
                    </thead>
                    <tbody>
                      {jobs.map((job) => (
                        <tr key={job.id}>
                          <td>{new Date(job.createdAt).toLocaleString()}</td>
                          <td>
                            <strong>
                              {job.intent.niche} · {job.intent.city}
                            </strong>
                            <small>{job.prompt}</small>
                          </td>
                          <td>
                            <span className={`status ${statusClass(job.status)}`}>
                              {statusLabel(job.status)}
                            </span>
                          </td>
                          <td>${job.approvedSpendUsd.toFixed(2)}</td>
                          <td>
                            {job.status === 'succeeded' ? (
                              <a href={`/api/jobs/${job.id}/file`}>Download</a>
                            ) : (
                              '—'
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>

          <aside className="sidebar">
            <section className="panel compact-panel">
              <div className="jobs-head">
                <h2>Live summary</h2>
                <button type="button" className="text-button" onClick={() => refreshHistory()}>
                  Refresh
                </button>
              </div>
              <div className="review-card">
                <div className="review-row">
                  <span>City</span>
                  <strong>{intent.city}</strong>
                </div>
                <div className="review-row">
                  <span>Niche</span>
                  <strong>{intent.niche}</strong>
                </div>
                <div className="review-row">
                  <span>Max leads</span>
                  <strong>{intent.maxLeads}</strong>
                </div>
                <div className="review-row">
                  <span>Est. spend</span>
                  <strong>${estimate.estimatedUsd.toFixed(2)}</strong>
                </div>
                <div className="review-row">
                  <span>Storage</span>
                  <strong>{supabaseConfigured ? historyMode : 'local'}</strong>
                </div>
              </div>
            </section>

            <section className="panel compact-panel">
              <div className="jobs-head">
                <h2>Recent jobs</h2>
              </div>
              {jobs.length === 0 ? (
                <p className="empty-state">No jobs yet.</p>
              ) : (
                <ul className="jobs">
                  {jobs.slice(0, 8).map((job) => (
                    <li key={job.id}>
                      <button
                        type="button"
                        className={`job-link${activeJobId === job.id ? ' active' : ''}`}
                        onClick={() => {
                          setActiveJobId(job.id)
                          setStep(3)
                        }}
                      >
                        <span>
                          <strong>
                            {job.intent.niche} · {job.intent.city}
                          </strong>
                          <small>{new Date(job.createdAt).toLocaleString()}</small>
                        </span>
                        <em className={job.status === 'succeeded' ? 'good' : job.status === 'failed' ? 'bad' : ''}>
                          {statusLabel(job.status)}
                        </em>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="panel compact-panel guardrails">
              <h2>Guardrails</h2>
              <ul>
                <li>
                  <span>✓</span> Cost estimate shown before run
                </li>
                <li>
                  <span>✓</span> Explicit approval required
                </li>
                <li>
                  <span>✓</span> Jobs + CSVs stored in Supabase
                </li>
              </ul>
            </section>
          </aside>
        </div>
      </main>
    </>
  )
}

export default App
