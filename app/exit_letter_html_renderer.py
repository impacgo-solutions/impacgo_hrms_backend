"""Experience & Relieving Letter placeholder mapping on top of the shared
template_rendering engine -- the exit-letter sibling of
offer_letter_html_renderer.py / payslip_html_renderer.py, sharing the same
validate / render / HTML-to-PDF pipeline.

Every value comes from real records (the employee, their approved exit
request, company, template signatory) -- never sample data. Values are raw;
escaping happens once in render_exit_letter_html.
"""

from __future__ import annotations

import datetime
import html
from decimal import Decimal
from types import SimpleNamespace

from . import template_rendering as tr

# One combined letter: the employee's service certificate (experience) and
# release confirmation (relieving) in a single document.
# The Full & Final Settlement Statement shares the same template /
# generation / history machinery.
LETTER_TYPES = {
    "experience_relieving": "Experience & Relieving Letter",
    "fnf_statement": "Full & Final Settlement Statement",
}

# Reference number prefix (EXR|FNF/<code>/<year>/<version>).
_NUMBER_PREFIX = {"experience_relieving": "EXR", "fnf_statement": "FNF"}

# Pre-rendered HTML fragments (already escaped) -- inserted verbatim.
VERBATIM_PLACEHOLDERS = ("earnings_rows", "deductions_rows")


