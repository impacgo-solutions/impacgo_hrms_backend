"""HRMS email endpoints (Microsoft Graph -- see app/email_service.py).

Admin (System Settings / RBAC Edit/Admin, or Owner):
    GET  /api/admin/email/status   configuration state (variable NAMES only)
    GET  /api/admin/email/diagnostics  token + Mail.Send permission check (sends nothing)
    POST /api/admin/email/test     send the Graph test email
    GET  /api/admin/email/logs     delivery log

HR (Owner, or Edit/Admin on Recruitment / Payroll (Process) / System
Settings -- never a self-service role):
    POST /api/hr/documents/send-email          offer / appointment / experience /
                                               relieving letter, payslip, other
    POST /api/payroll/payslips/{slip_id}/email payslip PDF -> the employee
    POST /api/offers/{offer_id}/email          offer letter PDF -> the candidate

Attachments come only from records the caller may see in their own
company (an employee document, a payslip, an offer) or from a file uploaded
in the same request -- never from a path supplied by the client. All sends
are rate-limited per user (middleware/rate_limit.py) and logged.
"""

from __future__ import annotations

import calendar
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud, database, email_service, models
from ..config import settings
from ..database import get_db
from ..deps import HR_DOCUMENT_COLUMNS, get_current_user, has_column_access, require_permission
from ..storage import uploads_root, winlong_path
from .payroll import payslip_pdf_bytes
from .recruitment import offer_letter_pdf

router = APIRouter(prefix="/api", tags=["email"])

_can = has_column_access


def require_hr_sender(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> models.User:
    if not has_column_access(db, user, *HR_DOCUMENT_COLUMNS):
        raise HTTPException(status_code=403, detail="Only HR / admin users can send HR documents.")
    return user


def _sender_name(db: Session, user: models.User) -> str:
    if user.employee_id:
        name = crud.employee_display_name(db, user.employee_id)
        if name and name != "—":
            return name
    return "HR Team"


def _result(result: email_service.SendResult) -> dict:
    if result.status == "FAILED":
        raise HTTPException(
            status_code=502,
            detail={"message": result.error_message or "Sending failed.", "code": result.error_code,
                    "log_id": str(result.log_id) if result.log_id else None},
        )
    if result.status == "SKIPPED":
        raise HTTPException(
            status_code=503,
            detail={"message": "Email is not configured for this organisation (Admin > Email Settings).",
                    "code": "not_configured", "log_id": str(result.log_id) if result.log_id else None},
        )
    return {"status": result.status, "log_id": str(result.log_id) if result.log_id else None,
            "provider_status": result.provider_status}


def _email_error(exc: email_service.EmailError) -> HTTPException:
    return HTTPException(status_code=413 if exc.code == "attachment_too_large" else 422,
                         detail={"message": str(exc), "code": exc.code})


# ── admin ──────────────────────────────────────────────────────────────────

@router.get("/admin/email/status")
def email_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
) -> dict:
    return email_service.status(database.get_session_tenant_slug(db))


@router.get("/admin/email/diagnostics")
def email_diagnostics(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
) -> dict:
    """Token + Mail.Send check for THIS tenant's Microsoft Graph configuration (sends nothing)."""
    from .. import graph_mail, tenant_email
    from ..tenant_email.providers.microsoft_graph import graph_config
    cfg = tenant_email.load_config(database.get_session_tenant_slug(db))
    if cfg is None or cfg.provider != "microsoft_graph":
        return {"configured": False, "error": "This organisation does not use Microsoft Graph email."}
    return graph_mail.diagnose(graph_config(cfg))


class TestEmailIn(BaseModel):
    recipient: str = Field(max_length=254)


@router.post("/admin/email/test")
def send_test_email(
    payload: TestEmailIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
) -> dict:
    try:
        result = email_service.send_test_email(
            db, to=payload.recipient.strip(), requested_by=_sender_name(db, current_user),
            company_id=current_user.company_id, created_by=current_user.id,
        )
    except email_service.EmailError as exc:
        raise _email_error(exc) from exc
    return _result(result)


