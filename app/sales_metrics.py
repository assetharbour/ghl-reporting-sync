"""Sales-team KPIs from the Appointments tab: the executable definition the
dashboard follows (the React dashboard computes the same figures from the same
rows). Pure functions, no I/O.

Definitions (agreed wording for the client):

  booked     appointments booked in the period (by the date they were BOOKED),
             including ones later cancelled or no-show. Deleted ones never count.
  due        booked minus cancelled minus still-to-come (start time in the future
             and still new/confirmed). These are the appointments that should
             have been held by now, so recent periods are not unfairly low.
  showed     status "showed".   noshow: status "noshow".
  full FF    the contact's Fact Find Outcome is "Full Fact Find ..." (not Advice
             Given, Review, Annual Review or Amendment). The outcome is stored per
             contact, so it is credited ONCE, to the contact's latest due
             appointment, never to every appointment of that contact.
  conversion appointment booked -> full fact find completed = full FF / due.
  show rate  showed / due.   FF per show: full FF / showed.
  rebooked   a cancelled or no-show appointment whose contact has another (not
             deleted) appointment booked LATER, i.e. the client was given a new slot.
  completed  the contact's mortgage has completed (completion date set, or the case
             is at Completion / Post Completion). Credited once to the contact's
             latest due appointment, and only if the completion is on or after
             the day that appointment was booked (so older business never counts).
  conversion to completion = completed / due. Completion takes months, so a recent
             period keeps rising as its cases complete.

The booker is the OFFICIAL booker (`booked_by_official`: the calendar booker for
bookings made inside GHL, otherwise the locked Booked By field of the booking
form). The same table is produced per booker (the sales rep: pay and targets)
and per advisor (who holds the meeting: workload and the spread of appointments).
`sales_team` (Harry, Ava, Hugo) marks the rows the client pays on.
"""

from collections import defaultdict
from datetime import datetime

from app.appointments import parse_dt
from app.writeback_rules import names_match


def is_full_fact_find(outcome) -> bool:
    return str(outcome or "").strip().lower().startswith("full fact find")


def _in_period(dt, start, end) -> bool:
    return dt is not None and (start is None or dt >= start) and (end is None or dt < end)


def _person(appointment: dict, group_by: str) -> str:
    if group_by == "booked_by":
        return appointment.get("booked_by_official") or "(unknown)"
    return appointment.get(group_by) or "(unknown)"


def build(appointments: list, contacts: dict, now: datetime, start=None, end=None, group_by: str = "booked_by",
          sales_team: list = None, date_field: str = "booked_at") -> list:
    """One row per person. `contacts` maps contact_id -> {"fact_find_outcome": str,
    "completed": bool, "completed_at": ISO date or ""}. `group_by` is "booked_by"
    (the official booker) or "advisor". Rows are sorted by name. `date_field` is the date the period applies to:
    "booked_at" (the day it was booked, the default) or "start_time" (the day the appointment takes place)."""
    in_period = [
        a for a in appointments
        if not a.get("deleted") and _in_period(parse_dt(a.get(date_field)), start, end)
    ]

    def starts_in_future(a):
        s = parse_dt(a.get("start_time"))
        return s is not None and s > now and a.get("status") in ("", "new", "confirmed")

    def due(a):
        return a.get("status") not in ("cancelled", "invalid") and not starts_in_future(a)

    # credit each contact's single outcome to its latest due appointment (all history, not just the period)
    latest_due = {}
    for a in appointments:
        if a.get("deleted") or not a.get("contact_id") or not due(a):
            continue
        best = latest_due.get(a["contact_id"])
        if best is None or (a.get("booked_at", ""), a["appointment_id"]) > (best.get("booked_at", ""), best["appointment_id"]):
            latest_due[a["contact_id"]] = a
    credited = {
        appt["appointment_id"] for cid, appt in latest_due.items()
        if is_full_fact_find((contacts.get(cid) or {}).get("fact_find_outcome"))
    }

    def completed_after_booking(cid, appt) -> bool:
        info = contacts.get(cid) or {}
        if not info.get("completed"):
            return False
        done, booked = parse_dt(info.get("completed_at")), parse_dt(appt.get("booked_at"))
        return done is None or booked is None or done >= booked

    credited_completed = {
        appt["appointment_id"] for cid, appt in latest_due.items() if completed_after_booking(cid, appt)
    }

    # rebooked: the contact has a later (not deleted) booking. One pass finds each contact's latest booking
    # (appointment ids are unique, so "later than this one" is simply "this one is not the latest").
    latest_booking = {}
    for o in appointments:
        if o.get("deleted") or not o.get("contact_id"):
            continue
        key = (o.get("booked_at", ""), o["appointment_id"])
        if o["contact_id"] not in latest_booking or key > latest_booking[o["contact_id"]]:
            latest_booking[o["contact_id"]] = key

    def rebooked(a) -> bool:
        if a.get("status") not in ("cancelled", "noshow") or not a.get("contact_id"):
            return False
        return (a.get("booked_at", ""), a["appointment_id"]) < latest_booking[a["contact_id"]]

    groups = defaultdict(list)
    for a in in_period:
        groups[_person(a, group_by)].append(a)

    table = []
    for person, rows in groups.items():
        def count(pred, rows=rows):
            return sum(1 for r in rows if pred(r))

        due_rows = [r for r in rows if due(r)]
        full = sum(1 for r in due_rows if r["appointment_id"] in credited)
        completed = sum(1 for r in due_rows if r["appointment_id"] in credited_completed)
        showed = count(lambda r: r.get("status") == "showed")
        table.append({
            "person": person,
            "booked": len(rows),
            "cancelled": count(lambda r: r.get("status") == "cancelled"),
            "no_show": count(lambda r: r.get("status") == "noshow"),
            "rebooked": count(rebooked),
            "showed": showed,
            "upcoming": count(starts_in_future),
            "due": len(due_rows),
            "full_fact_finds": full,
            "completed": completed,
            "is_sales_team": any(names_match(person, n) for n in (sales_team or [])),
            "conversion": full / len(due_rows) if due_rows else None,
            "conversion_to_completion": completed / len(due_rows) if due_rows else None,
            "completion_per_full_ff": completed / full if full else None,
            "show_rate": showed / len(due_rows) if due_rows else None,
            "ff_per_show": full / showed if showed else None,
        })
    return sorted(table, key=lambda r: r["person"].lower())


def advisor_distribution(appointments: list, advisor_names: list, start=None, end=None, date_field: str = "booked_at") -> list:
    """Appointments per listed advisor in the period: who the appointments were booked WITH. The person booking in GHL
    chooses the advisor; it is not an automatic rotation, so this is a distribution, not a fairness check. `share` is the
    advisor's part of the listed advisors' total. Appointments with someone who is not a listed advisor are returned
    separately as person "(not a listed advisor)"."""
    counts = {name: 0 for name in advisor_names}
    other = 0
    for a in appointments:
        if a.get("deleted") or not _in_period(parse_dt(a.get(date_field)), start, end):
            continue
        match = next((n for n in advisor_names if names_match(a.get("advisor", ""), n)), None)
        if match:
            counts[match] += 1
        else:
            other += 1
    total = sum(counts.values())
    rows = [{"person": n, "appointments": c, "share": c / total if total else None} for n, c in counts.items()]
    if other:
        rows.append({"person": "(not a listed advisor)", "appointments": other, "share": None})
    return rows
