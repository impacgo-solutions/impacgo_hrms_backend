"""Offer-letter-specific placeholder mapping on top of the shared
template_rendering engine (see that module's docstring for the full
rendering/security design) -- the Offer Letter Template's exact sibling of
payslip_html_renderer.py, sharing the identical validate/render/PDF
pipeline so both template types genuinely work the same way end to end.
"""

import datetime
import html
import re

from . import template_rendering as tr

# Placeholders whose value is pre-rendered (already-escaped) HTML.
VERBATIM_KEYS = ("compensation_terms",)

_RATE_UNIT_LABEL = {"hourly": "per hour", "daily": "per day", "monthly": "per month"}


def _terms_table(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><th style="text-align:left;padding:4px 12px 4px 0;font-weight:600">{html.escape(k)}</th>'
        f'<td style="padding:4px 0">{html.escape(v)}</td></tr>'
        for k, v in rows
    )
    return f'<table class="compensation-terms" style="border-collapse:collapse">{cells}</table>'


def _contract_placeholders(offer) -> dict[str, str]:
    """Contract-terms values for an offer whose employment_type is
    'contract' (compensation_type 'rate'); '—' for every other offer."""
    is_contract = (getattr(offer, "employment_type", None) or "").lower() == "contract"
    rate = getattr(offer, "rate_amount", None)
    unit = getattr(offer, "rate_unit", None)
    months = getattr(offer, "contract_duration_months", None)
    end = getattr(offer, "contract_end_date", None)
    rate_text = f"{float(rate):,.2f}" if is_contract and rate is not None else "—"
    unit_text = _RATE_UNIT_LABEL.get(unit or "", "—") if is_contract else "—"
    return {
        "is_contract": is_contract,
        "contract_rate": rate_text,
        "contract_rate_unit": unit_text,
        "contract_rate_in_words": tr.amount_in_words(float(rate)) if is_contract and rate is not None else "—",
        "contract_duration": f"{months} months" if is_contract and months else "—",
        "contract_end_date": end.isoformat() if is_contract and end else "—",
    }


def build_offer_letter_placeholders(offer_detail: dict, company) -> dict[str, str]:
    """Every placeholder key available to an Offer Letter Template,
    resolved from real backend data only (offer_detail from
    crud.get_offer_detail, company from core_companies) -- never mock/
    sample values. Raw (unescaped) strings/numbers; escaping happens once,
    uniformly, in render_offer_letter_html."""
    offer = offer_detail["offer"]
    application = offer_detail["application"]
    candidate = offer_detail["candidate"]
    opening = offer_detail["opening"]
    department = offer_detail["department"]
    designation = offer_detail["designation"]
    branch = offer_detail["branch"]
    reporting_manager = offer_detail["reporting_manager"]
    hr_contact = offer_detail["hr_contact"]

    def manager_name(employee) -> str:
        if employee is None:
            return "—"
        return f"{employee.first_name} {employee.last_name or ''}".strip()

    address_parts = [
        getattr(company, "address_line1", None),
        getattr(company, "address_line2", None),
        getattr(company, "city", None),
        getattr(company, "state", None),
        getattr(company, "pincode", None),
        getattr(company, "country", None),
    ]
    company_address = ", ".join(p for p in address_parts if p)

    offered_ctc = float(offer.offered_ctc) if offer.offered_ctc is not None else 0.0
    status_display = {
        "sent": "Sent", "accepted": "Accepted", "rejected": "Rejected", "withdrawn": "Withdrawn",
        "draft": "Draft", "approval_pending": "Approval Pending", "approved": "Approved",
        "viewed": "Viewed", "declined": "Declined", "expired": "Expired",
    }.get((offer.status or "").lower(), (offer.status or "—").title())
    offer_designation = getattr(offer, "designation", None)
    offer_employment_type = getattr(offer, "employment_type", None)
    contract = _contract_placeholders(offer)
    probation_text = f"{offer.probation_months} months" if getattr(offer, "probation_months", None) else "—"
    notice_text = f"{offer.notice_period_days} days" if getattr(offer, "notice_period_days", None) else "—"
    if contract["is_contract"]:
        # The contract-terms block replaces the CTC / probation / notice one.
        compensation_terms = _terms_table([
            ("Engagement", "Fixed-term contract"),
            ("Contract rate", f"₹{contract['contract_rate']} {contract['contract_rate_unit']}"),
            ("Contract duration", contract["contract_duration"]),
            ("Contract end date", contract["contract_end_date"]),
        ])
    else:
        compensation_terms = _terms_table([
            ("Annual CTC", f"₹{offered_ctc:,.2f}"),
            ("Probation period", probation_text),
            ("Notice period", notice_text),
        ])

    return {
        "candidate_name": candidate.name if candidate else "—",
        "candidate_email": (candidate.email if candidate else None) or "—",
        "candidate_phone": (candidate.phone if candidate else None) or "—",
        # The role title as posted on the job opening.
        "position": opening.title if opening else "—",
        # Alias of `position`.
        "role": opening.title if opening else "—",
        # The formal Designation name when the opening was linked to one --
        # falls back to the same posted title when it wasn't, never blank.
        "designation": offer_designation or (designation.name if designation else (opening.title if opening else "—")),
        "department": department.name if department else "—",
        "branch": branch.name if branch else "—",
        "employment_type": ((offer_employment_type or opening.employment_type).replace("_", " ").title() if opening else "—"),
        "work_mode": getattr(offer, "work_mode", None) or (getattr(opening, "work_mode", None) if opening else None) or "—",
        "probation_period": f"{offer.probation_months} months" if getattr(offer, "probation_months", None) else "—",
        "notice_period": f"{offer.notice_period_days} days" if getattr(offer, "notice_period_days", None) else "—",
        "working_hours": getattr(offer, "working_hours", None) or "—",
        "offer_expiry_date": offer.expiry_date.isoformat() if getattr(offer, "expiry_date", None) else "—",
        "benefits": getattr(offer, "benefits", None) or "—",
        "salary_breakup": getattr(offer, "salary_breakup", None) or "—",
        "other_terms": getattr(offer, "terms", None) or "—",
        "offered_ctc": f"{offered_ctc:,.2f}",
        # Real amount, spelled out -- computed from the same offered_ctc
        # every other CTC placeholder uses, never a separately-fabricated
        # figure.
        "offered_ctc_in_words": tr.amount_in_words(offered_ctc),
        "offer_date": offer.offer_date.isoformat() if offer.offer_date else "—",
        "joining_date": offer.proposed_joining_date.isoformat() if offer.proposed_joining_date else "—",
        "offer_status": status_display,
        # The department's assigned leadership (see Department.
        # senior_manager_id / hr_representative_id) -- the closest real,
        # non-fabricated stand-in this app has for "who this candidate will
        # report to" / "HR contact", since a pre-hire Offer/Candidate has no
        # manager assignment of its own (that only exists once someone is a
        # real Employee).
        "reporting_manager": manager_name(reporting_manager),
        "hr_contact": manager_name(hr_contact),
        "company_name": company.name if company else "—",
        "company_address": company_address or "—",
        "company_logo_url": (getattr(company, "logo_url", None) or ""),
        "company_logo": tr.resolve_company_logo(company),
        # Today's real date -- a system fact (like an auto-generated
        # footer), not employee/offer data, safe to expose for "Date:" /
        # letterhead lines.
        "current_date": datetime.date.today().isoformat(),
        # Contract offers (employment type Contract): rate-based terms.
        "compensation_type": "Contract rate" if contract["is_contract"] else "Annual CTC",
        "contract_rate": contract["contract_rate"],
        "contract_rate_unit": contract["contract_rate_unit"],
        "contract_rate_in_words": contract["contract_rate_in_words"],
        "contract_duration": contract["contract_duration"],
        "contract_end_date": contract["contract_end_date"],
        # Pre-rendered block: contract terms for a contract offer, else
        # CTC / probation / notice.
        "compensation_terms": compensation_terms,
    }


