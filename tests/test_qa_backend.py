"""QA pass (22 Oct 2026): risk-based tests added after a coverage and code review of the write-back and
audit modules. Covers the GHL client methods added for the audit, the real Sheets store of the write-back,
the failure paths that were untested, and the isolation of the audit from the proven sync."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import gspread
import httpx
import pytest
from fastapi.testclient import TestClient

from api import evonex as evonex_api
from api import sync as sync_api
from app import audit_runner, config, evonex, ghl_client
from app import writeback as wb
from app import writeback_store as wstore
from app.writeback_ghl import GHLError, GHLWriteback, pick_contact
from app.writeback_rules import CallInfo, collapse_events, decide, field_value_matches, parse_ts
from tests import test_writeback as tw
from tests.test_incremental import run, wired  # noqa: F401  (wired is a fixture)

REAL_ASYNC_CLIENT = httpx.AsyncClient     # captured once: patching twice in one test must not stack


def use_mock_ghl(monkeypatch, handler):
    """Route every GHLClient request to `handler`; safe to call again inside the same test."""
    monkeypatch.setattr(
        ghl_client.httpx, "AsyncClient",
        lambda **kw: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), timeout=kw.get("timeout")),
    )
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr(ghl_client.asyncio, "sleep", fake_sleep)
    return sleeps

# ---------------------------------------------------------------- GHL client: methods added for the audit

START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_calendar_events_are_fetched_in_31_day_windows_with_the_calendar_api_version(monkeypatch):
    seen = []

    def handler(request):
        p = request.url.params
        seen.append((request.headers["version"], p["locationId"], p["calendarId"], int(p["startTime"]), int(p["endTime"])))
        return httpx.Response(200, json={"events": [{"id": "shared"}, {"id": f"e{len(seen)}"}]})

    use_mock_ghl(monkeypatch, handler)
    events = run(ghl_client.GHLClient().get_calendar_events("CAL", START, START + timedelta(days=70)))
    assert len(seen) == 3 and {s[0] for s in seen} == {"2021-04-15"}
    assert seen[0][2] == "CAL" and seen[0][3] == int(START.timestamp() * 1000)
    assert seen[0][4] == seen[1][3] and seen[1][4] == seen[2][3]                       # contiguous windows, no gaps
    assert seen[2][4] == int((START + timedelta(days=70)).timestamp() * 1000)
    assert sorted(e["id"] for e in events) == ["e1", "e2", "e3", "shared"]               # the repeated id is de-duplicated


def test_calendar_events_return_none_if_any_window_fails(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"events": [{"id": "a"}]}) if len(calls) == 1 else httpx.Response(500, json={})

    use_mock_ghl(monkeypatch, handler)
    assert run(ghl_client.GHLClient().get_calendar_events("CAL", START, START + timedelta(days=70))) is None


def test_users_are_returned_as_an_id_to_name_map_and_none_on_failure(monkeypatch):
    paths = []

    def handler(request):
        paths.append((request.url.path, request.url.params["locationId"]))
        return httpx.Response(200, json={"users": [{"id": "u1", "name": "Hugo Sharpe"}, {"id": "u2"}, {"name": "no id"}]})

    use_mock_ghl(monkeypatch, handler)
    assert run(ghl_client.GHLClient().get_users()) == {"u1": "Hugo Sharpe", "u2": ""}
    assert paths == [("/users/", config.GHL_LOCATION_ID)]                                # the trailing slash matters to GHL
    use_mock_ghl(monkeypatch, lambda request: httpx.Response(401, json={}))
    assert run(ghl_client.GHLClient().get_users()) is None


def test_field_search_sends_the_exists_filter_and_paginates(monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        n = 100 if body["page"] == 1 else 4
        return httpx.Response(200, json={"contacts": [{"id": f"p{body['page']}-{i}"} for i in range(n)]})

    use_mock_ghl(monkeypatch, handler)
    found = run(ghl_client.GHLClient().search_contacts_with_field("FIELD"))
    assert len(found) == 104 and len(seen) == 2
    assert seen[0]["filters"] == [{"field": "customFields.FIELD", "operator": "exists"}] and seen[0]["pageLimit"] == 100


def test_field_search_is_incomplete_when_it_hits_the_page_cap_or_fails(monkeypatch):
    use_mock_ghl(monkeypatch, lambda request: httpx.Response(200, json={"contacts": [{"id": request.url.path + str(json.loads(request.content)["page"]) + str(i)} for i in range(100)]}))
    assert run(ghl_client.GHLClient().search_contacts_with_field("F")) is None
    use_mock_ghl(monkeypatch, lambda request: httpx.Response(500, json={}))
    assert run(ghl_client.GHLClient().search_contacts_with_field("F")) is None


def test_get_json_honours_retry_after_then_gives_up(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(429, headers={"Retry-After": "3"}, json={}) if len(attempts) == 1 else httpx.Response(200, json={"users": []})

    sleeps = use_mock_ghl(monkeypatch, handler)
    assert run(ghl_client.GHLClient().get_users()) == {} and sleeps == [3.0]
    use_mock_ghl(monkeypatch, lambda request: httpx.Response(503, json={}))
    assert run(ghl_client.GHLClient().get_users()) is None


# ---------------------------------------------------------------- write-back GHL client: untested paths

@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("app.writeback_ghl.time.sleep", lambda s: None)


def client_of(handler):
    return GHLWriteback(httpx.Client(transport=httpx.MockTransport(handler)))


def test_open_opportunity_lookup_uses_the_documented_snake_case_parameters(no_sleep):
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"opportunities": [{"id": "o1"}] if len(seen) == 1 else []})

    ghl = client_of(handler)
    assert ghl.has_open_mortgage_opportunity("C1") is True and ghl.has_open_mortgage_opportunity("C2") is False
    path, params = seen[0]
    assert path == "/opportunities/search"
    assert params == {"location_id": config.GHL_LOCATION_ID, "contact_id": "C1", "pipeline_id": "EQyj0fwCvT8vrLEGTw5I", "status": "open", "limit": "1"}


def test_network_errors_are_retried_then_reported(no_sleep):
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"contact": {"customFields": []}})

    assert client_of(flaky).get_contact_values("C1") == {} and len(calls) == 3

    def dead(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(GHLError) as err:
        client_of(dead).get_contact_values("C1")
    assert str(err.value) == "ReadTimeout"


def test_a_malformed_retry_after_header_does_not_crash(no_sleep):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "soon"}, json={}) if len(calls) == 1 else httpx.Response(200, json={"contact": {}})

    assert client_of(handler).get_contact_values("C1") == {}


def test_pick_contact_survives_lookup_errors_on_individual_candidates():
    class Flaky:
        def has_open_mortgage_opportunity(self, cid):
            if cid == "A":
                raise GHLError("http_500", 500)
            return cid == "B"

        def get_contact_values(self, cid):
            raise GHLError("http_500", 500)

    a, b = {"id": "A", "dateUpdated": "2026-10-20"}, {"id": "B", "dateUpdated": "2026-09-01"}
    assert pick_contact(Flaky(), [a, b]) == (b, 2, None)                       # A's lookup failed: B still wins
    both = type("Both", (Flaky,), {"has_open_mortgage_opportunity": lambda self, cid: True})()
    assert pick_contact(both, [a, b]) == (None, 2, "ambiguous_contact")        # cannot read phases: refuse to guess


# ---------------------------------------------------------------- write-back orchestrator: failure paths

def test_an_unexpected_exception_closes_the_claim_and_is_retryable():
    wbk, store, ghl, _ = tw.make(tw.call_events("c1", cancel=11))

    def boom(contact_id):
        raise RuntimeError("something nobody planned for")

    ghl.get_contact_values = boom
    assert wbk.process_cid("c1") == "failed"
    assert store.statuses("c1") == ["claimed", "failed"] and "exception_RuntimeError" in store.log[-1]["reason"]
    assert wbk._active_claims(store.recent_log()) == []                        # no dangling claim blocking the contact
    assert wbk._cid_state("c1", store.recent_log()) == "open"                  # and the sweeper may try again


def test_a_failed_read_back_is_recorded_as_unverified_and_not_resent():
    wbk, store, ghl, _ = tw.make(tw.call_events("c1", cancel=11))
    reads = []
    real = ghl.get_contact_values

    def flaky_reads(contact_id):
        reads.append(1)
        if len(reads) >= 2:                                                    # the first read is the fresh pre-write read
            raise GHLError("http_500", 500)
        return real(contact_id)

    ghl.get_contact_values = flaky_reads
    assert wbk.process_cid("c1") == "written_unverified" and len(ghl.puts) == 1
    assert "verify_read_failed" in store.log[-1]["reason"]


def test_an_unknown_cid_is_a_no_op():
    wbk, store, ghl, _ = tw.make([])
    assert wbk.process_cid("does-not-exist") == "no_events" and store.log == [] and ghl.puts == []


def test_a_call_with_no_usable_timestamps_is_never_written():
    d = decide(CallInfo(cid="x"), "No Answer", {"y1Bjm1Afle54l8kmZ4uu": "Admin Call"}, [], False)
    assert d.write is False and d.reason == "no_timestamp"


def test_a_sweep_with_no_time_budget_processes_nothing():
    wbk, store, ghl, _ = tw.make(tw.call_events("c1", cancel=11))
    out = wbk.sweep(budget_seconds=-1)
    assert out["candidates"] == 1 and out["processed"] == {} and ghl.puts == []


def test_parsers_reject_garbage_without_raising():
    assert parse_ts("not a date") is None and parse_ts(None) is None and parse_ts("") is None
    assert field_value_matches("2026-10-22", 10 ** 30) is False                # absurd epoch: no OverflowError
    assert field_value_matches("2026-10-22", "x" * 5) is False


def test_collapse_ignores_rows_with_unparseable_timestamps():
    rows = [{"event": "OUTBOUNDCALL", "subevent": "RINGCALL", "ts": "garbage", "cid": "c1", "dedup_key": "k1"}]
    info = collapse_events(rows)["c1"]
    assert info.ring_ts is None and info.event_count == 1


# ---------------------------------------------------------------- the write-back's real Sheets store

class SheetWS:
    """A worksheet that behaves like Google's: column reads and range reads trim trailing empty cells."""

    def __init__(self, rows=None):
        self.rows = [list(r) for r in (rows or [])]
        self.appended = []

    def col_values(self, n):
        col = [r[n - 1] if len(r) >= n else "" for r in self.rows]
        while col and col[-1] == "":
            col.pop()
        return col

    def get(self, rng, **kw):
        first, last = (int("".join(ch for ch in part if ch.isdigit())) for part in rng.split(":"))
        return [list(r) for r in self.rows[first - 1:last]]

    def row_values(self, n):
        return list(self.rows[0]) if self.rows else []

    def update(self, values=None, range_name=None, **kw):
        self.rows = [list(r) for r in values] + self.rows[1:]

    def append_row(self, values, **kw):
        self.rows.append(list(values))
        self.appended.append(kw)


