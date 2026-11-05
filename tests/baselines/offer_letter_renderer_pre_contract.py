"""Frozen copy of app/offer_letter_html_renderer.py as it was BEFORE contract
support (Phase 1). Used only by tests/test_contract_employment.py to prove
non-contract offer letters are byte-identical before and after."""
import datetime

from app import template_rendering as tr


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
    }


def render_offer_letter_html(html_body: str, css_styles: str, placeholders: dict[str, str]) -> str:
    return tr.render_template_html(html_body, css_styles, placeholders)


validate_template_html = tr.validate_template_html
render_html_to_pdf = tr.render_html_to_pdf
