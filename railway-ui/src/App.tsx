import './App.css'

function App() {
  const railwayQueries = [
    {
      label: 'Check auth + workspace context',
      command: 'railway whoami --json',
      reason: 'Confirms you are signed in and shows the active workspace.',
    },
    {
      label: 'Check linked project/service',
      command: 'railway status --json',
      reason: 'Shows which project, environment, and service this directory targets.',
    },
    {
      label: 'List projects to find the right one',
      command: 'railway project list --json',
      reason: 'Use this when status says the current directory is not linked.',
    },
    {
      label: 'Inspect recent service logs',
      command: 'railway logs --service <service-name> --lines 200 --json',
      reason: 'Use after deploy to diagnose crashes, port issues, or missing vars.',
    },
    {
      label: 'Check deployment lifecycle',
      command: 'railway deployment list --json',
      reason: 'Verify your latest deployment reached SUCCESS before sharing a URL.',
    },
  ]

  const searchPlaybook = [
    {
      where: 'Railway Docs',
      query: 'railway vite react static site deploy',
      expected: 'Build/start patterns, PORT handling, and static deployment guidance.',
    },
    {
      where: 'Railway Dashboard → Deployments',
      query: 'Failed deploy + build logs',
      expected: 'Exact build/runtime error lines to fix before redeploy.',
    },
    {
      where: 'Railway Dashboard → Variables',
      query: 'PORT and required app env vars',
      expected: 'Missing configuration causing startup/runtime failures.',
    },
    {
      where: 'Your codebase',
      query: 'rg "process\\.env|import\\.meta\\.env" src',
      expected: 'Every environment variable used by your app, for variable setup.',
    },
  ]

  const spendGuardrail = [
    'No paid call without explicit approval.',
    'Always estimate cost before execution.',
    'Applies to Apify and any paid external API.',
    'If estimate is unclear, call is blocked until clarified.',
  ]

  return (
    <main className="app">
      <header className="hero">
        <p className="eyebrow">Railway Launchpad</p>
        <h1>Ship this UI and know exactly what to query next</h1>
        <p className="subtitle">
          This screen gives you a practical playbook for deploying to Railway and
          debugging quickly with the right commands and searches.
        </p>
      </header>

      <section className="grid">
        <article className="card">
          <h2>Deploy checklist</h2>
          <ol>
            <li>
              Create a Railway project and service from this folder:
              <code>railway up</code>
            </li>
            <li>
              Confirm deployment reached <strong>SUCCESS</strong>:
              <code>railway deployment list --json</code>
            </li>
            <li>
              Open your service URL and verify the page loads:
              <code>railway domain</code>
            </li>
          </ol>
        </article>

        <article className="card">
          <h2>What to query in Railway CLI</h2>
          <ul className="query-list">
            {railwayQueries.map((item) => (
              <li key={item.command}>
                <h3>{item.label}</h3>
                <code>{item.command}</code>
                <p>{item.reason}</p>
              </li>
            ))}
          </ul>
        </article>

        <article className="card">
          <h2>Where to search when blocked</h2>
          <ul className="query-list">
            {searchPlaybook.map((item) => (
              <li key={`${item.where}-${item.query}`}>
                <h3>{item.where}</h3>
                <p>
                  <strong>Search/query:</strong> <code>{item.query}</code>
                </p>
                <p>
                  <strong>Look for:</strong> {item.expected}
                </p>
              </li>
            ))}
          </ul>
        </article>

        <article className="card">
          <h2>Spend approval gate (required)</h2>
          <p className="callout">
            This project enforces a manual gate before any paid API action.
          </p>
          <ul className="query-list">
            {spendGuardrail.map((rule) => (
              <li key={rule}>
                <p>{rule}</p>
              </li>
            ))}
          </ul>
        </article>
      </section>
    </main>
  )
}

export default App
