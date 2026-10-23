# Deployment checklist (nothing here has been done; it starts only when Talha says go)

Two repos, both on `main`, nothing committed or pushed yet:
- backend `ghl-reporting-sync`: remote `client` = github.com/assetharbour/ghl-reporting-sync (deploys to assetharbour-reports.vercel.app)
- dashboard `ghl-dashboard`: remote `origin` = github.com/assetharbour/ghl-dashboard (auto-deploys to reports.assetharbour.com)

Order matters: backend first (so the new sheet tabs and columns exist), dashboard second. The dashboard works before the audit has
run (empty states), so a short gap is safe.

## A. Before you push (your side, in GHL)
- [ ] Look at the local preview (http://localhost:5173/) page by page and note anything you want changed.
- [ ] Decide D1 to D8 in `docs/REQUIREMENTS_TRACEABILITY.md` (none blocks the deploy; D2, D3 and D7 change what staff see).
- [ ] Add **Booked By** to the S3 calendar booking form and make it required (client ask). Nothing breaks if you do it later: the calendar booker is used when the field is empty.
- [ ] Decide whether a GHL automation will auto-fill **Created By** at stages 1 to 3 (client ask). Until then the report shows "(not recorded)".
- [ ] Tell the sales team the rules: Booked By filled = that value is locked; later edits are flagged.

## B. Backend deploy (Vercel project for ghl-reporting-sync)
- [ ] Commit and push `ghl-reporting-sync` to `client main`.
- [ ] Add environment variables, then redeploy:
  - `FIELD_AUDIT=on`
  - `FIELD_AUDIT_SINCE=<go-live date, e.g. 2026-10-26>` (appointments booked before it are never flagged)
  - `WRITEBACK_MODE=dry_run` (NOT `on` yet)
  - `CALL_EVIDENCE=on` (needs `EVONEX_SHEET_ID`, already set for the Evonex receiver; same Vercel project). First run reads the Evonex history and judges appointments since the webhooks started (16 Oct)
  - leave `BOOKER_OWNER_FLAG` off for now (it would flag most bookings today)
  - leave the other `WRITEBACK_*`, `APPOINTMENT_CALENDAR_IDS`, `TRUSTED_BOOKING_SOURCES`, `SALES_TEAM_NAMES` at their defaults
- [ ] Make sure the existing cron-job.org sync job still runs every 15 minutes. Optionally add a second job: `POST /api/evonex/writeback/sweep` with header `x-cron-secret` (the sync also runs the sweep, so this is only a safety net).
- [ ] After the first sync: Leads tab header grew from 62 to 65 columns; tabs `Appointments`, `Field_Locks`, `Flags`, `Audit_State` exist in the Leads sheet; `Sync_Log` last row is success; `/api/sync/status` is healthy.
- [ ] Stage text in the Leads sheet is rewritten once ("Admin Contacted" becomes "Client Contacted"; "Post Appointment" appears). Expected.
- [ ] Check the tabs `Call_Evidence` (about 15 to 20 rows to begin with) and `Call_Activity` exist and hold no phone numbers; `Flags` may show a few `showed_without_call` rows (real mismatches, review them with Sara).
- [ ] Check `Appointments` has about 200 rows with an official booker filled in and `Flags` is small (only real mismatches).

## C. Dashboard deploy
- [ ] Commit and push `ghl-dashboard` to `origin main` (auto-deploys). No new environment variables.
- [ ] Open reports.assetharbour.com, sign in, click every page: Overview, Leads Received, Introducer Reports, Call Tracking, Sales Team, Advisor Performance, Case Management (new ERC Renewal Forecast), Gapping, Flags.
- [ ] Sales Team: booked totals match the Appointments tab; Booked By shows the field value or the calendar person; Source column says "Booked By field" or "Calendar".
- [ ] Hard refresh once on staff browsers (the bundle changed).

## D. Evonex write-back (separate, after A to C are stable)
- [ ] Dry run for at least one working day: read `Writeback_Log` (private Evonex sheet) rows with status `dry_run`; check contact matches, outcome (No Answer / Voicemail / Answered) and the intended field values look right on a handful of real calls.
- [ ] Check the GHL workflows S1 to S6 react correctly to a test contact before real writes.
- [ ] Set `WRITEBACK_MODE=on`, redeploy, watch the first hour: `written` rows, no `failed`, no `written_unverified`.
- [ ] Rollback for anything odd: set `WRITEBACK_MODE=off` (nothing else needs to change).

## E. User acceptance (UAT U1 to U8 in `docs/qa/QA_REPORT_2026-10-22.md`)
- [ ] U1 to U5 (live write, value formats, S1 workflow trigger, one dry-run call, one real call) are the write-back checks in section D.
- [ ] U6: first sync after deploy extends the Leads header and creates the four new tabs (section B).
- [ ] U7: one booking made from the contact page with Booked By filled, one with it empty, one from the calendar form. Official booker and basis (`field` or `calendar`) are right in `Appointments`; changing Booked By afterwards raises `booked_by_changed` and the report keeps the first value.
- [ ] U8: walk the Sales and Flags pages with Sara or Andy; numbers agree with their manual count for one week.
- [ ] After a staff member sets **Fact Find Outcome** on a test contact, the Full fact finds card and the booked-to-full-fact-find percentage move off 0.

## F. Housekeeping
- [ ] Rotate the GHL private token, the cron secret and the Evonex webhook token (they were pasted into chat; the Evonex receiver accepts a comma-separated list, so rotation has no downtime).
- [ ] Delete the old browser Google API key in Google Cloud Console (context file section 14).
- [ ] Update `ASSET_HARBOUR_REPORTING_CONTEXT.md` section 23 to "deployed" with the date.

## G. Open client-side items (not part of this deploy)
GHL workflows and fields for 1.10 (Contacted 1 to 6), 1.12 (auto move to Post Appointment), 1.14 (Docs Requested), 2.2 (missed-call text and task), protection status / disclaimer / 20-day task (3.5 to 3.7), Finding Property stage, Interested Not Ready reason, junk lead cleanup, Looms. Dashboard items still to build: Evonex call KPIs (1.11, 2.5), showed-to-documents conversion, revenue per salesperson (1.16), product type (needs D4).


## H. Leadership board (built 23 Oct, local only until approved)
- [ ] Talha checks http://localhost:5173/leadership locally and approves.
- [ ] Backend first: push `ghl-reporting-sync` (adds the `Lead_Speed` tab; no new environment variables, office hours default to 08:00 to 18:00).
- [ ] After the next sync: tab `Lead_Speed` exists, about 25 to 30 rows, no phone numbers; `Sync_Log` success.
- [ ] Then push `ghl-dashboard`; open /leadership on reports.assetharbour.com and compare the headline numbers with Overview and Sales Team for the same range.
