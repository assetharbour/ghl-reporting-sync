"""Google Sheets side of the write-back: read recent Evonex events, and keep
the Writeback_Log (idempotency keys, claims, audit trail) in the same private
"Evonex Events" sheet.

Writeback_Log is append-only. Rows hold ids, staff names and outcomes only:
no customer phone numbers, names or emails.
"""

import logging
import time
from datetime import datetime, timezone

import gspread
from gspread.utils import InsertDataOption, ValueInputOption

from app import config, evonex
from app.sheets_client import SheetsClient

logger = logging.getLogger("writeback")

LOG_TAB = "Writeback_Log"
LOG_COLUMNS = [
    "logged_at_utc", "cid", "status", "mode", "contact_id", "pair", "outcome",
    "caller", "count_before", "count_after", "reason", "claim_token",
]
EVENT_COLUMNS_READ = 26          # A..Z covers everything up to and including `oe`
EVENT_WINDOW_ROWS = 1500         # about three days of traffic

# Statuses
CLAIMED = "claimed"
FINAL = {"written", "written_unverified", "dry_run", "skipped"}   # never processed again
FAILED = "failed"                                                  # retried by the sweeper


RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)


def _with_retry(fn, delays=RETRY_DELAYS):
    """Sheets read/append with backoff (429 and transient errors). The closing
    log row matters most: if it were lost after a successful GHL write the call
    could be written again, so appends retry for ~15 s before giving up."""
    for attempt in range(len(delays) + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == len(delays):
                logger.error("Writeback sheet call failed after %d attempts: %s", attempt + 1, type(exc).__name__)
                raise
            time.sleep(delays[attempt])


class WritebackStore:
    def __init__(self):
        client = SheetsClient(sheet_id=config.EVONEX_SHEET_ID)
        client.gc.set_timeout(evonex.SHEETS_HTTP_TIMEOUT)
        self._sheet = client.sheet
        self._events = None
        self._log = None

    # ---- worksheets ------------------------------------------------------

    def _events_ws(self):
        if self._events is None:
            self._events = self._sheet.worksheet(evonex.TAB)
        return self._events

    def _log_ws(self):
        if self._log is None:
            try:
                ws = self._sheet.worksheet(LOG_TAB)
            except gspread.WorksheetNotFound:
                ws = self._sheet.add_worksheet(title=LOG_TAB, rows=1000, cols=len(LOG_COLUMNS))
            if not any(ws.row_values(1)):
                ws.update(values=[LOG_COLUMNS], range_name="A1")
            self._log = ws
        return self._log

    # ---- events ----------------------------------------------------------

    def recent_events(self, window: int = EVENT_WINDOW_ROWS) -> list:
        """The last `window` rows of Evonex_Events as dicts (columns A..Z only)."""
        ws = self._events_ws()
        total = len(_with_retry(lambda: ws.col_values(1)))      # one column: cheap
        if total < 2:
            return []
        first = max(2, total - window + 1)
        values = _with_retry(lambda: ws.get(f"A{first}:Z{total}", value_render_option="UNFORMATTED_VALUE"))
        header = evonex.COLUMNS[:EVENT_COLUMNS_READ]
        return [dict(zip(header, [str(c) for c in row], strict=False)) for row in values]

    # ---- log -------------------------------------------------------------

    def recent_log(self, window: int = 1500) -> list:
        ws = self._log_ws()
        total = len(_with_retry(lambda: ws.col_values(1)))
        if total < 2:
            return []
        first = max(2, total - window + 1)
        values = _with_retry(lambda: ws.get(f"A{first}:L{total}", value_render_option="UNFORMATTED_VALUE"))
        return [dict(zip(LOG_COLUMNS, [str(c) for c in row], strict=False)) for row in values]

    def append_log(self, **fields) -> None:
        row = {"logged_at_utc": datetime.now(timezone.utc).isoformat()}
        row.update(fields)
        ws = self._log_ws()
        values = [str(row.get(c, "")) for c in LOG_COLUMNS]
        _with_retry(lambda: ws.append_row(
            values,
            value_input_option=ValueInputOption.raw,
            insert_data_option=InsertDataOption.insert_rows,
            table_range="A1",
        ))
