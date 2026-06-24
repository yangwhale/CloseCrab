#!/usr/bin/env python3
"""Generate the APPENDIX-EQUITY computation PDF in the IRD-accepted format.

Consumes the JSON output of compute.py (taxpayer + rows + totals) and produces
a multi-page PDF mirroring the layout of past IRD-filed returns.

Page layout:
  - Per-page header: "APPENDIX-EQUITY" / "FILE NO: <file_no>" (top right)
                     "<TAXPAYER NAME>" / "SALARIES TAX - YEAR OF ASSESSMENT <YA>" (top left)
  - Page 1: Main table + grand totals + HK$ summary + methodology footnote
  - Pages 2-N: DIPN 38 detail blocks (one per apportioned vest)
  - Last page: Itinerary appendix (employment timeline + per-grant summary)

Usage:
    python gen_pdf.py --result result.json --output appendix_equity.pdf
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
    Table, TableStyle, PageBreak, KeepTogether,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.pdfgen import canvas


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(d) -> str:
    """Format like '25 Mar 2025' — IRD's house style."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


# ---------------------------------------------------------------------------
# Per-page header drawing
# ---------------------------------------------------------------------------


def make_header_drawer(taxpayer_name: str, year_of_assessment: str, file_no: str):
    """Build the onPage callback that draws the persistent page header."""
    def draw_header(canv: canvas.Canvas, doc):
        canv.saveState()
        page_w, page_h = A4
        canv.setFont("Helvetica-Bold", 10)
        canv.drawRightString(page_w - 1.5 * cm, page_h - 1.0 * cm, "APPENDIX-EQUITY")
        canv.drawRightString(page_w - 1.5 * cm, page_h - 1.0 * cm - 12,
                             f"FILE NO: {file_no}")
        canv.drawString(1.5 * cm, page_h - 1.6 * cm, taxpayer_name)
        canv.drawString(1.5 * cm, page_h - 1.6 * cm - 12,
                        f"SALARIES TAX - YEAR OF ASSESSMENT {year_of_assessment}")
        canv.restoreState()
    return draw_header


# ---------------------------------------------------------------------------
# Story builder
# ---------------------------------------------------------------------------