def _d(value: datetime.date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


def _long(value: datetime.date | None) -> str:
    return f"{value.day} {value.strftime('%B %Y')}" if value else "—"


def _tenure(start: datetime.date | None, end: datetime.date | None) -> str:
    if not start or not end or end < start:
        return "—"
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    years, months = divmod(max(months, 0), 12)
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if months or not years:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    return " ".join(parts)


def _name(employee) -> str:
    if employee is None:
        return "—"
    return f"{employee.first_name} {employee.last_name or ''}".strip()


def format_inr(value) -> str:
    """₹1,23,456.00 -- Indian digit grouping."""
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    whole, _, fraction = f"{abs(amount):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return f"{sign}₹{whole}.{fraction}"


_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven",
         "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _words_below_1000(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def amount_in_words(value) -> str:
    """Indian system: 'Rupees One Lakh Twenty Three Thousand Four Hundred
    Fifty Six and Fifty Paise Only'."""
    amount = abs(Decimal(str(value or 0)).quantize(Decimal("0.01")))
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    if rupees == 0 and paise == 0:
        return "Rupees Zero Only"
    parts = []
    for size, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        if rupees >= size:
            chunk = rupees // size
            parts.append(f"{amount_in_words(chunk)[7:-5] if chunk >= 1000 else _words_below_1000(chunk)} {label}")
            rupees %= size
    if rupees:
        parts.append(_words_below_1000(rupees))
    text = "Rupees " + " ".join(parts) if parts else "Rupees Zero"
    if paise:
        text += f" and {_words_below_1000(paise)} Paise"
    return text + " Only"


def _line_rows(lines) -> str:
    rows = []
    for line in lines:
        description = (
            f'<div class="line-desc">{html.escape(line.description)}</div>' if line.description else ""
        )
        rows.append(
            f'<tr><td>{html.escape(line.component)}{description}</td>'
            f'<td class="amount">{html.escape(format_inr(line.amount))}</td></tr>'
        )
    return "".join(rows) or '<tr><td class="none" colspan="2">None</td></tr>'


def _fnf_placeholders(settlement) -> dict[str, str]:
    """Settlement figures exactly as recorded (all "—" when there is none)."""
    if settlement is None:
        dash = "—"
        return {
            "earnings_rows": '<tr><td class="none" colspan="2">None</td></tr>',
            "deductions_rows": '<tr><td class="none" colspan="2">None</td></tr>',
            "total_earnings": dash, "total_deductions": dash, "net_amount": dash,
            "net_amount_words": dash, "net_amount_label": "Net amount payable",
            "settlement_status": "Not prepared", "settlement_approved_by": dash,
            "settlement_approved_date": dash, "payment_date": dash, "payment_mode": dash,
            "payment_reference": dash, "payment_details": "", "settlement_notes": "",
        }
    lines = list(getattr(settlement, "lines", []) or [])
    net = Decimal(str(settlement.net_amount or 0))
    status = "approved" if settlement.status == "posted" else (settlement.status or "draft")
    paid = status == "paid"
    if paid:
        details = f"Paid on {_long(settlement.payment_date)}"
        if settlement.payment_mode:
            details += f" by {settlement.payment_mode}"
        if settlement.payment_reference:
            details += f" (Ref. {settlement.payment_reference})"
        details += "."
    elif status == "approved":
        details = (
            "This amount will be credited to the employee's registered bank account."
            if net >= 0 else "Please remit this amount to the company to complete the settlement."
        )
    else:
        details = ""
    return {
        "earnings_rows": _line_rows(l for l in lines if l.line_type == "earning"),
        "deductions_rows": _line_rows(l for l in lines if l.line_type == "deduction"),
        "total_earnings": format_inr(settlement.payable_amount),
        "total_deductions": format_inr(settlement.recovery_amount),
        "net_amount": format_inr(abs(net)),
        "net_amount_words": amount_in_words(net),
        "net_amount_label": (
            "Net amount payable to the employee" if net >= 0 else "Net amount recoverable from the employee"
        ),
        "settlement_status": {"paid": "Paid", "approved": "Approved"}.get(status, "Draft"),
        "settlement_approved_by": settlement.approved_by_name or "—",
        "settlement_approved_date": _long(settlement.approved_at.date()) if settlement.approved_at else "—",
        "payment_date": _long(settlement.payment_date) if paid else "—",
        "payment_mode": (settlement.payment_mode or "—") if paid else "—",
        "payment_reference": (settlement.payment_reference or "—") if paid else "—",
        "payment_details": details,
        "settlement_notes": settlement.notes or "",
    }


def letter_number(letter_type: str, employee, issue_date: datetime.date, version: int) -> str:
    code = (employee.employee_code if employee else None) or "EMP"
    return f"{_NUMBER_PREFIX[letter_type]}/{code}/{issue_date.year}/{version}"


def embed_letter_image(path: str | None) -> str:
    """A template's seal / signature image (a local /media/... upload path)
    as a data URL for <img src>; a transparent pixel when none is set, so
    the image slot simply stays blank (room for a hand signature / stamp)."""
    if not path or not path.startswith("/media/"):
        return tr.TRANSPARENT_PIXEL_DATA_URL
    return tr.resolve_company_logo(SimpleNamespace(logo_url=path))


# Image placeholders -- left out of the stored placeholder snapshot.
IMAGE_PLACEHOLDERS = ("company_logo", "company_seal", "authorized_signature")


def build_exit_letter_placeholders(
    detail: dict,
    company,
    letter_type: str,
    *,
    signatory_name: str | None,
    signatory_designation: str | None,
    issue_date: datetime.date,
    version: int,
    seal_image_path: str | None = None,
    signature_image_path: str | None = None,
) -> dict[str, str]:
    """Every placeholder available to the Experience & Relieving Letter and
    F&F Settlement Statement templates. [detail] is
    crud.get_exit_letter_detail's dict."""
    employee = detail["employee"]
    exit_request = detail["exit_request"]
    designation = detail.get("designation")

    gender = (getattr(employee, "gender", None) or "").strip().lower()
    salutation = {"male": "Mr.", "female": "Ms."}.get(gender, "")
    subject, obj, possessive = {
        "male": ("he", "him", "his"),
        "female": ("she", "her", "her"),
    }.get(gender, ("they", "them", "their"))

    joining = getattr(employee, "date_of_joining", None)
    last_day = exit_request.last_working_day

    # Full & final settlement -- stated exactly as recorded, never assumed.
    settlement = detail.get("settlement")
    settlement_status = (getattr(settlement, "status", None) or "").lower()
    if settlement_status == "paid":
        fnf_status, fnf_statement = "Completed", "The full and final settlement of all dues has been completed."
    elif settlement_status in ("posted", "approved"):
        fnf_status, fnf_statement = "Processed", "The full and final settlement of dues has been processed."
    else:
        fnf_status, fnf_statement = (
            "In process", "The full and final settlement of dues is being processed as per company policy."
        )

    registration = [
        f"CIN: {company.cin}" if getattr(company, "cin", None) else "",
        f"GSTIN: {company.gstin}" if getattr(company, "gstin", None) else "",
    ]
    address_parts = [
        getattr(company, "address_line1", None), getattr(company, "address_line2", None),
        getattr(company, "city", None), getattr(company, "state", None),
        getattr(company, "pincode", None), getattr(company, "country", None),
    ]

    return {
        "letter_title": LETTER_TYPES[letter_type],
        "letter_number": letter_number(letter_type, employee, issue_date, version),
        "issue_date": _d(issue_date),
        "issue_date_long": _long(issue_date),
        "current_date": _d(issue_date),
        "employee_name": _name(employee),
        "employee_first_name": employee.first_name if employee else "—",
        "employee_last_name": (employee.last_name if employee else None) or "",
        "employee_salutation": salutation,
        "employee_id": (employee.employee_code if employee else None) or "—",
        "pronoun_subject": subject,
        "pronoun_object": obj,
        "pronoun_possessive": possessive,
        "designation": designation.name if designation else "—",
        "date_of_joining": _d(joining),
        "date_of_joining_long": _long(joining),
        "resignation_date": _d(exit_request.resignation_date),
        "resignation_date_long": _long(exit_request.resignation_date),
        "last_working_day": _d(last_day),
        "last_working_day_long": _long(last_day),
        # Relieved at close of business on the last working day.
        "relieving_date": _d(last_day),
        "relieving_date_long": _long(last_day),
        "exit_approved_date": _d(exit_request.decided_at.date() if exit_request.decided_at else None),
        "tenure": _tenure(joining, last_day),
        "full_and_final_status": fnf_status,
        "full_and_final_statement": fnf_statement,
        "authorized_signatory": (signatory_name or "").strip() or "—",
        "authorized_signatory_designation": (signatory_designation or "").strip() or "—",
        "company_name": company.name if company else "—",
        "company_legal_name": (getattr(company, "legal_name", None) or (company.name if company else "")) or "—",
        "company_address": ", ".join(p for p in address_parts if p) or "—",
        "company_cin": getattr(company, "cin", None) or "",
        "company_gstin": getattr(company, "gstin", None) or "",
        # "CIN: ... | GSTIN: ..." with only the numbers on record ("" if none).
        "company_registration": "  |  ".join(r for r in registration if r),
        "company_logo_url": getattr(company, "logo_url", None) or "",
        "company_logo": tr.resolve_company_logo(company),
        "company_seal": embed_letter_image(seal_image_path),
        "authorized_signature": embed_letter_image(signature_image_path),
        "service_period": _tenure(joining, last_day),
        **_fnf_placeholders(settlement),
    }


def render_exit_letter_html(html_body: str, css_styles: str, placeholders: dict[str, str]) -> str:
    return tr.render_template_html(html_body, css_styles, placeholders, verbatim_keys=VERBATIM_PLACEHOLDERS)


validate_template_html = tr.validate_template_html
render_html_to_pdf = tr.render_html_to_pdf
