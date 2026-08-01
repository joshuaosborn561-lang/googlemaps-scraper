# Railway UI Launchpad

This project is a minimal React + Vite UI you can deploy to Railway.  
It also includes a built-in on-screen playbook for:

- what to query in Railway CLI,
- where to search when deploys fail,
- what to verify before sharing your app URL.

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

## What to query (copy/paste commands)

```bash
railway whoami --json
railway status --json
railway project list --json
railway logs --service <service-name> --lines 200 --json
railway deployment list --json
```

## Where to search when blocked

1. **Railway docs**
   - Query: `railway vite react static site deploy`
   - Use this for start/build command patterns and PORT behavior.

2. **Railway dashboard → Deployments**
   - Open build/runtime logs from the latest failed deployment.

3. **Railway dashboard → Variables**
   - Confirm every variable required by your UI/backend is set.

4. **Your codebase**
   - Search env usage:
   ```bash
   rg "process\\.env|import\\.meta\\.env" src
   ```

## Spend approval gate (project policy)

Before any paid API call (including Apify), this workflow requires:

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
