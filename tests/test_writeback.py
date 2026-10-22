"""Tests for the Evonex -> GHL call write-back: pure rules, GHL client, and the
orchestrator (idempotency, locking, settle, failure handling) with fakes."""

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app import writeback as wb
from app import writeback_rules as rules
from app.writeback_ghl import GHLError, GHLWriteback, pick_contact
from app.writeback_rules import (
    F_ADMIN_COUNT, F_ADMIN_STATUS, F_ADVISOR_COUNT, F_ADVISOR_EMAIL, F_ADVISOR_NAME,
    F_ADVISOR_STATUS, F_CALL_PHASE, F_LAST_CALL_AT, F_LAST_CALL_BY,
)

T0 = datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ fixtures

class Clock:
    def __init__(self, start=T0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


_ids = itertools.count(1)


def ev(cid, sub, at, rpi="07700900123", on="Hugo Sharpe", oe="hsharpe@assetharbour.com", received=None, event="OUTBOUNDCALL"):
    n = next(_ids)
    return {
        "event": event, "subevent": sub, "ts": iso(at), "cid": cid, "rpi": rpi, "on": on, "oe": oe,
        "dedup_key": f"k{n}", "received_at_utc": (received or at).isoformat(),
    }


def call_events(cid, ring=0, start=None, end=None, cancel=None, base=T0, **kw):
    out = [ev(cid, "RINGCALL", base + timedelta(seconds=ring), **kw)]
    if cancel is not None:
        out.append(ev(cid, "CANCELCALL", base + timedelta(seconds=cancel), **kw))
    if start is not None:
        out.append(ev(cid, "STARTCALL", base + timedelta(seconds=start), **kw))
    if end is not None:
        out.append(ev(cid, "ENDCALL", base + timedelta(seconds=end), **kw))
    return out


class FakeStore:
    def __init__(self, clock, events=None):
        self.clock, self.events, self.log = clock, list(events or []), []

    def recent_events(self, window=0):
        return list(self.events)

    def recent_log(self, window=0):
        return [dict(r) for r in self.log]

    def append_log(self, **fields):
        row = {"logged_at_utc": self.clock.now().isoformat()}
        row.update({k: str(v) for k, v in fields.items()})
        self.log.append(row)

    def statuses(self, cid):
        return [r["status"] for r in self.log if r.get("cid") == cid]


class FakeGHL:
    """Stands in for GHL: holds contacts and applies PUTs like the real API."""

    def __init__(self, contacts=None, values=None):
        self.contacts = contacts if contacts is not None else [{"id": "C1", "dateUpdated": "2026-10-01"}]
        self.values = values if values is not None else {"C1": {F_CALL_PHASE: "Admin Call", F_ADMIN_COUNT: 0}}
        self.puts, self.open_opps = [], set()
        self.fail_search = self.fail_put_times = 0
        self.put_applies_when_failing = False
        self.drop_field = None          # simulate a field GHL silently ignores

    def find_contacts(self, e164):
        if self.fail_search:
            self.fail_search -= 1
            raise GHLError("http_500", 500)
        return list(self.contacts)

    def has_open_mortgage_opportunity(self, contact_id):
        return contact_id in self.open_opps

    def get_contact_values(self, contact_id):
        return dict(self.values[contact_id])

    def put_custom_fields(self, contact_id, fields):
        illegal = [f for f in fields if f["id"] not in rules.WRITE_FIELD_IDS]
        assert not illegal
        if self.fail_put_times:
            self.fail_put_times -= 1
            if self.put_applies_when_failing:
                self._apply(contact_id, fields)
            raise GHLError("http_500", 500)
        self.puts.append((contact_id, fields))
        self._apply(contact_id, fields)

    def _apply(self, contact_id, fields):
        for f in fields:
            if f["id"] != self.drop_field:
                self.values[contact_id][f["id"]] = f["field_value"]


def cfg(**over):
    base = dict(
        WRITEBACK_MODE="on", WRITEBACK_TALK_SECONDS=60, WRITEBACK_SETTLE_SECONDS=5.0,
        WRITEBACK_MAX_AGE_MINUTES=120,
        WRITEBACK_ADVISOR_NAMES=["Henny Lessey", "Joe Nicholls", "Xavier Collin", "Cecilia Szabo", "Carl Gladman", "Anthony Fox"],
        WRITEBACK_ADVISOR_STRICT=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def make(events=None, ghl=None, config=None, start=T0 + timedelta(minutes=10)):
    clock = Clock(start)
    store = FakeStore(clock, events)
    ghl = ghl or FakeGHL()
    return wb.Writeback(store, ghl, config or cfg(), now=clock.now, sleep=clock.sleep), store, ghl, clock


# ===================================================================== rules

@pytest.mark.parametrize("raw,expected", [
    ("07700900123", "+447700900123"), ("+44 7700 900123", "+447700900123"), ("00447700900123", "+447700900123"),
    ("447700900123", "+447700900123"), ("02079460000", "+442079460000"), ("03001234567", "+443001234567"),
    ("", None), (None, None), ("123", None), ("9112345678901234", None), ("7700900123", None),
])
def test_to_e164(raw, expected):
    assert rules.to_e164(raw) == expected


def test_search_candidates_start_with_e164_then_national_then_digits():
    assert rules.search_candidates("+447700900123") == ["+447700900123", "07700900123", "447700900123"]


def test_collapse_dedupes_and_orders_by_ts_not_arrival():
    e = call_events("c1", start=11, end=60)
    e.append(dict(e[1]))                                   # exact duplicate delivery
    e[2]["received_at_utc"] = (T0 - timedelta(seconds=5)).isoformat()   # ENDCALL "arrived" first
    info = rules.collapse_events(e)["c1"]
    assert info.event_count == 3 and info.ring_ts == T0
    assert info.start_ts == T0 + timedelta(seconds=11) and info.end_ts == T0 + timedelta(seconds=60)


def test_collapse_ignores_non_outbound_and_blank_cid():
    rows = [ev("c1", None, T0, event="RECORDINGREADY"), ev("", "RINGCALL", T0), ev("c2", "RINGCALL", T0, event="INBOUNDCALL")]
    assert rules.collapse_events(rows) == {}


def test_repeated_start_keeps_first_and_repeated_end_keeps_last():
    e = call_events("c1", start=10, end=40) + [ev("c1", "STARTCALL", T0 + timedelta(seconds=15)), ev("c1", "ENDCALL", T0 + timedelta(seconds=90))]
    info = rules.collapse_events(e)["c1"]
    assert info.start_ts == T0 + timedelta(seconds=10) and info.end_ts == T0 + timedelta(seconds=90)


@pytest.mark.parametrize("kw,expected", [
    (dict(cancel=11), ("No Answer", 0)),
    (dict(end=12), ("No Answer", 0)),                       # ENDCALL, never connected
    (dict(start=11, end=11 + 59), ("Voicemail", 59)),
    (dict(start=11, end=11 + 60), ("Answered", 60)),
    (dict(start=11, end=11 + 300), ("Answered", 300)),
    (dict(start=11), (None, 0)),                            # still in progress
    (dict(), (None, 0)),                                    # only ringing
])
def test_classify(kw, expected):
    info = rules.collapse_events(call_events("c1", **kw))["c1"]
    assert rules.classify(info, 60) == expected


def test_classify_threshold_is_configurable_and_negative_talk_clamped():
    info = rules.collapse_events(call_events("c1", start=10, end=40))["c1"]
    assert rules.classify(info, 30)[0] == "Answered" and rules.classify(info, 31)[0] == "Voicemail"
    skew = rules.collapse_events(call_events("c2", start=50, end=40))["c2"]
    assert rules.classify(skew, 60) == ("Voicemail", 0)


def test_is_terminal_waits_for_settle_and_for_a_start_after_cancel():
    settle = 5
    cancel_only = rules.collapse_events(call_events("c", cancel=11, base=T0))["c"]
    assert not rules.is_terminal(cancel_only, T0 + timedelta(seconds=12), settle)          # too fresh
    assert rules.is_terminal(cancel_only, T0 + timedelta(seconds=17), settle)
    cancel_then_start = rules.collapse_events(call_events("c", cancel=11, start=12))["c"]
    assert not rules.is_terminal(cancel_then_start, T0 + timedelta(hours=1), settle)       # connected, no end yet
    ended = rules.collapse_events(call_events("c", cancel=11, start=12, end=40))["c"]
    assert rules.is_terminal(ended, T0 + timedelta(seconds=50), settle)


@pytest.mark.parametrize("a,b,expected", [
    ("Ava Mae Morris-Boundy", "Ava Morris-Boundy", True), ("Joe Nicholls", "joe nicholls", True),
    ("Joe Nicholls", "Joseph Nicholls", False), ("Joe", "Joe Nicholls", False), ("Joe Nicholls", "Joe Smith", False),
    ("Zoë Müller", "Zoe Muller", True), ("", "Joe Nicholls", False), (None, None, False),
])
def test_names_match(a, b, expected):
    assert rules.names_match(a, b) is expected


def info_for(cid="c1", on="Hugo Sharpe", oe="hsharpe@assetharbour.com", **kw):
    return rules.collapse_events(call_events(cid, on=on, oe=oe, **kw))[cid]


def test_decide_admin_phase_writes_admin_pair_with_counter_plus_one():
    d = rules.decide(info_for(cancel=11), "No Answer", {F_CALL_PHASE: "Admin Call", F_ADMIN_COUNT: 2}, [], False)
    assert d.write and d.pair == "admin" and d.count_before == 2 and d.new_count == 3
    assert {f["id"]: f["field_value"] for f in d.fields} == {
        F_ADMIN_STATUS: "No Answer", F_ADMIN_COUNT: 3, F_LAST_CALL_AT: "2026-10-22", F_LAST_CALL_BY: "Hugo Sharpe",
    }


@pytest.mark.parametrize("raw,expected", [(None, 1), ("", 1), ("0", 1), (3, 4), (3.0, 4), ("2", 3), ("abc", 1), (-5, 1)])
def test_decide_counter_parsing(raw, expected):
    d = rules.decide(info_for(cancel=11), "No Answer", {F_CALL_PHASE: "Admin Call", F_ADMIN_COUNT: raw}, [], False)
    assert d.new_count == expected


def test_decide_admin_keeps_voicemail_status():
    d = rules.decide(info_for(start=10, end=40), "Voicemail", {F_CALL_PHASE: "Admin Call"}, [], False)
    assert d.status == "Voicemail"


def test_decide_advisor_phase_maps_voicemail_to_no_answer():
    vals = {F_CALL_PHASE: "Advisor Call", F_ADVISOR_NAME: "Joe Nicholls", F_ADVISOR_COUNT: 1}
    d = rules.decide(info_for(on="Joe Nicholls", start=10, end=40), "Voicemail", vals, [], True)
    assert d.pair == "advisor" and d.status == "No Answer" and d.new_count == 2
    ids = {f["id"] for f in d.fields}
    assert ids == {F_ADVISOR_STATUS, F_ADVISOR_COUNT, F_LAST_CALL_AT, F_LAST_CALL_BY}


def test_decide_advisor_answered_and_email_match():
    vals = {F_CALL_PHASE: "Advisor Call", F_ADVISOR_NAME: "Someone Else", F_ADVISOR_EMAIL: "JNicholls@assetharbour.com"}
    d = rules.decide(info_for(on="Joe Nicholls", oe="jnicholls@assetharbour.com", start=10, end=100), "Answered", vals, [], True)
    assert d.write and d.status == "Answered" and d.new_count == 1 and d.warnings == []


def test_decide_advisor_phase_other_caller_lenient_vs_strict_vs_unknown():
    vals = {F_CALL_PHASE: "Advisor Call", F_ADVISOR_NAME: "Henny Lessey"}
    names = ["Henny Lessey", "Joe Nicholls"]
    lenient = rules.decide(info_for(on="Joe Nicholls", oe="x@y.z", start=10, end=100), "Answered", vals, names, False)
    assert lenient.write and lenient.warnings == ["caller_in_advisor_list_but_not_the_named_advisor"]
    strict = rules.decide(info_for(on="Joe Nicholls", oe="x@y.z", start=10, end=100), "Answered", vals, names, True)
    assert not strict.write and strict.reason == "advisor_not_matched"
    admin_caller = rules.decide(info_for(on="Hugo Sharpe", start=10, end=100), "Answered", vals, names, False)
    assert not admin_caller.write and admin_caller.reason == "advisor_not_matched"


@pytest.mark.parametrize("phase,reason", [("None", "phase_none"), ("", "phase_empty"), (None, "phase_empty"), ("Something", "phase_something")])
def test_decide_other_phases_write_nothing(phase, reason):
    d = rules.decide(info_for(cancel=11), "No Answer", {F_CALL_PHASE: phase}, [], False)
    assert not d.write and d.reason == reason and d.fields == []


def test_last_call_at_uses_the_london_day():
    late = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)        # BST: already 2 July in London
    info = rules.collapse_events(call_events("c1", cancel=11, base=late))["c1"]
    d = rules.decide(info, "No Answer", {F_CALL_PHASE: "Admin Call"}, [], False)
    assert dict((f["id"], f["field_value"]) for f in d.fields)[F_LAST_CALL_AT] == "2026-07-02"
    winter = datetime(2026, 12, 1, 23, 30, tzinfo=timezone.utc)      # GMT: same day
    info = rules.collapse_events(call_events("c1", cancel=11, base=winter))["c1"]
    d = rules.decide(info, "No Answer", {F_CALL_PHASE: "Admin Call"}, [], False)
    assert dict((f["id"], f["field_value"]) for f in d.fields)[F_LAST_CALL_AT] == "2026-12-01"


def test_every_written_field_is_on_the_allow_list():
    for phase in ("Admin Call", "Advisor Call"):
        vals = {F_CALL_PHASE: phase, F_ADVISOR_NAME: "Hugo Sharpe"}
        d = rules.decide(info_for(start=10, end=100), "Answered", vals, [], False)
        assert d.write and {f["id"] for f in d.fields} <= rules.WRITE_FIELD_IDS
    assert F_CALL_PHASE not in rules.WRITE_FIELD_IDS and F_ADVISOR_NAME not in rules.WRITE_FIELD_IDS


@pytest.mark.parametrize("expected,actual,ok", [
    (3, 3, True), (3, 3.0, True), (3, "3", True), (3, 2, False), (3, None, False),
    ("2026-10-22", "2026-10-22T00:00:00.000Z", True), ("2026-10-22", 1792627200000, True), ("2026-10-22", "2026-10-21", False),
    ("Answered", "Answered", True), ("Answered", "No Answer", False), ("Hugo Sharpe", None, False),
])
def test_field_value_matches(expected, actual, ok):
    assert rules.field_value_matches(expected, actual) is ok


# ============================================================== GHL client

def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr("app.writeback_ghl.time.sleep", lambda s: None)


def test_find_contacts_rechecks_the_phone_and_stops_at_first_form_with_hits():
    queries = []

    def handler(request):
        queries.append(request.url.params["query"])
        return httpx.Response(200, json={"contacts": [
            {"id": "A", "phone": "+447700900123"},
            {"id": "B", "phone": "+447700900124"},                       # loose text hit, different number
            {"id": "C", "phone": "+441234", "additionalPhones": [{"phone": "07700 900123"}]},
        ]})
    found = GHLWriteback(mock_client(handler)).find_contacts("+447700900123")
    assert sorted(c["id"] for c in found) == ["A", "C"] and queries == ["+447700900123"]


def test_find_contacts_falls_back_to_national_form():
    def handler(request):
        if request.url.params["query"] == "07700900123":
            return httpx.Response(200, json={"contacts": [{"id": "A", "phone": "07700900123"}]})
        return httpx.Response(200, json={"contacts": []})
    assert [c["id"] for c in GHLWriteback(mock_client(handler)).find_contacts("+447700900123")] == ["A"]


def test_request_retries_5xx_and_429_but_not_4xx():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response([500, 429, 200][len(calls) - 1] if len(calls) <= 3 else 200,
                              json={"contact": {"customFields": [{"id": "x", "value": 1}]}})
    assert GHLWriteback(mock_client(handler)).get_contact_values("C1") == {"x": 1} and len(calls) == 3

    calls.clear()
    with pytest.raises(GHLError) as err:
        GHLWriteback(mock_client(lambda r: (calls.append(1), httpx.Response(401, json={}))[1])).get_contact_values("C1")
    assert err.value.status == 401 and len(calls) == 1


def test_request_gives_up_after_max_attempts():
    with pytest.raises(GHLError):
        GHLWriteback(mock_client(lambda r: httpx.Response(503, json={}))).get_contact_values("C1")


def test_put_refuses_fields_outside_the_allow_list_and_sends_one_request():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={})
    ghl = GHLWriteback(mock_client(handler))
    with pytest.raises(GHLError):
        ghl.put_custom_fields("C1", [{"id": F_CALL_PHASE, "field_value": "None"}])
    assert sent == []
    ghl.put_custom_fields("C1", [{"id": F_ADMIN_STATUS, "field_value": "Answered"}, {"id": F_ADMIN_COUNT, "field_value": 1}])
    assert len(sent) == 1 and sent[0].method == "PUT" and sent[0].url.path == "/contacts/C1"


