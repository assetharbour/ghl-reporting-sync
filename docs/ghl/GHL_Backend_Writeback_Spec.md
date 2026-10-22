# Asset Harbour: Evonex to GHL Write-back Spec (backend)

Version: 22 Oct 2026. Owner: Talha (GrowMySME). Deadline for the S1 to S6 go-live: 1 Nov 2026.

Read this file before writing any code that updates GoHighLevel (GHL) from Evonex call data. The companion file `GHL_Workflows_S1_S6_Reference.md` explains what every GHL workflow does with the fields written here.

---

## 0. How to use these docs in the repo

1. Put both files in the repo, for example `docs/ghl/`.
2. Add one line to `CLAUDE.md`: "Before changing anything that writes to GHL, read docs/ghl/GHL_Backend_Writeback_Spec.md and docs/ghl/GHL_Workflows_S1_S6_Reference.md."
3. Raw GHL workflow JSON exports exist (Claude project, `claude/workflow_json/`). They are NOT needed to build the backend. Everything the backend needs is in these two files. Use the JSON only to check a detail.

---

## 1. Goal in one paragraph

Staff no longer mark call results by hand. Evonex (the phone system) sends webhooks to our receiver in `ghl-reporting-sync`. For every finished outbound call the backend works out the result (Answered / No Answer / Voicemail) and writes it, plus an attempt counter, onto the GHL contact. GHL workflows react to those field changes and do everything else: move pipeline stages, create tasks, send messages. **The backend writes facts. GHL makes every decision.**

Current state (22 Oct): the Evonex webhook receiver is live in `ghl-reporting-sync` (Vercel) and stores events in the private "Evonex Events" Google Sheet. Outgoing Call and Call Recording events are enabled in the Evonex portal. **Write-back to GHL is not built yet.** Until it is, cases stop at Lead Received, because nothing else moves them.

---

## 2. Golden rules (do not break)

1. **Write only the fields listed as "Backend writes" in section 4.** Never write any other field, never move an opportunity stage, never add or remove tags, never create tasks.
2. **Status and counter go in the same API request.** The GHL workflows trigger on the counter changing and then read the status. If they arrive separately, the workflow reads the old status.
3. **The counter must always go up by 1** on every call written, even if the status is the same as before. GHL triggers on "attempt count has changed"; the same status twice in a row is still a new attempt.
4. **Read the contact fresh from GHL right before every write** (counter and call_phase). Workflows reset the counters to 0 and change call_phase at any time. Never use a cached value.
5. **One call is written once.** Idempotency key = Evonex call ID (`cid`). Evonex can deliver the same event more than once.
6. **Process one contact at a time.** Two calls to the same contact finishing close together must not both read count = 2 and both write 3. Serialise per contact (lock or queue).
7. **If unsure, log and do not write.** No contact match, ambiguous phase, unknown caller in the advisor phase: log the reason, write nothing.
8. **Keep PII out of logs** (phone numbers, names, emails). Log contact ID and cid instead.

---

## 3. GHL constants

| Item | Value |
|---|---|
| API base | `https://services.leadconnectorhq.com` (header `Version: 2021-07-28`) |
| Location ID | `F3nz6QISsbcAdh7pfnWc` |
| Main pipeline | "Mortgage – Enquiry to Completion" `EQyj0fwCvT8vrLEGTw5I` |
| Account timezone | UK (Europe/London). Use it for every date written |

Pipeline stages used by the S1 to S6 workflows:

| # | Stage | Stage ID |
|---|---|---|
| 1 | Lead Received | 42861e5d-6f4d-49ab-9b9d-8316a4da198c |
| 2 | Client Contacted (renamed from Admin Contacted) | 68272d52-f2b8-47ce-b657-718b465c062a |
| 3 | Appointment Booked | d803ee87-fdec-46e9-a94a-35b9cb5c851b |
| new | Post Appointment (not yet in the reporting sync) | 629e80ae-4f0f-4334-91ae-c677d3c11648 |
| 4 | Interested / Not Ready | 0acbc39a-c443-461e-891e-3806632eac2e |
| 5 | Docs Requested | fb49a23f-58c2-475d-82c1-c9f35e7b3672 |
| 6 | Docs Received / Full Review | a333899d-f065-44e2-9c05-af4f0c9dca84 |
| 7 | Advisor Recommendation | 2a78423f-3eb2-4852-b76f-6e116314809f |

