from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

import uuid
from datetime import datetime, timezone

from .. import crud, models, offer_letter_html_renderer, schemas
from ..database import get_db
from ..deps import (
    DEFAULT_LIST_LIMIT,
    get_current_user,
    require_module_enabled,
    require_permission,
    require_view_permission,
)


def _can_view_recruitment(db: Session, user: models.User) -> bool:
    """View/Edit/Admin on Recruitment (Owner always) -- the same rule as
    deps.require_view_permission, for endpoints that otherwise fall back
    to the caller's own rows."""
    role = crud.get_user_primary_role(db, user.id)
    if role is None:
        return False
    if role.name == crud.BUILTIN_ROLES[0]:
        return True
    return crud.effective_user_matrix(db, user).get("recruitment") in ("v", "e", "a")

router = APIRouter(
    prefix="/api", tags=["recruitment"],
    dependencies=[Depends(require_module_enabled("recruitment"))],
)

_TYPE_DISPLAY = {
    "full_time": "Full-time",
    "part_time": "Part-time",
    "contract": "Contract",
    "intern": "Intern",
}
_STATUS_DISPLAY = {"open": "Open", "on_hold": "On Hold", "closed": "Closed"}


def _job_opening_out(opening) -> schemas.JobOpeningOut:
    return schemas.JobOpeningOut(
        title=opening.title,
        dept=opening.department.name if opening.department else "—",
        type=_TYPE_DISPLAY.get(opening.employment_type, opening.employment_type.title()),
        openings=opening.vacancies,
        status=_STATUS_DISPLAY.get(opening.status, opening.status.title()),
        posted=opening.posted_date.isoformat(),
        description=opening.description,
    )


@router.get("/job-openings", response_model=list[schemas.JobOpeningOut])
def list_job_openings(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [
        _job_opening_out(o)
        for o in crud.list_job_openings(db, company.id, limit=limit, offset=offset)
    ]


@router.post(
    "/job-openings",
    response_model=schemas.JobOpeningOut,
    status_code=201,
)
def create_job_opening(
    payload: schemas.JobOpeningCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("recruitment")),
):
    company = db.get(models.Company, current_user.company_id)
    # employment_type from the Add Job Opening dialog is display text
    # ("Full-time"/"Part-time"/"Contract"/"Intern") -- normalize to the
    # lowercase codes this table stores.
    normalized_type = payload.employment_type.strip().lower().replace("-", "_")
    try:
        opening = crud.create_job_opening(
            db,
            company.id,
            payload.title,
            payload.department_name,
            normalized_type,
            payload.openings,
            description=payload.description,
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "job_opening", opening.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(opening)
    return _job_opening_out(opening)


@router.get("/candidate-pipeline", response_model=dict[str, list[schemas.CandidateCardOut]])
def get_candidate_pipeline(
    db: Session = Depends(get_db),
    # Candidate names/contacts/stages are recruitment data (was: any employee).
    current_user: models.User = Depends(require_view_permission("recruitment")),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_candidate_pipeline(db, company.id)


@router.post(
    "/candidates",
    response_model=schemas.CandidateCardOut,
    status_code=201,
)
def create_candidate(
    payload: schemas.CandidateCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("recruitment")),
):
    company = db.get(models.Company, current_user.company_id)
    opening = crud.find_job_opening_by_title(db, company.id, payload.job_opening_title)
    if opening is None:
        raise HTTPException(
            status_code=409,
            detail=f"No job opening titled '{payload.job_opening_title}'",
        )
    candidate = crud.create_candidate(
        db,
        company.id,
        payload.name,
        payload.years_experience,
        email=payload.email,
        phone=payload.phone,
    )
    application = crud.create_job_application(db, opening.id, candidate.id, "applied")
    crud.create_audit_log(db, company.id, current_user.id, "create", "candidate", candidate.id)
    db.commit()
    exp = candidate.years_experience
    return schemas.CandidateCardOut(
        id=application.id,
        name=candidate.name,
        role=opening.title,
        exp=f"{exp:g} yrs" if exp is not None else "—",
    )


@router.patch(
    "/candidates/{application_id}/stage",
    response_model=schemas.CandidateCardOut,
)
def update_candidate_stage(
    application_id: uuid.UUID,
    payload: schemas.CandidateStageUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("recruitment")),
):
    """Legacy Kanban "Move to" -- now goes through the recruitment
    workflow's validated transitions (recruitment_workflow.transition), so
    a board move can never skip required stages, feedback or approvals."""
    from .. import recruitment_workflow as rw

    ctx = rw.make_ctx(db, current_user)
    application = rw.get_application(ctx, application_id, lock=True)
    target = {"screened": "screening"}.get(payload.stage.strip().lower(), payload.stage.strip().lower())
    rw.transition(ctx, application, "move", {"target": target})
    db.commit()
    db.refresh(application)
    exp = application.candidate.years_experience
    return schemas.CandidateCardOut(
        id=application.id,
        name=application.candidate.name,
        role=application.opening.title,
        exp=f"{exp:g} yrs" if exp is not None else "—",
    )