def test_pick_contact_rules():
    ghl = FakeGHL(contacts=[], values={"A": {F_CALL_PHASE: "None"}, "B": {F_CALL_PHASE: "None"}, "C": {F_CALL_PHASE: "None"}})
    a, b, c = ({"id": "A", "dateUpdated": "2026-10-20"}, {"id": "B", "dateUpdated": "2026-09-01"}, {"id": "C", "dateUpdated": "2026-08-01"})
    assert pick_contact(ghl, [a]) == (a, 1, None) and pick_contact(ghl, []) == (None, 0, None)
    assert pick_contact(ghl, [b, a]) == (a, 2, None)                     # none open: most recently updated
    ghl.open_opps = {"B"}
    assert pick_contact(ghl, [a, b]) == (b, 2, None)                     # the one open case wins
    ghl.open_opps = {"A", "B"}
    assert pick_contact(ghl, [a, b, c]) == (None, 3, "ambiguous_contact")   # two open cases, neither calling
    ghl.values["B"][F_CALL_PHASE] = "Advisor Call"
    assert pick_contact(ghl, [a, b, c]) == (b, 3, None)                  # only B is in a calling phase
    ghl.values["A"][F_CALL_PHASE] = "Admin Call"
    assert pick_contact(ghl, [a, b, c]) == (None, 3, "ambiguous_contact")   # both calling: do not guess


