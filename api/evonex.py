"""Evonex webhook receiver (Phase 1: verify, then durably store every event).

Handling order (per the Evonex contract):
  401 no Authorization -> 403 bad token -> 403 missing/invalid signature
  -> 400 body not a JSON object -> write row -> 200 only after the row is stored.
The write is synchronous (no background task): returning 200 before the row
is durable would let Evonex believe an event was saved when it was not.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import config, evonex

logger = logging.getLogger("evonex")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

app = FastAPI()

_SAFE = re.compile(r"[^A-Za-z0-9_./-]")


def _safe(value, limit: int = 60) -> str:
    """Make an attacker-controllable value safe to put in a log line."""
    return _SAFE.sub("?", str(value if value is not None else ""))[:limit]


def _respond(status: int, body: dict, request_id: str, path: str, started: float,
             event=None, subevent=None, variant="-", reason=None) -> JSONResponse:
    # Never logs rpi/rpn/on/oe/om, the raw body, the token or the signature.
    logger.info(
        "request_id=%s path=%s event=%s subevent=%s status=%s latency_ms=%d variant=%s%s",
        request_id, _safe(path), _safe(event), _safe(subevent), status,
        int((time.monotonic() - started) * 1000), variant,
        f" reason={reason}" if reason else "",
    )
    return JSONResponse(status_code=status, content=body)


@app.get("/api/evonex/health")
async def health():
    return {"status": "ok"}


@app.post("/api/evonex/{path:path}")
async def receive(path: str, request: Request):
    started = time.monotonic()
    request_id = uuid.uuid4().hex[:12]
    try:
        # 1. Raw bytes, read once: the exact text is needed for signing/storage.
        body = await request.body()

        # 2. No Authorization header -> 401
        authorization = request.headers.get("authorization")
        if not authorization:
            return _respond(401, {"error": "unauthorized"}, request_id, path, started, reason="no_authorization")

        # 3. Token not in the allowed list -> 403
        token = evonex.extract_token(authorization)
        if not config.EVONEX_WEBHOOK_TOKENS or not evonex.token_is_valid(token):
            return _respond(403, {"error": "forbidden"}, request_id, path, started, reason="invalid_token")

        # 4. Missing / malformed signature header -> 403
        signature = request.headers.get("x-hub-signature-256")
        if not signature or not signature.strip().startswith("sha256="):
            return _respond(403, {"error": "forbidden"}, request_id, path, started, reason="missing_signature")

        # 5. Signature must match one of the candidate URLs -> else 403
        candidates = evonex.signature_candidates(
            request.url.path, request.url.query, str(request.url)
        )
        variant = evonex.matching_signature_variant(token, body, signature, candidates)
        if variant is None:
            return _respond(403, {"error": "forbidden"}, request_id, path, started, reason="invalid_signature")

        # 6. Authenticated, but the body must be a JSON object -> else 400
        raw_text = body.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_text)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return _respond(400, {"error": "invalid_body"}, request_id, path, started, variant=variant, reason="invalid_body")

        event = payload.get("event") if isinstance(payload.get("event"), str) else None
        subevent = payload.get("subevent") if isinstance(payload.get("subevent"), str) else None

        # 7. Build the row and append it; 200 only once it is durably written.
        if not config.EVONEX_SHEET_ID:
            logger.error("EVONEX_SHEET_ID is not configured")
            return _respond(500, {"error": "server_error"}, request_id, path, started, event, subevent, variant, "not_configured")

        row = evonex.build_row(
            path[:200], payload, raw_text, variant,
            datetime.now(timezone.utc).isoformat(),
        )
        stored = await asyncio.to_thread(evonex.write_row_with_retry, row)
        if not stored:
            return _respond(500, {"error": "server_error"}, request_id, path, started, event, subevent, variant, "write_failed")

        return _respond(200, {}, request_id, path, started, event, subevent, variant)

    except Exception as exc:
        # No stack trace in the response; only the exception type is logged.
        logger.error("request_id=%s unhandled %s", request_id, type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": "server_error"})
