"""Appointment Letter (app/appointment_letters.py).

Documents > Templates (HR: Owner, or Edit/Admin on Recruitment / Payroll
(Process) / System Settings -- deps.require_hr_documents):
    GET    /api/appointment-letter-templates                      list (a standard one is created on first use)
    GET    /api/appointment-letter-templates/meta                 placeholders + standard layout
    POST   /api/appointment-letter-templates                      create
    PUT    /api/appointment-letter-templates/{id}                 save (version + 1)
    DELETE /api/appointment-letter-templates/{id}                 delete (not the active one)
    POST   /api/appointment-letter-templates/{id}/activate        make active
    POST/DELETE /api/appointment-letter-templates/{id}/images/{seal|signature}
    POST   /api/appointment-letter-templates/preview              render an unsaved draft for a real employee

Employee Profile > Documents > Appointment Letter (HR, limited to the
employees the caller may see):
    POST /api/employees/{id}/appointment-letters/preview          the active template, no save
    POST /api/employees/{id}/appointment-letters                  generate / regenerate (new version)

Employee access (own letters) and HR:
    GET  /api/employees/{id}/appointment-letters                  list (employees: current version only)
    GET  /api/appointment-letters/{id}/pdf                        stored PDF (never re-rendered)
    GET  /api/appointment-letters/{id}/html                       stored HTML snapshot
"""

from __future__ import annotations

import base64
import binascii
import datetime
import logging
import uuid
from pathlib import Path as Path_

from fastapi import APIRouter, Depends, File, HTTPException, Path, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import appointment_letters as al, crud, models, template_designs
from ..database import get_db
from ..deps import HR_DOCUMENT_COLUMNS, get_current_user, has_column_access, require_hr_documents
from ..exit_letter_html_renderer import embed_letter_image
from ..storage import delete_uploaded_file, save_uploaded_file

router = APIRouter(prefix="/api", tags=["appointment-letters"])
logger = logging.getLogger(__name__)


# ── schemas ──────────────────────────────────────────────────────────────────

class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    html_body: str = Field(min_length=1)
    css_styles: str = ""
    is_active: bool = False
    signatory_name: str | None = Field(default=None, max_length=120)
    signatory_designation: str | None = Field(default=None, max_length=120)


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    html_body: str
    css_styles: str
    is_active: bool
    version: int
    signatory_name: str | None = None
    signatory_designation: str | None = None
    updated_at: datetime.datetime | None = None
    # Inline previews for the HR-only editor (the files are never served by /media).
    seal_image_data_url: str | None = None
    signature_image_data_url: str | None = None


class PreviewIn(BaseModel):
    html_body: str = Field(min_length=1)
    css_styles: str = ""
    # Real employee whose data is substituted -- never sample data.
    employee_id: uuid.UUID
    signatory_name: str | None = Field(default=None, max_length=120)
    signatory_designation: str | None = Field(default=None, max_length=120)
    # The saved template whose seal / signature to use when not overridden.
    template_id: uuid.UUID | None = None
    # Unsaved seal / signature: null = the saved image, "" = none, else a PNG/JPG data URL.
    seal_image_data_url: str | None = Field(default=None, max_length=1_500_000)
    signature_image_data_url: str | None = Field(default=None, max_length=1_500_000)


class RenderedOut(BaseModel):
    rendered_html: str


class GenerateIn(BaseModel):
    notes: str | None = Field(default=None, max_length=500)


class LetterOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    letter_title: str = al.LETTER_TITLE
    version: int
    letter_number: str
    template_name: str
    template_version: int
    is_current: bool
    notes: str | None = None
    file_size: int
    generated_by_name: str | None = None
    generated_at: datetime.datetime | None = None


# ── helpers ──────────────────────────────────────────────────────────────────

def _visible(db: Session, user: models.User, employee_id: uuid.UUID) -> bool:
    ids = crud.get_visible_employee_ids_for_docs(db, user)
    return ids is None or employee_id in ids


def _employee_for_hr(db: Session, user: models.User, employee_id: uuid.UUID) -> models.Employee:
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not _visible(db, user, employee.id):
        raise HTTPException(
            status_code=403,
            detail="This employee is outside your document access scope (your reporting team, or the "
                   "departments you are HR Representative for). Ask the Owner or that department's "
                   "HR Representative.",
        )
    return employee


def _template_out(row: models.AppointmentLetterTemplate) -> TemplateOut:
    out = TemplateOut.model_validate(row)
    if row.seal_image_path:
        out.seal_image_data_url = embed_letter_image(row.seal_image_path)
    if row.signature_image_path:
        out.signature_image_data_url = embed_letter_image(row.signature_image_path)
    return out