def test_ambiguous_contact_is_logged_and_nothing_is_written():
    ghl = FakeGHL(contacts=[{"id": "C1"}, {"id": "C2"}], values={"C1": {F_CALL_PHASE: "Admin Call"}, "C2": {F_CALL_PHASE: "Admin Call"}})
    ghl.open_opps = {"C1", "C2"}
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), ghl)
    assert wbk.process_cid("c1") == "skipped" and ghl.puts == []
    assert store.log[-1]["reason"] == "ambiguous_contact" and store.statuses("c1") == ["skipped"]


# ========================================================== orchestrator

def values(ghl, cid="C1"):
    return ghl.values[cid]


def test_spec_step2_admin_no_answer_writes_status_counter_date_caller_in_one_put():
    wbk, store, ghl, _ = make(call_events("c1", cancel=11))
    assert wbk.process_cid("c1") == "written"
    assert len(ghl.puts) == 1
    v = values(ghl)
    assert (v[F_ADMIN_STATUS], v[F_ADMIN_COUNT], v[F_LAST_CALL_AT], v[F_LAST_CALL_BY]) == ("No Answer", 1, "2026-10-22", "Hugo Sharpe")
    assert store.statuses("c1") == ["claimed", "written"]


def test_spec_step3_answered_call_increments_again():
    ghl = FakeGHL(values={"C1": {F_CALL_PHASE: "Admin Call", F_ADMIN_COUNT: 1}})
    wbk, _, ghl, _ = make(call_events("c1", start=11, end=11 + 120), ghl)
    assert wbk.process_cid("c1") == "written"
    assert (values(ghl)[F_ADMIN_STATUS], values(ghl)[F_ADMIN_COUNT]) == ("Answered", 2)


