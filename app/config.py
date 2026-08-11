"""Central configuration. Loads env vars and fails fast if any are missing."""

import os

from dotenv import load_dotenv

load_dotenv()

_REQUIRED_VARS = [
    "GHL_PRIVATE_TOKEN",
    "GHL_LOCATION_ID",
    "GHL_PIPELINE_ID",
    "GOOGLE_SHEET_ID",
    "CRON_SECRET",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
]

_missing = [name for name in _REQUIRED_VARS if not os.getenv(name)]
if _missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(_missing)}. "
        "Set them in .env (local) or Vercel project settings (production)."
    )

GHL_PRIVATE_TOKEN = os.environ["GHL_PRIVATE_TOKEN"]
GHL_LOCATION_ID = os.environ["GHL_LOCATION_ID"]
GHL_PIPELINE_ID = os.environ["GHL_PIPELINE_ID"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CRON_SECRET = os.environ["CRON_SECRET"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

GHL_BASE_URL = "https://services.leadconnectorhq.com"
GHL_HEADERS = {
    "Authorization": f"Bearer {GHL_PRIVATE_TOKEN}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
}

# GHL custom value IDs — confirmed live via /locations/{id}/customValues.
WEEKLY_REPORT_VALUE_ID = "Kr3JvQdXgUr1OGHnV45g"
MONTHLY_REPORT_VALUE_ID = "DeGLq4rXKreKLLGPVpb3"

# assetharbour-reports.vercel.app — a project domain registered via
# `vercel domains add` (not a raw `vercel alias set`, which does NOT
# exempt a hostname from Vercel Authentication/SSO — confirmed live,
# the raw alias 302'd to vercel.com/sso-api until it was added as a
# proper project domain). Chosen over the raw ghl-reporting-sync-sage
# alias so a client-facing PDF link doesn't expose internal repo/tool
# naming. GHL's email link needs a URL that resolves for an
# unauthenticated recipient — confirmed with a direct curl (real 200
# JSON, no redirect).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://assetharbour-reports.vercel.app")
