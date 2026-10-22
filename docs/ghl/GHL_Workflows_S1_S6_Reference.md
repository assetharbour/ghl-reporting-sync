# Asset Harbour: GHL Workflows S1 to S6 Reference

Version: 22 Oct 2026. Owner: Talha (GrowMySME).

How the GoHighLevel (GHL) workflows for stages 1 to 6 work after the v2 rebuild: what starts each one, which fields it reads and writes, where it moves the case, and how it depends on the call fields the backend writes. Backend rules are in `GHL_Backend_Writeback_Spec.md`.

---

## 1. The system in one page

- **Admin based.** Every task goes to the contact's **assigned user** (the admin / sales rep who owns the case). Advisors only make the fact find call from Evonex.
- **Fields, not tags.** States are fields or pipeline stages. Two tags remain on purpose: `if:appt-cancelled` (S3 cancel checks) and `if:docs:missing` (marker only, not a trigger).
- **Calls drive stages 1 to 3.** The backend writes call results (section 4 of the spec). The workflows react to the attempt **counter** changing ("has changed"), then read the status.
- **Pause Automations** (`If90lxAQst0DbYqHG15k`, multi-select): every workflow checks it first and stops if its stage is ticked. Options used: Pause Lead Received, Pause Admin Contacted, Pause Appointment Booked, Pause Post Appointment, Pause Docs Requested, Pause Docs Received / Full Review.
- **Working hours.** Tasks wait for Mon to Fri 08:00 to 19:00 ("Check Weekend Before The Task Assign" steps or a workflow-level window).
- **Notifications and most SMS steps are switched off during the build** (Talha's choice). Tasks carry the work.
- **Limits.** 6 missed admin calls, 6 missed advisor calls, 6 document chases, 5 missing-document chases, each then moves the case to Interested / Not Ready. The S2 booking reminder and S3 rebook reminder loops have no cap on purpose (the admin sets Not Ready to stop them).

### Case lifecycle

```
New opportunity (Lead Received)
  S1 Lead Intake: assign rep, call_phase = Admin Call, first call task
  Evonex admin calls  -> backend writes admin_call_status + admin_call_attempt_count
  S1 Admin No Answer Logic
     Answered            -> Client Contacted
     6 misses            -> Interested / Not Ready
     miss (< 6)          -> follow-up task next working day
Client Contacted
  S2 Client Contacted: "Book Fact Find Meeting" task every 2 days
     meeting booked in "S3 Appointment Calendar" -> S3 takes over
     client_readiness_status = Not Ready         -> Interested / Not Ready
Appointment Booked
  S3 Appointment Booked: reminders 48 h / 24 h / 2 h, call_phase = Advisor Call at 2 h
  Evonex advisor calls -> backend writes advisor_call_status + advisor_call_attempt_count
     Answered            -> Post Appointment
  S3 Advisor No Answer: miss (< 6) -> follow-up task; 6 misses -> Interested / Not Ready
  S3 Appointment Cancel: cancellation -> rebook task every 2 days, or Not Ready -> Interested / Not Ready
Post Appointment
  S4 Post Appointment: admin sets Fact Find Outcome + Docs Requested = Yes -> Docs Requested
Docs Requested
  S5 Docs Requested Handler: request email, chase every 3 days
     docs_status = Complete -> Docs Received / Full Review
     6 chases              -> Interested / Not Ready
Docs Received / Full Review
  S6 Docs Received Handler: pick review admin (Admin-Handover), review task
     docs_status = Complete -> Advisor Recommendation
     docs_status = Partial  -> S6 Missing Docs Chase (5 chases, then Interested / Not Ready)
```

---

## 2. Workflows

Status key: **Draft** = new clone, not published yet (goes live at cut-over). **Live** = published and running now.

### S1. New - WF - S1 Lead Intake (Draft, `0b7900e5-a403-4297-9977-93282f2515b2`)
- **Trigger:** Opportunity Created, pipeline "Mortgage – Enquiry to Completion", stage Lead Received.
- **Window:** Mon to Fri 08:00 to 19:00.
- **Flow:** Pause check (Pause Lead Received). Tag `ppc gme` or `pdf_download` = exit (a separate automation handles those leads). Round robin to 3 sales reps. Update fields: call_phase = Admin Call, admin_call_status = Pending, admin_call_attempt_count = 0. Reset case fields for a repeat enquiry: Appointment Booked = No, Booked By empty, Fact Find Outcome empty (added 22 Oct, export not yet re-checked). Welcome email. Task "S1 – Evonex Call (5 Min SLA)" due same day.
- **Backend link:** after this, admin calls are written to the admin pair.

### S1. New - WF – S1 Admin No Answer Logic (Draft, `4199089c-7090-4459-8554-7db31be1fd55`)
- **Trigger:** Contact Changed, admin_call_attempt_count has changed.
- **Flow:** Pause check. Find opportunity at Lead Received (else end). Branch on admin_call_status:
  - **Answered:** call_phase = None, stage = Client Contacted.
  - **No Answer or Voicemail:** count >= 6 = stage Interested / Not Ready, call_phase = None. Else wait 24 h, then: if last_call_at is Today (a newer call has its own run) or stage is no longer Lead Received = end; otherwise follow-up task "S1 – Follow-Up Call (Attempt N missed)".
  - Anything else: end.
- **Backend link:** needs status and counter in the same update; needs last_call_at as today's UK date.

### S2. New - WF – S2 Client Contacted Handler (Draft, `6a271e92-0f12-4acf-b434-fe59cca2dde2`)
- **Trigger:** Pipeline stage changed to Client Contacted.
- **Flow:** Pause check (Pause Admin Contacted). Safety: stage = Client Contacted. If Appointment Booked = Yes (booked on the first call) = end. Else client_readiness_status = Pending, task "S2 – Book Fact Find Meeting with Advisor", wait up to 2 days for Appointment Booked = Yes or client_readiness_status = Not Ready:
  - Booked = end (S3 runs).
  - Not Ready = stage Interested / Not Ready.
  - Timeout = new task, wait again (no cap).
- **Backend link:** none. call_phase is None here, so calls are logged only.

### S3. New - WF – S3 Appointment Booked Handler (Draft, `fa6df9b0-826f-43a1-977f-4869582344f8`)
- **Trigger:** Appointment Status in "S3 Appointment Calendar" (event type Normal; booked status filter to confirm).
- **Flow:** Remove tag if:appt-cancelled. Pause check (Pause Appointment Booked). Update fields: advisor_call_status = Pending, Appointment Booked = Yes, Fact Find Date = meeting date, advisor_call_attempt_count = 0, call_phase = None. Stage = Appointment Booked. Confirmation email. If Booked By empty = task "S3 – Set Booked By". Reminder emails at 48 h, 24 h and 2 h before the meeting, each after a check for tag if:appt-cancelled (cancelled = end). At 2 h: call_phase = **Advisor Call**, estate agent task if is_estate_agent_lead = Yes, advisor call task, "[S3] Fact Find Today" task to the assigned user. Then wait for **advisor_call_status = Answered** (1 day):
  - Answered: stage = Post Appointment, call_phase = None.
  - Timeout: stage is not Appointment Booked = end; advisor_call_status = Pending (no call yet) = task "[S3] No Advisor Call Logged" and wait again; otherwise (misses being chased by S3 Advisor No Answer) wait again without a task.
- **Status:** fix to "Answered only" and the timeout branch reported done 22 Oct; export not yet re-checked.
- **Backend link:** the advisor pair is only written while call_phase = Advisor Call (from 2 h before the meeting).

### S3. New - WF – S3 Advisor No Answer (Draft, `db1ff55d-c5d1-4cd6-bdf7-4b9a0573c726`)
- **Trigger:** Contact Changed, advisor_call_attempt_count has changed (the extra Appointment Status trigger was deleted 22 Oct).
- **Flow:** Pause check (Pause Appointment Booked). Find opportunity at Appointment Booked (else end). If advisor_call_status = No Answer: count >= 6 = stage Interested / Not Ready, call_phase = None. Else wait 24 h; if last_call_at is Today or stage is no longer Appointment Booked = end; otherwise task "S3 – Advisor Follow-Up Call" to the assigned user. Answered = nothing here (S3 Appointment Booked handles it).

### S3. New - WF – S3 Appointment Cancel Handler (Draft, `15a9bc2b-dcac-4d65-8411-7c6aec69f3c7`)
- **Trigger:** Appointment Status = cancelled, in "S3 Appointment Calendar", contact only, event type Normal.
- **Flow:** Pause check. Add tag if:appt-cancelled. Cancellation email to client. Update fields: Appointment Booked = No, Fact Find Date cleared, call_phase = None, client_readiness_status = Pending. Remove the contact from S3 Appointment Booked Handler. Task "S3 – Rebook Fact Find". Wait up to 2 days for Appointment Booked = Yes (rebooked, S3 restarts) or Not Ready (= Interested / Not Ready); timeout = reminder task and wait again (no cap).
- **Backend link:** call_phase = None after a cancellation, so advisor calls are no longer written.

### S4. New - WF – S4 Post Appointment (Draft, `4078cfab-5a16-46b7-bfed-40615de85837`)
- **Trigger:** Pipeline stage changed to Post Appointment.
- **Flow:** Pause check (Pause Post Appointment). Find opportunity at Post Appointment. Docs Requested = No. Task "Post Appointment – Confirm Fact Find Outcome". Wait 1 day for Docs Requested = Yes; then reminder task and wait 2 more days; then a "Case stuck" note and a 30 day wait for a late Yes. Every Yes goes through "Check Fact Find Outcome": empty = daily task "Set Fact Find Outcome" until set; set = stage Docs Requested.
- **Backend link:** none.

### S5. WF – S5 Docs Requested Handler (Live, `617b2384-5453-4a29-b498-f50fc81aeba4`, v40)
- **Trigger:** Pipeline stage changed to Docs Requested.
- **Window:** Mon to Fri 08:00 to 19:00.
- **Flow:** Pause check (Pause Docs Requested). Assign to the admin team user. Safety: stage = Docs Requested. docs_status = Pending, docs_chase_attempt_count = 0. Document request email. Task "S5 – Chase Documents". Count +1. Wait 3 days for docs_status = Complete:
  - Complete = stage Docs Received / Full Review.
  - Timeout: count >= 6 and still Docs Requested = stage Interested / Not Ready + task "S5 – Case Paused"; still Docs Requested = count +1, task "S5 – Documents Still Outstanding", wait again; other stage = end.

### S6. WF – S6 Docs Received Handler (Live, `2b5ab6d4-b1f4-49e2-ae06-25d3f4ae6797`, v31)
- **Trigger:** Pipeline stage changed to Docs Received / Full Review.
- **Window:** Mon to Fri 08:00 to 19:00.
- **Flow:** Pause check. **docs_status = Pending** (required: cases arrive with Complete from S5). Task "Assign Admin for Stage 6 Review" until Admin-Handover has a value (reminder every day). Admin-Handover = Ben Robertson / Lewis Flude / Alison Gulliver reassigns the contact to that admin (None ends: the field only has these three options). Safety: stage = Docs Received / Full Review. Task "S6 – Full Document Review". Wait 2 days for docs_status = Complete or Partial:
  - Complete = stage Advisor Recommendation.
  - Partial = tag if:docs:missing (S6 Missing Docs Chase starts from the docs_status change).
  - Timeout = back to Safety (stage recheck), task again.

### S6. WF – S6 Missing Docs Chase Logic (Live, `b29b4604-9f3a-460f-b83a-2ef2ca68988c`, v31)
- **Trigger:** Contact Changed, docs_status is Partial.
- **Window:** Mon to Fri 08:00 to 19:00.
- **Flow:** Pause check (Pause Docs Received / Full Review). Find opportunity; safety: stage = Docs Received / Full Review. Remove tag if:docs:missing. docs_review_attempt_count = 0, partial_docs = Partial_docs_remaining. Count +1; count >= 6 = stage Interested / Not Ready + task "S6 – Case Paused" (5 chases). Else task "S6 – Admin Chase Missing Documents", wait 3 days for partial_docs = Partial_docs_recieved:
  - Received = docs_status = Complete, stage Advisor Recommendation.
  - Timeout = still Docs Received = count +1 and chase again; other stage = end.

---

## 3. Field ownership

| Field | ID | Set by | Read by |
|---|---|---|---|
| call_phase | y1Bjm1Afle54l8kmZ4uu | S1 Intake (Admin Call), S1 Admin (None), S3 Booked (None, then Advisor Call at 2 h, None on Answered), S3 No Answer (None at 6), S3 Cancel (None) | **Backend** |
| admin_call_status | 64aVYbvKIcwrgnZjQt57 | S1 Intake (Pending), **Backend** | S1 Admin |
| admin_call_attempt_count | 8ja7M6RflYbgaOza5Lx0 | S1 Intake (0), **Backend** (+1) | S1 Admin (trigger, >= 6) |
| advisor_call_status | USSbwwmZyvqVHHx7EM1c | S3 Booked (Pending), **Backend** | S3 Booked (wait for Answered), S3 No Answer |
| advisor_call_attempt_count | 9s0YPzkLjWZzYLI6VjDd | S3 Booked (0), **Backend** (+1) | S3 No Answer (trigger, >= 6) |
| last_call_at | TPQm1GlwV11DifBaERbf | **Backend** | S1 Admin, S3 No Answer ("is Today") |
| Appointment Booked | 9EdC3XRHIT8f7XM292go | S1 Intake (No), S3 Booked (Yes), S3 Cancel (No) | S2, S3 Cancel |
| client_readiness_status | feCUoMzmjC1rJqOUehA9 (key admin_readiness_status) | S2 / S3 Cancel (Pending), staff (Not Ready) | S2, S3 Cancel |
| Booked By | ILfqxXose1Hx60Hd6oFO | Staff; S1 Intake clears | S3 Booked |
| Fact Find Date | BGI6dOCmacnxYlMVBDqq | S3 Booked; S3 Cancel clears | Reporting |
| Fact Find Outcome | fTifKixWDw4U92UpnOJ7 | Staff; S1 Intake clears | S4 |
| Docs Requested | DnjBkZQsPXeSeAui3NWD | S4 (No), staff (Yes) | S4 |
| docs_status | 7vyXOzDxvdjpoFF0VpGh | S5 (Pending), S6 (Pending), staff (Complete / Partial), S6 Chase (Complete) | S5, S6, S6 Chase (trigger) |
| docs_chase_attempt_count | 5MAkGIS9rJ2VRPVjS7vd | S5 | S5 |
| partial_docs | ypbzVrTnQIJ9AmFXwV6c | S6 Chase, staff | S6 Chase |
| docs_review_attempt_count | zbx0QmH1yMkWLD8JcMCW | S6 Chase | S6 Chase |
| Admin-Handover | 3RhIyOFmfqsLLKMbbkCM | Staff | S6 |
| is_estate_agent_lead | ewyFAHDVq2tRqrrovInA | Staff | S3 Booked |
| advisor-name | 7gGFW09JD3ByCBZW5PQQ | Staff | **Backend** (advisor match) |
| Pause Automations | If90lxAQst0DbYqHG15k | Staff | Every workflow |

---

## 4. Known gaps and cautions

1. **Write-back not built:** stages 1 to 3 do not move without it.
2. **call_phase = None** from Client Contacted until 2 hours before the meeting: calls in that window are logged only. An early advisor call does not move the case.
3. **Advisor match:** nothing sets Advisor Email; advisor-name is manual. See spec, open decision 8.1.
4. **Interested / Not Ready workflows** (outside S1 to S6) may still start on the retired tag `lf:notready`. The new workflows move the stage without that tag. Check that workflow's trigger before cut-over.
5. **Reporting sync:** the Post Appointment stage and the Client Contacted rename are not in the reporting sync yet. Appointment outcomes are not recorded as a field; "booked by" depends on staff filling Booked By; only the main pipeline is synced; Evonex call data is not yet in the reporting.
6. **Cut-over:** publish the 7 "New - WF" drafts and switch off the old live versions in the same session; switch off the old S5 Router and the old "Mortgage Document Request Task"; decide on workflow 5e33877e. Contacts already inside old workflows keep running old logic.
7. **Still to verify (22 Oct):** S3 Appointment Booked fix export, S1 Lead Intake reset export, S3 Booked trigger status filter.
