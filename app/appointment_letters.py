"""Appointment Letter: Documents > Templates > Appointment Letter (designs)
and Employee Profile > Documents > Appointment Letter (generate / history).

Same shape as the Experience & Relieving Letter (exit_letter_html_renderer +
the exit-letter functions in crud.py), but keyed by the employee alone:
* hcm_appointment_letter_templates -- several HTML+CSS designs per company,
  one active, version bumped on each save, plus the authorized signatory and
  seal / signature images.
* hcm_appointment_letters -- every generated letter is an immutable snapshot
  (rendered HTML, placeholder values, template name/version, stored PDF);
  regenerating adds version + 1.

Rendering, sanitising and HTML-to-PDF reuse template_rendering; the layout
and styles come from template_designs (the shared Designs gallery). Every
value comes from real records -- never sample data. Values are raw;
escaping happens once in render().
"""

from __future__ import annotations

import datetime
import html
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crud, models, template_designs, template_rendering as tr
from .exit_letter_html_renderer import amount_in_words, embed_letter_image, format_inr

LETTER_TITLE = "Appointment Letter"
DEFAULT_TEMPLATE_NAME = "Standard Appointment Letter"

# Pre-rendered HTML fragments (already escaped) -- inserted verbatim.
VERBATIM_PLACEHOLDERS = ("salary_breakdown",)
# Image data URLs -- left out of the stored placeholder snapshot.
IMAGE_PLACEHOLDERS = ("company_logo", "company_seal", "authorized_signature")

# {{ key }} -> description, for the template editor's placeholder list.
PLACEHOLDERS: dict[str, str] = {
    "letter_title": "Appointment Letter",
    "letter_number": "Reference number, e.g. APL/EMP-0042/2026/1",
    "letter_date": "Issue date, e.g. 9th November 2026",
    "issue_date": "Issue date, dd/mm/yyyy",
    "issue_date_long": "Issue date, e.g. 9 November 2026",
    "employee_name": "Employee's full name",
    "employee_first_name": "Employee's first name",
    "employee_salutation": "Mr. / Ms. (blank when gender is not recorded)",
    "employee_id": "Employee ID",
    "employee_email": "Work email",
    "employee_phone": "Personal phone",
    "employee_address": "Current address (falls back to permanent address)",
    "designation": "Designation",
    "department": "Department",
    "work_location": "Branch / work location",
    "work_mode": "Work mode (Office / Remote / Hybrid)",
    "employment_type": "Employment type, e.g. Full Time",
    "reporting_manager": "Reporting manager's name",
    "date_of_joining": "Date of joining, dd/mm/yyyy",
    "date_of_joining_long": "Date of joining, e.g. 3 December 2026",
    "probation_period": "Probation length, e.g. 6 months (from the probation end date)",
    "probation_end_date": "Probation end date, e.g. 3 June 2027",
    "contract_end_date": "Contract end date (contract employees), e.g. 31 March 2027",
    "annual_ctc": "Annual CTC, e.g. ₹6,00,000.00",
    "annual_ctc_words": "Annual CTC in words",
    "monthly_ctc": "Monthly CTC (annual / 12)",
    "salary_breakdown": "Salary table (monthly + annual per component) from the assigned salary structure",
    "authorized_signatory": "Authorized signatory (template setting)",
    "authorized_signatory_designation": "Signatory's designation (template setting)",
    "authorized_signature": "Signature image (an <img src> value)",
    "company_seal": "Company seal image (an <img src> value)",
    "company_name": "Company display name",
    "company_legal_name": "Company legal name (falls back to the display name)",
    "company_address": "Company registered address",
    "company_cin": "Company CIN line (blank when not set)",
    "company_gstin": "Company GSTIN (blank when not set)",
    "company_logo": "Company logo (an <img src> value)",
    "logo_state": "with-logo / no-logo -- put on the root element's class to hide an empty logo slot",
}

_BLANK = {"", "nan", "none", "null", "n/a", "na", "-", "—", "undefined"}


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return None if text.lower() in _BLANK else text