@router.get("/admin/email/logs")
def list_email_logs(
    limit: int = Query(default=100, ge=1, le=500),
    status: str | None = Query(default=None, pattern="^(QUEUED|SENDING|SENT|FAILED|SKIPPED)$"),
    email_type: str | None = Query(default=None, max_length=40),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
) -> list[dict]:
    q = select(models.EmailLog).where(models.EmailLog.company_id == current_user.company_id)
    if status:
        q = q.where(models.EmailLog.status == status)
    if email_type:
        q = q.where(models.EmailLog.email_type == email_type)
    try:
        rows = db.scalars(q.order_by(models.EmailLog.created_at.desc()).limit(limit)).all()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="The email log is not available yet (run backend/db/add_email_logs.sql).")
    return [
        {
            "id": str(r.id), "email_type": r.email_type, "sender": r.sender, "recipient": r.recipient,
            "cc": r.cc, "subject": r.subject, "attachments": r.attachment_names,
            "related_entity_type": r.related_entity_type,
            "related_entity_id": str(r.related_entity_id) if r.related_entity_id else None,
            "status": r.status, "transport": r.transport, "provider_status": r.provider_status,
            "error_code": r.error_code, "error_message": r.error_message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        }
        for r in rows
    ]


# ── attachments from real records ──────────────────────────────────────────

def _employee_document_attachment(db: Session, company_id: uuid.UUID, record_id: uuid.UUID) -> tuple[email_service.Attachment, models.Employee]:
    record = db.get(models.DocumentRecord, record_id)
    employee = db.get(models.Employee, record.employee_id) if record else None
    if record is None or employee is None or employee.company_id != company_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if not record.file_url or not record.file_url.startswith("/media/"):
        raise HTTPException(status_code=422, detail="This document has no stored file to attach.")
    root = uploads_root().resolve()
    path = (root / record.file_url.removeprefix("/media/")).resolve()
    if root not in path.parents:  # never outside the uploads store
        raise HTTPException(status_code=404, detail="Document not found")
    long_path = winlong_path(path)
    if not long_path.is_file():
        raise HTTPException(status_code=422, detail="The document's file is missing from storage.")
    if long_path.stat().st_size > settings.email_max_attachment_bytes:
        raise _email_error(email_service.EmailError(
            f"This document is larger than the {settings.email_max_attachment_bytes // (1024 * 1024)} MB email limit.",
            code="attachment_too_large",
        ))
    label = "".join(c if c.isalnum() or c in " -_" else "_" for c in (record.document_type or "Document")).strip() or "Document"
    try:
        attachment = email_service.make_attachment(f"{label}{path.suffix.lower()}", long_path.read_bytes())
    except email_service.EmailError as exc:
        raise _email_error(exc) from exc
    return attachment, employee


def _payslip_attachment(db: Session, company: models.Company, slip_id: uuid.UUID) -> tuple[email_service.Attachment, models.Employee, str]:
    detail = crud.get_salary_slip_detail(db, slip_id)
    if detail is None or detail["employee"] is None or detail["employee"].company_id != company.id:
        raise HTTPException(status_code=404, detail="Payslip not found")
    run = detail["run"]
    period = f"{calendar.month_name[run.period_month]} {run.period_year}" if run else "Payslip"
    file_period = f"{run.period_year}-{run.period_month:02d}" if run else "slip"
    pdf = email_service.make_attachment(f"Payslip_{file_period}.pdf", payslip_pdf_bytes(db, company, detail))
    return pdf, detail["employee"], period


async def _upload_attachment(file: UploadFile) -> email_service.Attachment:
    limit = settings.email_max_attachment_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise _email_error(email_service.EmailError(
            f"The file is larger than the {limit // (1024 * 1024)} MB email limit.", code="attachment_too_large"))
    try:
        return email_service.make_attachment(Path(file.filename or "document.pdf").name, content)
    except email_service.EmailError as exc:
        raise _email_error(exc) from exc


# ── HR documents ───────────────────────────────────────────────────────────

