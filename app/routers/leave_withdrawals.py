"""Leave Withdrawal -- an employee asks to revoke their pending / approved
leave; decided through the same Reporting Manager approval workflow as
Leave Apply (crud.can_decide_request_configurable / decide_configurable_
request with doctype "leave_withdrawal": the employee's reporting manager(s),
or a configured multi-step chain, plus the company-wide leave approvers).

The leave stays exactly as it is until the withdrawal is APPROVED. Then the
leave (and every auto-split leg of it) becomes "withdrawn" and the days of
every leg that had been approved go back to the balance -- exactly once
(the withdrawal row records restored_days; a decided withdrawal can't be
decided again). A REJECTED withdrawal changes nothing. Attendance's leave
gate (crud.get_blocking_leave_window) only blocks approved leave, so a
withdrawn leave no longer blocks Check In / Check Out.
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import crud, leave_policy, models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/api", tags=["leave"])
DOCTYPE = "leave_withdrawal"
WITHDRAWABLE = ("pending", "l1_approved", "sent_back", "approved")


class LeaveWithdrawalCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class LeaveWithdrawalDecision(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class LeaveWithdrawalOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str | None = None
    department: str | None = None
    reporting_manager_name: str | None = None
    leave_request_id: uuid.UUID
    leave_type: str
    from_date: datetime.date
    to_date: datetime.date
    days: float
    is_half_day: bool = False
    leave_status_before: str
    leave_status_now: str
    reason: str
    status: str
    requested_at: datetime.datetime
    approver_id: uuid.UUID | None = None
    approver_name: str | None = None
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    restored_days: float | None = None
    restored_detail: str | None = None
    can_decide: bool = False


def _group(db: Session, leave: models.LeaveRequest, lock: bool = False) -> list[models.LeaveRequest]:
    """The leave and every auto-split leg sharing its group id."""
    if leave.linked_leave_request_id is None:
        return [leave]
    gid = leave.linked_leave_request_id
    q = select(models.LeaveRequest).where(
        (models.LeaveRequest.linked_leave_request_id == gid) | (models.LeaveRequest.id == gid)
    ).order_by(models.LeaveRequest.id)
    if lock:
        q = q.with_for_update()
    rows = list(db.scalars(q).all())
    return rows or [leave]


def _approval_started(db: Session, leave: models.LeaveRequest) -> bool:
    """Has anyone approved any step of this leave yet? (approved / L1, or a
    recorded approve action in the tenant's configured multi-step chain)."""
    if leave.status in ("approved", "l1_approved"):
        return True
    return db.scalar(
        select(func.count()).select_from(models.ApprovalAction)
        .join(models.ApprovalRequest, models.ApprovalRequest.id == models.ApprovalAction.request_id)
        .where(models.ApprovalRequest.doctype == "leave_request", models.ApprovalRequest.document_id == leave.id,
               models.ApprovalAction.action.in_(("approve", "approved")))) > 0


def _leave_fields(db: Session, w: models.LeaveWithdrawal) -> dict:
    leave = db.get(models.LeaveRequest, w.leave_request_id)
    group = _group(db, leave) if leave else []
    days = sum(float(lr.days) for lr in group)
    return dict(
        leave_type=" + ".join(lr.leave_type.name for lr in group if lr.leave_type) or "—",
        start_date=crud._fmt_req_date(min(lr.from_date for lr in group)) if group else "—",
        end_date=crud._fmt_req_date(max(lr.to_date for lr in group)) if group else "—",
        number_of_days=f"{days:g}",
    )


def _manager_ids(employee: models.Employee) -> list[uuid.UUID]:
    """The employee's configured reporting manager(s) -- whatever their role."""
    return [i for i in dict.fromkeys((employee.reporting_manager_id, employee.dotted_line_manager_id)) if i]


def _first_approver_ids(db: Session, w: models.LeaveWithdrawal, employee: models.Employee) -> list[uuid.UUID]:
    """Who is asked first: under a configured chain (Administration >
    Approval Workflows) only the current step's approver(s); otherwise the
    reporting manager(s), as before."""
    if not crud._has_custom_approval_workflow(db, w.company_id, DOCTYPE):
        return _manager_ids(employee)
    from .. import approval_engine
    request = crud._get_or_start_approval_request(db, w.company_id, DOCTYPE, w.id, employee.id)
    ids = approval_engine.current_step_approver_employee_ids(db, request, employee)
    return sorted(ids, key=str) or _manager_ids(employee)


def _notify_requested(db: Session, w: models.LeaveWithdrawal, employee: models.Employee) -> None:
    """Withdrawal raised -> reporting manager(s): in-app + leave-style email;
    the configured HR inbox(es) get the email too (as for Leave Apply)."""
    from .. import email_service
    from ..config import settings
    name = crud.employee_display_name(db, employee.id)
    f = _leave_fields(db, w)
    for mid in _first_approver_ids(db, w, employee):
        crud.create_notification(db, w.company_id, crud.get_user_id_for_employee(db, mid),
                                 f"Leave Withdrawal request from {name}",
                                 f"{name} asked to withdraw {f['leave_type']} ({f['start_date']} – {f['end_date']}, "
                                 f"{f['number_of_days']} day(s)). Reason: {w.reason}",
                                 entity_type=DOCTYPE, entity_id=w.id)
        m = db.get(models.Employee, mid)
        email_service.send_leave_withdrawal_email(
            db, to=m.work_email if m else None, kind="requested", employee_name=name,
            employee_code=employee.employee_code, reason=w.reason, status="Pending Approval", decided_by=None,
            restored=None, notes=None, withdrawal_id=w.id, company_id=w.company_id, from_display_name=name, **f)
    if settings.email_hr_recipients:
        email_service.send_leave_withdrawal_email(
            db, to=settings.email_hr_recipients, kind="requested", employee_name=name,
            employee_code=employee.employee_code, reason=w.reason, status="Pending Approval", decided_by=None,
            restored=None, notes=None, withdrawal_id=w.id, company_id=w.company_id, from_display_name=name, **f)


def _notify_decided(db: Session, w: models.LeaveWithdrawal, employee: models.Employee, decider: str | None,
                    decider_employee_id: uuid.UUID | None) -> None:
    """Decided (or withdrawn without approval) -> the employee (in-app +
    Approved / Rejected leave email) and the reporting manager(s) other than
    the decider (in-app + notice email)."""
    from .. import email_service
    name = crud.employee_display_name(db, employee.id)
    f = _leave_fields(db, w)
    status = "Approved" if w.status == "approved" else "Rejected"
    restored = (f"{float(w.restored_days):g} day(s)" if w.restored_days else "—") if w.status == "approved" else "—"
    auto = w.approver_id is None and w.status == "approved"
    title = ("Leave withdrawn" if auto else f"Leave Withdrawal {w.status}")
    emp_user = crud.get_user_id_for_employee(db, employee.id)
    crud.create_notification(
        db, w.company_id, emp_user, title,
        (f"Your {f['leave_type']} ({f['start_date']} – {f['end_date']}) was withdrawn. {w.decision_notes} {w.restored_detail or ''}".strip()
         if auto and w.decision_notes == leave_policy.AUTO_APPROVE_NOTE else
         f"Your {f['leave_type']} ({f['start_date']} – {f['end_date']}) was withdrawn — it hadn't been approved yet, so no approval was needed."
         if auto else f"{decider} {w.status} your withdrawal of {f['leave_type']} ({f['start_date']} – {f['end_date']})."
         + (f" {w.restored_detail}." if w.status == "approved" and w.restored_detail else "")),
        entity_type=DOCTYPE, entity_id=w.id)
    email_service.send_leave_withdrawal_email(
        db, to=employee.work_email, kind="approved" if w.status == "approved" else "rejected", employee_name=name,
        employee_code=employee.employee_code, reason=w.reason, status=status,
        decided_by=decider or "No approval needed", restored=restored, notes=w.decision_notes, withdrawal_id=w.id,
        company_id=w.company_id, from_display_name=decider, **f)
    for mid in _manager_ids(employee):
        if mid == decider_employee_id:
            continue
        crud.create_notification(db, w.company_id, crud.get_user_id_for_employee(db, mid), f"{name}: {title}",
                                 f"{name}'s {f['leave_type']} ({f['start_date']} – {f['end_date']}) withdrawal: {status}"
                                 + (f" by {decider}" if decider else " (no approval needed)") + ".",
                                 entity_type=DOCTYPE, entity_id=w.id)
        m = db.get(models.Employee, mid)
        email_service.send_leave_withdrawal_email(
            db, to=m.work_email if m else None, kind="notice", employee_name=name,
            employee_code=employee.employee_code, reason=w.reason, status=status,
            decided_by=decider or "No approval needed", restored=restored, notes=w.decision_notes,
            withdrawal_id=w.id, company_id=w.company_id, from_display_name=decider, **f)


def _apply_withdrawal(db: Session, w: models.LeaveWithdrawal, group: list[models.LeaveRequest],
                      actor: models.User) -> None:
    """Approved withdrawal: every live leg becomes 'withdrawn'; approved
    legs give their days back to the balance (exactly once)."""
    from .. import approval_engine
    restored, parts = 0.0, []
    for lr in group:
        before = lr.status
        if before in ("withdrawn", "cancelled", "rejected"):
            continue
        if before == "approved":
            crud.adjust_leave_allocation_used(db, lr.employee_id, lr.leave_type_id, -float(lr.days),
                                              on_date=lr.from_date)
            restored += float(lr.days)
            parts.append(f"{float(lr.days):g} day(s) {lr.leave_type.name if lr.leave_type else ''}".strip())
        else:
            # Not yet fully approved: close its open approval, nothing to restore.
            approval_engine.close_request(db, lr.company_id, "leave_request", lr.id, actor,
                                          status="cancelled", comments="Withdrawn by employee")
        lr.status = "withdrawn"
        crud.create_audit_log(db, lr.company_id, actor.id, "withdraw", "leave_request", lr.id,
                              changes={"before": before, "after": "withdrawn", "withdrawal_id": str(w.id)})
    w.restored_days = restored
    w.restored_detail = ", ".join(parts) + " restored to the balance" if parts else "No balance to restore (leave wasn't approved yet)"


def _can_decide(db: Session, user: models.User, w: models.LeaveWithdrawal) -> bool:
    if w.status != "pending" or user.employee_id is None or user.employee_id == w.employee_id:
        return False
    # N-07: outside the reporting chain only an org-wide leave administrator
    # (Owner / Admin on Leave Approval) decides -- a granular leave
    # approve/reject action alone no longer does. Default flow only: in a
    # configured chain only the active step decides.
    if leave_policy.is_leave_admin(db, user) and not crud._has_custom_approval_workflow(db, w.company_id, DOCTYPE):
        return True
    return crud.can_decide_request_configurable(db, user, w.employee_id, DOCTYPE, w.id)


def _out(db: Session, w: models.LeaveWithdrawal, viewer: models.User | None = None,
         names: dict | None = None) -> LeaveWithdrawalOut:
    leave = db.get(models.LeaveRequest, w.leave_request_id)
    e = db.get(models.Employee, w.employee_id)

    def name(i):
        if i is None:
            return None
        return (names or {}).get(i) or crud.employee_display_name(db, i)

    group = _group(db, leave) if leave else []
    return LeaveWithdrawalOut(
        id=w.id, employee_id=w.employee_id, employee_name=name(w.employee_id) or "—",
        employee_code=e.employee_code if e else None,
        department=e.department.name if e and e.department else None,
        reporting_manager_name=name(e.reporting_manager_id) if e and e.reporting_manager_id else None,
        leave_request_id=w.leave_request_id,
        leave_type=" + ".join(lr.leave_type.name for lr in group if lr.leave_type) or "—",
        from_date=min(lr.from_date for lr in group) if group else w.requested_at.date(),
        to_date=max(lr.to_date for lr in group) if group else w.requested_at.date(),
        days=float(sum(float(lr.days) for lr in group)),
        is_half_day=bool(leave and leave.is_half_day),
        leave_status_before=w.leave_status_before, leave_status_now=leave.status if leave else "—",
        reason=w.reason, status=w.status, requested_at=w.requested_at, approver_id=w.approver_id,
        approver_name=name(w.approver_id), decision_notes=w.decision_notes, decided_at=w.decided_at,
        restored_days=float(w.restored_days) if w.restored_days is not None else None,
        restored_detail=w.restored_detail,
        can_decide=bool(viewer is not None and _can_decide(db, viewer, w)),
    )


@router.post("/leave-requests/{leave_request_id}/withdraw", response_model=LeaveWithdrawalOut, status_code=201)
def request_leave_withdrawal(
    leave_request_id: uuid.UUID,
    payload: LeaveWithdrawalCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The employee asks to withdraw their own leave. The leave is untouched
    (still blocking attendance if approved) until this is approved."""
    leave = crud.get_leave_request_for_update(db, leave_request_id)
    if leave is None or leave.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Leave request not found")
    if current_user.employee_id is None or current_user.employee_id != leave.employee_id:
        raise HTTPException(status_code=403, detail="You can only withdraw your own leave")
    if leave.status not in WITHDRAWABLE:
        raise HTTPException(status_code=409, detail=f"This leave is {leave.status.replace('_', ' ')} and can't be withdrawn.")
    group_ids = [lr.id for lr in _group(db, leave)]
    crud._advisory_lock(db, "leave_withdrawal", str(group_ids[0]))
    if db.scalar(select(models.LeaveWithdrawal.id).where(
            models.LeaveWithdrawal.leave_request_id.in_(group_ids),
            models.LeaveWithdrawal.status == "pending").limit(1)) is not None:
        raise HTTPException(status_code=409, detail="A withdrawal for this leave is already waiting for approval.")
    now = datetime.datetime.now(datetime.timezone.utc)
    w = models.LeaveWithdrawal(
        id=uuid.uuid4(), company_id=leave.company_id, employee_id=leave.employee_id,
        leave_request_id=leave.id, leave_status_before=leave.status, reason=payload.reason.strip(),
        status="pending", requested_at=now)
    db.add(w)
    db.flush()
    crud.create_audit_log(db, leave.company_id, current_user.id, "request_withdrawal", "leave_request", leave.id,
                          changes={"withdrawal_id": str(w.id), "leave_status": leave.status})
    if not _approval_started(db, leave):
        # Nobody has approved any step yet: withdrawn at once (as the
        # existing Cancel does) -- no approval needed, nothing to restore.
        from .. import approval_engine
        for lr in _group(db, leave, lock=True):
            if lr.status in ("withdrawn", "cancelled", "rejected", "approved"):
                continue
            before = lr.status
            lr.status = "withdrawn"
            approval_engine.close_request(db, lr.company_id, "leave_request", lr.id, current_user,
                                          status="cancelled", comments="Withdrawn by employee before approval")
            for note in db.scalars(select(models.Notification).where(
                    models.Notification.entity_type == "leave_request", models.Notification.entity_id == lr.id,
                    models.Notification.read_at.is_(None), models.Notification.user_id != current_user.id)):
                note.read_at = now
            crud.create_audit_log(db, lr.company_id, current_user.id, "withdraw", "leave_request", lr.id,
                                  changes={"before": before, "after": "withdrawn", "withdrawal_id": str(w.id)})
        w.status, w.decided_at, w.restored_days = "approved", now, 0
        w.restored_detail = "Withdrawn before any approval — no approval needed, nothing to restore"
        db.commit()
        db.refresh(w)
        _notify_decided(db, w, leave.employee, None, None)
        db.commit()
        return _out(db, w, current_user)
    if not leave_policy.has_any_approver(db, leave.company_id, leave.employee, DOCTYPE, w.id):
        # M-08: nobody else could ever decide it -- approved automatically,
        # days restored, 'auto_approve' audit entry.
        from .. import approval_engine
        w.status, w.decided_at = "approved", now
        w.decision_notes = leave_policy.AUTO_APPROVE_NOTE
        _apply_withdrawal(db, w, _group(db, leave, lock=True), current_user)
        approval_engine.close_request(db, w.company_id, DOCTYPE, w.id, current_user,
                                      status="approved", comments=leave_policy.AUTO_APPROVE_NOTE)
        crud.create_audit_log(db, w.company_id, current_user.id, "auto_approve", DOCTYPE, w.id,
                              changes={"leave_request_id": str(leave.id), "restored_days": str(w.restored_days),
                                       "reason": leave_policy.AUTO_APPROVE_NOTE})
        db.commit()
        db.refresh(w)
        _notify_decided(db, w, leave.employee, None, None)
        db.commit()
        return _out(db, w, current_user)
    db.commit()
    db.refresh(w)
    _notify_requested(db, w, leave.employee)
    db.commit()
    return _out(db, w, current_user)


@router.get("/leave-withdrawals", response_model=list[LeaveWithdrawalOut])
def list_leave_withdrawals(
    status: str | None = None,
    limit: int | None = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Withdrawals the caller may see. Plain defaults: the unified Approvals
    inbox (routers/approval_inbox.py) calls this handler directly."""
    if status is not None and status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=422, detail="status must be pending, approved or rejected")
    visible = None if leave_policy.is_leave_admin(db, current_user) \
        else crud.get_visible_employee_ids_for_requests(db, current_user, "leave_request")
    q = (select(models.LeaveWithdrawal)
         .where(models.LeaveWithdrawal.company_id == current_user.company_id)
         .order_by(models.LeaveWithdrawal.requested_at.desc()))
    if visible is not None:
        q = q.where(models.LeaveWithdrawal.employee_id.in_(visible or [uuid.UUID(int=0)]))
    if status:
        q = q.where(models.LeaveWithdrawal.status == status)
    rows = db.scalars(q.offset(offset).limit(limit)).all()
    names = crud.employee_display_names_bulk(db, {r.employee_id for r in rows} | {r.approver_id for r in rows if r.approver_id})
    return [_out(db, w, current_user, names) for w in rows]


@router.patch("/leave-withdrawals/{withdrawal_id}", response_model=LeaveWithdrawalOut)
def decide_leave_withdrawal(
    withdrawal_id: uuid.UUID,
    payload: LeaveWithdrawalDecision,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Approve -> the leave is withdrawn and its approved days restored once;
    Reject -> the leave stays exactly as it was."""
    w = db.scalar(select(models.LeaveWithdrawal).where(models.LeaveWithdrawal.id == withdrawal_id).with_for_update())
    if w is None or w.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Withdrawal request not found")
    if w.status != "pending":
        raise HTTPException(status_code=409, detail=f"This withdrawal was already {w.status}.")
    if current_user.employee_id is not None and current_user.employee_id == w.employee_id:
        raise HTTPException(status_code=403, detail="You can't approve or reject your own request")
    if not _can_decide(db, current_user, w):
        raise HTTPException(status_code=403, detail="Only this employee's assigned Reporting Manager can act on this request")
    leave = crud.get_leave_request_for_update(db, w.leave_request_id)
    if leave is None:
        raise HTTPException(status_code=404, detail="Leave request not found")
    group = _group(db, leave, lock=True)
    effective = crud.decide_configurable_request(
        db, current_user, w.employee_id, DOCTYPE, w.id, payload.status, payload.decision_notes)
    now = datetime.datetime.now(datetime.timezone.utc)
    if effective == "pending":  # more steps in a configured chain
        db.commit()
        return _out(db, w, current_user)
    w.status, w.approver_id, w.decision_notes, w.decided_at = effective, current_user.employee_id, payload.decision_notes, now
    if effective == "approved":
        _apply_withdrawal(db, w, group, current_user)
    crud.create_audit_log(db, w.company_id, current_user.id, effective, DOCTYPE, w.id,
                          changes={"leave_request_id": str(w.leave_request_id), "restored_days": str(w.restored_days)})
    db.commit()
    db.refresh(w)
    _notify_decided(db, w, db.get(models.Employee, w.employee_id),
                    crud.employee_display_name(db, current_user.employee_id) if current_user.employee_id else current_user.email,
                    current_user.employee_id)
    db.commit()
    return _out(db, w, current_user)


@router.get("/reports/leave-withdrawals")
def leave_withdrawal_report(
    from_date: datetime.date | None = Query(default=None),
    to_date: datetime.date | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(pending|approved|rejected)$"),
    employee_id: uuid.UUID | None = Query(default=None),
    manager_id: uuid.UUID | None = Query(default=None),
    leave_type_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Reports & Analytics > Leave Withdrawals: every withdrawal request
    (the full history -- one leave can have several) whose leave dates or
    request date fall in the period, with filters."""
    visible = crud.get_visible_employee_ids_for_requests(db, current_user, "leave_request")
    q = (select(models.LeaveWithdrawal, models.LeaveRequest, models.Employee)
         .join(models.LeaveRequest, models.LeaveRequest.id == models.LeaveWithdrawal.leave_request_id)
         .join(models.Employee, models.Employee.id == models.LeaveWithdrawal.employee_id)
         .where(models.LeaveWithdrawal.company_id == current_user.company_id)
         .order_by(models.LeaveWithdrawal.requested_at.desc()))
    if visible is not None:
        q = q.where(models.LeaveWithdrawal.employee_id.in_(visible or [uuid.UUID(int=0)]))
    if from_date:
        q = q.where(models.LeaveRequest.to_date >= from_date)
    if to_date:
        q = q.where(models.LeaveRequest.from_date <= to_date)
    if status:
        q = q.where(models.LeaveWithdrawal.status == status)
    if employee_id:
        q = q.where(models.LeaveWithdrawal.employee_id == employee_id)
    if manager_id:
        q = q.where(or_(models.Employee.reporting_manager_id == manager_id,
                        models.Employee.dotted_line_manager_id == manager_id))
    if leave_type_id:
        q = q.where(models.LeaveRequest.leave_type_id == leave_type_id)
    rows = db.execute(q.limit(5000)).all()
    names = crud.employee_display_names_bulk(db, {w.employee_id for w, _l, _e in rows}
                                             | {w.approver_id for w, _l, _e in rows if w.approver_id}
                                             | {e.reporting_manager_id for _w, _l, e in rows if e.reporting_manager_id})
    out = [_out(db, w, None, names) for w, _l, _e in rows]
    history: dict = {}
    for x in out:
        history.setdefault(str(x.leave_request_id), 0)
        history[str(x.leave_request_id)] += 1
    return {
        "rows": [{**x.model_dump(mode="json"), "attempt_count": history[str(x.leave_request_id)]} for x in out],
        "total": len(out),
        "pending": sum(1 for x in out if x.status == "pending"),
        "approved": sum(1 for x in out if x.status == "approved"),
        "rejected": sum(1 for x in out if x.status == "rejected"),
        "restored_days": round(sum(x.restored_days or 0 for x in out), 1),
    }
