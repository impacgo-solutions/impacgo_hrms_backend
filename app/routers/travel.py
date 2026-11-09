import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud, expense_claims, form_config, models, schemas, travel_expense as te
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled
from ..storage import delete_uploaded_file, save_uploaded_file

router = APIRouter(
    prefix="/api", tags=["travel"],
    dependencies=[Depends(require_module_enabled("travel"))],
)

_TRAVEL_ENTITY = "travel_request"
_EXPENSE_ENTITY = "expense_report"


# ───────────────────────────── shared helpers ──────────────────────────────


def _project_names(db: Session, company_id: uuid.UUID) -> dict[uuid.UUID, str]:
    return {p.id: p.name for p in db.scalars(select(models.Project).where(models.Project.company_id == company_id))}


def _can_see(db: Session, user: models.User, employee_id: uuid.UUID, doctype: str) -> bool:
    if user.employee_id == employee_id:
        return True
    visible = crud.get_visible_employee_ids_for_requests(db, user, doctype)
    return visible is None or employee_id in visible


def _require_owner(user: models.User, employee_id: uuid.UUID, what: str) -> None:
    if user.employee_id != employee_id:
        raise HTTPException(status_code=403, detail=f"You can only change your own {what}.")


def _require_editable(status: str, what: str) -> None:
    if status not in te.EDITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Only a draft or sent-back {what} can be changed (this one is {status.replace('_', ' ')}).",
        )


def _can_set_payment_fields(db: Session, user: models.User, employee_id: uuid.UUID | None = None) -> bool:
    """Finance / an administrator records payments: the company-wide
    fallback approvers (Owner / RBAC admins / Approvals admins), Travel &
    Expense Approval at Admin, or Payroll (Process) Edit/Admin -- never an
    ordinary approving manager (Edit), and never on their own claim."""
    return te.can_record_payment(db, user, employee_id)


def _attachment_out(a: models.Attachment) -> schemas.AttachmentOut:
    return schemas.AttachmentOut(
        id=a.id, file_name=a.file_name, file_url=a.file_url, mime_type=a.mime_type, size_bytes=a.size_bytes
    )


# ─────────────────────────────── travel requests ────────────────────────────


def _load_travel(db: Session, travel_request_id: uuid.UUID, user: models.User, lock: bool = False) -> models.TravelRequestModel:
    row = (crud.get_travel_request_for_update(db, travel_request_id) if lock
           else db.get(models.TravelRequestModel, travel_request_id))
    employee = db.get(models.Employee, row.employee_id) if row else None
    if row is None or employee is None or employee.company_id != user.company_id:
        raise HTTPException(status_code=404, detail="Travel request not found")
    if not _can_see(db, user, row.employee_id, _TRAVEL_ENTITY):
        raise HTTPException(status_code=404, detail="Travel request not found")
    return row


