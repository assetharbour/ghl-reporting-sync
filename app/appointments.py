"""Appointments from the GHL calendar API, normalised for the reporting sheet.

Every event on the sales booking calendar carries the two facts the client's pay
and fairness reports need, set by GHL itself and not editable by staff:
  * createdBy.userId   who booked it (the sales rep logged in on the contact page)
  * assignedUserId     the advisor the appointment is with (chosen by whoever books it in GHL)
plus dateAdded (when it was booked), startTime (when it takes place) and the
appointment status (new / confirmed / showed / noshow / cancelled / invalid).
"""

from datetime import datetime, timezone
from typing import Optional

APPT_COLUMNS = [
    "appointment_id", "contact_id", "calendar_id", "booked_by_user_id", "booked_by",
    "booked_source", "booked_at", "start_time", "end_time", "advisor_user_id", "advisor",
    "status", "status_updated_at", "deleted",
    # Appended: the OFFICIAL booker and where it came from ("calendar" or "field"), filled by the audit.
    "booked_by_official", "booked_by_basis",
    # Appended 23 Oct: the client's name, so appointments of contacts with no row in Leads are still readable. Names are
    # already in the Leads tab; the dashboard is behind the same login.
    "contact_name",
]


def _text(value) -> str:
    return "" if value is None else str(value)


def parse_dt(value) -> Optional[datetime]:
    """GHL returns ISO strings with an offset or Z; naive values are read as UTC."""
    try:
        text = str(value or "").strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_utc_text(value) -> str:
    """Store every timestamp as UTC ISO text so sorting and diffing are stable."""
    dt = parse_dt(value)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def event_to_row(event: dict, users: dict) -> Optional[dict]:
    """One calendar event -> one reporting row (None if it has no id)."""
    if not event.get("id"):
        return None
    created_by = event.get("createdBy") if isinstance(event.get("createdBy"), dict) else {}
    booker_id = created_by.get("userId") or ""
    advisor_id = event.get("assignedUserId") or ""
    status = (event.get("appointmentStatus") or event.get("appoinmentStatus") or event.get("status") or "").lower()
    return {
        "appointment_id": event["id"],
        "contact_id": _text(event.get("contactId")),
        "calendar_id": _text(event.get("calendarId")),
        "booked_by_user_id": booker_id,
        "booked_by": users.get(booker_id, ""),
        "booked_source": _text(created_by.get("source")),
        "booked_at": to_utc_text(event.get("dateAdded")),
        "start_time": to_utc_text(event.get("startTime")),
        "end_time": to_utc_text(event.get("endTime")),
        "advisor_user_id": advisor_id,
        "advisor": users.get(advisor_id, ""),
        "status": status,
        "status_updated_at": to_utc_text(event.get("dateUpdated")),
        "deleted": "TRUE" if event.get("deleted") else "",
        "booked_by_official": "",
        "booked_by_basis": "",
        "contact_name": "",
    }


def events_to_rows(events: list, users: dict) -> list:
    """De-duplicated by appointment id (the fresher copy wins), oldest booking first."""
    by_id = {}
    for event in events:
        row = event_to_row(event, users)
        if row:
            by_id[row["appointment_id"]] = row
    return sorted(by_id.values(), key=lambda r: (r["booked_at"], r["appointment_id"]))


def is_live(row: dict) -> bool:
    """An appointment that still counts: not deleted, not cancelled or invalid."""
    return not row.get("deleted") and row.get("status") not in ("cancelled", "invalid")


def contact_display_name(contact: dict) -> str:
    """The name GHL shows for a contact: contactName, else first + last name."""
    name = str(contact.get("contactName") or contact.get("name") or "").strip()
    if name:
        return name
    return " ".join(p for p in (str(contact.get("firstName") or "").strip(), str(contact.get("lastName") or "").strip()) if p)
