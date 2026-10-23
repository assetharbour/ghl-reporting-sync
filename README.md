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

## Evonex to GHL call write-back (`WRITEBACK_MODE`)

Read `docs/ghl/GHL_Backend_Writeback_Spec.md` and `docs/ghl/GHL_Workflows_S1_S6_Reference.md` first.
The backend writes FACTS (call status, attempt counter, last call date and caller) onto the GHL
contact; GHL workflows make every decision. Only the six fields in `WRITE_FIELD_IDS`
(`app/writeback_rules.py`) can ever be written, and a request containing any other field is refused.

Flow: an Evonex `ENDCALL` / `CANCELCALL` is stored as before and answered `200`; **after** the response
`app/writeback.py` waits out a 5 s settle period (Evonex sends a `CANCELCALL` about 1 s *before* the
`STARTCALL` on ~4 % of connected calls), rebuilds the call from `Evonex_Events`, classifies it
(no `STARTCALL` = No Answer; talk < 60 s = Voicemail; >= 60 s = Answered), finds the contact by
phone, claims the cid and contact in the `Writeback_Log` tab, reads the contact fresh, picks the
admin or advisor pair from `call_phase`, writes status + counter + `last_call_at` (London date) +
`last_call_by` in one `PUT`, then reads the contact back and records `written` /
`written_unverified`. `POST /api/evonex/writeback/sweep` (header `x-cron-secret`, also run by every
`/api/sync` call) retries calls whose inline attempt did not finish.

| Variable | Default | Meaning |
|---|---|---|
| `WRITEBACK_MODE` | `off` | `off` nothing runs; `dry_run` does everything except the PUT and logs what it would write; `on` writes. Any other value = off |
| `WRITEBACK_TALK_SECONDS` | 60 | Answered / Voicemail threshold |
| `WRITEBACK_SETTLE_SECONDS` | 5 | quiet time before a call is judged finished |
| `WRITEBACK_MAX_AGE_MINUTES` | 120 | older calls are never written |
| `WRITEBACK_ADVISOR_NAMES` | the 6 advisor-name options | callers allowed to write the advisor pair |
| `WRITEBACK_ADVISOR_STRICT` | false | true = only the case's own advisor writes the advisor pair |

`Writeback_Log` (private Evonex sheet) is the audit trail and idempotency store: statuses `claimed`,
`written`, `written_unverified`, `dry_run`, `skipped` (with a reason such as `phase_empty`,
`no_contact_match`, `ambiguous_contact`, `advisor_not_matched`, `too_old`), `released`, `failed`.
It holds ids, staff names and outcomes only, never customer phone numbers.

## Appointments, Created By / Booked By locking and flags (`FIELD_AUDIT`)

One audit cycle runs inside every `/api/sync` (after the Leads sync, can never fail it) when `FIELD_AUDIT=on`.
It only READS GHL and writes four tabs in the private Leads spreadsheet:

