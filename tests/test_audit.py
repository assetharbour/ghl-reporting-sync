"""Tests for appointments, Created By / Booked By locking and flags, the audit
runner, the sheet tab store and the sales KPI definitions."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import gspread
import pytest

from app import appointments as ap
from app import audit_runner as runner
from app import field_audit as fa
from app import sales_metrics as sm
from app.audit_store import AuditStores, TabStore

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
USERS = {"u-hugo": "Hugo Sharpe", "u-harry": "Harry Kalsi", "u-ava": "Ava Morris-Boundy",
         "u-joe": "Joe Nicholls", "u-xav": "Xavier Collin", "u-anita": "Anita Andrews"}
ADVISORS = ["Joe Nicholls", "Xavier Collin", "Cecilia Szabo", "Henny Lessey"]


def event(eid="e1", contact="c1", booker="u-hugo", advisor="u-joe", added="2026-10-20T09:00:00.000Z",
          start="2026-10-23T10:00:00+01:00", status="confirmed", deleted=False, source="contactdetails_page"):
    return {"id": eid, "contactId": contact, "calendarId": "cal", "createdBy": {"source": source, "userId": booker},
            "assignedUserId": advisor, "dateAdded": added, "startTime": start, "endTime": start,
            "dateUpdated": added, "appointmentStatus": status, "deleted": deleted}


def rows(*events):
    return ap.events_to_rows(list(events), USERS)


# =============================================================== appointments

def test_event_to_row_resolves_names_and_converts_to_utc():
    r = ap.event_to_row(event(), USERS)
    assert (r["booked_by"], r["advisor"], r["booked_source"]) == ("Hugo Sharpe", "Joe Nicholls", "contactdetails_page")
    assert r["booked_at"] == "2026-10-20T09:00:00Z" and r["start_time"] == "2026-10-23T09:00:00Z"   # BST -> UTC
    assert r["status"] == "confirmed" and r["deleted"] == ""


def test_event_to_row_tolerates_missing_pieces():
    r = ap.event_to_row({"id": "e9", "createdBy": "garbage", "appoinmentStatus": "SHOWED"}, USERS)
    assert r["booked_by"] == "" and r["advisor"] == "" and r["status"] == "showed" and r["booked_at"] == ""
    assert ap.event_to_row({"contactId": "x"}, USERS) is None


def test_events_deduped_by_id_freshest_wins_and_sorted_by_booking_time():
    a = event("e1", added="2026-10-21T09:00:00Z", status="new")
    b = event("e2", added="2026-10-20T09:00:00Z")
    a2 = event("e1", added="2026-10-21T09:00:00Z", status="cancelled")
    out = ap.events_to_rows([a, b, a2], USERS)
    assert [r["appointment_id"] for r in out] == ["e2", "e1"] and out[1]["status"] == "cancelled"


@pytest.mark.parametrize("status,deleted,live", [("confirmed", False, True), ("showed", False, True), ("cancelled", False, False),
                                                 ("invalid", False, False), ("confirmed", True, False)])
def test_is_live(status, deleted, live):
    assert ap.is_live(ap.event_to_row(event(status=status, deleted=deleted), USERS)) is live


# ============================================================ locks and flags

def snap(cid="c1", created="", booked="", advisor=""):
    return {"contact_id": cid, "created_by": created, "booked_by": booked, "advisor_name": advisor}


def test_snapshot_reads_the_three_fields_and_trims():
    contact = {"id": "c1", "customFields": [{"id": fa.F_CREATED_BY, "value": " Ava Morris-Boundy "}, {"id": fa.F_BOOKED_BY, "value": None},
                                            {"id": fa.F_ADVISOR_NAME, "value": "Joe Nicholls"}, {"id": "other", "value": "x"}]}
    assert fa.snapshot(contact) == {**snap("c1", "Ava Morris-Boundy", "", "Joe Nicholls"), "owner_user_id": ""}
    assert fa.snapshot({**contact, "assignedTo": "u-9"})["owner_user_id"] == "u-9"


def test_first_value_is_locked_without_a_flag():
    locks, flags = fa.audit_created_by([snap(created="Ava Morris-Boundy")], {}, NOW)
    assert flags == [] and len(locks) == 1
    assert (locks[0]["field"], locks[0]["first_value"], locks[0]["first_seen_at"]) == ("created_by", "Ava Morris-Boundy", "2026-10-22T12:00:00Z")
    assert fa.audit_created_by([snap()], {}, NOW) == ([], [])                      # empty field: nothing to lock


def lock(field="created_by", first="Ava Morris-Boundy", current=None, cid="c1"):
    return {(cid, field): {"contact_id": cid, "field": field, "first_value": first, "first_seen_at": "2026-10-01T00:00:00Z",
                           "current_value": current if current is not None else first, "current_seen_at": "2026-10-01T00:00:00Z"}}


def test_unchanged_value_writes_nothing():
    assert fa.audit_created_by([snap(created="Ava Morris-Boundy")], lock(), NOW) == ([], [])


def test_a_change_is_flagged_with_old_new_and_the_locked_value_and_the_lock_keeps_first_value():
    out_locks, flags = fa.audit_created_by([snap(created="Hugo Sharpe")], lock(), NOW)
    f = flags[0]
    assert (f["type"], f["field"], f["old_value"], f["new_value"], f["locked_value"], f["status"]) == \
        ("created_by_changed", "created_by", "Ava Morris-Boundy", "Hugo Sharpe", "Ava Morris-Boundy", "open")
    assert out_locks[0]["first_value"] == "Ava Morris-Boundy" and out_locks[0]["current_value"] == "Hugo Sharpe"
    # next cycle sees the same current value: no second flag
    assert fa.audit_created_by([snap(created="Hugo Sharpe")], {("c1", "created_by"): out_locks[0]}, NOW) == ([], [])


def test_clearing_and_restoring_are_flagged_distinctly():
    _, cleared = fa.audit_created_by([snap(created="")], lock(), NOW)
    assert cleared[0]["type"] == "created_by_cleared" and cleared[0]["detail"] == "was filled in, now empty"
    _, restored = fa.audit_created_by([snap(created="Ava Morris-Boundy")], lock(current="Hugo Sharpe"), NOW)
    assert restored[0]["type"] == "created_by_changed" and "locked value" in restored[0]["detail"]


def test_flag_ids_are_deterministic_and_sensitive_to_every_part():
    base = fa.flag_id("t", "c", "f", "a", "b", "x")
    assert base == fa.flag_id("t", "c", "f", "a", "b", "x")
    assert len({base, fa.flag_id("t2", "c", "f", "a", "b", "x"), fa.flag_id("t", "c2", "f", "a", "b", "x"), fa.flag_id("t", "c", "f2", "a", "b", "x"),
                fa.flag_id("t", "c", "f", "a2", "b", "x"), fa.flag_id("t", "c", "f", "a", "b2", "x"), fa.flag_id("t", "c", "f", "a", "b", "x2")}) == 7


SINCE = NOW - timedelta(days=7)


def cal(**kw):
    return rows(event(**kw))


def judge(snaps, locks, appts, **kw):
    kwargs = dict(advisor_names=ADVISORS, now=NOW, since=SINCE)
    kwargs.update(kw)
    return fa.audit_appointments(snaps, locks, appts, kwargs.pop("advisor_names"), kwargs.pop("now"), kwargs.pop("since"), **kwargs)


def types(flags):
    return [f["type"] for f in flags]


def test_with_an_empty_booked_by_field_the_calendar_booker_is_used_and_nothing_is_flagged():
    for added in ("2026-10-22T11:00:00Z", "2026-10-20T09:00:00Z"):              # fresh and past the grace period
        new_locks, flags, official = judge([snap(booked="", advisor="Joe Nicholls")], {}, cal(added=added))
        assert flags == [] and new_locks == [] and official == {"e1": ("Hugo Sharpe", "calendar")}


def test_a_filled_booked_by_field_wins_and_its_first_value_is_locked():
    new_locks, flags, official = judge([snap(booked="Hugo Sharpe", advisor="Joe Nicholls")], {}, cal())
    assert flags == [] and official == {"e1": ("Hugo Sharpe", "field")}
    assert (new_locks[0]["field"], new_locks[0]["first_value"]) == ("booked_by:e1", "Hugo Sharpe")


def test_a_field_value_that_differs_from_the_calendar_is_used_but_flagged():
    new_locks, flags, official = judge([snap(booked="Harry Kalsi")], {}, cal())
    assert official["e1"] == ("Harry Kalsi", "field")                         # the field wins, as agreed
    assert types(flags) == ["booked_by_mismatch"]
    assert (flags[0]["old_value"], flags[0]["new_value"], flags[0]["locked_value"], flags[0]["appointment_id"]) == ("Harry Kalsi", "Hugo Sharpe", "Harry Kalsi", "e1")


def test_the_field_matching_the_calendar_booker_by_first_and_last_word_is_not_a_mismatch():
    assert types(judge([snap(booked="Ava Mae Morris-Boundy")], {}, cal(booker="u-ava"))[1]) == []


def test_appointments_booked_before_since_are_never_judged_but_still_get_an_official_booker():
    old = cal(added="2026-08-01T09:00:00Z")
    _, flags, official = judge([snap(booked="Harry Kalsi")], {}, old)
    assert flags == [] and official["e1"] == ("Hugo Sharpe", "calendar")


def test_advisor_mismatch_and_non_advisor_assignment():
    assert types(judge([snap(booked="Hugo Sharpe", advisor="Xavier Collin")], {}, cal())[1]) == ["advisor_mismatch"]
    flags = judge([snap(booked="Hugo Sharpe")], {}, cal(advisor="u-anita"))[1]
    assert types(flags) == ["appointment_with_non_advisor"] and flags[0]["new_value"] == "Anita Andrews"


def test_only_the_latest_live_appointment_takes_the_field_and_older_ones_use_the_calendar():
    appts = rows(event("e1", booker="u-harry", added="2026-10-18T09:00:00Z"), event("e2", booker="u-hugo", added="2026-10-20T09:00:00Z"),
                 event("e3", booker="u-ava", added="2026-10-21T09:00:00Z", status="cancelled"))
    new_locks, flags, official = judge([snap(booked="Hugo Sharpe")], {}, appts)
    assert flags == [] and official["e1"] == ("Harry Kalsi", "calendar") and official["e2"] == ("Hugo Sharpe", "field")
    assert [r["field"] for r in new_locks] == ["booked_by:e2"]                  # the cancelled e3 is never judged or locked


WIDGET = dict(source="booking_widget")


def test_a_booking_from_another_source_has_no_calendar_booker_so_the_field_is_the_only_source():
    new_locks, flags, official = judge([snap(booked="Ava Morris-Boundy")], {}, cal(**WIDGET))
    assert flags == [] and official == {"e1": ("Ava Morris-Boundy", "field")}      # no calendar booker to disagree with
    assert (new_locks[0]["field"], new_locks[0]["first_value"]) == ("booked_by:e1", "Ava Morris-Boundy")


def form_lock(first="Ava Morris-Boundy", current=None, cid="c1", aid="e1"):
    return {(cid, f"booked_by:{aid}"): {"contact_id": cid, "field": f"booked_by:{aid}", "first_value": first, "first_seen_at": "2026-10-20T10:00:00Z",
                                         "current_value": current if current is not None else first, "current_seen_at": "2026-10-20T10:00:00Z"}}


def test_changing_the_field_later_is_flagged_and_the_locked_value_stays_official():
    new_locks, flags, official = judge([snap(booked="Hugo Sharpe")], form_lock(), cal(**WIDGET))
    assert types(flags) == ["booked_by_changed"]
    assert (flags[0]["old_value"], flags[0]["new_value"], flags[0]["locked_value"]) == ("Ava Morris-Boundy", "Hugo Sharpe", "Ava Morris-Boundy")
    assert official["e1"] == ("Ava Morris-Boundy", "field")
    assert new_locks[0]["first_value"] == "Ava Morris-Boundy" and new_locks[0]["current_value"] == "Hugo Sharpe"
    assert judge([snap(booked="Hugo Sharpe")], {("c1", "booked_by:e1"): new_locks[0]}, cal(**WIDGET))[1] == []      # not re-flagged next cycle
    assert types(judge([snap(booked="")], form_lock(), cal(**WIDGET))[1]) == ["booked_by_cleared"]


def test_an_edit_cannot_change_the_official_booker_even_when_a_calendar_booker_exists():
    """The pay protection: a rep edits Booked By after the fact; reports keep the locked first value."""
    new_locks, flags, official = judge([snap(booked="Harry Kalsi")], form_lock("Hugo Sharpe"), cal())
    assert official["e1"] == ("Hugo Sharpe", "field") and "booked_by_changed" in types(flags)
    cleared = judge([snap(booked="")], form_lock("Hugo Sharpe"), cal())
    assert cleared[2]["e1"] == ("Hugo Sharpe", "field") and types(cleared[1]) == ["booked_by_cleared"]        # clearing does not fall back to the calendar


def test_form_booking_without_booked_by_is_flagged_after_the_grace_period_and_has_no_official_booker():
    old = cal(added=(NOW - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), **WIDGET)
    new_locks, flags, official = judge([snap()], {}, old)
    assert types(flags) == ["booked_by_missing"] and official == {} and new_locks == []


def test_a_rebooking_starts_a_fresh_lock_and_the_old_appointment_keeps_its_own_official_booker():
    appts = rows(event("e1", added="2026-10-15T09:00:00Z", **WIDGET), event("e2", added="2026-10-21T09:00:00Z", **WIDGET))
    locks = form_lock("Ava Morris-Boundy", aid="e1")
    new_locks, flags, official = judge([snap(booked="Harry Kalsi")], locks, appts)
    assert flags == []                                                         # the field legitimately changed with the new booking
    assert official["e1"] == ("Ava Morris-Boundy", "field") and official["e2"] == ("Harry Kalsi", "field")


def test_form_booking_is_not_flagged_for_a_missing_booked_by_inside_the_grace_period():
    fresh = cal(added=(NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), **WIDGET)
    new_locks, flags, official = judge([snap()], {}, fresh)
    assert flags == [] and official == {} and new_locks == []


def test_a_user_id_from_an_untrusted_source_is_not_used_as_the_booker():
    _, _, official = judge([snap()], {}, cal(**WIDGET))
    assert "e1" not in official


def test_new_booking_sources_are_flagged_once():
    appts = rows(event("e1", **WIDGET), event("e2", **WIDGET), event("e3"))
    flags = fa.source_flags(appts, ("contactdetails_page",), NOW)
    assert types(flags) == ["booking_source_not_trusted"] and flags[0]["new_value"] == "booking_widget"


# ================================================================ tab store

class FakeWS:
    def __init__(self):
        self.values = []
        self.writes = {"update": 0, "batch": 0, "append": 0}

    def row_values(self, n):
        return list(self.values[0]) if self.values else []

    def update(self, values=None, range_name=None, **kw):
        self.writes["update"] += 1
        self.values = [list(r) for r in values]

    def get_all_values(self):
        return [list(r) for r in self.values]

    def append_rows(self, rows, **kw):
        self.writes["append"] += 1
        self.values += [list(r) for r in rows]

    def batch_update(self, updates, **kw):
        self.writes["batch"] += 1
        for u in updates:
            row = int(u["range"].split(":")[0][1:])
            self.values[row - 1] = list(u["values"][0])


class FakeSheet:
    def __init__(self):
        self.tabs = {}

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS()
        return self.tabs[title]


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr("app.writeback_store.time.sleep", lambda s: None)


def test_tab_is_created_with_its_header_and_rows_append():
    sheet = FakeSheet()
    store = TabStore(sheet, "T", ["id", "v"], ["id"])
    assert store.read() == []
    assert sheet.tabs["T"].values[0] == ["id", "v"]
    store.append([{"id": "1", "v": "a"}])
    assert store.read() == [{"id": "1", "v": "a"}]


def test_upsert_inserts_updates_and_skips_unchanged_rows():
    sheet = FakeSheet()
    store = TabStore(sheet, "T", ["id", "v"], ["id"])
    assert store.upsert([{"id": "1", "v": "a"}, {"id": "2", "v": "b"}]) == {"inserted": 2, "updated": 0}
    ws = sheet.tabs["T"]
    before = dict(ws.writes)
    assert store.upsert([{"id": "1", "v": "a"}, {"id": "2", "v": "b"}]) == {"inserted": 0, "updated": 0}
    assert ws.writes == before                                               # unchanged rows cost no writes
    assert store.upsert([{"id": "2", "v": "B"}, {"id": "3", "v": "c"}, {"id": "3", "v": "c2"}]) == {"inserted": 1, "updated": 1}
    assert {r["id"]: r["v"] for r in store.read()} == {"1": "a", "2": "B", "3": "c"}   # in-batch duplicate inserted once


def test_audit_stores_state_roundtrip():
    stores = AuditStores(sheet=FakeSheet())
    assert stores.get_state("last_run_utc") == ""
    stores.set_state("last_run_utc", "2026-10-22T12:00:00Z")
    stores.set_state("last_run_utc", "2026-10-22T12:15:00Z")
    assert stores.get_state("last_run_utc") == "2026-10-22T12:15:00Z" and len(stores.state.read()) == 1


# ================================================================== runner

def cfg(**over):
    base = dict(FIELD_AUDIT="on", FIELD_AUDIT_SINCE="", APPOINTMENT_CALENDAR_IDS=["cal"], BOOKED_BY_GRACE_HOURS=24.0,
                FULL_REFRESH_HOUR_UTC=3, WRITEBACK_ADVISOR_NAMES=ADVISORS, TRUSTED_BOOKING_SOURCES=["contactdetails_page"])
    base.update(over)
    return SimpleNamespace(**base)


def contact(cid, created="", booked="", advisor=""):
    fields = [{"id": fa.F_CREATED_BY, "value": created}, {"id": fa.F_BOOKED_BY, "value": booked}, {"id": fa.F_ADVISOR_NAME, "value": advisor}]
    return {"id": cid, "customFields": fields}


class FakeGHL:
    def __init__(self, events=None, contacts=None):
        self.events, self.contacts = events if events is not None else [], contacts or {}
        self.fail_calendar = self.fail_users = self.fail_search = False
        self.calls = []

    async def get_users(self):
        return None if self.fail_users else dict(USERS)

    async def get_calendar_events(self, calendar_id, start, end):
        self.calls.append(("events", calendar_id, start, end))
        return None if self.fail_calendar else list(self.events)

    async def search_contacts_with_field(self, field_id):
        self.calls.append(("with_field", field_id))
        return None if self.fail_search else [c for c in self.contacts.values()
                                              if any(f["id"] == field_id and f["value"] for f in c["customFields"])]

    async def search_contacts_updated_since(self, since):
        self.calls.append(("updated_since", since))
        return None if self.fail_search else list(self.contacts.values())

    async def get_all_opportunities(self):
        return []                                  # speed to lead has nothing to judge in these tests

    async def get_all_contacts(self, ids):
        self.calls.append(("get_all", tuple(ids)))
        return {i: self.contacts[i] for i in ids if i in self.contacts}


def go(ghl, stores, now=NOW, **over):
    return asyncio.run(runner.run_cycle(ghl, stores, cfg(**over), now=now))


def booked_hours_ago(h, **kw):
    return event(added=(NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"), **kw)


def test_mode_off_does_nothing_and_touches_nothing():
    ghl, stores = FakeGHL(), AuditStores(sheet=FakeSheet())
    assert go(ghl, stores, FIELD_AUDIT="") == {"mode": "off"} and ghl.calls == [] and stores.sheet.tabs == {}


def test_first_run_is_a_full_pass_writes_appointments_locks_and_flags_once():
    ghl = FakeGHL([booked_hours_ago(30)], {"c1": contact("c1", created="Ava Morris-Boundy", booked="Harry Kalsi", advisor="Joe Nicholls")})
    stores = AuditStores(sheet=FakeSheet())
    out = go(ghl, stores)
    assert out["full_pass"] and out["appointments"] == 1 and out["locks_written"] == 2
    assert [f["type"] for f in stores.flags.read()] == ["booked_by_mismatch"]                  # Harry in the field vs Hugo in the calendar
    assert {(r["field"], r["first_value"]) for r in stores.locks.read()} == {("created_by", "Ava Morris-Boundy"), ("booked_by:e1", "Harry Kalsi")}
    row = stores.appointments.read()[0]
    assert (row["booked_by"], row["booked_by_official"], row["booked_by_basis"]) == ("Hugo Sharpe", "Harry Kalsi", "field")
    assert stores.get_state(runner.STATE_LAST_RUN) == "2026-10-22T12:00:00Z"
    # a quarter of an hour later: incremental pass, no duplicate flags
    out2 = go(ghl, stores, now=NOW + timedelta(minutes=15))
    assert not out2["full_pass"] and out2["flags_new"] == 0 and len(stores.flags.read()) == 1
    assert ("updated_since", NOW - runner.OVERLAP) in ghl.calls


def test_with_no_booked_by_field_the_official_booker_is_the_calendar_booker_in_the_sheet():
    ghl = FakeGHL([booked_hours_ago(30)], {"c1": contact("c1")})
    stores = AuditStores(sheet=FakeSheet())
    go(ghl, stores)
    row = stores.appointments.read()[0]
    assert (row["booked_by_official"], row["booked_by_basis"]) == ("Hugo Sharpe", "calendar") and stores.flags.read() == []


def test_change_after_locking_is_flagged_in_a_later_cycle():
    ghl = FakeGHL([], {"c1": contact("c1", created="Ava Morris-Boundy")})
    stores = AuditStores(sheet=FakeSheet())
    go(ghl, stores)
    ghl.contacts["c1"] = contact("c1", created="Hugo Sharpe")
    out = go(ghl, stores, now=NOW + timedelta(minutes=15))
    flag = stores.flags.read()[0]
    assert out["flags_new"] == 1 and (flag["type"], flag["old_value"], flag["new_value"], flag["locked_value"]) == \
        ("created_by_changed", "Ava Morris-Boundy", "Hugo Sharpe", "Ava Morris-Boundy")
    assert [r["first_value"] for r in stores.locks.read() if r["field"] == "created_by"] == ["Ava Morris-Boundy"]


def test_appointment_contacts_not_in_the_search_are_fetched_individually():
    ghl = FakeGHL([booked_hours_ago(30)], {"c1": contact("c1")})
    go(ghl, AuditStores(sheet=FakeSheet()))
    assert ("get_all", ("c1",)) in ghl.calls


@pytest.mark.parametrize("failure", ["fail_calendar", "fail_users", "fail_search"])
def test_any_failed_read_aborts_without_writing_anything(failure):
    ghl = FakeGHL([booked_hours_ago(30)], {"c1": contact("c1", booked="Harry Kalsi")})
    setattr(ghl, failure, True)
    stores = AuditStores(sheet=FakeSheet())
    out = go(ghl, stores)
    assert "error" in out
    assert stores.get_state(runner.STATE_LAST_RUN) == "" and stores.flags.read() == [] and stores.locks.read() == []
    assert stores.appointments.read() == []                                    # nothing at all is written on a failed read


def test_flags_per_run_are_capped(monkeypatch):
    monkeypatch.setattr(runner, "MAX_FLAGS_PER_RUN", 3)
    events = [booked_hours_ago(30, eid=f"e{i}", contact=f"c{i}") for i in range(6)]
    ghl = FakeGHL(events, {f"c{i}": contact(f"c{i}", booked="Harry Kalsi") for i in range(6)})       # six mismatches with the calendar
    stores = AuditStores(sheet=FakeSheet())
    out = go(ghl, stores)
    assert out["flags_new"] == 3 and out["flags_capped"] and len(stores.flags.read()) == 3


def test_first_run_reads_a_long_history_later_runs_a_short_one():
    ghl = FakeGHL()
    stores = AuditStores(sheet=FakeSheet())
    go(ghl, stores)
    go(ghl, stores, now=NOW + timedelta(minutes=15))
    first, second = [c for c in ghl.calls if c[0] == "events"]
    assert (first[3] - first[2]).days == runner.FIRST_RUN_BACK_DAYS + runner.FORWARD_DAYS
    assert (second[3] - second[2]).days == runner.BACK_DAYS + runner.FORWARD_DAYS


def test_a_field_value_is_locked_and_a_later_change_flagged_across_cycles():
    ghl = FakeGHL([booked_hours_ago(2, source="booking_widget")], {"c1": contact("c1", booked="Ava Morris-Boundy")})
    stores = AuditStores(sheet=FakeSheet())
    go(ghl, stores)
    first = stores.appointments.read()[0]
    assert first["booked_by_official"] == "Ava Morris-Boundy" and first["booked_by_basis"] == "field"
    ghl.contacts["c1"] = contact("c1", booked="Hugo Sharpe")
    go(ghl, stores, now=NOW + timedelta(minutes=15))
    assert [f["type"] for f in stores.flags.read() if f["type"] != "booking_source_not_trusted"] == ["booked_by_changed"]
    assert stores.appointments.read()[0]["booked_by_official"] == "Ava Morris-Boundy"       # the lock, not the edit


def test_the_official_booker_survives_a_cycle_that_did_not_read_the_contact():
    ghl = FakeGHL([booked_hours_ago(2, source="booking_widget")], {"c1": contact("c1", booked="Ava Morris-Boundy")})
    stores = AuditStores(sheet=FakeSheet())
    go(ghl, stores)
    ghl.contacts = {}                                                          # contact missing from the next cycle's reads
    go(ghl, stores, now=NOW + timedelta(minutes=15))
    assert stores.appointments.read()[0]["booked_by_official"] == "Ava Morris-Boundy"


def test_run_safely_never_raises(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "FIELD_AUDIT", "on")
    monkeypatch.setattr(runner, "AuditStores", lambda: (_ for _ in ()).throw(RuntimeError("sheets down")))
    assert asyncio.run(runner.run_safely(FakeGHL())) == {"error": "RuntimeError"}
    monkeypatch.setattr(config, "FIELD_AUDIT", "off")
    assert asyncio.run(runner.run_safely(FakeGHL())) == {"mode": "off"}


# ============================================================== sales metrics

def appt(eid, contact, booker="Hugo Sharpe", advisor="Joe Nicholls", booked="2026-10-05T09:00:00Z", start="2026-10-08T09:00:00Z",
         status="showed", deleted=""):
    return {"appointment_id": eid, "contact_id": contact, "booked_by": booker, "booked_by_official": booker, "advisor": advisor,
            "booked_at": booked, "start_time": start, "status": status, "deleted": deleted}


FULL, ADVICE = {"fact_find_outcome": "Full Fact Find Took Place - YES"}, {"fact_find_outcome": "Advice Given - YES"}


def test_conversion_is_full_fact_finds_over_due_appointments_per_booker():
    appts = [
        appt("1", "c1"), appt("2", "c2"), appt("3", "c3", status="noshow"), appt("4", "c4", status="cancelled"),
        appt("5", "c5", start="2026-10-30T09:00:00Z", status="confirmed"),             # still to come: not due
        appt("6", "c6", booker="Harry Kalsi"), appt("7", "c7", deleted="TRUE"),
    ]
    contacts = {"c1": FULL, "c2": ADVICE, "c6": FULL, "c4": FULL, "c7": FULL}
    table = {r["person"]: r for r in sm.build(appts, contacts, NOW)}
    hugo = table["Hugo Sharpe"]
    assert (hugo["booked"], hugo["cancelled"], hugo["no_show"], hugo["showed"], hugo["upcoming"], hugo["due"]) == (5, 1, 1, 2, 1, 3)
    assert hugo["full_fact_finds"] == 1 and hugo["conversion"] == pytest.approx(1 / 3)
    assert hugo["show_rate"] == pytest.approx(2 / 3) and hugo["ff_per_show"] == 0.5
    assert table["Harry Kalsi"]["conversion"] == 1.0
    assert sum(r["booked"] for r in table.values()) == 6                               # the deleted one never counts


def test_a_contacts_single_outcome_is_credited_once_to_its_latest_due_appointment():
    appts = [appt("1", "c1", booker="Harry Kalsi", booked="2026-10-01T09:00:00Z", status="noshow"),
             appt("2", "c1", booker="Hugo Sharpe", booked="2026-10-06T09:00:00Z")]
    table = {r["person"]: r for r in sm.build(appts, {"c1": FULL}, NOW)}
    assert table["Hugo Sharpe"]["full_fact_finds"] == 1 and table["Harry Kalsi"]["full_fact_finds"] == 0


def test_period_filters_by_the_booking_date_and_empty_denominators_give_none():
    appts = [appt("1", "c1", booked="2026-09-20T09:00:00Z"), appt("2", "c2", booked="2026-10-05T09:00:00Z", status="cancelled")]
    row = sm.build(appts, {"c1": FULL}, NOW, start=datetime(2026, 10, 1, tzinfo=timezone.utc), end=datetime(2026, 11, 1, tzinfo=timezone.utc))[0]
    assert (row["booked"], row["due"], row["conversion"], row["show_rate"], row["ff_per_show"]) == (1, 0, None, None, None)


def test_the_same_table_by_advisor_and_unknown_people():
    appts = [appt("1", "c1", advisor="Joe Nicholls"), appt("2", "c2", advisor="Xavier Collin"), appt("3", "c3", advisor="")]
    table = {r["person"]: r for r in sm.build(appts, {}, NOW, group_by="advisor")}
    assert set(table) == {"Joe Nicholls", "Xavier Collin", "(unknown)"}


@pytest.mark.parametrize("text,expected", [("Full Fact Find Took Place - YES", True), ("Full Fact Find took place: Yes", True), ("Advice Given - YES", False),
                                           ("Review - YES", False), ("Annual Review - YES", False), ("Amendment - YES", False), ("", False), (None, False)])
def test_only_full_fact_find_outcomes_count(text, expected):
    assert sm.is_full_fact_find(text) is expected


def test_advisor_distribution_counts_appointments_per_advisor_and_reports_non_advisors():
    appts = [appt(str(i), f"c{i}", advisor="Joe Nicholls") for i in range(6)] + [appt("x", "cx", advisor="Xavier Collin"), appt("y", "cy", advisor="Anita Andrews")]
    table = {r["person"]: r for r in sm.advisor_distribution(appts, ["Joe Nicholls", "Xavier Collin", "Cecilia Szabo"])}
    assert table["Joe Nicholls"]["appointments"] == 6 and table["Joe Nicholls"]["share"] == pytest.approx(6 / 7) and "vs_equal" not in table["Joe Nicholls"]
    assert table["Cecilia Szabo"]["appointments"] == 0 and table["(not a listed advisor)"]["appointments"] == 1


def test_completion_conversion_credits_each_contact_once_and_only_after_the_appointment_was_booked():
    appts = [appt("1", "c1"), appt("2", "c2"), appt("3", "c3"), appt("4", "c4", booker="Harry Kalsi")]
    contacts = {
        "c1": {"fact_find_outcome": "Full Fact Find Took Place - YES", "completed": True, "completed_at": "2026-10-15"},
        "c2": {"completed": True, "completed_at": "2026-09-01"},                    # completed BEFORE the appointment was booked: older business
        "c3": {"completed": True, "completed_at": ""},                             # completed, date unknown: counts
        "c4": {"fact_find_outcome": "Full Fact Find Took Place - YES", "completed": False},
    }
    table = {r["person"]: r for r in sm.build(appts, contacts, NOW, sales_team=["Harry Kalsi", "Ava Morris-Boundy", "Hugo Sharpe"])}
    hugo, harry = table["Hugo Sharpe"], table["Harry Kalsi"]
    assert (hugo["due"], hugo["full_fact_finds"], hugo["completed"]) == (3, 1, 2)
    assert hugo["conversion_to_completion"] == pytest.approx(2 / 3) and hugo["completion_per_full_ff"] == 2.0
    assert (harry["completed"], harry["conversion_to_completion"], harry["completion_per_full_ff"]) == (0, 0.0, 0.0)


def test_a_completed_contact_is_credited_to_one_appointment_only():
    appts = [appt("1", "c1", booker="Harry Kalsi", booked="2026-10-01T09:00:00Z", status="noshow"),
             appt("2", "c1", booker="Hugo Sharpe", booked="2026-10-06T09:00:00Z")]
    contacts = {"c1": {"completed": True, "completed_at": "2026-10-20"}}
    table = {r["person"]: r for r in sm.build(appts, contacts, NOW)}
    assert table["Hugo Sharpe"]["completed"] == 1 and table["Harry Kalsi"]["completed"] == 0


def test_sales_team_rows_are_marked_and_others_are_not():
    appts = [appt("1", "c1", booker="Hugo Sharpe"), appt("2", "c2", booker="Sarah Bennett")]
    table = {r["person"]: r["is_sales_team"] for r in sm.build(appts, {}, NOW, sales_team=["Harry Kalsi", "Ava Morris-Boundy", "Hugo Sharpe"])}
    assert table == {"Hugo Sharpe": True, "Sarah Bennett": False}


def test_the_booker_view_uses_the_official_booker_not_the_calendar_name():
    a = appt("1", "c1", booker="Ava Morris-Boundy")
    a["booked_by"] = "Someone From The Calendar"
    assert sm.build([a], {}, NOW)[0]["person"] == "Ava Morris-Boundy"
    assert sm.build([{**a, "booked_by_official": ""}], {}, NOW)[0]["person"] == "(unknown)"


def test_a_cancelled_or_no_show_appointment_is_rebooked_when_the_contact_has_a_later_booking():
    appts = [
        appt("1", "c1", status="noshow", booked="2026-10-01T09:00:00Z"), appt("2", "c1", booked="2026-10-06T09:00:00Z"),   # no-show then rebooked
        appt("3", "c2", status="cancelled", booked="2026-10-02T09:00:00Z"),                                                # cancelled, never rebooked
        appt("4", "c3", status="cancelled", booked="2026-10-03T09:00:00Z"), appt("5", "c3", booked="2026-10-07T09:00:00Z", deleted="TRUE"),
        appt("6", "c4", booked="2026-10-03T09:00:00Z"), appt("7", "c4", booked="2026-10-08T09:00:00Z", status="noshow"),   # the LATER one is the no-show
    ]
    row = sm.build(appts, {}, NOW)[0]
    assert (row["no_show"], row["cancelled"], row["rebooked"]) == (2, 2, 1)


def test_only_appointments_still_on_count_as_upcoming():
    appts = [appt("1", "c1", start="2026-10-30T09:00:00Z", status="confirmed"), appt("2", "c2", start="2026-10-30T09:00:00Z", status="cancelled"),
             appt("3", "c3", start="2026-10-30T09:00:00Z", status="noshow")]
    row = sm.build(appts, {}, NOW)[0]
    assert (row["upcoming"], row["cancelled"], row["no_show"], row["due"]) == (1, 1, 1, 1)


def test_regression_an_appointment_with_a_blank_booking_time_never_counts_as_the_latest():
    """QA-F2 (found by differential testing): the JavaScript port sorted a blank booked_at LAST, the Python spec first."""
    appts = [appt("1", "c1", booker="Harry Kalsi", booked=""), appt("2", "c1", booker="Hugo Sharpe", booked="2026-10-06T09:00:00Z")]
    table = {r["person"]: r for r in sm.build(appts, {"c1": FULL}, NOW)}
    assert table["Hugo Sharpe"]["full_fact_finds"] == 1 and "Harry Kalsi" not in table      # a blank booking time is outside every period


def test_the_period_can_apply_to_the_appointment_date_instead_of_the_booked_date():
    from datetime import datetime, timezone
    a = {"appointment_id": "a", "contact_id": "c1", "booked_by_official": "Hugo Sharpe", "advisor": "Joe Nicholls", "status": "showed",
         "booked_at": "2026-09-28T09:00:00Z", "start_time": "2026-10-02T09:00:00Z", "deleted": ""}
    b = dict(a, appointment_id="b", contact_id="c2", status="confirmed", booked_at="2026-10-05T09:00:00Z", start_time="2026-11-03T09:00:00Z")
    start, end = datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)
    now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    booked = sm.build([a, b], {}, now, start, end)[0]
    held = sm.build([a, b], {}, now, start, end, date_field="start_time")[0]
    assert (booked["booked"], booked["upcoming"]) == (1, 1)            # only b was booked in October, and it is still to come
    assert (held["booked"], held["showed"], held["upcoming"]) == (1, 1, 0)   # only a was held in October
    fair = lambda f: next(r for r in sm.advisor_distribution([a, b], ["Joe Nicholls"], start, end, date_field=f) if r["person"] == "Joe Nicholls")["appointments"]
    assert (fair("booked_at"), fair("start_time")) == (1, 1)
