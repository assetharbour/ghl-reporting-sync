import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from api import sync as sync_api
from app import config, ghl_client, incremental, join
from app.field_mapping import CUSTOM_FIELD_IDS, PIPELINE
from app.sheets_client import SheetsClient

UTC = timezone.utc
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
LAST_SUCCESS = NOW - timedelta(minutes=15)
SINCE = LAST_SUCCESS - incremental.OVERLAP
OLD = "2026-10-01T00:00:00.000Z"      # before SINCE: an opportunity nobody touched
FRESH = "2026-10-16T11:50:00.000Z"    # after SINCE
STAGE_ID = {name: sid for sid, name in PIPELINE["stages"].items()}


def opp(oid, cid, stage="Lead Received", status="open", updated=OLD):
    return {
        "id": oid, "contactId": cid, "name": oid, "pipelineStageId": STAGE_ID[stage],
        "status": status, "assignedTo": None, "createdAt": "2026-06-10T09:00:00.000Z",
        "updatedAt": updated, "customFields": [],
    }


def contact(cid, first="Ann", admin_call=None, advisor=None):
    fields = []
    if admin_call:
        fields.append({"id": CUSTOM_FIELD_IDS["admin_call_status"], "value": admin_call})
    if advisor:
        fields.append({"id": CUSTOM_FIELD_IDS["advisor_name"], "value": advisor})
    return {"id": cid, "firstName": first, "lastName": "Lee", "assignedTo": None, "customFields": fields}


def sheet_values(pairs):
    """A Leads snapshot as the sheet would hold it after a normal sync."""
    return [list(join.COLUMNS)] + [incremental.row_values(join.build_row(o, c)) for o, c in pairs]


class FakeGHL:
    def __init__(self, opportunities, changed=None, contacts=None, complete=True):
        self.opportunities, self.changed = opportunities, changed if changed is not None else []
        self.contacts = {c["id"]: c for c in (contacts or [])}
        self.last_fetch_complete = complete
        self.search_fails = False
        self.calls = []

    async def get_all_opportunities(self):
        self.calls.append("opps")
        return self.opportunities

    async def search_contacts_updated_since(self, since):
        self.calls.append("search")
        return None if self.search_fails else self.changed

    async def get_all_contacts(self, ids):
        self.calls.append(("contacts", sorted(ids)))
        return {i: self.contacts[i] for i in ids if i in self.contacts}


class FakeSheets:
    def __init__(self, values, last_success=LAST_SUCCESS):
        self.values, self.last_success = values, last_success
        self.reads, self.upserts, self.stale, self.logs = 0, [], [], []

    def get_last_success_time(self):
        return self.last_success

    def read_leads_values(self):
        self.reads += 1
        return self.values

    def upsert_leads(self, rows, existing=None):
        self.upserts.append((rows, existing))
        known = {r[1] for r in (existing or [])[1:] if len(r) > 1}
        inserted = sum(1 for r in rows if r["opportunity_id"] not in known)
        return {"updated": len(rows) - inserted, "inserted": inserted, "total": len(rows)}

    def remove_stale_rows(self, ids, values=None, **kwargs):
        self.stale.append((ids, values))
        return {"removed": 0}

    def log_sync(self, status, records, error=""):
        self.logs.append((status, records, error))


def run(coro):
    return asyncio.run(coro)


def baseline():
    pairs = [(opp("o1", "c1"), contact("c1", advisor="Xavier")), (opp("o2", "c2"), contact("c2"))]
    return pairs, sheet_values(pairs)


# ---------------------------------------------------------------------- rules

@pytest.mark.parametrize(
    "last_success,expected",
    [
        (None, "no previous"),
        (NOW - timedelta(hours=25), "over 24 hours"),
        (NOW.replace(hour=2, minute=55) - timedelta(hours=1), None),   # now is 12:00; 01:55 is before 03:00 but today's refresh is already past
        (NOW - timedelta(minutes=15), None),
    ],
)
def test_full_refresh_rules(last_success, expected):
    reason = incremental.needs_full_refresh(last_success, NOW, 3)
    if expected is None and last_success < NOW.replace(hour=3, minute=0):
        assert reason == "daily full refresh"  # last success predates today's 03:00 -> refresh due
    elif expected is None:
        assert reason is None
    else:
        assert expected in reason