| Tab | Content |
|---|---|
| `Appointments` | one row per calendar appointment (`app/appointments.py`): who booked it in the calendar (`createdBy.userId`), the advisor (`assignedUserId`), booked_at, start_time, status, plus the OFFICIAL booker and its basis: `field` (the Booked By field was filled, first value locked) or `calendar` (field empty, the person GHL recorded on the calendar) |
| `Field_Locks` | the FIRST value ever seen: Created By per contact (the custom field, not GHL's native createdBy) and Booked By per appointment (`booked_by:<appointment_id>`, whenever the field is filled), plus the current value |
| `Flags` | append-only: `created_by_changed/cleared`, `booked_by_changed/cleared`, `booked_by_missing` (no field value and no usable calendar booker), `booked_by_mismatch` (field differs from the calendar booker), `advisor_mismatch`, `appointment_with_non_advisor`, `booking_source_not_trusted`; de-duplicated by `flag_id` |
| `Audit_State` | when the audit last completed |

`app/field_audit.py` is the pure engine, `app/audit_runner.py` the cycle, `app/audit_store.py` the sheet IO,
`app/sales_metrics.py` the KPI definitions (booked, due, showed, full fact find, conversion, appointments per advisor).
Variables: `FIELD_AUDIT` (off/on), `FIELD_AUDIT_SINCE` (appointments booked earlier are never flagged),
`APPOINTMENT_CALENDAR_IDS` (default the S3 Appointment Calendar), `BOOKED_BY_GRACE_HOURS` (24), `TRUSTED_BOOKING_SOURCES` (default `contactdetails_page`), `SALES_TEAM_NAMES` (Harry Kalsi, Ava Morris-Boundy, Hugo Sharpe).

## Call evidence and call activity (`CALL_EVIDENCE`)

Advisor "meetings" are phone calls made from Evonex at the appointment time (the calendar only drives reminders), so the audit can check each
finished appointment against the advisor's call. Needs `FIELD_AUDIT=on`, `CALL_EVIDENCE=on` and `EVONEX_SHEET_ID`. Pure engine: `app/call_evidence.py`;
the step runs inside the audit cycle (`call_evidence_step` in `app/audit_runner.py`) and can never stop the rest of the audit. It reads the private
Evonex sheet and the contacts' phone numbers (in memory only) and writes two more tabs in the Leads spreadsheet, with no phone numbers:

| Tab | Content |
|---|---|
| `Call_Evidence` | one row per finished appointment: result (`long_call`, `conversation`, `short_call`, `not_answered`, `other_caller`, `no_call`, `no_phone`), advisor calls, connected calls, best talk seconds, minutes from the start to the first connected call, caller |
| `Call_Activity` | outbound calls per London day per caller: calls, connected, conversations, long calls, no answers, talk seconds |

Rules: a call counts when it started within `CALL_MATCH_WINDOW_MINUTES` (180) either side of the appointment start, to the contact's number, by the
appointment's advisor (names matched like Evonex spells them). A connected call of `CALL_CONVERSATION_SECONDS` (60) is a conversation and of
`CALL_LONG_SECONDS` (600) a long call (a first guess from the first week of calls; revisit when Asset Harbour says how long a fact find runs). An appointment is
judged only once its window has closed (+15 min) and only if the Evonex history reaches back before the window, so history that starts later never produces false
"no call" results. A row is recomputed until 36 hours after the window closed, then frozen. The calendar status is never changed: three flags are raised
instead, once per appointment: `showed_without_call`, `noshow_with_call`, `unmarked_with_call` (a day after the appointment). First run reads up to 12,000
event rows and looks back 30 days; later runs read 1,500 rows and look back 4 days.

### Speed to lead (`Lead_Speed`, part of the call evidence step)

`app/lead_speed.py` measures how long each new lead waits for its first call and its first connected call (Evonex outbound calls to the contact's number,
from the opportunity's creation). The response clock starts when the office opens (`LEAD_SPEED_OFFICE_START` 8 to `LEAD_SPEED_OFFICE_END` 18, Monday to Friday,
Europe/London; bank holidays count as working days), so a lead that arrives overnight is not late and a call before opening counts as 0 minutes. Existing
Clients are not new enquiries and are skipped; so are leads created before the Evonex history began. Rows (`Lead_Speed` tab): status (`called`, `not_called`
after an hour, `waiting`, `no_phone`), first caller, minutes to first call, first connected caller and minutes. A row is final once a call connected, or after
14 days; leads older than 30 days are not re-judged. The opportunities list is fetched at most about once an hour for this.

Optional `BOOKER_OWNER_FLAG=on` raises `booker_not_owner` when the official booker is not the contact owner (the sales person), judged only in the first
`BOOKER_OWNER_WINDOW_HOURS` (48) after booking. Off by default: in the first week of real data 31 of 37 bookings had another owner (Anita, Alison, Sarah ...).

## QA

The 22 Oct 2026 QA report (approach, results, defect log, UAT checklist, residual risks, how to repeat) is `docs/qa/QA_REPORT_2026-10-22.md`.
