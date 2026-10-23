"""Evonex -> GHL call write-back orchestrator.

For every finished outbound call: work out the outcome, find the GHL contact,
read it fresh, and write the call status + attempt counter + last_call_at +
last_call_by in one request. GHL workflows react to those fields and make every
decision. See docs/ghl/GHL_Backend_Writeback_Spec.md (golden rules, section 2).

Safety properties
  * WRITEBACK_MODE off (default) / dry_run / on; only "on" ever PUTs to GHL.
  * Idempotent per cid: Writeback_Log holds a claim, then one final row.
  * Serialised per contact: claims on the same contact are ordered, later ones wait.
  * Fresh read before every write; the PUT sets absolute values, so a retry
    cannot double-count; the result is read back and compared.
  * If unsure: log the reason and write nothing.
  * Logs carry ids and reasons only, never phone numbers, names or emails.
"""

import logging
import time
import uuid
from datetime import datetime, timezone

from app import config
from app.writeback_ghl import GHLError, GHLWriteback, pick_contact
from app.writeback_rules import (
    classify, collapse_events, decide, field_value_matches, is_terminal, parse_ts, to_e164,
)
from app.writeback_store import CLAIMED, FAILED, FINAL, shared_store

logger = logging.getLogger("writeback")

RELEASED = "released"            # closes a claim without finishing the call
CLAIM_TTL_SECONDS = 180
LOCK_WAIT_SECONDS = 25
LOCK_POLL_SECONDS = 2
MAX_FAILED_ATTEMPTS = 4
RETRY_AFTER_FAILURE_SECONDS = 120