def test_spec_step8_replay_is_idempotent():
    wbk, store, ghl, _ = make(call_events("c1", cancel=11))
    assert wbk.process_cid("c1") == "written"
    assert wbk.process_cid("c1") == "done" and len(ghl.puts) == 1 and values(ghl)[F_ADMIN_COUNT] == 1
    # a duplicate Evonex delivery of the terminal event changes nothing either
    store.events += [dict(e, dedup_key="dup" + e["dedup_key"]) for e in store.events]
    assert wbk.process_cid("c1") == "done" and len(ghl.puts) == 1


def test_mode_off_does_nothing_at_all():
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), config=cfg(WRITEBACK_MODE="off"))
    assert wbk.process_cid("c1") == "off" and store.log == [] and ghl.puts == []
    assert wbk.sweep() == {"mode": "off"}


def test_dry_run_reads_but_never_writes_and_records_what_it_would_do():
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), config=cfg(WRITEBACK_MODE="dry_run"))
    assert wbk.process_cid("c1") == "dry_run"
    assert ghl.puts == [] and values(ghl)[F_ADMIN_COUNT] == 0
    row = store.log[-1]
    assert row["status"] == "dry_run" and row["count_before"] == "0" and row["count_after"] == "1" and "would_write_No Answer" in row["reason"]
    assert wbk.process_cid("c1") == "done"                                # not repeated