@router.get("/offers", response_model=list[schemas.OfferOut])
def list_offers(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    # Offers carry candidate compensation (was: any employee).
    current_user: models.User = Depends(require_view_permission("recruitment")),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_offers(db, company.id, limit=limit, offset=offset)


@router.post(
    "/offers",
    response_model=schemas.OfferOut,
    status_code=201,
)
def create_offer(
    payload: schemas.OfferCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("recruitment")),
):
    application = crud.find_application_by_candidate_and_opening(
        db, current_user.company_id, payload.candidate_name, payload.job_opening_title
    )
    if application is None:
        raise HTTPException(
            status_code=409,
            detail=f"No application found for '{payload.candidate_name}' / '{payload.job_opening_title}'",
        )
    company = db.get(models.Company, current_user.company_id)
    offer = crud.create_offer(
        db,
        application.id,
        payload.offered_ctc,
        payload.offer_date,
        payload.proposed_joining_date,
        payload.status,
    )
    crud.create_audit_log(db, company.id, current_user.id, "create", "offer", offer.id)
    db.commit()
    db.refresh(offer)
    return schemas.OfferOut(
        id=offer.id,
        candidate=application.candidate.name,
        role=application.opening.title,
        ctc=float(offer.offered_ctc),
        joining=offer.proposed_joining_date.isoformat()
        if offer.proposed_joining_date
        else "—",
        status=crud.normalize_offer_status(offer.status),
    )


@router.patch("/offers/{offer_id}", response_model=schemas.OfferOut)
def update_offer_status(
    offer_id: uuid.UUID,
    payload: schemas.OfferStatusUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("recruitment")),
):
    # Routed through the recruitment workflow (acceptance starts preboarding,
    # decline / withdraw close the application) instead of a bare status write.
    from .. import recruitment_onboarding as onb
    from .. import recruitment_workflow as rw

    ctx = rw.make_ctx(db, current_user)
    offer = onb.get_offer(ctx, offer_id, lock=True)
    status = payload.status.strip().lower()
    if status == "accepted":
        onb.accept_offer(ctx, offer, source="hr")
    elif status in ("rejected", "declined"):
        onb.decline_offer(ctx, offer, source="hr")
    elif status == "withdrawn":
        onb.withdraw_offer(ctx, offer, "Withdrawn from the offers list")
    else:
        raise HTTPException(status_code=422, detail="Use the Recruitment offer actions to change this offer.")
    db.commit()
    db.refresh(offer)
    return schemas.OfferOut(
        id=offer.id,
        candidate=offer.application.candidate.name,
        role=offer.application.opening.title,
        ctc=float(offer.offered_ctc),
        joining=offer.proposed_joining_date.isoformat()
        if offer.proposed_joining_date
        else "—",
        status=crud.normalize_offer_status(offer.status),
    )


_require_recruitment_edit = require_permission("recruitment")


@router.get(
    "/offer-letter-template",
    response_model=schemas.OfferLetterHtmlTemplateOut | None,
)
def get_offer_letter_html_template(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Documents > Templates > Offer Letter Template -- readable by any
    authenticated user (mirrors the Payslip Template's open-read
    convention), null when this company hasn't authored one yet."""
    company = db.get(models.Company, current_user.company_id)
    return crud.get_offer_letter_html_template(db, company.id)


@router.put("/offer-letter-template", response_model=schemas.OfferLetterHtmlTemplateOut)
def save_offer_letter_html_template(
    payload: schemas.OfferLetterHtmlTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_recruitment_edit),
):
    error = offer_letter_html_renderer.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    company = db.get(models.Company, current_user.company_id)
    row = crud.upsert_offer_letter_html_template(
        db,
        company.id,
        payload.name,
        payload.html_body,
        payload.css_styles,
        payload.is_active,
        current_user.employee_id,
    )
    crud.create_audit_log(db, company.id, current_user.id, "update", "offer_letter_html_template", row.id)
    db.commit()
    db.refresh(row)
    return row


