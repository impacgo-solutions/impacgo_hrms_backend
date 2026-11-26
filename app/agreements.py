"""Freelancer NDA / Agreement documents (Documents > Templates) and their
first-login acceptance.

* Templates live in hcm_agreement_templates (models.AgreementTemplate):
  several per type ('nda' | 'agreement'), one active per type. The active
  one is what a Freelancer must accept.
* Every company gets the predefined templates below (BUILTIN_TEMPLATES, 4
  per type) the first time they're needed (ensure_builtin_templates) -- no
  per-tenant SQL seeding, nothing company-specific in code. The first one
  of each type starts active; HR can preview, edit, add their own and
  switch the active one.
* A Freelancer's pending documents are the active templates whose CURRENT
  version they haven't accepted (an edited template -> version + 1 -> must
  be accepted again). Acceptances (hcm_agreement_acceptances) keep the
  template name, version, the exact rendered HTML shown, time, IP and user
  agent; they're never changed or deleted.

Rendering reuses template_rendering (the same sanitiser + {{ placeholder }}
substitution as payslips / offer / exit letters).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import crud, models, template_designs, template_rendering

DOC_TYPES: dict[str, str] = {"nda": "Non-Disclosure Agreement", "agreement": "Freelancer Agreement"}

# {{ key }} -> description, for the template editor's placeholder list.
PLACEHOLDERS: dict[str, str] = {
    "company_name": "Company display name",
    "company_legal_name": "Company legal name (falls back to the display name)",
    "company_address": "Company registered address",
    "company_logo": "Company logo (an <img src> value; Organization > Company Profile)",
    "company_cin": "Company CIN line (blank when not set)",
    "logo_state": "with-logo / no-logo -- put on the root element's class to hide an empty logo slot",
    "employee_name": "Freelancer's full name",
    "employee_id": "Freelancer's Employee ID",
    "employee_email": "Freelancer's work email",
    "employee_address": "Freelancer's address",
    "designation": "Freelancer's designation",
    "reporting_manager": "Reporting manager's name",
    "date_of_joining_long": "Date of joining, e.g. 3 November 2026",
    "effective_date_long": "Date the document is accepted, e.g. 3 November 2026",
    "rate": "Current rate card, e.g. INR 500.00 per hour",
}

# Shared letter layout + the "Classic Letterhead" style (app/template_designs.py):
# letterhead with the company logo, print-aligned A4 margins. HR can switch
# style in the template editor's Designs gallery.
_CSS = template_designs.theme_css("classic")


def _doc(title: str, subtitle: str, clauses: list[tuple[str, str]]) -> str:
    """One template's HTML from its clauses (heading, paragraph)."""
    return template_designs.agreement_layout(title, subtitle, clauses)


_CONF = ("Confidential Information",
         "&ldquo;Confidential Information&rdquo; means all non-public business, technical, financial, client, "
         "employee and product information disclosed by the Company in any form, including source code, designs, "
         "pricing, plans and personal data, whether or not marked confidential.")
_RETURN = ("Return of Information",
           "On completion of the engagement, or earlier on request, the Freelancer will return or securely destroy "
           "all Confidential Information and confirm this in writing. No copies may be retained.")
_LAW = ("Governing Law",
        "This document is governed by the laws of the place of the Company's registered office, whose courts have "
        "exclusive jurisdiction.")

