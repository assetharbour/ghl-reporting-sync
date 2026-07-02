from datetime import datetime

from fastapi import FastAPI, Header, HTTPException

from app import config, ghl_client, join, sheets_client

app = FastAPI()


@app.post("/api/sync")
async def run_sync(x_cron_secret: str = Header(None)):
    if x_cron_secret != config.CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    start = datetime.utcnow()

    try:
        ghl = ghl_client.GHLClient()
        opportunities = await ghl.get_all_opportunities()

        contact_ids = list({o["contactId"] for o in opportunities if o.get("contactId")})
        contact_map = await ghl.get_all_contacts(contact_ids)

        rows = join.build_all_rows(opportunities, contact_map)

        sheets = sheets_client.SheetsClient()
        result = sheets.upsert_leads(rows)

        sheets.log_sync("success", result["total"])

        duration = (datetime.utcnow() - start).total_seconds()

        return {
            "status": "success",
            "opportunities_fetched": len(opportunities),
            "contacts_fetched": len(contact_map),
            "rows_inserted": result["inserted"],
            "rows_updated": result["updated"],
            "duration_seconds": round(duration, 2),
        }

    except Exception as e:
        sheets_client.SheetsClient().log_sync("failed", 0, str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sync/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
