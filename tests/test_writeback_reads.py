"""The write-back must stay light on the Google Sheets READ quota (about 60 requests a minute per user, shared by the whole system):
a tail read is one request once the last row is known, a call costs a handful of reads instead of a dozen, and a call already closed costs none."""

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app import evonex
from app import writeback as wb
from app import writeback_store as wstore
from tests import test_writeback as tw
from tests.test_qa_backend import SheetBook, SheetWS, event_row


class CountingWS(SheetWS):
    def __init__(self, rows=None):
        super().__init__(rows)
        self.reads = 0

    def col_values(self, n):
        self.reads += 1
        return super().col_values(n)

    def get(self, rng, **kw):
        self.reads += 1
        return super().get(rng, **kw)

    def row_values(self, n):
        self.reads += 1
        return super().row_values(n)


def store_with(events=(), log=None):
    store = object.__new__(wstore.WritebackStore)
    tabs = {evonex.TAB: CountingWS([evonex.COLUMNS] + list(events)), wstore.LOG_TAB: CountingWS([wstore.LOG_COLUMNS] + list(log or []))}
    store._sheet, store._events, store._log = SheetBook(tabs), None, None
    return store


def reads(store):
    return store._sheet.tabs[evonex.TAB].reads, store._sheet.tabs[wstore.LOG_TAB].reads


def reset(store):
    for ws in store._sheet.tabs.values():
        ws.reads = 0


def ids(rows):
    return [r["cid"] for r in rows]


# ---------------------------------------------------------------------------------------------------------------- tail reads

def test_a_cold_tail_read_costs_two_requests_and_a_warm_one_costs_one():
    store = store_with([event_row(i, cid=f"c{i}") for i in range(10)])
    store.recent_events(window=3)
    ws = store._sheet.tabs[evonex.TAB]
    cold = ws.reads
    assert cold == 2                                                     # count the rows, then read them
    ws.reads = 0
    assert ids(store.recent_events(window=3)) == ["c7", "c8", "c9"]
    assert ws.reads == 1                                                 # the last row is remembered: a single request


def test_rows_other_instances_appended_since_are_still_read():
    store = store_with([event_row(i, cid=f"c{i}") for i in range(10)])
    store.recent_events(window=3)
    ws = store._sheet.tabs[evonex.TAB]
    ws.rows += [event_row(10, cid="c10"), event_row(11, cid="c11")]       # another warm instance wrote two more events
    ws.reads = 0
    assert ids(store.recent_events(window=3)) == ["c7", "c8", "c9", "c10", "c11"]      # at least the window, newest included
    assert ws.reads == 1
    ws.reads = 0
    assert ids(store.recent_events(window=3)) == ["c9", "c10", "c11"]                 # and the position moved on
    assert ws.reads == 1


def test_a_position_that_ran_far_behind_is_counted_again_rather_than_missing_the_newest_rows(monkeypatch):
    monkeypatch.setattr(wstore, "TAIL_SLACK_ROWS", 5)
    store = store_with([event_row(i, cid=f"c{i}") for i in range(10)])
    store.recent_events(window=3)
    ws = store._sheet.tabs[evonex.TAB]
    ws.rows += [event_row(100 + i, cid=f"n{i}") for i in range(20)]       # far more new rows than the slack covers
    ws.reads = 0
    assert ids(store.recent_events(window=3)) == ["n17", "n18", "n19"]
    assert ws.reads == 3                                                 # the slack was used up: the one wasted read, then the exact count and read


def test_a_position_that_is_too_high_is_corrected_not_trusted():
    store = store_with([event_row(i, cid=f"c{i}") for i in range(10)])
    store.recent_events(window=3)
    ws = store._sheet.tabs[evonex.TAB]
    del ws.rows[6:]                                                       # rows were removed from the sheet: only c0..c4 are left
    assert ids(store.recent_events(window=3)) == ["c2", "c3", "c4"]       # not an empty read that hides the newest rows
    ws.rows += [event_row(50, cid="late")]
    ws.reads = 0
    assert ids(store.recent_events(window=1))[-1] == "late" and ws.reads == 1             # the newest row is there (older ones may come along)


def test_empty_and_header_only_tabs_work_cold_and_warm():
    store = store_with([])
    assert store.recent_events() == [] and store.recent_events() == [] and store.recent_log() == []
    store._sheet.tabs[evonex.TAB].rows.append(event_row(1, cid="first"))
    assert ids(store.recent_events()) == ["first"]


