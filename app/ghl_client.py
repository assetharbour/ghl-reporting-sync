"""Async client for the GoHighLevel (LeadConnector) v2 API."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app import config

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
CONTACT_BATCH_SIZE = 10
BATCH_PAUSE_SECONDS = 1.2  # GHL burst limit is 100 req / 10s per location
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 4
CONTACT_SEARCH_PAGE_SIZE = 100
CONTACT_SEARCH_MAX_PAGES = 20
CALENDAR_API_VERSION = "2021-04-15"   # the calendars endpoints use this version header
CALENDAR_WINDOW_DAYS = 31


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
        # Callers that act on absence (deleting stale Sheet rows) must check
        # this: a page error below ends the loop with a partial list.
        self.last_fetch_complete = True

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
                    self.last_fetch_complete = False
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

        # Cursor pagination (startAfterId/startAfter) is a moving-target risk
        # against a live-updating pipeline: if an opportunity's sort position
        # shifts between page fetches, it can land in both the earlier and a
        # later page. Dedupe by id, keeping the last-seen copy (from the
        # later page, so the fresher read wins).
        deduped = {opp.get("id"): opp for opp in opportunities}
        opportunities = list(deduped.values())

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

    async def search_contacts_updated_since(self, since: datetime):
        """Contacts (custom fields included) whose dateUpdated is at or after
        `since`, via POST /contacts/search. Covers the whole location, so the
        caller filters to the contacts it cares about.

        Returns None whenever the result might be incomplete (request failure,
        or more pages than CONTACT_SEARCH_MAX_PAGES), so the caller can fall back
        to a full sync instead of silently missing changes."""
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        gte = since.astimezone(timezone.utc).strftime(fmt)
        lte = datetime.now(timezone.utc).strftime(fmt)
        found = {}

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            for page in range(1, CONTACT_SEARCH_MAX_PAGES + 1):
                body = {
                    "locationId": config.GHL_LOCATION_ID,
                    "page": page,
                    "pageLimit": CONTACT_SEARCH_PAGE_SIZE,
                    "filters": [
                        {"field": "dateUpdated", "operator": "range", "value": {"gte": gte, "lte": lte}}
                    ],
                }
                data = await self._post_json(client, "/contacts/search", body)
                if data is None:
                    return None
                batch = data.get("contacts") or []
                for contact in batch:
                    if contact.get("id"):
                        found[contact["id"]] = contact
                if len(batch) < CONTACT_SEARCH_PAGE_SIZE:
                    return list(found.values())

        logger.warning("Contact search hit the %d-page cap; treating it as incomplete", CONTACT_SEARCH_MAX_PAGES)
        return None

    async def get_users(self):
        """{user id: name} for the location, live. The static USERS map in
        field_mapping is out of date (it left 85 of 196 appointment bookers
        unresolved), so anything that names people reads this instead.
        None on failure."""
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            data = await self._get_json(
                client, "/users/", {"locationId": config.GHL_LOCATION_ID}, self._get_headers()
            )
        if data is None:
            return None
        return {u["id"]: u.get("name", "") for u in data.get("users", []) if u.get("id")}

    async def get_calendar_events(self, calendar_id: str, start: datetime, end: datetime):
        """Every event of one calendar between two datetimes (by start time), fetched
        in 31-day windows. Each event carries createdBy.userId (who booked it),
        assignedUserId (the advisor), dateAdded, startTime and appointmentStatus.
        Returns None if any window fails, so callers never act on a partial list."""
        found, cursor = {}, start
        headers = {**self._get_headers(), "Version": CALENDAR_API_VERSION}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            while cursor < end:
                window_end = min(cursor + timedelta(days=CALENDAR_WINDOW_DAYS), end)
                params = {
                    "locationId": config.GHL_LOCATION_ID, "calendarId": calendar_id,
                    "startTime": int(cursor.timestamp() * 1000), "endTime": int(window_end.timestamp() * 1000),
                }
                data = await self._get_json(client, "/calendars/events", params, headers)
                if data is None:
                    return None
                for event in data.get("events") or []:
                    if event.get("id"):
                        found[event["id"]] = event
                cursor = window_end
        return list(found.values())

    async def search_contacts_with_field(self, field_id: str):
        """Contacts (custom fields included) that have a value in one custom field.
        None if the result might be incomplete."""
        found = {}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            for page in range(1, CONTACT_SEARCH_MAX_PAGES + 1):
                body = {
                    "locationId": config.GHL_LOCATION_ID, "page": page, "pageLimit": CONTACT_SEARCH_PAGE_SIZE,
                    "filters": [{"field": f"customFields.{field_id}", "operator": "exists"}],
                }
                data = await self._post_json(client, "/contacts/search", body)
                if data is None:
                    return None
                batch = data.get("contacts") or []
                for contact in batch:
                    if contact.get("id"):
                        found[contact["id"]] = contact
                if len(batch) < CONTACT_SEARCH_PAGE_SIZE:
                    return list(found.values())
        logger.warning("Field search hit the %d-page cap; treating it as incomplete", CONTACT_SEARCH_MAX_PAGES)
        return None

    async def _get_json(self, client: httpx.AsyncClient, path: str, params: dict, headers: dict):
        """GET with retry/backoff (honours 429 Retry-After). None on final failure."""
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.get(f"{config.GHL_BASE_URL}{path}", headers=headers, params=params)
                if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(float(resp.headers.get("Retry-After") or 2 ** (attempt + 1)))
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                logger.error("GET %s failed: %s", path, type(e).__name__)
                return None
        return None

    async def _post_json(self, client: httpx.AsyncClient, path: str, body: dict):
        """POST with retry/backoff (honours 429 Retry-After). None on final failure."""
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.post(
                    f"{config.GHL_BASE_URL}{path}", headers=self._get_headers(), json=body
                )
                if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(float(resp.headers.get("Retry-After") or 2 ** (attempt + 1)))
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                logger.error("POST %s failed: %s", path, type(e).__name__)
                return None
        return None

    async def get_custom_value(self, value_id: str) -> dict:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                f"{config.GHL_BASE_URL}/locations/{config.GHL_LOCATION_ID}/customValues/{value_id}",
                headers=self._get_headers(),
            )
            resp.raise_for_status()
            return resp.json()["customValue"]

    async def update_custom_value(self, value_id: str, name: str, value: str) -> dict:
        """Confirmed live (Part 1): GHL_PRIVATE_TOKEN has
        locations/customValues.write — tested with a real read/write/
        read-back cycle against weekly_introducer_report_url."""
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.put(
                f"{config.GHL_BASE_URL}/locations/{config.GHL_LOCATION_ID}/customValues/{value_id}",
                headers=self._get_headers(),
                json={"name": name, "value": value},
            )
            resp.raise_for_status()
            return resp.json()["customValue"]

    async def list_custom_values(self) -> list:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                f"{config.GHL_BASE_URL}/locations/{config.GHL_LOCATION_ID}/customValues",
                headers=self._get_headers(),
            )
            resp.raise_for_status()
            return resp.json()["customValues"]

    async def create_custom_value(self, name: str, value: str) -> dict:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{config.GHL_BASE_URL}/locations/{config.GHL_LOCATION_ID}/customValues",
                headers=self._get_headers(),
                json={"name": name, "value": value},
            )
            resp.raise_for_status()
            return resp.json()["customValue"]

    async def upsert_custom_value_by_name(self, name: str, value: str, existing: list = None) -> dict:
        """Per-introducer custom values are created on demand (one per
        lead_source, named e.g. weekly_introducer_report_url_veriform) —
        unlike the two fixed weekly/monthly IDs in config.py, there's no
        known id ahead of time. `existing` lets a caller looping over many
        names reuse one list_custom_values() call instead of paying for a
        fresh list request per introducer."""
        custom_values = existing if existing is not None else await self.list_custom_values()
        match = next((cv for cv in custom_values if cv.get("name") == name), None)
        if match:
            return await self.update_custom_value(match["id"], name, value)
        return await self.create_custom_value(name, value)

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
