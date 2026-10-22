"""Pure rules for the Evonex -> GHL call write-back. No I/O: everything here is
a function of its arguments so it can be tested exhaustively.

The backend writes FACTS (call status, attempt counter, last call date/by);
GHL workflows make every decision. See docs/ghl/GHL_Backend_Writeback_Spec.md.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

# Contact custom field IDs (spec section 4; confirmed live 22 Oct 2026).
F_ADMIN_STATUS = "64aVYbvKIcwrgnZjQt57"
F_ADMIN_COUNT = "8ja7M6RflYbgaOza5Lx0"
F_ADVISOR_STATUS = "USSbwwmZyvqVHHx7EM1c"
F_ADVISOR_COUNT = "9s0YPzkLjWZzYLI6VjDd"
F_LAST_CALL_AT = "TPQm1GlwV11DifBaERbf"
F_LAST_CALL_BY = "LqzrVGm9327wuMKUvcqm"
F_CALL_PHASE = "y1Bjm1Afle54l8kmZ4uu"          # read only
F_ADVISOR_NAME = "7gGFW09JD3ByCBZW5PQQ"        # read only
F_ADVISOR_EMAIL = "1c6nZSPbkrEJkAeUVBZV"       # read only

# Golden rule 1: these are the ONLY fields the backend may ever write.
WRITE_FIELD_IDS = frozenset({
    F_ADMIN_STATUS, F_ADMIN_COUNT, F_ADVISOR_STATUS, F_ADVISOR_COUNT,
    F_LAST_CALL_AT, F_LAST_CALL_BY,
})

ADMIN_PHASE = "admin call"
ADVISOR_PHASE = "advisor call"

# Evonex sends CANCELCALL even for calls that then connect (about 4 % of calls,
# the cancel arrives ~1 s BEFORE the STARTCALL), so a cancel is only final once
# nothing else has arrived for a settle period.
OUTBOUND = "OUTBOUNDCALL"


# --------------------------------------------------------------------------
# Phone numbers
# --------------------------------------------------------------------------

def to_e164(raw) -> Optional[str]:
    """Normalise a UK-style number to E.164 (+44...). None if unusable.

    Evonex `rpi` is UK national format (07..., 01..., 03...); GHL stores E.164.
    """
    # ASCII digits only (\d would also accept other scripts' digits), and a '+' is only valid as the first character.
    s = re.sub(r"[^0-9+]", "", str(raw or ""))
    if not s or "+" in s[1:]:
        return None
    if s.startswith("+"):
        digits = s[1:]
        if digits.startswith("440") and len(digits) == 13:
            digits = "44" + digits[3:]            # "+44 (0)7700 ..." typed with the trunk zero still in
    elif s.startswith("00"):
        digits = s[2:]
    elif s.startswith("0") and len(s) == 11:
        digits = "44" + s[1:]
    elif s.startswith("44") and len(s) == 12:
        digits = s
    else:
        return None
    return "+" + digits if 8 <= len(digits) <= 15 else None


def search_candidates(e164: str) -> list:
    """Forms a GHL contact search may need: E.164 first, then national, then
    the bare digits."""
    out = [e164]
    if e164.startswith("+44"):
        out.append("0" + e164[3:])
    out.append(e164.lstrip("+"))
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------
# Events -> one call
# --------------------------------------------------------------------------

def parse_ts(value) -> Optional[datetime]:
    """Evonex timestamps are ISO-8601 UTC with a trailing Z. Never raises."""
    try:
        text = str(value or "").strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class CallInfo:
    cid: str
    rpi: str = ""
    owner_name: str = ""
    owner_email: str = ""
    ring_ts: Optional[datetime] = None
    start_ts: Optional[datetime] = None
    end_ts: Optional[datetime] = None
    cancel_ts: Optional[datetime] = None
    last_received: Optional[datetime] = None
    event_count: int = 0

    @property
    def finish_ts(self) -> Optional[datetime]:
        return self.end_ts or self.cancel_ts or self.start_ts or self.ring_ts


def collapse_events(rows) -> dict:
    """Group raw Evonex_Events rows (dicts) into one CallInfo per cid.

    Outbound only; duplicates (same dedup_key) are dropped; order is by the
    event ts, never by arrival. For repeats the first ring/start/cancel and the
    last end are kept.
    """
    calls, seen = {}, set()
    for r in rows:
        if r.get("event") != OUTBOUND or not r.get("cid"):
            continue
        # Scoped by cid: a duplicate delivery repeats the same cid AND key, while two different calls must
        # never be merged because a key was blank or corrupted (property test found the order dependence).
        key = (r["cid"], r.get("dedup_key") or "|".join(str(r.get(k, "")) for k in ("subevent", "ts")))
        if key in seen:
            continue
        seen.add(key)

        info = calls.setdefault(r["cid"], CallInfo(cid=r["cid"]))
        info.event_count += 1
        ts = parse_ts(r.get("ts"))
        received = parse_ts(r.get("received_at_utc"))
        if received and (info.last_received is None or received > info.last_received):
            info.last_received = received
        if r.get("rpi") and not info.rpi:
            info.rpi = r["rpi"]
        if r.get("on") and not info.owner_name:
            info.owner_name = r["on"]
        if r.get("oe") and not info.owner_email:
            info.owner_email = r["oe"]
        if ts is None:
            continue
        sub = (r.get("subevent") or "").upper()
        if sub == "RINGCALL" and (info.ring_ts is None or ts < info.ring_ts):
            info.ring_ts = ts
        elif sub == "STARTCALL" and (info.start_ts is None or ts < info.start_ts):
            info.start_ts = ts
        elif sub == "CANCELCALL" and (info.cancel_ts is None or ts < info.cancel_ts):
            info.cancel_ts = ts
        elif sub == "ENDCALL" and (info.end_ts is None or ts > info.end_ts):
            info.end_ts = ts
    return calls


def is_terminal(info: CallInfo, now: datetime, settle_seconds: float) -> bool:
    """A call is finished when ENDCALL arrived, or when it was cancelled and
    never connected; in both cases only after `settle_seconds` of quiet (late
    STARTCALL / ENDCALL for the same cid may still be on the way)."""
    finished = info.end_ts is not None or (info.cancel_ts is not None and info.start_ts is None)
    if not finished:
        return False
    if info.last_received is None:
        return True
    return (now - info.last_received).total_seconds() >= settle_seconds


def classify(info: CallInfo, talk_threshold_seconds: int):
    """Outcome per spec 5.3. Returns (outcome, talk_seconds); outcome is None
    while the call is not finished.

    No STARTCALL                        -> No Answer
    STARTCALL, talk < threshold          -> Voicemail
    STARTCALL, talk >= threshold         -> Answered
    """
    if info.start_ts is None:
        finished = info.end_ts is not None or info.cancel_ts is not None
        return ("No Answer", 0) if finished else (None, 0)
    if info.end_ts is None:
        return None, 0
    talk = max(0, int((info.end_ts - info.start_ts).total_seconds()))
    return ("Answered" if talk >= talk_threshold_seconds else "Voicemail"), talk


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

def _name_tokens(name) -> list:
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z\s'-]", " ", text).lower()
    return text.split()


def names_match(a, b) -> bool:
    """Same person by name: identical, or same first and last word (so the
    Evonex 'Ava Mae Morris-Boundy' matches the GHL 'Ava Morris-Boundy')."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    return len(ta) >= 2 and len(tb) >= 2 and ta[0] == tb[0] and ta[-1] == tb[-1]