# ── multiple saved templates (the singular GET/PUT above keep working as a
# "read/edit the active one" shortcut) ───────────────────────────────────────

@router.get("/offer-letter-templates", response_model=list[schemas.OfferLetterHtmlTemplateOut])
def list_offer_letter_html_templates(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_offer_letter_html_templates(db, company.id)


@router.post("/offer-letter-templates", response_model=schemas.OfferLetterHtmlTemplateOut, status_code=201)
def create_offer_letter_html_template(
    payload: schemas.OfferLetterHtmlTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_recruitment_edit),
):
    error = offer_letter_html_renderer.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    company = db.get(models.Company, current_user.company_id)
    row = crud.create_offer_letter_html_template(
        db, company.id, name=payload.name, html_body=payload.html_body, css_styles=payload.css_styles,
        is_active=payload.is_active, created_by=current_user.employee_id,
    )
    crud.create_audit_log(db, company.id, current_user.id, "create", "offer_letter_html_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.put("/offer-letter-templates/{template_id}", response_model=schemas.OfferLetterHtmlTemplateOut)
def update_offer_letter_html_template(
    template_id: uuid.UUID,
    payload: schemas.OfferLetterHtmlTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_recruitment_edit),
):
    error = offer_letter_html_renderer.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    company = db.get(models.Company, current_user.company_id)
    try:
        row = crud.update_offer_letter_html_template_by_id(
            db, company.id, template_id, name=payload.name, html_body=payload.html_body,
            css_styles=payload.css_styles, is_active=payload.is_active, updated_by=current_user.employee_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "update", "offer_letter_html_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/offer-letter-templates/{template_id}", status_code=204)
def delete_offer_letter_html_template(
    template_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_recruitment_edit),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        crud.delete_offer_letter_html_template(db, company.id, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "delete", "offer_letter_html_template", template_id)
    db.commit()