def _template_or_404(db: Session, user: models.User, template_id: uuid.UUID) -> models.AppointmentLetterTemplate:
    row = al.get_template(db, user.company_id, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    return row


def _validate(html_body: str, css_styles: str) -> None:
    error = al.validate_template_html(html_body, css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)


def _letter_out(letter: models.AppointmentLetter) -> LetterOut:
    return LetterOut(
        id=letter.id, employee_id=letter.employee_id, version=letter.version, letter_number=letter.letter_number,
        template_name=letter.template_name, template_version=letter.template_version, is_current=letter.is_current,
        notes=letter.notes, file_size=letter.file_size, generated_by_name=letter.generated_by_name,
        generated_at=letter.generated_at,
    )


# ── templates ────────────────────────────────────────────────────────────────

@router.get("/appointment-letter-templates", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db), current_user: models.User = Depends(require_hr_documents)):
    al.ensure_default_template(db, current_user.company_id, current_user.employee_id)
    db.commit()
    return [_template_out(t) for t in al.list_templates(db, current_user.company_id)]


@router.get("/appointment-letter-templates/meta")
def template_meta(_: models.User = Depends(require_hr_documents)):
    """Placeholder list + the standard layout ("Use standard layout")."""
    return {"placeholders": al.PLACEHOLDERS, "standard_html": template_designs.APPOINTMENT_LAYOUT,
            "standard_css": al.default_css()}


@router.post("/appointment-letter-templates", response_model=TemplateOut, status_code=201)
def create_template(payload: TemplateIn, db: Session = Depends(get_db),
                    current_user: models.User = Depends(require_hr_documents)):
    _validate(payload.html_body, payload.css_styles)
    row = al.create_template(
        db, current_user.company_id, name=payload.name, html_body=payload.html_body, css_styles=payload.css_styles,
        is_active=payload.is_active, signatory_name=payload.signatory_name,
        signatory_designation=payload.signatory_designation, created_by=current_user.employee_id,
    )
    crud.create_audit_log(db, current_user.company_id, current_user.id, "create", "appointment_letter_template", row.id)
    db.commit()
    db.refresh(row)
    return _template_out(row)


@router.put("/appointment-letter-templates/{template_id}", response_model=TemplateOut)
def update_template(template_id: uuid.UUID, payload: TemplateIn, db: Session = Depends(get_db),
                    current_user: models.User = Depends(require_hr_documents)):
    _validate(payload.html_body, payload.css_styles)
    try:
        row = al.update_template(
            db, current_user.company_id, template_id, name=payload.name, html_body=payload.html_body,
            css_styles=payload.css_styles, is_active=payload.is_active, signatory_name=payload.signatory_name,
            signatory_designation=payload.signatory_designation, updated_by=current_user.employee_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "appointment_letter_template",
                          row.id, changes={"version": str(row.version)})
    db.commit()
    db.refresh(row)
    return _template_out(row)


@router.delete("/appointment-letter-templates/{template_id}", status_code=204)
def delete_template(template_id: uuid.UUID, db: Session = Depends(get_db),
                    current_user: models.User = Depends(require_hr_documents)):
    row = _template_or_404(db, current_user, template_id)
    images = [p for p in (row.seal_image_path, row.signature_image_path) if p]
    try:
        al.delete_template(db, current_user.company_id, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "delete", "appointment_letter_template",
                          template_id)
    db.commit()
    for path in images:
        delete_uploaded_file(path)


@router.post("/appointment-letter-templates/{template_id}/activate", response_model=TemplateOut)
def activate_template(template_id: uuid.UUID, db: Session = Depends(get_db),
                      current_user: models.User = Depends(require_hr_documents)):
    try:
        row = al.activate_template(db, current_user.company_id, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "appointment_letter_template", row.id)
    db.commit()
    db.refresh(row)
    return _template_out(row)