_CONTRACT_KEYS_RE = re.compile(r"\{\{\s*(compensation_terms|contract_rate|contract_rate_unit|contract_duration|contract_end_date)\s*\}\}")
# A table row whose label cell mentions CTC and whose value cell shows {{offered_ctc}}
# -- e.g. the default template's <tr><td>Annual CTC</td><td>{{offered_ctc}} (...)</td></tr>.
_CTC_ROW_RE = re.compile(
    r"<tr[^>]*>\s*<t[dh][^>]*>[^<]*\bCTC\b[^<]*</t[dh]>\s*<t[dh][^>]*>(?:(?!</tr>).)*?"
    r"\{\{\s*offered_ctc\s*\}\}(?:(?!</tr>).)*?</tr>",
    re.IGNORECASE | re.DOTALL,
)
_CONTRACT_ROWS = (
    "<tr><td>Contract Rate</td><td>₹{{contract_rate}} {{contract_rate_unit}} ({{contract_rate_in_words}})</td></tr>"
    "<tr><td>Contract Duration</td><td>{{contract_duration}}</td></tr>"
    "<tr><td>Contract End Date</td><td>{{contract_end_date}}</td></tr>"
)


def adapt_template_for_contract(html_body: str) -> str:
    """Contract offers on a template written for CTC offers (no contract
    placeholders -- e.g. the default template): the Annual CTC row becomes
    Contract Rate / Duration / End Date rows; any other {{offered_ctc}} shows
    the contract rate. Templates that already use the contract placeholders
    are left exactly as written."""
    if _CONTRACT_KEYS_RE.search(html_body):
        return html_body
    body, n = _CTC_ROW_RE.subn(_CONTRACT_ROWS, html_body, count=1)
    body = re.sub(r"\{\{\s*offered_ctc_in_words\s*\}\}", "{{contract_rate_in_words}}", body)
    return re.sub(r"\{\{\s*offered_ctc\s*\}\}", "₹{{contract_rate}} {{contract_rate_unit}}", body)


def render_offer_letter_html(html_body: str, css_styles: str, placeholders: dict[str, str]) -> str:
    if placeholders.get("compensation_type") == "Contract rate":  # contract offers only
        html_body = adapt_template_for_contract(html_body)
    return tr.render_template_html(html_body, css_styles, placeholders, verbatim_keys=VERBATIM_KEYS)


validate_template_html = tr.validate_template_html
render_html_to_pdf = tr.render_html_to_pdf
