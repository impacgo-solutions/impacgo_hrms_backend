"""Renders one expense report as a PDF: employee + report details, every
expense row, taxes, advances, totals, approval status / history, the list of
attached records, and the receipts themselves appended after the summary
(images drawn on their own pages, PDF receipts merged in).

Core fonts (Helvetica) have no glyph for symbols such as the rupee sign, so
every amount is printed with its ISO 4217 code ("INR 1,200.00") -- never
converted, never a missing-glyph box."""

import datetime as dt
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_IMAGE_EXT = {".png", ".jpg", ".jpeg"}
_PAGE_W = A4[0] - 36 * mm
_ACCENT = colors.HexColor("#1f3a5f")


def _t(value) -> str:
    """Escaped text safe for a Paragraph; Latin-1 only (core font)."""
    if value is None or value == "":
        return "-"
    s = str(value).replace("\r", " ")
    s = s.encode("cp1252", "replace").decode("cp1252")  # unmappable chars -> '?'
    return escape(s).replace("\n", "<br/>")


def _money(amount, code: str) -> str:
    return f"{code} {float(amount or 0):,.2f}"


def _date(d) -> str:
    if not d:
        return "-"
    if isinstance(d, dt.datetime):
        return d.strftime("%d %b %Y, %H:%M")
    return d.strftime("%d %b %Y")


def _status(s: str | None) -> str:
    return (s or "-").replace("_", " ").title()


def _receipt_images(receipts):
    """(name, bytes) of image receipts -> reportlab Images scaled to the page."""
    out = []
    max_w, max_h = _PAGE_W, A4[1] - 70 * mm
    for name, data in receipts:
        try:
            img = Image(io.BytesIO(data))
            iw, ih = img.imageWidth, img.imageHeight
            scale = min(max_w / iw, max_h / ih, 1.0 if iw < max_w else max_w / iw)
            img.drawWidth, img.drawHeight = iw * scale, ih * scale
            out.append((name, img))
        except Exception:  # unreadable image: still listed in the attachments table
            continue
    return out


