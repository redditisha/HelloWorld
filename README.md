# news-collector

Runs on GitHub Actions every 15 minutes: fetches the RSS feeds listed in a
private Google Sheet and appends new articles to it, one tab per publish date.

This repo contains **code only** — no feed list, no data, no keys. The feed list
lives in the sheet's `_sources` tab, and run logs print counts and opaque
source ids only, so the repo and its logs can be public.

## Setup

1. Create a new GitHub repository (public: unlimited Actions minutes) and push
   this folder as its root:
   ```bash
   git init -b main
   git add .
   git commit -m "News collector"
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**:
   - `SHEET_ID` — the sheet id
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — the full contents of the service-account JSON key
3. **Actions** tab → enable workflows if prompted → **Collect news → Run workflow**
   to test once. It then runs on schedule.

The PC must have run one local sync first (it publishes `_sources`).

## Workflows
- `collect.yml` — every 15 min at :07/:22/:37/:52. Never two at once.
- `keepalive.yml` — an empty commit on the 1st of each month. GitHub disables
  schedules in public repos after 60 days without activity; this prevents it.
