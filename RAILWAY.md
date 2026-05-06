# Railway Deployment

Two scheduled crons run on Railway:

| Tool | Schedule | Config file |
|------|----------|-------------|
| sync_linnworks_to_square | every 30 min | railway.sync.json |
| pull_square_orders_to_linnworks | every 5 min | railway.pull.json |

## Setup

Each tool gets its own Railway service in the same project, both pointing
at this GitHub repo on `main` branch.

When creating each service:
1. Source: GitHub repo, branch `main`
2. In Settings → Config-as-code path, set the appropriate file:
   - sync service: `railway.sync.json`
   - pull service: `railway.pull.json`
3. Add the 7 environment variables from `.env.example`

Once both services build green and run their first cron successfully,
the migration is complete.
