"""Experience & Relieving Letter (one combined letter per exit).

Documents > Templates (HR: Owner, or Edit/Admin on Recruitment / Payroll
(Process) / System Settings -- deps.require_hr_documents):
    GET  /api/exit-letter-templates/{letter_type}            saved template (or null)
    PUT  /api/exit-letter-templates/{letter_type}            save (version + 1)
    POST /api/exit-letter-templates/{letter_type}/preview    render an unsaved draft
                                                             against a real exit request
    POST/DELETE /api/exit-letter-templates/{letter_type}/images/{seal|signature}
                                                             company seal / authorized
                                                             signature image (PNG/JPG)

People > Offboarding > Experience & Relieving Documents (HR, limited to the
employees the caller may see):
    GET  /api/exit-letters/exit-requests                     exit requests + eligibility + current letters
    GET  /api/exit-requests/{exit_id}/letters                one exit: eligibility + full history
    POST /api/exit-requests/{exit_id}/letters/{type}/preview render the saved template, no save
    POST /api/exit-requests/{exit_id}/letters/{type}         generate / regenerate (new version)

Employee access (own letters) and HR:
    GET  /api/employees/{employee_id}/exit-letters           list (employees: current versions only)
    GET  /api/exit-letters/{letter_id}/pdf                   stored PDF (never re-rendered)
    GET  /api/exit-letters/{letter_id}/html                  stored HTML snapshot
"""

from __future__ import annotations

import datetime
import logging
import uuid
from pathlib import Path as Path_

from fastapi import APIRouter, Depends, File, HTTPException, Path, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud, exit_letter_html_renderer as renderer, models, schemas
from ..database import get_db
from ..deps import HR_DOCUMENT_COLUMNS, get_current_user, has_column_access, require_hr_documents
from ..storage import delete_uploaded_file, save_uploaded_file

router = APIRouter(prefix="/api", tags=["exit-letters"])
logger = logging.getLogger(__name__)

# Experience & Relieving Letter and Full & Final Settlement Statement.
_TYPE = Path(..., pattern="^(experience_relieving|fnf_statement)$")


def _visible(db: Session, user: models.User, employee_id: uuid.UUID) -> bool:
    ids = crud.get_visible_employee_ids_for_docs(db, user)
    return ids is None or employee_id in ids


def _payroll(db: Session, user: models.User) -> bool:
    return has_column_access(db, user, "payroll_process")


def _require_type_access(db: Session, user: models.User, letter_type: str) -> None:
    """The F&F Settlement Statement shows salary figures: Payroll (Process)
    Edit/Admin (or the Owner) only -- the letter template itself holds no
    employee data and stays with HR documents access."""
    if letter_type == "fnf_statement" and not _payroll(db, user):
        raise HTTPException(status_code=403, detail="The F&F Settlement Statement requires Payroll (Process) access.")


