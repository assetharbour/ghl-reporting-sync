"""Created By / Booked By locking and flags: the pure decision engine (no I/O).

The client pays the sales team on appointments booked and full fact finds, so
they worry staff may edit Created By / Booked By "to suit them". GHL cannot lock a
single field per user, so instead the backend:

CREATED BY (the custom contact field "Created By", NOT GHL's native createdBy,
which is often an API or automation that is assigned to a person later)
  * LOCKS the first value ever seen per contact, with the time it was first seen,
    and FLAGS every later change or clearing. Reports use the locked value.

BOOKED BY (per appointment, because a contact can be booked more than once)
  * Official booker = the Booked By FIELD when it is filled (its first value is LOCKED per appointment, later edits
    are flagged); otherwise the person GHL recorded on the calendar event (createdBy.userId) for bookings made
    inside GHL. Older bookings therefore get their booker automatically from the calendar.
  * A field value that differs from the calendar booker is flagged (a rep booking on someone's behalf, or a stale
    value from an earlier booking).
  * Advisor: the calendar's assigned advisor is compared with the advisor-name field, and appointments given to
    someone who is not a listed advisor are flagged.

Flags are append-only and de-duplicated by a deterministic id, so the same issue
is reported once however many sync cycles see it.
"""

import hashlib
from datetime import timedelta

from app.appointments import is_live, parse_dt
from app.writeback_rules import names_match

F_CREATED_BY = "QzdysR7hgj1mhfJZKNc1"     # contact custom field "Created By"
F_BOOKED_BY = "ILfqxXose1Hx60Hd6oFO"      # contact custom field "Booked By"
F_ADVISOR_NAME = "7gGFW09JD3ByCBZW5PQQ"   # contact custom field "advisor-name"

LOCK_COLUMNS = ["contact_id", "field", "first_value", "first_seen_at", "current_value", "current_seen_at"]
FLAG_COLUMNS = [
    "flag_id", "detected_at", "type", "contact_id", "field", "old_value", "new_value",
    "locked_value", "appointment_id", "detail", "status",
]

DEFAULT_TRUSTED_SOURCES = ("contactdetails_page",)
MAX_FLAGS_PER_RUN = 300          # a bad run must never flood the sheet


def booked_by_lock_field(appointment_id: str) -> str:
    """Field name used for a per-appointment Booked By lock in Field_Locks."""
    return f"booked_by:{appointment_id}"