The backend never moves stages. It only reads them (contact matching, section 5.2).

---

## 4. Field contract

All fields are **contact** custom fields. Option values must be written exactly as shown (case and spaces matter).

### 4.1 Backend writes

| Field | ID | Type | Values the backend writes | Notes |
|---|---|---|---|---|
| admin_call_status | `64aVYbvKIcwrgnZjQt57` | Dropdown | `Answered`, `No Answer`, `Voicemail` | `Pending` is set by GHL (S1 Lead Intake), never by the backend |
| admin_call_attempt_count | `8ja7M6RflYbgaOza5Lx0` | Number | current + 1 | **Trigger field** of "S1 Admin No Answer Logic". GHL sets 0 at intake |
| advisor_call_status | `USSbwwmZyvqVHHx7EM1c` | Dropdown | `Answered`, `No Answer` | **No `Voicemail` option.** A short connected advisor call is written as `No Answer`. `Pending` is set by GHL (S3 Booked) |
| advisor_call_attempt_count | `9s0YPzkLjWZzYLI6VjDd` | Number | current + 1 | **Trigger field** of "S3 Advisor No Answer". GHL sets 0 at every booking |
| last_call_at | `TPQm1GlwV11DifBaERbf` | Date (date only) | call date in Europe/London | Workflows check "last_call_at is Today" to drop duplicate follow-up tasks. Wrong timezone = wrong day = duplicate or missing tasks |
| last_call_by | ID to look up | Single line text | caller full name (Evonex `on`) | For reporting. Look up the ID by name from `GET /locations/{id}/customFields` |
| last_missed_call_at | ID to look up (field may not exist yet) | Single line text | ISO timestamp, e.g. `2026-10-20T14:32:05Z` | Inbound missed calls only (phase 2, section 7). Text, not date, so every missed call changes the value and fires the trigger |

### 4.2 Backend reads (never writes)

| Field | ID | Values | Used for |
|---|---|---|---|
| call_phase | `y1Bjm1Afle54l8kmZ4uu` | `Admin Call`, `Advisor Call`, `None` | Decides which pair (admin or advisor) a call is written to. Set only by GHL workflows |
| advisor-name | `7gGFW09JD3ByCBZW5PQQ` | Dropdown of advisor names | Recognising the advisor's call (section 5.4). Set by the sales rep |
| Advisor Email | ID to look up | email | Second way to recognise the advisor. **No workflow sets it at the moment** (see open decision 8.1) |
| Contact phone numbers | standard | | Contact matching |

### 4.3 Never touch

Everything else, in particular: Appointment Booked, Booked By, Created By, Fact Find Outcome, Fact Find Date, Docs Requested, docs_status, partial_docs, client_readiness_status, docs_chase_attempt_count, docs_review_attempt_count, Admin-Handover, Pause Automations, is_estate_agent_lead, and all tags. These belong to staff or to GHL workflows.

---

## 5. Processing one outbound call

### 5.1 Which events

- Use `OUTBOUNDCALL` events grouped by `cid`. Subevents: `RINGCALL`, `STARTCALL` (answered/connected), `ENDCALL`, `CANCELCALL`.
- A call is finished when `ENDCALL` or `CANCELCALL` arrives for that `cid`. Process it once, at that point.
- De-duplicate on `cid + event + subevent` when storing; write to GHL once per `cid`.
- Inbound calls (`INBOUNDCALL`) are **not** written to the call pairs. Missed inbound calls are a separate feature (section 7).

### 5.2 Match the contact

1. Take the dialled number (`rpi`), normalise to E.164 UK (`+44...`).
2. Search GHL contacts by phone in location `F3nz6QISsbcAdh7pfnWc`.
3. One match: use it.
4. Several matches: prefer the contact with an **open** opportunity in pipeline `EQyj0fwCvT8vrLEGTw5I`; then the most recently updated. Log that there were several.
5. No match: log only, write nothing.

