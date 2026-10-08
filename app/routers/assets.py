import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled, require_permission

router = APIRouter(
    prefix="/api", tags=["assets"],
    dependencies=[Depends(require_module_enabled("assets"))],
)

_STATUS_DISPLAY = {
    "available": "Available",
    "assigned": "Assigned",
    "repair": "In Repair",
    "in_repair": "In Repair",
    "retired": "Retired",
}


def _format_inr(amount: float | None) -> str:
    """Indian digit grouping (last 3 digits, then pairs) -- e.g.
    189000 -> '1,89,000'. Reconstructs the seeded display strings closely,
    though a recurring-cost suffix like '/ yr' isn't preserved."""
    if amount is None:
        return "—"
    n = str(int(round(amount)))
    if len(n) <= 3:
        return f"₹{n}"
    last3, rest = n[-3:], n[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return f"₹{','.join(groups)},{last3}"


def _to_out(asset) -> schemas.AssetInventoryItemOut:
    return schemas.AssetInventoryItemOut(
        id=asset.id,
        tag=asset.asset_tag,
        type=asset.asset_type,
        model=asset.model or "",
        status=_STATUS_DISPLAY.get(asset.status, asset.status.title()),
        value=_format_inr(asset.purchase_value),
        purchased=asset.purchased_on.isoformat() if asset.purchased_on else "—",
    )


@router.get("/asset-inventory", response_model=list[schemas.AssetInventoryItemOut])
def list_asset_inventory(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [
        _to_out(a)
        for a in crud.list_asset_inventory(db, company.id, limit=limit, offset=offset)
    ]


@router.post(
    "/asset-inventory",
    response_model=schemas.AssetInventoryItemOut,
    status_code=201,
)
def create_asset_inventory_item(
    payload: schemas.AssetInventoryItemCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    company = db.get(models.Company, current_user.company_id)
    # M-04: value must be a non-negative amount and the purchase date a real
    # date (YYYY-MM-DD) not in the future -- 422 instead of silently storing
    # None / a negative number.
    raw_value = payload.value.strip()
    amount = crud.parse_inr_amount(raw_value)
    if amount is None or raw_value.lstrip("₹ ").startswith("-"):
        raise HTTPException(status_code=422, detail="Asset value must be a non-negative amount, e.g. 45000.")
    purchased = crud.parse_date_safe(payload.purchased)
    if purchased is None:
        raise HTTPException(status_code=422, detail="Purchase date must be a valid date in YYYY-MM-DD format.")
    if purchased > crud.company_today(db, company.id):
        raise HTTPException(status_code=422, detail="Purchase date can't be in the future.")
    try:
        asset = crud.create_asset_inventory_item(
            db,
            company.id,
            payload.tag,
            payload.type,
            payload.model,
            payload.status.lower().replace(" ", "_"),
            amount,
            purchased,
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "asset_inventory_item", asset.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(asset)
    return _to_out(asset)


@router.patch(
    "/asset-inventory/{item_id}/status",
    response_model=schemas.AssetInventoryItemOut,
)
def update_asset_inventory_status(
    item_id: uuid.UUID,
    payload: schemas.AssetInventoryItemStatusUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    company = db.get(models.Company, current_user.company_id)
    asset = crud.get_asset_inventory_item(db, company.id, item_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset.status = payload.status.lower().replace(" ", "_")
    crud.create_audit_log(db, company.id, current_user.id, "update", "asset_inventory_item", asset.id)
    db.commit()
    db.refresh(asset)
    return _to_out(asset)


@router.delete("/asset-inventory/{item_id}", status_code=204)
def delete_asset_inventory_item(
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    company = db.get(models.Company, current_user.company_id)
    asset = crud.get_asset_inventory_item(db, company.id, item_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    crud.create_audit_log(db, company.id, current_user.id, "delete", "asset_inventory_item", item_id)
    crud.delete_asset_inventory_item(db, item_id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This asset has assignment history and cannot be deleted.",
        ) from exc


def _assignment_out(a: models.AssetAssignment) -> schemas.AssetAssignmentOut:
    return schemas.AssetAssignmentOut(
        id=a.id,
        asset_tag=a.asset.asset_tag,
        asset_type=a.asset.asset_type,
        employee_id=a.employee_id,
        employee_name=crud._full_name(a.employee),
        assigned_on=a.assigned_on,
        returned_on=a.returned_on,
        return_condition=a.return_condition,
        return_notes=a.return_notes,
    )


@router.get("/asset-assignments", response_model=list[schemas.AssetAssignmentOut])
def list_asset_assignments(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [
        _assignment_out(a)
        for a in crud.list_asset_assignments(db, company.id, limit=limit, offset=offset)
    ]


@router.post(
    "/asset-assignments",
    response_model=schemas.AssetAssignmentOut,
    status_code=201,
)
def create_asset_assignment(
    payload: schemas.AssetAssignmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    company = db.get(models.Company, current_user.company_id)
    asset = crud.get_asset_inventory_item_by_tag(db, company.id, payload.asset_tag)
    if asset is None:
        raise HTTPException(status_code=409, detail=f"Asset tag '{payload.asset_tag}' not found")
    if asset.status == "assigned":
        raise HTTPException(
            status_code=409,
            detail=f"Asset tag '{payload.asset_tag}' is already assigned to another employee.",
        )
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=422, detail="Employee not found in your company.")
    if asset.purchased_on is not None and payload.assigned_on < asset.purchased_on:
        raise HTTPException(status_code=422, detail="Assigned date can't be before the asset's purchase date.")
    assignment = crud.create_asset_assignment(db, asset.id, payload.employee_id, payload.assigned_on)
    asset.status = "assigned"
    crud.create_audit_log(db, company.id, current_user.id, "create", "asset_assignment", assignment.id)
    db.commit()
    db.refresh(assignment)
    return _assignment_out(assignment)


@router.patch(
    "/asset-assignments/{assignment_id}/return",
    response_model=schemas.AssetAssignmentOut,
)
def process_asset_return(
    assignment_id: uuid.UUID,
    payload: schemas.AssetReturnUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    company = db.get(models.Company, current_user.company_id)
    assignment = crud.get_asset_assignment_for_update(db, assignment_id)
    if assignment is None or assignment.asset is None or assignment.asset.company_id != company.id:
        raise HTTPException(status_code=404, detail="Asset assignment not found")
    # M-04: one return per assignment; return date on/after assignment.
    if assignment.returned_on is not None:
        raise HTTPException(
            status_code=409,
            detail=f"This asset was already returned on {assignment.returned_on.isoformat()}.",
        )
    if payload.returned_on < assignment.assigned_on:
        raise HTTPException(status_code=422, detail="Return date can't be before the assigned date.")
    if payload.returned_on > crud.company_today(db, company.id):
        raise HTTPException(status_code=422, detail="Return date can't be in the future.")
    assignment.returned_on = payload.returned_on
    assignment.return_condition = payload.return_condition
    assignment.return_notes = payload.return_notes
    assignment.asset.status = "available"
    crud.create_audit_log(db, company.id, current_user.id, "update", "asset_assignment", assignment.id)
    db.commit()
    db.refresh(assignment)
    return _assignment_out(assignment)


def _asset_request_out(
    db: Session,
    r: models.AssetRequestModel,
    names: dict[uuid.UUID, str] | None = None,
) -> schemas.AssetRequestOut:
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    return schemas.AssetRequestOut(
        id=r.id,
        employee_id=r.employee_id,
        asset_type=r.asset_type,
        justification=r.justification,
        status=r.status,
        approver_id=r.approver_id,
        approver_name=name(r.approver_id),
        decision_notes=r.decision_notes,
        decided_at=r.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, r.employee_id), r.approver_id
        ),
    )


@router.get("/asset-requests", response_model=list[schemas.AssetRequestOut])
def list_asset_requests(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    rows = crud.list_asset_requests(db, company.id, limit=limit, offset=offset)
    approver_ids = {r.approver_id for r in rows if r.approver_id}
    names = crud.employee_display_names_bulk(db, approver_ids)
    return [_asset_request_out(db, r, names=names) for r in rows]


@router.post(
    "/asset-requests",
    response_model=schemas.AssetRequestOut,
    status_code=201,
)
def create_asset_request(
    payload: schemas.AssetRequestCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "asset_management", "asset_management", "manage"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    # WF-03: a requester with no Reporting Manager (e.g. the CEO) is no
    # longer refused -- crud.notify_new_request routes the request to the
    # fallback approvers (Owner / System Settings RBAC holders) who can
    # decide it; self-approval stays blocked.
    request = crud.create_asset_request(db, payload.employee_id, payload.asset_type, payload.justification)
    crud.create_audit_log(db, company.id, current_user.id, "create", "asset_request", request.id)
    db.commit()
    db.refresh(request)
    crud.notify_new_request(db, company.id, employee, "Asset Request", "asset_request", request.id)
    db.commit()
    return _asset_request_out(db, request)


@router.patch(
    "/asset-requests/{request_id}",
    response_model=schemas.AssetRequestOut,
)
def update_asset_request(
    request_id: uuid.UUID,
    payload: schemas.AssetRequestUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py.
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    request = crud.get_asset_request_for_update(db, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Asset request not found")
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
        db, current_user, request.employee_id, "asset_request", request.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's assigned Reporting Manager can act on this request",
        )
    effective_status = crud.decide_configurable_request(
        db, current_user, request.employee_id, "asset_request", request.id,
        payload.status, payload.decision_notes,
    )
    request.status = effective_status
    request.approver_id = current_user.employee_id
    request.decision_notes = payload.decision_notes
    if effective_status != "pending":
        request.decided_at = datetime.now(timezone.utc)
    crud.create_audit_log(db, company.id, current_user.id, payload.status, "asset_request", request.id)
    db.commit()
    db.refresh(request)
    if effective_status != "pending":
        crud.notify_decision(
            db, company.id, request.employee_id, "Asset Request",
            effective_status, payload.decision_notes, "asset_request", request.id,
        )
        db.commit()
    return _asset_request_out(db, request)


@router.get(
    "/asset-recovery-deductions",
    response_model=list[schemas.AssetRecoveryDeductionOut],
)
def list_asset_recovery_deductions(
    employee_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Assets > Asset Recovery Deduction audit trail -- every request
    regardless of status, each showing whether/when it was actually
    applied to a payslip (Payroll Settings > Deductions Configuration)."""
    return crud.list_asset_recovery_deductions(db, current_user.company_id, employee_id)


@router.post(
    "/asset-recovery-deductions",
    response_model=schemas.AssetRecoveryDeductionOut,
    status_code=201,
)
def create_asset_recovery_deduction(
    payload: schemas.AssetRecoveryDeductionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    """HR/Asset-management proposes a recovery amount for an employee
    (e.g. an unreturned/damaged asset), targeted at a specific payroll
    period -- starts 'pending', only picked up for payroll once approved
    (PATCH .../decision)."""
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if payload.asset_assignment_id is not None:
        assignment = db.get(models.AssetAssignment, payload.asset_assignment_id)
        if assignment is None or assignment.employee_id != payload.employee_id:
            raise HTTPException(status_code=404, detail="Asset assignment not found for this employee")
    deduction = crud.create_asset_recovery_deduction(
        db, current_user.company_id, payload.employee_id, payload.asset_assignment_id,
        payload.amount, payload.reason, payload.target_period_month, payload.target_period_year,
        current_user.id,
    )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "create", "asset_recovery_deduction", deduction.id,
    )
    db.commit()
    db.refresh(deduction)
    return crud.asset_recovery_deduction_out(db, deduction)


@router.patch(
    "/asset-recovery-deductions/{deduction_id}/decision",
    response_model=schemas.AssetRecoveryDeductionOut,
)
def decide_asset_recovery_deduction(
    deduction_id: uuid.UUID,
    payload: schemas.AssetRecoveryDeductionDecision,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    deduction = crud.get_asset_recovery_deduction_for_update(db, deduction_id)
    if deduction is None or deduction.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Asset recovery deduction not found")
    if deduction.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"This deduction was already {deduction.status} and cannot be decided again",
        )
    crud.decide_asset_recovery_deduction(
        db, deduction, payload.status, payload.decision_notes, current_user.employee_id,
    )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, payload.status, "asset_recovery_deduction", deduction.id,
    )
    db.commit()
    db.refresh(deduction)
    return crud.asset_recovery_deduction_out(db, deduction)
