"""Freelancer onboarding APIs.

* /api/freelancer-defaults -- Administration > General Settings > Freelancer
  Defaults: what Add Employee pre-selects for Employment Type = Freelancer
  (per company; values are only starting points the user can change).
* /api/agreement-templates -- Documents > Templates > NDA / Freelancer
  Agreement (HR documents access): list (the predefined ones are seeded on
  first use), create, edit, activate, delete, preview.
* /api/me/agreements -- the signed-in Freelancer's documents to accept on
  first login, and POST .../accept.
* /api/employees/{id}/agreements -- an employee's acceptance history.
See app/agreements.py.
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import agreements, crud, currencies, models, template_rendering
from .. import employee_validation as ev
from ..database import get_db
from ..deps import get_current_user, require_hr_documents, require_people_access_or_self, require_permission

router = APIRouter(prefix="/api", tags=["freelancer-onboarding"])


# -- Freelancer defaults ------------------------------------------------------

class FreelancerDefaultsIn(BaseModel):
    band_number: int | None = Field(default=None, ge=1, le=99)
    role_name: str | None = Field(default=None, max_length=80)
    reporting_manager_id: uuid.UUID | None = None
    designation_name: str | None = Field(default=None, max_length=120)
    branch_name: str | None = Field(default=None, max_length=120)
    employment_status: str | None = Field(default=None, max_length=20)
    rate_currency: str | None = Field(default=None, max_length=3)
    rate_unit: str | None = Field(default=None, pattern="^(hourly|daily)$")


class FreelancerDefaultsOut(FreelancerDefaultsIn):
    reporting_manager_name: str | None = None
    # The company's default currency (used when rate_currency is empty).
    company_currency: str
    configured: bool
    updated_at: datetime.datetime | None = None


def _defaults_out(db: Session, company_id: uuid.UUID) -> FreelancerDefaultsOut:
    row = db.get(models.FreelancerDefaults, company_id)
    company_currency = crud.company_currency(db, company_id)
    if row is None:
        return FreelancerDefaultsOut(employment_status="Active", company_currency=company_currency, configured=False)
    manager = db.get(models.Employee, row.reporting_manager_id) if row.reporting_manager_id else None
    if manager is not None and (manager.company_id != company_id or not manager.is_active):
        manager = None  # left / moved since -- don't pre-select an inactive manager
    return FreelancerDefaultsOut(
        band_number=row.band_number, role_name=row.role_name,
        reporting_manager_id=manager.id if manager else None,
        reporting_manager_name=f"{manager.first_name} {manager.last_name or ''}".strip() if manager else None,
        designation_name=row.designation_name, branch_name=row.branch_name,
        employment_status=row.employment_status or "Active",
        rate_currency=row.rate_currency, rate_unit=row.rate_unit,
        company_currency=company_currency, configured=True, updated_at=row.updated_at,
    )


@router.get("/freelancer-defaults", response_model=FreelancerDefaultsOut)
def get_freelancer_defaults(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """This company's Freelancer defaults (nothing configured -> only
    Employment Status Active)."""
    return _defaults_out(db, current_user.company_id)


@router.put("/freelancer-defaults", response_model=FreelancerDefaultsOut)
def put_freelancer_defaults(
    payload: FreelancerDefaultsIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    """Saves this company's Freelancer defaults; every value is validated
    against the company's own Bands / Roles / employees / Branches."""
    cid = current_user.company_id

    def blank(v: str | None) -> str | None:
        return (v or "").strip() or None

    band = payload.band_number
    if band is not None:
        bands = set(db.scalars(select(models.Band.band_number).where(
            models.Band.company_id == cid, models.Band.is_active.is_(True))))
        if bands and band not in bands:
            raise HTTPException(status_code=422, detail=f"Band {band} isn't one of this company's active bands.")
    role = blank(payload.role_name)
    if role is not None and db.scalar(select(models.Role.id).where(
            models.Role.company_id == cid, func.lower(models.Role.name) == role.lower())) is None:
        raise HTTPException(status_code=422, detail=f"Role '{role}' doesn't exist in this company.")
    if payload.reporting_manager_id is not None:
        m = db.get(models.Employee, payload.reporting_manager_id)
        if m is None or m.company_id != cid or not m.is_active:
            raise HTTPException(status_code=422, detail="The Reporting Manager must be an active employee of this company.")
    branch = blank(payload.branch_name)
    if branch is not None and db.scalar(select(models.Branch.id).where(
            models.Branch.company_id == cid, func.lower(models.Branch.name) == branch.lower())) is None:
        raise HTTPException(status_code=422, detail=f"Branch '{branch}' doesn't exist in this company.")
    status = blank(payload.employment_status)
    if status is not None:
        normalized = ev.EMPLOYEE_STATUSES.get(status.lower().replace(" ", "_"))
        if normalized not in ("Active", "Probation"):
            raise HTTPException(status_code=422, detail="Employment Status must be Active or Probation.")
        status = normalized
    try:
        currency = currencies.normalize_code(payload.rate_currency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = db.get(models.FreelancerDefaults, cid)
    if row is None:
        row = models.FreelancerDefaults(company_id=cid)
        db.add(row)
    row.band_number = band
    row.role_name = role
    row.reporting_manager_id = payload.reporting_manager_id
    row.designation_name = blank(payload.designation_name)
    row.branch_name = branch
    row.employment_status = status or "Active"
    row.rate_currency = currency
    row.rate_unit = payload.rate_unit
    row.updated_by = current_user.id
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    crud.create_audit_log(db, cid, current_user.id, "update", "freelancer_defaults", cid)
    db.commit()
    return _defaults_out(db, cid)


# -- NDA / Agreement templates -------------------------------------------------

_DOC_TYPE = "^(nda|agreement)$"


class AgreementTemplateIn(BaseModel):
    doc_type: str = Field(pattern=_DOC_TYPE)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=300)
    html_body: str = Field(min_length=1, max_length=200_000)
    css_styles: str = Field(default="", max_length=100_000)
    is_active: bool = False


class AgreementTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=300)
    html_body: str | None = Field(default=None, min_length=1, max_length=200_000)
    css_styles: str | None = Field(default=None, max_length=100_000)


class AgreementTemplateOut(BaseModel):
    id: uuid.UUID
    doc_type: str
    doc_type_label: str
    name: str
    description: str | None = None
    html_body: str
    css_styles: str
    is_active: bool
    version: int
    is_builtin: bool
    acceptance_count: int
    updated_at: datetime.datetime | None = None


class PreviewIn(BaseModel):
    html_body: str = Field(max_length=200_000)
    css_styles: str = Field(default="", max_length=100_000)


class RenderedOut(BaseModel):
    rendered_html: str


def _tpl_out(db: Session, t: models.AgreementTemplate) -> AgreementTemplateOut:
    return AgreementTemplateOut(
        id=t.id, doc_type=t.doc_type, doc_type_label=agreements.DOC_TYPES.get(t.doc_type, t.doc_type),
        name=t.name, description=t.description, html_body=t.html_body, css_styles=t.css_styles or "",
        is_active=t.is_active, version=t.version, is_builtin=t.builtin_key is not None,
        acceptance_count=agreements.acceptance_count(db, t.id), updated_at=t.updated_at,
    )


def _check_html(html_body: str, css: str) -> None:
    error = template_rendering.validate_template_html(html_body, css)
    if error:
        raise HTTPException(status_code=422, detail=error)