### 5.3 Work out the outcome

Talk time = `ENDCALL.ts` minus `STARTCALL.ts` (Evonex sends no duration on call events).

| Evonex data | Outcome |
|---|---|
| No `STARTCALL` for the cid | `No Answer` |
| `STARTCALL` and talk time < 60 s | `Voicemail` |
| `STARTCALL` and talk time >= 60 s | `Answered` |

The 60 second threshold (set by Talha, 21 Oct) must be a config value (env var), not hard coded.

### 5.4 Pick the pair from call_phase (read fresh)

| call_phase on the contact | Caller | Write |
|---|---|---|
| `Admin Call` | anyone | **admin pair**: admin_call_status = outcome, admin_call_attempt_count + 1 |
| `Advisor Call` | the case's advisor (Evonex owner name = advisor-name, or owner email = Advisor Email) | **advisor pair**: advisor_call_status = outcome mapped (Voicemail becomes `No Answer`), advisor_call_attempt_count + 1 |
| `Advisor Call` | anyone else | log only (see open decision 8.1) |
| `None` or empty | anyone | log only (see open decision 8.2) |

Always write last_call_at and last_call_by in the same request as the pair.

### 5.5 Write (one request)

`PUT /contacts/{contactId}` with:

```json
{
  "customFields": [
    { "id": "64aVYbvKIcwrgnZjQt57", "field_value": "No Answer" },
    { "id": "8ja7M6RflYbgaOza5Lx0", "field_value": 3 },
    { "id": "TPQm1GlwV11DifBaERbf", "field_value": "2026-10-22" },
    { "id": "<last_call_by id>",    "field_value": "Hugo Sharpe" }
  ]
}
```

(Admin example. For the advisor pair use `USSbwwmZyvqVHHx7EM1c` and `9s0YPzkLjWZzYLI6VjDd`. Confirm the exact customFields key name, `field_value` or `value`, against the GHL API version in use.)

Then store `cid` as processed, with contact ID, pair written, outcome, new count, and timestamp, in the Evonex Events sheet (or a processed-calls tab).

### 5.6 Pseudo-code

```
on terminal event (ENDCALL or CANCELCALL) for cid:
    if cid already processed: return
    events = all stored events for cid
    outcome = outcome_from(events)            # 5.3
    contact = match_contact(events.rpi)       # 5.2
    if not contact: log("no match", cid); mark processed; return
    with lock(contact.id):
        c = get_contact(contact.id)           # fresh read
        phase = c.call_phase
        if phase == "Admin Call":
            fields = admin_pair(outcome, c.admin_call_attempt_count + 1)
        elif phase == "Advisor Call" and is_case_advisor(events.owner, c):
            fields = advisor_pair(map_advisor(outcome), c.advisor_call_attempt_count + 1)
        else:
            log("not written", cid, phase); mark processed; return
        fields += last_call_at(today_london(events.ts)), last_call_by(events.owner_name)
        put_contact(contact.id, fields)        # one request
    mark processed
```

---

## 6. What each write makes GHL do

| Backend writes | GHL workflow that reacts | Result |
|---|---|---|
| admin_call_attempt_count +1 with admin_call_status = Answered | S1 Admin No Answer Logic | call_phase = None, stage moves to **Client Contacted**, S2 starts (booking task) |
| admin_call_attempt_count +1 with No Answer or Voicemail, count < 6 | S1 Admin No Answer Logic | waits 24 h; if no newer call today and still Lead Received, creates follow-up call task |
| admin_call_attempt_count reaches 6 with No Answer or Voicemail | S1 Admin No Answer Logic | stage moves to **Interested / Not Ready**, call_phase = None |
| advisor_call_status = Answered (count +1) | S3 Appointment Booked Handler (waiting for Answered) | call_phase = None, stage moves to **Post Appointment**, S4 starts |
| advisor_call_attempt_count +1 with No Answer, count < 6 | S3 Advisor No Answer | waits 24 h; if no newer call today and still Appointment Booked, creates advisor follow-up task |
| advisor_call_attempt_count reaches 6 with No Answer | S3 Advisor No Answer | stage moves to **Interested / Not Ready**, call_phase = None |
| last_call_at | S1 / S3 no-answer workflows | "Is Today" check stops duplicate follow-up tasks |
| last_missed_call_at (phase 2) | Missed Call Text-Back (not built yet) | SMS + call-back task |

