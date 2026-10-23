# Requirements traceability: what the client asked for vs what is built (22 Oct 2026)

Sources checked: the project scope doc "Sales Team Reporting, Evonex and Protection Scope" (items 1.1 to 3.9), the 6 Oct
reporting meeting summary, Sara's 9 Oct and 13 Oct emails, the 12 Oct recap, the 14 Oct "AH - New Build" thread, the 18 to 22 Oct
tickets (fact find outcome options, annual review calls, the 22 Oct ask for a round robin, conversion rate, "sales team = Harry, Ava, Hugo"), the S1 to S6
workflow reference and the Evonex write-back spec, and the context file.

Status key: **DONE** = built, tested, shown on the dashboard. **BUILT, NOT LIVE** = built and tested but switched off until deploy.
**PARTIAL** = part of the ask is there. **GHL** = the work is a GHL configuration or workflow change, not dashboard code.
**NOT BUILT** = nothing exists yet. **DECISION** = needs an answer before building.

Nothing below is deployed yet. "DONE" means done in the code you can look at on the local preview.

## Phase 1: Sales team reporting (scope 1.1 to 1.18)

| # | Requirement | Status | Where / what is missing |
|---|---|---|---|
| 1.1 | Booked By field; every report filtered by it | **DONE** (+GHL) | Field created 20 Oct. Rule (22 Oct): if the field is filled it is used and its first value is locked; if empty, the person GHL recorded on the calendar is used. Older bookings get their booker from the calendar automatically. A mismatch or a later edit raises a flag. GHL to do: add the field to the calendar booking form, make it required. Locking a field against editing is not possible in GHL, so flags are the control. |
| 1.2 | Fact find tracking: outcome after Appointment Booked, Full Fact Find dropdown, fact find date | **PARTIAL** | Showed / no-show / cancelled come from the calendar status. `Fact Find Outcome` and `Fact Find Date` fields exist and are synced. Every full-fact-find number is 0 today because nobody has filled the outcome yet. The live dropdown has 5 options; the scope doc lists 4 different ones (see D7). |
| 1.3 | Fact find sales report (client, date, Shown, Booked by, lead type; totals per advisor and salesperson per day/week/month) | **DONE** | Sales Team page: appointment list with client, booked on, appointment date, booked by, advisor, case type, lead source, status, fact find date, full fact find, completed; CSV export; totals by booker and advisor; per-week charts; per-month full-fact-find tables by advisor and by rep. A per-day chart does not exist (use the date range for a single day). |
| 1.4 | Appointments per salesperson: booked, date booked, appointment date, product type (Mortgage / Protection) | **PARTIAL** | Everything except product type. The calendar event does not say Mortgage or Protection. Needs a decision on where product type comes from (D4). |
| 1.5 | Booked date AND appointment date on reports, filter by either | **DONE** (22 Oct) | Both dates are shown, and the Sales page has a "Booked date / Appointment date" switch that decides which one the date range filters (KPIs, tables, weekly charts, round robin, list). The appointment date is the calendar start time: no contact field holds it. The old Advisor Performance "Appointments Booked" figure is still based on call status (D1). |
| 1.6 | Appointments per advisor for any day/week/month and daily busy view | **PARTIAL** | Per-advisor table for any date range, and a day-by-day view of next week. No daily view for an arbitrary past or future day. |
| 1.7 | Distribution check (asked for as "round robin") | **DONE** (reworded 23 Oct) | The advisor appointments are NOT a round robin: whoever books in GHL chooses the advisor. The Sales page chart is now "Appointments per advisor" (who each appointment was booked with), with no fair-share line or fairness wording. |
| 1.8 | Forward week view (Monday to Friday) | **DONE** | "Next working week" cards, per day and advisor. Shown every day, not only Fridays. |
| 1.9 | No-shows and rebooks by salesperson and week | **DONE** | Per-week no-show chart; rebooked count per rep. |
| 1.10 | Contact attempt stages Contacted 1 to 6 | **NOT BUILT** (GHL) | Needs new GHL stages and a workflow driven by the attempt counter. The Evonex write-back (built, not live) keeps that counter up to date, so this can follow. Held earlier for the Evonex decision. |
| 1.11 | Calls to appointments per salesperson | **DONE** (22 Oct, needs `CALL_EVIDENCE=on`) | Sales page "Call activity": calls per person per date range, with appointments booked and calls per booking for Harry, Ava and Hugo. Outbound calls only. Also "Advisor calls against appointments": each finished appointment checked against the advisor's Evonex call. |
| 1.12 | Automatic move to Post Appointment the day after the appointment | **NOT BUILT** (GHL) | The stage exists and is now mapped in the sync and dashboard. The automatic move is a GHL workflow. |
| 1.13 | Filters (date range, salesperson, advisor, product), Loom, sales team guide | **PARTIAL** | Date range, salesperson and advisor filters done. No product filter (no product data). Loom videos and the guide are not done. |
| 1.14 | Ready / Docs Requested change and showed-to-documents conversion | **PARTIAL** (GHL) | `Docs Requested` field exists and is synced. The workflow change is GHL. The showed-to-documents conversion figure is not on the dashboard yet. |
| 1.15 | Review of existing reports after the stage change | **DONE** | This session. See "Unification review" below. Open decisions D1, D2. |
| 1.16 | Revenue per salesperson | **NOT BUILT** | Flagged by the client as a later phase. |
| 1.17 | Stage colour coding, team Looms | **NOT BUILT** (GHL) | Not dashboard work. |
| 1.18 | Created By: set to whoever adds the lead, never changes, leads added and appointments booked per salesperson per month | **DONE** (+GHL) | Custom field created; first value locked; changes and clearing flagged; Sales page table "leads added by who created them" with appointments booked and full fact finds. Shows "(not recorded)" until staff or a GHL automation fills the field. Use the This month / Last month presets for the monthly view. |

