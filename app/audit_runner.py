"""One audit cycle: appointments -> locks -> flags. Runs inside the 15-minute
sync but can never affect it (the caller wraps it; see run_safely).

Per cycle the work is bounded:
  * the calendar events (a handful of API calls),
  * contacts updated since the last run (one search) and the contacts of
    appointments booked/changed since then,
  * once a day (and on the first run) a full pass over every contact that has
    Created By / Booked By set plus every appointment's contact.
Nothing is ever written to GHL; this module only reads GHL and writes the
reporting sheet.
"""

import logging
from datetime import datetime, timedelta, timezone

from app import config
from app.appointments import contact_display_name, events_to_rows, parse_dt
from app.audit_store import AuditStores
from app.call_evidence import build_call_activity, build_evidence, calls_by_phone, evidence_flags, is_ready
from app.field_audit import (
    F_BOOKED_BY, F_CREATED_BY, MAX_FLAGS_PER_RUN, audit_appointments, audit_created_by, snapshot, source_flags,
)

from app.writeback_rules import collapse_events, parse_ts, to_e164

logger = logging.getLogger("audit")

STATE_LAST_RUN = "last_run_utc"
OVERLAP = timedelta(minutes=10)
BACK_DAYS, FORWARD_DAYS, FIRST_RUN_BACK_DAYS = 120, 90, 400
EVENTS_NORMAL, EVENTS_BACKFILL = 1500, 12000          # Evonex event rows read per cycle (about 3 days / the whole history so far)
EVIDENCE_LOOKBACK_DAYS, EVIDENCE_BACKFILL_DAYS = 4, 30


NAME_FETCH_PER_CYCLE = 100         # contacts looked up per cycle to fill missing client names (the rest follow next cycle)


async def _fill_contact_names(ghl, stores, appointments, contacts) -> None:
    """Carry the client's name onto each appointment row. Names already stored are kept; missing ones come from the contacts
    this cycle already read, then from GHL (bounded per cycle). A failed lookup just leaves the name blank for next time."""
    stored = {r["appointment_id"]: r.get("contact_name", "") for r in stores.appointments.read()}
    known = {}
    for a in appointments:
        cid = a["contact_id"]
        name = stored.get(a["appointment_id"], "")
        if not name and cid in contacts:
            name = contact_display_name(contacts[cid])
        a["contact_name"] = name
        if name and cid:
            known[cid] = name
    for a in appointments:                                  # the same client's other appointments
        if not a["contact_name"] and a["contact_id"] in known:
            a["contact_name"] = known[a["contact_id"]]
    missing = sorted({a["contact_id"] for a in appointments if not a["contact_name"] and a["contact_id"]})[:NAME_FETCH_PER_CYCLE]
    if not missing:
        return
    try:
        fetched = await ghl.get_all_contacts(missing)
    except Exception as exc:
        logger.warning("client name lookup failed: %s", type(exc).__name__)
        return
    for a in appointments:
        if not a["contact_name"] and a["contact_id"] in fetched:
            a["contact_name"] = contact_display_name(fetched[a["contact_id"]])


