"""Local end-to-end test — runs the sync pipeline directly (no HTTP layer).

Usage: python test_sync.py
"""

import asyncio
import json
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app import ghl_client, join, sheets_client  # noqa: E402  (config loads .env on import)


async def main():
    start = datetime.utcnow()

    print("=== 1. Fetching opportunities from GHL ===")
    ghl = ghl_client.GHLClient()
    opportunities = await ghl.get_all_opportunities()
    print(f"Opportunities fetched: {len(opportunities)}")

    contact_ids = list({o["contactId"] for o in opportunities if o.get("contactId")})
    print(f"\n=== 2. Fetching {len(contact_ids)} linked contacts ===")
    contact_map = await ghl.get_all_contacts(contact_ids)
    print(f"Contacts fetched: {len(contact_map)}")

    print("\n=== 3. Building rows ===")
    rows = join.build_all_rows(opportunities, contact_map)
    print(f"Rows built: {len(rows)}")

    print("\n=== First 3 rows to be written ===")
    for row in rows[:3]:
        print(json.dumps(row, indent=2, default=str))

    print("\n=== 4. Upserting to Google Sheets ===")
    sheets = sheets_client.SheetsClient()
    result = sheets.upsert_leads(rows)
    sheets.log_sync("success", result["total"])

    duration = (datetime.utcnow() - start).total_seconds()

    print("\n=== RESULT ===")
    print(json.dumps({
        "status": "success",
        "opportunities_fetched": len(opportunities),
        "contacts_fetched": len(contact_map),
        "rows_inserted": result["inserted"],
        "rows_updated": result["updated"],
        "duration_seconds": round(duration, 2),
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