class SheetBook:
    def __init__(self, tabs):
        self.tabs = tabs

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = SheetWS()
        return self.tabs[title]


def real_store(events_rows, log_rows=None):
    store = object.__new__(wstore.WritebackStore)
    tabs = {evonex.TAB: SheetWS([evonex.COLUMNS] + events_rows)}
    if log_rows is not None:
        tabs[wstore.LOG_TAB] = SheetWS([wstore.LOG_COLUMNS] + log_rows)
    store._sheet, store._events, store._log = SheetBook(tabs), None, None
    return store


def event_row(i, cid="c1", sub="RINGCALL", rpi="07700900123"):
    values = dict.fromkeys(evonex.COLUMNS, "")
    values.update(received_at_utc="2026-10-22T10:00:00+00:00", event="OUTBOUNDCALL", subevent=sub, ts="2026-10-22T10:00:0%dZ" % (i % 10),
                  cid=cid, dedup_key=f"k{i}", rpi=rpi, on="Hugo Sharpe", oe="h@x.y")
    row = [values[c] for c in evonex.COLUMNS]
    while row and row[-1] == "":
        row.pop()                                                              # Sheets trims trailing empty cells
    return row


def test_recent_events_reads_only_the_tail_and_maps_columns_by_name():
    store = real_store([event_row(i, cid=f"c{i}") for i in range(10)])
    rows = store.recent_events(window=3)
    assert [r["cid"] for r in rows] == ["c7", "c8", "c9"]
    assert rows[0]["rpi"] == "07700900123" and rows[0]["on"] == "Hugo Sharpe" and rows[0]["oe"] == "h@x.y"
    assert set(rows[0]) == set(evonex.COLUMNS[:26])
    assert len(store.recent_events(window=1500)) == 10 and real_store([]).recent_events() == []