@router.post("/hr/documents/send-email")
async def send_hr_document(
    document_kind: str = Form(..., pattern="^(offer_letter|appointment_letter|experience_letter|relieving_letter|experience_relieving_letter|fnf_statement|payslip|other)$"),
    recipient: str = Form(..., max_length=254),
    cc: str | None = Form(default=None, max_length=1000),
    bcc: str | None = Form(default=None, max_length=1000),
    subject: str | None = Form(default=None, max_length=200),
    message: str | None = Form(default=None, max_length=5000),
    document_name: str | None = Form(default=None, max_length=100),
    employee_id: uuid.UUID | None = Form(default=None),
    document_record_id: uuid.UUID | None = Form(default=None),
    payslip_id: uuid.UUID | None = Form(default=None),
    # A generated Experience / Relieving Letter (its stored, immutable PDF).
    exit_letter_id: uuid.UUID | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_sender),
) -> dict:
    """HR selects an employee document / payslip, or uploads a PDF, and
    emails it from the HR mailbox as a real attachment."""
    company = db.get(models.Company, current_user.company_id)
    attachments: list[email_service.Attachment] = []
    employee: models.Employee | None = None
    related: tuple[str | None, uuid.UUID | None] = (None, None)
    if document_record_id:
        att, employee = _employee_document_attachment(db, company.id, document_record_id)
        attachments.append(att)
        related = ("employee_document", document_record_id)
    if payslip_id:
        if not _can(db, current_user, "payroll_process"):
            raise HTTPException(status_code=403, detail="Sending payslips requires Payroll (Process) access.")
        att, slip_employee, _period = _payslip_attachment(db, company, payslip_id)
        attachments.append(att)
        employee = employee or slip_employee
        related = ("payslip", payslip_id)
    if exit_letter_id:
        letter = db.get(models.ExitLetter, exit_letter_id)
        visible = crud.get_visible_employee_ids_for_docs(db, current_user)
        if letter is None or letter.company_id != company.id or (visible is not None and letter.employee_id not in visible):
            raise HTTPException(status_code=404, detail="Letter not found")
        if letter.letter_type == "fnf_statement" and not _can(db, current_user, "payroll_process"):
            raise HTTPException(status_code=403, detail="Sending F&F statements requires Payroll (Process) access.")
        content = crud.exit_letter_file_bytes(letter)
        if content is None:
            raise HTTPException(status_code=410, detail="The stored PDF for this letter is missing.")
        letter_employee = db.get(models.Employee, letter.employee_id)
        code = letter_employee.employee_code if letter_employee else "employee"
        title = (
            "Full_and_Final_Settlement_Statement" if letter.letter_type == "fnf_statement"
            else "Experience_and_Relieving_Letter"
        )
        attachments.append(email_service.make_attachment(f"{title}_{code}.pdf", content))
        employee = employee or letter_employee
        related = ("exit_letter", letter.id)
    if file is not None and file.filename:
        attachments.append(await _upload_attachment(file))
    if not attachments:
        raise HTTPException(status_code=422, detail="Attach a file, or choose an employee document or payslip.")
    if employee is None and employee_id:
        employee = db.get(models.Employee, employee_id)
        if employee is None or employee.company_id != company.id:
            raise HTTPException(status_code=404, detail="Employee not found")
    if employee is not None and related[0] is None:
        related = ("employee", employee.id)
    recipient_name = f"{employee.first_name} {employee.last_name or ''}".strip() if employee else None
    try:
        result = email_service.send_hr_document(
            db, document_kind=document_kind, to=recipient, cc=cc, bcc=bcc, subject=subject,
            message=message, document_name=document_name, attachments=attachments,
            recipient_name=recipient_name, company_id=company.id,
            company_name=company.name, sender_name=_sender_name(db, current_user),
            created_by=current_user.id, related_entity_type=related[0], related_entity_id=related[1],
        )
    except email_service.EmailError as exc:
        raise _email_error(exc) from exc
    crud.create_audit_log(
        db, company.id, current_user.id, "email", "hr_document", related[1],
        changes={"kind": document_kind, "recipient": recipient, "status": result.status},
    )
    db.commit()
    return _result(result)


class RecordEmailIn(BaseModel):
    recipient: str | None = Field(default=None, max_length=254)
    cc: str | None = Field(default=None, max_length=1000)
    message: str | None = Field(default=None, max_length=5000)