def _detail_for_hr(db: Session, user: models.User, exit_id: uuid.UUID) -> dict:
    detail = crud.get_exit_letter_detail(db, user.company_id, exit_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Exit request not found")
    if not _visible(db, user, detail["employee"].id):
        # M-10: a real record outside the caller's document scope is a 403
        # with the reason, not a misleading 404.
        raise HTTPException(
            status_code=403,
            detail="This employee is outside your document access scope (your reporting team, or the "
                   "departments you are HR Representative for). Ask the Owner or that department's "
                   "HR Representative.",
        )
    return detail


def _letter_out(letter: models.ExitLetter) -> schemas.ExitLetterOut:
    return schemas.ExitLetterOut(
        id=letter.id, employee_id=letter.employee_id, exit_request_id=letter.exit_request_id,
        letter_type=letter.letter_type, letter_title=renderer.LETTER_TYPES[letter.letter_type],
        version=letter.version, letter_number=letter.letter_number,
        template_name=letter.template_name, template_version=letter.template_version,
        is_current=letter.is_current, notes=letter.notes, file_size=letter.file_size,
        generated_by_name=letter.generated_by_name, generated_at=letter.generated_at,
    )


def _exit_summary(
    db: Session, company_id: uuid.UUID, detail: dict, user: models.User | None = None
) -> schemas.ExitLetterRequestOut:
    exit_request = detail["exit_request"]
    employee = detail["employee"]
    eligible, reason = crud.exit_letter_eligibility(db, company_id, exit_request)
    eligibility = {}
    for letter_type in crud.EXIT_LETTER_TYPES:
        ok, why = crud.exit_letter_eligibility(db, company_id, exit_request, letter_type)
        eligibility[letter_type] = {"eligible": ok, "reason": why}
    letters = crud.list_exit_letters(db, company_id, exit_request_id=exit_request.id)
    if user is not None and not _payroll(db, user):
        letters = [l for l in letters if l.letter_type != "fnf_statement"]
    return schemas.ExitLetterRequestOut(
        exit_request_id=exit_request.id,
        employee_id=employee.id,
        employee_name=f"{employee.first_name} {employee.last_name or ''}".strip(),
        employee_code=employee.employee_code,
        employee_email=employee.work_email,
        # Exited employees often lose their work mailbox -- emails default to this.
        employee_personal_email=employee.personal_email,
        designation=detail["designation"].name if detail["designation"] else None,
        department=detail["department"].name if detail["department"] else None,
        status=exit_request.status,
        resignation_date=exit_request.resignation_date,
        last_working_day=exit_request.last_working_day,
        eligible=eligible,
        eligibility_reason=reason,
        eligibility=eligibility,
        fnf_status=crud.fnf_status(detail.get("settlement")),
        letters=[_letter_out(l) for l in letters],
    )


# ── templates ──────────────────────────────────────────────────────────────

def _template_out(row: models.ExitLetterHtmlTemplate | None) -> schemas.ExitLetterTemplateOut | None:
    if row is None:
        return None
    out = schemas.ExitLetterTemplateOut.model_validate(row)
    # Inline previews for the HR-only editor (the files are never served by /media).
    if row.seal_image_path:
        out.seal_image_data_url = renderer.embed_letter_image(row.seal_image_path)
    if row.signature_image_path:
        out.signature_image_data_url = renderer.embed_letter_image(row.signature_image_path)
    return out

@router.get("/exit-letter-templates/{letter_type}", response_model=schemas.ExitLetterTemplateOut | None)
def get_exit_letter_template(
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    return _template_out(crud.get_exit_letter_template(db, current_user.company_id, letter_type))


@router.put("/exit-letter-templates/{letter_type}", response_model=schemas.ExitLetterTemplateOut)
def save_exit_letter_template(
    payload: schemas.ExitLetterTemplateUpdate,
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    error = renderer.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    row = crud.upsert_exit_letter_template(
        db, current_user.company_id, letter_type,
        name=payload.name, html_body=payload.html_body, css_styles=payload.css_styles,
        is_active=payload.is_active, signatory_name=payload.signatory_name,
        signatory_designation=payload.signatory_designation, updated_by=current_user.employee_id,
    )
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update",
                          f"{letter_type}_letter_template", row.id, changes={"version": str(row.version)})
    db.commit()
    db.refresh(row)
    return _template_out(row)


# ── multiple saved templates (the singular GET/PUT above keep working as a
# "read/edit the active one" shortcut) ───────────────────────────────────────

@router.get("/exit-letter-templates/{letter_type}/items", response_model=list[schemas.ExitLetterTemplateOut])
def list_exit_letter_templates(
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    return [_template_out(row) for row in crud.list_exit_letter_templates(db, current_user.company_id, letter_type)]


@router.post("/exit-letter-templates/{letter_type}/items", response_model=schemas.ExitLetterTemplateOut, status_code=201)
def create_exit_letter_template(
    payload: schemas.ExitLetterTemplateUpdate,
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    error = renderer.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    row = crud.create_exit_letter_template(
        db, current_user.company_id, letter_type, name=payload.name, html_body=payload.html_body,
        css_styles=payload.css_styles, is_active=payload.is_active, signatory_name=payload.signatory_name,
        signatory_designation=payload.signatory_designation, created_by=current_user.employee_id,
    )
    crud.create_audit_log(db, current_user.company_id, current_user.id, "create",
                          f"{letter_type}_letter_template", row.id)
    db.commit()
    db.refresh(row)
    return _template_out(row)


@router.put("/exit-letter-templates/{letter_type}/items/{template_id}", response_model=schemas.ExitLetterTemplateOut)
def update_exit_letter_template(
    template_id: uuid.UUID,
    payload: schemas.ExitLetterTemplateUpdate,
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    error = renderer.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    try:
        row = crud.update_exit_letter_template_by_id(
            db, current_user.company_id, letter_type, template_id, name=payload.name, html_body=payload.html_body,
            css_styles=payload.css_styles, is_active=payload.is_active, signatory_name=payload.signatory_name,
            signatory_designation=payload.signatory_designation, updated_by=current_user.employee_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update",
                          f"{letter_type}_letter_template", row.id, changes={"version": str(row.version)})
    db.commit()
    db.refresh(row)
    return _template_out(row)


@router.delete("/exit-letter-templates/{letter_type}/items/{template_id}", status_code=204)
def delete_exit_letter_template(
    template_id: uuid.UUID,
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    try:
        crud.delete_exit_letter_template(db, current_user.company_id, letter_type, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "delete",
                          f"{letter_type}_letter_template", template_id)
    db.commit()


@router.post("/exit-letter-templates/{letter_type}/items/{template_id}/activate", response_model=schemas.ExitLetterTemplateOut)
def activate_exit_letter_template(
    template_id: uuid.UUID,
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    try:
        row = crud.activate_exit_letter_template(db, current_user.company_id, letter_type, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update",
                          f"{letter_type}_letter_template", row.id)
    db.commit()
    db.refresh(row)
    return _template_out(row)


# Seal / signature images: PNG or JPG verified by content (no SVG -- it can
# carry script), small enough to embed in every generated PDF.
_IMAGE_KIND = Path(..., pattern="^(seal|signature)$")
_IMAGE_COLUMN = {"seal": "seal_image_path", "signature": "signature_image_path"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
_IMAGE_MAX_BYTES = 1024 * 1024


def _draft_image(data_url: str) -> str:
    """An editor's unsaved seal / signature for Live Preview only: "" = none;
    otherwise must be a PNG/JPG data URL whose bytes really are PNG/JPG."""
    import base64
    import binascii
    if not data_url:
        return renderer.embed_letter_image(None)
    prefix, _, encoded = data_url.partition(",")
    if prefix not in ("data:image/png;base64", "data:image/jpeg;base64"):
        raise HTTPException(status_code=400, detail="Seal and signature images must be PNG or JPG.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="The selected image could not be read.") from exc
    if len(raw) > _IMAGE_MAX_BYTES or not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
        raise HTTPException(status_code=400, detail="Seal and signature images must be PNG or JPG, under 1MB.")
    return data_url


def _saved_template(db: Session, company_id: uuid.UUID, letter_type: str) -> models.ExitLetterHtmlTemplate:
    row = crud.get_exit_letter_template(db, company_id, letter_type)
    if row is None:
        raise HTTPException(status_code=400, detail="Save the letter template first, then add the seal and signature.")
    return row


@router.post("/exit-letter-templates/{letter_type}/images/{kind}", response_model=schemas.ExitLetterTemplateOut)
def upload_exit_letter_image(  # sync def -> threadpool (blocking file/DB I/O)
    file: UploadFile = File(...),
    letter_type: str = _TYPE,
    kind: str = _IMAGE_KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """Company seal or authorized signature printed on every letter generated
    from now on ({{company_seal}} / {{authorized_signature}}). Bumps the
    template version; letters already issued keep their own snapshot."""
    label = "company seal" if kind == "seal" else "signature"
    if Path_(file.filename or "").suffix.lower() not in _IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Only PNG and JPG images are supported for the {label}.")
    head = file.file.read(8)
    if not (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")):
        raise HTTPException(status_code=400, detail="The selected file is not a valid PNG or JPG image.")
    file.file.seek(0)
    row = _saved_template(db, current_user.company_id, letter_type)
    file_url, size_bytes = save_uploaded_file(file, entity_type="exit_letter_asset", entity_id=current_user.company_id)
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
                          f"{letter_type}_letter_{kind}", row.id, changes={"version": str(row.version)})
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


@router.delete("/exit-letter-templates/{letter_type}/images/{kind}", response_model=schemas.ExitLetterTemplateOut)
def delete_exit_letter_image(
    letter_type: str = _TYPE,
    kind: str = _IMAGE_KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    row = _saved_template(db, current_user.company_id, letter_type)
    column = _IMAGE_COLUMN[kind]
    previous = getattr(row, column)
    if previous:
        setattr(row, column, None)
        row.version += 1
        row.updated_by = current_user.employee_id
        row.updated_at = datetime.datetime.now(datetime.timezone.utc)
        crud.create_audit_log(db, current_user.company_id, current_user.id, "update",
                              f"{letter_type}_letter_{kind}", row.id, changes={"version": str(row.version)})
        db.commit()
        delete_uploaded_file(previous)
        db.refresh(row)
    return _template_out(row)


@router.post("/exit-letter-templates/{letter_type}/preview", response_model=schemas.OfferLetterHtmlTemplatePreviewOut)
def preview_exit_letter_template(
    payload: schemas.ExitLetterTemplatePreviewRequest,
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """Live Preview of the in-progress (unsaved) HTML/CSS against a real exit
    request's employee data -- never sample data."""
    _require_type_access(db, current_user, letter_type)
    error = renderer.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    detail = _detail_for_hr(db, current_user, payload.exit_request_id)
    company = db.get(models.Company, current_user.company_id)
    saved = crud.get_exit_letter_template(db, company.id, letter_type)
    placeholders = renderer.build_exit_letter_placeholders(
        detail, company, letter_type,
        signatory_name=payload.signatory_name, signatory_designation=payload.signatory_designation,
        issue_date=crud.company_today(db, company.id), version=1,
        seal_image_path=saved.seal_image_path if saved else None,
        signature_image_path=saved.signature_image_path if saved else None,
    )
    for key, draft in (("company_seal", payload.seal_image_data_url),
                       ("authorized_signature", payload.signature_image_data_url)):
        if draft is not None:
            placeholders[key] = _draft_image(draft)
    return schemas.OfferLetterHtmlTemplatePreviewOut(
        rendered_html=renderer.render_exit_letter_html(payload.html_body, payload.css_styles, placeholders)
    )


# ── offboarding workflow ───────────────────────────────────────────────────

@router.get("/exit-letters/exit-requests", response_model=list[schemas.ExitLetterRequestOut])
def list_exit_letter_requests(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    rows = db.scalars(
        select(models.ExitRequestModel)
        .join(models.Employee, models.Employee.id == models.ExitRequestModel.employee_id)
        .where(models.Employee.company_id == current_user.company_id)
        .order_by(models.ExitRequestModel.resignation_date.desc())
    ).all()
    out = []
    for r in rows:
        detail = crud.get_exit_letter_detail(db, current_user.company_id, r.id)
        if detail is not None and _visible(db, current_user, detail["employee"].id):
            out.append(_exit_summary(db, current_user.company_id, detail, current_user))
    return out


@router.get("/exit-requests/{exit_id}/letters", response_model=schemas.ExitLetterRequestOut)
def get_exit_request_letters(
    exit_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    return _exit_summary(db, current_user.company_id, _detail_for_hr(db, current_user, exit_id), current_user)


def _active_template(db: Session, company_id: uuid.UUID, letter_type: str) -> models.ExitLetterHtmlTemplate:
    template = crud.get_exit_letter_template(db, company_id, letter_type)
    if template is None or not template.is_active:
        raise HTTPException(
            status_code=400,
            detail=f"No active {renderer.LETTER_TYPES[letter_type]} Template is configured yet -- set one up "
            f"under Documents > Templates > {renderer.LETTER_TYPES[letter_type]} Template first.",
        )
    return template


@router.post("/exit-requests/{exit_id}/letters/{letter_type}/preview", response_model=schemas.OfferLetterHtmlTemplatePreviewOut)
def preview_exit_letter(
    exit_id: uuid.UUID,
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    _require_type_access(db, current_user, letter_type)
    detail = _detail_for_hr(db, current_user, exit_id)
    company = db.get(models.Company, current_user.company_id)
    template = _active_template(db, company.id, letter_type)
    previous = [l for l in crud.list_exit_letters(db, company.id, exit_request_id=exit_id) if l.letter_type == letter_type]
    placeholders = renderer.build_exit_letter_placeholders(
        detail, company, letter_type,
        signatory_name=template.signatory_name, signatory_designation=template.signatory_designation,
        issue_date=crud.company_today(db, company.id),
        version=max((l.version for l in previous), default=0) + 1,
        seal_image_path=template.seal_image_path,
        signature_image_path=template.signature_image_path,
    )
    return schemas.OfferLetterHtmlTemplatePreviewOut(
        rendered_html=renderer.render_exit_letter_html(template.html_body, template.css_styles, placeholders)
    )


@router.post("/exit-requests/{exit_id}/letters/{letter_type}", response_model=schemas.ExitLetterOut, status_code=201)
def generate_exit_letter(
    exit_id: uuid.UUID,
    payload: schemas.ExitLetterGenerateRequest,
    letter_type: str = _TYPE,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """Generate (first time) or Regenerate (new version) the letter from the
    current active template and the employee's real data. Only once the
    employee is eligible (approved exit + last working day reached)."""
    _require_type_access(db, current_user, letter_type)
    detail = _detail_for_hr(db, current_user, exit_id)
    company = db.get(models.Company, current_user.company_id)
    eligible, reason = crud.exit_letter_eligibility(db, company.id, detail["exit_request"], letter_type)
    if not eligible:
        raise HTTPException(status_code=409, detail=reason)
    template = _active_template(db, company.id, letter_type)
    try:
        letter = crud.create_exit_letter(
            db, company, detail, letter_type, template,
            generated_by_user=current_user, notes=payload.notes,
        )
    except Exception as exc:  # PDF engine / storage failure -- nothing is saved
        db.rollback()
        logger.exception("Experience & Relieving Letter generation failed for exit %s", exit_id)
        raise HTTPException(
            status_code=503,
            detail="The PDF could not be generated right now. Please try again; if it keeps failing, "
            "ask your administrator to check the server's PDF engine (see README, Backend setup).",
        ) from exc
    crud.create_audit_log(
        db, company.id, current_user.id, "generate", f"{letter_type}_letter", letter.id,
        changes={"employee_id": str(letter.employee_id), "version": str(letter.version),
                 "template_version": str(letter.template_version)},
    )
    emailed_to = _email_letter_to_employee(db, company, detail, letter, current_user) if payload.email_employee else None
    db.commit()
    db.refresh(letter)
    out = _letter_out(letter)
    out.emailed_to = emailed_to
    return out


def _email_letter_to_employee(db: Session, company: models.Company, detail: dict,
                              letter: models.ExitLetter, user: models.User) -> str | None:
    """Queues the generated PDF to the employee (sent after commit); None
    when there is no usable address or the PDF is missing."""
    from .. import email_service
    employee = detail["employee"]
    to = (employee.personal_email or "").strip() or (employee.work_email or "").strip()
    content = crud.exit_letter_file_bytes(letter)
    if not to or not email_service.is_valid_email(to) or content is None:
        return None
    title = renderer.LETTER_TYPES[letter.letter_type]
    kind = "fnf_statement" if letter.letter_type == "fnf_statement" else "experience_relieving_letter"
    file_title = title.replace(" & ", "_and_").replace(" ", "_")
    sender = crud.employee_display_name(db, user.employee_id) if user.employee_id else "HR Team"
    log = email_service.queue_hr_document(
        db, document_kind=kind, to=to,
        attachments=[email_service.make_attachment(f"{file_title}_{employee.employee_code or 'employee'}.pdf", content)],
        recipient_name=f"{employee.first_name} {employee.last_name or ''}".strip(),
        company_id=company.id, company_name=company.name, sender_name=sender if sender != "—" else "HR Team",
        created_by=user.id, document_name=f"{title} ({letter.letter_number})",
        message=f"Please find attached your {title} (Ref. No. {letter.letter_number}).",
        related_entity_type="exit_letter", related_entity_id=letter.id,
        idempotency_key=f"{email_service.EXIT_LETTER}:{letter.id}:{to.lower()}",
    )
    return to if log is not None else None


# ── employee + HR access to generated letters ──────────────────────────────

def _can_read_letter(db: Session, user: models.User, letter: models.ExitLetter) -> bool:
    if letter.company_id != user.company_id:
        return False
    if user.employee_id == letter.employee_id:
        return True
    if letter.letter_type == "fnf_statement" and not _payroll(db, user):
        return False
    return has_column_access(db, user, *HR_DOCUMENT_COLUMNS) and _visible(db, user, letter.employee_id)


@router.get("/employees/{employee_id}/exit-letters", response_model=list[schemas.ExitLetterOut])
def list_employee_exit_letters(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Employees see their own current letters; HR sees every version for
    employees they may see."""
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")  # another company / tenant
    is_self = current_user.employee_id == employee_id
    # HR viewing their OWN exit letters gets the employee view (current only),
    # never the HR history of their own record.
    is_hr = (not is_self and has_column_access(db, current_user, *HR_DOCUMENT_COLUMNS)
             and _visible(db, current_user, employee_id))
    if not (is_self or is_hr):
        raise HTTPException(status_code=403, detail="You don't have permission to view these letters")
    letters = crud.list_exit_letters(db, current_user.company_id, employee_id=employee_id, current_only=not is_hr)
    return [_letter_out(l) for l in letters if _can_read_letter(db, current_user, l)]


def _letter_or_404(db: Session, user: models.User, letter_id: uuid.UUID) -> models.ExitLetter:
    letter = db.get(models.ExitLetter, letter_id)
    if letter is None or not _can_read_letter(db, user, letter):
        raise HTTPException(status_code=404, detail="Letter not found")
    return letter


@router.get("/exit-letters/{letter_id}/pdf")
def download_exit_letter_pdf(
    letter_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The PDF exactly as generated (stored file -- never re-rendered)."""
    letter = _letter_or_404(db, current_user, letter_id)
    content = crud.exit_letter_file_bytes(letter)
    if content is None:
        raise HTTPException(status_code=410, detail="The stored PDF for this letter is missing.")
    employee = db.get(models.Employee, letter.employee_id)
    code = "".join(c if c.isalnum() else "-" for c in (employee.employee_code if employee else "employee"))
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{letter.letter_type}-letter-{code}-v{letter.version}.pdf"'},
    )


@router.get("/exit-letters/{letter_id}/html", response_model=schemas.OfferLetterHtmlTemplatePreviewOut)
def view_exit_letter_html(
    letter_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    letter = _letter_or_404(db, current_user, letter_id)
    return schemas.OfferLetterHtmlTemplatePreviewOut(rendered_html=letter.rendered_html)
