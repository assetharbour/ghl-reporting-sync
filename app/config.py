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
