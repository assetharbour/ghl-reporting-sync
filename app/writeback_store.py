"""Google Sheets side of the write-back: read recent Evonex events, and keep
the Writeback_Log (idempotency keys, claims, audit trail) in the same private
"Evonex Events" sheet.

Writeback_Log is append-only. Rows hold ids, staff names and outcomes only:
no customer phone numbers, names or emails.
"""

import logging
import threading
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
TAIL_SLACK_ROWS = 1500           # how far past the remembered last row a read looks (rows other instances appended since)
DONE_REMEMBERED = 500            # call ids this instance already finished, to skip duplicate deliveries without touching the sheet

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
    """Google allows about 60 read requests a minute per user and the whole system shares that, so every read here is deliberate:
    the spreadsheet and both worksheets are opened once per warm instance (see shared_store), and once a tab's last row is known a
    tail read is ONE request (a range that runs past the end is trimmed by Google) instead of two (count the rows, then read)."""

    _events_total = None       # last known data row of Evonex_Events / Writeback_Log (None = unknown: count them once)
    _log_total = None
    _done_cids = None

    def __init__(self):
        client = SheetsClient(sheet_id=config.EVONEX_SHEET_ID)
        client.gc.set_timeout(evonex.SHEETS_HTTP_TIMEOUT)
        self._sheet = client.sheet
        self._events = None
        self._log = None

    @property
    def done(self) -> dict:
        """Call ids this instance has already closed with a final row (insertion ordered, bounded)."""
        if self._done_cids is None:
            self._done_cids = {}
        return self._done_cids

    def remember_done(self, cid: str) -> None:
        done = self.done
        done[cid] = True
        while len(done) > DONE_REMEMBERED:
            done.pop(next(iter(done)))

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

    def _tail(self, ws, last_col: str, window: int, attr: str) -> list:
        """The last `window` data rows of a tab (header excluded). One request when the last row is remembered, two when it is not."""
        hint = getattr(self, attr)
        if hint is not None:
            first, end = max(2, hint - window + 1), hint + TAIL_SLACK_ROWS
            values = _with_retry(lambda: ws.get(f"A{first}:{last_col}{end}", value_render_option="UNFORMATTED_VALUE"))
            if len(values) >= min(window, hint - 1) and len(values) < end - first + 1:
                setattr(self, attr, first - 1 + len(values))     # the range ran past the data, so this really is the end of it
                return values
            setattr(self, attr, None)                            # too few rows (the remembered position was too high) or the slack was used up
        total = len(_with_retry(lambda: ws.col_values(1)))      # one column: cheap
        setattr(self, attr, max(total, 1))
        if total < 2:
            return []
        first = max(2, total - window + 1)
        return _with_retry(lambda: ws.get(f"A{first}:{last_col}{total}", value_render_option="UNFORMATTED_VALUE"))

    def recent_events(self, window: int = EVENT_WINDOW_ROWS) -> list:
        """The last `window` rows of Evonex_Events as dicts (columns A..Z only)."""
        values = self._tail(self._events_ws(), "Z", window, "_events_total")
        header = evonex.COLUMNS[:EVENT_COLUMNS_READ]
        return [dict(zip(header, [str(c) for c in row], strict=False)) for row in values]

    # ---- log -------------------------------------------------------------

    def recent_log(self, window: int = 1500) -> list:
        values = self._tail(self._log_ws(), "L", window, "_log_total")
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


_shared = None
_shared_lock = threading.Lock()


def shared_store() -> "WritebackStore":
    """One store per warm serverless instance: the spreadsheet and its worksheets are opened once, not on every call."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = WritebackStore()
        return _shared
