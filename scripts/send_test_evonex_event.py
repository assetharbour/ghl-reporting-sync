#!/usr/bin/env python3
"""Send one signed test event to an Evonex webhook receiver.

The signature is computed exactly as the Evonex guide specifies:
    X-Hub-Signature-256 = "sha256=" + hex(HMAC-SHA256(key=token, msg=URL + body))

The token is read from an environment variable (never a CLI argument, so it
doesn't land in shell history) and is never printed.

Examples:
    export EVONEX_WEBHOOK_TOKEN=...
    python scripts/send_test_evonex_event.py https://host/api/evonex/call
    python scripts/send_test_evonex_event.py https://host/api/evonex/recordingready --event RECORDINGREADY
    python scripts/send_test_evonex_event.py https://host/api/evonex/call --unsigned --expect 403
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

DEFAULT_CONTEXT = {
    "RECORDINGREADY": (
        "Duration:245;FileSize:1024000;StartTime:2026-04-20T11:55:00;"
        "Url:https%3A%2F%2Fcontent.evonex.com%2Fcontent%2Frecording%2Ftest"
    ),
    "VOICEMAIL": (
        "Duration:18;LeftAt:2026-04-20T10:44:30;"
        "Url:https%3A%2F%2Fcontent.evonex.com%2Fcontent%2Fvm%2Ftest"
    ),
}


def sample_payload(event, subevent, cid, context=None):
    """Fictional payload (Ofcom drama-range numbers) built from the Appendix A field list."""
    return {
        "ts": "2026-04-20T13:00:00Z", "event": event, "subevent": subevent, "cid": cid,
        "lpi": "1001", "rpi": "+442075550000", "rpn": None, "opi": None,
        "context": context if context is not None else DEFAULT_CONTEXT.get(event),
        "sub": "1001", "mac": None, "pn": None, "fn": None, "ua": None,
        "on": "Test User", "un": "testuser", "oa": "Test Account", "od": "Test", "oj": "Test",
        "or": "User", "os": None, "oe": "test.user@example.com", "om": "+447700900000",
        "sbn": "Evonex", "lsn": "UK-DC1",
    }


def compute_signature(token, url, body):
    return "sha256=" + hmac.new(token.encode("utf-8"), (url + body).encode("utf-8"), hashlib.sha256).hexdigest()


def build_headers(token, url, body, bearer=False, signed=True):
    headers = {
        "Content-Type": "application/json",
        "Authorization": ("Bearer " + token) if bearer else token,
    }
    if signed:
        headers["X-Hub-Signature-256"] = compute_signature(token, url, body)
    return headers


def send(url, headers, body):
    request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="Full target URL, e.g. https://host/api/evonex/call")
    parser.add_argument("--event", default="OUTBOUNDCALL")
    parser.add_argument("--subevent", default=None)
    parser.add_argument("--cid", default=None, help="Call id (default: test-<random>)")
    parser.add_argument("--context", default=None)
    parser.add_argument("--payload-file", help="Send this JSON file's exact contents instead of a sample")
    parser.add_argument("--token-env", default="EVONEX_WEBHOOK_TOKEN", help="Env var holding the API token")
    parser.add_argument("--bearer", action="store_true", help="Send 'Authorization: Bearer <token>'")
    parser.add_argument("--unsigned", action="store_true", help="Omit X-Hub-Signature-256")
    parser.add_argument("--expect", type=int, default=200, help="Exit 0 only if this status comes back")
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"error: environment variable {args.token_env} is not set", file=sys.stderr)
        return 2

    if args.payload_file:
        with open(args.payload_file, encoding="utf-8") as handle:
            body = handle.read()
    else:
        subevent = args.subevent
        if subevent is None and args.event in ("OUTBOUNDCALL", "INBOUNDCALL"):
            subevent = "STARTCALL"
        cid = args.cid or "test-" + uuid.uuid4().hex[:8]
        body = json.dumps(sample_payload(args.event, subevent, cid, args.context), separators=(",", ":"))

    headers = build_headers(token, args.url, body, bearer=args.bearer, signed=not args.unsigned)
    status, text = send(args.url, headers, body)
    print(f"HTTP {status}")
    if text.strip():
        print(text.strip()[:300])
    return 0 if status == args.expect else 1


if __name__ == "__main__":
    sys.exit(main())
