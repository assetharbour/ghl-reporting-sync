"""Incremental Leads sync: re-read and re-write only what changed since the last
successful sync, instead of every contact and every row every cycle.

Each run still lists every opportunity (cheap; it is how new, moved, deleted and
stage/status changes are seen). Contact-level fields (admin, advisor, call status
and most custom fields) live on the CONTACT and do not move the opportunity's
updatedAt, so changed contacts come from GHL's contact search on dateUpdated.
Rows for unchanged opportunities+contacts are left exactly as they are in the sheet.

Safety nets: a 10-minute overlap on the watermark, one full refresh per day, a full
fallback whenever the changed-contact lookup is incomplete, and a shadow mode that
logs what an incremental run would have missed before it is ever switched on.
"""

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from app import config, join
from app.field_mapping import PIPELINE, USERS
from app.users import current_users
from app.join import COLUMNS

logger = logging.getLogger("incremental")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

OVERLAP = timedelta(minutes=10)
MAX_WATERMARK_AGE = timedelta(hours=24)

_PLAIN_NUMBER = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")


class FallbackToFull(Exception):
    """The incremental path can't be trusted this run: do a full sync instead.
    `opportunities` carries the list already fetched so it isn't fetched twice."""

    def __init__(self, reason: str, opportunities=None):
        super().__init__(reason)
        self.reason = reason
        self.opportunities = opportunities


def mode() -> str:
    value = config.INCREMENTAL_SYNC
    if value in ("1", "true", "yes", "on"):
        return "on"
    if value == "shadow":
        return "shadow"
    return "off"


def parse_ts(value):
    """GHL timestamp (ISO string, ms epoch number or digit string) -> aware UTC datetime."""
    ms = join._to_ms(value)
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def needs_full_refresh(last_success, now: datetime, refresh_hour_utc: int):
    """Reason string if this run must be a full sync, else None."""
    if last_success is None:
        return "no previous successful sync"
    if now - last_success > MAX_WATERMARK_AGE:
        return "last successful sync is over 24 hours old"
    today_refresh = now.replace(hour=refresh_hour_utc, minute=0, second=0, microsecond=0)
    if now >= today_refresh and last_success < today_refresh:
        return "daily full refresh"
    return None


# --------------------------------------------------------------- row comparison

def value_text(value) -> str:
    """Same conversion upsert_leads applies when writing a cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def canon(value) -> str:
    """Comparison form: a sheet shows 1000 where Python holds 1000.0, so plain
    decimal numbers are normalised. Phone-like (+44...) and zero-padded strings
    are left alone."""
    text = value_text(value).strip()
    if _PLAIN_NUMBER.match(text):
        number = float(text)
        return str(int(number)) if number == int(number) else repr(number)
    return text


def row_values(row: dict) -> list:
    """A row dict as the list of cell strings the sheet holds, in COLUMNS order."""
    return [value_text(row.get(col)) for col in COLUMNS]


def diff_columns(row: dict, existing: dict) -> list:
    return [
        col for col in COLUMNS
        if col != "last_synced" and canon(row.get(col)) != canon(existing.get(col, ""))
    ]


def row_differs(row: dict, existing: dict) -> bool:
    return bool(diff_columns(row, existing))


def index_existing(values: list) -> dict:
    """opportunity_id -> {column: value} from a raw Leads snapshot (header first).
    Raises ValueError if the header isn't the expected COLUMNS."""
    if not values or not any(values[0]):
        return {}
    header = values[0]
    if header[: len(COLUMNS)] != COLUMNS:
        raise ValueError("Leads header row differs from COLUMNS")
    index = {}
    for raw in values[1:]:
        padded = list(raw) + [""] * (len(header) - len(raw))
        record = dict(zip(header, padded))
        if record.get("opportunity_id"):
            index[record["opportunity_id"]] = record
    return index


# --------------------------------------------------------------------- planning

def opportunity_changed(opp: dict, existing: dict, since: datetime, users: dict = None) -> bool:
    users = USERS if users is None else users
    updated = parse_ts(opp.get("updatedAt"))
    if updated is not None and updated >= since:
        return True
    return (
        PIPELINE["stages"].get(opp.get("pipelineStageId"), "") != existing.get("pipeline_stage", "")
        or (opp.get("status") or "") != existing.get("case_status", "")
        or users.get(opp.get("assignedTo"), "") != existing.get("opportunity_owner", "")
        or join._to_date_str(opp.get("createdAt")) != existing.get("created_date", "")
    )


def plan_changes(opportunities: list, existing_by_opp: dict, changed_contact_ids: set, since: datetime, users: dict = None):
    """Opportunities whose row must be rebuilt, plus a Counter of why."""
    plan, reasons = [], Counter()
    for opp in opportunities:
        current = existing_by_opp.get(opp.get("id"))
        if current is None:
            reasons["new"] += 1
        elif opp.get("contactId") in changed_contact_ids:
            reasons["contact_changed"] += 1
        elif opportunity_changed(opp, current, since, users):
            reasons["opportunity_changed"] += 1
        else:
            continue
        plan.append(opp)
    return plan, reasons


# ------------------------------------------------------------------ the run