@router.post("/offer-letter-templates/{template_id}/activate", response_model=schemas.OfferLetterHtmlTemplateOut)
def activate_offer_letter_html_template(
    template_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_recruitment_edit),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        row = crud.activate_offer_letter_html_template(db, company.id, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "update", "offer_letter_html_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.post(
    "/offer-letter-template/preview",
    response_model=schemas.OfferLetterHtmlTemplatePreviewOut,
)
def preview_offer_letter_html_template(
    payload: schemas.OfferLetterHtmlTemplatePreviewRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_recruitment_edit),
):
    """Live Preview -- renders the in-progress (not-yet-saved) HTML/CSS
    against a real, selected offer's actual candidate/role/CTC data. Never
    fabricates sample data."""
    company = db.get(models.Company, current_user.company_id)
    error = offer_letter_html_renderer.validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    detail = crud.get_offer_detail(db, payload.offer_id, company.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Offer not found")
    placeholders = offer_letter_html_renderer.build_offer_letter_placeholders(detail, company)
    rendered = offer_letter_html_renderer.render_offer_letter_html(
        payload.html_body, payload.css_styles, placeholders
    )
    return schemas.OfferLetterHtmlTemplatePreviewOut(rendered_html=rendered)


def offer_letter_pdf(db: Session, company: models.Company, detail: dict) -> tuple[bytes, str]:
    """(PDF bytes, filename-safe candidate name) for an offer, from the
    company's active Offer Letter Template -- the same document Download
    produces (also emailed -- see routers/email.py). 400 without one."""
    template = crud.get_offer_letter_html_template(db, company.id)
    if template is None or not template.is_active:
        raise HTTPException(
            status_code=400,
            detail="No active Offer Letter Template is configured for your company yet -- "
            "set one up under Documents > Templates > Offer Letter Template first.",
        )
    placeholders = offer_letter_html_renderer.build_offer_letter_placeholders(detail, company)
    rendered = offer_letter_html_renderer.render_offer_letter_html(
        template.html_body, template.css_styles, placeholders
    )
    pdf_bytes = offer_letter_html_renderer.render_html_to_pdf(rendered)
    candidate_name = detail["candidate"].name if detail["candidate"] else "offer"
    safe_name = "".join(c if c.isalnum() else "-" for c in candidate_name).strip("-") or "offer"
    return pdf_bytes, safe_name


@router.get("/offers/{offer_id}/pdf")
def download_offer_letter_pdf(
    offer_id: uuid.UUID,
    db: Session = Depends(get_db),
    # SEC-02: an offer letter carries a candidate's compensation -- same
    # Recruitment Edit/Admin gate as creating/editing offers, checked before
    # any rendering (was: any authenticated employee).
    current_user: models.User = Depends(require_permission("recruitment")),
):
    company = db.get(models.Company, current_user.company_id)
    detail = crud.get_offer_detail(db, offer_id, company.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Offer not found")
    pdf_bytes, safe_name = offer_letter_pdf(db, company, detail)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="offer-letter-{safe_name}.pdf"'},
    )


_RESULT_STATUS = {"pass": "Accepted", "fail": "Rejected", None: "Scheduled"}


@router.get("/interviews", response_model=list[schemas.InterviewOut])
def list_interviews(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [
        schemas.InterviewOut(
            id=i.id,
            candidate=i.application.candidate.name,
            role=i.application.opening.title,
            interviewer=f"{i.interviewer.first_name} {i.interviewer.last_name}".strip()
            if i.interviewer and i.interviewer.last_name
            else (i.interviewer.first_name if i.interviewer else "Unassigned"),
            when=i.scheduled_at.strftime("%Y-%m-%d · %I:%M %p") if i.scheduled_at else "Awaiting interviewers",
            round=f"Round {i.round_no}",
            status=_RESULT_STATUS.get(i.result, "Scheduled"),
            feedback=i.feedback,
        )
        for i in crud.list_interviews(
            db, company.id, limit=limit, offset=offset,
            # Without Recruitment access an employee sees only the interviews
            # they are the interviewer for.
            interviewer_id=None if _can_view_recruitment(db, current_user) else (
                current_user.employee_id or uuid.uuid4()
            ),
        )
    ]


@router.post(
    "/interviews",
    response_model=schemas.InterviewOut,
    status_code=201,
)
def create_interview(
    payload: schemas.InterviewCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("recruitment")),
):
    application = crud.find_application_by_candidate_and_opening(
        db, current_user.company_id, payload.candidate_name, payload.job_opening_title
    )
    if application is None:
        raise HTTPException(
            status_code=409,
            detail=f"No application found for '{payload.candidate_name}' / '{payload.job_opening_title}'",
        )
    company = db.get(models.Company, current_user.company_id)
    interview = crud.create_interview(db, application.id, payload.round_no, payload.scheduled_at)
    if payload.interviewer_employee_id is not None:
        interviewer = db.get(models.Employee, payload.interviewer_employee_id)
        if interviewer is None or interviewer.company_id != current_user.company_id:
            raise HTTPException(status_code=404, detail="Interviewer not found")
        interview.interviewer_id = payload.interviewer_employee_id
    crud.create_audit_log(db, company.id, current_user.id, "create", "interview", interview.id)
    db.commit()
    db.refresh(interview)
    interviewer_name = "Unassigned" if interview.interviewer is None else crud._full_name(interview.interviewer)
    return schemas.InterviewOut(
        id=interview.id,
        candidate=application.candidate.name,
        role=application.opening.title,
        interviewer=interviewer_name,
        when=interview.scheduled_at.strftime("%Y-%m-%d · %I:%M %p"),
        round=f"Round {interview.round_no}",
        status="Scheduled",
        feedback=None,
    )


@router.patch("/interviews/{interview_id}", response_model=schemas.InterviewOut)
def update_interview_outcome(
    interview_id: uuid.UUID,
    payload: schemas.InterviewOutcomeUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("recruitment")),
):
    """Records the outcome of a completed interview -- backs the
    Interviews tab's "Record Outcome" action."""
    company = db.get(models.Company, current_user.company_id)
    interview = crud.record_interview_outcome(
        db, interview_id, current_user.company_id, payload.result, payload.feedback
    )
    if interview is None:
        raise HTTPException(status_code=404, detail="Interview not found")
    crud.create_audit_log(db, company.id, current_user.id, "update", "interview", interview.id)
    db.commit()
    db.refresh(interview)
    interviewer_name = (
        "Unassigned" if interview.interviewer is None else crud._full_name(interview.interviewer)
    )
    return schemas.InterviewOut(
        id=interview.id,
        candidate=interview.application.candidate.name,
        role=interview.application.opening.title,
        interviewer=interviewer_name,
        when=interview.scheduled_at.strftime("%Y-%m-%d · %I:%M %p"),
        round=f"Round {interview.round_no}",
        status=_RESULT_STATUS.get(interview.result, "Scheduled"),
        feedback=interview.feedback,
    )


