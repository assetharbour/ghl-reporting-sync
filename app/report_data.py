"""Computes introducer performance stats for a given period, read from the
same Leads tab the dashboard reads. Mirrors ghl-dashboard's Introducers.jsx
logic exactly (conversion = reached the Completion pipeline stage, not
case_status == 'won', same rule the dashboard was corrected to use)."""

import re
from datetime import date, datetime, timedelta


def parse_date(s):
    if not s or not str(s).strip():
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_number(v):
    if v is None:
        return None
    s = str(v).strip().replace("£", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def most_recent_completed_week(reference=None):
    """Monday-Sunday of the most recently fully completed week."""
    today = reference or date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_sunday = this_monday - timedelta(days=1)
    last_monday = last_sunday - timedelta(days=6)
    return last_monday, last_sunday


def most_recent_completed_month(reference=None):
    """First-to-last day of the most recently fully completed calendar month."""
    today = reference or date.today()
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    first_day_prev_month = last_day_prev_month.replace(day=1)
    return first_day_prev_month, last_day_prev_month


def slugify_introducer(name: str) -> str:
    """Stable identifier for a lead_source value, used both in the GHL
    custom value name (so the merge tag stays the same week to week) and
    the dated PDF URL path. Collapses anything non-alphanumeric to a
    single underscore and caps length so long org names stay usable as a
    GHL custom value name / URL segment."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug[:60]


def introducer_report_data(rows: list, start: date, end: date) -> dict:
    """rows: list of dicts keyed by the Leads tab's header row (as returned
    by SheetsClient.get_all_leads()). Returns everything the PDF and the
    archive row need."""
    period_rows = []
    for r in rows:
        d = parse_date(r.get("created_date"))
        if d and start <= d <= end:
            period_rows.append(r)

    by_source = {}
    for r in period_rows:
        source = str(r.get("lead_source") or "").strip()
        if not source:
            continue
        by_source.setdefault(source, []).append(r)

    stats = []
    for source, leads in by_source.items():
        won = sum(1 for r in leads if r.get("case_status") == "won")
        lost = sum(1 for r in leads if r.get("case_status") == "lost")
        open_count = sum(1 for r in leads if r.get("case_status") == "open")
        # Conversion = reached the Completion pipeline stage, same
        # definition as the dashboard (Overview/Introducers/Advisor
        # Performance all use this, not case_status == 'won').
        completions = sum(1 for r in leads if r.get("pipeline_stage") == "Completion")
        revenue_vals = [
            v for r in leads if (v := parse_number(r.get("total_revenue"))) is not None
        ]
        stats.append(
            {
                "source": source,
                "count": len(leads),
                "won": won,
                "lost": lost,
                "open": open_count,
                "completions": completions,
                "conv": completions / len(leads) if leads else 0.0,
                "revenue_sum": sum(revenue_vals) if revenue_vals else None,
                "revenue_recorded": len(revenue_vals),
            }
        )
    stats.sort(key=lambda s: -s["count"])

    total_leads = len(period_rows)
    total_revenue_vals = [
        v for r in period_rows if (v := parse_number(r.get("total_revenue"))) is not None
    ]
    top = stats[0] if stats else None

    return {
        "start": start,
        "end": end,
        "stats": stats,
        "by_source": by_source,
        "total_leads": total_leads,
        "total_introducers": len(stats),
        "total_revenue": sum(total_revenue_vals) if total_revenue_vals else None,
        "total_revenue_recorded": len(total_revenue_vals),
        "top_introducer": top["source"] if top else None,
        "top_introducer_leads": top["count"] if top else 0,
    }
