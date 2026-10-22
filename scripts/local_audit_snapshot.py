"""Build the Sales Team / Flags data from LIVE GHL and save it for the local dashboard. Read-only.

    python scripts/local_audit_snapshot.py [--since-days 30]

Runs one audit cycle exactly as the sync would (calendar events, contacts, locks, flags, official booker) but
against an IN-MEMORY sheet, so nothing is written to Google Sheets or to GHL. The tables are saved to
../ghl-dashboard/.local-audit.json (git-ignored), which the local dev server shows in place of the Appointments,
Flags and Field_Locks tabs that do not exist in the real sheet until FIELD_AUDIT is switched on.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("EVONEX_SHEET_ID", "1p44kYCI20N61-djbT5MIAz89JqhnbxVg20WRnYF7d1c")   # the private Evonex sheet (read only here)
from app import audit_runner, config  # noqa: E402
from app.appointments import APPT_COLUMNS  # noqa: E402
from app.audit_store import AuditStores  # noqa: E402
from app.call_evidence import ACTIVITY_COLUMNS, EVIDENCE_COLUMNS  # noqa: E402
from app.field_audit import FLAG_COLUMNS, LOCK_COLUMNS  # noqa: E402
from app.ghl_client import GHLClient  # noqa: E402


class MemoryWorksheet:
    def __init__(self):
        self.values = []

    def row_values(self, n):
        return list(self.values[0]) if self.values else []

    def update(self, values=None, range_name=None, **kw):
        self.values = [list(r) for r in values] + self.values[1:]

    def get_all_values(self):
        return [list(r) for r in self.values]

    def append_rows(self, rows, **kw):
        self.values += [list(r) for r in rows]

    def batch_update(self, updates, **kw):
        for u in updates:
            row = int("".join(ch for ch in u["range"].split(":")[0] if ch.isdigit()))
            self.values[row - 1] = list(u["values"][0])


class MemorySheet:
    def __init__(self):
        self.tabs = {}

    def worksheet(self, title):
        import gspread
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = MemoryWorksheet()
        return self.tabs[title]


async def main(since_days: int) -> None:
    now = datetime.now(timezone.utc)
    stores = AuditStores(sheet=MemorySheet())
    cfg = SimpleNamespace(
        FIELD_AUDIT="on", FIELD_AUDIT_SINCE=(now - timedelta(days=since_days)).isoformat(),
        APPOINTMENT_CALENDAR_IDS=config.APPOINTMENT_CALENDAR_IDS, BOOKED_BY_GRACE_HOURS=config.BOOKED_BY_GRACE_HOURS,
        FULL_REFRESH_HOUR_UTC=config.FULL_REFRESH_HOUR_UTC, WRITEBACK_ADVISOR_NAMES=config.WRITEBACK_ADVISOR_NAMES,
        TRUSTED_BOOKING_SOURCES=config.TRUSTED_BOOKING_SOURCES,
        CALL_EVIDENCE="on", CALL_MATCH_WINDOW_MINUTES=config.CALL_MATCH_WINDOW_MINUTES, CALL_CONVERSATION_SECONDS=config.CALL_CONVERSATION_SECONDS,
        CALL_LONG_SECONDS=config.CALL_LONG_SECONDS, BOOKER_OWNER_FLAG=config.BOOKER_OWNER_FLAG, BOOKER_OWNER_WINDOW_HOURS=config.BOOKER_OWNER_WINDOW_HOURS,
    )
    from app.writeback_store import WritebackStore
    reader = WritebackStore().recent_events     # reads the private Evonex sheet; nothing is written to it
    result = await audit_runner.run_cycle(GHLClient(), stores, cfg, now=now, events_reader=reader)
    if result.get("error"):
        raise SystemExit(f"audit cycle failed: {result}")

    def table(columns, rows):
        return [columns] + [[str(r.get(c, "")) for c in columns] for r in rows]

    out = {
        "generated_at": now.isoformat(),
        "note": "built read-only from live GHL; nothing was written to Google Sheets or GHL",
        "appointments": table(APPT_COLUMNS, stores.appointments.read()),
        "flags": table(FLAG_COLUMNS, stores.flags.read()),
        "locks": table(LOCK_COLUMNS[:5], stores.locks.read()),
        "call_evidence": table(EVIDENCE_COLUMNS, stores.evidence.read()),
        "call_activity": table(ACTIVITY_COLUMNS, stores.activity.read()),
    }
    target = os.path.join(os.path.dirname(ROOT), "ghl-dashboard", ".local-audit.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    print(f"saved {target}")
    print(f"  appointments: {len(out['appointments']) - 1} | flags: {len(out['flags']) - 1} | created-by locks: {len([r for r in out['locks'][1:] if r[1] == 'created_by'])}")
    print(f"  call evidence: {len(out['call_evidence']) - 1} rows | call activity: {len(out['call_activity']) - 1} rows")
    print(f"  cycle: {result}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=30, help="judge Booked By / advisor flags for appointments booked in the last N days")
    asyncio.run(main(parser.parse_args().since_days))