def test_daily_refresh_happens_once():
    before = datetime(2026, 10, 16, 2, 50, tzinfo=UTC)
    after = datetime(2026, 10, 16, 3, 5, tzinfo=UTC)
    assert incremental.needs_full_refresh(before - timedelta(minutes=15), before, 3) is None
    assert incremental.needs_full_refresh(before, after, 3) == "daily full refresh"      # first run after 03:00
    assert incremental.needs_full_refresh(after, after + timedelta(minutes=15), 3) is None  # already done today


def test_canon_normalises_numbers_but_not_phones_or_padded_ids():
    assert incremental.canon("1000") == incremental.canon(1000.0) == "1000"
    assert incremental.canon("12.30") == incremental.canon(12.3)
    assert incremental.canon("+447398649862") != incremental.canon("447398649862")
    assert incremental.canon("0123") != incremental.canon("123")
    assert incremental.canon(True) == "TRUE" and incremental.canon(None) == ""


# ------------------------------------------------------------ the incremental run

def test_nothing_changed_writes_nothing_and_fetches_no_contacts():
    pairs, values = baseline()
    ghl, sheets = FakeGHL([o for o, _ in pairs]), FakeSheets(values)
    result, written = run(incremental.run(ghl, sheets, NOW))
    assert written == 0 and sheets.upserts == []
    assert result["rows_rebuilt"] == 0 and result["mode"] == "incremental"
    assert not any(isinstance(c, tuple) and c[0] == "contacts" for c in ghl.calls)
    assert sheets.reads == 1                         # one sheet read, reused below
    assert sheets.stale[0][1] is values              # stale cleanup reuses the snapshot


def test_contact_only_change_uses_search_result_with_no_extra_call():
    pairs, values = baseline()
    ghl = FakeGHL([o for o, _ in pairs], changed=[contact("c1", advisor="Xavier", admin_call="Answered")])
    sheets = FakeSheets(values)
    result, written = run(incremental.run(ghl, sheets, NOW))
    rows, existing = sheets.upserts[0]
    assert written == 1 and rows[0]["opportunity_id"] == "o1" and rows[0]["admin_call_status"] == "Answered"
    assert existing is values
    assert result["plan_reasons"] == {"contact_changed": 1}
    assert not any(isinstance(c, tuple) for c in ghl.calls)   # no per-contact GET


def test_opportunity_only_change_fetches_that_contact():
    pairs, values = baseline()
    moved = opp("o2", "c2", stage="Docs Requested", updated=FRESH)
    ghl = FakeGHL([pairs[0][0], moved], contacts=[contact("c2")])
    sheets = FakeSheets(values)
    result, _ = run(incremental.run(ghl, sheets, NOW))
    assert ("contacts", ["c2"]) in ghl.calls
    assert sheets.upserts[0][0][0]["pipeline_stage"] == "Docs Requested"
    assert result["contacts_fetched"] == 1


def test_stage_change_is_caught_even_if_updatedat_did_not_move():
    pairs, values = baseline()
    moved = opp("o2", "c2", stage="Offer Issued", updated=OLD)    # updatedAt stale, stage differs
    ghl = FakeGHL([pairs[0][0], moved], contacts=[contact("c2")])
    sheets = FakeSheets(values)
    run(incremental.run(ghl, sheets, NOW))
    assert sheets.upserts[0][0][0]["pipeline_stage"] == "Offer Issued"


def test_touched_but_identical_row_is_not_rewritten():
    pairs, values = baseline()
    touched = opp("o1", "c1", updated=FRESH)                       # updatedAt moved, nothing else
    ghl = FakeGHL([touched, pairs[1][0]], changed=[contact("c1", advisor="Xavier")])  # contact re-saved, same data
    sheets = FakeSheets(values)
    result, written = run(incremental.run(ghl, sheets, NOW))
    assert result["rows_rebuilt"] == 1 and written == 0 and sheets.upserts == []


def test_new_opportunity_is_inserted():
    pairs, values = baseline()
    brand_new = opp("o3", "c3", updated=FRESH)
    ghl = FakeGHL([*(o for o, _ in pairs), brand_new], changed=[contact("c3", first="Zed")])
    sheets = FakeSheets(values)
    result, written = run(incremental.run(ghl, sheets, NOW))
    assert written == 1 and result["rows_inserted"] == 1 and result["plan_reasons"] == {"new": 1}


def test_planned_opportunity_with_missing_contact_is_skipped_not_fatal():
    pairs, values = baseline()
    moved = opp("o2", "c2", stage="Exchange", updated=FRESH)
    ghl = FakeGHL([pairs[0][0], moved], contacts=[])               # GHL no longer has contact c2
    sheets = FakeSheets(values)
    result, written = run(incremental.run(ghl, sheets, NOW))
    assert written == 0 and result["rows_rebuilt"] == 0


