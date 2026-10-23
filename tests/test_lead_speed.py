"""Speed to lead: office-hours clock, first call and first connected call, which leads count, and the runner step."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import audit_runner as runner
from app import call_evidence as ce
from app import lead_speed as ls
from app.audit_store import AuditStores
from app.writeback_rules import CallInfo
from tests.test_audit import NOW, FakeGHL, FakeSheet, cfg
from tests.test_call_evidence import PHONE, contact_with_phone, evonex_rows

UTC = timezone.utc
# Thursday 22 Oct 2026, 10:00 London (BST = UTC+1) is 09:00 UTC. The clocks go back on Sunday 25 Oct.
THU_10 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
COVER = datetime(2026, 10, 16, 7, 0, tzinfo=UTC)


def call(cid, caller="Hugo Sharpe", at=THU_10, talk=None, phone="07700900123"):
    info = CallInfo(cid=cid, rpi=phone, owner_name=caller, ring_ts=at)
    if talk is None:
        info.cancel_ts = at + timedelta(seconds=20)
    else:
        info.start_ts = at + timedelta(seconds=10)
        info.end_ts = info.start_ts + timedelta(seconds=talk)
    return info


def lead(created=THU_10, oid="o1", cid="c1", source="Website"):
    return {"opportunity_id": oid, "contact_id": cid, "created_at": created, "source": source}


def judge(leads, calls=(), phones=None, existing=None, now=None, cover=COVER, **kw):
    by_phone = ce.calls_by_phone({c.cid: c for c in calls})
    return ls.build_lead_speed(leads, {"c1": ls.to_e164(PHONE)} if phones is None else phones, by_phone, existing or {},
                               now or THU_10 + timedelta(hours=5), cover, **kw)


# ---------------------------------------------------------------- the office-hours clock

def london(y, m, d, h, mi=0):
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, h, mi, tzinfo=ZoneInfo("Europe/London")).astimezone(UTC)


def test_office_hours_are_monday_to_friday_eight_inclusive_to_six_exclusive_in_london_time():
    assert ls.in_office_hours(london(2026, 10, 22, 8, 0)) and ls.in_office_hours(london(2026, 10, 22, 17, 59))
    assert not ls.in_office_hours(london(2026, 10, 22, 7, 59)) and not ls.in_office_hours(london(2026, 10, 22, 18, 0))
    assert not ls.in_office_hours(london(2026, 10, 24, 12)) and not ls.in_office_hours(london(2026, 10, 25, 12))      # Saturday, Sunday
    assert ls.in_office_hours(london(2026, 10, 22, 12), office_start=9, office_end=13) and not ls.in_office_hours(london(2026, 10, 22, 13), office_end=13)


def test_the_clock_starts_at_the_next_opening_time():
    assert ls.clock_start(london(2026, 10, 22, 10)) == london(2026, 10, 22, 10)                 # open: starts now
    assert ls.clock_start(london(2026, 10, 22, 6, 30)) == london(2026, 10, 22, 8)               # early: opens later today
    assert ls.clock_start(london(2026, 10, 22, 22)) == london(2026, 10, 23, 8)                  # evening: tomorrow morning
    assert ls.clock_start(london(2026, 10, 23, 19)) == london(2026, 10, 26, 8)                  # Friday evening: Monday
    assert ls.clock_start(london(2026, 10, 24, 12)) == london(2026, 10, 26, 8)                  # Saturday: Monday
    assert ls.clock_start(london(2026, 10, 25, 12)) == london(2026, 10, 26, 8)                  # Sunday: Monday
    assert ls.clock_start(london(2026, 10, 26, 8)) == london(2026, 10, 26, 8)
    assert ls.clock_start(london(2026, 10, 24, 12)) == datetime(2026, 10, 26, 8, 0, tzinfo=UTC)  # Monday 08:00 GMT after the clocks went back


# ---------------------------------------------------------------- minutes to the first call

def test_minutes_to_first_call_and_first_connect_with_the_callers():
    calls = [call("a", "Ava Mae Morris-Boundy", THU_10 + timedelta(minutes=2)), call("b", "Hugo Sharpe", THU_10 + timedelta(minutes=10), talk=300)]
    row = judge([lead()], calls)[0]
    assert row["status"] == "called" and row["office_hours"] == "TRUE"
    assert (row["first_call_by"], row["minutes_to_first_call"]) == ("Ava Mae Morris-Boundy", 2.0)
    assert (row["first_connect_by"], row["minutes_to_first_connect"]) == ("Hugo Sharpe", 10.2)        # rings at +10:00, connects 10 s later: 10.17 minutes
    assert row["first_call_at"] == "2026-10-22T09:02:00Z" and row["clock_starts_at"] == "2026-10-22T09:00:00Z"
    assert PHONE not in str(row.values()) and "7700" not in str(row.values())


def test_an_out_of_hours_lead_is_timed_from_when_the_office_opened_and_an_early_call_counts_as_immediate():
    evening = london(2026, 10, 22, 22)
    called = judge([lead(created=evening)], [call("a", at=london(2026, 10, 23, 8, 20), talk=100)], now=london(2026, 10, 23, 12))[0]
    assert called["office_hours"] == "FALSE" and called["minutes_to_first_call"] == 20.0
    assert called["clock_starts_at"] == "2026-10-23T07:00:00Z"
    early = judge([lead(created=london(2026, 10, 23, 6))], [call("a", at=london(2026, 10, 23, 6, 30))], now=london(2026, 10, 23, 12))[0]
    assert early["minutes_to_first_call"] == 0.0


def test_calls_before_the_lead_existed_are_ignored():
    row = judge([lead()], [call("old", at=THU_10 - timedelta(minutes=1), talk=500), call("new", at=THU_10 + timedelta(minutes=30))])[0]
    assert row["first_call_at"] == "2026-10-22T09:30:00Z" and row["minutes_to_first_call"] == 30.0 and row["first_connect_at"] == ""


def test_a_lead_with_no_call_is_waiting_then_not_called_and_one_without_a_phone_is_marked():
    assert judge([lead()], [], now=THU_10 + timedelta(minutes=59))[0]["status"] == "waiting"
    assert judge([lead()], [], now=THU_10 + timedelta(minutes=60))[0]["status"] == "not_called"
    assert judge([lead(created=london(2026, 10, 22, 22))], [], now=london(2026, 10, 23, 8, 30))[0]["status"] == "waiting"      # the clock started at 08:00
    assert judge([lead()], [call("a", talk=100)], phones={})[0]["status"] == "no_phone"
    assert judge([lead(cid="other")], [call("a", talk=100)])[0]["status"] == "no_phone"


def test_only_calls_to_this_leads_number_count():
    assert judge([lead()], [call("a", at=THU_10 + timedelta(minutes=3), phone="07700900999", talk=100)])[0]["status"] == "not_called"


# ---------------------------------------------------------------- which leads are judged

def test_existing_clients_leads_before_call_tracking_and_old_or_future_leads_are_not_judged():
    now = THU_10 + timedelta(hours=5)
    assert judge([lead(source="Existing Client")], now=now) == [] and judge([lead(source=" existing client ")], now=now) == []
    assert judge([lead(created=COVER - timedelta(minutes=1))], now=now) == []
    assert len(judge([lead(created=COVER)], now=now)) == 1
    assert judge([lead(created=THU_10 - timedelta(days=ls.LOOKBACK_DAYS, seconds=-1))], now=THU_10, cover=COVER - timedelta(days=60)) != []
    assert judge([lead(created=THU_10 - timedelta(days=ls.LOOKBACK_DAYS + 1))], now=THU_10, cover=COVER - timedelta(days=60)) == []
    assert judge([lead(created=now + timedelta(minutes=1))], now=now) == []
    assert judge([lead()], now=now, cover=None) == []


def test_a_connected_lead_is_final_and_an_unanswered_one_stops_being_checked_after_two_weeks():
    done = {"o1": {"first_connect_at": "2026-10-22T09:05:00Z"}}
    assert judge([lead()], [call("a", talk=100)], existing=done) == []
    waiting = {"o1": {"first_connect_at": ""}}
    assert len(judge([lead()], [call("a", talk=100)], existing=waiting)) == 1                      # still open: may be re-judged
    late = THU_10 + timedelta(days=ls.FREEZE_DAYS, minutes=1)
    assert judge([lead()], existing=waiting, now=late) == []
    assert len(judge([lead()], existing={}, now=late)) == 1                                       # never stored: judged once


def test_the_office_hours_are_configurable():
    created = london(2026, 10, 22, 7, 30)
    assert judge([lead(created=created)], now=london(2026, 10, 22, 12), office_start=7)[0]["office_hours"] == "TRUE"
    assert judge([lead(created=created)], now=london(2026, 10, 22, 12))[0]["office_hours"] == "FALSE"


# ---------------------------------------------------------------- the runner step

def opportunity(oid, cid, created, **kw):
    return {"id": oid, "contactId": cid, "createdAt": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"), **kw}


class SpeedGHL(FakeGHL):
    def __init__(self, opportunities, contacts):
        super().__init__([], contacts)
        self.opportunities = opportunities

    async def get_all_opportunities(self):
        self.calls.append(("opps",))
        return list(self.opportunities)


def leads_tab(stores, rows):
    ws = stores.sheet.add_worksheet("Leads", 10, 3)
    ws.update(values=[["opportunity_id", "lead_source"]] + rows, range_name="A1")


def run_step(ghl, stores, rows, now, coverage=None):
    calls = __import__("app.writeback_rules", fromlist=["collapse_events"]).collapse_events(rows)
    return asyncio.run(runner.lead_speed_step(ghl, stores, calls, coverage or COVER, now, cfg()))


def test_the_runner_step_writes_lead_speed_skips_existing_clients_and_throttles_the_opportunity_fetch():
    created = THU_10
    ghl = SpeedGHL([opportunity("o1", "c1", created), opportunity("o2", "c2", created), opportunity("o3", "c3", created - timedelta(days=3))],
                   {"c1": contact_with_phone("c1"), "c2": contact_with_phone("c2", "07700900456")})
    stores = AuditStores(sheet=FakeSheet())
    leads_tab(stores, [["o1", "Website"], ["o2", "Existing Client"], ["o3", "Veriform"]])
    rows = evonex_rows(("k", "Hugo Sharpe", 0, 120, "07700900123"))                                # START of test_call_evidence is 21 Oct 15:00
    # shift: use the helper's START-based times, so create the lead just before that call
    created = datetime(2026, 10, 21, 14, 50, tzinfo=UTC)
    ghl.opportunities = [opportunity("o1", "c1", created), opportunity("o2", "c2", created)]
    now = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)
    out = run_step(ghl, stores, rows, now, coverage=datetime(2026, 10, 16, 7, tzinfo=UTC))
    assert out["leads"] == 1 and out["inserted"] == 1
    row = stores.speed.read()[0]
    assert (row["opportunity_id"], row["status"], row["first_call_by"], row["first_connect_by"]) == ("o1", "called", "Hugo Sharpe", "Hugo Sharpe")
    assert float(row["minutes_to_first_call"]) == 10.0
    assert "7700" not in str(stores.sheet.tabs["Lead_Speed"].values)
    assert run_step(ghl, stores, rows, now + timedelta(minutes=30)) == {"skipped": "ran recently"}
    assert len([c for c in ghl.calls if c == ("opps",)]) == 1
    assert run_step(ghl, stores, rows, now + timedelta(minutes=51))["leads"] == 0                       # the answered lead is final: nothing left to judge


def test_the_runner_step_survives_an_empty_opportunity_list_and_missing_leads_tab():
    stores = AuditStores(sheet=FakeSheet())
    assert run_step(SpeedGHL([], {}), stores, [], THU_10) == {"error": "no_opportunities"}
    ghl = SpeedGHL([opportunity("o1", "c1", THU_10)], {"c1": contact_with_phone("c1")})
    assert run_step(ghl, stores, [], THU_10 + timedelta(hours=3))["leads"] == 1                          # no Leads tab: the lead still counts