@pytest.mark.parametrize("phase", ["None", "", None])
def test_phase_none_is_logged_and_nothing_written(phase):
    ghl = FakeGHL(values={"C1": {F_CALL_PHASE: phase}})
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), ghl)
    assert wbk.process_cid("c1") == "skipped" and ghl.puts == []
    assert store.log[-1]["status"] == "skipped" and store.log[-1]["reason"].startswith("phase_")


def test_no_contact_match_and_unusable_number_are_skipped():
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), FakeGHL(contacts=[]))
    assert wbk.process_cid("c1") == "skipped" and store.log[-1]["reason"] == "no_contact_match"
    wbk, store, ghl, _ = make(call_events("c2", cancel=11, rpi=""), FakeGHL())
    assert wbk.process_cid("c2") == "skipped" and store.log[-1]["reason"] == "no_usable_number" and ghl.puts == []


def test_several_matching_contacts_prefers_open_opportunity_and_notes_it():
    ghl = FakeGHL(contacts=[{"id": "C1", "dateUpdated": "2026-10-20"}, {"id": "C2", "dateUpdated": "2026-09-01"}],
                  values={"C1": {F_CALL_PHASE: "None"}, "C2": {F_CALL_PHASE: "Admin Call"}})
    ghl.open_opps = {"C2"}
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), ghl)
    assert wbk.process_cid("c1") == "written" and ghl.puts[0][0] == "C2"
    assert "multiple_contacts_2" in store.log[-1]["reason"]


def test_too_old_call_is_never_written():
    wbk, store, ghl, clock = make(call_events("c1", cancel=11), start=T0 + timedelta(hours=3))
    assert wbk.process_cid("c1") == "skipped" and store.log[-1]["reason"] == "too_old" and ghl.puts == []