BUILTIN_TEMPLATES: list[dict] = [
    # -- NDA --
    {
        "key": "nda_standard", "doc_type": "nda", "name": "Standard Freelancer NDA",
        "description": "One-way NDA protecting the Company's information. Suitable for most freelancers.",
        "html": _doc("Non-Disclosure Agreement", "Freelancer confidentiality undertaking", [
            _CONF,
            ("Obligations", "The Freelancer will use Confidential Information only to perform services for the "
             "Company, will not disclose it to any third party, and will protect it with at least reasonable care."),
            ("Exclusions", "Obligations do not apply to information that is or becomes public without the "
             "Freelancer's fault, was lawfully known before disclosure, or must be disclosed by law (with prompt "
             "notice to the Company)."),
            _RETURN,
            ("Term", "These obligations start on the date above and continue for three (3) years after the "
             "engagement ends; trade secrets and personal data remain protected for as long as they stay confidential."),
            _LAW,
        ]),
    },
    {
        "key": "nda_mutual", "doc_type": "nda", "name": "Mutual NDA",
        "description": "Both parties protect each other's confidential information.",
        "html": _doc("Mutual Non-Disclosure Agreement", "Reciprocal confidentiality", [
            ("Confidential Information", "Each party may disclose non-public business, technical or financial "
             "information to the other. Information received is the Confidential Information of the disclosing party."),
            ("Mutual Obligations", "Each party will use the other's Confidential Information only for the purpose "
             "of the engagement, restrict access to those who need it, and protect it with the care it uses for its "
             "own confidential information."),
            ("Exclusions", "Information that is public, independently developed, or lawfully received from a third "
             "party without restriction is not Confidential Information."),
            _RETURN,
            ("Term", "Obligations survive for three (3) years after the engagement ends."),
            _LAW,
        ]),
    },
    {
        "key": "nda_ip", "doc_type": "nda", "name": "Software & IP Confidentiality NDA",
        "description": "For developers and designers with access to source code, systems and credentials.",
        "html": _doc("Software & IP Confidentiality Agreement", "Source code, systems and credentials", [
            _CONF,
            ("Source Code and Systems", "The Freelancer will not copy, publish or upload Company source code, "
             "repositories, designs or documentation to any personal or public location (including public code "
             "hosting or AI tools) without written approval."),
            ("Credentials and Access", "Access credentials are personal, must not be shared, and must be stored "
             "securely. Any suspected compromise must be reported to the Company immediately."),
            ("Security Incidents", "The Freelancer will report any actual or suspected unauthorised access to "
             "Confidential Information within 24 hours."),
            _RETURN,
            ("Term", "Obligations continue for five (5) years after the engagement ends; trade secrets for as long "
             "as they remain secret."),
            _LAW,
        ]),
    },
    {
        "key": "nda_data", "doc_type": "nda", "name": "Client Data Protection NDA",
        "description": "For freelancers who handle client or personal data.",
        "html": _doc("Data Protection & Confidentiality Agreement", "Client and personal data", [
            _CONF,
            ("Personal Data", "The Freelancer will process personal data only on the Company's documented "
             "instructions, only for the engagement, and in line with applicable data protection law."),
            ("Client Information", "Client names, contracts, data and deliverables are Confidential Information "
             "and may not be used to solicit or work for those clients directly."),
            ("Safeguards", "The Freelancer will keep data on Company-approved systems only, use strong passwords "
             "and encryption, and not transfer data to personal devices or third parties."),
            ("Breach Notification", "Any personal data breach must be reported to the Company without delay and "
             "in any case within 24 hours of discovery."),
            _RETURN,
            _LAW,
        ]),
    },
    # -- Agreement --
    {
        "key": "agreement_hourly", "doc_type": "agreement", "name": "Freelance Services Agreement (Rate Card)",
        "description": "Standard terms for freelancers paid by the hour or day from approved timesheets.",
        "html": _doc("Freelance Services Agreement", "Rate-card engagement", [
            ("Services", "The Freelancer will provide the services assigned by the Company, as {{ designation }}, "
             "reporting to {{ reporting_manager }}, starting {{ date_of_joining_long }}."),
            ("Fees", "The Company will pay the Freelancer at {{ rate }} for approved timesheet time. Timesheets "
             "must be submitted in the Company's HRMS and are paid only once approved."),
            ("Independent Contractor", "The Freelancer is an independent contractor, not an employee, and is "
             "responsible for their own taxes. Applicable tax will be deducted at source where required by law."),
            ("Intellectual Property", "All work product created for the Company is assigned to the Company on "
             "creation, and the Freelancer waives any moral rights to the extent permitted by law."),
            ("Confidentiality", "The Freelancer is bound by the Company's Non-Disclosure Agreement."),
            ("Termination", "Either party may end this engagement with seven (7) days' written notice. Approved "
             "time up to the end date will be paid."),
            _LAW,
        ]),
    },
    {
        "key": "agreement_contractor", "doc_type": "agreement", "name": "Independent Contractor Agreement",
        "description": "General-purpose contractor terms with a notice period and non-solicitation.",
        "html": _doc("Independent Contractor Agreement", "General contractor terms", [
            ("Engagement", "The Company engages the Freelancer as an independent contractor in the role of "
             "{{ designation }} from {{ date_of_joining_long }}."),
            ("Compensation", "Fees are payable at {{ rate }} against approved timesheets or invoices, within "
             "thirty (30) days of approval."),
            ("Status", "Nothing in this agreement creates employment, partnership or agency. The Freelancer "
             "controls how the work is done, uses their own equipment unless agreed otherwise, and is responsible "
             "for their own statutory obligations."),
            ("Work Product", "All deliverables and related intellectual property belong to the Company on payment."),
            ("Non-Solicitation", "For twelve (12) months after the engagement, the Freelancer will not solicit the "
             "Company's employees or the clients they worked with."),
            ("Termination", "Either party may terminate with fourteen (14) days' written notice, or immediately "
             "for material breach."),
            _LAW,
        ]),
    },
    {
        "key": "agreement_project", "doc_type": "agreement", "name": "Project-Based Agreement",
        "description": "For fixed-scope work tracked against project milestones.",
        "html": _doc("Project Services Agreement", "Project / milestone engagement", [
            ("Scope", "The Freelancer will deliver the projects assigned in the Company's HRMS, to the scope, "
             "timelines and acceptance criteria set for each project."),
            ("Fees", "Work is paid at {{ rate }} for approved time, unless a fixed project fee is agreed in writing."),
            ("Acceptance", "Deliverables are accepted once approved by {{ reporting_manager }} or another "
             "authorised Company representative. Defects reported within fourteen (14) days will be fixed at no "
             "extra cost."),
            ("Intellectual Property", "All deliverables, code and materials created under this agreement are "
             "owned by the Company."),
            ("Confidentiality", "The Freelancer is bound by the Company's Non-Disclosure Agreement."),
            ("Termination", "The Company may end a project early with written notice; approved work up to that "
             "date will be paid."),
            _LAW,
        ]),
    },
    {
        "key": "agreement_consulting", "doc_type": "agreement", "name": "Consulting Agreement",
        "description": "For advisory or consulting engagements.",
        "html": _doc("Consulting Agreement", "Advisory services", [
            ("Services", "The Freelancer will provide consulting and advisory services in their area of expertise "
             "as {{ designation }}, as requested by the Company."),
            ("Fees and Expenses", "Fees are payable at {{ rate }} for approved time. Expenses are reimbursed only "
             "if approved in advance and supported by receipts."),
            ("Conflicts of Interest", "The Freelancer will disclose any engagement with a competitor of the "
             "Company and will not use Company information for any other client."),
            ("Independent Contractor", "The Freelancer is not an employee and has no authority to bind the Company."),
            ("Intellectual Property", "Reports, recommendations and other work product are owned by the Company."),
            ("Termination", "Either party may terminate with seven (7) days' written notice."),
            _LAW,
        ]),
    },
]


