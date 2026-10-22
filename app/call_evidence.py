"""Call evidence for appointments, and daily call activity: the pure engine (no I/O).

Asset Harbour's advisor "meetings" are PHONE calls: the advisor rings the client from Evonex at the appointment time,
and the calendar entry only drives reminders. So the real proof that an appointment took place is an outbound Evonex
call by that advisor to that client's number around the appointment time.

For every appointment that has finished (start time + the match window + a settle period), this module looks for the
Evonex calls to the contact's phone number inside [start - window, start + window] and records what it found:

  result            meaning
  long_call         the appointment's advisor had a connected call of at least `long_seconds` (a real fact find call)
  conversation      a connected call of at least `conversation_seconds` but shorter than that
  short_call        connected, but shorter than `conversation_seconds` (voicemail, or hung up)
  not_answered      the advisor rang, nobody picked up
  other_caller      no call by the advisor, but someone else had a conversation with the client
  no_call           no call to that number in the window
  no_phone          the contact has no usable phone number, so nothing can be matched

The calendar status stays exactly as staff marked it (client decision): this evidence never changes a number. It only
raises flags where the two disagree (showed_without_call, noshow_with_call, unmarked_with_call).

Rows hold ids, staff names, durations and counts only; never a phone number.
"""

from datetime import datetime, timedelta, timezone

from app.appointments import is_live, parse_dt
from app.field_audit import _flag, flag_id
from app.writeback_rules import LONDON, classify, names_match, to_e164

EVIDENCE_COLUMNS = [
    "appointment_id", "contact_id", "advisor", "start_time", "result", "advisor_calls", "connected_calls",
    "best_talk_seconds", "first_connected_offset_min", "best_caller", "other_caller_calls", "other_caller_best_talk",
    "evaluated_at",
]
ACTIVITY_COLUMNS = ["day", "caller", "calls", "connected", "conversations", "long_calls", "no_answer", "talk_seconds"]

LONG_CALL, CONVERSATION, SHORT_CALL, NOT_ANSWERED = "long_call", "conversation", "short_call", "not_answered"
OTHER_CALLER, NO_CALL, NO_PHONE = "other_caller", "no_call", "no_phone"

FREEZE_HOURS = 36           # an appointment's evidence stops being recomputed this long after its match window closed
SETTLE_MINUTES = 15         # wait this long after the window closes for late ENDCALL events
UNMARKED_AFTER_HOURS = 24   # give staff a day to mark the appointment before "call happened, not marked" is raised


def call_time(info):
    """When the call started ringing (or connected, or was cancelled)."""
    return info.ring_ts or info.start_ts or info.cancel_ts or info.end_ts


def _stamp(now) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def calls_by_phone(calls: dict) -> dict:
    """{e164: [CallInfo]} for every call with a usable number and a time."""
    out = {}
    for info in calls.values():
        phone = to_e164(info.rpi)
        if phone and call_time(info):
            out.setdefault(phone, []).append(info)
    return out


def _summarise(calls: list, conversation_seconds: int, wanted) -> dict:
    """Counts and the best (longest) connected call among `calls` that `wanted` accepts."""
    total = connected = 0
    best_talk, best_caller, first_connect, picked = 0, "", None, False
    for info in calls:
        if not wanted(info):
            continue
        outcome, talk = classify(info, conversation_seconds)
        if outcome is None:
            continue                                  # still in progress or never finished
        total += 1
        if info.start_ts is not None:
            connected += 1
            if first_connect is None or info.start_ts < first_connect:
                first_connect = info.start_ts
            if not picked or talk > best_talk:
                picked, best_talk, best_caller = True, talk, info.owner_name
    return {"calls": total, "connected": connected, "best_talk": best_talk, "best_caller": best_caller, "first_connect": first_connect}


def is_ready(appt: dict, existing: dict, now, window_minutes: int, coverage_start=None) -> bool:
    """True when this appointment's evidence should be (re)computed now."""
    if not is_live(appt):
        return False
    start = parse_dt(appt.get("start_time"))
    window = timedelta(minutes=window_minutes)
    if start is None or now < start + window + timedelta(minutes=SETTLE_MINUTES):
        return False                                  # the window has not closed yet
    if appt["appointment_id"] in existing and now > start + window + timedelta(hours=FREEZE_HOURS):
        return False                                  # frozen
    return not (coverage_start is not None and start - window < coverage_start)   # Evonex history must reach back this far