def test_unfinished_call_is_left_for_later():
    wbk, store, ghl, _ = make(call_events("c1", start=11))                 # connected, no ENDCALL yet
    assert wbk.process_cid("c1") == "not_terminal" and store.log == [] and ghl.puts == []


def test_cancel_then_start_is_not_mistaken_for_no_answer():
    """Evonex sends CANCELCALL ~1 s before STARTCALL on connected calls (4 % of calls)."""
    events = call_events("c1", cancel=10, start=11, end=14)
    wbk, store, ghl, clock = make(events[:2], config=cfg(WRITEBACK_TALK_SECONDS=1), start=T0 + timedelta(seconds=11))

    def late_events(seconds):                                              # STARTCALL / ENDCALL arrive during the settle wait
        store.events = events
        clock.sleep(seconds)
    wbk._sleep = late_events
    assert wbk.process_cid("c1") == "written"
    assert values(ghl)[F_ADMIN_STATUS] == "Answered" and values(ghl)[F_ADMIN_COUNT] == 1


def test_cancel_with_nothing_else_after_the_settle_is_no_answer():
    wbk, store, ghl, clock = make(call_events("c1", cancel=11), start=T0 + timedelta(seconds=12))
    assert wbk.process_cid("c1") == "written" and values(ghl)[F_ADMIN_STATUS] == "No Answer"
    assert clock.now() >= T0 + timedelta(seconds=16)                       # it waited the settle period


def test_advisor_pair_end_to_end_and_status_mapping():
    ghl = FakeGHL(values={"C1": {F_CALL_PHASE: "Advisor Call", F_ADVISOR_NAME: "Joe Nicholls", F_ADVISOR_COUNT: 0}})
    events = call_events("a1", on="Joe Nicholls", oe="jnicholls@assetharbour.com", cancel=11) \
        + call_events("a2", on="Joe Nicholls", oe="jnicholls@assetharbour.com", start=20, end=20 + 200, base=T0 + timedelta(minutes=1))
    wbk, store, ghl, _ = make(events, ghl)
    assert wbk.process_cid("a1") == "written"
    assert (values(ghl)[F_ADVISOR_STATUS], values(ghl)[F_ADVISOR_COUNT]) == ("No Answer", 1)
    assert wbk.process_cid("a2") == "written"
    assert (values(ghl)[F_ADVISOR_STATUS], values(ghl)[F_ADVISOR_COUNT]) == ("Answered", 2)
    assert F_ADMIN_STATUS not in values(ghl) and F_ADMIN_COUNT not in values(ghl)


def test_advisor_phase_by_an_unrelated_caller_is_skipped():
    ghl = FakeGHL(values={"C1": {F_CALL_PHASE: "Advisor Call", F_ADVISOR_NAME: "Joe Nicholls"}})
    wbk, store, ghl, _ = make(call_events("c1", on="Hugo Sharpe", cancel=11), ghl)
    assert wbk.process_cid("c1") == "skipped" and ghl.puts == [] and store.log[-1]["reason"] == "advisor_not_matched"


def test_counter_is_read_fresh_every_time():
    wbk, store, ghl, _ = make(call_events("c1", cancel=11) + call_events("c2", cancel=11, base=T0 + timedelta(minutes=2)))
    wbk.process_cid("c1")
    values(ghl)[F_ADMIN_COUNT] = 0                                         # a workflow reset the counter in between
    wbk.process_cid("c2")
    assert values(ghl)[F_ADMIN_COUNT] == 1                                 # not 2: no cached value


# ---- idempotency under concurrency

def other_claim(store, cid, contact, token, age=1.0):
    store.log.append({"logged_at_utc": (store.clock.now() - timedelta(seconds=age)).isoformat(), "cid": cid,
                      "status": "claimed", "contact_id": contact, "claim_token": token})


def test_second_worker_for_the_same_cid_yields_without_closing_the_winners_claim():
    wbk, store, ghl, _ = make(call_events("c1", cancel=11))
    other_claim(store, "c1", "C1", "other")                                # another worker got there first
    assert wbk.process_cid("c1") == "duplicate" and ghl.puts == []
    assert store.log[-1]["status"] == "released"
    # the winner's claim is still active (the release did not close it)
    assert [r["claim_token"] for _, r in wbk._active_claims(store.recent_log())] == ["other"]


