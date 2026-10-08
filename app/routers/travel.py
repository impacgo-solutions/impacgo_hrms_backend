import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud, expense_claims, models, schemas
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled, require_permission

router = APIRouter(
    prefix="/api", tags=["travel"],
    dependencies=[Depends(require_module_enabled("travel"))],
)


def _expense_report_out(
    db: Session,
    claim,
    names: dict[uuid.UUID, str] | None = None,
    codes: dict[uuid.UUID, str] | None = None,
) -> schemas.ExpenseReportOut:
    """names/codes, when given, are bulk-resolved lookups for a whole
    result set (see list_expense_reports) -- performance only, same output
    either way."""
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    if codes is not None:
        code = codes.get(claim.employee_id)
    else:
        employee = db.get(models.Employee, claim.employee_id)
        code = employee.employee_code if employee else None

    return schemas.ExpenseReportOut(
        id=claim.id,
        employee_id=claim.employee_id,
        reference=claim.purpose,
        claim_date=claim.claim_date,
        status=claim.status,
        items=[
            schemas.ExpenseLineItemOut(
                expense_date=line.expense_date,
                category=line.category,
                description=line.description,
                amount=float(line.amount),
            )
            for line in claim.lines
        ],
        approver_id=claim.approver_id,
        approver_name=name(claim.approver_id),
        decision_notes=claim.decision_notes,
        decided_at=claim.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, claim.employee_id), claim.approver_id
        ),
        employee_name=name(claim.employee_id),
        employee_code=code,
    )


def _travel_request_out(
    db: Session,
    r: models.TravelRequestModel,
    names: dict[uuid.UUID, str] | None = None,
    codes: dict[uuid.UUID, str] | None = None,
) -> schemas.TravelRequestOut:
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    if codes is not None:
        code = codes.get(r.employee_id)
    else:
        employee = db.get(models.Employee, r.employee_id)
        code = employee.employee_code if employee else None

    return schemas.TravelRequestOut(
        id=r.id,
        employee_id=r.employee_id,
        purpose=r.purpose,
        destination=r.destination,
        from_date=r.from_date,
        to_date=r.to_date,
        travel_mode=r.travel_mode,
        estimated_cost=float(r.estimated_cost) if r.estimated_cost is not None else None,
        status=r.status,
        approver_id=r.approver_id,
        approver_name=name(r.approver_id),
        decision_notes=r.decision_notes,
        decided_at=r.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, r.employee_id), r.approver_id
        ),
        employee_name=name(r.employee_id),
        employee_code=code,
    )


@router.get("/expense-reports", response_model=list[schemas.ExpenseReportOut])
def list_expense_reports(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_requests(db, current_user, "expense_claim")
    rows = crud.list_expense_claims(
        db, company.id, employee_ids=visible_ids, limit=limit, offset=offset,
    )
    id_pool = {c.employee_id for c in rows} | {c.approver_id for c in rows if c.approver_id}
    names = crud.employee_display_names_bulk(db, id_pool)
    codes = crud.employee_codes_bulk(db, {c.employee_id for c in rows})
    payroll = crud.reimbursement_payroll_statuses(db, company.id, "expense_claim", rows)
    out = [_expense_report_out(db, claim, names=names, codes=codes) for claim in rows]
    for item in out:
        item.payroll_status = payroll.get(item.id)
    return out


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
    if claim is None:
        raise HTTPException(status_code=404, detail="Expense report not found")
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
    crud.create_audit_log(db, claim.company_id, current_user.id, payload.status, "expense_report", claim.id)
    db.commit()
    db.refresh(claim)
    if effective_status != "pending":
        crud.notify_decision(
            db, claim.company_id, claim.employee_id, "Expense Report",
            effective_status, payload.decision_notes, "expense_report", claim.id,
        )
        db.commit()
    return _expense_report_out(db, claim)


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
    payroll = crud.reimbursement_payroll_statuses(db, company.id, "travel_request", rows)
    out = [_travel_request_out(db, r, names=names, codes=codes) for r in rows]
    for item in out:
        item.payroll_status = payroll.get(item.id)
    return out


@router.post("/travel-requests", status_code=201)
def create_travel_request(
    payload: schemas.TravelRequestApiCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs New Travel Request — self-service, same reasoning as Add Work
    Entry (see routers/work.py)."""
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
    clash = db.scalar(
        select(models.TravelRequestModel).where(
            models.TravelRequestModel.employee_id == payload.employee_id,
            models.TravelRequestModel.status.in_(("pending", "approved", "sent_back")),
            models.TravelRequestModel.from_date <= payload.to_date,
            models.TravelRequestModel.to_date >= payload.from_date,
        ).limit(1)
    )
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A {clash.status.replace('_', ' ')} travel request to {clash.destination or 'another destination'} "
                f"({clash.from_date.isoformat()} to {clash.to_date.isoformat()}) already overlaps these dates."
            ),
        )

    # WF-03: a requester with no Reporting Manager (e.g. the CEO) is no
    # longer refused -- crud.notify_new_request routes the request to the
    # fallback approvers (Owner / System Settings RBAC holders) who can
    # decide it; self-approval stays blocked.
    request = crud.create_travel_request(
        db,
        payload.employee_id,
        payload.purpose,
        payload.destination,
        payload.from_date,
        payload.to_date,
        payload.travel_mode,
        payload.estimated_cost,
    )
    crud.create_audit_log(db, company.id, current_user.id, "create", "travel_request", request.id)
    db.commit()
    crud.notify_new_request(db, company.id, employee, "Travel Request", "travel_request", request.id)
    db.commit()
    return {"id": str(request.id)}


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
    crud.create_audit_log(db, company.id, current_user.id, payload.status, "travel_request", request.id)
    db.commit()
    db.refresh(request)
    if effective_status != "pending":
        crud.notify_decision(
            db, company.id, request.employee_id, "Travel Request",
            effective_status, payload.decision_notes, "travel_request", request.id,
        )
        db.commit()
    return _travel_request_out(db, request)


@router.post("/expense-reports", status_code=201)
def create_expense_report(
    payload: schemas.ExpenseReportApiCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs New Expense Report — self-service. Reuses hcm.expense_claims,
    the same table Payroll > Reimbursements writes to (see
    crud.create_reimbursement), just with real multiple line items."""
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "travel_expense_approval", "expense_report", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")

    # WF-03: a requester with no Reporting Manager (e.g. the CEO) is no
    # longer refused -- crud.notify_new_request routes the request to the
    # fallback approvers (Owner / System Settings RBAC holders) who can
    # decide it; self-approval stays blocked.
    line_items = [
        {
            "date": item.date,
            "category": item.category,
            "description": item.description,
            "amount": item.amount,
        }
        for item in payload.items
    ]
    if employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    claim = crud.create_expense_report(
        db, company.id, payload.employee_id, payload.reference, payload.submitted_date, line_items
    )
    if getattr(claim, "idempotent_replay", False):
        # M-21: a double-click / retry of the report just filed -- same
        # report back, no second row, audit entry or approver notification.
        replay = {"id": str(claim.id), "claim_no": claim.claim_no, "duplicate": True}
        db.rollback()
        return replay
    crud.create_audit_log(db, company.id, current_user.id, "create", "expense_report", claim.id)
    db.commit()
    crud.notify_new_request(db, company.id, employee, "Expense Report", "expense_report", claim.id)
    db.commit()
    return {"id": str(claim.id), "claim_no": claim.claim_no}
