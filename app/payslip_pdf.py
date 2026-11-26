"""Renders a payslip PDF from a resolved slip detail dict (see
crud.get_salary_slip_detail) and the company's one common payslip
template (see crud.get_payslip_template). Demo-quality layout via
reportlab -- not a pixel-perfect statutory form, just a legible, real
downloadable artifact where none existed before."""

import calendar
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


def render_payslip_pdf(slip_detail: dict, template: dict, company_name: str) -> bytes:
    slip = slip_detail["slip"]
    run = slip_detail["run"]
    employee = slip_detail["employee"]
    lines = slip_detail["lines"]

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PayslipTitle", parent=styles["Heading1"], fontSize=16, spaceAfter=2)
    sub_style = ParagraphStyle("PayslipSub", parent=styles["Normal"], fontSize=10, textColor=colors.grey)
    section_style = ParagraphStyle("SectionHeading", parent=styles["Heading3"], fontSize=11, spaceBefore=10, spaceAfter=4)
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    story = []

    period_label = f"{calendar.month_name[run.period_month]} {run.period_year}" if run else "—"
    employee_name = f"{employee.first_name} {employee.last_name or ''}".strip() if employee else "—"

    story.append(Paragraph(company_name, title_style))
    story.append(Paragraph(f"Payslip for {period_label}", sub_style))
    story.append(Spacer(1, 8))

    header_rows = [
        ["Employee Name", employee_name, "Employee Code", employee.employee_code if employee else "—"],
        ["Pay Period", period_label, "Status", (slip.status or "—").title()],
        ["Working Days", str(slip.working_days), "LOP Days", str(slip.lop_days)],
    ]
    header_table = Table(header_rows, colWidths=[35 * mm, 55 * mm, 35 * mm, 55 * mm])
    header_table.setStyle(
        TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ])
    )
    story.append(header_table)

    section_labels = (template or {}).get("section_labels", {})
    sections = [
        ("earning", section_labels.get("earning", "Earnings")),
        ("deduction", section_labels.get("deduction", "Deductions")),
        ("tax", section_labels.get("tax", "Tax")),
        ("benefit", section_labels.get("benefit", "Benefits (Employer Cost)")),
    ]

    for component_type, label in sections:
        rows = [(l["name"], l["amount"]) for l in lines if l["component_type"] == component_type]
        if not rows:
            continue
        story.append(Paragraph(label, section_style))
        table_data = [["Component", "Amount (₹)"]] + [
            [name, f"{amount:,.2f}"] for name, amount in rows
        ]
        table = Table(table_data, colWidths=[100 * mm, 60 * mm])
        table.setStyle(
            TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.lightgrey),
            ])
        )
        story.append(table)

    # Approved Travel/Expense reimbursements (separate from earnings).
    reimbursements = slip_detail.get("reimbursements") or []
    if reimbursements:
        story.append(Paragraph("Reimbursements", section_style))
        table_data = [["Component", "Reference", "Amount (₹)"]] + [
            [r["label"], r.get("reference") or "—", f"{r['amount']:,.2f}"] for r in reimbursements
        ]
        table = Table(table_data, colWidths=[60 * mm, 50 * mm, 50 * mm])
        table.setStyle(
            TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.lightgrey),
            ])
        )
        story.append(table)
    reimbursements_total = sum(r["amount"] for r in reimbursements)

    story.append(Spacer(1, 10))
    summary_rows = [
        ["Gross Pay", f"₹{float(slip.gross_pay):,.2f}"],
        ["Total Deductions", f"₹{float(slip.total_deductions):,.2f}"],
        ["Net Pay", f"₹{float(slip.net_pay):,.2f}"],
    ]
    if reimbursements:
        summary_rows += [
            ["Reimbursements", f"₹{reimbursements_total:,.2f}"],
            ["Total Payable", f"₹{float(slip.net_pay) + reimbursements_total:,.2f}"],
        ]
    summary_table = Table(summary_rows, colWidths=[100 * mm, 60 * mm])
    summary_table.setStyle(
        TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    story.append(summary_table)

    footer_text = (template or {}).get("footer_text")
    if footer_text:
        story.append(Spacer(1, 16))
        story.append(Paragraph(footer_text, footer_style))

    doc.build(story)
    return buf.getvalue()