def _dash(value) -> str:
    return _clean(value) or "—"


def _d(value: datetime.date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


def _long(value: datetime.date | None) -> str:
    return f"{value.day} {value.strftime('%B %Y')}" if value else "—"


def _months_between(start: datetime.date | None, end: datetime.date | None) -> str:
    if not start or not end or end <= start:
        return "as per company policy"
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    months = max(months, 1)
    return f"{months} month{'s' if months != 1 else ''}"


def _label(value: str | None) -> str:
    """full_time -> Full Time."""
    text = _clean(value)
    return text.replace("_", " ").title() if text else "—"


def _name(employee) -> str:
    return f"{employee.first_name} {employee.last_name or ''}".strip() if employee is not None else "—"


def letter_number(employee, issue_date: datetime.date, version: int) -> str:
    code = (employee.employee_code if employee else None) or "EMP"
    return f"APL/{code}/{issue_date.year}/{version}"


def _salary_breakdown(structure: dict | None, annual_ctc: float | None) -> str:
    """Monthly + annual per component from the assigned salary structure;
    just the CTC row when no structure is assigned."""
    rows = []
    components = [c for c in (structure or {}).get("components", []) if c.get("amount")]
    earnings = [c for c in components if "deduction" not in (c.get("type") or "").lower()]
    for c in earnings:
        annual = float(c["amount"])
        rows.append(f'<tr><td>{html.escape(c["name"])}</td><td class="amount">{html.escape(format_inr(annual / 12))}</td>'
                    f'<td class="amount">{html.escape(format_inr(annual))}</td></tr>')
    if not rows and not annual_ctc:
        return '<p class="muted">The salary breakdown will be shared separately.</p>'
    total = float(annual_ctc) if annual_ctc else sum(float(c["amount"]) for c in earnings)
    if not rows:
        rows.append(f'<tr><td>Cost to Company</td><td class="amount">{html.escape(format_inr(total / 12))}</td>'
                    f'<td class="amount">{html.escape(format_inr(total))}</td></tr>')
    return ('<table class="salary"><thead><tr><th>Component</th><th class="amount">Monthly</th>'
            '<th class="amount">Annual</th></tr></thead><tbody>' + "".join(rows) + '</tbody>'
            f'<tfoot><tr><td>Total (CTC)</td><td class="amount">{html.escape(format_inr(total / 12))}</td>'
            f'<td class="amount">{html.escape(format_inr(total))}</td></tr></tfoot></table>')


def build_placeholders(
    db: Session,
    company: models.Company,
    employee: models.Employee,
    *,
    signatory_name: str | None,
    signatory_designation: str | None,
    issue_date: datetime.date,
    version: int,
    seal_image_path: str | None = None,
    signature_image_path: str | None = None,
) -> dict[str, str]:
    gender = (_clean(employee.gender) or "").lower()
    manager = employee.reporting_manager
    structure = crud.get_employee_salary_structure(db, employee.id)
    annual = float(employee.annual_ctc) if employee.annual_ctc else None
    address_parts = [getattr(company, k, None) for k in
                     ("address_line1", "address_line2", "city", "state", "pincode", "country")]
    gstin = _clean(getattr(company, "gstin", None)) or ""
    return {
        "letter_title": LETTER_TITLE,
        "letter_number": letter_number(employee, issue_date, version),
        "letter_date": template_designs.ordinal_date(issue_date),
        "issue_date": _d(issue_date),
        "issue_date_long": _long(issue_date),
        "current_date": _d(issue_date),
        "employee_name": _name(employee),
        "employee_first_name": _dash(employee.first_name),
        "employee_salutation": {"male": "Mr.", "female": "Ms."}.get(gender, ""),
        "employee_id": _dash(employee.employee_code),
        "employee_email": _dash(employee.work_email),
        "employee_phone": _dash(employee.personal_phone),
        "employee_address": _clean(employee.current_address) or _clean(employee.permanent_address) or "",
        "designation": _dash(employee.designation.name if employee.designation else None),
        "department": _dash(employee.department.name if employee.department else None),
        "work_location": _dash(employee.branch.name if employee.branch else None),
        "work_mode": _label(employee.work_mode),
        "employment_type": _label(employee.employment_type),
        "reporting_manager": _name(manager) if manager is not None else "—",
        "date_of_joining": _d(employee.date_of_joining),
        "date_of_joining_long": _long(employee.date_of_joining),
        "probation_period": _months_between(employee.date_of_joining, employee.probation_end_date),
        "probation_end_date": _long(employee.probation_end_date),
        "contract_end_date": _long(employee.contract_end_date),
        "annual_ctc": format_inr(annual) if annual else "—",
        "annual_ctc_words": amount_in_words(annual) if annual else "—",
        "monthly_ctc": format_inr(annual / 12) if annual else "—",
        "salary_breakdown": _salary_breakdown(structure, annual),
        "authorized_signatory": (signatory_name or "").strip() or "—",
        "authorized_signatory_designation": (signatory_designation or "").strip() or "—",
        "authorized_signature": embed_letter_image(signature_image_path),
        "company_seal": embed_letter_image(seal_image_path),
        "company_name": _dash(company.name),
        "company_address": ", ".join(p for p in address_parts if _clean(p)) or "—",
        "company_gstin": gstin,
        **template_designs.company_values(company),
    }


def render(html_body: str, css_styles: str, placeholders: dict[str, str]) -> str:
    return tr.render_template_html(html_body, css_styles, placeholders, verbatim_keys=VERBATIM_PLACEHOLDERS)


validate_template_html = tr.validate_template_html


def default_css() -> str:
    return template_designs.theme_css("classic") + template_designs.APPOINTMENT_EXTRA_CSS


# ── templates ────────────────────────────────────────────────────────────────

_MODEL = models.AppointmentLetterTemplate


def ensure_default_template(db: Session, company_id: uuid.UUID, updated_by: uuid.UUID | None) -> None:
    """Every company starts with one active standard template the first time
    templates are listed -- no per-tenant SQL seeding. Never re-created once
    the company has any template (even if HR deleted the default)."""
    if db.scalar(select(_MODEL.id).where(_MODEL.company_id == company_id).limit(1)) is not None:
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    db.add(_MODEL(
        id=uuid.uuid4(), company_id=company_id, name=DEFAULT_TEMPLATE_NAME,
        html_body=template_designs.APPOINTMENT_LAYOUT, css_styles=default_css(),
        is_active=True, version=1, updated_by=updated_by, created_at=now, updated_at=now,
    ))
    db.flush()


def list_templates(db: Session, company_id: uuid.UUID) -> list[models.AppointmentLetterTemplate]:
    return crud._list_html_templates(db, _MODEL, company_id)


def get_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID):
    return crud._get_html_template_by_id(db, _MODEL, company_id, template_id)


