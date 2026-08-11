"""Builds the Introducer Performance PDF. reportlab, not weasyprint --
weasyprint's system dependencies (Cairo/Pango) are not present in Vercel's
Python runtime, confirmed by a live test (OSError: cannot load library
'pango-1.0-0')."""

import io
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    NextPageTemplate,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

BRAND_GREEN = colors.HexColor("#6DA544")
BRAND_NAVY = colors.HexColor("#2E3A48")
BRAND_PINK = colors.HexColor("#E91E63")
PAGE_BG = colors.HexColor("#F7F9FB")
CARD_BG = colors.HexColor("#FFFFFF")
INK = colors.HexColor("#1F2937")
MUTED = colors.HexColor("#6B7280")
LINE = colors.HexColor("#E5EAF0")

LOGO_PATH = os.path.join(
    os.path.dirname(__file__), "..", "assets", "assetharbour-logo.png"
)

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
HEADER_H = 32 * mm
FOOTER_H = 12 * mm

STYLES = {
    "title": ParagraphStyle(
        "title", fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=colors.white
    ),
    "subtitle": ParagraphStyle(
        "subtitle", fontName="Helvetica", fontSize=10.5, leading=13, textColor=colors.HexColor("#C7DDB5")
    ),
    "section": ParagraphStyle(
        "section", fontName="Helvetica-Bold", fontSize=12.5, leading=15, textColor=BRAND_NAVY,
        spaceBefore=2, spaceAfter=6,
    ),
    "stat_label": ParagraphStyle(
        "stat_label", fontName="Helvetica", fontSize=8, leading=10, textColor=MUTED
    ),
    "stat_value": ParagraphStyle(
        "stat_value", fontName="Helvetica-Bold", fontSize=21, leading=24, textColor=BRAND_NAVY
    ),
    "stat_value_alert": ParagraphStyle(
        "stat_value_alert", fontName="Helvetica-Bold", fontSize=21, leading=24, textColor=BRAND_PINK
    ),
    # Smaller than stat_value on purpose — a business name doesn't fit the
    # same 21pt used for a short number, and forcing it there breaks mid-
    # word (e.g. "Whittingto/n") instead of wrapping between words.
    "stat_value_name": ParagraphStyle(
        "stat_value_name", fontName="Helvetica-Bold", fontSize=12.5, leading=15, textColor=BRAND_NAVY
    ),
    "body": ParagraphStyle(
        "body", fontName="Helvetica", fontSize=10, leading=14.5, textColor=INK
    ),
    "table_header": ParagraphStyle(
        "table_header", fontName="Helvetica-Bold", fontSize=8.5, leading=10, textColor=colors.white
    ),
    "table_cell": ParagraphStyle(
        "table_cell", fontName="Helvetica", fontSize=9, leading=11, textColor=INK
    ),
    "table_cell_bold": ParagraphStyle(
        "table_cell_bold", fontName="Helvetica-Bold", fontSize=9, leading=11, textColor=INK
    ),
    "footer": ParagraphStyle(
        "footer", fontName="Helvetica", fontSize=7.5, leading=9, textColor=MUTED
    ),
}


def _fmt_currency(v):
    if v is None:
        return None
    return f"£{v:,.0f}"


def _fmt_pct(numerator, denominator):
    if not denominator:
        return "—"
    return f"{(numerator / denominator) * 100:.1f}%"


def build_highlights(data: dict, period_label: str) -> list:
    """Short, number-backed lines. Every sentence traces to a real value in
    `data` -- no template filler, no unbacked qualitative claims."""
    lines = []
    top = data["top_introducer"]
    top_leads = data["top_introducer_leads"]
    total_leads = data["total_leads"]

    if top and total_leads:
        share = (top_leads / total_leads) * 100
        lines.append(
            f"{top} sent the most leads this period: {top_leads} of {total_leads} total, {share:.0f}% of the period's volume."
        )
    elif total_leads:
        lines.append(f"{total_leads} leads were logged this period, none with a recorded introducer.")
    else:
        lines.append("No leads were logged for this period.")

    eligible = [s for s in data["stats"] if s["count"] >= 3]
    if eligible:
        best = max(eligible, key=lambda s: s["conv"])
        if best["conv"] > 0:
            lines.append(
                f"{best['source']} had the best conversion rate among introducers with 3 or more leads: "
                f"{best['conv'] * 100:.0f}% of {best['count']} leads reached completion."
            )

    if data["total_revenue"] is not None:
        lines.append(
            f"Recorded revenue for the period: {_fmt_currency(data['total_revenue'])} "
            f"across {data['total_revenue_recorded']} of {total_leads} cases."
        )

    return lines


