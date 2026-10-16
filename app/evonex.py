"""Evonex webhook receiver: request verification, row building and durable storage.

Phase 1 only captures and stores every event. Rows are always appended (never
de-duplicated at write time); downstream consumers de-duplicate on dedup_key.
"""

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import unquote

import gspread
from gspread.utils import InsertDataOption, ValueInputOption

from app import config
from app.sheets_client import SheetsClient

logger = logging.getLogger("evonex")

TAB = "Evonex_Events"

# FIXED order. New columns may only be appended at the end, never inserted or
# reordered (same rule as the Leads tab).
COLUMNS = [
    "received_at_utc", "path", "event", "subevent", "ts", "cid", "dedup_key",
    "lpi", "rpi", "rpn", "opi", "sub", "context",
    "ctx_duration", "ctx_file_size", "ctx_start_time", "ctx_left_at", "ctx_url",
    "on", "un", "oa", "od", "oj", "or", "os", "oe", "om", "oref",
    "sbn", "lsn", "mac", "pn", "fn", "ua",
    "extra_fields", "signature_url_variant", "raw_json",
]

# Every payload key that has its own column (path/received_at/etc. are ours).
KNOWN_KEYS = {
    "ts", "event", "subevent", "cid", "lpi", "rpi", "rpn", "opi", "context",
    "sub", "mac", "pn", "fn", "ua", "on", "un", "oa", "od", "oj", "or", "os",
    "oe", "om", "oref", "sbn", "lsn",
}

# A Google Sheets cell holds at most 50,000 characters.
MAX_RAW_JSON = 45000
MAX_CELL = 49000

WRITE_RETRY_DELAYS = (0.5, 1.0, 2.0)
WRITE_DEADLINE_SECONDS = 20.0
SHEETS_HTTP_TIMEOUT = 8.0


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

def extract_token(authorization: str) -> str:
    """Strip an optional 'Bearer ' prefix (case-insensitive) and whitespace."""
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def token_is_valid(token: str) -> bool:
    """Constant-time compare against every allowed token (no early exit)."""
    ok = False
    for allowed in config.EVONEX_WEBHOOK_TOKENS:
        ok |= hmac.compare_digest(token.encode("utf-8"), allowed.encode("utf-8"))
    return ok


def compute_signature(token: str, url: str, body: bytes) -> str:
    """sha256=<lowercase hex> of HMAC-SHA256(key=token, msg=url + raw body)."""
    digest = hmac.new(token.encode("utf-8"), url.encode("utf-8") + body, hashlib.sha256)
    return "sha256=" + digest.hexdigest()


def signature_candidates(path: str, query: str, request_url: str) -> list:
    """URLs Evonex may have signed, as (variant, url) pairs, de-duplicated.

    (a) the configured public base URL + path (+ ?query when present)
    (b) the URL as this server saw it, with the scheme forced to https
    """
    a = config.PUBLIC_BASE_URL.rstrip("/") + path + (f"?{query}" if query else "")
    b = request_url
    if b.startswith("http://"):
        b = "https://" + b[len("http://"):]
    candidates = [("a", a)]
    if b != a:
        candidates.append(("b", b))
    return candidates


def matching_signature_variant(token: str, body: bytes, signature_header: str, candidates: list):
    """Return 'a' / 'b' for the first URL candidate whose signature matches,
    else None. Compared in constant time."""
    given = (signature_header or "").strip().encode("utf-8")
    matched = None
    for variant, url in candidates:
        expected = compute_signature(token, url, body).encode("utf-8")
        if hmac.compare_digest(expected, given) and matched is None:
            matched = variant
    return matched


# --------------------------------------------------------------------------
# Row building
# --------------------------------------------------------------------------

_CONTEXT_KEYS = {
    "duration": "ctx_duration",
    "filesize": "ctx_file_size",
    "starttime": "ctx_start_time",
    "leftat": "ctx_left_at",
    "url": "ctx_url",
}