async def call_evidence_step(ghl, stores, appointments, now, cfg, events_reader) -> tuple:
    """Match finished appointments to the advisor's Evonex call. Returns (flags, summary). Reads GHL contacts (phone
    numbers, in memory only) and the Evonex events; writes Call_Evidence and Call_Activity (no phone numbers)."""
    window = int(getattr(cfg, "CALL_MATCH_WINDOW_MINUTES", 180))
    conversation = int(getattr(cfg, "CALL_CONVERSATION_SECONDS", 60))
    long_seconds = int(getattr(cfg, "CALL_LONG_SECONDS", 600))
    existing = {r["appointment_id"]: r for r in stores.evidence.read()}
    backfill = not existing
    asked = EVENTS_BACKFILL if backfill else EVENTS_NORMAL
    rows = events_reader(asked)
    # The Evonex history starts when the webhooks were switched on, and a read may stop short of it. Either way an
    # appointment whose match window begins before the earliest event we hold cannot be judged ("no call" would be a guess).
    stamps = [t for t in (parse_ts(r.get("ts")) for r in rows) if t]
    coverage = min(stamps) if stamps else now
    calls = collapse_events(rows)

    horizon = now - timedelta(days=EVIDENCE_BACKFILL_DAYS if backfill else EVIDENCE_LOOKBACK_DAYS)
    ready = [a for a in appointments
             if (parse_dt(a["start_time"]) or now) >= horizon and is_ready(a, existing, now, window, coverage)]
    phones = {}
    ids = sorted({a["contact_id"] for a in ready if a["contact_id"]})
    if ids:
        for cid, contact in (await ghl.get_all_contacts(ids)).items():
            phone = to_e164(contact.get("phone"))
            if phone:
                phones[cid] = phone
    evidence = build_evidence(ready, phones, calls_by_phone(calls), existing, now, window_minutes=window,
                              conversation_seconds=conversation, long_seconds=long_seconds, coverage_start=coverage,
                              all_calls=list(calls.values()))
    result = stores.evidence.upsert(evidence) if evidence else {"inserted": 0, "updated": 0}
    # A read that reached the start of the history holds every day completely; only a read that hit its row limit can
    # have cut the oldest day short.
    truncated = len(rows) >= asked
    activity = build_call_activity(calls, coverage if truncated else None, now)
    if activity:
        stores.activity.upsert(activity)
    flags = evidence_flags(evidence, appointments, now)
    return flags, {"evidence": len(evidence), **{f"evidence_{k}": v for k, v in result.items()}, "activity_rows": len(activity), "backfill": backfill}


def mode(cfg=config) -> str:
    return "on" if cfg.FIELD_AUDIT == "on" else "off"


def _since(cfg, now):
    explicit = parse_dt(cfg.FIELD_AUDIT_SINCE) if cfg.FIELD_AUDIT_SINCE else None
    return explicit or (now - timedelta(days=7))


