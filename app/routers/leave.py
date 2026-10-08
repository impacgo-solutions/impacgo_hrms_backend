import datetime as _dt
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import attendance_rules, crud, leave_policy, models, schemas
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled
from ..storage import save_uploaded_file

router = APIRouter(
    prefix="/api", tags=["leave"],
    dependencies=[Depends(require_module_enabled("leave"))],
)

_ENTITY_TYPE = "leave_request"


def require_leave_admin(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
) -> models.User:
    """H-10: leave types and allocations are configured only by a leave
    administrator -- Organization Owner, or Admin on the Leave Approval
    column (role, Employee Permissions or Module Access). Edit (e.g. the
    Manager template) is no longer enough."""
    if not leave_policy.is_leave_admin(db, current_user):
        raise HTTPException(
            status_code=403,
            detail="Only a leave administrator (Owner, or Admin on Leave Approval) can configure leave",
        )
    return current_user


def _leave_type_or_422(db: Session, company_id: uuid.UUID, name: str | None, leave_type_id: uuid.UUID | None = None,
                       code: str | None = None) -> models.LeaveType:
    try:
        return leave_policy.require_leave_type(db, company_id, name, leave_type_id, code)
    except leave_policy.LeaveTypeNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _requested_days_or_422(db: Session, company_id: uuid.UUID, employee: models.Employee, payload) -> float:
    try:
        return leave_policy.validate_requested_days(
            db, company_id, employee, payload.from_date, payload.to_date, payload.is_half_day, payload.days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _certificate_names(db: Session, leave_request_id: uuid.UUID) -> list[str]:
    return [
        a.file_name
        for a in crud.list_attachments_for_entity(db, _ENTITY_TYPE, leave_request_id)
    ]


@router.get("/leave-types", response_model=list[schemas.LeaveTypeOut])
def list_leave_types(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return crud.list_leave_types(db, current_user.company_id)


@router.post(
    "/leave-types",
    response_model=schemas.LeaveTypeOut,
    status_code=201,
)
def create_leave_type(
    payload: schemas.LeaveTypeCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_leave_admin),
):
    code = (payload.code or "").strip().upper() or leave_policy.unique_code(db, current_user.company_id, payload.name)
    try:
        leave_type = crud.create_leave_type(
            db,
            current_user.company_id,
            " ".join(payload.name.split()),
            code,
            is_paid=payload.is_paid,
            max_days_per_year=payload.max_days_per_year,
            carry_forward=payload.carry_forward,
            is_encashable=payload.is_encashable,
            applicable_employment_types=payload.applicable_employment_types,
        )
        crud.create_audit_log(db, current_user.company_id, current_user.id, "create", "leave_type", leave_type.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(leave_type)
    return leave_type


def _fmt_minutes_of_day(total_minutes: int) -> str:
    total_minutes %= 24 * 60
    hour24, minute = divmod(total_minutes, 60)
    period = "AM" if hour24 < 12 else "PM"
    hour12 = hour24 % 12 or 12
    return f"{hour12}:{minute:02d} {period}"


def _half_day_time_range(db: Session, lr: models.LeaveRequest) -> str | None:
    """"09:00 AM - 01:00 PM" style descriptive range for a half-day
    request, computed from the REQUESTER'S OWN shift assignment active on
    lr.from_date -- never a fixed/hardcoded time, and computed once here
    so Employee/Manager/HR all see the identical value regardless of who's
    viewing. None (never a guessed default) when the request isn't
    half-day, the employee has no shift assignment, or the shift is a
    night shift (a simple start/end midpoint isn't a meaningful "morning
    vs afternoon" split across a shift that crosses midnight)."""
    if not lr.is_half_day or lr.half_day_period is None:
        return None
    shift = crud._get_active_shift(db, lr.employee_id, lr.from_date)
    if shift is None or shift.is_night:
        return None
    start_minutes = shift.start_time.hour * 60 + shift.start_time.minute
    end_minutes = shift.end_time.hour * 60 + shift.end_time.minute
    if end_minutes <= start_minutes:
        return None
    midpoint = (start_minutes + end_minutes) // 2
    if lr.half_day_period == "morning":
        return f"{_fmt_minutes_of_day(start_minutes)} - {_fmt_minutes_of_day(midpoint)}"
    return f"{_fmt_minutes_of_day(midpoint)} - {_fmt_minutes_of_day(end_minutes)}"


def _leave_request_out(
    db: Session,
    lr: models.LeaveRequest,
    names: dict[uuid.UUID, str] | None = None,
    certs: dict[uuid.UUID, list[str]] | None = None,
) -> schemas.LeaveRequestOut:
    """names/certs are pre-resolved bulk lookups (see list_leave_requests)
    for a whole result set at once -- performance only, never passed by the
    single-row create/update call sites below, which fall back to the
    original one-row-at-a-time helpers (fine at N=1)."""
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    def cert_names() -> list[str]:
        if certs is not None:
            return certs.get(lr.id, [])
        return _certificate_names(db, lr.id)

    return schemas.LeaveRequestOut(
        id=lr.id,
        employee_id=lr.employee_id,
        employee_name=name(lr.employee_id),
        leave_type_name=lr.leave_type.name,
        from_date=lr.from_date,
        to_date=lr.to_date,
        days=float(lr.days),
        reason=lr.reason,
        is_half_day=lr.is_half_day,
        half_day_period=lr.half_day_period,
        half_day_time_range=_half_day_time_range(db, lr),
        status=lr.status,
        certificate_file_names=cert_names(),
        approver_id=lr.approver_id,
        approver_name=name(lr.approver_id),
        decision_notes=lr.decision_notes,
        decided_at=lr.approved_at,
        applied_date=lr.created_at,
        reporting_manager_name=name(lr.employee.reporting_manager_id),
        decided_by_manager_type=crud.decided_by_manager_type(lr.employee, lr.approver_id),
        linked_request_id=lr.linked_leave_request_id,
    )


@router.get("/leave-requests", response_model=list[schemas.LeaveRequestOut])
def list_leave_requests(
    status: str | None = None,
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # An org-wide leave administrator decides any leave (N-07 / M-08), so
    # they see every leave request of the company.
    visible_ids = None if leave_policy.is_leave_admin(db, current_user) \
        else crud.get_visible_employee_ids_for_requests(db, current_user, "leave_request")
    rows = crud.list_leave_requests(
        db, current_user.company_id, status=status, employee_ids=visible_ids,
        limit=limit, offset=offset,
    )
    # Bulk-resolve every name and certificate list this whole page needs in
    # 2 queries total instead of up to 4 per row (see
    # crud.employee_display_names_bulk's docstring for why this matters at
    # real employee/request counts).
    name_ids: set[uuid.UUID] = set()
    for lr in rows:
        name_ids.add(lr.employee_id)
        if lr.approver_id:
            name_ids.add(lr.approver_id)
        if lr.employee.reporting_manager_id:
            name_ids.add(lr.employee.reporting_manager_id)
    names = crud.employee_display_names_bulk(db, name_ids)
    certs = crud.attachment_file_names_bulk(db, _ENTITY_TYPE, [lr.id for lr in rows])
    # PERF-09 / P10: half-day shift lookups for the whole page in 2 queries.
    crud.prefetch_active_shifts(
        db, [(lr.employee_id, lr.from_date) for lr in rows if lr.is_half_day and lr.half_day_period is not None]
    )
    return [_leave_request_out(db, lr, names=names, certs=certs) for lr in rows]


@router.get("/leave-requests/working-days")
def preview_leave_days(
    from_date: _dt.date,
    to_date: _dt.date,
    is_half_day: bool = False,
    employee_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
) -> dict:
    """H-06: the day count the server will charge for this range (weekly
    offs and holidays of the employee's branch excluded) -- the Apply Leave
    form shows it instead of counting calendar days itself."""
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to_date cannot be before from_date")
    if (to_date - from_date).days > 366:
        raise HTTPException(status_code=422, detail="A leave request can't span more than a year")
    target = employee_id or current_user.employee_id
    employee = db.get(models.Employee, target) if target else None
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if employee.id != current_user.employee_id:
        visible = crud.get_visible_employee_ids_for_requests(db, current_user, "leave_request")
        if visible is not None and employee.id not in visible and not leave_policy.is_leave_admin(db, current_user):
            raise HTTPException(status_code=404, detail="Employee not found")
    dates = leave_policy.working_dates(db, current_user.company_id, employee.branch_id, from_date, to_date)
    days = (0.5 if dates else 0.0) if is_half_day else float(len(dates))
    return {"days": days, "working_dates": [d.isoformat() for d in dates],
            "calendar_days": (to_date - from_date).days + 1}


@router.post(
    "/leave-requests",
    response_model=schemas.LeaveRequestOut,
    status_code=201,
)
def create_leave_request(
    payload: schemas.LeaveRequestCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "leave_approval", "leave_request", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    # WF-03: no Reporting Manager (e.g. the CEO) no longer blocks the
    # request -- crud.notify_new_request routes it to the fallback approvers
    # (Owner / System Settings RBAC holders) who can decide it via
    # can_decide_request / approval_engine.can_act, never the requester.
    #
    # WF-01: serialize concurrent submissions for this employee so the
    # overlap check below and the insert are atomic (5 parallel identical
    # POSTs used to create 5 requests). Held until this transaction commits.
    # H-09: an existing leave type only (422 otherwise).
    requested_type = _leave_type_or_422(db, current_user.company_id, payload.leave_type_name, payload.leave_type_id)
    if payload.overflow_leave_type_name:
        _leave_type_or_422(db, current_user.company_id, payload.overflow_leave_type_name)
    # H-06: the server's own working-day count, never the client's.
    requested_days = _requested_days_or_422(db, current_user.company_id, employee, payload)
    crud._advisory_lock(db, "leave_request", str(payload.employee_id))
    existing = crud.get_overlapping_leave_request(
        db, payload.employee_id, payload.from_date, payload.to_date
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="You already have a leave request that overlaps these dates.",
        )
    try:
        leave_type, primary_days, overflow_legs = crud.resolve_leave_request_split(
            db, current_user.company_id, payload.employee_id,
            requested_type.name, requested_days,
            overflow_leave_type_name=payload.overflow_leave_type_name,
            on_date=payload.from_date, leave_type_id=requested_type.id,
        )
    except leave_policy.LeaveTypeNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    leave_request = crud.create_leave_request(
        db,
        current_user.company_id,
        payload.employee_id,
        leave_type.name,
        payload.from_date,
        payload.to_date,
        primary_days,
        payload.reason,
        payload.is_half_day,
        half_day_period=payload.half_day_period,
    )
    request_type_label = "Leave Request"
    if overflow_legs:
        # Requested days exceeded balance -- the shortfall becomes one or
        # two more linked requests (the employee's chosen other leave type,
        # then LOP for whatever that still doesn't cover), all sharing
        # linked_leave_request_id as a group id (the primary leg points at
        # its own id; every other leg points at the same value), so
        # approving/rejecting any one of them (routers/leave.py's PATCH
        # handler) decides the whole group together.
        leave_request.linked_leave_request_id = leave_request.id
        leg_names = [leave_type.name]
        legs: list[models.LeaveRequest] = []
        for leg_type, leg_days in overflow_legs:
            leg_request = crud.create_leave_request(
                db,
                current_user.company_id,
                payload.employee_id,
                leg_type.name,
                payload.from_date,
                payload.to_date,
                leg_days,
                payload.reason,
                payload.is_half_day,
                half_day_period=payload.half_day_period,
            )
            leg_request.linked_leave_request_id = leave_request.id
            crud.create_audit_log(
                db, current_user.company_id, current_user.id, "create", "leave_request", leg_request.id
            )
            leg_names.append(leg_type.name)
            legs.append(leg_request)
        request_type_label = f"Leave Request ({' + '.join(leg_names)})"
    else:
        legs = []

    crud.create_audit_log(db, current_user.company_id, current_user.id, "create", "leave_request", leave_request.id)
    db.flush()
    # M-08: nobody but the requester could ever decide this (e.g. the
    # Owner with no other approver, fallback approver or leave admin) --
    # approve it automatically, with an 'auto_approve' audit entry.
    if not leave_policy.has_any_approver(db, current_user.company_id, employee, "leave_request", leave_request.id):
        leave_policy.auto_approve(db, [leave_request, *legs], current_user)
        db.commit()
        db.refresh(leave_request)
        crud.create_notification(
            db, current_user.company_id, crud.get_user_id_for_employee(db, employee.id),
            title=f"{request_type_label} approved", body=leave_policy.AUTO_APPROVE_NOTE,
            entity_type=_ENTITY_TYPE, entity_id=leave_request.id,
        )
        db.commit()
        return _leave_request_out(db, leave_request)
    db.commit()
    db.refresh(leave_request)
    crud.notify_new_request(
        db, current_user.company_id, employee, request_type_label, "leave_request", leave_request.id
    )
    db.commit()
    return _leave_request_out(db, leave_request)


@router.patch(
    "/leave-requests/{leave_request_id}",
    response_model=schemas.LeaveRequestOut,
)
def update_leave_request(
    leave_request_id: uuid.UUID,
    payload: schemas.LeaveRequestUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: authorization is solely
    # crud.can_decide_request below (checked once the record is loaded, so
    # it can resolve the requester's actual reporting_manager_id) --
    # deliberately NOT require_permission("leave_approval"), which would
    # hardcode "your ROLE must have Edit/Admin on this RBAC column" as an
    # extra gate on top, blocking a legitimate Reporting Manager whose role
    # (e.g. Team Lead) only has View on this column. Owner and System
    # Settings / RBAC admins still get their usual override inside
    # can_decide_request itself.
    current_user: models.User = Depends(get_current_user),
):
    leave_request = crud.get_leave_request_for_update(db, leave_request_id)
    if leave_request is None:
        raise HTTPException(status_code=404, detail="Leave request not found")
    # Two Reporting Managers: checked before the permission gate below so
    # that a manager whose independent/parallel step has already been
    # superseded by the other manager's decision sees a clear "already
    # decided" message instead of a confusing permission error once
    # can_decide_request_configurable (correctly) stops recognizing them
    # as the current decider.
    old_status = leave_request.status
    if old_status == "cancelled":
        raise HTTPException(
            status_code=409,
            detail="This request was cancelled by the employee and cannot be decided",
        )
    if old_status == "withdrawn":
        raise HTTPException(
            status_code=409,
            detail="This leave was withdrawn and cannot be decided",
        )
    if old_status in ("approved", "rejected"):
        decider = crud.employee_display_name(db, leave_request.approver_id)
        decider_type = crud.decided_by_manager_type(
            leave_request.employee, leave_request.approver_id
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"This request was already {old_status} by {decider}"
                f" ({decider_type}) and cannot be decided again"
                if decider_type
                else f"This request was already {old_status} and cannot be decided again"
            ),
        )
    # A custom role holding either granular leave grant (approve or reject)
    # may decide ANY leave request company-wide, purely additive to the
    # existing reporting-chain rule below -- e.g. a dedicated "Leave
    # Approver" role that isn't anyone's manager. Checked as "can this role
    # act on leave at all" rather than matched to this specific decision's
    # status value, same coarse granularity the reporting-chain check below
    # already has (it doesn't distinguish approved/rejected/sent_back/
    # l1_approved either -- it's about WHO may decide, not which decision).
    # C3: the self-approval guard (and company scoping) applies BEFORE the
    # granular-grant shortcut -- previously a "Leave Approver" could approve
    # their own leave because the shortcut skipped can_decide_request*.
    if current_user.employee_id is not None and current_user.employee_id == leave_request.employee_id:
        raise HTTPException(status_code=403, detail="You can't approve or reject your own request")
    if leave_request.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Leave request not found")
    # N-07: a granular leave approve/reject action ALONE no longer decides
    # anyone's leave -- outside the reporting chain only an org-wide leave
    # administrator (Owner, or Admin on Leave Approval, e.g. HR) may.
    # A configured approval chain (Administration > Approval Workflows)
    # decides exactly WHO acts at each step, so the shortcut only applies
    # to the default (unconfigured) flow.
    org_wide = leave_policy.is_leave_admin(db, current_user) and not crud._has_custom_approval_workflow(
        db, current_user.company_id, "leave_request"
    )
    if not org_wide and not crud.can_decide_request_configurable(
        db, current_user, leave_request.employee_id, "leave_request", leave_request.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's assigned Reporting Manager can act on this request",
        )

    # An auto-split request (see POST /leave-requests) shares
    # linked_leave_request_id as a group id across every leg (the primary
    # leg points at its own id; every other leg points at the same value)
    # -- e.g. requested days exceeded balance, so the shortfall became one
    # or two more legs (the employee's chosen other leave type, then LOP
    # for whatever that still didn't cover). Fetch+lock every leg in the
    # group now (same FOR UPDATE pattern, ordered by id so two concurrent
    # decisions on any two legs of the same group always acquire locks in
    # the same order) so one decision applies to the whole group. A
    # pre-existing request from before this feature shipped simply has no
    # group id -- siblings stays empty and behavior is unchanged.
    siblings: list[models.LeaveRequest] = []
    if leave_request.linked_leave_request_id is not None:
        group_id = leave_request.linked_leave_request_id
        member_ids = sorted(
            {
                r.id
                for r in db.scalars(
                    select(models.LeaveRequest).where(
                        (models.LeaveRequest.linked_leave_request_id == group_id)
                        | (models.LeaveRequest.id == group_id)
                    )
                )
            },
            key=str,
        )
        for member_id in member_ids:
            if member_id == leave_request.id:
                continue
            locked = crud.get_leave_request_for_update(db, member_id)
            if locked is not None:
                siblings.append(locked)
        for sib in siblings:
            if sib.status in ("approved", "rejected"):
                raise HTTPException(
                    status_code=409,
                    detail="One of this request's linked requests was already decided -- "
                    "contact an administrator to reconcile it.",
                )

    effective_status = crud.decide_configurable_request(
        db, current_user, leave_request.employee_id, "leave_request", leave_request.id,
        payload.status, payload.decision_notes,
    )
    if effective_status == "approved":
        # H-07: re-check the balance at final approval -- other requests
        # may have been approved since this one was submitted.
        shortfall = leave_policy.approval_shortfall(
            db, [r for r in (leave_request, *siblings) if r.status != "approved"]
        )
        if shortfall:
            db.rollback()
            raise HTTPException(status_code=409, detail=shortfall)
    decided_at = datetime.now(timezone.utc) if effective_status != "pending" else None
    for req in [leave_request, *siblings]:
        req_old_status = req.status
        req.status = effective_status
        req.approver_id = current_user.employee_id
        req.decision_notes = payload.decision_notes
        if decided_at is not None:
            req.approved_at = decided_at
        if req_old_status != "approved" and effective_status == "approved":
            crud.adjust_leave_allocation_used(
                db, req.employee_id, req.leave_type_id, float(req.days),
                on_date=req.from_date,
            )
            # Approved leave explains those days: drop any auto-absent
            # marks now, not at the next nightly pass (payroll LOP).
            attendance_rules.clear_auto_absences(db, req.employee_id, req.from_date, req.to_date)
        elif req_old_status == "approved" and effective_status != "approved":
            crud.adjust_leave_allocation_used(
                db, req.employee_id, req.leave_type_id, -float(req.days),
                on_date=req.from_date,
            )
        crud.create_audit_log(
            db, req.company_id, current_user.id, payload.status, "leave_request", req.id
        )
    db.commit()
    db.refresh(leave_request)
    if effective_status != "pending":
        request_type_label = "Leave Request"
        if siblings:
            names = [leave_request.leave_type.name] + [s.leave_type.name for s in siblings]
            request_type_label = f"Leave Request ({' + '.join(names)})"
        crud.notify_decision(
            db, leave_request.company_id, leave_request.employee_id, request_type_label,
            effective_status, payload.decision_notes, "leave_request", leave_request.id,
        )
        db.commit()
    return _leave_request_out(db, leave_request)


@router.post(
    "/leave-requests/{leave_request_id}/cancel",
    response_model=schemas.LeaveRequestOut,
)
def cancel_leave_request(
    leave_request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """WF-04: the requester withdraws their own leave request before it is
    decided. Only the employee the request belongs to (403 otherwise), only
    while it is 'pending' or 'sent_back' (409 otherwise). Sets status
    'cancelled' on the request and every linked auto-split leg; nothing is
    reversed because a pending/sent-back request never touched the balance.
    Closes the configurable-workflow ApprovalRequest (if any), marks the
    approvers' unread "new request" notifications read, audit-logs, and
    returns the same LeaveRequestOut row as GET/PATCH."""
    leave_request = crud.get_leave_request_for_update(db, leave_request_id)
    if leave_request is None or leave_request.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Leave request not found")
    if current_user.employee_id is None or current_user.employee_id != leave_request.employee_id:
        raise HTTPException(status_code=403, detail="You can only cancel your own leave requests")
    if leave_request.status not in ("pending", "sent_back"):
        raise HTTPException(
            status_code=409,
            detail=f"Only pending or sent-back requests can be cancelled (this one is {leave_request.status})",
        )
    group = [leave_request]
    if leave_request.linked_leave_request_id is not None:
        group_id = leave_request.linked_leave_request_id
        for member in db.scalars(
            select(models.LeaveRequest)
            .where(
                (models.LeaveRequest.linked_leave_request_id == group_id)
                | (models.LeaveRequest.id == group_id)
            )
            .order_by(models.LeaveRequest.id)
            .with_for_update()
        ):
            if member.id != leave_request.id:
                if member.status in ("approved", "rejected"):
                    raise HTTPException(
                        status_code=409,
                        detail="One of this request's linked requests was already decided -- "
                        "contact an administrator.",
                    )
                group.append(member)
    from .. import approval_engine

    now = datetime.now(timezone.utc)
    for req in group:
        previous = req.status
        req.status = "cancelled"
        approval_engine.close_request(
            db, req.company_id, "leave_request", req.id, current_user,
            status="cancelled", comments="Cancelled by requester",
        )
        for note in db.scalars(
            select(models.Notification).where(
                models.Notification.entity_type == _ENTITY_TYPE,
                models.Notification.entity_id == req.id,
                models.Notification.read_at.is_(None),
                models.Notification.user_id != current_user.id,
            )
        ):
            note.read_at = now
        crud.create_audit_log(
            db, req.company_id, current_user.id, "cancel", "leave_request", req.id,
            changes={"before": previous, "after": "cancelled"},
        )
    db.commit()
    db.refresh(leave_request)
    return _leave_request_out(db, leave_request)


@router.patch(
    "/leave-requests/{leave_request_id}/edit",
    response_model=schemas.LeaveRequestOut,
)
def edit_leave_request(
    leave_request_id: uuid.UUID,
    payload: schemas.LeaveRequestEdit,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """WF-04: the requester edits their own leave request before it is
    decided -- only the employee it belongs to (403), only while 'pending'
    or 'sent_back' (409). Same checks as creating one: no overlap with
    their other live requests (serialized by the same advisory lock) and
    enough balance for the requested type. A request that was auto-split
    across leave types can't be edited in place (409 -- cancel and apply
    again). Editing a sent-back request resubmits it: status back to
    'pending', its approval workflow restarts at the first step and the
    approvers are notified again."""
    leave_request = crud.get_leave_request_for_update(db, leave_request_id)
    if leave_request is None or leave_request.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Leave request not found")
    if current_user.employee_id is None or current_user.employee_id != leave_request.employee_id:
        raise HTTPException(status_code=403, detail="You can only edit your own leave requests")
    if leave_request.status not in ("pending", "sent_back"):
        raise HTTPException(
            status_code=409,
            detail=f"Only pending or sent-back requests can be edited (this one is {leave_request.status})",
        )
    if leave_request.linked_leave_request_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This request was split across leave types -- cancel it and apply again instead.",
        )
    requested_type = _leave_type_or_422(db, current_user.company_id, payload.leave_type_name, payload.leave_type_id)
    requested_days = _requested_days_or_422(db, current_user.company_id, leave_request.employee, payload)
    crud._advisory_lock(db, "leave_request", str(leave_request.employee_id))
    clash = db.scalar(
        select(models.LeaveRequest).where(
            models.LeaveRequest.employee_id == leave_request.employee_id,
            models.LeaveRequest.id != leave_request.id,
            # M-06: withdrawn leave doesn't clash either.
            models.LeaveRequest.status.not_in(leave_policy.NON_BLOCKING_STATUSES),
            models.LeaveRequest.from_date <= payload.to_date,
            models.LeaveRequest.to_date >= payload.from_date,
        ).limit(1)
    )
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail="You already have a leave request that overlaps these dates.",
        )
    try:
        leave_type, primary_days, overflow_legs = crud.resolve_leave_request_split(
            db, current_user.company_id, leave_request.employee_id,
            requested_type.name, requested_days, on_date=payload.from_date,
            leave_type_id=requested_type.id, exclude_request_ids=[leave_request.id],
        )
    except leave_policy.LeaveTypeNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if overflow_legs:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Not enough {leave_type.name} balance for {requested_days:g} day(s). "
                "Cancel this request and apply again to split it across leave types."
            ),
        )
    before = {
        "leave_type": leave_request.leave_type.name if leave_request.leave_type else None,
        "from_date": str(leave_request.from_date), "to_date": str(leave_request.to_date),
        "days": float(leave_request.days), "status": leave_request.status,
    }
    resubmitted = leave_request.status == "sent_back"
    leave_request.leave_type_id = leave_type.id
    leave_request.from_date = payload.from_date
    leave_request.to_date = payload.to_date
    leave_request.days = primary_days
    leave_request.reason = payload.reason
    leave_request.is_half_day = payload.is_half_day
    leave_request.half_day_period = payload.half_day_period if payload.is_half_day else None
    if resubmitted:
        from .. import approval_engine

        leave_request.status = "pending"
        leave_request.approver_id = None
        leave_request.approved_at = None
        approval_engine.resubmit_request(
            db, leave_request.company_id, "leave_request", leave_request.id, current_user,
            comments="Edited and resubmitted by requester",
        )
    crud.create_audit_log(
        db, leave_request.company_id, current_user.id,
        "resubmit" if resubmitted else "update", "leave_request", leave_request.id,
        changes={"before": before, "after": {
            "leave_type": leave_type.name, "from_date": str(payload.from_date),
            "to_date": str(payload.to_date), "days": primary_days, "status": leave_request.status,
        }},
    )
    db.commit()
    db.refresh(leave_request)
    if resubmitted:
        employee = db.get(models.Employee, leave_request.employee_id)
        crud.notify_new_request(
            db, leave_request.company_id, employee, "Leave Request (resubmitted)",
            "leave_request", leave_request.id,
        )
        db.commit()
    return _leave_request_out(db, leave_request)


@router.post(
    "/leave-requests/{leave_request_id}/attachments",
    response_model=schemas.AttachmentOut,
    status_code=201,
)
def upload_leave_request_attachment(  # API-06: sync def -> threadpool (blocking file/DB I/O)
    leave_request_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs the Apply Leave form's optional Medical Certificate upload --
    called after the leave request itself is created, once its id is known."""
    leave_request = crud.get_leave_request(db, leave_request_id)
    if leave_request is None:
        raise HTTPException(status_code=404, detail="Leave request not found")
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    if visible_ids is not None and leave_request.employee_id not in visible_ids:
        raise HTTPException(status_code=404, detail="Leave request not found")
    file_url, size_bytes = save_uploaded_file(
        file, entity_type=_ENTITY_TYPE, entity_id=leave_request_id
    )
    attachment = crud.create_attachment(
        db,
        company_id=leave_request.company_id,
        entity_type=_ENTITY_TYPE,
        entity_id=leave_request_id,
        file_name=file.filename or "file",
        file_url=file_url,
        mime_type=file.content_type,
        size_bytes=size_bytes,
    )
    crud.create_audit_log(
        db, leave_request.company_id, current_user.id, "create", "leave_attachment", attachment.id
    )
    db.commit()
    db.refresh(attachment)
    return schemas.AttachmentOut(
        id=attachment.id,
        file_name=attachment.file_name,
        file_url=attachment.file_url,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
    )


@router.get(
    "/leave-requests/{leave_request_id}/attachments",
    response_model=list[schemas.AttachmentOut],
)
def list_leave_request_attachments(
    leave_request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    leave_request = crud.get_leave_request(db, leave_request_id)
    if leave_request is None:
        raise HTTPException(status_code=404, detail="Leave request not found")
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    if visible_ids is not None and leave_request.employee_id not in visible_ids:
        raise HTTPException(status_code=404, detail="Leave request not found")
    return [
        schemas.AttachmentOut(
            id=a.id,
            file_name=a.file_name,
            file_url=a.file_url,
            mime_type=a.mime_type,
            size_bytes=a.size_bytes,
        )
        for a in crud.list_attachments_for_entity(db, _ENTITY_TYPE, leave_request_id)
    ]


@router.patch("/leave-types/{leave_type_id}", response_model=schemas.LeaveTypeOut)
def update_leave_type(
    leave_type_id: uuid.UUID,
    payload: schemas.LeaveTypeUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_leave_admin),
):
    # Company-scoped: a leave type of another company in the same schema is 404.
    if leave_policy.find_leave_type(db, current_user.company_id, leave_type_id=leave_type_id) is None:
        raise HTTPException(status_code=404, detail="Leave type not found")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("code"):
        changes["code"] = changes["code"].strip().upper()
        clash = leave_policy.find_leave_type(db, current_user.company_id, code=changes["code"])
        if clash is not None and clash.id != leave_type_id:
            raise HTTPException(status_code=409, detail=f"Leave type code '{changes['code']}' already exists")
    if changes.get("name"):
        changes["name"] = " ".join(changes["name"].split())
        clash = leave_policy.find_leave_type(db, current_user.company_id, name=changes["name"])
        if clash is not None and clash.id != leave_type_id:
            raise HTTPException(status_code=409, detail=f"Leave type '{changes['name']}' already exists")
    lt = crud.update_leave_type(db, leave_type_id, **changes)
    if lt is None:
        raise HTTPException(status_code=404, detail="Leave type not found")
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "leave_type", lt.id)
    db.commit()
    db.refresh(lt)
    return lt


@router.delete("/leave-types/{leave_type_id}", status_code=204)
def delete_leave_type(
    leave_type_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_leave_admin),
):
    if leave_policy.find_leave_type(db, current_user.company_id, leave_type_id=leave_type_id) is None:
        raise HTTPException(status_code=404, detail="Leave type not found")
    try:
        found = crud.delete_leave_type(db, leave_type_id)
        if found:
            # Its (unused -- no request references the type) allocations go too.
            for alloc in db.scalars(select(models.LeaveAllocation).where(
                    models.LeaveAllocation.leave_type_id == leave_type_id)):
                db.delete(alloc)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not found:
        raise HTTPException(status_code=404, detail="Leave type not found")
    crud.create_audit_log(db, current_user.company_id, current_user.id, "delete", "leave_type", leave_type_id)
    db.commit()


@router.get("/leave-balances", response_model=list[schemas.LeaveBalanceOut])
def list_leave_balances(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    employee_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Current-fiscal-year allocations the caller may see. remaining =
    allocated + carried forward - used - pending (H-07; may be negative)."""
    visible_ids = None if leave_policy.is_leave_admin(db, current_user) \
        else crud.get_visible_employee_ids_for_docs(db, current_user)
    today = crud.company_today(db, current_user.company_id)
    start, end = leave_policy.fiscal_window(db, current_user.company_id, today)
    LA, LT, FY, E = models.LeaveAllocation, models.LeaveType, models.FiscalYear, models.Employee
    q = (select(LA, LT, E)
         .join(LT, LT.id == LA.leave_type_id)
         .join(E, E.id == LA.employee_id)
         .join(FY, FY.id == LA.fiscal_year_id)
         .where(E.company_id == current_user.company_id, FY.start_date <= today, FY.end_date >= today)
         .order_by(LA.employee_id, LT.name, LA.id))
    if visible_ids is not None:
        q = q.where(LA.employee_id.in_(visible_ids or [uuid.UUID(int=0)]))
    if employee_id is not None:
        q = q.where(LA.employee_id == employee_id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    # Allocations of a leave type the employee's employment type isn't
    # eligible for (contract employees, by default) have no balance.
    rows = [(la, lt) for la, lt, e in db.execute(q).all() if crud.leave_type_applies(e, lt)]
    pending = _pending_by_employee_type(db, {la.employee_id for la, _ in rows}, start, end)
    return [_balance_out(la, lt, pending.get((la.employee_id, la.leave_type_id), 0.0)) for la, lt in rows]


def _pending_by_employee_type(db: Session, employee_ids: set, start: _dt.date, end: _dt.date) -> dict:
    if not employee_ids:
        return {}
    from sqlalchemy import func

    LR = models.LeaveRequest
    return {(eid, tid): float(d or 0) for eid, tid, d in db.execute(
        select(LR.employee_id, LR.leave_type_id, func.sum(LR.days)).where(
            LR.employee_id.in_(employee_ids), LR.status.in_(leave_policy.PENDING_STATUSES),
            LR.from_date >= start, LR.from_date <= end,
        ).group_by(LR.employee_id, LR.leave_type_id)).all()}


def _balance_out(la: models.LeaveAllocation, lt: models.LeaveType, pending: float = 0.0) -> schemas.LeaveBalanceOut:
    allocated, cf, used = float(la.allocated_days or 0), float(la.carried_forward_days or 0), float(la.used_days or 0)
    return schemas.LeaveBalanceOut(
        id=la.id, employee_id=la.employee_id, leave_type_id=la.leave_type_id,
        leave_type_name=lt.name, leave_type_code=lt.code,
        allocated=allocated, used=used, carried_forward=cf, pending=pending,
        remaining=allocated + cf - used - pending,
    )


def _allocation_target_or_error(db: Session, current_user: models.User, employee_id: uuid.UUID) -> models.Employee:
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if current_user.employee_id is not None and employee.id == current_user.employee_id:
        raise HTTPException(status_code=403, detail="You can't allocate leave to yourself")
    return employee


@router.post("/leave-balances", response_model=schemas.LeaveBalanceOut, status_code=201)
def create_leave_balance(
    payload: schemas.LeaveBalanceCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_leave_admin),
):
    """H-10/M-07: leave admin only, never for oneself, employee of the
    caller's company (404), existing leave type (422), one allocation per
    employee / type / fiscal year (409 -- edit the existing one)."""
    employee = _allocation_target_or_error(db, current_user, payload.employee_id)
    leave_type = _leave_type_or_422(
        db, current_user.company_id, payload.leave_type_name, payload.leave_type_id, payload.leave_type_code
    )
    fiscal_year = crud.get_or_create_fiscal_year(db, current_user.company_id)
    crud._advisory_lock(db, "leave_allocation", str(employee.id), str(leave_type.id))
    if db.scalar(select(models.LeaveAllocation.id).where(
            models.LeaveAllocation.employee_id == employee.id,
            models.LeaveAllocation.leave_type_id == leave_type.id,
            models.LeaveAllocation.fiscal_year_id == fiscal_year.id).limit(1)) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{leave_type.name} is already allocated to this employee for {fiscal_year.name} -- edit that allocation instead.",
        )
    alloc = crud.create_leave_allocation(
        db,
        employee_id=employee.id,
        leave_type_id=leave_type.id,
        fiscal_year_id=fiscal_year.id,
        allocated_days=payload.allocated,
    )
    # Leave already approved this year counts against the new allocation.
    alloc.used_days = leave_policy._requested_days_in_window(
        db, employee.id, leave_type.id, ("approved",), fiscal_year.start_date, fiscal_year.end_date)
    db.flush()
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "create", "leave_allocation", alloc.id,
        changes={"employee_id": str(employee.id), "leave_type": leave_type.name, "allocated": payload.allocated},
    )
    db.commit()
    db.refresh(alloc)
    pending = _pending_by_employee_type(db, {employee.id}, fiscal_year.start_date, fiscal_year.end_date)
    return _balance_out(alloc, leave_type, pending.get((employee.id, leave_type.id), 0.0))


@router.patch("/leave-balances/{allocation_id}", response_model=schemas.LeaveBalanceOut)
def update_leave_balance(
    allocation_id: uuid.UUID,
    payload: schemas.LeaveBalanceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_leave_admin),
):
    existing = db.get(models.LeaveAllocation, allocation_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Leave allocation not found")
    _allocation_target_or_error(db, current_user, existing.employee_id)
    before = float(existing.allocated_days or 0)
    alloc = crud.update_leave_allocation(db, allocation_id, payload.allocated)
    if alloc is None:
        raise HTTPException(status_code=404, detail="Leave allocation not found")
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "leave_allocation", alloc.id,
        changes={"before": before, "after": payload.allocated},
    )
    db.commit()
    db.refresh(alloc)
    fy = db.get(models.FiscalYear, alloc.fiscal_year_id)
    pending = _pending_by_employee_type(db, {alloc.employee_id}, fy.start_date, fy.end_date) if fy else {}
    return _balance_out(alloc, alloc.leave_type, pending.get((alloc.employee_id, alloc.leave_type_id), 0.0))
