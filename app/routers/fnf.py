"""Full & Final Settlement (Payroll > Full & Final Settlements, People >
Offboarding). HR enters every earning / deduction; the totals are always
computed from those lines.

    GET  /api/fnf-settlements                          every exit + its settlement status
    GET  /api/exit-requests/{exit_id}/fnf              settlement, reference items, allowed actions
    PUT  /api/exit-requests/{exit_id}/fnf              save the draft (lines + notes)
    POST /api/exit-requests/{exit_id}/fnf/approve      draft -> approved
    POST /api/exit-requests/{exit_id}/fnf/reopen       approved -> draft (reason required)
    POST /api/exit-requests/{exit_id}/fnf/mark-paid    approved -> paid (date, mode, reference)

Prepare / mark paid: Payroll (Process) Edit or Admin, or the Owner.
Approve / reopen: Payroll (Process) Admin or the Owner -- and not the person
who prepared the figures (the Owner excepted). The F&F Settlement
Statement is generated from an approved / paid settlement through
routers/exit_letters.py (letter_type 'fnf_statement').
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud, exit_letter_html_renderer as renderer, models, schemas
from ..database import get_db
from ..deps import _OWNER_ROLE_NAME, require_module_enabled, require_permission

router = APIRouter(
    prefix="/api",
    tags=["fnf"],
    dependencies=[Depends(require_module_enabled("payroll"))],
)

_require_payroll = require_permission("payroll_process")


def _is_owner(db: Session, user: models.User) -> bool:
    role = crud.get_user_primary_role(db, user.id)
    return role is not None and role.name == _OWNER_ROLE_NAME


def _is_approver(db: Session, user: models.User) -> bool:
    if _is_owner(db, user):
        return True
    # Same effective matrix require_permission uses (role + individual grants).
    return crud.effective_user_matrix(db, user).get("payroll_process") == "a"


def _detail(db: Session, user: models.User, exit_id: uuid.UUID) -> dict:
    detail = crud.get_exit_letter_detail(db, user.company_id, exit_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Exit request not found")
    return detail


def _prepare_block(exit_request: models.ExitRequestModel) -> str | None:
    if exit_request.status in crud.FNF_EXIT_STATUSES:
        return None
    return (
        f"The exit is {exit_request.status.replace('_', ' ')} -- a full & final settlement can be "
        "prepared once the exit is approved."
    )


def _summary_fields(detail: dict) -> dict:
    exit_request, employee = detail["exit_request"], detail["employee"]
    settlement = detail.get("settlement")
    block = _prepare_block(exit_request)
    return dict(
        exit_request_id=exit_request.id,
        employee_id=employee.id,
        employee_name=f"{employee.first_name} {employee.last_name or ''}".strip(),
        employee_code=employee.employee_code,
        designation=detail["designation"].name if detail["designation"] else None,
        department=detail["department"].name if detail["department"] else None,
        exit_status=exit_request.status,
        resignation_date=exit_request.resignation_date,
        last_working_day=exit_request.last_working_day,
        fnf_status=crud.fnf_status(settlement),
        net_amount=float(settlement.net_amount) if settlement is not None else None,
        can_prepare=block is None,
        prepare_blocked_reason=block,
    )


def _settlement_out(settlement: models.FinalSettlement | None) -> schemas.FnfSettlementOut | None:
    if settlement is None:
        return None
    return schemas.FnfSettlementOut(
        id=settlement.id,
        status=crud.fnf_status(settlement),
        payable_amount=float(settlement.payable_amount or 0),
        recovery_amount=float(settlement.recovery_amount or 0),
        net_amount=float(settlement.net_amount or 0),
        notes=settlement.notes,
        prepared_by_name=settlement.prepared_by_name,
        prepared_at=settlement.prepared_at,
        approved_by_name=settlement.approved_by_name,
        approved_at=settlement.approved_at,
        approval_notes=settlement.approval_notes,
        payment_date=settlement.payment_date,
        payment_mode=settlement.payment_mode,
        payment_reference=settlement.payment_reference,
        paid_at=settlement.paid_at,
        lines=[schemas.FnfLineOut.model_validate(l) for l in settlement.lines],
    )


def _detail_out(db: Session, user: models.User, detail: dict) -> schemas.FnfDetailOut:
    settlement = detail.get("settlement")
    status = crud.fnf_status(settlement)
    employee, exit_request = detail["employee"], detail["exit_request"]
    approver = _is_approver(db, user)
    approve_block = None
    if status == "draft":
        if not approver:
            approve_block = "Only a Payroll (Process) Admin or the Organization Owner can approve."
        elif not settlement.lines:
            approve_block = "Add at least one earning or deduction before approving."
        elif (not _is_owner(db, user) and settlement.prepared_by is not None
              and settlement.prepared_by == user.employee_id):
            approve_block = "You prepared this settlement -- another payroll approver must approve it."
    return schemas.FnfDetailOut(
        **_summary_fields(detail),
        date_of_joining=employee.date_of_joining,
        service_period=renderer._tenure(employee.date_of_joining, exit_request.last_working_day),
        settlement=_settlement_out(settlement),
        reference_items=[schemas.FnfReferenceItemOut(**i) for i in crud.fnf_reference_items(db, user.company_id, detail)],
        earning_components=list(crud.FNF_EARNING_COMPONENTS),
        deduction_components=list(crud.FNF_DEDUCTION_COMPONENTS),
        can_edit=status in ("none", "draft") and _prepare_block(exit_request) is None,
        can_approve=status == "draft" and approve_block is None,
        approve_blocked_reason=approve_block if status == "draft" else None,
        can_reopen=status == "approved" and approver,
        can_mark_paid=status == "approved",
    )


def _refresh(db: Session, user: models.User, exit_id: uuid.UUID) -> schemas.FnfDetailOut:
    db.expire_all()
    return _detail_out(db, user, _detail(db, user, exit_id))


def _existing(detail: dict) -> models.FinalSettlement:
    settlement = detail.get("settlement")
    if settlement is None:
        raise HTTPException(status_code=404, detail="No full & final settlement has been prepared for this exit yet.")
    return settlement


@router.get("/fnf-settlements", response_model=list[schemas.FnfSummaryOut])
def list_fnf_settlements(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll),
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
        if detail is not None:
            out.append(schemas.FnfSummaryOut(**_summary_fields(detail)))
    return out


@router.get("/exit-requests/{exit_id}/fnf", response_model=schemas.FnfDetailOut)
def get_fnf(
    exit_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll),
):
    return _detail_out(db, current_user, _detail(db, current_user, exit_id))


@router.put("/exit-requests/{exit_id}/fnf", response_model=schemas.FnfDetailOut)
def save_fnf(
    exit_id: uuid.UUID,
    payload: schemas.FnfSaveRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll),
):
    detail = _detail(db, current_user, exit_id)
    try:
        settlement = crud.save_fnf_draft(
            db, detail, lines=[l.model_dump() for l in payload.lines], notes=payload.notes, actor=current_user,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "final_settlement", settlement.id,
        changes={"lines": str(len(payload.lines)), "net_amount": str(settlement.net_amount)},
    )
    if settlement.lines:
        crud.notify_fnf_event(db, current_user.company_id, detail, settlement, "submitted", current_user)
    db.commit()
    return _refresh(db, current_user, exit_id)


@router.post("/exit-requests/{exit_id}/fnf/approve", response_model=schemas.FnfDetailOut)
def approve_fnf(
    exit_id: uuid.UUID,
    payload: schemas.FnfApproveRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll),
):
    if not _is_approver(db, current_user):
        raise HTTPException(status_code=403, detail="Only a Payroll (Process) Admin or the Organization Owner can approve.")
    detail = _detail(db, current_user, exit_id)
    settlement = _existing(detail)
    try:
        crud.approve_fnf(db, settlement, actor=current_user, notes=payload.notes,
                         is_owner=_is_owner(db, current_user))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "approve", "final_settlement",
                          settlement.id, changes={"net_amount": str(settlement.net_amount)})
    crud.notify_fnf_event(db, current_user.company_id, detail, settlement, "approved", current_user,
                          note=payload.notes)
    db.commit()
    return _refresh(db, current_user, exit_id)


@router.post("/exit-requests/{exit_id}/fnf/reopen", response_model=schemas.FnfDetailOut)
def reopen_fnf(
    exit_id: uuid.UUID,
    payload: schemas.FnfReopenRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll),
):
    if not _is_approver(db, current_user):
        raise HTTPException(status_code=403, detail="Only a Payroll (Process) Admin or the Organization Owner can reopen.")
    detail = _detail(db, current_user, exit_id)
    settlement = _existing(detail)
    try:
        crud.reopen_fnf(db, settlement, actor=current_user, reason=payload.reason)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, current_user.company_id, current_user.id, "reopen", "final_settlement",
                          settlement.id, changes={"reason": payload.reason.strip()[:200]})
    crud.notify_fnf_event(db, current_user.company_id, detail, settlement, "reopened", current_user,
                          note=payload.reason.strip())
    db.commit()
    return _refresh(db, current_user, exit_id)


@router.post("/exit-requests/{exit_id}/fnf/mark-paid", response_model=schemas.FnfDetailOut)
def mark_fnf_paid(
    exit_id: uuid.UUID,
    payload: schemas.FnfMarkPaidRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll),
):
    detail = _detail(db, current_user, exit_id)
    settlement = _existing(detail)
    try:
        crud.mark_fnf_paid(
            db, current_user.company_id, settlement, actor=current_user, payment_date=payload.payment_date,
            payment_mode=payload.payment_mode, payment_reference=payload.payment_reference,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "mark_paid", "final_settlement", settlement.id,
        changes={"payment_date": payload.payment_date.isoformat(), "payment_mode": payload.payment_mode},
    )
    crud.notify_fnf_event(db, current_user.company_id, detail, settlement, "paid", current_user)
    db.commit()
    return _refresh(db, current_user, exit_id)
