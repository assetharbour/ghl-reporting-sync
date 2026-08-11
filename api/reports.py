"""Introducer report generation endpoints (weekly/monthly PDFs)."""

import logging

from fastapi import FastAPI

logger = logging.getLogger(__name__)

app = FastAPI()


@app.get("/api/reports/test-weasyprint")
async def test_weasyprint():
    """Temporary diagnostic — confirms whether weasyprint's system
    dependencies (Cairo/Pango/GDK-Pixbuf) are actually present in Vercel's
    Python runtime. Removed once the PDF library decision is made."""
    try:
        from weasyprint import HTML

        pdf_bytes = HTML(string="<h1>Test</h1><p>weasyprint works</p>").write_pdf()
        return {
            "status": "success",
            "pdf_bytes_generated": len(pdf_bytes),
        }
    except Exception as e:
        return {
            "status": "failed",
            "error_type": type(e).__name__,
            "error": str(e),
        }