def _require_template(db: Session, user: models.User, template_id: uuid.UUID) -> models.AgreementTemplate:
    t = agreements.get_template(db, user.company_id, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


@router.get("/agreement-templates/placeholders")
def agreement_placeholders(_: models.User = Depends(require_hr_documents)):
    return {"placeholders": agreements.PLACEHOLDERS, "doc_types": agreements.DOC_TYPES}


@router.get("/agreement-templates", response_model=list[AgreementTemplateOut])
def list_agreement_templates(
    doc_type: str | None = Query(default=None, pattern=_DOC_TYPE),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    agreements.ensure_builtin_templates(db, current_user.company_id)
    db.commit()
    return [_tpl_out(db, t) for t in agreements.list_templates(db, current_user.company_id, doc_type)]


@router.post("/agreement-templates", response_model=AgreementTemplateOut, status_code=201)
def create_agreement_template(
    payload: AgreementTemplateIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    _check_html(payload.html_body, payload.css_styles)
    now = datetime.datetime.now(datetime.timezone.utc)
    t = models.AgreementTemplate(
        id=uuid.uuid4(), company_id=current_user.company_id, doc_type=payload.doc_type,
        name=payload.name.strip(), description=(payload.description or "").strip() or None,
        html_body=payload.html_body, css_styles=payload.css_styles or "", is_active=False, version=1,
        created_by=current_user.id, created_at=now, updated_by=current_user.id, updated_at=now,
    )
    db.add(t)
    db.flush()
    if payload.is_active:
        agreements.activate_template(db, t)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "create", "agreement_template", t.id)
    db.commit()
    return _tpl_out(db, t)


@router.put("/agreement-templates/{template_id}", response_model=AgreementTemplateOut)
def update_agreement_template(
    template_id: uuid.UUID,
    payload: AgreementTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """Editing the content (HTML / CSS) bumps the version, so Freelancers
    accept the new text again; a rename or description change doesn't."""
    t = _require_template(db, current_user, template_id)
    html_body = payload.html_body if payload.html_body is not None else t.html_body
    css = payload.css_styles if payload.css_styles is not None else (t.css_styles or "")
    _check_html(html_body, css)
    if html_body != t.html_body or css != (t.css_styles or ""):
        t.html_body, t.css_styles = html_body, css
        t.version = (t.version or 1) + 1
    if payload.name is not None:
        t.name = payload.name.strip()
    if payload.description is not None:
        t.description = payload.description.strip() or None
    t.updated_by = current_user.id
    t.updated_at = datetime.datetime.now(datetime.timezone.utc)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "agreement_template", t.id)
    db.commit()
    return _tpl_out(db, t)


@router.post("/agreement-templates/{template_id}/activate", response_model=AgreementTemplateOut)
def activate_agreement_template(
    template_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """Makes this the template Freelancers must accept for its type."""
    t = _require_template(db, current_user, template_id)
    agreements.activate_template(db, t)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "agreement_template", t.id,
                          changes={"activated": True})
    db.commit()
    return _tpl_out(db, t)


@router.delete("/agreement-templates/{template_id}", status_code=204)
def delete_agreement_template(
    template_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """The active template and any template someone has accepted are kept
    (the acceptance record points at it)."""
    t = _require_template(db, current_user, template_id)
    if t.is_active:
        raise HTTPException(status_code=409, detail="Activate another template of this type before deleting this one.")
    if agreements.acceptance_count(db, t.id):
        raise HTTPException(status_code=409, detail="This template has been accepted by employees and is kept as a record.")
    db.delete(t)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "delete", "agreement_template", template_id)
    db.commit()


@router.post("/agreement-templates/preview", response_model=RenderedOut)
def preview_agreement_html(
    payload: PreviewIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """Live preview of unsaved HTML / CSS with sample freelancer values."""
    _check_html(payload.html_body, payload.css_styles)
    company = db.get(models.Company, current_user.company_id)
    values = agreements.placeholders_for(db, company, None, crud.company_today(db, company.id))
    return RenderedOut(rendered_html=agreements.render(payload.html_body, payload.css_styles, values))


@router.get("/agreement-templates/{template_id}/preview", response_model=RenderedOut)
def preview_agreement_template(
    template_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    t = _require_template(db, current_user, template_id)
    company = db.get(models.Company, current_user.company_id)
    values = agreements.placeholders_for(db, company, None, crud.company_today(db, company.id))
    return RenderedOut(rendered_html=agreements.render(t.html_body, t.css_styles, values))


# -- First-login acceptance ------------------------------------------------------

class PendingDocOut(BaseModel):
    template_id: uuid.UUID
    doc_type: str
    doc_type_label: str
    name: str
    version: int
    rendered_html: str


class AcceptanceOut(BaseModel):
    id: uuid.UUID
    template_id: uuid.UUID
    doc_type: str
    doc_type_label: str
    template_name: str
    template_version: int
    accepted_at: datetime.datetime


class MyAgreementsOut(BaseModel):
    required: bool
    pending: list[PendingDocOut]
    accepted: list[AcceptanceOut]


class AcceptIn(BaseModel):
    version: int = Field(ge=1)


def _acceptance_out(a: models.AgreementAcceptance) -> AcceptanceOut:
    return AcceptanceOut(
        id=a.id, template_id=a.template_id, doc_type=a.doc_type,
        doc_type_label=agreements.DOC_TYPES.get(a.doc_type, a.doc_type),
        template_name=a.template_name, template_version=a.template_version, accepted_at=a.accepted_at,
    )


def _my_agreements(db: Session, user: models.User) -> MyAgreementsOut:
    employee = db.get(models.Employee, user.employee_id) if user.employee_id else None
    if employee is None or not agreements.requires_acceptance(employee):
        return MyAgreementsOut(required=False, pending=[], accepted=[])
    pending = agreements.pending_templates(db, employee)
    db.commit()  # ensure_builtin_templates may have seeded
    company = db.get(models.Company, employee.company_id)
    values = agreements.placeholders_for(db, company, employee, crud.company_today(db, company.id))
    return MyAgreementsOut(
        required=True,
        pending=[PendingDocOut(
            template_id=t.id, doc_type=t.doc_type, doc_type_label=agreements.DOC_TYPES.get(t.doc_type, t.doc_type),
            name=t.name, version=t.version, rendered_html=agreements.render(t.html_body, t.css_styles, values),
        ) for t in pending],
        accepted=[_acceptance_out(a) for a in agreements.acceptances_for(db, employee.id)],
    )


@router.get("/me/agreements", response_model=MyAgreementsOut)
def my_agreements(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The NDA / Agreement documents the signed-in Freelancer still has to
    accept before using the app (required=False for everyone else)."""
    return _my_agreements(db, current_user)


@router.post("/me/agreements/{template_id}/accept", response_model=MyAgreementsOut)
def accept_my_agreement(
    template_id: uuid.UUID,
    payload: AcceptIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    employee = db.get(models.Employee, current_user.employee_id) if current_user.employee_id else None
    if employee is None or not agreements.requires_acceptance(employee):
        raise HTTPException(status_code=409, detail="Only Freelancers accept these documents.")
    t = _require_template(db, current_user, template_id)
    if not t.is_active:
        raise HTTPException(status_code=409, detail="This document is no longer in use -- please reload.")
    try:
        row = agreements.accept(db, employee, current_user.id, t, payload.version,
                                request.client.host if request.client else None,
                                request.headers.get("user-agent"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "create", "agreement_acceptance", row.id,
                          changes={"template": t.name, "version": t.version, "doc_type": t.doc_type})
    db.commit()
    return _my_agreements(db, current_user)


@router.get("/employees/{employee_id}/agreements", response_model=list[AcceptanceOut])
def employee_agreements(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    """Employee Profile: which NDA / Agreement versions this employee accepted, and when."""
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    return [_acceptance_out(a) for a in agreements.acceptances_for(db, employee_id)]


@router.get("/agreement-acceptances/{acceptance_id}/html", response_model=RenderedOut)
def acceptance_html(
    acceptance_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The exact document text an employee accepted -- the employee
    themselves, or HR documents access."""
    a = db.get(models.AgreementAcceptance, acceptance_id)
    if a is None or a.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Not found")
    if a.employee_id != current_user.employee_id:
        require_hr_documents(current_user, db)
    return RenderedOut(rendered_html=a.rendered_html or "")