def builtin_css() -> str:
    return _CSS


def ensure_builtin_templates(db: Session, company_id: uuid.UUID) -> None:
    """Seeds the predefined templates for [company_id] once: only a type that
    has NO templates at all gets them (a company that deleted or replaced
    them keeps its own), and only then is the first one made active.
    Flushes; the caller commits."""
    rows = list(db.scalars(select(models.AgreementTemplate).where(
        models.AgreementTemplate.company_id == company_id)))
    # A predefined template whose content HR never edited (still version 1 --
    # every content edit bumps the version) and nobody has accepted yet
    # follows the latest built-in wording / layout; once edited or accepted
    # it is never touched again.
    by_key = {b["key"]: b for b in BUILTIN_TEMPLATES}
    for t in rows:
        b = by_key.get(t.builtin_key or "")
        if (b is not None and (t.version or 1) == 1
                and (t.html_body != b["html"] or (t.css_styles or "") != _CSS)
                and acceptance_count(db, t.id) == 0):
            t.html_body, t.css_styles = b["html"], _CSS
    types_present = {t.doc_type for t in rows}
    missing = [d for d in DOC_TYPES if d not in types_present]
    if not missing:
        db.flush()
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        # Savepoint: a concurrent first request seeding the same company hits
        # the builtin / one-active unique indexes -- then theirs stands.
        with db.begin_nested():
            for doc_type in missing:
                for i, b in enumerate(t for t in BUILTIN_TEMPLATES if t["doc_type"] == doc_type):
                    db.add(models.AgreementTemplate(
                        id=uuid.uuid4(), company_id=company_id, doc_type=doc_type, name=b["name"],
                        description=b["description"], html_body=b["html"], css_styles=_CSS,
                        is_active=i == 0, version=1, builtin_key=b["key"], created_at=now, updated_at=now,
                    ))
    except IntegrityError:
        pass