def active_template(db: Session, company_id: uuid.UUID) -> models.AppointmentLetterTemplate | None:
    return db.scalar(select(_MODEL).where(_MODEL.company_id == company_id, _MODEL.is_active.is_(True)))


def create_template(db: Session, company_id: uuid.UUID, *, name: str, html_body: str, css_styles: str,
                    is_active: bool, signatory_name: str | None, signatory_designation: str | None,
                    created_by: uuid.UUID | None) -> models.AppointmentLetterTemplate:
    now = datetime.datetime.now(datetime.timezone.utc)
    row = _MODEL(
        id=uuid.uuid4(), company_id=company_id, name=name, html_body=html_body, css_styles=css_styles,
        is_active=False, version=1, signatory_name=(signatory_name or "").strip() or None,
        signatory_designation=(signatory_designation or "").strip() or None,
        updated_by=created_by, created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    if is_active:
        crud._activate_html_template(db, _MODEL, company_id, row.id)
    return row


def update_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID, *, name: str, html_body: str,
                    css_styles: str, is_active: bool, signatory_name: str | None,
                    signatory_designation: str | None, updated_by: uuid.UUID | None):
    row = get_template(db, company_id, template_id)
    if row is None:
        raise ValueError("Template not found.")
    row.name, row.html_body, row.css_styles = name, html_body, css_styles
    row.signatory_name = (signatory_name or "").strip() or None
    row.signatory_designation = (signatory_designation or "").strip() or None
    row.version += 1
    row.updated_by = updated_by
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()
    if is_active and not row.is_active:
        crud._activate_html_template(db, _MODEL, company_id, row.id)
    elif not is_active and row.is_active:
        row.is_active = False
        db.flush()
    return row