After a call that moves the case on, call_phase becomes `None`, so later calls on that contact are logged only (until a workflow sets a phase again).

---

## 7. Phase 2 (after the core write-back works)

1. **Missed inbound calls:** needs Incoming and Missed events enabled in the Evonex portal. Write last_missed_call_at (create the field first). A GHL "Missed Call Text-Back" workflow will trigger on it (not built yet).
2. **Flags tab** in the reporting sheet: any change to Created By or Booked By after first seen (old, new, time); cases where Booked By differs from the contact owner.
3. **Reporting:** add the Post Appointment stage and the Client Contacted rename to `field_mapping.py` and the dashboard `STAGE_ORDER`; add the new fields as Sheet columns (append only, never insert mid-list); meeting booked date and cancellations from the GHL calendar API ("S3 Appointment Calendar"); call KPIs (dials, connect rate, talk time, speed to lead) from the Evonex Events sheet.

---

## 8. Open decisions (need Talha before or during the build)

1. **How to recognise the advisor's call.** The plan was: write the advisor pair only if the caller is the case's advisor (advisor-name or Advisor Email). In the current workflows **nothing sets Advisor Email**, and advisor-name is set by hand by the sales rep and may be empty or spelled differently from the Evonex user name. If the match fails, the advisor's call is only logged and the case never reaches Post Appointment. Suggested default: in `Advisor Call` phase, write the advisor pair for any caller who is in a configured advisor list (Evonex owner emails), and log a warning when it is not the named advisor. Confirm.
2. **Calls while call_phase = None.** call_phase is `None` from Client Contacted until 2 hours before the fact find meeting, and after Post Appointment. Admin calls made to book the meeting, and an advisor call made earlier than 2 hours before the meeting, land here. Suggested default: log only (keeps them in the Evonex Events sheet for KPIs). Note: an early advisor call will then not move the case; staff would need the call to happen in the 2 hour window or later. Confirm.
3. **Several contacts with the same number** (joint applicants, family): confirm the tie-break in 5.2.
4. **Field IDs to look up:** last_call_by, last_missed_call_at (create first), Advisor Email.

---

## 9. Must verify before go-live

1. **An API field update fires GHL "Contact Changed" triggers.** Test on one contact: update admin_call_attempt_count by API and check the S1 workflow enrolment history. If it does not fire, fallback = backend calls a GHL inbound webhook trigger.
2. The `customFields` request format and date format accepted by the API.
3. Rate limits: GHL API limits per location; add retry with backoff on 429 / 5xx; make retries idempotent (cid).

---

## 10. End-to-end test (one test contact)

| Step | Action | Expected |
|---|---|---|
| 1 | Create an opportunity at Lead Received for a test contact | S1 sets call_phase = Admin Call, admin_call_status = Pending, count 0 |
| 2 | Outbound call, no answer | admin_call_status = No Answer, count 1, last_call_at today; follow-up task next working day |
| 3 | Outbound call, answered, >= 60 s | admin_call_status = Answered, count 2; stage Client Contacted; S2 booking task |
| 4 | Book the meeting in "S3 Appointment Calendar" | Stage Appointment Booked; advisor_call_status = Pending, advisor count 0; call_phase = Advisor Call 2 h before |
| 5 | Advisor calls, no answer | advisor_call_status = No Answer, count 1; follow-up task after 24 h |
| 6 | Advisor calls, answered >= 60 s | advisor_call_status = Answered, count 2; stage Post Appointment; S4 task |
| 7 | Six unanswered admin calls on another test contact | stage Interested / Not Ready after the 6th |
| 8 | Replay the same webhook | nothing changes (idempotent) |
