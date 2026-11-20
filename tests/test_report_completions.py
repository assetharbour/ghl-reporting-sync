"""Introducer report completions count Completion and Post Completion (20 Nov 2026)."""
from datetime import date

from app.report_data import introducer_report_data


def test_post_completion_counts_and_expected_dates_do_not():
    rows = [
        {"lead_source": "Alpha", "created_date": "2026-10-02", "pipeline_stage": "Completion"},
        {"lead_source": "Alpha", "created_date": "2026-10-03", "pipeline_stage": "Post Completion"},
        # an expected completion date on a case still at Exchange is not a completion
        {"lead_source": "Alpha", "created_date": "2026-10-04", "pipeline_stage": "Exchange", "completion_date": "2026-10-30"},
        {"lead_source": "Alpha", "created_date": "2026-10-05", "pipeline_stage": "Lead Received"},
    ]
    stats = introducer_report_data(rows, date(2026, 10, 1), date(2026, 10, 31))["stats"]
    assert stats[0]["completions"] == 2
    assert stats[0]["conv"] == 0.5
