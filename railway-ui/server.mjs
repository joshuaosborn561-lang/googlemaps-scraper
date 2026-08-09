import express from 'express'
import { spawn, spawnSync } from 'node:child_process'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createClient } from '@supabase/supabase-js'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const distDir = path.join(__dirname, 'dist')
const dataDir = path.join(__dirname, 'data')
const outputDir = path.join(dataDir, 'outputs')
const port = Number(process.env.PORT) || 4173

const app = express()
app.use(express.json({ limit: '2mb' }))

const supabaseUrl = process.env.SUPABASE_URL
const supabaseAnonKey = process.env.SUPABASE_ANON_KEY
const supabaseIngestSecret = process.env.SUPABASE_INGEST_SECRET

const supabase =
  supabaseUrl && supabaseAnonKey && supabaseIngestSecret
    ? createClient(supabaseUrl, supabaseAnonKey)
    : null

const STATE_CODES = [
  'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DC', 'DE', 'FL', 'GA', 'HI', 'IA',
  'ID', 'IL', 'IN', 'KS', 'KY', 'LA', 'MA', 'MD', 'ME', 'MI', 'MN', 'MO', 'MS',
  'MT', 'NC', 'ND', 'NE', 'NH', 'NJ', 'NM', 'NV', 'NY', 'OH', 'OK', 'OR', 'PA',
  'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VA', 'VT', 'WA', 'WI', 'WV', 'WY',
]

const CATEGORY_HINTS = [
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

/**
 * @typedef {{
 * id: string
 * prompt: string
 * tags: string[]
 * status: 'queued'|'running'|'completed'|'failed'
 * createdAt: string
 * finishedAt: string|null
 * estimate: {requestEstimate:number,mapsCost:number,llmCost:number,apifyCost:number,total:number}
 * approvals: {maps:boolean,llm:boolean,apify:boolean}
 * downloadUrl: string|null
 * localFilePath: string|null
 * error: string|null
 * logs: string[]
 * }} JobRecord
 */

/** In-memory overlay for active runs only. Source of truth is Supabase. */
/** @type {Map<string, JobRecord>} */
const activeJobs = new Map()

function parsePrompt(prompt) {
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
    usesFallback: /(fallback|web search|apify|owner)/.test(lower),
    leadTarget: leadsMatch ? Number(leadsMatch[1]) : 500,
  }
}

function estimateFromPlan(plan) {
  const zipEstimate = Math.max(120, plan.states.length * 340)
  const requestEstimate = zipEstimate * plan.categories.length
  const mapsCost = requestEstimate * 0.0005
  const llmRecords = Math.min(plan.leadTarget, requestEstimate * 0.32)
  const llmCost = llmRecords * 0.002
  const apifySearches = plan.usesFallback ? Math.ceil(llmRecords * 0.18) : 0
  const apifyCost = apifySearches * 0.0005
  return {
    requestEstimate,
    mapsCost,
    llmCost,
    apifyCost,
    total: mapsCost + llmCost + apifyCost,
  }
}

function resolvePythonBinary() {
  if (spawnSync('python3', ['--version']).status === 0) return 'python3'
  return 'python'
}

function slugId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

function requireSupabase() {
  if (!supabase || !supabaseIngestSecret) {
    const error = new Error('Supabase is not configured on this service.')
    error.statusCode = 503
    throw error
  }
}

function mapRemoteJob(row) {
  const hasExport = Boolean(row.has_export)
  return {
    id: row.id,
    prompt: row.prompt || '',
    tags: Array.isArray(row.tags) ? row.tags : [],
    status: row.status,
    createdAt: row.created_at,
    finishedAt: row.finished_at || null,
    estimate: {
      requestEstimate: Number(row.request_estimate || 0),
      mapsCost: Number(row.estimate_maps || 0),
      llmCost: Number(row.estimate_llm || 0),
      apifyCost: Number(row.estimate_apify || 0),
      total: Number(row.estimate_total || 0),
    },
    approvals: { maps: true, llm: true, apify: true },
    downloadUrl: hasExport || row.status === 'completed' ? `/api/jobs/${row.id}/file` : null,
    localFilePath: null,
    error: row.error || null,
    logs: [],
  }
}