async def run_cycle(ghl, stores, cfg=config, now=None, users=None, events_reader=None) -> dict:
    if mode(cfg) != "on":
        return {"mode": "off"}
    now = now or datetime.now(timezone.utc)
    if users is None:
        users = await ghl.get_users()
        if users is None:
            return {"mode": "on", "error": "users_fetch_failed"}
    last_text = stores.get_state(STATE_LAST_RUN)
    last_run = parse_dt(last_text)
    full_pass = (
        last_run is None
        or now - last_run > timedelta(hours=24)
        or (now.hour == cfg.FULL_REFRESH_HOUR_UTC and last_run.date() != now.date())
    )

    # 1. Calendar events. Never act on a partial fetch.
    back = FIRST_RUN_BACK_DAYS if last_run is None else BACK_DAYS
    events = []
    for calendar_id in cfg.APPOINTMENT_CALENDAR_IDS:
        found = await ghl.get_calendar_events(calendar_id, now - timedelta(days=back), now + timedelta(days=FORWARD_DAYS))
        if found is None:
            return {"mode": "on", "error": "calendar_fetch_failed"}
        events.extend(found)
    appointments = events_to_rows(events, users)

    # 2. Which contacts to look at this cycle.
    since = _since(cfg, now)
    cutoff = None if full_pass else last_run - OVERLAP
    recent_appts = [
        a for a in appointments
        if (parse_dt(a["booked_at"]) or now) >= since
        and (cutoff is None or (parse_dt(a["booked_at"]) or now) >= cutoff or (parse_dt(a["status_updated_at"]) or now) >= cutoff)
    ]
    contacts = {}
    if full_pass:
        for field_id in (F_CREATED_BY, F_BOOKED_BY):
            found = await ghl.search_contacts_with_field(field_id)
            if found is None:
                return {"mode": "on", "error": "contact_search_failed"}
            contacts.update({c["id"]: c for c in found if c.get("id")})
    else:
        found = await ghl.search_contacts_updated_since(cutoff)
        if found is None:
            return {"mode": "on", "error": "contact_search_failed"}
        contacts.update({c["id"]: c for c in found if c.get("id")})
    need = sorted({a["contact_id"] for a in recent_appts if a["contact_id"]} - set(contacts))
    if need:
        contacts.update(await ghl.get_all_contacts(need))
    snaps = [snapshot(c) for c in contacts.values()]

    # 3. Locks, flags and the official booker of every appointment.
    lock_rows = stores.locks.read()
    locks = {(r["contact_id"], r["field"]): r for r in lock_rows}
    new_locks, flags = audit_created_by(snaps, locks, now)
    for row in new_locks:
        locks[(row["contact_id"], row["field"])] = row
    trusted = cfg.TRUSTED_BOOKING_SOURCES
    appt_locks, appt_flags, official = audit_appointments(
        snaps, locks, appointments, cfg.WRITEBACK_ADVISOR_NAMES, now, since, cfg.BOOKED_BY_GRACE_HOURS, trusted,
        owner_names=users if getattr(cfg, "BOOKER_OWNER_FLAG", "off") == "on" else None,
        owner_window_hours=float(getattr(cfg, "BOOKER_OWNER_WINDOW_HOURS", 48.0)),
    )
    new_locks += appt_locks
    flags += appt_flags + source_flags(appointments, trusted, now)

    # `official` is rebuilt every cycle from the calendar (bookings made inside GHL) and from the locks
    # (everything else), so it never depends on which contacts this cycle happened to read.
    for row in appointments:
        row["booked_by_official"], row["booked_by_basis"] = official.get(row["appointment_id"], ("", ""))
    await _fill_contact_names(ghl, stores, appointments, contacts)
    appt_result = stores.appointments.upsert(appointments)

    # 3b. Call evidence (optional; a failure here never stops the rest of the audit).
    evidence_summary = None
    if events_reader is not None and getattr(cfg, "CALL_EVIDENCE", "off") == "on":
        try:
            ev_flags, evidence_summary = await call_evidence_step(ghl, stores, appointments, now, cfg, events_reader)
            flags += ev_flags
        except Exception as exc:
            logger.error("call evidence failed: %s", type(exc).__name__)
            evidence_summary = {"error": type(exc).__name__}

    known = {r["flag_id"] for r in stores.flags.read()}
    fresh = {}
    for flag in flags:
        if flag["flag_id"] not in known:
            fresh.setdefault(flag["flag_id"], flag)
    fresh_list = list(fresh.values())
    capped = len(fresh_list) > MAX_FLAGS_PER_RUN
    if new_locks:
        stores.locks.upsert(new_locks)
    stores.flags.append(fresh_list[:MAX_FLAGS_PER_RUN])
    stores.set_state(STATE_LAST_RUN, now.strftime("%Y-%m-%dT%H:%M:%SZ"))

    result = {
        "mode": "on", "full_pass": full_pass, "appointments": len(appointments), **{f"appointments_{k}": v for k, v in appt_result.items()},
        "contacts_checked": len(snaps), "locks_written": len(new_locks),
        "flags_new": min(len(fresh_list), MAX_FLAGS_PER_RUN), "flags_capped": capped,
    }
    if evidence_summary is not None:
        result["call_evidence"] = evidence_summary
    logger.info("audit %s", " ".join(f"{k}={v}" for k, v in result.items()))
    return result


async def run_safely(ghl) -> dict:
    """Entry point for the sync cycle: never raises, never blocks the sync on failure."""
    try:
        if mode() != "on":
            return {"mode": "off"}
        reader = None
        if config.CALL_EVIDENCE == "on" and config.EVONEX_SHEET_ID:
            from app.writeback_store import WritebackStore
            reader = WritebackStore().recent_events
        return await run_cycle(ghl, AuditStores(), events_reader=reader)
    except Exception as exc:
        logger.error("audit failed: %s", type(exc).__name__)
        return {"error": type(exc).__name__}
