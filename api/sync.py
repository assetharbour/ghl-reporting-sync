import logging
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from app import config, ghl_client, join, sheets_client

logger = logging.getLogger(__name__)

app = FastAPI()

LOCK_STALE_MINUTES = 10


def _lock_is_held(sheets: sheets_client.SheetsClient) -> bool:
    """A sync is considered in-flight if the last Sync_Log row is 'running'
    and younger than LOCK_STALE_MINUTES. Older 'running' rows are treated
    as crashed and the lock is not honoured."""
    last = sheets.get_last_log_entry()
    if not last or last.get("status") != "running":
        return False
    try:
        ts = datetime.fromisoformat(last["timestamp"])
    except (KeyError, ValueError):
        return False
    age_minutes = (datetime.now(timezone.utc) - ts).total_seconds() / 60
    return age_minutes < LOCK_STALE_MINUTES


async def do_full_sync(sheets: sheets_client.SheetsClient = None) -> dict:
    """Full sync cycle: fetch opportunities -> fetch contacts -> join -> upsert -> log.

    Must never raise unhandled — every outcome (success or failure) is
    recorded in Sync_Log, which is the source of truth for whether a
    cycle actually completed.
    """
    sheets = sheets or sheets_client.SheetsClient()
    try:
        ghl = ghl_client.GHLClient()
        opportunities = await ghl.get_all_opportunities()

        contact_ids = list({o["contactId"] for o in opportunities if o.get("contactId")})
        contact_map = await ghl.get_all_contacts(contact_ids)

        rows = join.build_all_rows(opportunities, contact_map)

        result = sheets.upsert_leads(rows)
        sheets.log_sync("success", result["total"])

        return {
            "status": "success",
            "opportunities_fetched": len(opportunities),
            "contacts_fetched": len(contact_map),
            "rows_inserted": result["inserted"],
            "rows_updated": result["updated"],
        }

    except Exception as e:
        logger.exception("Sync failed")
        try:
            sheets.log_sync("failed", 0, str(e))
        except Exception:
            logger.exception("Failed to write failure entry to Sync_Log")
        return {"status": "failed", "error": str(e)}


@app.post("/api/sync")
async def run_sync(
    background_tasks: BackgroundTasks,
    wait: bool = False,
    x_cron_secret: str = Header(None),
):
    if x_cron_secret != config.CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    start = datetime.now(timezone.utc)
    sheets = sheets_client.SheetsClient()

    if _lock_is_held(sheets):
        return {"status": "skipped", "reason": "previous sync still running"}

    # Claim the lock immediately so a rapid second request sees "running".
    sheets.log_sync("running", 0)

    if wait:
        result = await do_full_sync(sheets)
        result["duration_seconds"] = round((datetime.now(timezone.utc) - start).total_seconds(), 2)
        return result

    background_tasks.add_task(do_full_sync, sheets)
    return {
        "status": "accepted",
        "note": "sync running in background — check Sync_Log for result",
    }


@app.get("/api/sync/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