@router.get("/travel-requests", response_model=list[schemas.TravelRequestOut])
def list_travel_requests(
    status: str | None = None,
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_requests(db, current_user, "travel_request")
    rows = crud.list_travel_requests(
        db, company.id, status=status, employee_ids=visible_ids, limit=limit, offset=offset,
    )
    id_pool = {r.employee_id for r in rows} | {r.approver_id for r in rows if r.approver_id}
    names = crud.employee_display_names_bulk(db, id_pool)
    codes = crud.employee_codes_bulk(db, {r.employee_id for r in rows})
    projects = _project_names(db, company.id)
    payroll = crud.reimbursement_payroll_statuses(db, company.id, "travel_request", rows)
    custom = form_config.load_custom_values(db, company.id, "travel_request", [r.id for r in rows])
    out = [te.travel_request_out(db, r, names=names, codes=codes, viewer=current_user, projects=projects,
                                 custom_values=custom) for r in rows]
    for item in out:
        item.payroll_status = payroll.get(item.id)
    return out


@router.get("/travel-requests/{travel_request_id}", response_model=schemas.TravelRequestOut)
def get_travel_request(
    travel_request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _load_travel(db, travel_request_id, current_user)
    out = te.travel_request_out(db, row, viewer=current_user)
    out.payroll_status = crud.reimbursement_payroll_statuses(db, row_company(db, row), "travel_request", [row]).get(row.id)
    return out


def row_company(db: Session, row: models.TravelRequestModel) -> uuid.UUID:
    return db.get(models.Employee, row.employee_id).company_id


def _submit_travel(db: Session, current_user: models.User, row: models.TravelRequestModel, employee: models.Employee, resubmit: bool) -> None:
    """draft / sent-back -> pending, then tell the approvers. Required custom
    fields must be filled in before a request can be submitted."""
    te.require_custom_complete(db, employee.company_id, "travel_request", row.id)
    clash = te.find_travel_overlap(db, row.employee_id, row.from_date, row.to_date, exclude_id=row.id)
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A {clash.status.replace('_', ' ')} travel request to {clash.destination or 'another destination'} "
                f"({clash.from_date.isoformat()} to {clash.to_date.isoformat()}) already overlaps these dates."
            ),
        )
    row.status = "pending"
    row.submitted_at = datetime.now(timezone.utc)
    if resubmit:
        row.decided_at, row.decision_notes, row.approver_id = None, None, None


def _notify_step_passed(db: Session, company_id: uuid.UUID, employee_id: uuid.UUID, label: str,
                        entity_type: str, entity_id: uuid.UUID, actor_employee_id: uuid.UUID | None) -> None:
    """A configured multi-step workflow: this approval moved the request on
    (crud.decide_configurable_request already told the next approver) --
    tell the employee where their request stands."""
    ref = crud.request_reference(db, entity_type, entity_id)
    what = f"{label}{f' {ref}' if ref else ''}"
    actor = crud.employee_display_name(db, actor_employee_id)
    crud.notify_requester_update(
        db, company_id, employee_id, request_type_label=label, event_key=f"{entity_type}_progress",
        title=f"{what} approved by {actor}, now with the next approver",
        body=f"Your {what[0].lower() + what[1:]} was approved by {actor} and has moved to the next approver.",
        entity_type=entity_type, entity_id=entity_id, actor_employee_id=actor_employee_id,
    )
    db.commit()


