"""Google Sheets tabs for the appointment / lock / flag audit (all in the private
Leads spreadsheet, next to Leads and Sync_Log).

  Appointments  one row per calendar appointment, upserted by appointment_id
  Field_Locks   first value ever seen for Created By / Booked By per contact
  Flags         append-only list of changes and mismatches (de-duplicated by id)
  Audit_State   one row remembering when the audit last completed
  Call_Evidence one row per finished appointment: what the advisor's Evonex call looked like (app/call_evidence.py)
  Call_Activity outbound calls per London day per caller (counts and talk time only)
  Lead_Speed    one row per new lead: minutes to the first call and to the first connected call (app/lead_speed.py)

Rows hold ids, staff names and statuses only. No client names, phone numbers or
emails: join to the Leads tab by contact_id for anything about the client.
"""

import logging

from gspread.utils import InsertDataOption, ValueInputOption, rowcol_to_a1
import gspread

from app import config
from app.appointments import APPT_COLUMNS
from app.call_evidence import ACTIVITY_COLUMNS, EVIDENCE_COLUMNS
from app.lead_speed import SPEED_COLUMNS
from app.field_audit import FLAG_COLUMNS, LOCK_COLUMNS
from app.sheets_client import SheetsClient
from app.writeback_store import _with_retry

logger = logging.getLogger("audit")

APPOINTMENTS_TAB = "Appointments"
LOCKS_TAB = "Field_Locks"
FLAGS_TAB = "Flags"
STATE_TAB = "Audit_State"
EVIDENCE_TAB = "Call_Evidence"
ACTIVITY_TAB = "Call_Activity"
SPEED_TAB = "Lead_Speed"
STATE_COLUMNS = ["key", "value"]


class TabStore:
    """A small keyed table on one worksheet. The header row is the contract:
    columns are only ever appended at the end, never inserted or reordered."""

    def __init__(self, sheet, title: str, headers: list, key_cols: list):
        self.sheet, self.title, self.headers, self.key_cols = sheet, title, headers, key_cols
        self._ws_cache = None

    def _ws(self):
        if self._ws_cache is None:
            try:
                ws = self.sheet.worksheet(self.title)
            except gspread.WorksheetNotFound:
                ws = self.sheet.add_worksheet(title=self.title, rows=1000, cols=len(self.headers))
            header = _with_retry(lambda: ws.row_values(1))
            if not any(header):
                _with_retry(lambda: ws.update(values=[self.headers], range_name="A1"))
            elif len(header) < len(self.headers) and list(header) == list(self.headers[: len(header)]):
                # Columns were appended in a newer version: extend the header row so the dashboard sees them.
                _with_retry(lambda: ws.update(values=[self.headers], range_name="A1"))
            self._ws_cache = ws
        return self._ws_cache

    def _key(self, row: dict) -> tuple:
        return tuple(str(row.get(c, "")) for c in self.key_cols)

    def _values(self, row: dict) -> list:
        return [str(row.get(c, "")) for c in self.headers]

    def read(self) -> list:
        values = _with_retry(lambda: self._ws().get_all_values())
        return [dict(zip(self.headers, r, strict=False)) for r in values[1:] if any(r)]

    def append(self, rows: list) -> int:
        if not rows:
            return 0
        ws = self._ws()
        _with_retry(lambda: ws.append_rows(
            [self._values(r) for r in rows],
            value_input_option=ValueInputOption.raw,
            insert_data_option=InsertDataOption.insert_rows,
            table_range="A1",
        ))
        return len(rows)

    def upsert(self, rows: list, existing: list = None) -> dict:
        """Update rows whose key exists and whose values changed; append the rest.
        Unchanged rows cost no writes."""
        if not rows:
            return {"inserted": 0, "updated": 0}
        ws = self._ws()
        current = existing if existing is not None else _with_retry(lambda: ws.get_all_values())[1:]
        index = {}
        for offset, values in enumerate(current):
            row = dict(zip(self.headers, values, strict=False))
            index[self._key(row)] = (offset + 2, [str(v) for v in values])  # +2: header + 1-based

        updates, inserts = [], []
        for row in rows:
            key, wanted = self._key(row), self._values(row)
            hit = index.get(key)
            if hit is None:
                inserts.append(wanted)
                index[key] = (None, wanted)             # a duplicate in this batch updates, not re-inserts
            elif hit[0] is not None and hit[1][: len(wanted)] != wanted:
                updates.append({"range": f"A{hit[0]}:{rowcol_to_a1(hit[0], len(wanted))}", "values": [wanted]})
        if updates:
            _with_retry(lambda: ws.batch_update(updates, value_input_option=ValueInputOption.raw))
        if inserts:
            _with_retry(lambda: ws.append_rows(
                inserts, value_input_option=ValueInputOption.raw,
                insert_data_option=InsertDataOption.insert_rows, table_range="A1",
            ))
        return {"inserted": len(inserts), "updated": len(updates)}


class AuditStores:
    def __init__(self, sheet=None):
        self.sheet = sheet or SheetsClient(sheet_id=config.GOOGLE_SHEET_ID).sheet
        self.appointments = TabStore(self.sheet, APPOINTMENTS_TAB, APPT_COLUMNS, ["appointment_id"])
        self.locks = TabStore(self.sheet, LOCKS_TAB, LOCK_COLUMNS, ["contact_id", "field"])
        self.flags = TabStore(self.sheet, FLAGS_TAB, FLAG_COLUMNS, ["flag_id"])
        self.state = TabStore(self.sheet, STATE_TAB, STATE_COLUMNS, ["key"])
        self.evidence = TabStore(self.sheet, EVIDENCE_TAB, EVIDENCE_COLUMNS, ["appointment_id"])
        self.activity = TabStore(self.sheet, ACTIVITY_TAB, ACTIVITY_COLUMNS, ["day", "caller"])
        self.speed = TabStore(self.sheet, SPEED_TAB, SPEED_COLUMNS, ["opportunity_id"])

    def get_state(self, key: str) -> str:
        for row in self.state.read():
            if row.get("key") == key:
                return row.get("value", "")
        return ""

    def set_state(self, key: str, value: str) -> None:
        self.state.upsert([{"key": key, "value": value}])