def build_story(result: dict):
    styles = getSampleStyleSheet()
    base = "Helvetica"
    bold = "Helvetica-Bold"

    taxpayer = result["taxpayer"]
    rows = result["rows"]
    totals = result["totals"]
    hk_start = date.fromisoformat(taxpayer["hk_employment_start"])
    hk_end_day_before = date.fromordinal(hk_start.toordinal() - 1)

    sty_section = ParagraphStyle(
        "section", parent=styles["Normal"],
        fontName=bold, fontSize=10, leading=14,
        spaceBefore=6, spaceAfter=8)
    sty_normal = ParagraphStyle(
        "normal", parent=styles["Normal"],
        fontName=base, fontSize=9, leading=11)
    sty_normal_bold = ParagraphStyle(
        "normal_b", parent=styles["Normal"],
        fontName=bold, fontSize=9, leading=11)
    sty_small = ParagraphStyle(
        "small", parent=styles["Normal"],
        fontName=base, fontSize=8, leading=10)
    sty_hdr = ParagraphStyle(
        "hdr", parent=sty_small, fontName=bold,
        alignment=TA_CENTER, leading=10)

    story = []
    story.append(Paragraph(
        "<u>COMPUTATION FOR INCOME ON SHARE AWARDS/UNITS</u>", sty_section))
    story.append(Paragraph(
        f"Start Date of Hong Kong employment:&nbsp;&nbsp;&nbsp;<b>{fmt_date(hk_start)}</b>",
        sty_normal))
    story.append(Spacer(1, 8))

    # ---------- Main table ----------
    col_letter_row = ["", "", "(A)", "(B)", "(C) = (A x B)", "", "", "", ""]
    header_row = [
        Paragraph("Grant<br/>date", sty_hdr),
        Paragraph("Vesting<br/>date", sty_hdr),
        Paragraph("Number of<br/>share award<br/>vested", sty_hdr),
        Paragraph("Open market<br/>value on<br/>vesting date", sty_hdr),
        Paragraph("Net amount<br/>(Foreign<br/>Amount)", sty_hdr),
        Paragraph("Exchange<br/>Rate", sty_hdr),
        Paragraph("Net amount<br/>(HKD<br/>Amount)", sty_hdr),
        Paragraph("Net Assessable<br/>income<br/>(HKD Amount)", sty_hdr),
        Paragraph("Note", sty_hdr),
    ]

    data = [col_letter_row, header_row]
    for r in rows:
        data.append([
            fmt_date(r["grant_date"]),
            fmt_date(r["vest_date"]),
            f"{r['shares']:.4f}",
            f"USD {r['fmv_usd']:,.4f}",
            f"USD {r['usd']:,.2f}",
            f"{r['fx_rate']:.6f}",
            f"{r['hkd']:,.2f}",
            f"{r['assessable_hkd']:,.2f}",
            r["note"],
        ])

    # Totals row
    data.append([
        "", "",
        f"{totals['shares']:.4f}",
        "", "", "",
        f"{totals['net_amount_hkd']:,.2f}",
        f"{totals['net_assessable_hkd']:,.2f}",
        "",
    ])

    col_widths = [1.7*cm, 1.7*cm, 1.9*cm, 2.2*cm, 2.2*cm,
                  1.7*cm, 2.1*cm, 2.4*cm, 1.1*cm]
    tbl = Table(data, colWidths=col_widths, repeatRows=2)
    n_data = len(rows)
    last_data_row = 2 + n_data - 1
    totals_row = last_data_row + 1

    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEADING", (0, 0), (-1, -1), 9.5),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Oblique"),
        ("ALIGN", (2, 0), (4, 0), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("TOPPADDING", (0, 0), (-1, 0), 1),
        ("LINEABOVE", (0, 1), (-1, 1), 0.5, colors.black),
        ("LINEBELOW", (0, 1), (-1, 1), 0.5, colors.black),
        ("VALIGN", (0, 1), (-1, 1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 3),
        ("TOPPADDING", (0, 1), (-1, 1), 3),
        ("ALIGN", (0, 2), (1, last_data_row), "CENTER"),
        ("ALIGN", (2, 2), (-2, last_data_row), "RIGHT"),
        ("ALIGN", (-1, 2), (-1, last_data_row), "CENTER"),
        ("BOTTOMPADDING", (0, 2), (-1, last_data_row), 1.5),
        ("TOPPADDING", (0, 2), (-1, last_data_row), 1.5),
        ("LINEABOVE", (2, totals_row), (2, totals_row), 0.5, colors.black),
        ("LINEABOVE", (6, totals_row), (7, totals_row), 0.5, colors.black),
        ("LINEBELOW", (6, totals_row), (7, totals_row), 0.5, colors.black),
        ("ALIGN", (2, totals_row), (2, totals_row), "RIGHT"),
        ("ALIGN", (6, totals_row), (7, totals_row), "RIGHT"),
        ("TOPPADDING", (0, totals_row), (-1, totals_row), 2),
        ("BOTTOMPADDING", (0, totals_row), (-1, totals_row), 2),
    ]))
    story.append(tbl)

    # ---------- HK$ summary row ----------
    summary_data = [
        ["", "", "", "", "", "HK$",
         f"{totals['net_amount_hkd_int']:,}",
         f"{totals['net_assessable_hkd_int']:,}", ""],
        ["", "", "", "", "(As per reported on Employer's Return)",
         "", "", "", ""],
    ]
    sum_tbl = Table(summary_data, colWidths=col_widths)
    sum_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (6, 0), (7, 0), bold),
        ("ALIGN", (5, 0), (5, 0), "RIGHT"),
        ("ALIGN", (6, 0), (7, 0), "RIGHT"),
        ("LINEBELOW", (6, 0), (7, 0), 1, colors.black),
        ("FONTNAME", (4, 1), (4, 1), "Helvetica-Oblique"),
        ("ALIGN", (4, 1), (4, 1), "RIGHT"),
        ("SPAN", (4, 1), (7, 1)),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    story.append(Spacer(1, 4))
    story.append(sum_tbl)

    # ---------- Methodology footnote ----------
    story.append(Spacer(1, 10))
    sty_methodology = ParagraphStyle(
        "methodology", parent=styles["Normal"],
        fontName=base, fontSize=8.5, leading=11,
        textColor=colors.HexColor("#333333"))
    story.append(Paragraph(
        f"<i><b>Note on methodology:</b> The income exclusion of HK$ "
        f"{totals['excluded_hkd_int']:,} (=&nbsp;HK$ {totals['net_amount_hkd_int']:,} "
        f"&minus; HK$ {totals['net_assessable_hkd_int']:,}) is computed under "
        "<b>DIPN No. 38 paragraphs 43&ndash;46</b> (share award benefits granted "
        "pursuant to a non-Hong Kong employment that vest after the employee's "
        "transfer of employment to Hong Kong), applying per-vesting-period "
        "day-count apportionment under <b>section 8(1A)(a) of the Inland Revenue "
        "Ordinance</b>. This is not the annual day-in-day-out apportionment "
        "method, because the apportionment is specific to each share award's "
        "individual vesting period (see Notes (1a)&ndash;(1ae) and Itinerary on the "
        "last page).</i>",
        sty_methodology))

    story.append(PageBreak())

    # ---------- DIPN section ----------
    story.append(Paragraph(
        "<u>COMPUTATION FOR INCOME ON SHARE AWARDS/UNITS</u>", sty_section))
    story.append(Paragraph("Notes:", sty_normal_bold))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "(1)&nbsp;&nbsp;As share awards/units were granted pursuant to a non "
        "Hong Kong employment and vested after the taxpayer's transfer of "
        "employment to "
        f"{taxpayer['hk_employer']}, gains attributable to Hong Kong services "
        "should be determined using the following formula in accordance with the "
        'Department Interpretation Practice Notes No. 38 ("DIPN No.38"):-',
        sty_normal))
    story.append(Spacer(1, 8))

    formula_data = [
        [Paragraph("Total number of days on employment in Hong Kong during the vesting period",
                   ParagraphStyle("f", parent=sty_small, alignment=TA_CENTER)),
         "X",
         Paragraph("<b>Gross Income</b>", sty_small)],
        [Paragraph("Total number of days in the vesting period",
                   ParagraphStyle("f2", parent=sty_small, alignment=TA_CENTER)),
         "", ""],
    ]
    f_tbl = Table(formula_data, colWidths=[12*cm, 1*cm, 4*cm])
    f_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEABOVE", (0, 0), (0, 0), 0.5, colors.black),
        ("LINEBELOW", (0, 0), (0, 0), 0.5, colors.black),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("SPAN", (1, 0), (1, 1)),
        ("SPAN", (2, 0), (2, 1)),
        ("VALIGN", (1, 0), (-1, 1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(f_tbl)
    story.append(Spacer(1, 12))

    sty_text = ParagraphStyle(
        "btext", parent=sty_normal, fontName=base, fontSize=9, leading=11,
        alignment=TA_LEFT)
    sty_num = ParagraphStyle(
        "bnum", parent=sty_normal, fontName=base, fontSize=9, leading=11,
        alignment=TA_RIGHT)
    sty_dt = ParagraphStyle(
        "bdate", parent=sty_normal, fontName=base, fontSize=9, leading=11,
        alignment=TA_CENTER)

    for r in rows:
        if not r["apportioned"]:
            continue
        note = r["note"]
        block_rows = [
            [Paragraph(f"<b>{note}</b>", sty_text),
             Paragraph("Total number of days in the vesting period", sty_text),
             Paragraph(fmt_date(r["grant_date"]), sty_dt),
             Paragraph(fmt_date(r["vest_date"]), sty_dt),
             Paragraph(f"{r['total_days']:,.2f}", sty_num)],
            ["",
             Paragraph("Total number of days spent outside Hong Kong", sty_text),
             Paragraph(fmt_date(r["grant_date"]), sty_dt),
             Paragraph(fmt_date(hk_end_day_before), sty_dt),
             Paragraph(f"{r['outside_days']:,.2f}", sty_num)],
            ["",
             Paragraph("Total number of days on employment in Hong Kong during the vesting period",
                       sty_text),
             "", "",
             Paragraph(f"{r['hk_days']:,.2f}", sty_num)],
            ["", "", "", "", ""],
            ["",
             Paragraph("Assessable income attributable to Hong Kong employment period",
                       sty_text),
             "", "", ""],
            ["",
             Paragraph(f"{r['hkd']:,.2f}&nbsp;X&nbsp;&nbsp;&nbsp;&nbsp;"
                       f"{r['hk_days']:,.2f}&nbsp;&nbsp;/&nbsp;&nbsp;&nbsp;&nbsp;"
                       f"{r['total_days']:,.2f}",
                       sty_text),
             "", "",
             Paragraph(f"{r['assessable_hkd']:,.2f}", sty_num)],
        ]
        block_widths = [1*cm, 9*cm, 2.4*cm, 2.4*cm, 2.2*cm]
        block_tbl = Table(block_rows, colWidths=block_widths)
        block_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEABOVE", (4, 2), (4, 2), 0.5, colors.black),
            ("LINEABOVE", (4, 5), (4, 5), 0.5, colors.black),
            ("LINEBELOW", (4, 5), (4, 5), 0.5, colors.black),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(KeepTogether([block_tbl, Spacer(1, 6)]))

    # ---------- Itinerary appendix ----------
    story.append(PageBreak())
    story.append(Paragraph(
        "<u>APPENDIX: ITINERARY &mdash; HONG KONG / NON-HONG KONG EMPLOYMENT</u>",
        sty_section))
    story.append(Paragraph(
        "Supporting information for the partial exemption claim under section "
        "8(1A)(a) of the Inland Revenue Ordinance and DIPN No. 38 paragraphs "
        "43&ndash;46. The relevant employment periods are as follows:",
        sty_normal))
    story.append(Spacer(1, 8))

    sty_it_cell = ParagraphStyle(
        "it_cell", parent=sty_normal, fontName=base, fontSize=9, leading=11,
        alignment=TA_LEFT)
    sty_it_cell_c = ParagraphStyle(
        "it_cell_c", parent=sty_normal, fontName=base, fontSize=9, leading=11,
        alignment=TA_CENTER)

    prior_label = (taxpayer["prior_employer"]
                   if taxpayer.get("prior_employer")
                   else "a non-Hong Kong entity")

    timeline = [
        [Paragraph("Period", sty_hdr),
         Paragraph("Employment status", sty_hdr),
         Paragraph("Source of employment", sty_hdr)],
        [Paragraph(f"Up to and including {fmt_date(hk_end_day_before)}", sty_it_cell_c),
         Paragraph(f"Employed by {prior_label}", sty_it_cell),
         Paragraph("Non-Hong Kong employment", sty_it_cell_c)],
        [Paragraph(f"From {fmt_date(hk_start)} onwards", sty_it_cell_c),
         Paragraph(f"Transferred to {taxpayer['hk_employer']}", sty_it_cell),
         Paragraph("Hong Kong employment", sty_it_cell_c)],
    ]
    t_timeline = Table(timeline, colWidths=[5*cm, 8*cm, 4.5*cm])
    t_timeline.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t_timeline)
    story.append(Spacer(1, 14))

    story.append(Paragraph(
        "<b>Apportionment treatment of each share award grant:</b>",
        sty_normal))
    story.append(Spacer(1, 6))

    # Group notes by grant_date
    by_grant: dict[str, dict] = {}
    for r in rows:
        gd = r["grant_date"]
        g = by_grant.setdefault(
            gd, {"outside": 0, "notes": [], "apportioned": False})
        g["outside"] = r["outside_days"]
        g["apportioned"] = r["apportioned"]
        if r["note"]:
            g["notes"].append(r["note"].strip("()"))

    grant_summary = [
        [Paragraph("Date of conditional grant", sty_hdr),
         Paragraph("Source of employment at grant", sty_hdr),
         Paragraph(f"Outside HK days<br/>(grant date &rarr; {fmt_date(hk_end_day_before)}, inclusive)",
                   sty_hdr),
         Paragraph("DIPN 38 apportionment applied?", sty_hdr)],
    ]
    for gd in sorted(by_grant.keys()):
        g = by_grant[gd]
        if g["apportioned"]:
            src = "Non-Hong Kong employment"
            outside = f"{g['outside']} days"
            applied = f"Yes (notes {', '.join(g['notes'])})"
        else:
            src = "Hong Kong employment"
            outside = "0 (n/a)"
            applied = "No (full amount assessable)"
        grant_summary.append([
            Paragraph(fmt_date(gd), sty_it_cell_c),
            Paragraph(src, sty_it_cell_c),
            Paragraph(outside, sty_it_cell_c),
            Paragraph(applied, sty_it_cell_c),
        ])

    t_grants = Table(grant_summary, colWidths=[3*cm, 4*cm, 4*cm, 6.5*cm])
    t_grants.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), base),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t_grants)
    story.append(Spacer(1, 14))

    story.append(Paragraph("<b>Notes:</b>", sty_normal_bold))
    story.append(Paragraph(
        f'1.&nbsp;&nbsp;"Outside HK days" is counted from the grant date to '
        f'{fmt_date(hk_end_day_before)} inclusive (i.e. up to the day before the '
        f'start of Hong Kong employment on {fmt_date(hk_start)}). For each '
        'individual vesting event, the same outside-HK day count is used in the '
        'DIPN 38 apportionment, as demonstrated in the Notes above.',
        sty_normal))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"2.&nbsp;&nbsp;Share awards granted on or after {fmt_date(hk_start)} "
        "were made under Hong Kong employment, so the full vesting income is "
        "fully assessable to Hong Kong salaries tax with no apportionment, in "
        "accordance with DIPN No. 38.",
        sty_normal))

    return story


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--result", type=Path, required=True,
                   help="Computation result JSON (from compute.py)")
    p.add_argument("--output", type=Path, required=True,
                   help="Output PDF path")
    args = p.parse_args()

    result = json.loads(args.result.read_text())
    taxpayer = result["taxpayer"]

    doc = BaseDocTemplate(
        str(args.output),
        pagesize=A4,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=2.5 * cm, bottomMargin=1.5 * cm,
        title=f"YA {taxpayer['year_of_assessment']} Stock Award Computation",
        author=taxpayer["name"],
    )
    frame = Frame(
        doc.leftMargin, doc.bottomMargin,
        doc.width, doc.height,
        showBoundary=0,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    template = PageTemplate(
        id="main", frames=[frame],
        onPage=make_header_drawer(
            taxpayer["name"], taxpayer["year_of_assessment"], taxpayer["file_no"]),
    )
    doc.addPageTemplates([template])

    story = build_story(result)
    doc.build(story)
    print(f"Wrote: {args.output}  ({args.output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
