# Meme Sniper V0.7.3 — 24/7 Railway deployment

This deployment keeps **paper trading only**. `LIVE_TRADING_ENABLED=false` and there is still no transaction signer/private-key path in the application.

## What this gives you

- Runs continuously even when your PC is off.
- Railway-provided HTTPS URL that works on your phone.
- Password protection built into Meme Sniper.
- Persistent SQLite database on a Railway Volume.
- Automatic service restart policy can be enabled in Railway.
- `/health` endpoint for deployment health checks.

## 1. Create a safe copy of your current data

On Windows, in your existing Meme Sniper folder, double-click:

`prepare_cloud_db.bat`

It creates:

`cloud_seed\memesniper.db`

using SQLite's backup API and verifies the copy. It does **not** copy `.env` or API keys.

## 2. Deploy this folder as one Railway service

Railway detects the root `Dockerfile`. Generate a public Railway domain for the service and set `/health` as the health-check path.

Attach **one persistent volume** to the service with mount path:

`/data`

Set this service variable:

`DATABASE_URL=sqlite+aiosqlite:////data/memesniper.db`

Railway volumes persist across deploys/restarts. Do not run multiple replicas against this SQLite database.

## 3. Add variables in Railway

Copy the API settings from your own local `.env` directly into Railway's Variables page. Do not paste keys into chat or commit them to the ZIP/repository.

Also set:

```
ENVIRONMENT=production
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_USERNAME=meme
DASHBOARD_PASSWORD=<a long unique password you choose>
LIVE_TRADING_ENABLED=false
PAPER_SIGNAL_SHADOW_MODE=true
```

Railway provides `PORT`; Meme Sniper reads it automatically.

## 4. Upload your existing database

After the `/data` volume exists, upload `cloud_seed\memesniper.db` to:

`/data/memesniper.db`

Do this **before** starting the long-running test if you want to retain the existing research history. If you intentionally want a clean database, skip this step.

Railway's CLI supports volume file upload/download. The Railway UI/plugin can also be used where available.

## 5. Networking and phone access

Generate a Railway domain under the service's Networking settings. Railway terminates HTTPS. Open that HTTPS address on your phone and enter the dashboard username/password.

Do not disable dashboard authentication on a public domain.

## 6. Recommended service settings

- One replica only (SQLite).
- Healthcheck: `/health`.
- Restart policy: `Always` on a plan that supports it, otherwise `On Failure`.
- Keep serverless/sleeping mode OFF because the scanners must run even when nobody has the dashboard open.
- Add Railway volume backups once the service is stable.

## 7. V0.7.3 forward-test behavior

The first V0.7.3 launch creates a new forward-test epoch while preserving historical results. Old pending/open strategy positions are quarantined so they do not contaminate the new test.

Signals run in **SHADOW** mode. They still enter/exit simulated positions and accumulate research statistics, but their P/L is excluded from the main verified P/L.

The verified wallet-copy strategy is intentionally much more selective:

- wallet profile >= 75
- trader intelligence >= 75
- copyability >= 65
- at least 20 eligible observations
- >=55% positive 30-second observations
- median 60-second forward return >= 5%
- median 300-second forward return >= 7%
- Rug Shield must pass
- maximum 5 concurrent copy positions
- new copy entries pause for the Perth day after verified copy P/L reaches -$15; all monitoring/learning continues

These gates do not guarantee profit. They exist to stop the current system from paying paper fees/slippage on weak signals while still collecting enough data to evaluate the strategy.