function csvToJsonRows(csvContent) {
  const lines = csvContent.split('\n').map((line) => line.trim()).filter(Boolean)
  if (lines.length < 2) return []
  const headers = lines[0].split(',').map((value) => value.trim())
  return lines.slice(1).map((line) => {
    const parts = line.split(',')
    const row = {}
    for (let index = 0; index < headers.length; index += 1) {
      row[headers[index]] = (parts[index] || '').trim()
    }
    return row
  })
}

async function upsertJobRemote(job) {
  requireSupabase()
  const { error } = await supabase.rpc('ingest_scrape_job', {
    p_secret: supabaseIngestSecret,
    p_job: {
      id: job.id,
      prompt: job.prompt,
      tags: job.tags,
      status: job.status,
      estimate: job.estimate,
      downloadUrl: job.downloadUrl,
      error: job.error,
      createdAt: job.createdAt,
      finishedAt: job.finishedAt,
    },
  })
  if (error) throw error
}

async function upsertExportRemote(jobId, csvContent) {
  requireSupabase()
  const { error } = await supabase.rpc('upsert_scrape_export', {
    p_secret: supabaseIngestSecret,
    p_job_id: jobId,
    p_filename: `leads-${jobId}.csv`,
    p_content: csvContent,
  })
  if (error) throw error
}

async function ingestLeadsRemote(jobId, tags, rows) {
  requireSupabase()
  if (!rows.length) return
  const { error } = await supabase.rpc('ingest_scrape_leads', {
    p_secret: supabaseIngestSecret,
    p_job_id: jobId,
    p_tags: tags,
    p_rows: rows.slice(0, 2000),
  })
  if (error) throw error
}

async function listJobsRemote() {
  requireSupabase()
  const { data, error } = await supabase.rpc('list_scrape_jobs', {
    p_secret: supabaseIngestSecret,
  })
  if (error) throw error
  const rows = Array.isArray(data) ? data : []
  return rows.map(mapRemoteJob)
}

async function getExportRemote(jobId) {
  requireSupabase()
  const { data, error } = await supabase.rpc('get_scrape_export', {
    p_secret: supabaseIngestSecret,
    p_job_id: jobId,
  })
  if (error) throw error
  return data
}

async function ensureFiles() {
  await fs.mkdir(outputDir, { recursive: true })
}

async function runJob(job) {
  job.status = 'running'
  job.logs.unshift('Starting scrape job...')
  activeJobs.set(job.id, job)

  try {
    await upsertJobRemote(job)
  } catch (error) {
    console.error('Failed to mark running in Supabase:', error)
  }

  const outputPath = path.join(outputDir, `${job.id}.csv`)
  const python = resolvePythonBinary()
  const args = ['-m', 'gmscraper', 'run', job.prompt, '--out', outputPath, '--yes']
  const child = spawn(python, args, { cwd: __dirname, env: process.env })

  child.stdout.on('data', (chunk) => {
    job.logs.unshift(chunk.toString().trim())
  })
  child.stderr.on('data', (chunk) => {
    job.logs.unshift(chunk.toString().trim())
  })

  child.on('close', async (code) => {
    try {
      if (code === 0) {
        const csvContent = await fs.readFile(outputPath, 'utf8')
        await upsertExportRemote(job.id, csvContent)
        await ingestLeadsRemote(job.id, job.tags, csvToJsonRows(csvContent))

        job.status = 'completed'
        job.finishedAt = new Date().toISOString()
        job.localFilePath = outputPath
        job.downloadUrl = `/api/jobs/${job.id}/file`
        job.error = null
        job.logs.unshift('Job completed and stored in Supabase.')
      } else {
        job.status = 'failed'
        job.finishedAt = new Date().toISOString()
        job.error =
          'Job failed. Ensure gmscraper and its Python dependencies are available in this deployment.'
        job.logs.unshift(job.error)
      }

      await upsertJobRemote(job)
    } catch (error) {
      job.status = 'failed'
      job.finishedAt = new Date().toISOString()
      job.error = error instanceof Error ? error.message : 'Failed to persist results to Supabase.'
      job.logs.unshift(job.error)
      try {
        await upsertJobRemote(job)
      } catch (persistError) {
        console.error('Failed to persist failed job state:', persistError)
      }
    } finally {
      // Keep completed/failed in memory briefly; list always comes from Supabase.
      setTimeout(() => activeJobs.delete(job.id), 60_000)
    }
  })
}

