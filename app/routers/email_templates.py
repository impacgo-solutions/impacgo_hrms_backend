"""Documents > Templates > Email Templates: the company's own HTML+CSS
design for each transactional email content template (see
email_service.EMAIL_KINDS). Same multi-template/one-active shape as the
Payslip / Offer Letter / Exit Letter template routers -- several named
templates per email_kind, exactly one active, used automatically by
email_service.render_email() the moment it's active (no code change, no
redeploy).

Every kind always has a built-in professional default
(app/email_templates/<kind>.html, branded with the sending company's name),
used whenever the company has no active template of its own -- so every
tenant, including a brand-new one, sends polished emails with zero setup.
GET .../default returns it so a company can start customizing from it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Path

from .. import crud, models, schemas
from .. import email_service as es
from .. import template_rendering as tr
from ..database import get_db
from ..deps import get_current_user, require_hr_documents
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api", tags=["email-templates"])

_KIND = Path(..., pattern="^(" + "|".join(es.EMAIL_KINDS) + ")$")

_CANDIDATE_TOKENS = ["heading", "candidate_name", "opening_title", "stage_name", "message_html",
                     "details_block_html", "cta_html", "sender_name"]
_LEAVE_TOKENS = ["employee_name", "employee_id", "leave_type", "start_date", "end_date", "number_of_days"]

# The insertable-token chips the editor shows per email_kind, read directly
# off each kind's context dict in email_service.py / recruitment_onboarding.py
# / recruitment_workflow.py. company_name is available in every kind (the
# sending company, filled in by render_email).
EMAIL_KIND_TOKENS: dict[str, list[str]] = {k: v + ["company_name"] for k, v in {
    "request_notification": ["heading", "intro_line", "details_html", "hrms_url", "cta_label"],
    "birthday": ["heading", "employee_name", "designation", "department", "details_html", "body_html",
                 "hrms_url", "cta_label"],
    "work_anniversary": ["heading", "employee_name", "years_of_service", "designation", "department",
                         "details_html", "body_html", "hrms_url", "cta_label"],
    "holiday_reminder": ["heading", "holiday_name", "holiday_date", "holiday_type", "branch_name",
                         "details_html", "body_html", "hrms_url", "cta_label"],
    "promotion_announcement": ["heading", "employee_name", "new_designation", "previous_designation",
                               "details_html", "body_html", "hrms_url", "cta_label"],
    "leave_applied": _LEAVE_TOKENS + ["reason", "status", "hrms_url", "action_buttons_html"],
    "leave_approved": _LEAVE_TOKENS + ["approved_by", "status", "hrms_url", "notes_html"],
    "leave_rejected": _LEAVE_TOKENS + ["rejected_by", "rejection_reason", "status", "hrms_url"],
    "leave_withdrawal": ["heading", "intro_line"] + _LEAVE_TOKENS + ["reason", "status", "decided_by", "restored",
                                                                     "hrms_url", "status_color", "notes_html"],
    "candidate_interview_scheduled": _CANDIDATE_TOKENS,
    "candidate_interview_rescheduled": _CANDIDATE_TOKENS,
    "candidate_interview_cancelled": _CANDIDATE_TOKENS,
    "candidate_next_step": _CANDIDATE_TOKENS + ["next_stage_name"],
    "candidate_rejected": _CANDIDATE_TOKENS,
    "candidate_message": _CANDIDATE_TOKENS,
    "hr_document": ["document_name", "recipient_name", "message_html", "attachment_names", "sender_name"],
    "test_email": ["sender", "requested_by", "sent_at"],
}.items()}

_LINK = "https://app.impacgo.com"


def _rows(pairs: dict[str, str]) -> str:
    return "".join(es._detail_row(k, v) for k, v in pairs.items())


def _candidate_block(pairs: dict[str, str]) -> str:
    """Same interview-details block recruitment_onboarding.email_candidate builds."""
    return ('<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-top:1px solid #e2e8f0;'
            'border-bottom:1px solid #e2e8f0;padding:8px 0;margin:6px 0 18px;">' + _rows(pairs) + "</table>")


_INTERVIEW = {"Candidate": "Ravi Kumar", "Position": "Backend Engineer", "Stage": "Technical Round",
              "When": "Mon, 12 Oct 2026, 11:00 AM", "Mode": "Video", "Link": "https://meet.example.com/abc-defg"}


def _candidate(heading: str, message: str, **extra) -> dict[str, str]:
    return {"heading": heading, "candidate_name": "Ravi Kumar", "opening_title": "Backend Engineer",
            "stage_name": "Technical Round", "message_html": es._paragraphs_html(message), "details_block_html": "",
            "cta_html": "", "sender_name": "HR Team", **extra}


# Fabricated sample values for Preview -- these are notifications with no
# single real entity to substitute (unlike a payslip), so sample data is the
# right call here. company_name is the previewing company's real name.
_SAMPLE_CONTEXT: dict[str, dict[str, str]] = {
    "request_notification": {
        "heading": "Leave request awaiting your approval",
        "intro_line": "Asha Rao has submitted a leave request that needs your decision.",
        "details_html": _rows({"Employee": "Asha Rao (EMP-0042)", "Request": "Earned Leave, 2 days",
                               "Dates": "12 Oct 2026 - 13 Oct 2026"}),
        "hrms_url": _LINK, "cta_label": "Review Now",
    },
    "birthday": {
        "heading": "🎉 Happy Birthday, Asha Rao!", "employee_name": "Asha Rao",
        "designation": "Software Engineer", "department": "Engineering",
        "details_html": _rows({"Employee": "Asha Rao (EMP-0042)", "Designation": "Software Engineer",
                               "Department": "Engineering", "Occasion": "Birthday 🎂", "Date": "12 Oct 2026"}),
        "body_html": es._paragraphs_html("Today is Asha Rao's birthday 🎂 Wishing them a wonderful year ahead filled "
                                         "with happiness, health and success. Everyone, let's make their day special "
                                         "with your warm wishes!"),
        "hrms_url": _LINK, "cta_label": "Send Your Wishes",
    },
    "work_anniversary": {
        "heading": "🎉 Happy 3rd Work Anniversary, Asha Rao!", "employee_name": "Asha Rao",
        "years_of_service": "3 years", "designation": "Software Engineer", "department": "Engineering",
        "details_html": _rows({"Employee": "Asha Rao (EMP-0042)", "Designation": "Software Engineer",
                               "Department": "Engineering", "Years with us": "3 years",
                               "Occasion": "Work Anniversary 🎊", "Date": "12 Oct 2026"}),
        "body_html": es._paragraphs_html("Today Asha Rao completes 3 years with us 🎊 Thank you for your dedication, "
                                         "hard work and everything you bring to the team. Everyone, let's congratulate "
                                         "them on this milestone!"),
        "hrms_url": _LINK, "cta_label": "Send Your Wishes",
    },
    "holiday_reminder": {
        "heading": "Holiday Tomorrow: Diwali", "holiday_name": "Diwali", "holiday_date": "Tuesday, 20 Oct 2026",
        "holiday_type": "Company holiday", "branch_name": "",
        "details_html": _rows({"Holiday": "Diwali", "Date": "20 Oct 2026", "Type": "Company holiday"}),
        "body_html": es._paragraphs_html("Tomorrow is a company holiday for Diwali. Wishing you and your family a "
                                         "joyful celebration!"),
        "hrms_url": _LINK, "cta_label": "View Holiday Calendar",
    },
    "promotion_announcement": {
        "heading": "Congratulations, Asha Rao!", "employee_name": "Asha Rao",
        "new_designation": "Senior Software Engineer", "previous_designation": "Software Engineer",
        "details_html": _rows({"New Designation": "Senior Software Engineer",
                               "Previous Designation": "Software Engineer", "Announced By": "HR Team"}),
        "body_html": es._paragraphs_html("We're delighted to announce that Asha Rao has been promoted to Senior "
                                         "Software Engineer. Please join us in congratulating them on this "
                                         "well-deserved achievement!"),
        "hrms_url": _LINK, "cta_label": "View Profile",
    },
    "leave_applied": {
        "employee_name": "Asha Rao", "employee_id": "EMP-0042", "leave_type": "Earned Leave",
        "start_date": "12 Oct 2026", "end_date": "13 Oct 2026", "number_of_days": "2",
        "reason": "Family function", "status": "Pending", "hrms_url": _LINK,
    },
    "leave_approved": {
        "employee_name": "Asha Rao", "employee_id": "EMP-0042", "leave_type": "Earned Leave",
        "start_date": "12 Oct 2026", "end_date": "13 Oct 2026", "number_of_days": "2",
        "approved_by": "Priya Sharma", "status": "Approved", "hrms_url": _LINK,
        "notes_html": es._paragraphs_html("Comments: Enjoy your time off!"),
    },
    "leave_rejected": {
        "employee_name": "Asha Rao", "employee_id": "EMP-0042", "leave_type": "Earned Leave",
        "start_date": "12 Oct 2026", "end_date": "13 Oct 2026", "number_of_days": "2",
        "rejected_by": "Priya Sharma", "rejection_reason": "Critical release that week", "status": "Rejected",
        "hrms_url": _LINK,
    },
    "leave_withdrawal": {
        "heading": "Your Leave Withdrawal Has Been Approved",
        "intro_line": "Hi Asha Rao, your leave withdrawal was approved by Priya Sharma. The leave is withdrawn and "
                      "the days are back in your balance.",
        "employee_name": "Asha Rao", "employee_id": "EMP-0042", "leave_type": "Earned Leave",
        "start_date": "12 Oct 2026", "end_date": "13 Oct 2026", "number_of_days": "2",
        "reason": "Plans changed", "status": "Approved", "decided_by": "Priya Sharma", "restored": "2 days",
        "hrms_url": _LINK, "status_color": "#16a34a", "notes_html": "",
    },
    "candidate_interview_scheduled": _candidate(
        "Your interview is scheduled",
        "Your Technical Round for Backend Engineer is scheduled. Please find the details below -- we look forward "
        "to speaking with you.", details_block_html=_candidate_block(_INTERVIEW)),
    "candidate_interview_rescheduled": _candidate(
        "Your interview has been rescheduled",
        "Your Technical Round for Backend Engineer has a new time. Please note the updated details below.",
        details_block_html=_candidate_block({**_INTERVIEW, "When": "Wed, 14 Oct 2026, 03:00 PM"})),
    "candidate_interview_cancelled": _candidate(
        "Your interview has been cancelled",
        "We're sorry -- your Technical Round for Backend Engineer has been cancelled. We apologise for the "
        "inconvenience and will be in touch with you shortly about the next steps.",
        details_block_html=_candidate_block({k: _INTERVIEW[k] for k in ("Position", "Stage", "When")})),
    "candidate_next_step": _candidate(
        "Good news -- you're moving forward!",
        "Thank you for attending the Technical Round for Backend Engineer. We're pleased to let you know you have "
        "progressed to the next stage: HR Round. We'll share the schedule with you shortly.",
        next_stage_name="HR Round"),
    "candidate_rejected": _candidate(
        "Thank you for your interest",
        "Thank you for the time you invested in applying for Backend Engineer. After careful consideration we will "
        "not be moving forward with your application at this time. We will keep your profile on file for future "
        "opportunities.", stage_name=""),
    "candidate_message": _candidate(
        "We need a little more information",
        "Could you please share your updated resume and notice period? Simply reply with the details.",
        stage_name=""),
    "hr_document": {
        "document_name": "Payslip - September 2026", "recipient_name": "Asha Rao",
        "message_html": es._paragraphs_html("Please find your payslip for September 2026 attached."),
        "attachment_names": "Payslip_Sep_2026.pdf", "sender_name": "HR Team",
    },
    "test_email": {"sender": "info@impacgo.com", "requested_by": "HR Team", "sent_at": "12 Oct 2026, 10:00 AM UTC"},
}


@router.get("/email-templates/kinds")
def list_email_kinds(current_user: models.User = Depends(get_current_user)):
    """The catalog the frontend's kind cards and chip picker are built
    from -- one entry per email_kind with its insertable tokens. L-04:
    authenticated callers only (it was reachable without a token)."""
    return [{"email_kind": k, "tokens": EMAIL_KIND_TOKENS[k]} for k in es.EMAIL_KINDS]


@router.get("/email-templates/{email_kind}/default")
def get_default_email_template(
    email_kind: str = _KIND,
    current_user: models.User = Depends(require_hr_documents),
):
    """The built-in design used whenever the company has no active template
    of its own for this kind -- the starting point for customizing it."""
    return {"email_kind": email_kind, "name": "Default template", "html_body": es._template(email_kind),
            "css_styles": ""}


@router.get("/email-templates/{email_kind}/items", response_model=list[schemas.EmailHtmlTemplateOut])
def list_email_templates(
    email_kind: str = _KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    return crud.list_email_templates(db, current_user.company_id, email_kind)


@router.post("/email-templates/{email_kind}/items", response_model=schemas.EmailHtmlTemplateOut, status_code=201)
def create_email_template(
    payload: schemas.EmailHtmlTemplateUpdate,
    email_kind: str = _KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    error = tr.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    row = crud.create_email_template(
        db, current_user.company_id, email_kind, name=payload.name, html_body=payload.html_body,
        css_styles=payload.css_styles, is_active=payload.is_active, created_by=current_user.employee_id,
    )
    crud.create_audit_log(db, current_user.company_id, current_user.id, "create", f"{email_kind}_email_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.put("/email-templates/{email_kind}/items/{template_id}", response_model=schemas.EmailHtmlTemplateOut)
def update_email_template(
    template_id: uuid.UUID,
    payload: schemas.EmailHtmlTemplateUpdate,
    email_kind: str = _KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    error = tr.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    try:
        row = crud.update_email_template_by_id(
            db, current_user.company_id, template_id, name=payload.name, html_body=payload.html_body,
            css_styles=payload.css_styles, is_active=payload.is_active, updated_by=current_user.employee_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", f"{email_kind}_email_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/email-templates/{email_kind}/items/{template_id}", status_code=204)
def delete_email_template(
    template_id: uuid.UUID,
    email_kind: str = _KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    try:
        crud.delete_email_template(db, current_user.company_id, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "delete", f"{email_kind}_email_template", template_id)
    db.commit()


@router.post("/email-templates/{email_kind}/items/{template_id}/activate", response_model=schemas.EmailHtmlTemplateOut)
def activate_email_template(
    template_id: uuid.UUID,
    email_kind: str = _KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    try:
        row = crud.activate_email_template(db, current_user.company_id, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", f"{email_kind}_email_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.post("/email-templates/{email_kind}/items/{template_id}/deactivate",
             response_model=schemas.EmailHtmlTemplateOut)
def deactivate_email_template(
    template_id: uuid.UUID,
    email_kind: str = _KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """Switches back to the built-in default design for this kind (the
    custom template stays saved, inactive)."""
    row = crud.get_email_template_by_id(db, current_user.company_id, template_id)
    if row is None or row.email_kind != email_kind:
        raise HTTPException(status_code=404, detail="Template not found.")
    row.is_active = False
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", f"{email_kind}_email_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.post("/email-templates/{email_kind}/preview", response_model=schemas.EmailHtmlTemplatePreviewOut)
def preview_email_template(
    payload: schemas.EmailHtmlTemplatePreviewRequest,
    email_kind: str = _KIND,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_hr_documents),
):
    """Renders the in-progress (not-yet-saved) HTML/CSS against sample data
    -- these are notifications with no single real entity like a payslip to
    substitute -- branded with the previewing company's real name, exactly
    as a real email of this kind would be."""
    error = tr.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    company_name = es._company_name(db, current_user.company_id) or "Your Company"
    sample = {"company_name": company_name, **_SAMPLE_CONTEXT[email_kind]}
    verbatim = tuple(k for k in sample if k.endswith("_html"))
    content = tr.substitute_placeholders(payload.html_body, sample, verbatim_keys=verbatim)
    content = es._PLACEHOLDER_RE.sub("", content)
    if payload.css_styles:
        content = f"<style>{tr.sanitize_template_css(payload.css_styles)}</style>{content}"
    rendered = es.render_template("layout", {
        "content_html": content, "preheader": "", "brand_color": "#2564cf", "brand_name": company_name,
        "footer_note": f"This is an automated message from {company_name} via Impacgo HRMS. "
                       "Please do not reply to this email.",
        "company_logo_html": es._company_logo_html(db, current_user.company_id),
    })
    return schemas.EmailHtmlTemplatePreviewOut(rendered_html=rendered)