def test_recent_events_turns_numeric_cells_into_text():
    row = event_row(1)
    row[evonex.COLUMNS.index("rpi")] = 7700900123                              # UNFORMATTED_VALUE may hand back a number
    assert real_store([row]).recent_events()[0]["rpi"] == "7700900123"


def test_the_log_tab_is_created_with_its_header_and_appends_rows():
    store = real_store([event_row(1)])
    assert store.recent_log() == []
    ws = store._sheet.tabs[wstore.LOG_TAB]
    assert ws.rows[0] == wstore.LOG_COLUMNS
    store.append_log(cid="c1", status="claimed", mode="on", claim_token="t", contact_id="C1")
    store.append_log(cid="c1", status="written", count_after=3, reason="")
    log = store.recent_log()
    assert [r["status"] for r in log] == ["claimed", "written"] and log[1]["count_after"] == "3" and log[0]["claim_token"] == "t"
    assert ws.appended[0]["value_input_option"] == "RAW"                       # ids and tokens are never parsed as formulas or dates


def test_log_rows_never_hold_customer_phone_numbers_or_names():
    store = real_store([event_row(1)])
    wbk = wb.Writeback(store, tw.FakeGHL(), tw.cfg(), now=lambda: datetime(2026, 10, 22, 10, 10, tzinfo=timezone.utc), sleep=lambda s: None)
    store.events = None
    assert wbk.process_cid("c1") in ("written", "skipped", "not_terminal", "failed", "dry_run")
    blob = json.dumps(store._sheet.tabs[wstore.LOG_TAB].rows)
    assert "07700900123" not in blob and "+447700900123" not in blob