def build_evidence(appointments: list, phones: dict, by_phone: dict, existing: dict, now, *, window_minutes: int = 120,
                   conversation_seconds: int = 60, long_seconds: int = 600, coverage_start=None) -> list:
    """Evidence rows for every appointment that is ready to judge.

    `phones` maps contact_id -> E.164 number; `by_phone` is calls_by_phone(); `existing` maps appointment_id -> the row
    already stored (frozen rows are left alone); `coverage_start` is the earliest Evonex event time that was read, so an
    appointment whose window starts before it is skipped rather than wrongly judged "no_call".
    """
    window = timedelta(minutes=window_minutes)
    rows = []
    for appt in appointments:
        if not is_ready(appt, existing, now, window_minutes, coverage_start):
            continue
        start = parse_dt(appt["start_time"])
        phone = phones.get(appt.get("contact_id", ""))
        row = {
            "appointment_id": appt["appointment_id"], "contact_id": appt.get("contact_id", ""), "advisor": appt.get("advisor", ""),
            "start_time": appt.get("start_time", ""), "result": NO_PHONE, "advisor_calls": 0, "connected_calls": 0,
            "best_talk_seconds": 0, "first_connected_offset_min": "", "best_caller": "", "other_caller_calls": 0,
            "other_caller_best_talk": 0, "evaluated_at": _stamp(now),
        }
        if phone:
            nearby = [c for c in by_phone.get(phone, []) if abs(call_time(c) - start) <= window]
            advisor = appt.get("advisor", "")
            mine = _summarise(nearby, conversation_seconds, lambda c: bool(advisor) and names_match(c.owner_name, advisor))
            others = _summarise(nearby, conversation_seconds, lambda c: not (advisor and names_match(c.owner_name, advisor)))
            if mine["best_talk"] >= long_seconds:
                result = LONG_CALL
            elif mine["best_talk"] >= conversation_seconds:
                result = CONVERSATION
            elif mine["connected"]:
                result = SHORT_CALL
            elif mine["calls"]:
                result = NOT_ANSWERED
            elif others["best_talk"] >= conversation_seconds:
                result = OTHER_CALLER
            else:
                result = NO_CALL
            offset = ""
            if mine["first_connect"] is not None:
                offset = round((mine["first_connect"] - start).total_seconds() / 60, 1)
            row.update(
                result=result, advisor_calls=mine["calls"], connected_calls=mine["connected"], best_talk_seconds=mine["best_talk"],
                first_connected_offset_min=offset, best_caller=mine["best_caller"], other_caller_calls=others["calls"],
                other_caller_best_talk=others["best_talk"],
            )
        rows.append(row)
    return rows


def evidence_flags(evidence: list, appointments: list, now) -> list:
    """Disagreements between what staff marked on the calendar and what the phone records show."""
    by_id = {a["appointment_id"]: a for a in appointments}
    flags = []
    for row in evidence:
        appt = by_id.get(row["appointment_id"])
        if appt is None:
            continue
        status, result, snap = appt.get("status", ""), row["result"], {"contact_id": row["contact_id"]}
        start = parse_dt(appt.get("start_time"))
        real_call = result in (LONG_CALL, CONVERSATION)
        kind = detail = None
        if status == "showed" and result in (NO_CALL, NOT_ANSWERED, SHORT_CALL):
            kind, detail = "showed_without_call", "marked Showed, but the advisor has no connected call of a minute or more with the client around that time"
        elif status == "noshow" and real_call:
            kind, detail = "noshow_with_call", "marked No-show, but the advisor had a real conversation with the client around that time"
        elif status in ("", "new", "confirmed") and real_call and start and now >= start + timedelta(hours=UNMARKED_AFTER_HOURS):
            kind, detail = "unmarked_with_call", "the advisor spoke to the client, but the appointment was never marked Showed or No-show"
        if kind:
            flag = _flag(kind, snap, "appointment_status", status or "(blank)", result, "", now, row["appointment_id"], detail)
            flag["flag_id"] = flag_id(kind, row["contact_id"], "appointment_status", "", "", row["appointment_id"])   # once per appointment
            flags.append(flag)
    return flags


def build_call_activity(calls: dict, coverage_start=None) -> list:
    """Outbound calls per London day per caller. Days that begin before `coverage_start` are left out because the
    events that were read may only hold part of them."""
    agg = {}
    for info in calls.values():
        when = call_time(info)
        outcome, talk = classify(info, 60)
        if when is None or outcome is None:
            continue
        local = when.astimezone(LONDON)
        day_start = datetime(local.year, local.month, local.day, tzinfo=LONDON).astimezone(timezone.utc)
        if coverage_start is not None and day_start < coverage_start:
            continue
        key = (local.date().isoformat(), info.owner_name or "(unknown)")
        a = agg.setdefault(key, {"calls": 0, "connected": 0, "conversations": 0, "long_calls": 0, "no_answer": 0, "talk_seconds": 0})
        a["calls"] += 1
        if info.start_ts is None:
            a["no_answer"] += 1
        else:
            a["connected"] += 1
            a["talk_seconds"] += talk
            a["conversations"] += talk >= 60
            a["long_calls"] += talk >= 600
    return [{"day": d, "caller": c, **v} for (d, c), v in sorted(agg.items())]
