"""Introducer report generation: weekly and monthly PDFs.

PDFs are not persisted to disk or object storage -- Vercel's Python
functions have no filesystem that survives between invocations. Instead
each PDF path is dated (e.g. /api/reports/weekly/2026-08-03.pdf) and the
GET handler deterministically regenerates that exact period's report on
every request, reading the same Leads tab the dashboard reads. Two
requests for the same dated path produce byte-identical output (barring
the Leads tab itself changing), so the URL is stable enough for GHL's
email link even though nothing is cached.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response

from app import config, ghl_client, report_data, report_pdf, sheets_client

logger = logging.getLogger(__name__)

app = FastAPI()


def _month_end(start: date) -> date:
    next_month = start.replace(day=28) + timedelta(days=4)
    return next_month.replace(day=1) - timedelta(days=1)


def _weekly_period_label(start: date, end: date) -> str:
    return f"Week of {start.strftime('%d %B').lstrip('0')} to {end.strftime('%d %B %Y').lstrip('0')}"


def _monthly_period_label(start: date) -> str:
    return start.strftime("%B %Y")


def _render_weekly_pdf(period_start: date) -> bytes:
    period_end = period_start + timedelta(days=6)
    rows = sheets_client.SheetsClient().get_all_leads()
    data = report_data.introducer_report_data(rows, period_start, period_end)
    return report_pdf.generate_introducer_report_pdf(
        data, "weekly", _weekly_period_label(period_start, period_end)
    )


def _render_monthly_pdf(period_start: date) -> bytes:
    period_end = _month_end(period_start)
    rows = sheets_client.SheetsClient().get_all_leads()
    data = report_data.introducer_report_data(rows, period_start, period_end)
    return report_pdf.generate_introducer_report_pdf(
        data, "monthly", _monthly_period_label(period_start)
    )


@app.get("/api/reports/weekly/{period_start}.pdf")
async def get_weekly_pdf(period_start: str):
    try:
        start = datetime.strptime(period_start, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="period_start must be YYYY-MM-DD")
    pdf_bytes = _render_weekly_pdf(start)
    return Response(content=pdf_bytes, media_type="application/pdf")


@app.get("/api/reports/monthly/{period_start}.pdf")
async def get_monthly_pdf(period_start: str):
    try:
        start = datetime.strptime(period_start, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="period_start must be YYYY-MM-DD")
    pdf_bytes = _render_monthly_pdf(start)
    return Response(content=pdf_bytes, media_type="application/pdf")


async def _generate_and_publish(report_type: str, start: date, end: date, pdf_url: str, value_id: str):
    """Background task: build the PDF once (to get real numbers for the
    archive row), archive it, then point the GHL custom value at the
    stable dated URL. Must never raise unhandled -- errors are logged so
    the deployment's function logs are the source of truth for a failed
    background run (mirrors do_full_sync's never-raise contract)."""
    try:
        sheets = sheets_client.SheetsClient()
        rows = sheets.get_all_leads()
        data = report_data.introducer_report_data(rows, start, end)

        # Render once now purely to validate it builds without error before
        # publishing the URL -- the GET endpoint above re-renders on each
        # actual fetch, this call's bytes are discarded.
        label = (
            _weekly_period_label(start, end) if report_type == "weekly" else _monthly_period_label(start)
        )
        report_pdf.generate_introducer_report_pdf(data, report_type, label)

        sheets.append_report_archive(
            {
                "report_type": report_type,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "pdf_url": pdf_url,
                "total_leads": data["total_leads"],
                "total_introducers": data["total_introducers"],
                "top_introducer": data["top_introducer"] or "",
                "top_introducer_leads": data["top_introducer_leads"],
            }
        )

        ghl = ghl_client.GHLClient()
        value_name = (
            "weekly_introducer_report_url" if report_type == "weekly" else "monthly_introducer_report_url"
        )
        await ghl.update_custom_value(value_id, value_name, pdf_url)

        logger.info("%s report published for %s to %s: %s", report_type, start, end, pdf_url)
    except Exception:
        logger.exception("%s report generation failed for period starting %s", report_type, start)


@app.post("/api/reports/weekly")
async def run_weekly_report(background_tasks: BackgroundTasks, x_cron_secret: str = Header(None)):
    if x_cron_secret != config.CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    start, end = report_data.most_recent_completed_week()
    pdf_url = f"{config.PUBLIC_BASE_URL}/api/reports/weekly/{start.isoformat()}.pdf"

    background_tasks.add_task(
        _generate_and_publish, "weekly", start, end, pdf_url, config.WEEKLY_REPORT_VALUE_ID
    )
    return {"status": "accepted", "period_start": start.isoformat(), "period_end": end.isoformat()}


@app.post("/api/reports/monthly")
async def run_monthly_report(background_tasks: BackgroundTasks, x_cron_secret: str = Header(None)):
    if x_cron_secret != config.CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    start, end = report_data.most_recent_completed_month()
    pdf_url = f"{config.PUBLIC_BASE_URL}/api/reports/monthly/{start.isoformat()}.pdf"

    background_tasks.add_task(
        _generate_and_publish, "monthly", start, end, pdf_url, config.MONTHLY_REPORT_VALUE_ID
    )
    return {"status": "accepted", "period_start": start.isoformat(), "period_end": end.isoformat()}