async def run(ghl, sheets, now=None):
    """One incremental cycle. Returns (result dict, rows written) or raises
    FallbackToFull when a full sync should run instead."""
    now = now or datetime.now(timezone.utc)
    last_success = sheets.get_last_success_time()
    reason = needs_full_refresh(last_success, now, config.FULL_REFRESH_HOUR_UTC)
    if reason:
        raise FallbackToFull(reason)
    since = last_success - OVERLAP

    opportunities = await ghl.get_all_opportunities()
    fetch_complete = ghl.last_fetch_complete

    changed = await ghl.search_contacts_updated_since(since)
    if changed is None:
        raise FallbackToFull("changed-contact lookup failed or was incomplete", opportunities)
    changed_by_id = {contact["id"]: contact for contact in changed}

    values = sheets.read_leads_values()
    try:
        existing = index_existing(values)
    except ValueError as exc:
        raise FallbackToFull(str(exc), opportunities)

    users = await current_users(ghl)
    plan, reasons = plan_changes(opportunities, existing, set(changed_by_id), since, users)

    # Opportunity-only changes still need their contact; search results already
    # carry the contact (custom fields included) for everything else.
    missing = {o.get("contactId") for o in plan if o.get("contactId")} - set(changed_by_id)
    fetched = await ghl.get_all_contacts(sorted(missing)) if missing else {}
    contact_map = {**fetched, **changed_by_id}

    rebuilt = join.build_all_rows(plan, contact_map, users)
    to_write = [
        row for row in rebuilt
        if row["opportunity_id"] not in existing or row_differs(row, existing[row["opportunity_id"]])
    ]
    outcome = (
        sheets.upsert_leads(to_write, existing=values)
        if to_write else {"updated": 0, "inserted": 0, "total": 0}
    )

    removed = 0
    if fetch_complete:
        try:
            removed = sheets.remove_stale_rows({o["id"] for o in opportunities}, values=values)["removed"]
        except Exception:
            logger.exception("Stale-row cleanup failed (sync itself succeeded)")
    else:
        logger.warning("Skipping stale-row cleanup: opportunity fetch was incomplete")

    result = {
        "status": "success",
        "mode": "incremental",
        "opportunities_fetched": len(opportunities),
        "contacts_changed": len(changed_by_id),
        "contacts_fetched": len(fetched),
        "rows_rebuilt": len(rebuilt),
        "rows_inserted": outcome["inserted"],
        "rows_updated": outcome["updated"],
        "rows_removed": removed,
        "plan_reasons": dict(reasons),
    }
    return result, outcome["total"]


# --------------------------------------------------------------- shadow mode

def _record_shadow(sheets, entry: dict):
    """Durable copy of the shadow result (Vercel's per-request log is too crowded to rely on)."""
    try:
        sheets.log_shadow(entry)
    except Exception:
        logger.exception("shadow: could not write Sync_Shadow")

async def shadow_check(ghl, sheets, opportunities: list, rows: list):
    """Run beside a normal FULL sync: log which rows really changed versus which
    an incremental run would have rebuilt. A non-zero `missed` is the signal that
    incremental is not yet safe. Never raises. Returns the Leads snapshot it read
    (or None) so the caller can reuse it for the upsert."""
    try:
        values = sheets.read_leads_values()
    except Exception:
        logger.exception("shadow: could not read the Leads tab")
        return None
    try:
        existing = index_existing(values)
        last_success = sheets.get_last_success_time()
        if last_success is None:
            logger.info("shadow: no previous successful sync, nothing to compare")
            _record_shadow(sheets, {"missed_detail": "no previous successful sync"})
            return values
        since = last_success - OVERLAP
        changed = await ghl.search_contacts_updated_since(since)
        if changed is None:
            logger.warning("shadow: changed-contact lookup incomplete this run")
            _record_shadow(sheets, {"since_utc": since.isoformat(), "missed_detail": "changed-contact lookup incomplete"})
            return values
        plan, reasons = plan_changes(opportunities, existing, {c["id"] for c in changed}, since, await current_users(ghl))
        planned = {o["id"] for o in plan}

        true_changes = {}
        for row in rows:
            current = existing.get(row["opportunity_id"])
            if current is None:
                true_changes[row["opportunity_id"]] = ["(new row)"]
            else:
                columns = diff_columns(row, current)
                if columns:
                    true_changes[row["opportunity_id"]] = columns
        missed = [oid for oid in true_changes if oid not in planned]
        logger.info(
            "shadow since=%s contacts_changed=%d planned=%d true_changes=%d missed=%d reasons=%s",
            since.strftime("%H:%M:%S"), len(changed), len(planned), len(true_changes), len(missed), dict(reasons),
        )
        for oid in missed[:10]:
            logger.warning("shadow MISSED opportunity=%s columns=%s", oid, ",".join(true_changes[oid]))
        _record_shadow(sheets, {
            "since_utc": since.isoformat(), "contacts_changed": len(changed), "planned": len(planned),
            "true_changes": len(true_changes), "missed": len(missed),
            "missed_detail": "; ".join(f"{oid}:{','.join(true_changes[oid])}" for oid in missed[:10]),
        })
    except Exception:
        logger.exception("shadow check failed")
    return values