def _hiring_requisition_out(db: Session, requisition) -> schemas.HiringRequisitionOut:
    return schemas.HiringRequisitionOut(
        id=requisition.id,
        requested_by=requisition.requested_by,
        designation_title=requisition.designation_title,
        department_name=requisition.department.name if requisition.department else "—",
        positions_count=requisition.positions_count,
        justification=requisition.justification,
        status=requisition.status,
        approver_id=requisition.approver_id,
        approver_name=crud.employee_display_name(db, requisition.approver_id)
        if requisition.approver_id
        else "—",
        decision_notes=requisition.decision_notes,
        decided_at=requisition.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, requisition.requested_by), requisition.approver_id
        ),
    )


@router.get("/hiring-requisitions", response_model=list[schemas.HiringRequisitionOut])
def list_hiring_requisitions(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [
        _hiring_requisition_out(db, r)
        for r in crud.list_hiring_requisitions(
            db, company.id, limit=limit, offset=offset,
            # Without Recruitment access a manager sees only the requisitions
            # they raised.
            requested_by=None if _can_view_recruitment(db, current_user) else (
                current_user.employee_id or uuid.uuid4()
            ),
        )
    ]


@router.post(
    "/hiring-requisitions",
    response_model=schemas.HiringRequisitionOut,
    status_code=201,
)
def create_hiring_requisition(
    payload: schemas.HiringRequisitionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("recruitment")),
):
    company = db.get(models.Company, current_user.company_id)
    if not crud.can_submit_self_service_request(
        db, current_user, payload.requested_by, "recruitment", "hiring_requisition", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    try:
        requisition = crud.create_hiring_requisition(
            db,
            company.id,
            payload.requested_by,
            payload.designation_title,
            payload.department_name,
            payload.positions_count,
            payload.justification,
        )
        crud.create_audit_log(
            db, company.id, current_user.id, "create", "hiring_requisition", requisition.id
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(requisition)
    requester = db.get(models.Employee, requisition.requested_by)
    if requester is not None:
        crud.notify_new_request(
            db, company.id, requester, "Hiring Requisition", "hiring_requisition", requisition.id
        )
        db.commit()
    return _hiring_requisition_out(db, requisition)


@router.patch(
    "/hiring-requisitions/{requisition_id}",
    response_model=schemas.HiringRequisitionOut,
)
def update_hiring_requisition(
    requisition_id: uuid.UUID,
    payload: schemas.HiringRequisitionUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py -- authorization is solely
    # crud.can_decide_request_configurable below, not a blanket
    # require_permission("recruitment") gate, so only someone in the
    # requester's actual (possibly custom, multi-step) approval chain can
    # decide this specific requisition.
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    requisition = crud.get_hiring_requisition_for_update(db, requisition_id, current_user.company_id)
    if requisition is None:
        raise HTTPException(status_code=404, detail="Hiring requisition not found")
    if requisition.status in ("approved", "rejected"):
        decider = crud.employee_display_name(db, requisition.approver_id)
        decider_type = crud.decided_by_manager_type(
            db.get(models.Employee, requisition.requested_by), requisition.approver_id
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"This request was already {requisition.status} by {decider}"
                f" ({decider_type}) and cannot be decided again"
                if decider_type
                else f"This request was already {requisition.status} and cannot be decided again"
            ),
        )
    # Approvals inbox decisions run through the same recruitment workflow as
    # the Recruitment screen: approval chain, stage history, notifications
    # and -- once approved -- the linked draft Job Opening.
    from .. import recruitment_workflow as rw

    ctx = rw.make_ctx(db, current_user)
    rw.decide_requisition(ctx, requisition, payload.status, payload.decision_notes)
    db.commit()
    db.refresh(requisition)
    return _hiring_requisition_out(db, requisition)