## Phase 2: Evonex to GHL (scope 2.1 to 2.6)

| # | Requirement | Status | Where / what is missing |
|---|---|---|---|
| 2.1 | Automatic call logging on the GHL contact | **BUILT, NOT LIVE** | Write-back writes the call result, attempt counter, last call date and caller to six contact fields; off by default, `dry_run` first. Webhook capture into the private Evonex sheet is already live. |
| 2.2 | Missed call follow up (text back, task) | **NOT BUILT** (GHL) | The write-back sets `last_missed_call_at`; a GHL workflow can send the text and task from that. |
| 2.3 | Recordings and AI summaries on the record | **NOT BUILT** | Recording-ready events are captured. Needs the Content Access API key from SCG (not yet requested). |
| 2.4 | Screen pop | **SCG / Evonex setting** | Not our code. |
| 2.5 | Call activity reporting on the Sales tab | **PARTIAL** (22 Oct, needs `CALL_EVIDENCE=on`) | Calls, connected, answer rate, conversations, long calls and talk time per person. "Missed" means outbound calls nobody answered; inbound missed calls are not captured (the webhook feed used here is outbound only). |
| 2.6 | Testing and go live | **PENDING** | After deploy: dry run, then switch on. |

## Phase 3: ERC, introducer, protection (scope 3.1 to 3.9)

| # | Requirement | Status | Where / what is missing |
|---|---|---|---|
| 3.1 | ERC date import | **DONE** (confirm) | 123 cases carry an ERC Date. Confirm with Sara that no unmatched-rows list is outstanding. |
| 3.2 | ERC forecast: next 6 months, across 2027, per month, by advisor | **DONE** (22 Oct) | Case Management, "ERC Renewal Forecast": next 6 months, all of next year, month-by-month chart, advisor filter, click to see clients. Only as complete as the ERC dates in GHL. |
| 3.3 | Introducer revenue: which fields feed it, protection included? | **PARTIAL** | Revenue = broker fee + procuration fee + solicitor commission; protection is not included; blank is not counted as 0. Andy's reconciliation example (48 leads, £9,904) is from 6 Oct and no longer reproducible (Existing Client is now 130 leads). A reply to Andy is still needed. |
| 3.4 | Weekly introducer report without commissions | **PARTIAL** | Per-introducer PDF reports exist. Confirm whether the sample without commissions was sent and approved. |
| 3.5 | Protection status dropdown | **NOT BUILT** (GHL) | Needs Andy's confirmation of the statuses. |
| 3.6 | Disclaimer sent field and date | **NOT BUILT** (GHL) | Same. |
| 3.7 | 20-day protection follow-up task from Application Submitted | **NOT BUILT** (GHL) | Same. |
| 3.8 | Protection follow-up report by advisor | **NOT BUILT** | Needs 3.5 to 3.7 first. |
| 3.9 | Testing and handover | **PENDING** | |

## Leadership board (requested 23 Oct 2026)

