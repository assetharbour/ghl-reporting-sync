# Post-deploy audit, 23 Oct 2026 (about 19 hours after the 22 Oct deploy)

Read-only: live Google sheets, Evonex events, GHL contacts, opportunities and calendars. Nothing was changed. Aggregates only; no phone
numbers or client names were printed or stored.

## Healthy (checked and correct)
| Check | Result |
|---|---|
| Sync | 96 successful cycles in 24 h, none missed, no gap over 25 minutes, no errors; incremental shadow log shows 0 missed contacts |
| Leads sheet | 664 rows = 664 live GHL opportunities, none missing, none extra; header grew 62 to 65 columns; stages now "Client Contacted" (23) and "Post Appointment" (1); no blank stage |
| New tabs | Appointments, Field_Locks, Flags, Audit_State, Call_Evidence, Call_Activity all exist; no phone-number-like value in any of them |
| Appointments tab | 197 rows, identical to the live S3 calendar on every compared field (contact, booker, advisor, status, times, deleted); official booker present on 196 (calendar basis), the 1 blank is the unverified booking-widget booking, correctly flagged |
| Call evidence | 15 of 15 eligible appointments judged; an independent recompute (separate code path) agrees on all 15 |
| Flags | 7 open, no duplicate ids |
| Evonex capture | 3,162 events, webhook delay p50 0.6 s / p90 1.1 s / max 4.4 s; 128 duplicate deliveries correctly de-duplicated |
| Write-back (dry run) | 161 log rows since deploy, 0 failed, 0 stuck in "claimed", 0 writes to GHL (last_call_at / last_call_by are empty on every contact) |

## Findings that need a decision
1. **CRITICAL: most appointments are on calendars the reports do not read.** The reports read only the S3 Appointment Calendar (a calendar of type round robin in GHL, although the advisor is chosen by whoever books) (196 events in 120 days).
   The other calendars hold 508 more appointments with contacts (Ed Ford's personal calendar alone has 220). In the last 30 days: 91 of 249 appointments
   booked were on S3. For the sales team (Harry, Ava, Hugo): 81 on S3 against 160 on all calendars (Hugo 30 vs 80, Harry 18 vs 43, Ava 33 vs 37). Since
   21 Oct 13:25 nothing has been booked on S3 at all: 19 appointments were booked on 22 Oct on advisors' personal calendars (Ed Ford, Anthony Fox,
   Carl Gladman, Xavier Collin), by Hugo, Harry, Ava, Sarah Bennett, Georgie Wilde and Oliver Donoghue. Every one is a normal booking from the contact
   page, so the booker is available. As built, the Sales Team numbers (and the pay basis) undercount.
2. **Bug: Call_Activity is missing 16 Oct** (121 calls). The first captured day is treated as incomplete even when the whole history was read.
3. **Design: "showed without a call" can be a false alarm.** The match is by the client's number on file within 2 hours. In 4 of the 6 flags the advisor
   had real long calls to other numbers in the window (client used another number) or called more than 2 hours later (a 16-minute call 3 hours after).
4. **Write-back cannot do anything yet:** all 71 calls that matched a contact were skipped as `phase_empty`. Only 8 contacts have a call_phase at all,
   all "None"; nobody is in Admin Call or Advisor Call, so the GHL workflows that set it are not live. 25 of the 71 matched calls hit a phone number
   shared by 2 or 3 contacts (duplicate contacts), which live mode may have to skip as ambiguous.
5. **Inbound calls are not captured.** 131 recording events belong to calls with no outbound events (inbound calls). Missed-call follow-up (scope 2.2)
   and "missed" counts need the inbound call webhook switched on in Evonex.
6. Small robustness items: cancel events often carry a junk number ("911..." on 26 of 79), and the code relies on arrival order to pick the real one;
   4 calls have been "ringing" for two days (lost end event) and are never counted; 7 live appointments belong to contacts with no row in Leads (client
   name shows a dash).
7. User side: Fact Find Outcome and Docs Requested are empty on every contact (so full-fact-find numbers stay 0); Created By and Booked By fields are
   unused (no locks yet); 9 contacts have 2 opportunities.


## Decisions taken (Talha, 23 Oct) and what was done
- Advisor appointments come from the **S3 calendar only** (the other calendars are not advisor calls): no change to which calendars are read. Note: nothing has been booked on S3 since 21 Oct 13:25.
- Fixed: Call_Activity first-day bug; junk number choice (parseable, then most frequent, then smallest); calls that only rang for 15 minutes count as No Answer; connected calls that never ended count as connected with unknown talk time.
- Changed: call match window 120 to 180 minutes; evidence records the advisor's connected calls (a minute or more) to other numbers in the window; the flags say "To review" and mention them; flag labels say "(to review)".
- Added: `contact_name` on every appointment (bounded to 100 lookups per cycle), shown on the Sales and Flags pages when the client has no case in Leads; audit tab headers are extended automatically when columns are appended.
- Write-back stays in `dry_run`; GHL workflows are said to be built, but no contact we saw had call_phase set to Admin Call or Advisor Call (see the live test in the reply).
- Inbound call webhook: not needed.
