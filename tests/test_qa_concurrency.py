"""QA: the write-back's exactly-once and no-lost-update guarantees, exercised with REAL threads.

Vercel runs each webhook's follow-up work in its own worker, so two workers can process the same call (Evonex
delivers ~4 % of events twice) or two calls to the same contact at the same moment. There is no database: the
claim rows in Writeback_Log are the only coordination. These tests run several workers at once against a store
and a fake GHL that behave like the real ones (appends are atomic and ordered, a PUT is applied atomically,
every call takes a little time so the interleavings are real)."""

import random
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import writeback as wb
from app.writeback_rules import F_ADMIN_COUNT, F_ADMIN_STATUS, F_CALL_PHASE
from tests import test_writeback as tw


class AtomicStore(tw.FakeStore):
    """Appends and reads are individually atomic (like Google Sheets) and take a little time."""

    def __init__(self):
        super().__init__(clock=None)
        self.lock = threading.Lock()

    def recent_events(self, window=0):
        time.sleep(random.random() * 0.004)
        return list(self.events)

    def recent_log(self, window=0):
        time.sleep(random.random() * 0.004)
        with self.lock:
            return [dict(r) for r in self.log]

    def append_log(self, **fields):
        time.sleep(random.random() * 0.004)
        row = {"logged_at_utc": datetime.now(timezone.utc).isoformat()}
        row.update({k: str(v) for k, v in fields.items()})
        with self.lock:
            self.log.append(row)


class AtomicGHL(tw.FakeGHL):
    def __init__(self, contacts):
        super().__init__(contacts=[{"id": c} for c in contacts], values={c: {F_CALL_PHASE: "Admin Call", F_ADMIN_COUNT: 0} for c in contacts})
        self.lock = threading.Lock()
        self.put_log = []

    def find_contacts(self, e164):
        time.sleep(random.random() * 0.004)
        return list(self.contacts)

    def get_contact_values(self, contact_id):
        time.sleep(random.random() * 0.004)
        with self.lock:
            return dict(self.values[contact_id])

    def put_custom_fields(self, contact_id, fields):
        time.sleep(random.random() * 0.004)
        with self.lock:
            self.puts.append((contact_id, fields))
            self.put_log.append({f["id"]: f["field_value"] for f in fields})
            self._apply(contact_id, fields)


@pytest.fixture(autouse=True)
def quick_polling(monkeypatch):
    monkeypatch.setattr(wb, "LOCK_POLL_SECONDS", 0.005)
    monkeypatch.setattr(wb, "LOCK_WAIT_SECONDS", 20)


def recent_events(cid, n=1):
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    return tw.call_events(cid, cancel=11, base=base)


def run_workers(jobs):
    """Start every job on its own thread at the same moment and wait for all of them."""
    barrier = threading.Barrier(len(jobs))
    results, errors = [], []

    def worker(job):
        try:
            barrier.wait()
            results.append(job())
        except Exception as exc:                                              # noqa: BLE001 - surfaced below
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(j,)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    assert not any(t.is_alive() for t in threads), "a worker never finished (deadlock?)"
    return results


def new_worker(store, ghl):
    return wb.Writeback(store, ghl, tw.cfg(WRITEBACK_MAX_AGE_MINUTES=60, WRITEBACK_SETTLE_SECONDS=0.0))


@pytest.mark.parametrize("round_", range(5))
def test_eight_workers_on_the_same_call_write_exactly_once(round_):
    store, ghl = AtomicStore(), AtomicGHL(["C1"])
    store.events = recent_events("dup")
    results = run_workers([lambda: new_worker(store, ghl).process_cid("dup") for _ in range(8)])
    assert len(ghl.puts) == 1, results                                        # exactly one PUT for one call
    assert ghl.values["C1"][F_ADMIN_COUNT] == 1
    assert results.count("written") == 1 and set(results) <= {"written", "duplicate", "done"}
    finals = [r for r in store.log if r["cid"] == "dup" and r["status"] in ("written", "written_unverified", "skipped", "dry_run")]
    assert len(finals) == 1                                                   # one final row per cid


@pytest.mark.parametrize("round_", range(5))
def test_six_calls_to_one_contact_at_once_never_lose_an_update(round_):
    store, ghl = AtomicStore(), AtomicGHL(["C1"])
    cids = [f"call{i}" for i in range(6)]
    store.events = [e for c in cids for e in recent_events(c)]
    run_workers([lambda c=c: new_worker(store, ghl).process_cid(c) for c in cids])
    assert len(ghl.puts) == 6
    counts = sorted(p[F_ADMIN_COUNT] for p in ghl.put_log)
    assert counts == [1, 2, 3, 4, 5, 6]                                       # each worker read the previous worker's result: no two wrote the same count
    assert ghl.values["C1"][F_ADMIN_COUNT] == 6


def test_duplicates_and_different_calls_mixed_together():
    store, ghl = AtomicStore(), AtomicGHL(["C1"])
    cids = [f"mix{i}" for i in range(4)]
    store.events = [e for c in cids for e in recent_events(c)]
    run_workers([lambda c=c: new_worker(store, ghl).process_cid(c) for c in cids for _ in range(2)])     # every call delivered twice
    assert sorted(p[F_ADMIN_COUNT] for p in ghl.put_log) == [1, 2, 3, 4]
    assert ghl.values["C1"][F_ADMIN_COUNT] == 4


def test_calls_to_different_contacts_do_not_wait_for_each_other():
    store, ghl = AtomicStore(), AtomicGHL(["C1", "C2", "C3"])
    store.events = []
    for i, c in enumerate(["C1", "C2", "C3"]):
        store.events += tw.call_events(f"k{i}", cancel=11, base=datetime.now(timezone.utc) - timedelta(minutes=10), rpi=f"0770090012{i}")
    # one fake GHL serving three contacts: route each number to its own contact
    ghl.find_contacts = lambda e164: [{"id": "C" + str(int(e164[-1]) + 1)}]
    started = time.monotonic()
    run_workers([lambda c=f"k{i}": new_worker(store, ghl).process_cid(c) for i in range(3)])
    assert sorted(p[0] for p in ghl.puts) == ["C1", "C2", "C3"]
    assert time.monotonic() - started < 5                                      # no cross-contact lock contention
    assert all(ghl.values[c][F_ADMIN_COUNT] == 1 for c in ("C1", "C2", "C3"))


def test_a_sweeper_racing_the_inline_worker_does_not_double_write():
    store, ghl = AtomicStore(), AtomicGHL(["C1"])
    store.events = recent_events("race")
    run_workers([
        lambda: new_worker(store, ghl).process_cid("race"),
        lambda: new_worker(store, ghl).sweep(),
        lambda: new_worker(store, ghl).sweep(),
    ])
    assert len(ghl.puts) == 1 and ghl.values["C1"][F_ADMIN_COUNT] == 1 and ghl.values["C1"][F_ADMIN_STATUS] == "No Answer"