@router.post("/travel-requests", status_code=201)
def create_travel_request(
    payload: schemas.TravelRequestApiCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs New Travel Request — self-service, same reasoning as Add Work
    Entry (see routers/work.py). With save_as_draft the request is stored
    but not submitted: nobody is notified until it is submitted."""
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "travel_expense_approval", "travel_request", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    if employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    # L-20: one trip at a time -- an open (pending / sent back) or approved
    # request of the same employee overlapping these dates is a conflict.
    # Serialized per employee so a double-click can't create both.
    expense_claims.lock_employee(db, "travel_request", company.id, payload.employee_id)
    row = models.TravelRequestModel(
        id=uuid.uuid4(), employee_id=payload.employee_id, status="draft", from_date=payload.from_date or datetime.now().date(),
        to_date=payload.to_date or datetime.now().date(),
    )
    te.apply_travel_form(db, row, payload, company.id)
    db.add(row)
    db.flush()
    if not payload.save_as_draft:
        # WF-03: a requester with no Reporting Manager (e.g. the CEO) is no
        # longer refused -- crud.notify_new_request routes the request to the
        # fallback approvers (Owner / System Settings RBAC holders) who can
        # decide it; self-approval stays blocked.
        _submit_travel(db, current_user, row, employee, resubmit=False)
    crud.create_audit_log(db, company.id, current_user.id, "save_draft" if payload.save_as_draft else "create", _TRAVEL_ENTITY, row.id)
    db.commit()
    if row.status == "pending":
        crud.notify_new_request(db, company.id, employee, "Travel Request", "travel_request", row.id)
        db.commit()
    return {"id": str(row.id), "status": row.status}


@router.patch("/travel-requests/{travel_request_id}/edit", response_model=schemas.TravelRequestOut)
def edit_travel_request(
    travel_request_id: uuid.UUID,
    payload: schemas.TravelRequestEdit,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The employee changes their own draft / sent-back request (the whole
    form is sent; itinerary and costing rows are replaced). Unless saved as a
    draft, it is submitted -- a sent-back request goes back to its approvers."""
    row = _load_travel(db, travel_request_id, current_user, lock=True)
    _require_owner(current_user, row.employee_id, "travel request")
    _require_editable(row.status, "travel request")
    employee = db.get(models.Employee, row.employee_id)
    was_sent_back = row.status == "sent_back"
    te.apply_travel_form(db, row, payload, employee.company_id)
    if not payload.save_as_draft:
        _submit_travel(db, current_user, row, employee, resubmit=was_sent_back)
    crud.create_audit_log(
        db, employee.company_id, current_user.id,
        "save_draft" if payload.save_as_draft else ("resubmit" if was_sent_back else "submit"), _TRAVEL_ENTITY, row.id)
    db.commit()
    db.refresh(row)
    if row.status == "pending":
        crud.notify_new_request(db, employee.company_id, employee, "Travel Request", "travel_request", row.id)
        db.commit()
    return te.travel_request_out(db, row, viewer=current_user)


@router.post("/travel-requests/{travel_request_id}/submit", response_model=schemas.TravelRequestOut)
def submit_travel_request(
    travel_request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _load_travel(db, travel_request_id, current_user, lock=True)
    _require_owner(current_user, row.employee_id, "travel request")
    _require_editable(row.status, "travel request")
    if not row.itinerary and not row.destination:
        raise HTTPException(status_code=400, detail="Add at least one travel itinerary row before submitting.")
    employee = db.get(models.Employee, row.employee_id)
    was_sent_back = row.status == "sent_back"
    _submit_travel(db, current_user, row, employee, resubmit=was_sent_back)
    crud.create_audit_log(db, employee.company_id, current_user.id, "resubmit" if was_sent_back else "submit", _TRAVEL_ENTITY, row.id)
    db.commit()
    db.refresh(row)
    crud.notify_new_request(db, employee.company_id, employee, "Travel Request", "travel_request", row.id)
    db.commit()
    return te.travel_request_out(db, row, viewer=current_user)


@router.delete("/travel-requests/{travel_request_id}", status_code=204)
def delete_travel_request(
    travel_request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _load_travel(db, travel_request_id, current_user, lock=True)
    _require_owner(current_user, row.employee_id, "travel request")
    _require_editable(row.status, "travel request")
    employee = db.get(models.Employee, row.employee_id)
    for a in crud.list_attachments_for_entity(db, _TRAVEL_ENTITY, row.id):
        delete_uploaded_file(a.file_url)
        crud.delete_attachment(db, a)
    crud.create_audit_log(db, employee.company_id, current_user.id, "delete", _TRAVEL_ENTITY, row.id)
    form_config.delete_custom_values_for(db, employee.company_id, row.id)
    db.delete(row)
    db.commit()


@router.patch(
    "/travel-requests/{travel_request_id}",
    response_model=schemas.TravelRequestOut,
)
def update_travel_request(
    travel_request_id: uuid.UUID,
    payload: schemas.TravelRequestUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py.
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    request = crud.get_travel_request_for_update(db, travel_request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Travel request not found")
    if request.status == "draft":
        raise HTTPException(status_code=409, detail="This request has not been submitted yet.")
    # Two Reporting Managers: same atomic-with-lock guard as
    # update_work_entry/update_leave_request -- blocks a conflicting second
    # decision from the employee's other manager. Checked before the
    # permission gate so that manager sees a clear "already decided"
    # message instead of a confusing permission error once
    # can_decide_request_configurable (correctly) stops recognizing them
    # as the current decider.
    if request.status in ("approved", "rejected"):
        decider = crud.employee_display_name(db, request.approver_id)
        decider_type = crud.decided_by_manager_type(
            db.get(models.Employee, request.employee_id), request.approver_id
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"This request was already {request.status} by {decider}"
                f" ({decider_type}) and cannot be decided again"
                if decider_type
                else f"This request was already {request.status} and cannot be decided again"
            ),
        )
    if not crud.can_decide_request_configurable(
        db, current_user, request.employee_id, "travel_request", request.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's Reporting Manager or a manager above them in the chain can act on this request",
        )
    effective_status = crud.decide_configurable_request(
        db, current_user, request.employee_id, "travel_request", request.id,
        payload.status, payload.decision_notes,
    )
    request.status = effective_status
    request.approver_id = current_user.employee_id
    request.decision_notes = payload.decision_notes
    if effective_status != "pending":
        request.decided_at = datetime.now(timezone.utc)
    crud.create_audit_log(
        db, company.id, current_user.id, payload.status, "travel_request", request.id,
        changes={"notes": payload.decision_notes} if payload.decision_notes else None,
    )
    db.commit()
    db.refresh(request)
    if effective_status != "pending":
        crud.notify_decision(
            db, company.id, request.employee_id, "Travel Request",
            effective_status, payload.decision_notes, "travel_request", request.id,
        )
        db.commit()
    elif payload.status == "approved":
        _notify_step_passed(db, company.id, request.employee_id, "Travel Request", "travel_request",
                            request.id, current_user.employee_id)
    return te.travel_request_out(db, request, viewer=current_user)


@router.post("/travel-requests/{travel_request_id}/attachments", response_model=schemas.AttachmentOut, status_code=201)
def upload_travel_attachment(  # sync def -> threadpool (blocking file/DB I/O)
    travel_request_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The Copy of Invitation / Announcement. Owner only, while the request
    can still be amended (draft, pending, sent back)."""
    row = _load_travel(db, travel_request_id, current_user)
    _require_owner(current_user, row.employee_id, "travel request")
    if row.status not in ("draft", "pending", "sent_back"):
        raise HTTPException(status_code=409, detail="Documents can no longer be added to a decided request.")
    company_id = row_company(db, row)
    file_url, size_bytes = save_uploaded_file(file, entity_type=_TRAVEL_ENTITY, entity_id=row.id)
    a = crud.create_attachment(
        db, company_id=company_id, entity_type=_TRAVEL_ENTITY, entity_id=row.id, file_name=file.filename or "file",
        file_url=file_url, mime_type=file.content_type, size_bytes=size_bytes,
    )
    a.uploaded_by = current_user.id
    crud.create_audit_log(db, company_id, current_user.id, "attach", _TRAVEL_ENTITY, row.id)
    db.commit()
    db.refresh(a)
    return _attachment_out(a)


@router.get("/travel-requests/{travel_request_id}/attachments", response_model=list[schemas.AttachmentOut])
def list_travel_attachments(
    travel_request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _load_travel(db, travel_request_id, current_user)
    return [_attachment_out(a) for a in crud.list_attachments_for_entity(db, _TRAVEL_ENTITY, row.id)]


@router.delete("/travel-requests/{travel_request_id}/attachments/{attachment_id}", status_code=204)
def delete_travel_attachment(
    travel_request_id: uuid.UUID,
    attachment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _load_travel(db, travel_request_id, current_user)
    _require_owner(current_user, row.employee_id, "travel request")
    _require_editable(row.status, "travel request")
    a = crud.get_attachment_for_entity(db, _TRAVEL_ENTITY, row.id, attachment_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    delete_uploaded_file(a.file_url)
    crud.delete_attachment(db, a)
    db.commit()


@router.get("/travel-requests/{travel_request_id}/history")
def travel_request_history(
    travel_request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _load_travel(db, travel_request_id, current_user)
    return te.history_for(db, row_company(db, row), _TRAVEL_ENTITY, row.id)


# ─────────────────────────────── expense claims ─────────────────────────────


def _load_claim(db: Session, claim_id: uuid.UUID, user: models.User, lock: bool = False) -> models.ExpenseClaim:
    row = crud.get_expense_claim_for_update(db, claim_id) if lock else db.get(models.ExpenseClaim, claim_id)
    if row is None or row.company_id != user.company_id or not _can_see(db, user, row.employee_id, "expense_claim"):
        raise HTTPException(status_code=404, detail="Expense report not found")
    return row


@router.get("/expense-reports", response_model=list[schemas.ExpenseReportOut])
def list_expense_reports(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_requests(db, current_user, "expense_claim")
    # Drafts are private to their owner: approvers / admins see a claim once
    # it has been submitted.
    rows = crud.list_expense_claims(
        db, company.id, employee_ids=visible_ids, limit=limit, offset=offset,
        drafts_of=current_user.employee_id,
    )
    id_pool = {c.employee_id for c in rows} | {c.approver_id for c in rows if c.approver_id} \
        | {c.expense_approver_id for c in rows if c.expense_approver_id}
    names = crud.employee_display_names_bulk(db, id_pool)
    codes = crud.employee_codes_bulk(db, {c.employee_id for c in rows})
    payroll = crud.reimbursement_payroll_statuses(db, company.id, "expense_claim", rows)
    custom = form_config.load_custom_values(db, company.id, "expense_claim", [c.id for c in rows])
    out = [te.expense_report_out(db, claim, names=names, codes=codes, viewer=current_user, custom_values=custom)
           for claim in rows]
    for item in out:
        item.payroll_status = payroll.get(item.id)
    return out


@router.get("/expense-reports/{claim_id}", response_model=schemas.ExpenseReportOut)
def get_expense_report(
    claim_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    claim = _load_claim(db, claim_id, current_user)
    out = te.expense_report_out(db, claim, viewer=current_user)
    out.payroll_status = crud.reimbursement_payroll_statuses(db, claim.company_id, "expense_claim", [claim]).get(claim.id)
    return out


def _submit_claim(db: Session, claim: models.ExpenseClaim, resubmit: bool) -> None:
    te.require_custom_complete(db, claim.company_id, "expense_claim", claim.id)
    claim.status = "pending"
    claim.submitted_at = datetime.now(timezone.utc)
    if resubmit:
        claim.decided_at, claim.decision_notes, claim.approver_id = None, None, None


def _notify_claim(db: Session, employee: models.Employee, claim: models.ExpenseClaim) -> None:
    crud.notify_new_request(
        db, claim.company_id, employee, "Expense Report", "expense_report", claim.id,
        extra_approver_ids=[claim.expense_approver_id] if claim.expense_approver_id else None,
    )
    db.commit()


@router.post("/expense-reports", status_code=201)
def create_expense_report(
    payload: schemas.ExpenseReportApiCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs New Expense Claim — self-service. Reuses hcm.expense_claims, the
    same table Payroll > Reimbursements writes to (see
    crud.create_reimbursement), with real expense rows, taxes & charges and
    advances. With save_as_draft nothing is submitted or notified."""
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "travel_expense_approval", "expense_report", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    if employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    can_pay = _can_set_payment_fields(db, current_user, payload.employee_id)
    if (payload.is_paid or payload.clearance_date) and not can_pay:
        raise HTTPException(status_code=403, detail="Only Finance / an administrator can mark an expense report paid or set its clearance date (never on their own).")

    expense_claims.lock_employee(db, "expense_report", company.id, payload.employee_id)
    posting = payload.posting_date or payload.submitted_date
    reference = payload.reference or f"Expense claim {posting.strftime('%d %b %Y')}"
    if not payload.save_as_draft:
        existing = expense_claims.recent_duplicate(
            db, company.id, payload.employee_id, te.SERIES_PREFIX, payload.submitted_date, reference, te.quick_net_total(payload))
        if existing is not None:
            # M-21: a double-click / retry of the report just filed -- same
            # report back, no second row, audit entry or approver notification.
            db.rollback()
            return {"id": str(existing.id), "claim_no": existing.claim_no, "duplicate": True}
    te.check_claim_links(db, company.id, employee, payload)
    totals = te.compute_claim(db, company.id, employee, payload)
    claim = models.ExpenseClaim(
        id=uuid.uuid4(), company_id=company.id, created_at=expense_claims.now(),
        claim_no=te.next_claim_series(db, company.id, posting), employee_id=employee.id,
        claim_date=payload.submitted_date, total_amount=0, status="draft",
    )
    db.add(claim)
    db.flush()
    te.apply_claim(db, claim, payload, totals, employee, can_pay)
    if not payload.save_as_draft:
        _submit_claim(db, claim, resubmit=False)
    crud.create_audit_log(db, company.id, current_user.id, "save_draft" if payload.save_as_draft else "create", _EXPENSE_ENTITY, claim.id)
    db.commit()
    if claim.status == "pending":
        _notify_claim(db, employee, claim)
    return {"id": str(claim.id), "claim_no": claim.claim_no, "status": claim.status}


@router.patch("/expense-reports/{claim_id}/edit", response_model=schemas.ExpenseReportOut)
def edit_expense_report(
    claim_id: uuid.UUID,
    payload: schemas.ExpenseReportEdit,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    claim = _load_claim(db, claim_id, current_user, lock=True)
    _require_owner(current_user, claim.employee_id, "expense report")
    _require_editable(claim.status, "expense report")
    employee = db.get(models.Employee, claim.employee_id)
    can_pay = _can_set_payment_fields(db, current_user, claim.employee_id)
    if (payload.is_paid or payload.clearance_date) and not can_pay:
        raise HTTPException(status_code=403, detail="Only Finance / an administrator can mark an expense report paid or set its clearance date (never on their own).")
    was_sent_back = claim.status == "sent_back"
    te.check_claim_links(db, claim.company_id, employee, payload)
    totals = te.compute_claim(db, claim.company_id, employee, payload, claim_id=claim.id)
    te.apply_claim(db, claim, payload, totals, employee, can_pay, editing=True)
    if not payload.save_as_draft:
        _submit_claim(db, claim, resubmit=was_sent_back)
    crud.create_audit_log(
        db, claim.company_id, current_user.id,
        "save_draft" if payload.save_as_draft else ("resubmit" if was_sent_back else "submit"), _EXPENSE_ENTITY, claim.id)
    db.commit()
    db.refresh(claim)
    if claim.status == "pending":
        _notify_claim(db, employee, claim)
    return te.expense_report_out(db, claim, viewer=current_user)


@router.post("/expense-reports/{claim_id}/submit", response_model=schemas.ExpenseReportOut)
def submit_expense_report(
    claim_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    claim = _load_claim(db, claim_id, current_user, lock=True)
    _require_owner(current_user, claim.employee_id, "expense report")
    _require_editable(claim.status, "expense report")
    if not claim.lines:
        raise HTTPException(status_code=400, detail="Add at least one expense before submitting.")
    employee = db.get(models.Employee, claim.employee_id)
    was_sent_back = claim.status == "sent_back"
    _submit_claim(db, claim, resubmit=was_sent_back)
    crud.create_audit_log(db, claim.company_id, current_user.id, "resubmit" if was_sent_back else "submit", _EXPENSE_ENTITY, claim.id)
    db.commit()
    db.refresh(claim)
    _notify_claim(db, employee, claim)
    return te.expense_report_out(db, claim, viewer=current_user)


@router.delete("/expense-reports/{claim_id}", status_code=204)
def delete_expense_report(
    claim_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    claim = _load_claim(db, claim_id, current_user, lock=True)
    _require_owner(current_user, claim.employee_id, "expense report")
    _require_editable(claim.status, "expense report")
    for a in crud.list_attachments_for_entity(db, _EXPENSE_ENTITY, claim.id):
        delete_uploaded_file(a.file_url)
        crud.delete_attachment(db, a)
    crud.create_audit_log(db, claim.company_id, current_user.id, "delete", _EXPENSE_ENTITY, claim.id)
    form_config.delete_custom_values_for(db, claim.company_id, claim.id)
    db.delete(claim)
    db.commit()


@router.patch(
    "/expense-reports/{claim_id}",
    response_model=schemas.ExpenseReportOut,
)
def update_expense_report(
    claim_id: uuid.UUID,
    payload: schemas.ExpenseReportUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py.
    current_user: models.User = Depends(get_current_user),
):
    claim = crud.get_expense_claim_for_update(db, claim_id)
    # Another company's claim is simply not found (no "already decided by"
    # detail leaks across companies).
    if claim is None or claim.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Expense report not found")
    if claim.status == "draft":
        raise HTTPException(status_code=409, detail="This expense report has not been submitted yet.")
    # Two Reporting Managers: same atomic-with-lock guard as
    # update_work_entry/update_leave_request -- blocks a conflicting second
    # decision from the employee's other manager. Checked before the
    # permission gate so that manager sees a clear "already decided"
    # message instead of a confusing permission error once
    # can_decide_request_configurable (correctly) stops recognizing them
    # as the current decider.
    if claim.status in ("approved", "rejected"):
        decider = crud.employee_display_name(db, claim.approver_id)
        decider_type = crud.decided_by_manager_type(
            db.get(models.Employee, claim.employee_id), claim.approver_id
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"This request was already {claim.status} by {decider}"
                f" ({decider_type}) and cannot be decided again"
                if decider_type
                else f"This request was already {claim.status} and cannot be decided again"
            ),
        )
    if not crud.can_decide_request_configurable(
        db, current_user, claim.employee_id, "expense_claim", claim.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's assigned Reporting Manager can act on this request",
        )
    effective_status = crud.decide_configurable_request(
        db, current_user, claim.employee_id, "expense_claim", claim.id,
        payload.status, payload.decision_notes,
    )
    claim.status = effective_status
    claim.approver_id = current_user.employee_id
    claim.decision_notes = payload.decision_notes
    if effective_status != "pending":
        claim.decided_at = datetime.now(timezone.utc)
    if effective_status == "approved":
        # The advances this claim used are now spent (claimed).
        te.apply_advances_on_approval(db, claim)
    crud.create_audit_log(
        db, claim.company_id, current_user.id, payload.status, "expense_report", claim.id,
        changes={"notes": payload.decision_notes} if payload.decision_notes else None,
    )
    db.commit()
    db.refresh(claim)
    if effective_status != "pending":
        crud.notify_decision(
            db, claim.company_id, claim.employee_id, "Expense Report",
            effective_status, payload.decision_notes, "expense_report", claim.id,
        )
        db.commit()
    elif payload.status == "approved":
        _notify_step_passed(db, claim.company_id, claim.employee_id, "Expense Report", "expense_report",
                            claim.id, current_user.employee_id)
    return te.expense_report_out(db, claim, viewer=current_user)


@router.patch("/expense-reports/{claim_id}/payment", response_model=schemas.ExpenseReportOut)
def update_expense_payment(
    claim_id: uuid.UUID,
    payload: schemas.ExpensePaymentUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Finance / an administrator records that an APPROVED claim was paid, and
    when it cleared (the Accounting tab's Is Paid and Clearance Date)."""
    claim = _load_claim(db, claim_id, current_user, lock=True)
    if not _can_set_payment_fields(db, current_user, claim.employee_id):
        raise HTTPException(status_code=403, detail="Only Finance / an administrator can record a payment, and never on their own expense report.")
    if claim.status != "approved":
        raise HTTPException(status_code=409, detail="Only an approved expense report can be marked paid.")
    posted = claim.posting_date or claim.claim_date
    if payload.clearance_date and payload.clearance_date < posted:
        raise HTTPException(status_code=400, detail="Clearance date cannot be before the posting date.")
    was_paid = bool(claim.is_paid)
    claim.is_paid, claim.clearance_date = payload.is_paid, payload.clearance_date
    claim.updated_at = datetime.now(timezone.utc)
    crud.create_audit_log(db, claim.company_id, current_user.id, "paid" if payload.is_paid else "unpaid", _EXPENSE_ENTITY, claim.id)
    db.commit()
    db.refresh(claim)
    if payload.is_paid and not was_paid:
        ref = claim.claim_no or "your expense report"
        cleared = f" It cleared on {claim.clearance_date.strftime('%d %b %Y')}." if claim.clearance_date else ""
        crud.notify_requester_update(
            db, claim.company_id, claim.employee_id, request_type_label="Expense Report",
            event_key="expense_report_paid",
            title=f"Expense Report {ref} reimbursement paid",
            body=f"Your expense report {ref} has been reimbursed.{cleared}",
            entity_type="expense_report", entity_id=claim.id,
            extra_details={"Payment Status": "Paid",
                           "Clearance Date": claim.clearance_date.strftime("%d %b %Y") if claim.clearance_date else "—"},
            actor_employee_id=current_user.employee_id,
        )
        db.commit()
    return te.expense_report_out(db, claim, viewer=current_user)


@router.post("/expense-reports/{claim_id}/attachments", response_model=schemas.AttachmentOut, status_code=201)
def upload_expense_attachment(  # sync def -> threadpool (blocking file/DB I/O)
    claim_id: uuid.UUID,
    file: UploadFile = File(...),
    # The expense row (0-based, in the report's order) this file is the
    # receipt of; omitted = a general supporting document.
    line_index: int | None = Form(default=None, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Receipts / supporting documents. Owner only, while the claim can still be amended."""
    claim = _load_claim(db, claim_id, current_user, lock=line_index is not None)
    _require_owner(current_user, claim.employee_id, "expense report")
    if claim.status not in ("draft", "pending", "sent_back"):
        raise HTTPException(status_code=409, detail="Receipts can no longer be added to an expense report that has been decided.")
    line = None
    if line_index is not None:
        line = next((l for l in claim.lines if (l.sort_order or 0) == line_index), None)
        if line is None:
            raise HTTPException(status_code=422, detail="That expense row no longer exists. Reopen the report and try again.")
    file_url, size_bytes = save_uploaded_file(file, entity_type=_EXPENSE_ENTITY, entity_id=claim.id)
    a = crud.create_attachment(
        db, company_id=claim.company_id, entity_type=_EXPENSE_ENTITY, entity_id=claim.id,
        file_name=file.filename or "file", file_url=file_url, mime_type=file.content_type, size_bytes=size_bytes,
    )
    a.uploaded_by = current_user.id
    if line is not None:
        # One receipt per expense: a new one replaces the previous file.
        previous = line.receipt_attachment_id
        line.receipt_attachment_id = a.id
        db.flush()
        if previous is not None and previous != a.id:
            te.drop_attachments(db, [previous])
    crud.create_audit_log(db, claim.company_id, current_user.id, "attach", _EXPENSE_ENTITY, claim.id)
    db.commit()
    db.refresh(a)
    return _attachment_out(a)


@router.get("/expense-reports/{claim_id}/attachments", response_model=list[schemas.AttachmentOut])
def list_expense_attachments(
    claim_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    claim = _load_claim(db, claim_id, current_user)
    return [_attachment_out(a) for a in crud.list_attachments_for_entity(db, _EXPENSE_ENTITY, claim.id)]


@router.delete("/expense-reports/{claim_id}/attachments/{attachment_id}", status_code=204)
def delete_expense_attachment(
    claim_id: uuid.UUID,
    attachment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    claim = _load_claim(db, claim_id, current_user)
    _require_owner(current_user, claim.employee_id, "expense report")
    _require_editable(claim.status, "expense report")
    a = crud.get_attachment_for_entity(db, _EXPENSE_ENTITY, claim.id, attachment_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    delete_uploaded_file(a.file_url)
    crud.delete_attachment(db, a)
    db.commit()


@router.get("/expense-reports/{claim_id}/history")
def expense_report_history(
    claim_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    claim = _load_claim(db, claim_id, current_user)
    return te.history_for(db, claim.company_id, (_EXPENSE_ENTITY, "reimbursement"), claim.id)
