"""Async client for the GoHighLevel (LeadConnector) v2 API."""

import asyncio
import logging

import httpx

from app import config

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
CONTACT_BATCH_SIZE = 10
BATCH_PAUSE_SECONDS = 1.2  # GHL burst limit is 100 req / 10s per location
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 4


class GHLClient:
    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {config.GHL_PRIVATE_TOKEN}",
            "Version": "2021-07-28",
            "Content-Type": "application/json",
        }

    async def get_all_opportunities(self) -> list:
        """Fetch every opportunity in the configured pipeline, paginating
        with startAfterId/startAfter until an empty page is returned."""
        opportunities = []
        start_after_id = None
        start_after = None

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            while True:
                params = {
                    "location_id": config.GHL_LOCATION_ID,
                    "pipeline_id": config.GHL_PIPELINE_ID,
                    "limit": PAGE_SIZE,
                }
                if start_after_id:
                    params["startAfterId"] = start_after_id
                if start_after:
                    params["startAfter"] = start_after

                try:
                    resp = await client.get(
                        f"{config.GHL_BASE_URL}/opportunities/search",
                        headers=self._get_headers(),
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error("Failed to fetch opportunities page: %s", e)
                    break

                page = data.get("opportunities", [])
                if not page:
                    break

                opportunities.extend(page)

                meta = data.get("meta", {})
                start_after_id = meta.get("startAfterId")
                start_after = meta.get("startAfter")
                if not start_after_id or len(page) < PAGE_SIZE:
                    break

        logger.info("Fetched %d opportunities", len(opportunities))
        return opportunities

    async def get_all_contacts(self, contact_ids: list) -> dict:
        """Fetch only the contacts linked to our pipeline's opportunities,
        in parallel batches of 10 to stay under rate limits."""
        contact_map = {}

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            for i in range(0, len(contact_ids), CONTACT_BATCH_SIZE):
                batch = contact_ids[i : i + CONTACT_BATCH_SIZE]
                results = await asyncio.gather(
                    *(self._get_contact(client, cid) for cid in batch)
                )
                for cid, contact in zip(batch, results):
                    if contact is not None:
                        contact_map[cid] = contact
                if i + CONTACT_BATCH_SIZE < len(contact_ids):
                    await asyncio.sleep(BATCH_PAUSE_SECONDS)

        logger.info("Fetched %d of %d contacts", len(contact_map), len(contact_ids))
        return contact_map

    async def _get_contact(self, client: httpx.AsyncClient, contact_id: str):
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.get(
                    f"{config.GHL_BASE_URL}/contacts/{contact_id}",
                    headers=self._get_headers(),
                )
                if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
                    wait = float(resp.headers.get("Retry-After") or 2 ** (attempt + 1))
                    logger.warning(
                        "Rate limited fetching contact %s — retrying in %.1fs",
                        contact_id, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json().get("contact")
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                logger.error("Failed to fetch contact %s: %s", contact_id, e)
                return None
        return None