# ---------------------------------------------------------------- audit isolation from the sync

def test_the_audit_runs_after_a_successful_sync_and_its_failure_cannot_fail_the_sync(wired, monkeypatch):  # noqa: F811
    calls = []

    async def audit(ghl):
        calls.append(ghl)
        raise RuntimeError("audit bug")                                        # run_safely is supposed to never raise: prove the sync survives if it does

    monkeypatch.setattr(sync_api.audit_runner, "run_safely", audit)
    result = run(sync_api.do_full_sync(wired.sheets))
    assert result["status"] == "success" and len(calls) == 1
    assert wired.sheets.logs[-1][0] == "success"                              # never overwritten by a failure row
    assert not any(row[0] == "failed" for row in wired.sheets.logs)


def test_the_audit_does_not_run_when_the_sync_itself_failed(wired, monkeypatch):  # noqa: F811
    called = []

    async def audit(ghl):
        called.append(1)

    async def broken(self):
        raise RuntimeError("ghl down")

    monkeypatch.setattr(sync_api.audit_runner, "run_safely", audit)
    monkeypatch.setattr(wired.ghl.__class__, "get_all_opportunities", broken)
    assert run(sync_api.do_full_sync(wired.sheets))["status"] == "failed" and called == []


def test_a_failed_contact_search_in_an_incremental_audit_cycle_writes_nothing():
    from tests.test_audit import AuditStores, FakeGHL, FakeSheet, NOW, go
    ghl, stores = FakeGHL(), AuditStores(sheet=FakeSheet())
    go(ghl, stores)                                                            # first (full) run succeeds
    ghl.fail_search = True
    out = go(ghl, stores, now=NOW + timedelta(minutes=15))
    assert out == {"mode": "on", "error": "contact_search_failed"}
    assert stores.get_state(audit_runner.STATE_LAST_RUN) == "2026-10-22T12:00:00Z"   # the failed cycle did not advance the state