| Ask | Status | Notes |
|---|---|---|
| One clean, insightful leadership board with good charts | **DONE** (local, not deployed) | `/leadership`: headline cards, honours, leaderboards (sales team, advisors, handover team), speed to lead, funnel, 12-week trend, top introducers, needs attention |
| Who is on top in each of the three teams | **DONE** | Sales team by appointments booked; advisors and handover team by completions in the period |
| Speed to lead | **DONE** (data since 16 Oct) | New `Lead_Speed` tab (backend deploy needed). Only 27 new leads have call data so far, so per-person speed shows from three first calls |
| Decisions | **OPEN** | D11 Post Completion cases count as completed? (other pages say no); D12 office hours 08:00 to 18:00 Monday to Friday; D13 "Existing Client" appears as a top introducer, as on the Introducer page |

## Dashboard fixes agreed on 6 Oct

| Item | Status | Notes |
|---|---|---|
| Active cases (open, last 30 days, filter by advisor; no abandoned/lost) | **DONE** (existing) | |
| Completions: a case moved back from Completion must not count | **PARTIAL** | Pipeline reports count by current stage, so a moved-back case drops out. The Sales page "Completed" also accepts a recorded completion date, so a case moved back after a completion date was set would still count there (D6). The activity log is not built. |
| Junk and test leads | **GHL data** | Clean up in GHL. |
| Stuck-case flag: new ownership, drop Interested Not Ready, Appointment Booked only after the date has passed | **DONE** (22 Oct) | Sales reps are now expected owners at stages 1 to 3; Interested / Not Ready is excluded; an Appointment Booked case is flagged only once its calendar appointment has passed (no calendar appointment: the 30-day rule applies). |
| Finding Property as its own stage | **DECISION / GHL** | Still a dropdown value plus a dashboard proxy. |
| Interested Not Ready secondary reason | **GHL** | Needs a GHL field. |
| Stage 1 to 3 slowness (20 to 30 min) | **GHL** | Not dashboard. |
| Case management report weekly | **DONE** (existing) | Case Management page. |

## Unification review (user request 22 Oct: remove double or outdated logic)

Changed (clear, low risk):
- **Stage names**: "Admin Contacted" is now "Client Contacted" and the new "Post Appointment" stage is mapped (sync, funnel, stuck rule). Before, Post Appointment cases had a blank stage in the sheet.
- **Expected case owners**: Ava, Harry and Hugo added (S1 assigns them at stages 1 to 3). The Admin Assignment text now describes the current handover.
- **Product end date** is the ERC Date everywhere (Call Tracking annual review, Case Management roll-off), with the old roll-off date only as a fallback (5 of 123 cases had that one).
- **Leads Received "Appointments Booked"** now counts calendar appointments (same as the Sales page); the old call-based figure is shown next to it as "Calls Logged".
- **Stuck rule** (above), **Booked By rule** (above), date-basis caption under the date picker on every page.
- **Sidebar order** follows the journey: Overview, Leads Received, Introducer Reports, Call Tracking, Sales Team, Advisor Performance, Case Management, Gapping, Flags.

Left unchanged on purpose (major or unsure):
- **D1** Advisor Performance "Appointments Booked / Completed" are still call-status based. Their sub-labels now say so. Switching them to the calendar changes advisor numbers the client already knows.
- **D2** Sarah Bennett owns 87 cases but is not in any documented workflow, so those show under "Admin assignment needs review". Add her as an expected owner?
- **D3** Advisor list: Carl Gladman and Anthony Fox have had no appointments on the S3 calendar, and Anita / Ava / Harry / Hugo appear as advisors on a few. Decide who is an active advisor.

## Decisions for Talha (none blocks the staged deploy)

| # | Decision |
|---|---|
| D1 | Move Advisor Performance appointment KPIs to the calendar? |
| D2 | Is Sarah Bennett an expected case owner? |
| D3 | Active advisor list for the appointments-per-advisor chart. |
| D4 | Where does product type (Mortgage / Protection) come from per appointment? |
| D5 | (resolved 22 Oct: appointment-date mode added) |
| D11 | Completions: the Completion stage only (14 today, the same on every page), or Completion plus Post Completion (20)? 6 cases sit at Post Completion, which only follows a completion. |
| D9 | Fact find call length: the matching treats 10 minutes or more as a long call and 1 minute or more as a conversation (first guess from the first week of calls). Confirm with Asset Harbour how long a fact find usually runs. |
| D10 | Booker must be the lead's owner: built but OFF (`BOOKER_OWNER_FLAG`) because 31 of 37 recent bookings would be flagged. Turn on once stage 1 to 3 ownership is clean. |
| D6 | Should a recorded completion date still count if the case was moved back? |
| D7 | Fact Find Outcome options: scope doc (Review, Amendments, Partial FF, Full Fact Find Completed) vs the live 5-option field. Reports count outcomes starting "Full Fact Find". |
| D8 | Rotate the secrets that were pasted into chat, and delete the old browser Google API key (see context file section 10). |
