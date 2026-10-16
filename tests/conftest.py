"""Test setup. Dummy env values are set BEFORE app.config is imported:
python-dotenv never overrides variables that are already set, so the real
.env (live GHL token, service account, Evonex token) can never leak into tests."""

import os

os.environ.update(
    {
        "GHL_PRIVATE_TOKEN": "dummy",
        "GHL_LOCATION_ID": "dummy",
        "GHL_PIPELINE_ID": "dummy",
        "GOOGLE_SHEET_ID": "dummy-leads-sheet",
        "CRON_SECRET": "dummy",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
        "PUBLIC_BASE_URL": "https://assetharbour-reports.vercel.app",
        "EVONEX_WEBHOOK_TOKEN": "dummy-evonex-token",
        "EVONEX_SHEET_ID": "dummy-evonex-sheet",
    }
)
