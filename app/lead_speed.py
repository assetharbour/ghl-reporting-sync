"""Speed to lead: how quickly a new lead gets its first call. The pure engine (no I/O).

For every new lead (an opportunity created after the Evonex call history begins) this finds the first outbound call to the
contact's phone number and the first call that connected, and records how long each took.

Response time is counted from when the office was OPEN: a lead that arrives at 22:00 or on a Sunday is not "late" because
nobody called it overnight, so its clock starts at the next opening time (`office_start` o'clock on the next weekday,
Europe/London). A call made before that opening time counts as an immediate response (0 minutes). Bank holidays are not
known and count as working days.

Leads whose source is "Existing Client" are renewals of people already on the books, not new enquiries, so they are not
part of speed to lead. Rows hold ids, staff names, timestamps and minutes only; never a phone number.
"""

from datetime import datetime, timedelta, timezone

from app.call_evidence import call_time
from app.writeback_rules import LONDON, to_e164

SPEED_COLUMNS = [
    "opportunity_id", "contact_id", "created_at", "office_hours", "clock_starts_at", "status", "first_call_at", "first_call_by",
    "minutes_to_first_call", "first_connect_at", "first_connect_by", "minutes_to_first_connect", "evaluated_at",
]

EXCLUDED_SOURCES = ("existing client",)
GRACE_MINUTES = 60          # a lead nobody has called yet is only "not called" once its clock has run this long
LOOKBACK_DAYS = 30          # leads created longer ago than this are not (re)judged
FREEZE_DAYS = 14            # a lead that was never called stops being re-checked after this long


def _stamp(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def in_office_hours(dt: datetime, office_start: int = 8, office_end: int = 18) -> bool:
    local = dt.astimezone(LONDON)
    return local.weekday() < 5 and office_start <= local.hour < office_end


def clock_start(dt: datetime, office_start: int = 8, office_end: int = 18) -> datetime:
    """When the response clock starts for a lead created at `dt`: now if the office is open, else the next opening time."""
    local = dt.astimezone(LONDON)
    if in_office_hours(dt, office_start, office_end):
        return dt
    day = local.date()
    if local.weekday() < 5 and local.hour < office_start:
        pass                                              # early on a weekday: the office opens later today
    else:
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return datetime(day.year, day.month, day.day, office_start, tzinfo=LONDON).astimezone(timezone.utc)


def _minutes(since: datetime, when: datetime):
    return round(max(0.0, (when - since).total_seconds()) / 60, 1)


def is_ready(lead: dict, existing: dict, now, coverage_start) -> bool:
    created = lead["created_at"]
    if str(lead.get("source", "")).strip().lower() in EXCLUDED_SOURCES:
        return False
    if coverage_start is None or created < coverage_start or created > now:
        return False                                      # before call tracking began (or from the future): cannot be judged
    if now - created > timedelta(days=LOOKBACK_DAYS):
        return False
    row = existing.get(lead["opportunity_id"])
    if row:
        if row.get("first_connect_at"):
            return False                                  # answered: final
        if now - created > timedelta(days=FREEZE_DAYS):
            return False
    return True


def build_lead_speed(leads: list, phones: dict, by_phone: dict, existing: dict, now, coverage_start, *,
                     office_start: int = 8, office_end: int = 18) -> list:
    """Rows for every lead that is ready to (re)judge. `phones` maps contact_id -> E.164, `by_phone` is
    call_evidence.calls_by_phone(), `existing` maps opportunity_id -> the stored row."""
    rows = []
    for lead in leads:
        if not is_ready(lead, existing, now, coverage_start):
            continue
        created = lead["created_at"]
        start = clock_start(created, office_start, office_end)
        row = {
            "opportunity_id": lead["opportunity_id"], "contact_id": lead.get("contact_id", ""), "created_at": _stamp(created),
            "office_hours": "TRUE" if in_office_hours(created, office_start, office_end) else "FALSE", "clock_starts_at": _stamp(start),
            "status": "no_phone", "first_call_at": "", "first_call_by": "", "minutes_to_first_call": "", "first_connect_at": "",
            "first_connect_by": "", "minutes_to_first_connect": "", "evaluated_at": _stamp(now),
        }
        phone = phones.get(lead.get("contact_id", ""))
        if phone:
            calls = sorted((c for c in by_phone.get(phone, []) if call_time(c) >= created), key=call_time)
            if calls:
                first = calls[0]
                row.update(status="called", first_call_at=_stamp(call_time(first)), first_call_by=first.owner_name,
                           minutes_to_first_call=_minutes(start, call_time(first)))
                connected = sorted((c for c in calls if c.start_ts is not None), key=lambda c: c.start_ts)
                if connected:
                    row.update(first_connect_at=_stamp(connected[0].start_ts), first_connect_by=connected[0].owner_name,
                               minutes_to_first_connect=_minutes(start, connected[0].start_ts))
            else:
                row["status"] = "not_called" if now >= start + timedelta(minutes=GRACE_MINUTES) else "waiting"
        rows.append(row)
    return rows


__all__ = ["SPEED_COLUMNS", "build_lead_speed", "clock_start", "in_office_hours", "is_ready", "to_e164"]