def render_expense_report_pdf(
    report,  # schemas.ExpenseReportOut
    *,
    company_name: str,
    history: list[dict],
    attachments: list[dict],  # {name, mime, size, line (1-based or None), data: bytes | None}
) -> bytes:
    code = report.currency_code or "INR"
    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Heading1"], fontSize=17, spaceAfter=2, textColor=_ACCENT)
    sub = ParagraphStyle("S", parent=styles["Normal"], fontSize=10, textColor=colors.grey)
    h = ParagraphStyle("H", parent=styles["Heading3"], fontSize=11, spaceBefore=12, spaceAfter=4, textColor=_ACCENT)
    cell = ParagraphStyle("C", parent=styles["Normal"], fontSize=8.5, leading=10.5)
    cellb = ParagraphStyle("CB", parent=cell, fontName="Helvetica-Bold")
    cellr = ParagraphStyle("CR", parent=cell, alignment=2)
    foot = ParagraphStyle("F", parent=styles["Normal"], fontSize=8, textColor=colors.grey)

    def P(v, st=cell):
        return Paragraph(_t(v), st)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=16 * mm, bottomMargin=16 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"Expense Report {report.series or report.reference or ''}".strip(), author=company_name,
    )
    story: list = [
        Paragraph(_t(company_name), title),
        Paragraph(f"Expense Report - {_t(report.series or report.reference)}", sub),
        Spacer(1, 8),
    ]

    # ── Employee + report details ──
    def kv(rows):
        data = [[P(a, cellb), P(b), P(c, cellb), P(d)] for a, b, c, d in rows]
        t = Table(data, colWidths=[30 * mm, 53 * mm, 32 * mm, _PAGE_W - 115 * mm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ]))
        return t

    story.append(Paragraph("Employee &amp; Report Details", h))
    story.append(kv([
        ("Employee", report.employee_name, "Employee Code", report.employee_code),
        ("Department", report.department_name, "Company", report.company_name or company_name),
        ("Report ID", report.series, "Reference", report.reference),
        ("Claim Date", _date(report.claim_date), "Posting Date", _date(report.posting_date)),
        ("Submitted", _date(report.submitted_at), "Currency", code),
        ("Project", report.project_name, "Cost Center", report.cost_center),
        ("Task", report.task_title, "Payable Account", report.payable_account),
        ("Remark", report.remark, "", ""),
    ]))
    if report.custom_fields:
        story.append(Spacer(1, 4))
        items = list(report.custom_fields.items())
        rows = [(items[i][0], items[i][1], *(items[i + 1] if i + 1 < len(items) else ("", ""))) for i in range(0, len(items), 2)]
        story.append(kv(rows))

    # ── Expense rows ──
    receipt_name = {}
    for a in attachments:
        if a.get("line"):
            receipt_name[a["line"]] = a["name"]
    head = [P(x, cellb) for x in ("#", "Date", "Category", "Description", "Receipt", f"Amount ({code})")]
    rows = [head]
    for i, it in enumerate(report.items, 1):
        r = it.receipt.file_name if getattr(it, "receipt", None) else receipt_name.get(i)
        rows.append([
            P(i), P(_date(it.expense_date)), P(it.category), P(it.description), P(r or "-"),
            P(f"{float(it.amount or 0):,.2f}" + ("" if (it.currency_code or code) == code else f" {it.currency_code}"), cellr),
        ])
    if len(rows) == 1:
        rows.append([P(""), P("No expense rows"), "", "", "", ""])
    t = Table(rows, colWidths=[8 * mm, 22 * mm, 28 * mm, _PAGE_W - 8 * mm - 22 * mm - 28 * mm - 36 * mm - 28 * mm, 36 * mm, 28 * mm],
              repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef6")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [Paragraph("Expenses", h), t]

    # ── Taxes ──
    if report.taxes:
        rows = [[P(x, cellb) for x in ("Account Head", "Description", "Rate", f"Tax ({code})")]]
        for x in report.taxes:
            rows.append([P(x.account_head), P(x.description), P(f"{x.rate:g}%" if x.rate is not None else "-"),
                         P(f"{float(x.tax_amount or 0):,.2f}", cellr)])
        tt = Table(rows, colWidths=[50 * mm, _PAGE_W - 50 * mm - 20 * mm - 32 * mm, 20 * mm, 32 * mm], repeatRows=1)
        tt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef6")),
                                ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [Paragraph("Taxes &amp; Charges", h), tt]

    # ── Advances ──
    if report.advances:
        rows = [[P(x, cellb) for x in ("Advance", "Purpose", "Posting Date", f"Adjusted ({code})")]]
        for x in report.advances:
            rows.append([P(str(x.advance_id)[:8].upper()), P(x.purpose), P(_date(x.posting_date)),
                         P(f"{float(x.allocated_amount or 0):,.2f}", cellr)])
        at = Table(rows, colWidths=[28 * mm, _PAGE_W - 28 * mm - 28 * mm - 32 * mm, 28 * mm, 32 * mm], repeatRows=1)
        at.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef6")),
                                ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [Paragraph("Advances Adjusted", h), at]

    # ── Totals ──
    tot = [
        [P("Total Claimed", cellb), P(_money(report.total_claimed, code), cellr)],
        [P("Taxes & Charges", cellb), P(_money(report.total_taxes, code), cellr)],
        [P("Less: Advances", cellb), P("- " + _money(report.advance_amount, code), cellr)],
        [P("Net Payable", cellb), P(_money(report.net_payable, code), ParagraphStyle("N", parent=cellr, fontName="Helvetica-Bold", fontSize=10))],
    ]
    totals = Table(tot, colWidths=[40 * mm, 45 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([("LINEABOVE", (0, 3), (-1, 3), 0.8, _ACCENT), ("TOPPADDING", (0, 0), (-1, -1), 2),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [Spacer(1, 8), KeepTogether(totals)]

    # ── Approval ──
    appr = [
        ("Status", _status(report.status), "Payment Status", report.payroll_status or ("Paid" if report.is_paid else "Not paid")),
        ("Approver", report.approver_name if report.approver_name != "—" else None,
         "Decided On", _date(report.decided_at)),
        ("Expense Approver", report.expense_approver_name, "Clearance Date", _date(report.clearance_date)),
        ("Decision Notes", report.decision_notes, "", ""),
    ]
    story += [Paragraph("Approval", h), kv(appr)]
    if history:
        rows = [[P(x, cellb) for x in ("When", "By", "Action", "Notes")]]
        for e in history:
            try:
                when = dt.datetime.fromisoformat(e["at"])
            except Exception:
                when = None
            rows.append([P(_date(when) if when else e.get("at")), P(e.get("actor")), P(e.get("action")), P(e.get("notes"))])
        ht = Table(rows, colWidths=[34 * mm, 40 * mm, 34 * mm, _PAGE_W - 108 * mm], repeatRows=1)
        ht.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef6")),
                                ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [Paragraph("Approval History", h), ht]

    # ── Attached records ──
    story.append(Paragraph("Attached Records", h))
    if attachments:
        rows = [[P(x, cellb) for x in ("#", "File", "Linked To", "Type", "Size")]]
        for i, a in enumerate(attachments, 1):
            size = a.get("size") or 0
            rows.append([
                P(i), P(a["name"]), P(f"Expense row {a['line']}" if a.get("line") else "Supporting document"),
                P(a.get("mime")), P(f"{size / 1024:,.0f} KB" if size else "-"),
            ])
        att = Table(rows, colWidths=[8 * mm, _PAGE_W - 8 * mm - 36 * mm - 34 * mm - 20 * mm, 36 * mm, 34 * mm, 20 * mm], repeatRows=1)
        att.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef6")),
                                 ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(att)
    else:
        story.append(Paragraph("No receipts or documents attached.", cell))

    story += [Spacer(1, 10), Paragraph(f"Generated on {_date(dt.datetime.now())}", foot)]

    # ── Image receipts: one per page, after the summary ──
    images = _receipt_images([
        (a["name"], a["data"]) for a in attachments
        if a.get("data") and a["name"].lower().rsplit(".", 1)[-1:] and "." + a["name"].lower().rsplit(".", 1)[-1] in _IMAGE_EXT
    ])
    from reportlab.platypus import PageBreak
    for name, img in images:
        story += [PageBreak(), Paragraph(f"Receipt - {_t(name)}", h), Spacer(1, 4), img]

    doc.build(story)
    main = buf.getvalue()

    # ── PDF receipts: merged in after the generated pages ──
    pdfs = [(a["name"], a["data"]) for a in attachments if a.get("data") and a["name"].lower().endswith(".pdf")]
    if not pdfs:
        return main
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for page in PdfReader(io.BytesIO(main)).pages:
        w.add_page(page)
    for _name, data in pdfs:
        try:
            for page in PdfReader(io.BytesIO(data)).pages:
                w.add_page(page)
        except Exception:
            continue  # corrupt / encrypted receipt: still listed above
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()
