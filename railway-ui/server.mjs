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
const jobsPath = path.join(dataDir, 'jobs.json')
const port = Number(process.env.PORT) || 4173

const app = express()
app.use(express.json({ limit: '1mb' }))

const supabaseUrl = process.env.SUPABASE_URL
const supabaseServiceRole = process.env.SUPABASE_SERVICE_ROLE_KEY
const supabaseBucket = process.env.SUPABASE_EXPORT_BUCKET || 'lead_exports'
const supabaseJobsTable = process.env.SUPABASE_JOBS_TABLE || 'scrape_jobs'
const supabaseLeadsTable = process.env.SUPABASE_LEADS_TABLE || 'scrape_leads'

const supabase =
  supabaseUrl && supabaseServiceRole
    ? createClient(supabaseUrl, supabaseServiceRole)
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

/** @type {JobRecord[]} */
let jobs = []

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

async function ensureFiles() {
  await fs.mkdir(outputDir, { recursive: true })
  try {
    await fs.access(jobsPath)
  } catch {
    await fs.writeFile(jobsPath, '[]', 'utf8')
  }
}

async function saveJobs() {
  await fs.writeFile(jobsPath, JSON.stringify(jobs, null, 2), 'utf8')
}

async function loadJobs() {
  const raw = await fs.readFile(jobsPath, 'utf8')
  jobs = JSON.parse(raw)
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

async function persistToSupabase(job) {
  if (!supabase) return

  try {
    await supabase.from(supabaseJobsTable).upsert(
      {
        id: job.id,
        prompt: job.prompt,
        tags: job.tags,
        status: job.status,
        estimate_total: job.estimate.total,
        download_url: job.downloadUrl,
        error: job.error,
        created_at: job.createdAt,
        finished_at: job.finishedAt,
      },
      { onConflict: 'id' },
    )
  } catch (error) {
    console.error('Supabase job upsert failed:', error)
  }

  if (!job.localFilePath || job.status !== 'completed') return

  try {
    const fileBuffer = await fs.readFile(job.localFilePath)
    const storagePath = `${job.id}.csv`
    const upload = await supabase.storage.from(supabaseBucket).upload(storagePath, fileBuffer, {
      contentType: 'text/csv',
      upsert: true,
    })
    if (!upload.error) {
      const { data } = supabase.storage.from(supabaseBucket).getPublicUrl(storagePath)
      job.downloadUrl = data.publicUrl
    }
  } catch (error) {
    console.error('Supabase storage upload failed:', error)
  }

  try {
    const csvContent = await fs.readFile(job.localFilePath, 'utf8')
    const rows = csvToJsonRows(csvContent)
    if (rows.length > 0) {
      const leadPayload = rows.slice(0, 2000).map((row) => ({
        job_id: job.id,
        tags: job.tags,
        raw: row,
      }))
      await supabase.from(supabaseLeadsTable).insert(leadPayload)
    }
  } catch (error) {
    console.error('Supabase leads insert failed:', error)
  }
}

async function runJob(job) {
  job.status = 'running'
  job.logs.unshift('Starting gmscraper run...')
  await saveJobs()
  await persistToSupabase(job)

  const planPath = path.join(dataDir, `${job.id}.plan.json`)
  const outputPath = path.join(outputDir, `${job.id}.csv`)
  const planJson = {
    prompt: job.prompt,
    tags: job.tags,
    createdAt: job.createdAt,
  }
  await fs.writeFile(planPath, JSON.stringify(planJson, null, 2), 'utf8')

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
    if (code === 0) {
      job.status = 'completed'
      job.finishedAt = new Date().toISOString()
      job.localFilePath = outputPath
      job.downloadUrl = `/api/jobs/${job.id}/file`
      job.error = null
      job.logs.unshift('Job completed. CSV ready for download.')
    } else {
      job.status = 'failed'
      job.finishedAt = new Date().toISOString()
      job.error =
        'Job failed. Ensure gmscraper and its Python dependencies are available in this deployment.'
      job.logs.unshift(job.error)
    }
    await saveJobs()
    await persistToSupabase(job)
  })
}

app.get('/api/health', (_request, response) => {
  response.json({
    ok: true,
    supabaseConfigured: Boolean(supabase),
    historyMode: supabase ? 'supabase+local' : 'local',
  })
})

app.get('/api/jobs', async (_request, response) => {
  const ordered = [...jobs].sort((a, b) => b.createdAt.localeCompare(a.createdAt))
  response.json(ordered)
})

app.post('/api/jobs', async (request, response) => {
  const prompt = String(request.body?.prompt || '').trim()
  const tags = Array.isArray(request.body?.tags)
    ? request.body.tags.map((tag) => String(tag).trim()).filter(Boolean)
    : []
  const approvals = request.body?.approvals || {}

  if (prompt.length < 20) {
    return response.status(400).json({ error: 'Prompt is too short.' })
  }

  const plan = parsePrompt(prompt)
  const estimate = estimateFromPlan(plan)
  const requiresApify = plan.usesFallback

  if (!approvals.maps || !approvals.llm || (requiresApify && !approvals.apify)) {
    return response.status(400).json({ error: 'Missing required paid-action approvals.' })
  }

  /** @type {JobRecord} */
  const job = {
    id: slugId(),
    prompt,
    tags,
    status: 'queued',
    createdAt: new Date().toISOString(),
    finishedAt: null,
    estimate,
    approvals: {
      maps: Boolean(approvals.maps),
      llm: Boolean(approvals.llm),
      apify: Boolean(approvals.apify),
    },
    downloadUrl: null,
    localFilePath: null,
    error: null,
    logs: ['Queued by UI'],
  }

  jobs.push(job)
  await saveJobs()
  await persistToSupabase(job)
  void runJob(job)
  return response.status(201).json(job)
})

app.get('/api/jobs/:id/file', async (request, response) => {
  const job = jobs.find((entry) => entry.id === request.params.id)
  if (!job || !job.localFilePath || job.status !== 'completed') {
    return response.status(404).json({ error: 'File not found for this job.' })
  }

  try {
    await fs.access(job.localFilePath)
    response.download(job.localFilePath, `leads-${job.id}.csv`)
  } catch {
    response.status(404).json({ error: 'Local file is missing.' })
  }
})

app.use(express.static(distDir))
app.use((_request, response) => {
  response.sendFile(path.join(distDir, 'index.html'))
})

await ensureFiles()
await loadJobs()

app.listen(port, '0.0.0.0', () => {
  console.log(`Server listening on ${port}`)
})