def list_templates(db: Session, company_id: uuid.UUID, doc_type: str | None = None) -> list[models.AgreementTemplate]:
    q = select(models.AgreementTemplate).where(models.AgreementTemplate.company_id == company_id)
    if doc_type:
        q = q.where(models.AgreementTemplate.doc_type == doc_type)
    return list(db.scalars(q.order_by(models.AgreementTemplate.doc_type,
                                      models.AgreementTemplate.is_active.desc(),
                                      models.AgreementTemplate.name)))


def get_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID) -> models.AgreementTemplate | None:
    t = db.get(models.AgreementTemplate, template_id)
    return t if t is not None and t.company_id == company_id else None


def activate_template(db: Session, template: models.AgreementTemplate) -> None:
    """Makes [template] the one active template of its type."""
    for other in db.scalars(select(models.AgreementTemplate).where(
            models.AgreementTemplate.company_id == template.company_id,
            models.AgreementTemplate.doc_type == template.doc_type,
            models.AgreementTemplate.is_active.is_(True),
            models.AgreementTemplate.id != template.id)):
        other.is_active = False
    db.flush()  # the partial unique index allows only one active per type
    template.is_active = True
    db.flush()


def acceptance_count(db: Session, template_id: uuid.UUID) -> int:
    from sqlalchemy import func
    return db.scalar(select(func.count()).select_from(models.AgreementAcceptance).where(
        models.AgreementAcceptance.template_id == template_id)) or 0


# -- rendering ---------------------------------------------------------------

def _long(d: datetime.date | None) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else "—"


def _rate_text(db: Session, employee: models.Employee | None) -> str:
    if employee is None or employee.contract_rate_amount is None:
        return "the rate agreed in writing"
    code = crud.employee_rate_currency(db, employee)
    per = {"hourly": "per hour", "daily": "per day", "monthly": "per month"}.get(employee.contract_rate_unit or "", "")
    return f"{code} {float(employee.contract_rate_amount):,.2f} {per}".strip()


_BLANK_VALUES = {"", "nan", "none", "null", "n/a", "na", "-", "—", "undefined"}