def test_same_contact_calls_are_serialised_later_one_waits_for_the_earlier():
    wbk, store, ghl, clock = make(call_events("c2", cancel=11))
    other_claim(store, "c1", "C1", "first")                                # c1 is mid-write on the same contact

    def finish_first(seconds):
        clock.sleep(seconds)
        store.log.append({"logged_at_utc": clock.now().isoformat(), "cid": "c1", "status": "written", "claim_token": "first"})
        values(ghl)[F_ADMIN_COUNT] = 1                                     # c1's write landed
    wbk._sleep = finish_first
    assert wbk.process_cid("c2") == "written"
    assert values(ghl)[F_ADMIN_COUNT] == 2                                 # read AFTER c1: 1 -> 2, never both writing 1


def test_lock_timeout_is_a_retryable_failure_not_a_write():
    wbk, store, ghl, _ = make(call_events("c2", cancel=11))
    other_claim(store, "c1", "C1", "stuck")                                # never closed (within TTL for the wait)
    assert wbk.process_cid("c2") == "failed" and ghl.puts == []
    assert store.log[-1]["status"] == "failed" and store.log[-1]["reason"] == "lock_timeout"


def test_a_crashed_workers_claim_expires():
    wbk, store, ghl, _ = make(call_events("c2", cancel=11))
    other_claim(store, "c1", "C1", "dead", age=wb.CLAIM_TTL_SECONDS + 30)
    assert wbk.process_cid("c2") == "written"


# ---- failure handling

def test_search_failure_is_logged_and_retried_by_the_sweeper_then_given_up():
    ghl = FakeGHL()
    ghl.fail_search = 99
    wbk, store, ghl, clock = make(call_events("c1", cancel=11), ghl)
    for _ in range(wb.MAX_FAILED_ATTEMPTS):
        assert wbk.process_cid("c1") == "failed"
    assert wbk.process_cid("c1") == "gave_up" and ghl.puts == []


def test_failed_put_is_retried_once_and_counts_once():
    ghl = FakeGHL()
    ghl.fail_put_times = 1
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), ghl)
    assert wbk.process_cid("c1") == "written"
    assert len(ghl.puts) == 1 and values(ghl)[F_ADMIN_COUNT] == 1


def test_put_that_errored_but_was_applied_is_not_sent_twice():
    ghl = FakeGHL()
    ghl.fail_put_times, ghl.put_applies_when_failing = 1, True
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), ghl)
    assert wbk.process_cid("c1") == "written"
    assert ghl.puts == [] and values(ghl)[F_ADMIN_COUNT] == 1              # verified from the read-back, no second PUT


def test_put_failing_twice_is_a_retryable_failure():
    ghl = FakeGHL()
    ghl.fail_put_times = 2
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), ghl)
    assert wbk.process_cid("c1") == "failed" and values(ghl)[F_ADMIN_COUNT] == 0
    assert store.statuses("c1")[-1] == "failed"


def test_unverified_write_is_recorded_and_not_resent():
    ghl = FakeGHL()
    ghl.drop_field = F_LAST_CALL_AT                                        # GHL ignores the date field
    wbk, store, ghl, _ = make(call_events("c1", cancel=11), ghl)
    assert wbk.process_cid("c1") == "written_unverified"
    assert len(ghl.puts) == 1 and F_LAST_CALL_AT in store.log[-1]["reason"]
    assert wbk.process_cid("c1") == "done"


# ---- sweep

def test_sweep_processes_only_finished_unprocessed_recent_calls():
    ghl = FakeGHL(values={f"C1": {F_CALL_PHASE: "Admin Call", F_ADMIN_COUNT: 0}})
    events = (call_events("done", cancel=11) + call_events("fresh", cancel=11, base=T0 + timedelta(minutes=9, seconds=58))
              + call_events("live", start=11, base=T0 + timedelta(minutes=2)) + call_events("todo", cancel=11, base=T0 + timedelta(minutes=3))
              + call_events("ancient", cancel=11, base=T0 - timedelta(hours=5)))
    wbk, store, ghl, clock = make(events, ghl)
    store.append_log(cid="done", status="written")
    store.append_log(cid="claimedcid", status="claimed", claim_token="t")
    out = wbk.sweep()
    assert out["processed"] == {"written": 1}                              # only "todo"
    assert store.statuses("todo") == ["claimed", "written"] and store.statuses("fresh") == []
    assert store.statuses("ancient") == [] and store.statuses("live") == []
    assert wbk.sweep()["processed"] == {}                                  # nothing left


def test_sweep_respects_the_failure_backoff_and_max_calls():
    ghl = FakeGHL()
    events = sum((call_events(f"c{i}", cancel=11, base=T0 + timedelta(minutes=i)) for i in range(5)), [])
    wbk, store, ghl, clock = make(events, ghl)
    store.append_log(cid="c0", status="failed", reason="ghl_http_500")     # failed just now: wait
    out = wbk.sweep(max_calls=2)
    assert out["candidates"] == 4 and sum(out["processed"].values()) == 2
    assert store.statuses("c0") == ["failed"]
    clock.sleep(wb.RETRY_AFTER_FAILURE_SECONDS + 1)
    assert wbk.sweep(max_calls=10)["processed"]                            # c0 eligible again