def _same_email(a, b) -> bool:
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    return bool(a) and a == b


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------

@dataclass
class Decision:
    write: bool
    reason: str = ""
    pair: Optional[str] = None            # "admin" | "advisor"
    status: Optional[str] = None
    count_before: Optional[int] = None
    new_count: Optional[int] = None
    fields: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def as_int(value) -> int:
    """GHL number fields arrive as int, float, numeric string, '' or None."""
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError, OverflowError):    # OverflowError: float('inf')
        return 0


def london_date(ts: datetime) -> str:
    """Date written to last_call_at: the call's date in Europe/London.
    A wrong timezone means a wrong day, which breaks the 'is Today' checks."""
    return ts.astimezone(LONDON).date().isoformat()


def decide(info: CallInfo, outcome: str, contact_values: dict, advisor_names, advisor_strict: bool) -> Decision:
    """Spec 5.4: pick the pair from call_phase (read fresh) and build the update.

    `contact_values` maps custom-field id -> current value from a FRESH read.
    """
    phase = str(contact_values.get(F_CALL_PHASE) or "").strip().lower()
    warnings = []

    if phase == ADMIN_PHASE:
        pair, count_id, status_id = "admin", F_ADMIN_COUNT, F_ADMIN_STATUS
        status = outcome
    elif phase == ADVISOR_PHASE:
        named = contact_values.get(F_ADVISOR_NAME)
        named_email = contact_values.get(F_ADVISOR_EMAIL)
        # Spec 5.4: Evonex owner name = advisor-name, OR owner email = Advisor Email.
        is_case_advisor = names_match(info.owner_name, named) or _same_email(info.owner_email, named_email)
        if not is_case_advisor:
            in_list = any(names_match(info.owner_name, n) for n in advisor_names)
            if advisor_strict or not in_list:
                return Decision(False, "advisor_not_matched")
            warnings.append("caller_in_advisor_list_but_not_the_named_advisor")
        pair, count_id, status_id = "advisor", F_ADVISOR_COUNT, F_ADVISOR_STATUS
        # advisor_call_status has no Voicemail option: a short connected call is a No Answer
        status = "Answered" if outcome == "Answered" else "No Answer"
    else:
        return Decision(False, "phase_" + (phase.replace(" ", "_") or "empty"))

    before = as_int(contact_values.get(count_id))
    after = before + 1
    finish = info.finish_ts
    if finish is None:
        return Decision(False, "no_timestamp")
    fields = [
        {"id": status_id, "field_value": status},
        {"id": count_id, "field_value": after},
        {"id": F_LAST_CALL_AT, "field_value": london_date(finish)},
        {"id": F_LAST_CALL_BY, "field_value": info.owner_name},
    ]
    return Decision(True, "ok", pair, status, before, after, fields, warnings)


def field_value_matches(expected, actual) -> bool:
    """Compare a value we sent with what GHL returns (dates come back as ISO
    datetimes or epoch milliseconds; numbers may come back as float/str)."""
    if isinstance(expected, int) and not isinstance(expected, bool):
        return as_int(actual) == expected and str(actual).strip() not in ("", "None")
    exp = str(expected)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp):
        if isinstance(actual, (int, float)) or str(actual).isdigit():
            try:
                return datetime.fromtimestamp(int(actual) / 1000, tz=timezone.utc).date().isoformat() == exp
            except (ValueError, OverflowError, OSError):
                return False
        return str(actual or "")[:10] == exp
    return str(actual or "").strip() == exp.strip()