def parse_context(context) -> dict:
    """Parse 'Duration:245;FileSize:1;StartTime:2026-04-20T11:55:00;Url:https%3A...'.

    Split on ';', then on the FIRST ':' only (values such as StartTime contain
    colons). Keys match case-insensitively; values are URL-decoded. Anything
    that is not key:value (e.g. '3600', a park bay, a group name) yields no
    parsed columns. Never raises.
    """
    parsed = {}
    try:
        if not isinstance(context, str) or not context:
            return parsed
        for part in context.split(";"):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            column = _CONTEXT_KEYS.get(key.strip().lower())
            if column:
                parsed[column] = unquote(value.strip())
    except Exception:
        return {}
    return parsed


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def make_dedup_key(path: str, payload: dict) -> str:
    """SHA-256 hex of cid|event|subevent|ts|un|path, nulls as empty strings."""
    parts = [
        _text(payload.get("cid")), _text(payload.get("event")),
        _text(payload.get("subevent")), _text(payload.get("ts")),
        _text(payload.get("un")), path,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def build_row(path: str, payload: dict, raw_text: str, variant: str, received_at: str) -> list:
    """One sheet row (list of strings) in COLUMNS order."""
    ctx = parse_context(payload.get("context"))

    raw_json = raw_text
    extra = {}
    if len(raw_json) > MAX_RAW_JSON:
        raw_json = raw_json[:MAX_RAW_JSON]
        # Inserted first so a huge unknown key can't push the flag out of the cell.
        extra["raw_json_truncated"] = True
    extra.update({k: v for k, v in payload.items() if k not in KNOWN_KEYS})
    extra_fields = (
        json.dumps(extra, ensure_ascii=False, separators=(",", ":")) if extra else ""
    )

    values = {
        "received_at_utc": received_at,
        "path": path,
        "event": _text(payload.get("event")),
        "subevent": _text(payload.get("subevent")),
        "ts": _text(payload.get("ts")),
        "cid": _text(payload.get("cid")),
        "dedup_key": make_dedup_key(path, payload),
        "extra_fields": extra_fields,
        "signature_url_variant": variant,
        "raw_json": raw_json,
    }
    for key in KNOWN_KEYS - {"ts", "event", "subevent", "cid"}:
        values[key] = _text(payload.get(key))
    values.update(ctx)

    row = []
    for column in COLUMNS:
        cell = values.get(column, "")
        limit = MAX_RAW_JSON if column == "raw_json" else MAX_CELL
        row.append(cell[:limit])
    return row


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

_worksheet_cache = {}


def _worksheet():
    """The Evonex_Events worksheet, created/headed on first use per cold start."""
    sheet_id = config.EVONEX_SHEET_ID
    cached = _worksheet_cache.get(sheet_id)
    if cached is not None:
        return cached

    client = SheetsClient(sheet_id=sheet_id)
    client.gc.set_timeout(SHEETS_HTTP_TIMEOUT)
    try:
        ws = client.sheet.worksheet(TAB)
    except gspread.WorksheetNotFound:
        ws = client.sheet.add_worksheet(title=TAB, rows=1000, cols=len(COLUMNS))

    header = ws.row_values(1)
    if not any(header):
        ws.update(values=[COLUMNS], range_name="A1")
    elif header[: len(COLUMNS)] != COLUMNS:
        logger.warning("Evonex_Events header row differs from the expected columns; not overwriting")

    _worksheet_cache[sheet_id] = ws
    return ws


def _append_to_sheet(values: list) -> None:
    """RAW so '+44...' numbers and ids stay text and nothing becomes a formula;
    INSERT_ROWS with an A1 anchor so rows always append below the header."""
    _worksheet().append_row(
        values,
        value_input_option=ValueInputOption.raw,
        insert_data_option=InsertDataOption.insert_rows,
        table_range="A1",
    )


def _retry_after_seconds(exc) -> float:
    """Honour a 429 Retry-After header (capped), else 0 to use normal backoff."""
    try:
        response = getattr(exc, "response", None)
        if response is not None and response.status_code == 429:
            return min(float(response.headers.get("Retry-After") or 0), 5.0)
    except (TypeError, ValueError):
        pass
    return 0.0


def write_row_with_retry(values: list) -> bool:
    """Append one row. One attempt plus up to 3 retries (0.5 s, 1 s, 2 s),
    stopping early past a total deadline so the request stays well inside the
    function timeout. Returns True only once the row is durably written."""
    started = time.monotonic()
    attempts = len(WRITE_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            _append_to_sheet(values)
            return True
        except Exception as exc:
            if attempt == attempts - 1 or time.monotonic() - started > WRITE_DEADLINE_SECONDS:
                logger.error("Evonex sheet write failed after %d attempt(s): %s", attempt + 1, type(exc).__name__)
                return False
            time.sleep(max(WRITE_RETRY_DELAYS[attempt], _retry_after_seconds(exc)))
    return False
