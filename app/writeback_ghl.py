"""GoHighLevel calls for the call write-back (synchronous: it runs in a worker
thread, never in the webhook's request path).

Reads: contact search by phone, one fresh contact, open opportunities.
Writes: one PUT /contacts/{id}, restricted to the six backend-owned fields.
"""

import logging
import time

import httpx

from app import config
from app.writeback_rules import (
    ADMIN_PHASE, ADVISOR_PHASE, F_CALL_PHASE, WRITE_FIELD_IDS, search_candidates, to_e164,
)

logger = logging.getLogger("writeback")

TIMEOUT = 15.0
MAX_ATTEMPTS = 4
BACKOFF = (0.5, 1.0, 2.0)
MORTGAGE_PIPELINE_ID = "EQyj0fwCvT8vrLEGTw5I"   # Mortgage - Enquiry to Completion


class GHLError(Exception):
    """A GHL call failed after retries (carries the HTTP status when there was one)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GHLWriteback:
    def __init__(self, client: httpx.Client = None):
        self._client = client or httpx.Client(timeout=TIMEOUT)

    def _request(self, method, path, **kwargs):
        """One call with retry on 429 / 5xx / network errors. 4xx other than 429
        fail immediately. Retries are safe here: reads are idempotent, and the
        PUT sets absolute values (the orchestrator verifies before re-sending)."""
        last = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self._client.request(
                    method, config.GHL_BASE_URL + path, headers=config.GHL_HEADERS, **kwargs
                )
            except httpx.HTTPError as exc:
                last = GHLError(type(exc).__name__)
            else:
                if resp.status_code < 300:
                    return resp.json() if resp.content else {}
                last = GHLError(f"http_{resp.status_code}", resp.status_code)
                if resp.status_code != 429 and resp.status_code < 500:
                    raise last
                if resp.status_code == 429:
                    try:
                        time.sleep(min(float(resp.headers.get("Retry-After") or 0), 5.0))
                    except ValueError:
                        pass
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BACKOFF[attempt])
        raise last

    # ---- reads ---------------------------------------------------------

    def find_contacts(self, e164: str) -> list:
        """Contacts whose stored phone equals this number. The search is a loose
        text query, so every hit is re-checked against the normalised number."""
        found = {}
        for query in search_candidates(e164):
            data = self._request(
                "GET", "/contacts/",
                params={"locationId": config.GHL_LOCATION_ID, "query": query, "limit": 20},
            )
            for c in data.get("contacts", []):
                phones = [c.get("phone")] + [
                    p.get("phone") if isinstance(p, dict) else p for p in (c.get("additionalPhones") or [])
                ]
                if any(to_e164(p) == e164 for p in phones if p):
                    found[c["id"]] = c
            if found:
                break
        return list(found.values())

    def has_open_mortgage_opportunity(self, contact_id: str) -> bool:
        data = self._request(
            "GET", "/opportunities/search",
            params={
                "location_id": config.GHL_LOCATION_ID, "contact_id": contact_id,
                "pipeline_id": MORTGAGE_PIPELINE_ID, "status": "open", "limit": 1,
            },
        )
        return bool(data.get("opportunities"))

    def get_contact_values(self, contact_id: str) -> dict:
        """FRESH read: custom field id -> value (never cached; workflows change
        these fields at any time)."""
        contact = self._request("GET", f"/contacts/{contact_id}").get("contact", {})
        return {f.get("id"): f.get("value") for f in contact.get("customFields", [])}

    # ---- write ---------------------------------------------------------

    def put_custom_fields(self, contact_id: str, fields: list) -> None:
        """Status, counter, last_call_at and last_call_by in ONE request (spec
        rule 2). Refuses to send any field outside the backend allow-list."""
        illegal = [f["id"] for f in fields if f["id"] not in WRITE_FIELD_IDS]
        if illegal:
            raise GHLError("field_not_allowed")
        self._request("PUT", f"/contacts/{contact_id}", json={"customFields": fields})


def pick_contact(ghl, candidates: list):
    """Spec 5.2 with a safety rule. Returns (contact or None, how_many, reason).

    * one match: use it
    * several: contacts with an OPEN opportunity in the main pipeline come first;
      exactly one of them wins. If several have one, the one whose call_phase is
      Admin Call / Advisor Call wins; if that still leaves more than one, the
      call is ambiguous and nothing is written (reason "ambiguous_contact"):
      writing to the wrong joint applicant would start the wrong workflow.
    * several, none with an open opportunity: the most recently updated (the
      call_phase check afterwards stops anything being written unless that
      contact really is in a calling phase).
    """
    if not candidates:
        return None, 0, None
    n = len(candidates)
    if n == 1:
        return candidates[0], 1, None

    ranked = sorted(candidates, key=lambda c: str(c.get("dateUpdated") or ""), reverse=True)
    open_cases = []
    for c in ranked:
        try:
            if ghl.has_open_mortgage_opportunity(c["id"]):
                open_cases.append(c)
        except GHLError:
            continue
    if not open_cases:
        return ranked[0], n, None
    if len(open_cases) == 1:
        return open_cases[0], n, None

    calling = []
    for c in open_cases:
        try:
            phase = str(ghl.get_contact_values(c["id"]).get(F_CALL_PHASE) or "").strip().lower()
        except GHLError:
            continue
        if phase in (ADMIN_PHASE, ADVISOR_PHASE):
            calling.append(c)
    if len(calling) == 1:
        return calling[0], n, None
    return None, n, "ambiguous_contact"