def _clean(value) -> str | None:
    """A stored value, or None when it's empty or a spreadsheet-import
    placeholder ('nan', 'None', 'null', ...) that must never be printed."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _BLANK_VALUES else text


def _or_dash(value) -> str:
    return _clean(value) or "—"


def placeholders_for(db: Session, company: models.Company, employee: models.Employee | None,
                     on: datetime.date | None = None) -> dict[str, str]:
    """Merge values for [employee] (None = sample values, for HR's preview).
    Missing values render as "—", never as 'nan' / 'None'."""
    address = ", ".join(p for p in (_clean(getattr(company, attr, None)) for attr in (
        "address_line1", "address_line2", "city", "state", "pincode", "country")) if p) or "—"
    company_name = _or_dash(company.name)
    on = on or datetime.date.today()
    base = {
        # company_logo / logo_state / company_cin for the letterhead.
        **template_designs.company_values(company),
        "company_name": company_name,
        "company_legal_name": _clean(getattr(company, "legal_name", None)) or company_name,
        "company_address": address,
        "effective_date_long": _long(on),
    }
    if employee is None:
        return {**base, "employee_name": "Priya Sharma", "employee_id": "FL-0001",
                "employee_email": "priya.sharma@example.com", "employee_address": "Bengaluru, India",
                "designation": "UI/UX Designer", "reporting_manager": "Reporting Manager",
                "date_of_joining_long": _long(on), "rate": "INR 500.00 per hour"}
    manager = db.get(models.Employee, employee.reporting_manager_id) if employee.reporting_manager_id else None
    designation = db.get(models.Designation, employee.designation_id) if getattr(employee, "designation_id", None) else None
    return {
        **base,
        "employee_name": " ".join(p for p in (_clean(employee.first_name), _clean(employee.last_name)) if p) or "—",
        "employee_id": _or_dash(employee.employee_code),
        "employee_email": _or_dash(employee.work_email),
        "employee_address": _or_dash(getattr(employee, "current_address", None)),
        "designation": _or_dash(getattr(designation, "name", None)),
        "reporting_manager": (" ".join(p for p in (_clean(manager.first_name), _clean(manager.last_name)) if p)
                              or "—") if manager else "—",
        "date_of_joining_long": _long(employee.date_of_joining),
        "rate": _rate_text(db, employee),
    }


def render(template_html: str, css: str, values: dict[str, str]) -> str:
    return template_rendering.render_template_html(template_html, css, values)


# -- first-login acceptance --------------------------------------------------

def requires_acceptance(employee: models.Employee | None) -> bool:
    """Only Freelancers accept NDA / Agreement documents."""
    return employee is not None and crud.is_freelancer(employee)


def pending_templates(db: Session, employee: models.Employee) -> list[models.AgreementTemplate]:
    """The active NDA / Agreement templates whose current version [employee]
    hasn't accepted yet (empty for anyone who isn't a Freelancer)."""
    if not requires_acceptance(employee):
        return []
    ensure_builtin_templates(db, employee.company_id)
    active = [t for t in list_templates(db, employee.company_id) if t.is_active]
    accepted = {
        (a.template_id, a.template_version)
        for a in db.scalars(select(models.AgreementAcceptance).where(
            models.AgreementAcceptance.employee_id == employee.id))
    }
    order = list(DOC_TYPES)
    return sorted((t for t in active if (t.id, t.version) not in accepted),
                  key=lambda t: order.index(t.doc_type) if t.doc_type in order else 99)


def accept(db: Session, employee: models.Employee, user_id: uuid.UUID | None, template: models.AgreementTemplate,
           version: int, ip: str | None, user_agent: str | None) -> models.AgreementAcceptance:
    """Records [employee]'s acceptance of [template] at [version] (must be the
    current version -- ValueError otherwise). Idempotent for the same
    version. Flushes; the caller commits."""
    if version != template.version:
        raise ValueError("This document was updated while you were reading it -- please review the latest version.")
    existing = db.scalar(select(models.AgreementAcceptance).where(
        models.AgreementAcceptance.employee_id == employee.id,
        models.AgreementAcceptance.template_id == template.id,
        models.AgreementAcceptance.template_version == template.version))
    if existing is not None:
        return existing
    company = db.get(models.Company, employee.company_id)
    today = crud.company_today(db, employee.company_id)
    row = models.AgreementAcceptance(
        id=uuid.uuid4(), company_id=employee.company_id, employee_id=employee.id, user_id=user_id,
        template_id=template.id, doc_type=template.doc_type, template_name=template.name,
        template_version=template.version,
        rendered_html=render(template.html_body, template.css_styles,
                             placeholders_for(db, company, employee, today)),
        accepted_at=datetime.datetime.now(datetime.timezone.utc),
        ip_address=(ip or "")[:64] or None, user_agent=(user_agent or "")[:300] or None,
    )
    db.add(row)
    db.flush()
    return row


def acceptances_for(db: Session, employee_id: uuid.UUID) -> list[models.AgreementAcceptance]:
    return list(db.scalars(select(models.AgreementAcceptance).where(
        models.AgreementAcceptance.employee_id == employee_id
    ).order_by(models.AgreementAcceptance.accepted_at.desc())))

