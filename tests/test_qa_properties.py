"""QA: property-based tests (hypothesis). The pure functions receive data typed by staff, produced by Evonex and
returned by GHL and Google Sheets, so they must never raise on odd input and must keep their invariants."""

import re
from datetime import datetime, timedelta, timezone

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import field_audit as fa
from app import sales_metrics as sm
from app.appointments import event_to_row, events_to_rows, to_utc_text
from app.writeback_rules import (
    WRITE_FIELD_IDS, F_ADMIN_COUNT, F_ADVISOR_COUNT, F_ADVISOR_EMAIL, F_ADVISOR_NAME, F_CALL_PHASE,
    as_int, classify, collapse_events, decide, field_value_matches, is_terminal, names_match, parse_ts, search_candidates, to_e164,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

text = st.text(max_size=40)
anything = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=True, allow_infinity=True), text, st.lists(text, max_size=3))
timestamps = st.one_of(
    st.datetimes(min_value=datetime(2024, 1, 1), max_value=datetime(2028, 1, 1)).map(lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")),
    text,
)


# ------------------------------------------------------------------------------------------ phone numbers

@FAST
@given(anything)
def test_to_e164_never_raises_and_only_returns_plausible_numbers(raw):
    out = to_e164(raw)
    assert out is None or re.fullmatch(r"\+[0-9]{8,15}", out)


@FAST
@given(st.from_regex(r"0[1-9][0-9]{9}", fullmatch=True))
def test_uk_national_numbers_round_trip_to_e164_and_back(national):
    e164 = to_e164(national)
    assert e164 == "+44" + national[1:]
    assert search_candidates(e164)[:2] == [e164, national]


# ------------------------------------------------------------------------------------------ names and parsing

@FAST
@given(text, text)
def test_names_match_is_symmetric_and_never_raises(a, b):
    assert names_match(a, b) == names_match(b, a)


@FAST
@given(text)
def test_a_name_matches_itself_when_it_has_two_words(name):
    words = re.sub(r"[^a-zA-Z\s'-]", " ", name).split()
    if len(words) >= 2:
        assert names_match(name, name)


@FAST
@given(anything)
def test_parse_ts_and_utc_text_never_raise(value):
    parse_ts(value)
    assert to_utc_text(value) == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", to_utc_text(value))


@FAST
@given(anything)
def test_as_int_always_returns_a_non_negative_int(value):
    assert as_int(value) >= 0


@FAST
@given(anything, anything)
def test_field_value_matches_never_raises(expected, actual):
    field_value_matches(expected, actual)


# ------------------------------------------------------------------------------------------ Evonex events

def _with_content_key(row):
    """Evonex rows carry a dedup_key that is a hash of their content (see evonex.make_dedup_key), so two rows
    with the same key are the same event; a blank key falls back to the content as well."""
    if row["dedup_key"] == "blank":
        return {**row, "dedup_key": ""}
    return {**row, "dedup_key": "|".join([row["cid"], row["event"], row["subevent"], row["ts"]])}


event_row = st.fixed_dictionaries(
    {"event": st.sampled_from(["OUTBOUNDCALL", "INBOUNDCALL", "RECORDINGREADY", "", "X"]),
     "subevent": st.sampled_from(["RINGCALL", "STARTCALL", "ENDCALL", "CANCELCALL", "", "ringcall", "WHATEVER"]),
     "ts": timestamps, "cid": st.sampled_from(["a", "b", "c", ""]), "rpi": text, "on": text, "oe": text,
     "dedup_key": st.sampled_from(["content", "blank"]), "received_at_utc": timestamps},
).map(_with_content_key)


@FAST
@given(st.lists(event_row, max_size=25), st.integers(0, 600))
def test_collapse_classify_and_terminal_never_raise_and_stay_consistent(rows, threshold):
    calls = collapse_events(rows)
    assert all(c.cid for c in calls.values())
    for info in calls.values():
        outcome, talk = classify(info, threshold)
        assert outcome in (None, "No Answer", "Voicemail", "Answered") and talk >= 0
        if outcome in ("Voicemail", "Answered"):
            assert info.start_ts is not None and info.end_ts is not None
            assert (outcome == "Answered") == (talk >= threshold)
        is_terminal(info, NOW, 5)


@FAST
@given(st.lists(event_row, max_size=25))
def test_collapse_is_independent_of_row_order(rows):
    a = collapse_events(rows)
    b = collapse_events(list(reversed(rows)))
    assert a.keys() == b.keys()
    for cid in a:
        x, y = a[cid], b[cid]
        assert (x.ring_ts, x.start_ts, x.end_ts, x.cancel_ts) == (y.ring_ts, y.start_ts, y.end_ts, y.cancel_ts)


@FAST
@given(
    st.lists(event_row, min_size=1, max_size=12),
    st.dictionaries(st.sampled_from([F_CALL_PHASE, F_ADMIN_COUNT, F_ADVISOR_COUNT, F_ADVISOR_NAME, F_ADVISOR_EMAIL, "other"]), anything, max_size=6),
    st.booleans(),
)
def test_decide_only_ever_writes_allowed_fields_and_always_counts_up_by_one(rows, values, strict):
    for info in collapse_events(rows).values():
        d = decide(info, "No Answer", values, ["Joe Nicholls"], strict)
        if d.write:
            assert {f["id"] for f in d.fields} <= WRITE_FIELD_IDS and len(d.fields) == 4
            assert d.new_count == d.count_before + 1 and d.pair in ("admin", "advisor")
            assert d.status in ("Answered", "No Answer", "Voicemail")
        else:
            assert d.fields == [] and d.reason


# ------------------------------------------------------------------------------------------ appointments, audit, KPIs

USERS = {"u1": "Hugo Sharpe", "u2": "Ava Morris-Boundy", "u3": "Joe Nicholls", "u4": "Xavier Collin"}
ids = st.sampled_from(["u1", "u2", "u3", "u4", "", None, "ghost"])
event = st.fixed_dictionaries(
    {"id": st.sampled_from(["e1", "e2", "e3", "e4", "e5", "e6", ""]), "contactId": st.sampled_from(["c1", "c2", "c3", None]),
     "calendarId": st.just("cal"),
     "createdBy": st.one_of(st.none(), text, st.fixed_dictionaries({"source": st.sampled_from(["contactdetails_page", "booking_widget", None]), "userId": ids})),
     "assignedUserId": ids, "dateAdded": timestamps, "startTime": timestamps, "dateUpdated": timestamps,
     "appointmentStatus": st.sampled_from(["new", "confirmed", "showed", "noshow", "cancelled", "invalid", "", None, "NoShow"]),
     "deleted": st.booleans()},
)


@FAST
@given(st.lists(event, max_size=20))
def test_events_normalise_without_raising_and_ids_are_unique(events):
    rows = events_to_rows(events, USERS)
    assert len({r["appointment_id"] for r in rows}) == len(rows)
    assert all(r["appointment_id"] for r in rows)
    for e in events:
        event_to_row(e, USERS)


contact_values = st.fixed_dictionaries({"contact_id": st.sampled_from(["c1", "c2", "c3"]), "created_by": text, "booked_by": text, "advisor_name": text})


@FAST
@given(st.lists(event, max_size=15), st.lists(contact_values, max_size=6))
def test_the_audit_never_raises_and_every_flag_is_well_formed(events, snaps):
    appts = events_to_rows(events, USERS)
    locks, flags, official = fa.audit_appointments(snaps, {}, appts, ["Joe Nicholls"], NOW, NOW - timedelta(days=30))
    created_locks, created_flags = fa.audit_created_by(snaps, {}, NOW)
    for f in flags + created_flags:
        assert set(f) == set(fa.FLAG_COLUMNS) and f["status"] == "open" and f["flag_id"] and f["type"]
    assert all(set(r) == set(fa.LOCK_COLUMNS) for r in locks + created_locks)
    assert set(official) <= {a["appointment_id"] for a in appts}
    fa.source_flags(appts, ("contactdetails_page",), NOW)


@FAST
@given(st.lists(event, max_size=25), st.lists(st.sampled_from(["c1", "c2", "c3"]), max_size=3, unique=True))
def test_kpi_invariants_hold_for_any_appointment_data(events, completed):
    appts = events_to_rows(events, USERS)
    for a in appts:
        a["booked_by_official"] = a["booked_by"]
    contacts = {"c1": {"fact_find_outcome": "Full Fact Find Took Place - YES", "completed": "c1" in completed, "completed_at": ""},
                "c2": {"fact_find_outcome": "Advice Given - YES", "completed": "c2" in completed, "completed_at": ""}}
    for group in ("booked_by", "advisor"):
        table = sm.build(appts, contacts, NOW, group_by=group)
        counted = [a for a in appts if not a["deleted"] and parse_ts(a["booked_at"])]
        assert sum(r["booked"] for r in table) == len(counted)                 # every appointment lands in exactly one row
        for r in table:
            assert 0 <= r["due"] <= r["booked"] and r["upcoming"] + r["due"] + r["cancelled"] <= r["booked"] + r["cancelled"]
            assert r["upcoming"] + r["due"] <= r["booked"]                       # upcoming and due never overlap
            assert r["full_fact_finds"] <= r["due"] and r["completed"] <= r["due"]
            for key in ("conversion", "conversion_to_completion", "show_rate"):
                assert r[key] is None or 0 <= r[key] <= 1
            assert (r["conversion"] is None) == (r["due"] == 0)
    sm.advisor_distribution(appts, ["Joe Nicholls", "Xavier Collin"])