@router.post("/payroll/payslips/{slip_id}/email")
def email_payslip(
    slip_id: uuid.UUID,
    payload: RecordEmailIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("payroll_process")),
) -> dict:
    """Emails the payslip PDF to the employee's work email (or [recipient])."""
    company = db.get(models.Company, current_user.company_id)
    pdf, employee, period = _payslip_attachment(db, company, slip_id)
    to = (payload.recipient or employee.work_email or "").strip()
    if not to:
        raise HTTPException(status_code=422, detail="This employee has no work email on file -- enter a recipient.")
    try:
        result = email_service.send_payslip(
            db, to=to, cc=payload.cc, message=payload.message, pdf=pdf,
            employee_name=f"{employee.first_name} {employee.last_name or ''}".strip(), period=period,
            company_id=company.id, company_name=company.name, sender_name=_sender_name(db, current_user),
            created_by=current_user.id, related_entity_type="payslip", related_entity_id=slip_id,
        )
    except email_service.EmailError as exc:
        raise _email_error(exc) from exc
    crud.create_audit_log(db, company.id, current_user.id, "email", "payslip", slip_id,
                          changes={"recipient": to, "status": result.status})
    db.commit()
    return _result(result)


def _payslip_pdf_factory(tenant_slug: str | None, company_id: uuid.UUID, slip_id: uuid.UUID):
    """Builds one payslip PDF in the email worker thread (own DB session)."""
    def build() -> list[email_service.Attachment]:
        db = crud.open_tenant_session(tenant_slug) if tenant_slug else database.SessionLocal()
        try:
            company = db.get(models.Company, company_id)
            pdf, _employee, _period = _payslip_attachment(db, company, slip_id)
            return [pdf]
        finally:
            db.close()
    return build


# Lets the email outbox rebuild a payslip PDF when it retries a queued
# payslip email after a restart (the factory above lives only in memory).
email_service.register_attachment_recipe(
    "payslip",
    lambda tenant_slug, r: _payslip_pdf_factory(tenant_slug, uuid.UUID(r["company_id"]), uuid.UUID(r["slip_id"]))(),
)


class RunPayslipsEmailIn(BaseModel):
    # Also email employees whose payslip for this run was already emailed.
    resend: bool = False
    message: str | None = Field(default=None, max_length=5000)


@router.post("/payroll/runs/{run_id}/email-payslips")
def email_run_payslips(
    run_id: uuid.UUID,
    payload: RunPayslipsEmailIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("payroll_process")),
) -> dict:
    """Payroll > Payslips > Email Payslips: every employee on the run gets
    their own payslip PDF at their work email. Answers at once: recipients
    are resolved here (bulk queries); queueing runs as a background task and
    each PDF is rendered by the email worker just before it is sent -- the
    outcome of every email is in the email log. Employees already emailed
    this run's payslip are skipped unless [resend]; employees without a
    work email are reported, never guessed."""
    company = db.get(models.Company, current_user.company_id)
    run = db.get(models.PayrollRun, run_id)
    if run is None or run.company_id != company.id:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    slips = db.scalars(select(models.SalarySlip).where(models.SalarySlip.payroll_run_id == run.id)).all()
    if not slips:
        raise HTTPException(status_code=409, detail="This payroll run has no payslips yet -- run payslips first.")
    already = set() if payload.resend else set(db.scalars(
        select(models.EmailLog.related_entity_id).where(
            models.EmailLog.company_id == company.id,
            models.EmailLog.email_type == email_service.PAYSLIP,
            models.EmailLog.related_entity_type == "payslip",
            models.EmailLog.related_entity_id.in_([s.id for s in slips]),
            models.EmailLog.status.in_(("QUEUED", "SENDING", "SENT")),
        )
    ).all())
    employees = {e.id: e for e in db.scalars(
        select(models.Employee).where(models.Employee.id.in_({s.employee_id for s in slips}))
    ).all()}
    targets, skipped, no_email, not_allowed = [], [], [], []
    for slip in slips:
        employee = employees.get(slip.employee_id)
        name = f"{employee.first_name} {employee.last_name or ''}".strip() if employee else "—"
        to = (employee.work_email or "").strip() if employee else ""
        if slip.id in already:
            skipped.append(name)
        elif not to or not email_service.is_valid_email(to):
            no_email.append(name)
        elif not email_service.is_allowed_recipient(to):
            not_allowed.append(name)
        else:
            targets.append((slip.id, to, name))
    crud.create_audit_log(db, company.id, current_user.id, "email", "payroll_run", run.id,
                          changes={"queued": str(len(targets)), "skipped": str(len(skipped))})
    db.commit()
    if targets:
        background.add_task(
            _queue_run_payslips, database.get_session_tenant_slug(db), company.id, run.id, targets,
            payload.message, _sender_name(db, current_user), current_user.id,
            # A deliberate resend gets its own key (never swallowed as a duplicate).
            f":resend:{uuid.uuid4().hex[:8]}" if payload.resend else "",
        )
    return {"queued": len(targets), "skipped_already_sent": skipped, "no_email": no_email,
            "domain_not_allowed": not_allowed, "failed": []}


