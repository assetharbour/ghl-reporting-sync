# GHL Reporting Sync — Asset Harbour Mortgage

GHL → Google Sheets sync service. Fetches opportunities from the
Mortgage pipeline and their linked contacts, joins on contact_id,
upserts one row per case into Google Sheets every 15 minutes.

## Architecture

GHL Private API → FastAPI (Vercel serverless) → Google Sheets → React Dashboard

## Stack

- FastAPI + httpx (async GHL fetching with rate-limit backoff)
- gspread (Sheets upsert, keyed on contact_id)
- Deployed on Vercel, triggered by cron-job.org every 15 min

## Endpoints

- `POST /api/sync` — runs full sync (requires `x-cron-secret` header)
- `GET /api/sync/health` — health check

## Environment Variables (set in Vercel)

`GHL_PRIVATE_TOKEN`, `GHL_LOCATION_ID`, `GHL_PIPELINE_ID`,
`GOOGLE_SHEET_ID`, `CRON_SECRET`, `GOOGLE_SERVICE_ACCOUNT_JSON`

## Local development

1. Copy `.env.example` to `.env` and fill values
2. `pip install -r requirements.txt`
3. `python test_sync.py`

`test_sync.py` runs the full pipeline directly (no HTTP layer) and prints
the result; `verify_sheet.py` reads the Leads and Sync_Log tabs back to
confirm data landed.