# Seal / signature: PNG or JPG verified by content (no SVG -- it can carry
# script), small enough to embed in every generated PDF.
_IMAGE_KIND = Path(..., pattern="^(seal|signature)$")
_IMAGE_COLUMN = {"seal": "seal_image_path", "signature": "signature_image_path"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
_IMAGE_MAX_BYTES = 1024 * 1024


def _is_png_or_jpg(raw: bytes) -> bool:
    return raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")


@router.post("/appointment-letter-templates/{template_id}/images/{kind}", response_model=TemplateOut)
def upload_template_image(  # sync def -> threadpool (blocking file/DB I/O)
    template_id: uuid.UUID,
    file: UploadFile = File(...),
    kind: str = _IMAGE_KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """Company seal or authorized signature printed on letters generated from
    this template from now on. Bumps the template version; letters already
    issued keep their own snapshot."""
    label = "company seal" if kind == "seal" else "signature"
    if Path_(file.filename or "").suffix.lower() not in _IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only PNG and JPG images are supported for the {label}.")
    if not _is_png_or_jpg(file.file.read(8)):
        raise HTTPException(status_code=400, detail="The selected file is not a valid PNG or JPG image.")
    file.file.seek(0)
    row = _template_or_404(db, current_user, template_id)
    file_url, size_bytes = save_uploaded_file(file, entity_type="appointment_letter_asset",
                                              entity_id=current_user.company_id)
    if size_bytes > _IMAGE_MAX_BYTES:
        delete_uploaded_file(file_url)
        raise HTTPException(status_code=400, detail=f"The {label} image must be smaller than 1MB.")
    column = _IMAGE_COLUMN[kind]
    previous = getattr(row, column)
    setattr(row, column, file_url)
    row.version += 1
    row.updated_by = current_user.employee_id
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update",
                          f"appointment_letter_{kind}", row.id, changes={"version": str(row.version)})
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_uploaded_file(file_url)
        raise
    if previous:
        delete_uploaded_file(previous)
    db.refresh(row)
    return _template_out(row)


@router.delete("/appointment-letter-templates/{template_id}/images/{kind}", response_model=TemplateOut)
def delete_template_image(template_id: uuid.UUID, kind: str = _IMAGE_KIND, db: Session = Depends(get_db),
                          current_user: models.User = Depends(require_hr_documents)):
    row = _template_or_404(db, current_user, template_id)
    column = _IMAGE_COLUMN[kind]
    previous = getattr(row, column)
    if previous:
        setattr(row, column, None)
        row.version += 1
        row.updated_by = current_user.employee_id
        row.updated_at = datetime.datetime.now(datetime.timezone.utc)
        crud.create_audit_log(db, current_user.company_id, current_user.id, "update",
                              f"appointment_letter_{kind}", row.id, changes={"version": str(row.version)})
        db.commit()
        delete_uploaded_file(previous)
        db.refresh(row)
    return _template_out(row)


def _draft_image(data_url: str) -> str:
    """An editor's unsaved seal / signature for the preview only: "" = none;
    otherwise a PNG/JPG data URL whose bytes really are PNG/JPG."""
    if not data_url:
        return embed_letter_image(None)
    prefix, _, encoded = data_url.partition(",")
    if prefix not in ("data:image/png;base64", "data:image/jpeg;base64"):
        raise HTTPException(status_code=400, detail="Seal and signature images must be PNG or JPG.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="The selected image could not be read.") from exc
    if len(raw) > _IMAGE_MAX_BYTES or not _is_png_or_jpg(raw):
        raise HTTPException(status_code=400, detail="Seal and signature images must be PNG or JPG, under 1MB.")
    return data_url


@router.post("/appointment-letter-templates/preview", response_model=RenderedOut)
def preview_template(payload: PreviewIn, db: Session = Depends(get_db),
                     current_user: models.User = Depends(require_hr_documents)):
    """The in-progress (unsaved) HTML/CSS rendered against a real employee."""
    _validate(payload.html_body, payload.css_styles)
    employee = _employee_for_hr(db, current_user, payload.employee_id)
    company = db.get(models.Company, current_user.company_id)
    saved = _template_or_404(db, current_user, payload.template_id) if payload.template_id else None
    placeholders = al.build_placeholders(
        db, company, employee, signatory_name=payload.signatory_name,
        signatory_designation=payload.signatory_designation, issue_date=crud.company_today(db, company.id),
        version=al.next_version(db, company.id, employee.id),
        seal_image_path=saved.seal_image_path if saved else None,
        signature_image_path=saved.signature_image_path if saved else None,
    )
    for key, draft in (("company_seal", payload.seal_image_data_url),
                       ("authorized_signature", payload.signature_image_data_url)):
        if draft is not None:
            placeholders[key] = _draft_image(draft)
    return RenderedOut(rendered_html=al.render(payload.html_body, payload.css_styles, placeholders))


# ── per-employee letters ─────────────────────────────────────────────────────

def _active_template_or_400(db: Session, company_id: uuid.UUID) -> models.AppointmentLetterTemplate:
    al.ensure_default_template(db, company_id, None)
    template = al.active_template(db, company_id)
    if template is None:
        raise HTTPException(
            status_code=400,
            detail="No active Appointment Letter template -- make one active under Documents > Templates > "
                   "Appointment Letter first.",
        )
    return template


@router.post("/employees/{employee_id}/appointment-letters/preview", response_model=RenderedOut)
def preview_letter(employee_id: uuid.UUID, db: Session = Depends(get_db),
                   current_user: models.User = Depends(require_hr_documents)):
    employee = _employee_for_hr(db, current_user, employee_id)
    company = db.get(models.Company, current_user.company_id)
    template = _active_template_or_400(db, company.id)
    db.commit()  # keeps a just-created standard template
    placeholders = al.build_placeholders(
        db, company, employee, signatory_name=template.signatory_name,
        signatory_designation=template.signatory_designation, issue_date=crud.company_today(db, company.id),
        version=al.next_version(db, company.id, employee.id),
        seal_image_path=template.seal_image_path, signature_image_path=template.signature_image_path,
    )
    return RenderedOut(rendered_html=al.render(template.html_body, template.css_styles, placeholders))


@router.post("/employees/{employee_id}/appointment-letters", response_model=LetterOut, status_code=201)
def generate_letter(employee_id: uuid.UUID, payload: GenerateIn, db: Session = Depends(get_db),
                    current_user: models.User = Depends(require_hr_documents)):
    """Generate (first time) or Regenerate (new version) from the active
    template and the employee's current real data."""
    employee = _employee_for_hr(db, current_user, employee_id)
    company = db.get(models.Company, current_user.company_id)
    template = _active_template_or_400(db, company.id)
    try:
        letter = al.create_letter(db, company, employee, template, generated_by_user=current_user,
                                  notes=payload.notes)
    except Exception as exc:  # PDF engine / storage failure -- nothing is saved
        db.rollback()
        logger.exception("Appointment Letter generation failed for employee %s", employee_id)
        raise HTTPException(
            status_code=503,
            detail="The PDF could not be generated right now. Please try again; if it keeps failing, "
                   "ask your administrator to check the server's PDF engine (see README, Backend setup).",
        ) from exc
    crud.create_audit_log(
        db, company.id, current_user.id, "generate", "appointment_letter", letter.id,
        changes={"employee_id": str(employee.id), "version": str(letter.version),
                 "template_version": str(letter.template_version)},
    )
    db.commit()
    db.refresh(letter)
    return _letter_out(letter)


def _can_read_letter(db: Session, user: models.User, letter: models.AppointmentLetter) -> bool:
    if letter.company_id != user.company_id:
        return False
    if user.employee_id == letter.employee_id:
        return True
    return has_column_access(db, user, *HR_DOCUMENT_COLUMNS) and _visible(db, user, letter.employee_id)


@router.get("/employees/{employee_id}/appointment-letters", response_model=list[LetterOut])
def list_employee_letters(employee_id: uuid.UUID, db: Session = Depends(get_db),
                          current_user: models.User = Depends(get_current_user)):
    """Employees see their own current letter; HR sees every version for
    employees they may see."""
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    is_self = current_user.employee_id == employee_id
    is_hr = (not is_self and has_column_access(db, current_user, *HR_DOCUMENT_COLUMNS)
             and _visible(db, current_user, employee_id))
    if not (is_self or is_hr):
        raise HTTPException(status_code=403, detail="You don't have permission to view these letters")
    return [_letter_out(l) for l in al.list_letters(db, current_user.company_id, employee_id, current_only=not is_hr)]


def _letter_or_404(db: Session, user: models.User, letter_id: uuid.UUID) -> models.AppointmentLetter:
    letter = db.get(models.AppointmentLetter, letter_id)
    if letter is None or not _can_read_letter(db, user, letter):
        raise HTTPException(status_code=404, detail="Letter not found")
    return letter


@router.get("/appointment-letters/{letter_id}/pdf")
def download_letter_pdf(letter_id: uuid.UUID, db: Session = Depends(get_db),
                        current_user: models.User = Depends(get_current_user)):
    """The PDF exactly as generated (stored file -- never re-rendered)."""
    letter = _letter_or_404(db, current_user, letter_id)
    content = al.letter_file_bytes(letter)
    if content is None:
        raise HTTPException(status_code=410, detail="The stored PDF for this letter is missing.")
    employee = db.get(models.Employee, letter.employee_id)
    code = "".join(c if c.isalnum() else "-" for c in (employee.employee_code if employee else "employee"))
    return Response(
        content=content, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="appointment-letter-{code}-v{letter.version}.pdf"'},
    )


@router.get("/appointment-letters/{letter_id}/html", response_model=RenderedOut)
def view_letter_html(letter_id: uuid.UUID, db: Session = Depends(get_db),
                     current_user: models.User = Depends(get_current_user)):
    return RenderedOut(rendered_html=_letter_or_404(db, current_user, letter_id).rendered_html)
