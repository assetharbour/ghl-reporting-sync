"""Central configuration. Loads env vars and fails fast if any are missing."""

import logging
import math
import os

from dotenv import load_dotenv

load_dotenv()


def _env_number(name: str, default, cast):
    """A numeric optional setting. A typo such as WRITEBACK_TALK_SECONDS=60s must never stop the whole
    app from starting (this module is imported by the sync, the reports and the Evonex receiver), so an
    unreadable or non-positive value falls back to the default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        value = None
    if value is None or not math.isfinite(value) or value < 0:      # rejects nan, inf and negatives
        logging.getLogger(__name__).warning("Ignoring invalid %s=%r; using %r", name, raw, default)
        return default
    return value


_REQUIRED_VARS = [
    "GHL_PRIVATE_TOKEN",
    "GHL_LOCATION_ID",
    "GHL_PIPELINE_ID",
    "GOOGLE_SHEET_ID",
    "CRON_SECRET",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
]

_missing = [name for name in _REQUIRED_VARS if not os.getenv(name)]
if _missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(_missing)}. "
        "Set them in .env (local) or Vercel project settings (production)."
    )

GHL_PRIVATE_TOKEN = os.environ["GHL_PRIVATE_TOKEN"]
GHL_LOCATION_ID = os.environ["GHL_LOCATION_ID"]
GHL_PIPELINE_ID = os.environ["GHL_PIPELINE_ID"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CRON_SECRET = os.environ["CRON_SECRET"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

GHL_BASE_URL = "https://services.leadconnectorhq.com"
GHL_HEADERS = {
    "Authorization": f"Bearer {GHL_PRIVATE_TOKEN}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
}

# GHL custom value IDs — confirmed live via /locations/{id}/customValues.
WEEKLY_REPORT_VALUE_ID = "Kr3JvQdXgUr1OGHnV45g"
MONTHLY_REPORT_VALUE_ID = "DeGLq4rXKreKLLGPVpb3"

# assetharbour-reports.vercel.app — a project domain registered via
# `vercel domains add` (not a raw `vercel alias set`, which does NOT
# exempt a hostname from Vercel Authentication/SSO — confirmed live,
# the raw alias 302'd to vercel.com/sso-api until it was added as a
# proper project domain). Chosen over the raw ghl-reporting-sync-sage
# alias so a client-facing PDF link doesn't expose internal repo/tool
# naming. GHL's email link needs a URL that resolves for an
# unauthenticated recipient — confirmed with a direct curl (real 200
# JSON, no redirect).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://assetharbour-reports.vercel.app")

# Evonex webhook receiver. Deliberately optional at import: the sync and
# report endpoints must keep working even if these are unset.
# EVONEX_WEBHOOK_TOKEN is a comma-separated list so a rotated token can be
# added alongside the old one.
EVONEX_WEBHOOK_TOKENS = [
    t.strip() for t in os.environ.get("EVONEX_WEBHOOK_TOKEN", "").split(",") if t.strip()
]
EVONEX_SHEET_ID = os.environ.get("EVONEX_SHEET_ID", "").strip()

# Incremental Leads sync. "" / "off": always a full sync (the default).
# "shadow": still a full sync, but also logs what an incremental run would have
# missed. "on" (or 1/true/yes): incremental, with a full refresh once a day.
INCREMENTAL_SYNC = os.environ.get("INCREMENTAL_SYNC", "").strip().lower()
FULL_REFRESH_HOUR_UTC = _env_number("FULL_REFRESH_HOUR_UTC", 3, int)

# Evonex -> GHL call write-back (see docs/ghl/GHL_Backend_Writeback_Spec.md).
# WRITEBACK_MODE: "off" (default, nothing runs), "dry_run" (does everything
# except the final PUT and records what it WOULD write in Writeback_Log), "on".
# Anything else is treated as off, so a typo can never switch writing on.
_wb_mode = os.environ.get("WRITEBACK_MODE", "off").strip().lower()
WRITEBACK_MODE = _wb_mode if _wb_mode in ("off", "dry_run", "on") else "off"
# Connected for at least this long = Answered, shorter = Voicemail (set by Talha, 21 Oct).
WRITEBACK_TALK_SECONDS = _env_number("WRITEBACK_TALK_SECONDS", 60, int)
# Quiet time after a call's last event before it is judged finished (Evonex sends
# CANCELCALL ~1 s before STARTCALL on about 4 % of connected calls).
WRITEBACK_SETTLE_SECONDS = _env_number("WRITEBACK_SETTLE_SECONDS", 5.0, float)
# Calls whose last event is older than this are never written (stops a replay
# after an outage from bumping counters hours or days late).
WRITEBACK_MAX_AGE_MINUTES = _env_number("WRITEBACK_MAX_AGE_MINUTES", 120, int)
# Advisors (spec open decision 8.1). Default = the options of the GHL advisor-name field.
WRITEBACK_ADVISOR_NAMES = [
    n.strip() for n in os.environ.get(
        "WRITEBACK_ADVISOR_NAMES",
        "Henny Lessey,Joe Nicholls,Xavier Collin,Cecilia Szabo,Carl Gladman,Anthony Fox",
    ).split(",") if n.strip()
]
# true = in Advisor Call phase only the case's own advisor is written; false =
# any caller in WRITEBACK_ADVISOR_NAMES is written (with a logged warning).
WRITEBACK_ADVISOR_STRICT = os.environ.get("WRITEBACK_ADVISOR_STRICT", "").strip().lower() in ("1", "true", "yes")

# Created By / Booked By locking, flags and the Appointments tab (see app/field_audit.py).
# "off" (default) does nothing; "on" runs one audit cycle inside every sync. Reads GHL,
# writes only the reporting sheet, never GHL.
FIELD_AUDIT = os.environ.get("FIELD_AUDIT", "off").strip().lower()
# Appointments booked before this date are never flagged (so switching the audit on does not
# flood the Flags tab with history). Empty = the last 7 days.
FIELD_AUDIT_SINCE = os.environ.get("FIELD_AUDIT_SINCE", "").strip()
# Calendars whose appointments are reported. Default: "S3 Appointment Calendar" (round robin).
APPOINTMENT_CALENDAR_IDS = [
    c.strip() for c in os.environ.get("APPOINTMENT_CALENDAR_IDS", "Ms3bV1PlOqPVqjguHa6b").split(",") if c.strip()
]
# Hours a freshly booked appointment may sit without Booked By before it is flagged.
BOOKED_BY_GRACE_HOURS = _env_number("BOOKED_BY_GRACE_HOURS", 24.0, float)
# The sales team the client pays on appointments and full fact finds (Sarah, 22 Oct: "currently Harry, Ava & Hugo").
SALES_TEAM_NAMES = [
    n.strip() for n in os.environ.get("SALES_TEAM_NAMES", "Harry Kalsi,Ava Morris-Boundy,Hugo Sharpe").split(",") if n.strip()
]
# createdBy.source values meaning "a logged-in person booked this from inside GHL". Their calendar booker is the
# official Booked By. Any other source (a public booking form, a widget, an API) falls back to the Booked By
# field on the contact, which the calendar's own form fills in.
TRUSTED_BOOKING_SOURCES = [
    s.strip() for s in os.environ.get("TRUSTED_BOOKING_SOURCES", "contactdetails_page").split(",") if s.strip()
]

# Call evidence (app/call_evidence.py): match each finished appointment to the advisor's Evonex call. Needs FIELD_AUDIT=on
# and EVONEX_SHEET_ID. "off" (default) does nothing; "on" adds the Call_Evidence and Call_Activity tabs and three flags.
# Reads the private Evonex sheet and GHL, writes only the reporting sheet.
_ce = os.environ.get("CALL_EVIDENCE", "off").strip().lower()
CALL_EVIDENCE = "on" if _ce == "on" else "off"
# A call counts as "around the appointment" when it started within this many minutes either side of the start time
# (widened from 120 to 180 on 23 Oct: an advisor sometimes starts the call well after the booked time).
CALL_MATCH_WINDOW_MINUTES = _env_number("CALL_MATCH_WINDOW_MINUTES", 180, int)
# Talk time that makes a connected call a real conversation (same as the write-back's Answered threshold) and a "long call"
# (a real fact find). Chosen from the first week of real calls (22 Oct 2026): advisors' matched calls were either under a
# minute or 9 to 55 minutes. Revisit once Asset Harbour confirms how long a fact find normally runs.
CALL_CONVERSATION_SECONDS = _env_number("CALL_CONVERSATION_SECONDS", 60, int)
CALL_LONG_SECONDS = _env_number("CALL_LONG_SECONDS", 600, int)
# Flag a booking whose booker is not the contact's owner (the sales person). OFF by default: in the first week of real data
# 31 of 37 bookings had an owner other than the booker (Anita, Alison, Sarah ...), so it would flag most appointments until
# lead ownership at stages 1 to 3 is cleaned up.
_bo = os.environ.get("BOOKER_OWNER_FLAG", "off").strip().lower()
BOOKER_OWNER_FLAG = "on" if _bo == "on" else "off"
BOOKER_OWNER_WINDOW_HOURS = _env_number("BOOKER_OWNER_WINDOW_HOURS", 48.0, float)