def _header_footer(canvas, doc, title, period_label):
    canvas.saveState()

    # Page background
    canvas.setFillColor(PAGE_BG)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # Navy header band
    canvas.setFillColor(BRAND_NAVY)
    canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)

    if os.path.exists(LOGO_PATH):
        logo_h = 11 * mm
        logo_w = logo_h * (1665 / 504)
        canvas.drawImage(
            LOGO_PATH,
            MARGIN,
            PAGE_H - HEADER_H + (HEADER_H - logo_h) / 2 + 3 * mm,
            width=logo_w,
            height=logo_h,
            mask="auto",
        )
        text_y_title = PAGE_H - HEADER_H + 9.5 * mm
        text_y_sub = PAGE_H - HEADER_H + 5 * mm
    else:
        text_y_title = PAGE_H - HEADER_H + 9.5 * mm
        text_y_sub = PAGE_H - HEADER_H + 5 * mm

    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 15)
    canvas.drawString(MARGIN, text_y_title, title)
    canvas.setFillColor(colors.HexColor("#C7DDB5"))
    canvas.setFont("Helvetica", 10)
    canvas.drawString(MARGIN, text_y_sub, period_label)

    # Footer
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(MARGIN, FOOTER_H - 6 * mm, "Asset Harbour Mortgages & Protection Ltd")
    canvas.drawRightString(PAGE_W - MARGIN, FOOTER_H - 6 * mm, f"Page {doc.page}")

    canvas.restoreState()


def _stat_block(label, value, alert=False, name_style=False):
    if name_style:
        style = STYLES["stat_value_name"]
    else:
        style = STYLES["stat_value_alert"] if alert else STYLES["stat_value"]
    t = Table(
        [[Paragraph(label.upper(), STYLES["stat_label"])], [Paragraph(value, style)]],
        colWidths=[None],
    )
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, LINE),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return t


def generate_introducer_report_pdf(data: dict, report_type: str, period_label: str) -> bytes:
    """report_type: 'weekly' or 'monthly'. Returns PDF bytes."""
    title = "Introducer Performance"
    buf = io.BytesIO()

    content_w = PAGE_W - 2 * MARGIN
    frame = Frame(
        MARGIN,
        FOOTER_H,
        content_w,
        PAGE_H - HEADER_H - FOOTER_H - 6 * mm,
        id="body",
        topPadding=8 * mm,
    )

    def on_page(canvas, doc):
        _header_footer(canvas, doc, title, period_label)

    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=HEADER_H,
        bottomMargin=FOOTER_H,
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=on_page)])

    story = []

    # Summary stats row
    stat_cells = [
        _stat_block("Total Leads", str(data["total_leads"])),
        _stat_block("Introducers", str(data["total_introducers"])),
    ]
    if data["total_revenue"] is not None:
        stat_cells.append(_stat_block("Recorded Revenue", _fmt_currency(data["total_revenue"])))
    else:
        stat_cells.append(_stat_block("Recorded Revenue", "—"))
    top_introducer_display = data["top_introducer"] or "—"
    stat_cells.append(_stat_block("Top Introducer", top_introducer_display, name_style=True))

    # Unequal widths: the first 3 cards hold short numbers, the last holds
    # a business name that can run long — give it noticeably more room so
    # it wraps between words instead of needing a tiny font to fit.
    stat_widths = [content_w * 0.21, content_w * 0.19, content_w * 0.22, content_w * 0.38]
    stats_table = Table([stat_cells], colWidths=stat_widths)
    stats_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(stats_table)
    story.append(Spacer(1, 8 * mm))

    # Highlights
    story.append(Paragraph("Highlights", STYLES["section"]))
    for line in build_highlights(data, period_label):
        story.append(Paragraph(line, STYLES["body"]))
        story.append(Spacer(1, 2))
    story.append(Spacer(1, 6 * mm))

    # Ranked introducer table
    story.append(Paragraph("Introducers, ranked by leads", STYLES["section"]))
    header_row = [
        Paragraph(h, STYLES["table_header"])
        for h in ["Introducer", "Leads", "Won", "Lost", "Open", "Conversion", "Revenue"]
    ]
    table_rows = [header_row]
    for s in data["stats"]:
        revenue_text = _fmt_currency(s["revenue_sum"]) if s["revenue_sum"] is not None else "—"
        table_rows.append(
            [
                Paragraph(s["source"], STYLES["table_cell_bold"]),
                Paragraph(str(s["count"]), STYLES["table_cell"]),
                Paragraph(str(s["won"]), STYLES["table_cell"]),
                Paragraph(str(s["lost"]), STYLES["table_cell"]),
                Paragraph(str(s["open"]), STYLES["table_cell"]),
                Paragraph(_fmt_pct(s["completions"], s["count"]), STYLES["table_cell"]),
                Paragraph(revenue_text, STYLES["table_cell"]),
            ]
        )

    if len(table_rows) == 1:
        table_rows.append(
            [Paragraph("No leads recorded for this period.", STYLES["table_cell"])] + [Paragraph("", STYLES["table_cell"])] * 6
        )

    col_widths = [
        content_w * 0.28,
        content_w * 0.10,
        content_w * 0.10,
        content_w * 0.10,
        content_w * 0.10,
        content_w * 0.14,
        content_w * 0.18,
    ]
    intro_table = Table(table_rows, colWidths=col_widths, repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_NAVY),
        ("BOX", (0, 0), (-1, -1), 0.75, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]
    for i in range(1, len(table_rows)):
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FAFBFC")))
        else:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), CARD_BG))
    intro_table.setStyle(TableStyle(style_cmds))
    story.append(intro_table)

    doc.build(story)
    return buf.getvalue()