# ---------------------------------------------------------------- webhook: unexpected errors never leak details

def test_an_unexpected_error_in_the_webhook_is_a_bare_500_without_details(monkeypatch):
    from tests.test_evonex import BASE, outbound, send

    def explode(row):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(config, "EVONEX_WEBHOOK_TOKENS", ["dummy-evonex-token"])
    monkeypatch.setattr(config, "EVONEX_SHEET_ID", "sheet")
    monkeypatch.setattr(evonex, "write_row_with_retry", explode)
    client = TestClient(evonex_api.app, base_url=BASE, raise_server_exceptions=False)
    r = send(client, "/api/evonex/call", outbound("ENDCALL"))
    assert r.status_code == 500 and r.json() == {"error": "server_error"} and "secret" not in r.text


# ---------------------------------------------------------------- non-functional: request budget and log hygiene

class CountingStore(tw.FakeStore):
    def __init__(self, clock, events):
        super().__init__(clock, events)
        self.counts = {"events": 0, "log_reads": 0, "log_appends": 0}

    def recent_events(self, window=0):
        self.counts["events"] += 1
        return super().recent_events(window)

    def recent_log(self, window=0):
        self.counts["log_reads"] += 1
        return super().recent_log(window)

    def append_log(self, **fields):
        self.counts["log_appends"] += 1
        super().append_log(**fields)


def test_google_sheets_request_budget_per_call_stays_small():
    """Each Sheets read or append is one API request (read quota: 60 per minute per user). Two sheet round trips are
    one `recent_*` read (a column read plus a range read), so the budget counts store operations."""
    clock = tw.Clock(tw.T0 + timedelta(minutes=10))
    store = CountingStore(clock, tw.call_events("c1", cancel=11))
    wbk = wb.Writeback(store, tw.FakeGHL(), tw.cfg(), now=clock.now, sleep=clock.sleep)
    assert wbk.process_cid("c1") == "written"
    # events: 1 read; log: 1 state read + 1 verdict read; appends: claim + final row
    assert store.counts == {"events": 1, "log_reads": 2, "log_appends": 2}
    # a duplicate delivery costs one read of each kind and writes nothing
    store.counts = dict.fromkeys(store.counts, 0)
    assert wbk.process_cid("c1") == "done"
    assert store.counts == {"events": 1, "log_reads": 1, "log_appends": 0}


def test_the_write_back_logs_ids_and_reasons_only(caplog):
    caplog.set_level("DEBUG")
    wbk, store, ghl, _ = tw.make(tw.call_events("c1", cancel=11, rpi="07700900123", on="Hugo Sharpe", oe="hugo.sharpe@example.org"))
    ghl.contacts = [{"id": "C1", "dateUpdated": "2026-10-01", "phone": "+447700900123", "firstName": "Priya", "lastName": "Customer"}]
    assert wbk.process_cid("c1") == "written"
    text = " ".join(r.getMessage() for r in caplog.records)
    for secret in ("07700900123", "7700900123", "Hugo", "Sharpe", "hugo.sharpe", "Priya", "Customer"):
        assert secret not in text, secret
    assert "cid=c1" in text and "contact=C1" in text