def _queue_run_payslips(tenant_slug, company_id, run_id, targets, message, sender, created_by, key_suffix="") -> None:
    """Background: one queued email per payslip (PDF rendered at send time)."""
    db = crud.open_tenant_session(tenant_slug) if tenant_slug else database.SessionLocal()
    try:
        company = db.get(models.Company, company_id)
        run = db.get(models.PayrollRun, run_id)
        period = f"{calendar.month_name[run.period_month]} {run.period_year}"
        file_name = f"Payslip_{run.period_year}-{run.period_month:02d}.pdf"
        for slip_id, to, name in targets:
            try:
                email_service.queue_hr_document(
                    db, document_kind="payslip", to=to, recipient_name=name,
                    attachments_factory=_payslip_pdf_factory(tenant_slug, company_id, slip_id),
                    attachment_recipe={"kind": "payslip", "company_id": str(company_id), "slip_id": str(slip_id)},
                    attachment_names=file_name, document_name=f"Payslip - {period}", message=message,
                    company_id=company_id, company_name=company.name, sender_name=sender,
                    created_by=created_by, related_entity_type="payslip", related_entity_id=slip_id,
                    idempotency_key=f"{email_service.PAYSLIP}:{slip_id}:{to.lower()}{key_suffix}",
                )
            except Exception:
                logging.getLogger(__name__).exception("Could not queue payslip email for slip %s", slip_id)
        db.commit()
    except Exception:
        db.rollback()
        logging.getLogger(__name__).exception("Bulk payslip email failed for run %s", run_id)
    finally:
        db.close()


@router.post("/offers/{offer_id}/email")
def email_offer_letter(
    offer_id: uuid.UUID,
    payload: RecordEmailIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("recruitment")),
) -> dict:
    """Emails the offer letter PDF (company Offer Letter Template) to the candidate."""
    company = db.get(models.Company, current_user.company_id)
    detail = crud.get_offer_detail(db, offer_id, company.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Offer not found")
    candidate = detail["candidate"]
    to = (payload.recipient or (candidate.email if candidate else "") or "").strip()
    if not to:
        raise HTTPException(status_code=422, detail="This candidate has no email on file -- enter a recipient.")
    pdf_bytes, safe_name = offer_letter_pdf(db, company, detail)
    try:
        result = email_service.send_offer_letter(
            db, to=to, cc=payload.cc, message=payload.message,
            pdf=email_service.make_attachment(f"Offer_Letter_{safe_name}.pdf", pdf_bytes),
            candidate_name=candidate.name if candidate else None,
            company_id=company.id, company_name=company.name, sender_name=_sender_name(db, current_user),
            created_by=current_user.id, related_entity_type="offer", related_entity_id=offer_id,
        )
    except email_service.EmailError as exc:
        raise _email_error(exc) from exc
    crud.create_audit_log(db, company.id, current_user.id, "email", "offer_letter", offer_id,
                          changes={"recipient": to, "status": result.status})
    db.commit()
    return _result(result)