def test_the_log_tail_works_the_same_way():
    store = store_with([event_row(1)], log=[[f"2026-10-22T10:00:0{i}Z", f"c{i}", "skipped"] for i in range(5)])
    assert [r["cid"] for r in store.recent_log(window=2)] == ["c3", "c4"]
    ws = store._sheet.tabs[wstore.LOG_TAB]
    store.append_log(cid="cx", status="claimed", mode="dry_run", claim_token="t")
    ws.reads = 0
    assert [r["cid"] for r in store.recent_log(window=2)][-2:] == ["c4", "cx"]            # at least the window, newest last
    assert ws.reads == 1


def test_done_calls_are_remembered_up_to_a_limit(monkeypatch):
    monkeypatch.setattr(wstore, "DONE_REMEMBERED", 3)
    store = store_with()
    for cid in "abcd":
        store.remember_done(cid)
    assert list(store.done) == ["b", "c", "d"]


def test_one_shared_store_per_instance(monkeypatch):
    built = []
    monkeypatch.setattr(wstore, "_shared", None)
    monkeypatch.setattr(wstore, "WritebackStore", lambda: built.append(1) or object())
    first, second = wstore.shared_store(), wstore.shared_store()
    assert first is second and built == [1]


# ----------------------------------------------------------------------------------------------------- reads per finished call

T0 = tw.T0


def real_writeback(events, mode="dry_run", **cfg_over):
    store = store_with(events)
    clock = tw.Clock(T0 + timedelta(minutes=10))
    wbk = wb.Writeback(store, tw.FakeGHL(), tw.cfg(WRITEBACK_MODE=mode, **cfg_over), now=clock.now, sleep=clock.sleep)
    return wbk, store, clock


def sheet_rows(call_events):
    """Evonex_Events rows (as the real sheet holds them) for a list of event dicts."""
    out = []
    for e in call_events:
        row = [e.get(c, "") for c in evonex.COLUMNS]
        while row and row[-1] == "":
            row.pop()
        out.append(row)
    return out


def test_a_finished_call_costs_three_reads_when_warm_and_the_duplicate_delivery_costs_none():
    rows = sheet_rows(tw.call_events("c1", cancel=11) + tw.call_events("c2", cancel=12))
    wbk, store, clock = real_writeback(rows)
    wbk.process_cid("c1")                                                # cold: opens the worksheets, counts the rows
    reset(store)
    assert wbk.process_cid("c2") == "dry_run"
    events_reads, log_reads = reads(store)
    assert events_reads == 1                                             # the event window: a single request
    assert log_reads <= 2                                                # the early check and the claim check, one request each
    assert events_reads + log_reads <= 3
    reset(store)
    assert wbk.process_cid("c2") == "done" and reads(store) == (0, 0)    # the same call delivered again: nothing is read
    assert wbk.process_cid("c1") == "done" and reads(store) == (0, 0)


def test_the_call_is_read_after_the_quiet_period_not_before():
    rows = sheet_rows(tw.call_events("c1", cancel=11))
    wbk, store, clock = real_writeback(rows)
    sleeps = []
    original = wbk._sleep
    wbk._sleep = lambda s: (sleeps.append((s, store._sheet.tabs[evonex.TAB].reads)), original(s))[1]
    assert wbk.process_cid("c1") == "dry_run"
    assert sleeps[0][0] == 5.0 and sleeps[0][1] == 0                      # it slept the settle period first, with no sheet read before it
    assert len(sleeps) == 1                                              # and needed no second sleep (and no second events read)


def test_a_late_start_after_a_cancel_is_still_caught_with_the_single_read():
    base = tw.call_events("c1", cancel=10)
    late = tw.call_events("c1", cancel=10, start=11, end=14)
    rows = sheet_rows(late)                                              # by the time it reads, the whole call has arrived
    wbk, store, _ = real_writeback(rows, WRITEBACK_TALK_SECONDS=1)
    assert wbk.process_cid("c1") == "dry_run"
    assert store.recent_log()[-1]["outcome"] == "Answered" and base                     # connected, not a cancelled "No Answer"


def test_mode_off_reads_nothing():
    wbk, store, _ = real_writeback(sheet_rows(tw.call_events("c1", cancel=11)), mode="off")
    assert wbk.process_cid("c1") == "off" and reads(store) == (0, 0)


def test_an_unfinished_call_is_left_alone_after_one_read_each_of_events_and_nothing_else():
    wbk, store, _ = real_writeback(sheet_rows(tw.call_events("c1", start=11)))
    assert wbk.process_cid("c1") == "not_terminal" and store.recent_log() == []


def test_the_entry_points_use_the_shared_store_so_the_spreadsheet_is_opened_once(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(wb, "shared_store", lambda: sentinel)
    monkeypatch.setattr(wb, "GHLWriteback", lambda: "ghl")
    first, second = wb._build(), wb._build()
    assert first.store is sentinel and second.store is sentinel and first.ghl == "ghl"