def test_failed_contact_lookup_falls_back_and_hands_back_opportunities():
    pairs, values = baseline()
    ghl, sheets = FakeGHL([o for o, _ in pairs]), FakeSheets(values)
    ghl.search_fails = True
    with pytest.raises(incremental.FallbackToFull) as caught:
        run(incremental.run(ghl, sheets, NOW))
    assert "lookup" in caught.value.reason and caught.value.opportunities == ghl.opportunities
    assert sheets.reads == 0 and sheets.upserts == []


def test_unexpected_header_falls_back():
    pairs, values = baseline()
    values[0][3] = "pipeline_stage_RENAMED"
    ghl, sheets = FakeGHL([o for o, _ in pairs]), FakeSheets(values)
    with pytest.raises(incremental.FallbackToFull):
        run(incremental.run(ghl, sheets, NOW))


def test_no_previous_success_means_full_without_touching_ghl():
    ghl, sheets = FakeGHL([]), FakeSheets([], last_success=None)
    with pytest.raises(incremental.FallbackToFull):
        run(incremental.run(ghl, sheets, NOW))
    assert ghl.calls == []


def test_stale_cleanup_skipped_when_opportunity_fetch_incomplete():
    pairs, values = baseline()
    ghl, sheets = FakeGHL([o for o, _ in pairs], complete=False), FakeSheets(values)
    run(incremental.run(ghl, sheets, NOW))
    assert sheets.stale == []


def test_search_uses_the_watermark_minus_overlap():
    pairs, values = baseline()
    seen = []

    class Recording(FakeGHL):
        async def search_contacts_updated_since(self, since):
            seen.append(since)
            return []

    run(incremental.run(Recording([o for o, _ in pairs]), FakeSheets(values), NOW))
    assert seen == [LAST_SUCCESS - timedelta(minutes=10)]


# ---------------------------------------------------------------------- shadow

@pytest.fixture
def shadow_logs(caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("incremental"), "propagate", True)
    caplog.set_level(logging.INFO, logger="incremental")
    return caplog


def full_rows(pairs):
    return join.build_all_rows([o for o, _ in pairs], {c["id"]: c for _, c in pairs})


def test_shadow_reports_zero_missed_when_every_change_is_planned(shadow_logs):
    pairs, values = baseline()
    changed_contact = contact("c1", advisor="Xavier", admin_call="Answered")
    new_pairs = [(pairs[0][0], changed_contact), pairs[1]]
    ghl, sheets = FakeGHL([o for o, _ in new_pairs], changed=[changed_contact]), FakeSheets(values)
    snapshot = run(incremental.shadow_check(ghl, sheets, [o for o, _ in new_pairs], full_rows(new_pairs)))
    assert snapshot is values
    assert "true_changes=1" in shadow_logs.text and "missed=0" in shadow_logs.text


def test_shadow_flags_a_change_ghl_did_not_report(shadow_logs):
    pairs, values = baseline()
    changed_contact = contact("c1", advisor="Xavier", admin_call="Answered")
    new_pairs = [(pairs[0][0], changed_contact), pairs[1]]
    ghl = FakeGHL([o for o, _ in new_pairs], changed=[])           # search says: nothing changed
    run(incremental.shadow_check(ghl, FakeSheets(values), [o for o, _ in new_pairs], full_rows(new_pairs)))
    assert "missed=1" in shadow_logs.text
    assert "MISSED opportunity=o1 columns=admin_call_status" in shadow_logs.text
    assert "Ann" not in shadow_logs.text                             # column names only, no values


def test_shadow_never_raises_and_still_returns_a_snapshot():
    pairs, values = baseline()
    ghl = FakeGHL([o for o, _ in pairs])

    async def boom(since):
        raise RuntimeError("ghl down")

    ghl.search_contacts_updated_since = boom
    assert run(incremental.shadow_check(ghl, FakeSheets(values), [o for o, _ in pairs], full_rows(pairs))) is values

    class Unreadable(FakeSheets):
        def read_leads_values(self):
            raise RuntimeError("sheet down")

    assert run(incremental.shadow_check(ghl, Unreadable(values), [], [])) is None


# ------------------------------------------------ do_full_sync wiring (api/sync.py)