def delete_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID) -> None:
    crud._delete_html_template(db, _MODEL, company_id, template_id)


def activate_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID):
    return crud._activate_html_template(db, _MODEL, company_id, template_id)


# ── generated letters ────────────────────────────────────────────────────────

def list_letters(db: Session, company_id: uuid.UUID, employee_id: uuid.UUID,
                 *, current_only: bool = False) -> list[models.AppointmentLetter]:
    q = select(models.AppointmentLetter).where(
        models.AppointmentLetter.company_id == company_id, models.AppointmentLetter.employee_id == employee_id)
    if current_only:
        q = q.where(models.AppointmentLetter.is_current.is_(True))
    return list(db.scalars(q.order_by(models.AppointmentLetter.version.desc())).all())


def next_version(db: Session, company_id: uuid.UUID, employee_id: uuid.UUID) -> int:
    return max((l.version for l in list_letters(db, company_id, employee_id)), default=0) + 1


def create_letter(db: Session, company: models.Company, employee: models.Employee,
                  template: models.AppointmentLetterTemplate, *, generated_by_user: models.User,
                  notes: str | None) -> models.AppointmentLetter:
    """Renders [template] with the employee's real data, stores the PDF and
    records an immutable snapshot as a new version (earlier versions stay,
    marked not current)."""
    from .storage import uploads_root, winlong_path

    # Serialise concurrent generations for the same employee (double click,
    # two HR users): the second waits here and then gets the next version.
    db.execute(select(models.Employee.id).where(models.Employee.id == employee.id).with_for_update())
    previous = list_letters(db, company.id, employee.id)
    version = max((p.version for p in previous), default=0) + 1
    issue_date = crud.company_today(db, company.id)
    placeholders = build_placeholders(
        db, company, employee, signatory_name=template.signatory_name,
        signatory_designation=template.signatory_designation, issue_date=issue_date, version=version,
        seal_image_path=template.seal_image_path, signature_image_path=template.signature_image_path,
    )
    rendered = render(template.html_body, template.css_styles, placeholders)
    pdf_bytes = tr.render_html_to_pdf(rendered)

    letter_id = uuid.uuid4()
    folder = uploads_root() / "appointment_letter" / str(employee.id)
    winlong_path(folder).mkdir(parents=True, exist_ok=True)
    file_name = f"{letter_id}.pdf"
    winlong_path(folder / file_name).write_bytes(pdf_bytes)

    for p in previous:
        p.is_current = False
    row = models.AppointmentLetter(
        id=letter_id, company_id=company.id, employee_id=employee.id, version=version,
        letter_number=placeholders["letter_number"], template_id=template.id, template_name=template.name,
        template_version=template.version, rendered_html=rendered,
        placeholders={k: v for k, v in placeholders.items() if k not in IMAGE_PLACEHOLDERS},
        file_url=f"appointment_letter/{employee.id}/{file_name}", file_size=len(pdf_bytes), is_current=True,
        notes=(notes or "").strip() or None, generated_by=generated_by_user.id,
        generated_by_name=(crud.employee_display_name(db, generated_by_user.employee_id)
                           if generated_by_user.employee_id else None),
        generated_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def letter_file_bytes(letter: models.AppointmentLetter) -> bytes | None:
    from .storage import uploads_root, winlong_path

    root = uploads_root().resolve()
    path = (root / letter.file_url).resolve()
    if root not in path.parents:
        return None
    long_path = winlong_path(path)
    return long_path.read_bytes() if long_path.is_file() else None