def flag_id(kind, contact_id, field, old, new, appointment_id="") -> str:
    raw = "|".join([kind, contact_id, field, old or "", new or "", appointment_id or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def snapshot(contact: dict) -> dict:
    """The values the audit needs from one GHL contact (search result or GET)."""
    values = {f.get("id"): f.get("value") for f in (contact.get("customFields") or [])}

    def clean(field_id):
        value = values.get(field_id)
        return "" if value is None else str(value).strip()

    return {
        "contact_id": contact.get("id", ""),
        "created_by": clean(F_CREATED_BY),
        "booked_by": clean(F_BOOKED_BY),
        "advisor_name": clean(F_ADVISOR_NAME),
        "owner_user_id": str(contact.get("assignedTo") or ""),
    }


def _stamp(now) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag(kind, snap, field, old, new, locked, now, appointment_id="", detail=""):
    return {
        "flag_id": flag_id(kind, snap["contact_id"], field, old, new, appointment_id),
        "detected_at": _stamp(now),
        "type": kind, "contact_id": snap["contact_id"], "field": field,
        "old_value": old or "", "new_value": new or "", "locked_value": locked or "",
        "appointment_id": appointment_id or "", "detail": detail, "status": "open",
    }


def _new_lock(contact_id, field, value, now) -> dict:
    return {"contact_id": contact_id, "field": field, "first_value": value, "first_seen_at": _stamp(now),
            "current_value": value, "current_seen_at": _stamp(now)}


def _change_flag(snap, field_label, lock, current, now, appointment_id=""):
    """The flag and updated lock row for a locked value that changed or was cleared."""
    previous, first = lock.get("current_value", ""), lock.get("first_value", "")
    kind = f"{field_label}_cleared" if not current else f"{field_label}_changed"
    if current and current == first:
        detail = "put back to the locked value"
    elif not current:
        detail = "was filled in, now empty"
    else:
        detail = "changed after being set; reports keep the locked value"
    flag = _flag(kind, snap, field_label, previous, current, first, now, appointment_id, detail)
    return flag, {**lock, "current_value": current, "current_seen_at": _stamp(now)}


def audit_created_by(snaps: list, locks: dict, now):
    """Lock the first Created By per contact and flag later changes.

    `locks` maps (contact_id, field) -> lock row. Returns (lock rows to write,
    flags); a lock row is only returned when something changed."""
    out_locks, flags = [], []
    for snap in snaps:
        current = snap["created_by"]
        lock = locks.get((snap["contact_id"], "created_by"))
        if lock is None:
            if current:
                out_locks.append(_new_lock(snap["contact_id"], "created_by", current, now))
            continue
        if current == lock.get("current_value", ""):
            continue
        flag, row = _change_flag(snap, "created_by", lock, current, now)
        flags.append(flag)
        out_locks.append(row)
    return out_locks, flags


def audit_appointments(snaps: list, locks: dict, appointments: list, advisor_names: list, now, since,
                       grace_hours: float = 24.0, trusted_sources=DEFAULT_TRUSTED_SOURCES, owner_names=None, owner_window_hours: float = 48.0):
    """Booked By and advisor checks, per appointment.

    OFFICIAL BOOKER, in this order (Talha, 22 Oct 2026):
      1. the Booked By FIELD on the contact, when it is filled: its first value is LOCKED for that appointment, so
         a later edit is flagged and does not change reports (basis "field");
      2. otherwise the person GHL recorded on the calendar event (createdBy.userId) when the booking came from a
         trusted in-GHL source such as the contact page (basis "calendar"). Every older booking uses this.
    A field value that differs from the calendar booker is flagged (booked_by_mismatch): it may be a rep booking on
    someone's behalf, or a stale value left over from an earlier booking.

    Returns (lock_rows, flags, official) where `official` maps appointment_id -> (name, basis). Only the contact's
    LATEST live appointment, booked on/after `since`, is judged or locked, so switching the audit on never floods the
    sheet with history and a rebooking starts fresh.
    """
    def calendar_booker(appt) -> str:
        trusted = bool(appt.get("booked_by_user_id")) and appt.get("booked_source") in trusted_sources
        return appt.get("booked_by", "") if trusted else ""

    official = {}
    for appt in appointments:
        if appt.get("deleted"):
            continue
        lock = locks.get((appt.get("contact_id", ""), booked_by_lock_field(appt["appointment_id"])))
        if lock and lock.get("first_value"):
            official[appt["appointment_id"]] = (lock["first_value"], "field")
        elif calendar_booker(appt):
            official[appt["appointment_id"]] = (calendar_booker(appt), "calendar")

    by_contact = {}
    for appt in appointments:
        if appt.get("contact_id") and is_live(appt):
            by_contact.setdefault(appt["contact_id"], []).append(appt)
    snap_by = {s["contact_id"]: s for s in snaps}

    out_locks, flags = [], []
    for cid, appts in by_contact.items():
        snap = snap_by.get(cid)
        if snap is None:
            continue
        appt = max(appts, key=lambda a: (a["booked_at"], a["appointment_id"]))
        booked_at = parse_dt(appt["booked_at"])
        if booked_at is None or (since and booked_at < since):
            continue
        aid, entered = appt["appointment_id"], snap["booked_by"]
        booker = calendar_booker(appt)
        key = (cid, booked_by_lock_field(aid))
        lock = locks.get(key)

        if lock is None and entered:
            out_locks.append(_new_lock(cid, booked_by_lock_field(aid), entered, now))
            official[aid] = (entered, "field")
            held = entered
        elif lock is not None:
            held = lock.get("first_value", "")
            if entered != lock.get("current_value", ""):
                flag, row = _change_flag(snap, "booked_by", lock, entered, now, aid)
                flags.append(flag)
                out_locks.append(row)
        else:
            held = ""

        if held and booker and not names_match(booker, held):
            flags.append(_flag("booked_by_mismatch", snap, "booked_by", held, booker, held, now, aid,
                               "the Booked By field differs from the person the calendar says booked it"))
        if not held and not booker and now - booked_at >= timedelta(hours=grace_hours):
            flags.append(_flag("booked_by_missing", snap, "booked_by", "", "", "", now, aid,
                               "no Booked By value and no usable calendar booker for this appointment"))

        # Optional: the official booker should be the sales person who owns the lead (contact owner). Judged only in the
        # first hours after booking, because the owner changes as the case moves on, and once per appointment.
        owner = (owner_names or {}).get(snap.get("owner_user_id", ""), "")
        who = held or booker
        if owner and who and now - booked_at <= timedelta(hours=owner_window_hours) and not names_match(who, owner):
            flag = _flag("booker_not_owner", snap, "booked_by", owner, who, "", now, aid,
                         "booked by someone other than the lead's owner (the sales person)")
            flag["flag_id"] = flag_id("booker_not_owner", cid, "booked_by", "", "", aid)
            flags.append(flag)

        advisor = appt["advisor"]
        if advisor and snap["advisor_name"] and not names_match(advisor, snap["advisor_name"]):
            flags.append(_flag("advisor_mismatch", snap, "advisor_name", snap["advisor_name"], advisor, "", now, aid,
                               "appointment is with a different advisor than the advisor-name field"))
        if advisor and not any(names_match(advisor, n) for n in advisor_names):
            flags.append(_flag("appointment_with_non_advisor", snap, "advisor", "", advisor, "", now, aid,
                               "this appointment was booked with someone who is not a listed advisor"))
    return out_locks, flags, official


def source_flags(appointments: list, trusted_sources, now) -> list:
    """One info flag per booking source that is not trusted, so a new way of
    booking (a widget, an API) is noticed the first time it appears."""
    seen, flags = set(), []
    for appt in appointments:
        source = appt.get("booked_source", "")
        if source and source not in trusted_sources and source not in seen:
            seen.add(source)
            flags.append(_flag("booking_source_not_trusted", {"contact_id": ""}, "booked_source", "", source, "", now,
                               detail="appointments from this source use the Booked By field as the official booker"))
    return flags