@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_SYNC", "")
    monkeypatch.setattr(config, "FULL_REFRESH_HOUR_UTC", 3)
    monkeypatch.setattr(incremental, "datetime", type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))
    state = type("S", (), {})()
    pairs, values = baseline()
    state.pairs, state.values = pairs, values
    state.ghl = FakeGHL([o for o, _ in pairs], contacts=[c for _, c in pairs])
    state.sheets = FakeSheets(values)
    monkeypatch.setattr(sync_api.ghl_client, "GHLClient", lambda: state.ghl)
    return state


def test_default_is_the_original_full_sync(wired):
    result = run(sync_api.do_full_sync(wired.sheets))
    assert result["mode"] == "full" and result["status"] == "success"
    assert ("contacts", ["c1", "c2"]) in wired.ghl.calls and "search" not in wired.ghl.calls
    rows, existing = wired.sheets.upserts[0]
    assert len(rows) == 2 and existing is None                      # every row, own read inside upsert
    assert wired.sheets.logs[-1] == ("success", 2, "")


def test_on_mode_runs_incrementally(wired, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_SYNC", "on")
    wired.ghl.changed = [contact("c1", advisor="Xavier", admin_call="Answered")]
    result = run(sync_api.do_full_sync(wired.sheets))
    assert result["mode"] == "incremental" and result["rows_rebuilt"] == 1
    assert not any(isinstance(c, tuple) for c in wired.ghl.calls)
    assert wired.sheets.logs[-1] == ("success", 1, "")


@pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
def test_on_mode_accepts_common_truthy_values(wired, monkeypatch, value):
    monkeypatch.setattr(config, "INCREMENTAL_SYNC", value.lower())
    assert run(sync_api.do_full_sync(wired.sheets))["mode"] == "incremental"


def test_unknown_setting_value_stays_full(wired, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_SYNC", "maybe")
    assert run(sync_api.do_full_sync(wired.sheets))["mode"] == "full"


def test_fallback_runs_full_without_fetching_opportunities_twice(wired, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_SYNC", "on")
    wired.ghl.search_fails = True
    result = run(sync_api.do_full_sync(wired.sheets))
    assert result["mode"] == "full" and wired.ghl.calls.count("opps") == 1


def test_force_full_overrides_on_mode(wired, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_SYNC", "on")
    result = run(sync_api.do_full_sync(wired.sheets, force_full=True))
    assert result["mode"] == "full" and "search" not in wired.ghl.calls


def test_first_run_with_no_history_is_full(wired, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_SYNC", "on")
    wired.sheets.last_success = None
    assert run(sync_api.do_full_sync(wired.sheets))["mode"] == "full"


def test_shadow_mode_does_a_full_sync_and_reuses_one_sheet_read(wired, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_SYNC", "shadow")
    result = run(sync_api.do_full_sync(wired.sheets))
    rows, existing = wired.sheets.upserts[0]
    assert result["mode"] == "full" and len(rows) == 2
    assert existing is wired.values and wired.sheets.reads == 1


def test_failures_are_still_logged_as_failed(wired, monkeypatch):
    monkeypatch.setattr(config, "INCREMENTAL_SYNC", "on")

    async def boom():
        raise RuntimeError("ghl exploded")

    wired.ghl.get_all_opportunities = boom
    result = run(sync_api.do_full_sync(wired.sheets))
    assert result["status"] == "failed" and wired.sheets.logs[-1][0] == "failed"


# ------------------------------------------------------ GHL contact search call

def use_mock_ghl(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        ghl_client.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), timeout=kw.get("timeout")),
    )
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr(ghl_client.asyncio, "sleep", fake_sleep)
    return sleeps


def page_of(n, offset=0):
    return {"contacts": [{"id": f"c{offset + i}", "customFields": []} for i in range(n)], "total": 999}


def test_search_sends_the_documented_filter_and_paginates(monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, request.headers["authorization"], request.headers["version"], body))
        return httpx.Response(200, json=page_of(100, 0) if body["page"] == 1 else page_of(5, 100))

    use_mock_ghl(monkeypatch, handler)
    found = run(ghl_client.GHLClient().search_contacts_updated_since(SINCE))
    assert len(found) == 105 and len(seen) == 2
    path, auth, version, body = seen[0]
    assert path == "/contacts/search" and auth == f"Bearer {config.GHL_PRIVATE_TOKEN}" and version == "2021-07-28"
    assert body["locationId"] == config.GHL_LOCATION_ID and body["pageLimit"] == 100 and body["page"] == 1
    flt = body["filters"][0]
    assert flt["field"] == "dateUpdated" and flt["operator"] == "range"
    assert flt["value"]["gte"] == SINCE.strftime("%Y-%m-%dT%H:%M:%S.000Z") and flt["value"]["lte"].endswith("Z")


def test_search_dedupes_contacts_that_repeat_across_pages(monkeypatch):
    def handler(request):
        page = json.loads(request.content)["page"]
        return httpx.Response(200, json=page_of(100, 0) if page == 1 else page_of(3, 98))   # overlaps c98, c99

    use_mock_ghl(monkeypatch, handler)
    assert len(run(ghl_client.GHLClient().search_contacts_updated_since(SINCE))) == 101


def test_search_returns_none_on_persistent_error(monkeypatch):
    use_mock_ghl(monkeypatch, lambda request: httpx.Response(500, json={}))
    assert run(ghl_client.GHLClient().search_contacts_updated_since(SINCE)) is None


def test_search_retries_after_429_honouring_retry_after(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json=page_of(2))

    sleeps = use_mock_ghl(monkeypatch, handler)
    assert len(run(ghl_client.GHLClient().search_contacts_updated_since(SINCE))) == 2
    assert sleeps == [7.0]


def test_search_returns_none_when_it_hits_the_page_cap(monkeypatch):
    pages = []

    def handler(request):
        page = json.loads(request.content)["page"]
        pages.append(page)
        return httpx.Response(200, json=page_of(100, page * 100))

    use_mock_ghl(monkeypatch, handler)
    assert run(ghl_client.GHLClient().search_contacts_updated_since(SINCE)) is None
    assert max(pages) == ghl_client.CONTACT_SEARCH_MAX_PAGES


# -------------------------------------------------------- sheet helper methods

class FakeWorksheet:
    def __init__(self, rows, grid_rows=None):
        self.rows, self.row_count = rows, grid_rows or len(rows)
        self.full_reads, self.batches, self.appended, self.id = 0, [], [], 7

    def get(self, rng):
        first, last = (int(part[1:]) for part in rng.split(":"))
        return self.rows[first - 1:last]

    def get_all_values(self):
        self.full_reads += 1
        return self.rows

    def update(self, **kwargs):
        pass

    def batch_update(self, updates):
        self.batches.append(updates)

    def append_rows(self, rows, **kwargs):
        self.appended.append(rows)


def sheets_with(ws):
    client = object.__new__(SheetsClient)
    client._get_or_create_ws = lambda title, cols: ws
    client.sheet = type("Sp", (), {"batch_update": lambda self, body: ws.batches.append(body)})()
    return client


def log_rows(*statuses):
    rows = [["timestamp", "records_processed", "status", "error_message"]]
    for i, status in enumerate(statuses):
        rows.append([f"2026-10-16T0{i}:00:00+00:00", "5", status, ""])
    return rows


def test_last_success_skips_later_running_and_failed_rows():
    ws = FakeWorksheet(log_rows("success", "success", "failed", "running"))
    assert sheets_with(ws).get_last_success_time() == datetime(2026, 10, 16, 1, 0, tzinfo=UTC)
    assert ws.full_reads == 0                                       # tail read only


def test_last_success_falls_back_to_full_read_then_none():
    ws = FakeWorksheet(log_rows("failed", "running"))
    assert sheets_with(ws).get_last_success_time() is None and ws.full_reads == 1
    assert sheets_with(FakeWorksheet([["h"]], grid_rows=1)).get_last_success_time() is None


def test_last_success_treats_naive_timestamps_as_utc():
    ws = FakeWorksheet([["h", "h", "h"], ["2026-10-16T03:00:00", "1", "success"]])
    assert sheets_with(ws).get_last_success_time().tzinfo is not None


def test_upsert_and_stale_cleanup_reuse_a_snapshot_without_rereading():
    pairs = [(opp(f"o{i}", f"c{i}"), contact(f"c{i}")) for i in range(30)]
    values = sheet_values(pairs)
    ws = FakeWorksheet(values)
    client = sheets_with(ws)
    row = join.build_row(pairs[0][0], contact("c0", admin_call="Answered"))
    result = client.upsert_leads([row], existing=values)
    assert result == {"updated": 1, "inserted": 0, "total": 1} and ws.full_reads == 0
    live = {f"o{i}" for i in range(29)}                                   # o29 is gone: 1 of 30 is under the 10% rail
    removed = client.remove_stale_rows(live, values=values)
    assert removed["removed"] == 1 and ws.full_reads == 0