app.get('/api/health', (_request, response) => {
  response.json({
    ok: true,
    supabaseConfigured: Boolean(supabase),
    historyMode: supabase ? 'supabase' : 'unavailable',
    persistence: 'supabase-primary',
  })
})

app.get('/api/jobs', async (_request, response) => {
  try {
    const remoteJobs = await listJobsRemote()
    const byId = new Map(remoteJobs.map((job) => [job.id, job]))

    for (const active of activeJobs.values()) {
      byId.set(active.id, {
        ...active,
        downloadUrl:
          active.status === 'completed' ? `/api/jobs/${active.id}/file` : active.downloadUrl,
      })
    }

    const ordered = [...byId.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt))
    response.json(ordered)
  } catch (error) {
    console.error('Failed to load jobs from Supabase:', error)
    response.status(error.statusCode || 500).json({
      error: error instanceof Error ? error.message : 'Failed to load jobs from Supabase.',
    })
  }
})

app.post('/api/jobs', async (request, response) => {
  try {
    requireSupabase()

    const prompt = String(request.body?.prompt || '').trim()
    const tags = Array.isArray(request.body?.tags)
      ? request.body.tags.map((tag) => String(tag).trim()).filter(Boolean)
      : []
    if (prompt.length < 20) {
      return response.status(400).json({ error: 'Prompt is too short.' })
    }

    const plan = parsePrompt(prompt)
    const estimate = estimateFromPlan(plan)

    /** @type {JobRecord} */
    const job = {
      id: slugId(),
      prompt,
      tags,
      status: 'queued',
      createdAt: new Date().toISOString(),
      finishedAt: null,
      estimate,
      approvals: { maps: true, llm: true, apify: true },
      downloadUrl: null,
      localFilePath: null,
      error: null,
      logs: ['Queued by UI'],
    }

    await upsertJobRemote(job)
    activeJobs.set(job.id, job)
    void runJob(job)
    return response.status(201).json(job)
  } catch (error) {
    console.error('Failed to create job:', error)
    return response.status(error.statusCode || 500).json({
      error: error instanceof Error ? error.message : 'Failed to create job.',
    })
  }
})

app.get('/api/jobs/:id/file', async (request, response) => {
  try {
    const exportPayload = await getExportRemote(request.params.id)
    const filename = exportPayload?.filename || `leads-${request.params.id}.csv`
    const content = exportPayload?.content || ''

    response.setHeader('Content-Type', 'text/csv; charset=utf-8')
    response.setHeader('Content-Disposition', `attachment; filename="${filename}"`)
    return response.status(200).send(content)
  } catch (error) {
    // Temporary local fallback while an active job is finishing write.
    const active = activeJobs.get(request.params.id)
    if (active?.localFilePath) {
      try {
        await fs.access(active.localFilePath)
        return response.download(active.localFilePath, `leads-${active.id}.csv`)
      } catch {
        // fall through
      }
    }

    console.error('Failed to fetch export from Supabase:', error)
    return response.status(404).json({ error: 'File not found in Supabase for this job.' })
  }
})

app.use(express.static(distDir))
app.use((_request, response) => {
  response.sendFile(path.join(distDir, 'index.html'))
})

await ensureFiles()

app.listen(port, '0.0.0.0', () => {
  console.log(`Server listening on ${port} (Supabase primary persistence)`)
})