class Writeback:
    def __init__(self, store, ghl, cfg=config, now=None, sleep=time.sleep):
        self.store, self.ghl, self.cfg = store, ghl, cfg
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._token = None

    # ---- state from the log ----------------------------------------------

    def _age(self, row) -> float:
        ts = parse_ts(row.get("logged_at_utc"))
        return (self._now() - ts).total_seconds() if ts else 1e9

    def _cid_state(self, cid, log) -> str:
        rows = [r for r in log if r.get("cid") == cid]
        if any(r.get("status") in FINAL for r in rows):
            return "done"
        if sum(1 for r in rows if r.get("status") == FAILED) >= MAX_FAILED_ATTEMPTS:
            return "gave_up"
        return "open"

    def _active_claims(self, log) -> list:
        """(index, row) for claims not yet closed by a later row for the same
        cid and younger than the TTL (a crashed worker's claim expires)."""
        active = []
        for i, row in enumerate(log):
            if row.get("status") != CLAIMED or self._age(row) >= CLAIM_TTL_SECONDS:
                continue
            closed = any(
                (later.get("claim_token") == row.get("claim_token") and later.get("status") != CLAIMED)
                or (later.get("cid") == row.get("cid") and later.get("status") in FINAL)
                for later in log[i + 1:]
            )
            if not closed:
                active.append((i, row))
        return active

    def _verdict(self, log, cid, contact_id, token) -> str:
        """'ok' we hold the claim; 'duplicate' another worker owns this cid or
        it is already done; 'wait' another call on the same contact goes first."""
        if self._cid_state(cid, log) == "done":
            return "duplicate"
        active = self._active_claims(log)
        mine = next((i for i, r in active if r.get("claim_token") == token), None)
        if mine is None:
            return "duplicate"
        if any(i < mine and r.get("cid") == cid for i, r in active):
            return "duplicate"
        if any(i < mine and r.get("contact_id") == contact_id and r.get("cid") != cid for i, r in active):
            return "wait"
        return "ok"

    def _acquire(self, cid, contact_id, token) -> str:
        self.store.append_log(cid=cid, status=CLAIMED, mode=self.cfg.WRITEBACK_MODE,
                              contact_id=contact_id, claim_token=token)
        deadline = self._now().timestamp() + LOCK_WAIT_SECONDS
        while True:
            verdict = self._verdict(self.store.recent_log(), cid, contact_id, token)
            if verdict != "wait":
                return verdict
            if self._now().timestamp() >= deadline:
                return "locked"
            self._sleep(LOCK_POLL_SECONDS)

    def _close(self, cid, status, **fields):
        self.store.append_log(cid=cid, status=status, mode=self.cfg.WRITEBACK_MODE, **fields)
        if status in FINAL and hasattr(self.store, "remember_done"):
            self.store.remember_done(cid)

    # ---- one call -----------------------------------------------------------

    def process_cid(self, cid: str) -> str:
        """Entry point for a webhook's terminal event. Returns a short result
        string (also useful in tests); never raises for expected conditions."""
        if self.cfg.WRITEBACK_MODE == "off":
            return "off"
        done = getattr(self.store, "done", None)
        if done is not None and cid in done:
            return "done"                              # this instance already closed it: no sheet reads at all
        # Wait out the quiet period BEFORE reading: the first read used to land inside it, find the call not yet settled and need a
        # second read after sleeping. Waiting first is the same delay for one read less (the sheet read quota is shared and small).
        self._sleep(self.cfg.WRITEBACK_SETTLE_SECONDS)
        info = collapse_events(self.store.recent_events(400)).get(cid)
        if info is None:
            return "no_events"
        state = self._cid_state(cid, self.store.recent_log())
        if state != "open":
            if state == "done" and done is not None:
                self.store.remember_done(cid)
            return state
        info = self._settle(cid, info)
        if info is None:
            return "not_terminal"          # the sweeper will pick it up
        return self._process(info)

    def _settle(self, cid, info):
        """Wait out the quiet period (a late STARTCALL/ENDCALL may follow a
        CANCELCALL), re-reading the events once. None if still not finished."""
        for _ in range(2):
            if is_terminal(info, self._now(), self.cfg.WRITEBACK_SETTLE_SECONDS):
                return info
            finished = info.end_ts is not None or (info.cancel_ts is not None and info.start_ts is None)
            if not finished or info.last_received is None:
                return None
            quiet = (self._now() - info.last_received).total_seconds()
            self._sleep(max(0.0, self.cfg.WRITEBACK_SETTLE_SECONDS - quiet) + 0.5)
            info = collapse_events(self.store.recent_events(400)).get(cid) or info
        return info if is_terminal(info, self._now(), self.cfg.WRITEBACK_SETTLE_SECONDS) else None

    def _process(self, info) -> str:
        cid = info.cid
        self._token = None
        try:
            return self._process_inner(info)
        except GHLError as exc:
            self._close(cid, FAILED, claim_token=self._token or "", reason=f"ghl_{exc}")
            logger.error("writeback cid=%s failed: ghl_%s", cid, exc)
            return "failed"
        except Exception as exc:                      # never leave a claim dangling
            self._close(cid, FAILED, claim_token=self._token or "", reason=f"exception_{type(exc).__name__}")
            logger.error("writeback cid=%s unhandled %s", cid, type(exc).__name__)
            return "failed"

    def _process_inner(self, info) -> str:
        cid, mode, cfg = info.cid, self.cfg.WRITEBACK_MODE, self.cfg
        finish = info.finish_ts
        if finish and (self._now() - finish).total_seconds() > cfg.WRITEBACK_MAX_AGE_MINUTES * 60:
            self._close(cid, "skipped", reason="too_old", caller=info.owner_name)
            return "skipped"

        outcome, talk = classify(info, cfg.WRITEBACK_TALK_SECONDS)
        if outcome is None:
            return "not_finished"

        e164 = to_e164(info.rpi)
        if e164 is None:
            self._close(cid, "skipped", reason="no_usable_number", caller=info.owner_name, outcome=outcome)
            return "skipped"

        contact, n, ambiguous = pick_contact(self.ghl, self.ghl.find_contacts(e164))
        if contact is None:
            self._close(cid, "skipped", reason=ambiguous or "no_contact_match",
                        caller=info.owner_name, outcome=outcome)
            return "skipped"
        contact_id = contact["id"]

        token = self._token = uuid.uuid4().hex
        verdict = self._acquire(cid, contact_id, token)
        if verdict == "duplicate":
            self._close(cid, RELEASED, contact_id=contact_id, claim_token=token, reason="duplicate")
            return "duplicate"
        if verdict == "locked":
            self._close(cid, FAILED, contact_id=contact_id, claim_token=token, reason="lock_timeout")
            return "failed"

        values = self.ghl.get_contact_values(contact_id)          # fresh read, never cached
        decision = decide(info, outcome, values, cfg.WRITEBACK_ADVISOR_NAMES, cfg.WRITEBACK_ADVISOR_STRICT)
        note = ";".join(
            ([f"multiple_contacts_{n}"] if n > 1 else []) + decision.warnings
        )
        base = {"contact_id": contact_id, "claim_token": token, "caller": info.owner_name, "outcome": outcome}

        if not decision.write:
            self._close(cid, "skipped", reason=";".join(filter(None, [decision.reason, note])), **base)
            return "skipped"

        base.update(pair=decision.pair, count_before=decision.count_before, count_after=decision.new_count)
        if mode == "dry_run":
            self._close(cid, "dry_run", reason=";".join(filter(None, [f"would_write_{decision.status}", note])), **base)
            logger.info("writeback cid=%s contact=%s dry_run pair=%s", cid, contact_id, decision.pair)
            return "dry_run"

        status, detail = self._write(contact_id, decision)
        self._close(cid, status, reason=";".join(filter(None, [detail, note])), **base)
        logger.info("writeback cid=%s contact=%s %s pair=%s", cid, contact_id, status, decision.pair)
        return status

    def _verified(self, contact_id, decision):
        """True when every written field reads back with the value we sent;
        otherwise the list of mismatching field ids."""
        values = self.ghl.get_contact_values(contact_id)
        bad = [f["id"] for f in decision.fields if not field_value_matches(f["field_value"], values.get(f["id"]))]
        return bad

    def _write(self, contact_id, decision):
        """PUT once (retrying only a PUT that raised), then read back. The PUT
        carries absolute values, so re-sending can never double-count."""
        raised = None
        for _ in range(2):
            try:
                self.ghl.put_custom_fields(contact_id, decision.fields)
                raised = None
            except GHLError as exc:
                raised = exc
            try:
                bad = self._verified(contact_id, decision)
            except GHLError:
                bad = ["verify_read_failed"]
            if not bad:
                return "written", ""
            if raised is None:
                return "written_unverified", "mismatch_" + ",".join(bad)
        raise raised

    # ---- safety-net sweep ---------------------------------------------------

    def sweep(self, max_calls: int = 25, budget_seconds: float = 45.0) -> dict:
        """Process finished calls whose inline attempt never completed (worker
        died, GHL was down, a CANCELCALL needed more settle time)."""
        if self.cfg.WRITEBACK_MODE == "off":
            return {"mode": "off"}
        started = time.monotonic()
        calls = collapse_events(self.store.recent_events())
        log = self.store.recent_log()
        claimed_now = {r.get("cid") for _, r in self._active_claims(log)}
        max_age = self.cfg.WRITEBACK_MAX_AGE_MINUTES * 60

        todo = []
        for cid, info in calls.items():
            finish = info.finish_ts
            if not finish or (self._now() - finish).total_seconds() > max_age:
                continue
            if cid in claimed_now or self._cid_state(cid, log) != "open":
                continue
            failed = [r for r in log if r.get("cid") == cid and r.get("status") == FAILED]
            if failed and self._age(failed[-1]) < RETRY_AFTER_FAILURE_SECONDS:
                continue
            if not is_terminal(info, self._now(), self.cfg.WRITEBACK_SETTLE_SECONDS):
                continue
            todo.append(info)
        todo.sort(key=lambda i: i.finish_ts)

        results = {}
        for info in todo[:max_calls]:
            if time.monotonic() - started > budget_seconds:
                break
            res = self._process(info)
            results[res] = results.get(res, 0) + 1
        return {"mode": self.cfg.WRITEBACK_MODE, "candidates": len(todo), "processed": results}


# --------------------------------------------------------------------------
# Entry points used by the API layer (build the real store/client, never raise)
# --------------------------------------------------------------------------

def _build() -> Writeback:
    return Writeback(shared_store(), GHLWriteback())


def process_cid_safely(cid: str) -> None:
    try:
        if config.WRITEBACK_MODE == "off":
            return
        _build().process_cid(cid)
    except Exception as exc:
        logger.error("writeback cid=%s entry failed: %s", cid, type(exc).__name__)


def sweep_safely() -> dict:
    try:
        if config.WRITEBACK_MODE == "off":
            return {"mode": "off"}
        return _build().sweep()
    except Exception as exc:
        logger.error("writeback sweep failed: %s", type(exc).__name__)
        return {"error": type(exc).__name__}
