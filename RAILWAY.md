# Railway Deployment

Two scheduled crons run on Railway, each as its own service in the same
project. Both share the same Docker image (built from the repo root
`Dockerfile`) but specify different start commands and cron schedules
via their respective config files:

| Tool | Schedule | Config file |
|------|----------|-------------|
| sync_linnworks_to_square | every 30 min | railway.sync.json |
| pull_square_orders_to_linnworks | every 5 min | railway.pull.json |

## Setup

1. Create a new Railway service from this GitHub repo (`main` branch).
2. In Settings → Build, ensure builder is "Dockerfile" (or auto-detected).
3. In Settings → Source, set "Config-as-code path" to either:
   - `railway.sync.json` (for the sync service), or
   - `railway.pull.json` (for the pull service)
4. Add the 7 environment variables (see `.env.example`).
5. Wait for the build to succeed and the cron to register.
