"""Call evidence: matching an advisor's Evonex call to the appointment, the three disagreement flags, daily call
activity, the optional booker-is-not-the-owner flag, and the runner step that ties them together."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import audit_runner as runner
from app import call_evidence as ce
from app import field_audit as fa
from app.appointments import events_to_rows
from app.audit_store import AuditStores
from app.writeback_rules import CallInfo
from tests.test_audit import ADVISORS, NOW, USERS, FakeGHL, FakeSheet, booked_hours_ago, cfg, contact, event, judge, snap, types

PHONE = "+447700900123"
START = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
LATER = START + timedelta(hours=40)        # evidence window closed and the unmarked grace period over


def at(minutes):
    return START + timedelta(minutes=minutes)


def call(cid, caller="Joe Nicholls", ring=0, talk=None, phone="07700900123", cancel_only=False):
    """A call ringing `ring` minutes after the appointment start; talk=None means it never connected."""
    info = CallInfo(cid=cid, rpi=phone, owner_name=caller, ring_ts=at(ring))
    if cancel_only:
        info.cancel_ts = at(ring) + timedelta(seconds=20)
    elif talk is None:
        info.end_ts = at(ring) + timedelta(seconds=25)
    else:
        info.start_ts = at(ring) + timedelta(seconds=10)
        info.end_ts = info.start_ts + timedelta(seconds=talk)
    return info


def appt(**kw):
    base = dict(appointment_id="a1", contact_id="c1", advisor="Joe Nicholls", start_time="2026-10-21T15:00:00Z", status="showed", deleted="")
    base.update(kw)
    return base


def evidence(calls, appts=None, phones=None, existing=None, now=LATER, **kw):
    return ce.build_evidence(appts or [appt()], PHONE_MAP if phones is None else phones, ce.calls_by_phone({c.cid: c for c in calls}),
                             existing or {}, now, **kw)


PHONE_MAP = {"c1": PHONE}


def result_of(calls, **kw):
    rows = evidence(calls, **kw)
    return rows[0]["result"] if rows else None


# ---------------------------------------------------------------- matching and the result categories

def test_a_long_connected_call_by_the_advisor_is_a_long_call_and_records_the_details():
    rows = evidence([call("x", ring=1, talk=1500)])
    row = rows[0]
    assert row["result"] == ce.LONG_CALL and row["best_talk_seconds"] == 1500 and row["best_caller"] == "Joe Nicholls"
    assert row["advisor_calls"] == 1 and row["connected_calls"] == 1 and row["first_connected_offset_min"] == pytest.approx(1.2, abs=0.1)
    assert PHONE not in str(row.values()) and "07700" not in str(row.values())       # never a phone number in the sheet


def test_result_categories_by_talk_time():
    assert result_of([call("x", talk=600)]) == ce.LONG_CALL            # exactly the long threshold
    assert result_of([call("x", talk=599)]) == ce.CONVERSATION
    assert result_of([call("x", talk=60)]) == ce.CONVERSATION
    assert result_of([call("x", talk=59)]) == ce.SHORT_CALL
    assert result_of([call("x", talk=None)]) == ce.NOT_ANSWERED
    assert result_of([call("x", cancel_only=True)]) == ce.NOT_ANSWERED
    assert result_of([]) == ce.NO_CALL


def test_the_best_call_wins_when_the_advisor_rang_several_times():
    rows = evidence([call("a", ring=-5, talk=None), call("b", ring=0, talk=20), call("c", ring=2, talk=900)])
    assert rows[0]["result"] == ce.LONG_CALL and rows[0]["advisor_calls"] == 3 and rows[0]["connected_calls"] == 2


def test_the_window_is_three_hours_by_default_inclusive_and_excludes_beyond():
    assert result_of([call("x", ring=180, talk=900)]) == ce.LONG_CALL
    assert result_of([call("x", ring=-180, talk=900)]) == ce.LONG_CALL
    assert result_of([call("x", ring=181, talk=900)]) == ce.NO_CALL
    assert result_of([call("x", ring=-181, talk=900)]) == ce.NO_CALL
    assert result_of([call("x", ring=121, talk=900)], window_minutes=120) == ce.NO_CALL         # the window is configurable
    assert result_of([call("x", ring=121, talk=900)], window_minutes=240) == ce.LONG_CALL


def test_only_calls_to_the_contacts_own_number_count():
    assert result_of([call("x", talk=900, phone="07700900999")]) == ce.NO_CALL
    assert result_of([call("x", talk=900, phone="+44 7700 900123")]) == ce.LONG_CALL            # formats are normalised


def test_a_call_by_someone_else_is_not_the_advisors_call():
    assert result_of([call("x", caller="Hugo Sharpe", talk=900)]) == ce.OTHER_CALLER
    assert result_of([call("x", caller="Hugo Sharpe", talk=20)]) == ce.NO_CALL                 # a short call by someone else proves nothing
    assert result_of([call("x", caller="Hugo Sharpe", talk=None)]) == ce.NO_CALL
    rows = evidence([call("x", caller="Hugo Sharpe", talk=900)])
    assert rows[0]["other_caller_calls"] == 1 and rows[0]["other_caller_best_talk"] == 900 and rows[0]["advisor_calls"] == 0


def test_the_advisor_name_matches_the_way_evonex_spells_it():
    assert result_of([call("x", caller="Joe Michael Nicholls", talk=900)]) == ce.LONG_CALL


def test_no_usable_phone_means_no_judgement():
    assert result_of([call("x", talk=900)], phones={}) == ce.NO_PHONE


def test_a_call_that_is_still_ringing_is_not_counted_but_a_stale_one_is_a_no_answer():
    from app.writeback_rules import classify
    ringing = CallInfo(cid="r", rpi="07700900123", owner_name="Joe Nicholls", ring_ts=at(0))
    assert classify(ringing, 60, at(10)) == (None, 0)                      # 10 minutes: still in progress
    assert classify(ringing, 60, at(16)) == ("No Answer", 0)               # 16 minutes with nothing else: the cancel event was lost
    assert classify(ringing, 60) == (None, 0)                              # the write-back never passes `now`: unchanged
    assert result_of([ringing]) == ce.NOT_ANSWERED                         # judged long after: counts as an unanswered call
    connected_no_end = CallInfo(cid="c", rpi="07700900123", owner_name="Joe Nicholls", ring_ts=at(0), start_ts=at(0))
    assert classify(connected_no_end, 60, LATER) == (None, 0)             # no end event: talk time unknown, never guessed
    assert result_of([connected_no_end]) == ce.NO_CALL


# ---------------------------------------------------------------- when an appointment is judged

def test_nothing_is_written_until_the_window_has_closed_and_settled():
    closes = START + timedelta(minutes=180 + ce.SETTLE_MINUTES)
    assert evidence([call("x", talk=900)], now=closes - timedelta(seconds=1)) == []
    assert len(evidence([call("x", talk=900)], now=closes)) == 1


def test_cancelled_deleted_and_timeless_appointments_are_skipped():
    for kw in ({"status": "cancelled"}, {"status": "invalid"}, {"deleted": "TRUE"}, {"start_time": ""}):
        assert evidence([call("x", talk=900)], appts=[appt(**kw)]) == []


def test_a_row_is_frozen_once_the_window_closed_long_ago_but_recomputed_before_that():
    calls = [call("x", talk=900)]
    just_closed = START + timedelta(minutes=180) + timedelta(hours=ce.FREEZE_HOURS - 1)
    assert len(evidence(calls, existing={"a1": {}}, now=just_closed)) == 1
    assert evidence(calls, existing={"a1": {}}, now=START + timedelta(minutes=180, hours=ce.FREEZE_HOURS + 1)) == []
    assert len(evidence(calls, existing={}, now=START + timedelta(days=30))) == 1             # never stored: still judged


def test_an_appointment_older_than_the_event_history_is_skipped_not_called_no_call():
    assert evidence([], coverage_start=START - timedelta(minutes=179)) == []
    assert len(evidence([], coverage_start=START - timedelta(minutes=180))) == 1
    assert len(evidence([], coverage_start=None)) == 1


# ---------------------------------------------------------------- flags

def flags_for(status, calls, now=LATER, **kw):
    a = appt(status=status)
    return ce.evidence_flags(evidence(calls, appts=[a], now=now, **kw), [a], now)


def test_showed_without_a_real_call_is_flagged_but_showed_with_one_is_not():
    for calls in ([], [call("x", talk=None)], [call("x", talk=30)]):
        assert types(flags_for("showed", calls)) == ["showed_without_call"]
    assert flags_for("showed", [call("x", talk=900)]) == []
    assert flags_for("showed", [call("x", talk=65)]) == []
    assert flags_for("showed", [call("x", caller="Hugo Sharpe", talk=900)]) == []             # someone else called: not our call to make
    assert flags_for("showed", [], phones={}) == []                                          # no phone: cannot judge


def test_noshow_with_a_real_conversation_is_flagged():
    assert types(flags_for("noshow", [call("x", talk=900)])) == ["noshow_with_call"]
    assert types(flags_for("noshow", [call("x", talk=61)])) == ["noshow_with_call"]
    assert flags_for("noshow", [call("x", talk=30)]) == [] and flags_for("noshow", []) == []


def test_a_call_that_happened_but_was_never_marked_is_flagged_after_a_day():
    for status in ("new", "confirmed", ""):
        assert types(flags_for(status, [call("x", talk=900)])) == ["unmarked_with_call"]
    soon = START + timedelta(minutes=200)                                                    # the window has closed and settled, a day has not passed
    assert flags_for("confirmed", [call("x", talk=900)], now=soon) == []                        # staff still have time
    assert flags_for("confirmed", [call("x", talk=20)]) == [] and flags_for("confirmed", []) == []
    assert flags_for("cancelled", [call("x", talk=900)]) == []


def test_each_flag_is_raised_once_per_appointment_whatever_the_evidence_does_later():
    first = flags_for("showed", [])[0]
    again = flags_for("showed", [call("x", talk=30)])[0]                                     # a different bad result, same appointment
    assert first["flag_id"] == again["flag_id"]
    other = ce.evidence_flags(evidence([], appts=[appt(appointment_id="a2")]), [appt(appointment_id="a2")], LATER)
    assert other[0]["flag_id"] != first["flag_id"]
    f = first
    assert (f["type"], f["contact_id"], f["appointment_id"], f["status"], f["field"]) == ("showed_without_call", "c1", "a1", "open", "appointment_status")
    assert set(f) == set(fa.FLAG_COLUMNS)


# ---------------------------------------------------------------- daily call activity

def test_activity_counts_calls_per_london_day_and_caller():
    calls = [call("a", ring=0, talk=900), call("b", ring=1, talk=30), call("c", ring=2, talk=None), call("d", caller="Hugo Sharpe", ring=3, talk=70),
             call("e", ring=60 * 24, talk=65)]
    rows = {(r["day"], r["caller"]): r for r in ce.build_call_activity({c.cid: c for c in calls})}
    joe = rows[("2026-10-21", "Joe Nicholls")]
    assert (joe["calls"], joe["connected"], joe["conversations"], joe["long_calls"], joe["no_answer"], joe["talk_seconds"]) == (3, 2, 1, 1, 1, 930)
    assert rows[("2026-10-21", "Hugo Sharpe")]["conversations"] == 1
    assert rows[("2026-10-22", "Joe Nicholls")]["calls"] == 1


def test_activity_uses_the_london_date_and_skips_a_day_that_may_be_incomplete():
    late = CallInfo(cid="l", rpi="1", owner_name="Joe Nicholls", ring_ts=datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc))      # 00:30 on the 22nd in London (BST)
    late.end_ts = late.ring_ts + timedelta(seconds=10)
    assert ce.build_call_activity({"l": late})[0]["day"] == "2026-10-22"
    cover = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)       # events start midday on the 21st: the 21st is partial
    assert ce.build_call_activity({"c": call("c", talk=900)}, cover) == []
    assert len(ce.build_call_activity({"l": late}, cover)) == 1


def test_activity_thresholds_are_inclusive():
    rows = ce.build_call_activity({"a": call("a", talk=60), "b": call("b", caller="Hugo Sharpe", talk=600), "c": call("c", caller="Ava Morris-Boundy", talk=59)})
    by = {r["caller"]: r for r in rows}
    assert (by["Joe Nicholls"]["conversations"], by["Joe Nicholls"]["long_calls"]) == (1, 0)
    assert (by["Hugo Sharpe"]["conversations"], by["Hugo Sharpe"]["long_calls"]) == (1, 1)
    assert (by["Ava Morris-Boundy"]["conversations"], by["Ava Morris-Boundy"]["connected"]) == (0, 1)


def test_unfinished_and_nameless_calls():
    ringing = CallInfo(cid="r", rpi="1", owner_name="Joe Nicholls", ring_ts=at(0))
    nameless = call("n", caller="", talk=100)
    rows = ce.build_call_activity({"r": ringing, "n": nameless})
    assert [(r["caller"], r["calls"]) for r in rows] == [("(unknown)", 1)]


# ---------------------------------------------------------------- booker is not the owner (optional flag)

OWNERS = {"u-anita": "Anita Andrews", "u-hugo": "Hugo Sharpe"}


def owned(owner_user_id, **kw):
    return {**snap(**kw), "owner_user_id": owner_user_id}


def test_a_booking_by_someone_other_than_the_owner_is_flagged_once():
    appts = events_to_rows([event(added="2026-10-22T09:00:00Z")], USERS)
    _, flags, _ = judge([owned("u-anita")], {}, appts, owner_names=OWNERS)
    assert types(flags) == ["booker_not_owner"]
    f = flags[0]
    assert (f["old_value"], f["new_value"], f["appointment_id"]) == ("Anita Andrews", "Hugo Sharpe", "e1")
    _, again, _ = judge([owned("u-anita")], {}, appts, owner_names={"u-anita": "Someone Else Entirely"})
    assert again[0]["flag_id"] == flags[0]["flag_id"]                                          # a changed owner name does not re-flag


def test_the_owner_check_is_silent_when_it_should_be():
    fresh = events_to_rows([event(added="2026-10-22T09:00:00Z")], USERS)
    assert types(judge([owned("u-hugo")], {}, fresh, owner_names=OWNERS)[1]) == []            # the booker owns the lead
    assert types(judge([owned("u-anita")], {}, fresh)[1]) == []                                # switched off (no owner names)
    assert types(judge([owned("")], {}, fresh, owner_names=OWNERS)[1]) == []                   # lead has no owner
    assert types(judge([owned("u-unknown")], {}, fresh, owner_names=OWNERS)[1]) == []          # owner is not a known user
    old = events_to_rows([event(added="2026-10-19T09:00:00Z")], USERS)
    assert types(judge([owned("u-anita")], {}, old, owner_names=OWNERS)[1]) == []              # owners change; only the first 48 hours count


def test_the_owner_check_judges_the_booked_by_field_when_it_is_filled():
    appts = events_to_rows([event(added="2026-10-22T09:00:00Z")], USERS)
    # the field names Anita (the owner) although the calendar says Hugo: the field is the official booker, so no owner flag
    flags = judge([owned("u-anita", booked="Anita Andrews")], {}, appts, owner_names=OWNERS)[1]
    assert "booker_not_owner" not in types(flags) and "booked_by_mismatch" in types(flags)


# ---------------------------------------------------------------- the runner step

def evonex_rows(*calls_spec):
    """Raw Evonex_Events rows for outbound calls: (cid, caller, ring minutes after START, talk seconds or None, phone)."""
    rows = []
    for cid, caller, ring, talk, phone in calls_spec:
        base = {"event": "OUTBOUNDCALL", "cid": cid, "rpi": phone, "on": caller}
        t0 = at(ring)
        rows.append({**base, "subevent": "RINGCALL", "ts": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "dedup_key": cid + "r", "received_at_utc": t0.isoformat()})
        if talk is not None:
            t1 = t0 + timedelta(seconds=5)
            t2 = t1 + timedelta(seconds=talk)
            rows.append({**base, "subevent": "STARTCALL", "ts": t1.strftime("%Y-%m-%dT%H:%M:%SZ"), "dedup_key": cid + "s", "received_at_utc": t1.isoformat()})
            rows.append({**base, "subevent": "ENDCALL", "ts": t2.strftime("%Y-%m-%dT%H:%M:%SZ"), "dedup_key": cid + "e", "received_at_utc": t2.isoformat()})
        else:
            t1 = t0 + timedelta(seconds=20)
            rows.append({**base, "subevent": "CANCELCALL", "ts": t1.strftime("%Y-%m-%dT%H:%M:%SZ"), "dedup_key": cid + "c", "received_at_utc": t1.isoformat()})
    return rows


def contact_with_phone(cid, phone=PHONE):
    return {**contact(cid), "phone": phone}


def run_with_evidence(ghl, stores, rows, now=LATER, **over):
    asked = []

    def reader(window):
        asked.append(window)
        return rows

    out = asyncio.run(runner.run_cycle(ghl, stores, cfg(CALL_EVIDENCE="on", **over), now=now, events_reader=reader))
    return out, asked


EARLIER = evonex_rows(("old", "Joe Nicholls", -60 * 24 * 3, 30, "07700900999"))      # an unrelated call three days before: the history reaches back that far


def finished_appointment():
    return event(added=(START - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), start="2026-10-21T15:00:00Z", status="showed")


def test_the_runner_writes_evidence_activity_and_flags_without_any_phone_number():
    ghl = FakeGHL([finished_appointment()], {"c1": contact_with_phone("c1")})
    stores = AuditStores(sheet=FakeSheet())
    out, asked = run_with_evidence(ghl, stores, EARLIER + evonex_rows(("k1", "Joe Nicholls", 1, 1500, "07700900123")))
    assert asked == [runner.EVENTS_BACKFILL] and out["call_evidence"]["backfill"] and out["call_evidence"]["evidence"] == 1
    row = stores.evidence.read()[0]
    assert (row["appointment_id"], row["result"], row["best_talk_seconds"]) == ("e1", "long_call", "1500")
    assert {r["caller"] for r in stores.activity.read()} == {"Joe Nicholls"}
    assert sorted(r["day"] for r in stores.activity.read()) == ["2026-10-18", "2026-10-21"]       # the whole history is counted, including its first day
    assert "showed_without_call" not in [f["type"] for f in stores.flags.read()]
    dump = str(stores.sheet.tabs["Call_Evidence"].values) + str(stores.sheet.tabs["Call_Activity"].values) + str(stores.sheet.tabs["Flags"].values)
    assert "7700" not in dump


def test_a_showed_appointment_with_no_call_raises_the_flag_once_across_cycles():
    ghl = FakeGHL([finished_appointment()], {"c1": contact_with_phone("c1")})
    stores = AuditStores(sheet=FakeSheet())
    run_with_evidence(ghl, stores, EARLIER)
    assert [f["type"] for f in stores.flags.read()] == ["showed_without_call"]
    out, asked = run_with_evidence(ghl, stores, EARLIER, now=LATER + timedelta(minutes=15))
    assert asked == [runner.EVENTS_NORMAL] and not out["call_evidence"]["backfill"]
    assert len(stores.flags.read()) == 1 and len(stores.evidence.read()) == 1


def test_a_full_event_read_means_older_appointments_are_not_judged():
    ghl = FakeGHL([finished_appointment()], {"c1": contact_with_phone("c1")})
    stores = AuditStores(sheet=FakeSheet())
    many = evonex_rows(("k", "Joe Nicholls", 600, 100, "07700900999")) * 4000                 # 12 000 rows that all start 10 hours after the appointment
    out, _ = run_with_evidence(ghl, stores, many)
    assert out["call_evidence"]["evidence"] == 0 and stores.evidence.read() == [] and stores.flags.read() == []


def test_without_a_reader_or_with_the_switch_off_nothing_happens_and_a_failure_never_stops_the_audit():
    ghl = FakeGHL([finished_appointment()], {"c1": contact_with_phone("c1")})
    stores = AuditStores(sheet=FakeSheet())
    assert "call_evidence" not in asyncio.run(runner.run_cycle(ghl, stores, cfg(CALL_EVIDENCE="on"), now=LATER))
    assert "call_evidence" not in asyncio.run(runner.run_cycle(ghl, stores, cfg(CALL_EVIDENCE="off"), now=LATER, events_reader=lambda n: []))

    def boom(window):
        raise RuntimeError("sheet down")

    out = asyncio.run(runner.run_cycle(FakeGHL([finished_appointment()], {"c1": contact_with_phone("c1")}), AuditStores(sheet=FakeSheet()),
                                       cfg(CALL_EVIDENCE="on"), now=LATER, events_reader=boom))
    assert out["call_evidence"] == {"error": "RuntimeError"} and out["appointments"] == 1


def test_the_runner_only_fetches_phones_for_appointments_that_are_ready():
    future = event(eid="e2", contact="c2", start="2026-10-25T15:00:00Z")
    ghl = FakeGHL([finished_appointment(), future], {"c1": contact_with_phone("c1"), "c2": contact_with_phone("c2")})
    stores = AuditStores(sheet=FakeSheet())
    run_with_evidence(ghl, stores, EARLIER)
    assert [r["appointment_id"] for r in stores.evidence.read()] == ["e1"]                    # the future appointment is not judged


def test_appointments_from_before_the_evonex_history_began_are_not_judged():
    ghl = FakeGHL([finished_appointment()], {"c1": contact_with_phone("c1")})
    stores = AuditStores(sheet=FakeSheet())
    recent_only = evonex_rows(("k", "Joe Nicholls", 60 * 24, 30, "07700900999"))                 # the first event we hold is a day AFTER the appointment
    out, _ = run_with_evidence(ghl, stores, recent_only)
    assert out["call_evidence"]["evidence"] == 0 and stores.flags.read() == []
    out, _ = run_with_evidence(ghl, AuditStores(sheet=FakeSheet()), [])                          # no history at all: nothing can be judged
    assert out["call_evidence"]["evidence"] == 0


# ---------------------------------------------------------------- 23 Oct fixes

def test_the_first_day_of_history_is_counted_unless_the_read_was_cut_short():
    first_day = {c.cid: c for c in [call("a", ring=-60 * 6, talk=900), call("b", ring=-60 * 5, talk=30)]}       # 09:00 and 10:00 on the 21st
    cover = at(-60 * 6)                                                  # the first event we hold is 09:00 that day
    assert ce.build_call_activity(first_day, None)[0]["calls"] == 2       # the read reached the start of the history: the day is complete
    assert ce.build_call_activity(first_day, cover) == []                 # the read hit its limit: that day may be partial


def test_a_connected_call_that_never_ended_is_counted_as_connected_with_no_talk_time_once_it_is_clearly_lost():
    lost = CallInfo(cid="l", rpi="07700900123", owner_name="Joe Nicholls", ring_ts=at(0), start_ts=at(0))
    assert ce.build_call_activity({"l": lost}, None, at(60)) == []                        # an hour: it may still be running
    row = ce.build_call_activity({"l": lost}, None, at(60 * 5))[0]
    assert (row["calls"], row["connected"], row["conversations"], row["talk_seconds"], row["no_answer"]) == (1, 1, 0, 0, 0)
    rang = CallInfo(cid="r", rpi="07700900123", owner_name="Joe Nicholls", ring_ts=at(0))
    assert ce.build_call_activity({"r": rang}, None, at(10)) == []
    assert ce.build_call_activity({"r": rang}, None, at(20))[0]["no_answer"] == 1


def test_the_flag_says_to_review_and_mentions_the_advisors_calls_to_other_numbers():
    other = call("o", ring=30, talk=700, phone="07700900999")              # the advisor had a real call to a DIFFERENT number
    short = call("s", ring=1, talk=20)
    rows = evidence([short, other], all_calls=[short, other])
    r = rows[0]
    assert r["result"] == ce.SHORT_CALL and r["advisor_other_number_calls"] == 1 and r["advisor_other_number_best_talk"] == 700
    flag = ce.evidence_flags(rows, [appt(status="showed")], LATER)[0]
    assert flag["detail"].startswith("To review") and "1 other connected call" in flag["detail"] and "11 min" in flag["detail"]
    quiet = ce.evidence_flags(evidence([short], all_calls=[short]), [appt(status="showed")], LATER)[0]
    assert "other connected" not in quiet["detail"] and quiet["detail"].startswith("To review")


def test_calls_to_other_numbers_do_not_count_when_short_late_or_by_someone_else():
    calls = [call("a", ring=5, talk=30, phone="07700900999"),                       # under a minute
             call("b", ring=200, talk=900, phone="07700900999"),                    # outside the window
             call("c", caller="Hugo Sharpe", ring=5, talk=900, phone="07700900999"),  # not the advisor
             call("d", ring=5, talk=900, phone="07700900123")]                      # the client's own number: not "other"
    assert evidence(calls, all_calls=calls)[0]["advisor_other_number_calls"] == 0


def test_the_client_number_is_the_one_that_parses_whatever_order_the_events_arrive_in():
    from app.writeback_rules import collapse_events, pick_number
    def row(sub, rpi, n):
        return {"event": "OUTBOUNDCALL", "cid": "k", "subevent": sub, "ts": f"2026-10-21T15:0{n}:00Z", "dedup_key": f"k{n}", "rpi": rpi, "on": "Joe Nicholls"}
    rows = [row("RINGCALL", "07700900123", 0), row("CANCELCALL", "9110000000000000", 1), row("STARTCALL", "07700900123", 2), row("ENDCALL", "07700900123", 3)]
    assert collapse_events(rows)["k"].rpi == "07700900123" == collapse_events(list(reversed(rows)))["k"].rpi
    assert collapse_events([row("CANCELCALL", "9110000000000000", 1)])["k"].rpi == "9110000000000000"       # only junk: kept so the call is still visible
    assert pick_number({}) == "" and pick_number({"07700900123": 1, "07700900124": 1}) == "07700900123"      # ties: smallest, never arrival order
    assert pick_number({"9110000000000000": 3, "07700900123": 1}) == "07700900123"                           # a number that parses beats a more frequent junk one
    assert pick_number({"07700900124": 1, "07700900123": 3}) == "07700900123"                                # then the most frequent


def test_the_runner_stores_client_names_and_keeps_them_between_cycles():
    ghl = FakeGHL([finished_appointment()], {"c1": {**contact_with_phone("c1"), "contactName": "Pat Example"}})
    stores = AuditStores(sheet=FakeSheet())
    run_with_evidence(ghl, stores, EARLIER)
    assert stores.appointments.read()[0]["contact_name"] == "Pat Example"
    ghl.contacts["c1"]["contactName"] = "Renamed Later"                                       # an already stored name is never re-fetched or changed
    run_with_evidence(ghl, stores, EARLIER, now=LATER + timedelta(minutes=15))
    assert stores.appointments.read()[0]["contact_name"] == "Pat Example"


def test_names_come_from_first_and_last_name_when_there_is_no_display_name_and_stay_blank_when_unknown():
    from app.appointments import contact_display_name
    assert contact_display_name({"firstName": "Pat", "lastName": "Example"}) == "Pat Example"
    assert contact_display_name({"firstName": " Pat "}) == "Pat" and contact_display_name({"contactName": "A B", "firstName": "x"}) == "A B"
    assert contact_display_name({}) == ""


def test_an_existing_shorter_header_row_is_extended_when_columns_were_appended():
    sheet = FakeSheet()
    from app.audit_store import TabStore
    store = TabStore(sheet, "T", ["a", "b", "c"], ["a"])
    ws = sheet.add_worksheet("T", 10, 3)
    ws.update(values=[["a", "b"]], range_name="A1")
    store._ws()
    assert sheet.tabs["T"].values[0] == ["a", "b", "c"]
    other = TabStore(sheet, "T2", ["x", "y", "w"], ["x"])
    ws2 = sheet.add_worksheet("T2", 10, 3)
    ws2.update(values=[["z", "y"]], range_name="A1")                                          # not a prefix: left strictly alone
    other._ws()
    assert sheet.tabs["T2"].values[0] == ["z", "y"]


def test_client_name_lookups_are_bounded_per_cycle_and_finish_on_the_next(monkeypatch):
    monkeypatch.setattr(runner, "NAME_FETCH_PER_CYCLE", 1)
    events = [finished_appointment(), event(eid="e2", contact="c2", added="2026-10-20T09:00:00Z", start="2026-10-21T16:00:00Z", status="showed")]
    ghl = FakeGHL(events, {"c1": {**contact("c1"), "contactName": "Pat One"}, "c2": {**contact("c2"), "contactName": "Sam Two"}})
    stores = AuditStores(sheet=FakeSheet())
    names = lambda: sorted((r["appointment_id"], r["contact_name"]) for r in stores.appointments.read())
    asyncio.run(runner._fill_contact_names(ghl, stores, ap_rows(events), {}))
    rows = ap_rows(events)
    asyncio.run(runner._fill_contact_names(ghl, stores, rows, {}))
    assert sorted(r["contact_name"] for r in rows).count("") == 1                              # one looked up, one still to do
    stores.appointments.upsert(rows)
    rows2 = ap_rows(events)
    asyncio.run(runner._fill_contact_names(ghl, stores, rows2, {}))
    assert sorted(r["contact_name"] for r in rows2) == ["Pat One", "Sam Two"]
    assert names() == [("e1", "Pat One"), ("e2", "")]


def ap_rows(events):
    return events_to_rows(events, USERS)


# ---------------------------------------------------------------- changed rules re-judge frozen rows and heal open flags

def test_changed_rules_rejudge_frozen_evidence_and_resolve_flags_that_no_longer_apply():
    ghl = FakeGHL([finished_appointment()], {"c1": contact_with_phone("c1")})
    stores = AuditStores(sheet=FakeSheet())
    later = START + timedelta(days=10)                                       # long after the window: the row is frozen
    run_with_evidence(ghl, stores, EARLIER, now=later)
    assert [r["result"] for r in stores.evidence.read()] == ["no_call"]
    assert [(f["type"], f["status"]) for f in stores.flags.read()] == [("showed_without_call", "open")]
    assert stores.get_state(runner.STATE_EVIDENCE_RULES) == ce.RULES_VERSION

    with_call = EARLIER + evonex_rows(("k1", "Joe Nicholls", 5, 900, "07700900123"))
    out, asked = run_with_evidence(ghl, stores, with_call, now=later + timedelta(minutes=15))
    assert not out["call_evidence"]["rules_changed"] and asked == [runner.EVENTS_NORMAL]
    assert [r["result"] for r in stores.evidence.read()] == ["no_call"]       # same rules: the frozen row is left alone

    stores.set_state(runner.STATE_EVIDENCE_RULES, "an older version")          # the rules changed
    out, asked = run_with_evidence(ghl, stores, with_call, now=later + timedelta(minutes=30))
    assert out["call_evidence"]["rules_changed"] and asked == [runner.EVENTS_BACKFILL] and out["call_evidence"]["flags_refreshed"] == 1
    assert [r["result"] for r in stores.evidence.read()] == ["long_call"]
    flag = stores.flags.read()[0]
    assert flag["status"] == "resolved" and flag["detail"].startswith("Resolved") and "To review" in flag["detail"]
    assert len(stores.flags.read()) == 1                                       # healed in place, nothing duplicated
    assert stores.get_state(runner.STATE_EVIDENCE_RULES) == ce.RULES_VERSION
    out, _ = run_with_evidence(ghl, stores, with_call, now=later + timedelta(minutes=45))
    assert not out["call_evidence"]["rules_changed"] and out["call_evidence"]["flags_refreshed"] == 0


def test_an_open_flag_gets_its_text_refreshed_when_the_wording_changed_and_other_flags_are_left_alone():
    ghl = FakeGHL([finished_appointment()], {"c1": contact_with_phone("c1")})
    stores = AuditStores(sheet=FakeSheet())
    later = START + timedelta(days=10)
    run_with_evidence(ghl, stores, EARLIER, now=later)
    row = stores.flags.read()[0]
    stores.flags.upsert([{**row, "detail": "marked Showed, no call"}])                        # an old wording
    other = {**row, "flag_id": "zzzz", "type": "booked_by_missing", "appointment_id": row["appointment_id"], "detail": "keep me"}
    closed = {**row, "flag_id": "c1c1", "status": "resolved", "detail": "old closed text"}
    unjudged = {**row, "flag_id": "u1u1", "appointment_id": "not-judged-this-cycle", "detail": "old unjudged text"}
    stores.flags.append([other, closed, unjudged])
    stores.set_state(runner.STATE_EVIDENCE_RULES, "old")
    run_with_evidence(ghl, stores, EARLIER, now=later + timedelta(minutes=15))
    flags = {f["flag_id"]: f for f in stores.flags.read()}
    assert flags[row["flag_id"]]["detail"].startswith("To review") and flags[row["flag_id"]]["status"] == "open"
    assert flags["zzzz"]["detail"] == "keep me" and flags["zzzz"]["status"] == "open"        # only call flags are managed here
    assert (flags["c1c1"]["status"], flags["c1c1"]["detail"]) == ("resolved", "old closed text")         # closed flags are never reopened or rewritten
    assert (flags["u1u1"]["status"], flags["u1u1"]["detail"]) == ("open", "old unjudged text")           # nor those of appointments not judged this cycle
