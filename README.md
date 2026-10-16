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

- `POST /api/sync` — requires `x-cron-secret` header. Responds immediately
  (`{"status": "accepted"}`) and runs the sync in the background. This is
  what cron-job.org should call — its 30s client timeout no longer matters,
  since the response returns in under a second regardless of how long the
  underlying sync takes.
- `POST /api/sync?wait=true` — same auth, but runs the sync synchronously
  and returns the full result JSON (`opportunities_fetched`, `rows_updated`,
  etc). Use this for manual `curl` testing and debugging — not for the cron
  schedule, since it can take 20–50s and will re-trigger client timeouts.
- `GET /api/sync/health` — health check

**`Sync_Log` is the source of truth for whether a sync actually succeeded.**
A `200 {"status": "accepted"}` from the cron endpoint means the request was
accepted, not that the sync completed — always check `Sync_Log` (or the
dashboard's "Last updated" pill) to confirm. Every cycle writes exactly one
of `running` (claimed at start) → `success` or `failed` (final outcome).

**Overlap lock:** if a sync is still `running` and under 10 minutes old when
a new request comes in, the new request returns `{"status": "skipped",
"reason": "previous sync still running"}` without starting another sync. A
`running` row older than 10 minutes is treated as a crashed run and ignored.

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

## Incremental sync (`INCREMENTAL_SYNC`)

By default every cycle re-reads every contact and rewrites every row. Setting
`INCREMENTAL_SYNC` changes that (it is read from the Vercel environment; a redeploy
is needed after changing it):

| Value | Behaviour |
|---|---|
| unset / `off` | Always the full sync described above. |
| `shadow` | Still a full sync, but each run also logs what an incremental run would have missed: `shadow since=... contacts_changed=N planned=P true_changes=T missed=M`. `missed` must stay 0 before switching on. |
| `on` (`1`/`true`/`yes`) | Incremental: list all opportunities (new, moved, deleted, stage/status changes), ask GHL for contacts changed since the last successful sync (minus a 10-minute overlap) via `POST /contacts/search` on `dateUpdated`, and rebuild/write only rows that actually changed. |

Contact-level fields (admin, advisor, call status...) live on the contact and do **not**
move the opportunity's `updatedAt`, which is why changed contacts are queried
separately. Safety nets in `on` mode: one full refresh per day (first run after
`FULL_REFRESH_HOUR_UTC`, default 3), a full sync if there is no successful sync in the
last 24 h, if the changed-contact lookup fails or is incomplete, or if the Leads header
row differs from `COLUMNS`. `POST /api/sync?full=true` forces a full sync.

In `on` mode `Sync_Log.records_processed` is the number of rows **written**, and an
unchanged row keeps its old `last_synced` (the daily full refresh rewrites them all).
Each run logs one line: `sync mode=... opportunities=... rows_written=... rows_removed=... ms=...`.

Tests: `python -m pytest` (needs `requirements-dev.txt`).