# ============================================================= API wiring

def test_terminal_outbound_event_schedules_the_write_back_after_the_response(monkeypatch):
    from api import evonex as api
    from app import config

    monkeypatch.setattr(config, "WRITEBACK_MODE", "on")
    t = api._writeback_task("OUTBOUNDCALL", "ENDCALL", {"cid": "c1"})
    assert t is not None and t.args == ("c1",) and t.func is wb.process_cid_safely
    assert api._writeback_task("OUTBOUNDCALL", "CANCELCALL", {"cid": "c1"}) is not None
    for event, sub, payload in [("OUTBOUNDCALL", "RINGCALL", {"cid": "c1"}), ("OUTBOUNDCALL", "STARTCALL", {"cid": "c1"}),
                                ("RECORDINGREADY", None, {"cid": "c1"}), ("OUTBOUNDCALL", "ENDCALL", {}), ("OUTBOUNDCALL", "ENDCALL", {"cid": 5})]:
        assert api._writeback_task(event, sub, payload) is None
    monkeypatch.setattr(config, "WRITEBACK_MODE", "off")
    assert api._writeback_task("OUTBOUNDCALL", "ENDCALL", {"cid": "c1"}) is None


def test_webhook_runs_the_write_back_after_storing_and_still_returns_200(monkeypatch):
    from fastapi.testclient import TestClient
    from api import evonex as api
    from app import config, evonex
    from tests.test_evonex import BASE, outbound, send

    rows, ran = [], []
    monkeypatch.setattr(evonex, "_append_to_sheet", lambda values: rows.append(values))
    monkeypatch.setattr(config, "EVONEX_WEBHOOK_TOKENS", ["dummy-evonex-token"])
    monkeypatch.setattr(config, "EVONEX_SHEET_ID", "dummy-evonex-sheet")
    monkeypatch.setattr(config, "WRITEBACK_MODE", "on")
    monkeypatch.setattr(wb, "process_cid_safely", lambda cid: ran.append(cid))
    client = TestClient(api.app, base_url=BASE, raise_server_exceptions=False)
    assert send(client, "/api/evonex/call", outbound("ENDCALL")).status_code == 200
    assert len(rows) == 1 and ran == ["call-out-0001"]
    assert send(client, "/api/evonex/call", outbound("RINGCALL")).status_code == 200 and ran == ["call-out-0001"]


def test_sweep_endpoint_needs_the_cron_secret(monkeypatch):
    from fastapi.testclient import TestClient
    from api import evonex as api
    from app import config

    monkeypatch.setattr(wb, "sweep_safely", lambda: {"mode": "on", "processed": {}})
    client = TestClient(api.app, raise_server_exceptions=False)
    assert client.post("/api/evonex/writeback/sweep").status_code == 401
    assert client.post("/api/evonex/writeback/sweep", headers={"x-cron-secret": "wrong"}).status_code == 401
    ok = client.post("/api/evonex/writeback/sweep", headers={"x-cron-secret": config.CRON_SECRET})
    assert ok.status_code == 200 and ok.json()["mode"] == "on"


def test_safe_entry_points_never_raise_and_respect_mode_off(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "WRITEBACK_MODE", "on")
    monkeypatch.setattr(wb, "_build", lambda: (_ for _ in ()).throw(RuntimeError("sheets down")))
    wb.process_cid_safely("c1")                                            # must not raise
    assert wb.sweep_safely() == {"error": "RuntimeError"}
    monkeypatch.setattr(config, "WRITEBACK_MODE", "off")
    assert wb.sweep_safely() == {"mode": "off"}


def test_invalid_mode_env_value_is_treated_as_off(monkeypatch):
    import importlib
    from app import config
    monkeypatch.setenv("WRITEBACK_MODE", "ON ")
    importlib.reload(config)
    assert config.WRITEBACK_MODE == "on"
    monkeypatch.setenv("WRITEBACK_MODE", "yes")
    importlib.reload(config)
    assert config.WRITEBACK_MODE == "off"
    monkeypatch.delenv("WRITEBACK_MODE")
    importlib.reload(config)
    assert config.WRITEBACK_MODE == "off"


# ============================================================ store helpers

def test_store_retry_helper_retries_then_raises(monkeypatch):
    from app import writeback_store as ws
    monkeypatch.setattr(ws.time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("429")
        return "ok"
    assert ws._with_retry(flaky) == "ok" and len(calls) == 3
    with pytest.raises(RuntimeError):
        ws._with_retry(lambda: (_ for _ in ()).throw(RuntimeError("down")))