def test_the_sweep_cron_endpoint_compares_the_secret_in_constant_time_and_rejects_empty(monkeypatch):
    monkeypatch.setattr(wb, "sweep_safely", lambda: {"mode": "on"})
    client = TestClient(evonex_api.app, raise_server_exceptions=False)
    assert client.post("/api/evonex/writeback/sweep", headers={"x-cron-secret": ""}).status_code == 401
    assert client.post("/api/evonex/writeback/sweep", headers={"x-cron-secret": config.CRON_SECRET + "x"}).status_code == 401
    assert client.get("/api/evonex/writeback/sweep").status_code in (404, 405)                # GET is not a way in
    assert client.post("/api/evonex/writeback/sweep", headers={"x-cron-secret": config.CRON_SECRET}).status_code == 200


# ---------------------------------------------------------------- regression tests for defects found by the fuzzer (QA-B5..B7)

def test_regression_phone_numbers_with_a_plus_in_the_middle_or_non_ascii_digits_are_rejected():
    from app.writeback_rules import to_e164
    assert to_e164("0770+0900123") is None and to_e164("+4477+00900123") is None          # QA-B5: '+' only valid first
    assert to_e164("\u0660\u0667\u0667\u0660\u0660\u0669\u0660\u0660\u0661\u0662\u0663") is None   # Arabic-Indic digits are not a UK number
    assert to_e164("+44 (0)7700 900123") == "+447700900123"                          # trunk zero typed after +44 (QA-B8: was +4407700900123)
    assert to_e164("07700 900123") == "+447700900123"


def test_regression_two_calls_sharing_a_dedup_key_are_never_merged():
    rows = [{"event": "OUTBOUNDCALL", "subevent": "RINGCALL", "ts": "2026-10-22T10:00:00Z", "cid": "a", "dedup_key": "same", "received_at_utc": "2026-10-22T10:00:00Z"},
            {"event": "OUTBOUNDCALL", "subevent": "RINGCALL", "ts": "2026-10-22T10:05:00Z", "cid": "b", "dedup_key": "same", "received_at_utc": "2026-10-22T10:05:00Z"}]
    assert set(collapse_events(rows)) == {"a", "b"} == set(collapse_events(list(reversed(rows))))          # QA-B6
    dup = rows + [dict(rows[0])]                                                                          # a real duplicate delivery is still dropped
    assert collapse_events(dup)["a"].event_count == 1


def test_regression_an_infinite_or_huge_counter_value_reads_as_zero():
    from app.writeback_rules import as_int
    for value in (float("inf"), float("-inf"), float("nan"), "inf", 10 ** 400):
        assert as_int(value) == 0                                                  # QA-B7: no OverflowError


# ---------------------------------------------------------------- configuration robustness (QA-B9)

@pytest.mark.parametrize("name,bad,default", [
    ("WRITEBACK_TALK_SECONDS", "60s", 60), ("WRITEBACK_TALK_SECONDS", "-5", 60), ("WRITEBACK_SETTLE_SECONDS", "five", 5.0),
    ("WRITEBACK_MAX_AGE_MINUTES", "", 120), ("BOOKED_BY_GRACE_HOURS", "nan", 24.0), ("WRITEBACK_SETTLE_SECONDS", "inf", 5.0), ("FULL_REFRESH_HOUR_UTC", "3am", 3),
])
def test_a_bad_numeric_setting_never_stops_the_app_from_starting(monkeypatch, name, bad, default):
    import importlib
    monkeypatch.setenv(name, bad)
    importlib.reload(config)
    try:
        assert getattr(config, name) == default
    finally:
        monkeypatch.delenv(name)
        importlib.reload(config)


def test_good_numeric_settings_are_honoured(monkeypatch):
    import importlib
    monkeypatch.setenv("WRITEBACK_TALK_SECONDS", "30")
    monkeypatch.setenv("BOOKED_BY_GRACE_HOURS", "2.5")
    importlib.reload(config)
    try:
        assert config.WRITEBACK_TALK_SECONDS == 30 and config.BOOKED_BY_GRACE_HOURS == 2.5
    finally:
        monkeypatch.delenv("WRITEBACK_TALK_SECONDS")
        monkeypatch.delenv("BOOKED_BY_GRACE_HOURS")
        importlib.reload(config)
