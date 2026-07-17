"""Google Sheets writer — upserts Leads rows and appends Sync_Log entries."""

import json
import logging
from datetime import datetime, timezone

import gspread
from gspread.utils import rowcol_to_a1

from app import config
from app.join import COLUMNS

logger = logging.getLogger(__name__)

LEADS_TAB = "Leads"
LOG_TAB = "Sync_Log"
LOG_HEADERS = ["timestamp", "records_processed", "status", "error_message"]


class SheetsClient:
    def __init__(self):
        creds_json = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
        self.gc = gspread.service_account_from_dict(creds_json)
        self.sheet = self.gc.open_by_key(config.GOOGLE_SHEET_ID)

    def _get_or_create_ws(self, title: str, cols: int):
        try:
            return self.sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            return self.sheet.add_worksheet(title=title, rows=1000, cols=cols)

    def upsert_leads(self, rows: list) -> dict:
        """Upsert rows into the Leads tab keyed on opportunity_id.

        Values are always written positionally in COLUMNS order — the header
        row is COLUMNS, and every data row is built from the same list.
        """
        ws = self._get_or_create_ws(LEADS_TAB, len(COLUMNS))
        existing = ws.get_all_values()

        if not existing or not any(existing[0]):
            ws.update(values=[COLUMNS], range_name="A1")
            existing = [COLUMNS]

        # opportunity_id (column B) is the true unique key — a GHL contact
        # can have multiple opportunities (verified live: 10 contacts
        # currently do). Keying on contact_id (column A) instead, as this
        # used to, collapses every opportunity under one contact into a
        # single lookup slot: when a sync batch contains 2+ opportunities
        # for the same contact, they all resolve to the same existing
        # row_num, so batch_update silently applies them in sequence and
        # only the last one survives — the others vanish from the sheet
        # with no error. Confirmed in production: Isabel Cristina De
        # Oliveira Campos has 2 opportunities (one 'abandoned', one
        # 'open') sharing one contact_id; the abandoned one was being
        # silently dropped every sync, leaving only the open one visible.
        index = {
            r[1]: row_num
            for row_num, r in enumerate(existing[1:], start=2)
            if r and len(r) > 1 and r[1]
        }

        def to_values(row: dict) -> list:
            values = []
            for col in COLUMNS:
                v = row.get(col, "")
                if v is None:
                    v = ""
                elif isinstance(v, bool):
                    v = str(v).upper()
                elif isinstance(v, list):
                    # multi-select GHL fields arrive as lists
                    v = ", ".join(str(item) for item in v)
                values.append(v)
            return values

        last_col = rowcol_to_a1(1, len(COLUMNS)).rstrip("1")
        updates = []
        appends = []
        updated = inserted = 0

        for row in rows:
            values = to_values(row)
            row_num = index.get(row["opportunity_id"])
            if row_num:
                updates.append({
                    "range": f"A{row_num}:{last_col}{row_num}",
                    "values": [values],
                })
                updated += 1
            else:
                appends.append(values)
                inserted += 1

        if updates:
            ws.batch_update(updates)
        if appends:
            ws.append_rows(appends, value_input_option="RAW")

        logger.info("Sheet upsert: %d updated, %d inserted", updated, inserted)
        return {"updated": updated, "inserted": inserted, "total": updated + inserted}

    def log_sync(self, status: str, records: int, error: str = ""):
        ws = self._get_or_create_ws(LOG_TAB, len(LOG_HEADERS))
        if not ws.acell("A1").value:
            ws.update(values=[LOG_HEADERS], range_name="A1")
        ws.append_row(
            [datetime.now(timezone.utc).isoformat(), records, status, error],
            value_input_option="RAW",
        )

    def get_last_log_entry(self):
        """Most recent Sync_Log row as a dict, or None if the tab has no data rows yet."""
        ws = self._get_or_create_ws(LOG_TAB, len(LOG_HEADERS))
        values = ws.get_all_values()
        if len(values) <= 1:
            return None
        last = values[-1]
        last = last + [""] * (len(LOG_HEADERS) - len(last))
        return dict(zip(LOG_HEADERS, last))
