import uuid
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import attendance_rules, crud, models, schemas
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled, require_permission

router = APIRouter(
    prefix="/api", tags=["attendance"],
    dependencies=[Depends(require_module_enabled("attendance"))],
)

# GET /attendance/records: widest from_date..to_date span (inclusive days)
# -- a full leap year fits.
ATTENDANCE_RANGE_MAX_DAYS = 366


def require_attendance_admin(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
) -> models.User:
    """H-20: shift and holiday setup is Admin level -- the Owner, Admin on
    Team Attendance, or an HR people administrator. A Manager (Edit on Team
    Attendance) may only (re)assign shifts for their own reports."""
    if not attendance_rules.is_attendance_admin(db, current_user):
        raise HTTPException(status_code=403, detail="Only an attendance administrator can set up shifts and holidays")
    return current_user


def _require_assignable(db: Session, current_user: models.User, employee_ids) -> None:
    if attendance_rules.employees_outside_scope(db, current_user, employee_ids):
        raise HTTPException(
            status_code=403,
            detail="You can only assign or change shifts for employees who report to you",
        )


def _parse_hhmm(value: str, label: str):
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label} must be a valid HH:MM time") from exc


def _shift_out(db: Session, shift: models.Shift) -> schemas.ShiftOut:
    start = shift.start_time.strftime("%I:%M %p").lstrip("0")
    end = shift.end_time.strftime("%I:%M %p").lstrip("0")
    return schemas.ShiftOut(
        id=shift.id,
        name=shift.name,
        time=f"{start} - {end}",
        type="Night" if shift.is_night else "Day",
        assigned=crud.count_active_shift_assignments(db, shift.id),
        is_active=shift.is_active,
        start_time=shift.start_time.strftime("%H:%M"),
        end_time=shift.end_time.strftime("%H:%M"),
        break_minutes=shift.break_minutes,
        max_breaks=shift.max_breaks,
        grace_minutes=shift.grace_minutes,
    )


@router.get("/shifts", response_model=list[schemas.ShiftOut])
def list_shifts(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """include_inactive=false is the default (e.g. an Assign-employee
    picker should never offer a deactivated shift); Shift Management's own
    admin table passes include_inactive=true so a deactivated shift stays
    visible with a Reactivate action instead of disappearing."""
    company = db.get(models.Company, current_user.company_id)
    shifts = crud.list_shifts(db, company.id, active_only=not include_inactive)
    return [_shift_out(db, s) for s in shifts]


@router.get("/shifts/mine", response_model=schemas.ShiftOut | None)
def get_my_active_shift(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The caller's own currently-active shift assignment (if any) -- lets
    the Attendance screen gate its Check-In/Check-Out buttons by the
    SAME shift timing crud.clock_in_out actually uses server-side (an
    individual shift assignment now takes priority over the company-wide
    working-hours policy there), instead of always reading the company-
    wide policy regardless of whether this employee has their own shift.
    Returns null for an admin-only account with no employee row, or an
    employee with no active shift assignment."""
    if current_user.employee_id is None:
        return None
    shift = crud._get_active_shift(
        db, current_user.employee_id, crud.company_today(db, current_user.company_id)
    )
    if shift is None:
        return None
    return _shift_out(db, shift)


@router.get("/shifts/current-assignments", response_model=list[schemas.ShiftCurrentAssignmentOut])
def list_current_shift_assignments(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("team_attendance")),
):
    """Which shift every employee is on today (at most one each) and their
    next scheduled change -- the Shift Management checklists show
    "Currently on <shift>" / "Moves to <shift> from <date>" next to each
    employee, so a move (now or scheduled) is visible before saving."""
    today = crud.company_today(db, current_user.company_id)
    rows: dict[uuid.UUID, schemas.ShiftCurrentAssignmentOut] = {}
    for a, s in crud.current_shift_assignments(db, current_user.company_id, today):
        rows[a.employee_id] = schemas.ShiftCurrentAssignmentOut(
            employee_id=a.employee_id, shift_id=s.id, shift_name=s.name, from_date=a.from_date,
        )
    for emp_id, (a, s) in crud.upcoming_shift_assignments(db, current_user.company_id, today).items():
        row = rows.setdefault(emp_id, schemas.ShiftCurrentAssignmentOut(employee_id=emp_id))
        row.upcoming_shift_id, row.upcoming_shift_name, row.upcoming_from_date = s.id, s.name, a.from_date
    return list(rows.values())


@router.get("/shifts/mine/schedule", response_model=schemas.MyShiftScheduleOut)
def get_my_shift_schedule(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The caller's shift today plus the next scheduled change, so the
    Attendance screen can show "From <date>: <shift>" and switch on its own
    once that date begins."""
    if current_user.employee_id is None:
        return schemas.MyShiftScheduleOut()
    today = crud.company_today(db, current_user.company_id)
    current = crud._get_active_shift(db, current_user.employee_id, today)
    nxt = crud.upcoming_shift_assignments(
        db, current_user.company_id, today, employee_id=current_user.employee_id,
    ).get(current_user.employee_id)
    return schemas.MyShiftScheduleOut(
        current=_shift_out(db, current) if current else None,
        upcoming=schemas.ShiftUpcomingOut(shift=_shift_out(db, nxt[1]), effective_date=nxt[0].from_date) if nxt else None,
    )


@router.get("/shifts/assignment-history", response_model=list[schemas.ShiftAssignmentHistoryOut])
def get_shift_assignment_history(
    employee_id: uuid.UUID | None = None,
    shift_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("team_attendance")),
):
    """Complete assignment history (newest effective date first): employee,
    previous shift, new shift, effective from / to, status, and when / by
    whom it was assigned and last changed. Attendance records are separate
    and never rewritten by any of this."""
    today = crud.company_today(db, current_user.company_id)
    rows = crud.shift_assignment_history(db, current_user.company_id, employee_id=employee_id, shift_id=shift_id)
    shift_names = {s.id: s.name for s in crud.list_shifts(db, current_user.company_id, active_only=False)}
    emp_names = crud.employee_display_names_bulk(db, [r.employee_id for r in rows])
    user_ids = {u for r in rows for u in (r.created_by, r.updated_by) if u}
    users = {u.id: u for u in db.scalars(select(models.User).where(models.User.id.in_(user_ids))).all()} if user_ids else {}
    user_emp_names = crud.employee_display_names_bulk(db, [u.employee_id for u in users.values() if u.employee_id])

    def who(uid):
        u = users.get(uid) if uid else None
        if u is None:
            return None
        return user_emp_names.get(u.employee_id) if u.employee_id else (u.email or None)

    def status(r):
        if r.to_date is not None and r.to_date < r.from_date:
            return "cancelled"
        if r.from_date > today:
            return "scheduled"
        if r.to_date is None or r.to_date >= today:
            return "active"
        return "ended"

    return [
        schemas.ShiftAssignmentHistoryOut(
            id=r.id, employee_id=r.employee_id, employee_name=emp_names.get(r.employee_id, "—"),
            shift_id=r.shift_id, shift_name=shift_names.get(r.shift_id, "—"),
            previous_shift_name=shift_names.get(r.previous_shift_id) if r.previous_shift_id else None,
            effective_from=r.from_date, effective_to=r.to_date if (r.to_date is None or r.to_date >= r.from_date) else None,
            status=status(r), assigned_at=r.created_at, assigned_by=who(r.created_by),
            updated_at=r.updated_at, updated_by=who(r.updated_by),
        )
        for r in rows
    ]


def _assignment_start(db: Session, company_id: uuid.UUID, requested: "date | None") -> date:
    """The Effective Date of an assignment change: today (company timezone)
    when not given. A future date is scheduled -- the current shift stays in
    force until then. A past date backdates the assignment: shift lookups
    for those dates use it from then on, but attendance already recorded
    keeps the status it was given (records are never recomputed)."""
    return requested or crud.company_today(db, company_id)


@router.post(
    "/shifts",
    response_model=schemas.ShiftOut,
    status_code=201,
)
def create_shift(
    payload: schemas.ShiftCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_attendance_admin),
):
    company = db.get(models.Company, current_user.company_id)
    start = _parse_hhmm(payload.start_time, "start_time")
    end = _parse_hhmm(payload.end_time, "end_time")
    try:
        crud._check_shift_times(start, end, payload.is_night)  # M-15
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        shift = crud.create_shift(
            db, company.id, payload.name, start, end, payload.is_night,
            break_minutes=payload.break_minutes, max_breaks=payload.max_breaks,
            grace_minutes=payload.grace_minutes,
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "shift", shift.id)
        crud.queue_shift_change_push(db, company.id, shift.id, employee_ids=[])
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(shift)
    return _shift_out(db, shift)


@router.patch(
    "/shifts/{shift_id}",
    response_model=schemas.ShiftOut,
)
def update_shift(
    shift_id: uuid.UUID,
    payload: schemas.ShiftUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_attendance_admin),
):
    """Rename/re-time a shift and/or toggle it active/inactive (Activate /
    Deactivate) -- all through this one PATCH, matching PATCH
    /api/bands/{id}'s shape. get_shift_for_update scopes the fetch by the
    caller's own company_id, so this can't reach another company's shift
    even inside the shared acme schema."""
    company = db.get(models.Company, current_user.company_id)
    shift = crud.get_shift_for_update(db, shift_id, company.id)
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")

    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if "start_time" in updates:
        updates["start_time"] = _parse_hhmm(updates["start_time"], "start_time")
    if "end_time" in updates:
        updates["end_time"] = _parse_hhmm(updates["end_time"], "end_time")
    try:
        crud._check_shift_times(  # M-15
            updates.get("start_time", shift.start_time), updates.get("end_time", shift.end_time),
            bool(updates.get("is_night", shift.is_night)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        crud.update_shift(db, shift, updates)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    crud.create_audit_log(db, company.id, current_user.id, "update", "shift", shift.id)
    crud.queue_shift_change_push(db, company.id, shift.id)
    db.commit()
    db.refresh(shift)
    return _shift_out(db, shift)


@router.delete("/shifts/{shift_id}", status_code=204)
def delete_shift(
    shift_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_attendance_admin),
):
    company = db.get(models.Company, current_user.company_id)
    shift = crud.get_shift_for_update(db, shift_id, company.id)
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")

    try:
        crud.delete_shift(db, shift)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    crud.create_audit_log(db, company.id, current_user.id, "delete", "shift", shift_id)
    crud.queue_shift_change_push(db, company.id, shift_id, employee_ids=[])
    db.commit()


@router.post(
    "/shifts/{shift_id}/assignments",
    status_code=201,
)
def assign_employee_to_shift(
    shift_id: uuid.UUID,
    payload: schemas.ShiftAssignmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("team_attendance")),
):
    company = db.get(models.Company, current_user.company_id)
    shift = crud.get_shift_for_update(db, shift_id, company.id)
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")
    if not shift.is_active:
        raise HTTPException(status_code=409, detail="Cannot assign employees to an inactive shift")
    if crud.employee_ids_outside_company(db, [payload.employee_id], company.id):
        raise HTTPException(status_code=404, detail="Employee not found")
    _require_assignable(db, current_user, [payload.employee_id])  # H-20
    from_date = _assignment_start(db, company.id, payload.from_date)
    if (err := attendance_rules.assignment_start_error(db, company.id, [payload.employee_id], from_date)):
        raise HTTPException(status_code=422, detail=err)  # L-12
    assignment = crud.create_shift_assignment(db, payload.employee_id, shift_id, from_date)
    crud.create_audit_log(db, company.id, current_user.id, "create", "shift_assignment", assignment.id)
    crud.queue_shift_change_push(db, company.id, shift_id, employee_ids=[payload.employee_id])
    db.commit()
    return {"id": str(assignment.id)}


@router.post(
    "/shifts/{shift_id}/assignments/bulk",
    response_model=schemas.ShiftAssignmentBulkResult,
    status_code=201,
)
def bulk_assign_employees_to_shift(
    shift_id: uuid.UUID,
    payload: schemas.ShiftAssignmentBulkCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("team_attendance")),
):
    """Attendance > Shift Management > Assign — the multi-select checkbox
    counterpart of assign_employee_to_shift above. shift_id and every
    employee_id are both re-validated against the caller's own company_id
    (get_shift_for_update / employee_ids_outside_company) so this can't
    reach another company's shift or employee inside the shared acme
    schema, then crud.bulk_create_shift_assignments does the actual inserts
    -- one commit for the whole batch, with same-shift duplicates skipped
    rather than rewritten."""
    company = db.get(models.Company, current_user.company_id)
    shift = crud.get_shift_for_update(db, shift_id, company.id)
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")
    if not shift.is_active:
        raise HTTPException(status_code=409, detail="Cannot assign employees to an inactive shift")

    invalid_ids = crud.employee_ids_outside_company(db, payload.employee_ids, company.id)
    if invalid_ids:
        raise HTTPException(
            status_code=400,
            detail="One or more selected employees do not belong to your company",
        )

    _require_assignable(db, current_user, payload.employee_ids)  # H-20
    from_date = _assignment_start(db, company.id, payload.from_date)
    if (err := attendance_rules.assignment_start_error(db, company.id, payload.employee_ids, from_date)):
        raise HTTPException(status_code=422, detail=err)  # L-12
    result = crud.bulk_create_shift_assignments(db, shift_id, payload.employee_ids, from_date)
    for employee_id, assignment_id in result["assigned"]:
        crud.create_audit_log(
            db, company.id, current_user.id, "create", "shift_assignment", assignment_id
        )
    if result["assigned"]:
        crud.queue_shift_change_push(db, company.id, shift_id, employee_ids=[eid for eid, _ in result["assigned"]])
    db.commit()
    return schemas.ShiftAssignmentBulkResult(
        assigned_employee_ids=[eid for eid, _ in result["assigned"]],
        already_assigned_employee_ids=result["skipped_duplicate"],
    )


@router.get(
    "/shifts/{shift_id}/assignments",
    response_model=list[schemas.ShiftAssignmentOut],
)
def list_shift_assignments(
    shift_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("team_attendance")),
):
    """Shift Management > Manage Employees -- who's currently actively
    assigned to this shift, so the checklist dialog can pre-check them
    before the admin adds/removes anyone. Nothing exposed this before;
    ShiftOut.assigned was always just a count (count_active_shift_
    assignments), never the actual employee list."""
    company = db.get(models.Company, current_user.company_id)
    shift = crud.get_shift_for_update(db, shift_id, company.id)
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")
    rows = crud.list_active_shift_assignments(db, shift_id)
    names = crud.employee_display_names_bulk(db, [r.employee_id for r in rows])
    return [
        schemas.ShiftAssignmentOut(
            employee_id=r.employee_id,
            employee_name=names.get(r.employee_id, "—"),
            from_date=r.from_date,
        )
        for r in rows
    ]


@router.post(
    "/shifts/{shift_id}/assignments/bulk-unassign",
    response_model=schemas.ShiftAssignmentBulkUnassignResult,
)
def bulk_unassign_employees_from_shift(
    shift_id: uuid.UUID,
    payload: schemas.ShiftAssignmentBulkUnassign,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("team_attendance")),
):
    """Shift Management > Manage Employees -- the "uncheck to remove" half
    of the checklist dialog, mirroring bulk_assign_employees_to_shift
    above. Unlike assigning, this is allowed even if the shift has been
    deactivated in the meantime (an admin should always be able to take
    people off a shift, active or not)."""
    company = db.get(models.Company, current_user.company_id)
    shift = crud.get_shift_for_update(db, shift_id, company.id)
    if shift is None:
        raise HTTPException(status_code=404, detail="Shift not found")

    invalid_ids = crud.employee_ids_outside_company(db, payload.employee_ids, company.id)
    if invalid_ids:
        raise HTTPException(
            status_code=400,
            detail="One or more selected employees do not belong to your company",
        )

    _require_assignable(db, current_user, payload.employee_ids)  # H-20
    effective = _assignment_start(db, company.id, payload.effective_date)
    if (err := attendance_rules.assignment_start_error(db, company.id, [], effective)):
        raise HTTPException(status_code=422, detail=err)  # L-12
    result = crud.bulk_end_shift_assignments(db, shift_id, payload.employee_ids, effective)
    for employee_id in result["unassigned"]:
        crud.create_audit_log(
            db, company.id, current_user.id, "update", "shift_assignment", employee_id
        )
    if result["unassigned"]:
        crud.queue_shift_change_push(db, company.id, shift_id, employee_ids=result["unassigned"])
    db.commit()
    return schemas.ShiftAssignmentBulkUnassignResult(
        unassigned_employee_ids=result["unassigned"],
        not_assigned_employee_ids=result["skipped_not_assigned"],
    )


def _holiday_out(h: models.Holiday) -> schemas.HolidayOut:
    return schemas.HolidayOut(
        id=h.id,
        date=h.holiday_date.isoformat(),
        day=h.holiday_date.strftime("%A"),
        name=h.name,
        region=h.branch.name if h.branch else "All Branches",
        is_optional=bool(h.is_optional),
        imported=h.source_import_id is not None,
    )


@router.get("/holidays", response_model=list[schemas.HolidayOut])
def list_holidays(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [_holiday_out(h) for h in crud.list_holidays(db, company.id)]


@router.post(
    "/holidays",
    response_model=schemas.HolidayOut,
    status_code=201,
)
def create_holiday(
    payload: schemas.HolidayCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_attendance_admin),  # H-20
):
    company = db.get(models.Company, current_user.company_id)
    branch = _holiday_branch(db, company.id, payload.branch_name)
    try:
        holiday = crud.create_holiday(
            db,
            company.id,
            payload.holiday_date,
            payload.name,
            branch_id=branch.id if branch else None,
            is_optional=payload.is_optional,
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "holiday", holiday.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(holiday)
    return _holiday_out(holiday)


def _holiday_branch(db: Session, company_id: uuid.UUID, branch_name: str | None) -> "models.Branch | None":
    """The named branch, or None for "All Branches" / blank. M-16: an
    unknown branch is a 422, never silently "All Branches"."""
    name = (branch_name or "").strip()
    if not name or name.lower() in ("all", "all branches"):
        return None
    branch = db.scalar(select(models.Branch).where(
        models.Branch.company_id == company_id, func.lower(models.Branch.name) == name.lower()).limit(1))
    if branch is None:
        raise HTTPException(status_code=422, detail=f"Unknown branch '{name}'")
    return branch


@router.patch("/holidays/{holiday_id}", response_model=schemas.HolidayOut)
def update_holiday(
    holiday_id: uuid.UUID,
    payload: schemas.HolidayUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_attendance_admin),
):
    """Edit a holiday's date, name, branch or optional flag. Leave already
    applied keeps its charged days (the change applies to new requests,
    the attendance absence check and payroll)."""
    holiday = db.scalar(select(models.Holiday).where(
        models.Holiday.id == holiday_id, models.Holiday.company_id == current_user.company_id).with_for_update())
    if holiday is None:
        raise HTTPException(status_code=404, detail="Holiday not found")
    fields = payload.model_fields_set
    new_date = payload.holiday_date if "holiday_date" in fields and payload.holiday_date else holiday.holiday_date
    new_name = " ".join((payload.name or holiday.name).split()) if "name" in fields else holiday.name
    branch_id = holiday.branch_id
    if "branch_name" in fields:
        branch = _holiday_branch(db, current_user.company_id, payload.branch_name)
        branch_id = branch.id if branch else None
    dup = crud.find_duplicate_holiday(db, current_user.company_id, new_date, new_name, branch_id)
    if dup is not None and dup.id != holiday.id:
        raise HTTPException(status_code=409, detail=f"Holiday '{new_name}' on {new_date} already exists for this branch")
    before = {"date": str(holiday.holiday_date), "name": holiday.name,
              "branch_id": str(holiday.branch_id) if holiday.branch_id else None, "optional": bool(holiday.is_optional)}
    holiday.holiday_date, holiday.name, holiday.branch_id = new_date, new_name, branch_id
    if "is_optional" in fields and payload.is_optional is not None:
        holiday.is_optional = payload.is_optional
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "holiday", holiday.id,
                          changes={"before": before})
    db.commit()
    db.refresh(holiday)
    return _holiday_out(holiday)


@router.delete("/holidays/{holiday_id}", status_code=204)
def delete_holiday(
    holiday_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_attendance_admin),
):
    holiday = db.scalar(select(models.Holiday).where(
        models.Holiday.id == holiday_id, models.Holiday.company_id == current_user.company_id))
    if holiday is None:
        raise HTTPException(status_code=404, detail="Holiday not found")
    crud.create_audit_log(db, current_user.company_id, current_user.id, "delete", "holiday", holiday.id,
                          changes={"date": str(holiday.holiday_date), "name": holiday.name})
    db.delete(holiday)
    db.commit()


@router.get(
    "/attendance/leave-gate",
    response_model=schemas.AttendanceLeaveGateOut | None,
)
def get_my_attendance_leave_gate(
    on_date: date | None = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The caller's own leave-based Check-In/Check-Out restriction (if any)
    for [on_date] (today by default) -- lets the Attendance screen disable
    the buttons and show the reason up front. See crud.get_blocking_leave_
    window's docstring for exactly what this does and doesn't block."""
    if current_user.employee_id is None:
        return None
    target_date = on_date or crud.company_today(db, current_user.company_id)
    block = crud.get_blocking_leave_window(db, current_user.employee_id, target_date)
    if block is None:
        return None
    return schemas.AttendanceLeaveGateOut(
        full_day=block["full_day"],
        start=block["start"].strftime("%H:%M") if block["start"] else None,
        end=block["end"].strftime("%H:%M") if block["end"] else None,
        reason=block["reason"],
    )


@router.post(
    "/attendance/clock",
    response_model=schemas.AttendanceRecordOut,
)
def clock_in_out(
    payload: schemas.AttendanceClockRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "team_attendance", "attendance_record", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only clock in/out for yourself")
    try:
        record = crud.clock_in_out(
            db, company.id, payload.employee_id, crud.company_today(db, company.id),
            datetime.now(timezone.utc),
            payload.action, work_mode=payload.work_mode,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "update", "attendance_record", record.id)
    # Live Daily Attendance: open screens re-read their list (sent on commit).
    crud.queue_attendance_change_push(db, company.id, record.employee_id, record.attendance_date)
    db.commit()
    db.refresh(record)
    return crud.serialize_attendance_record(record)


@router.get(
    "/attendance/breaks/mine",
    response_model=schemas.BreakStatusOut,
)
def get_my_break_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The caller's own live break state for today, resolved entirely from
    the backend (see crud.get_break_status) -- the sole source of truth
    the Attendance screen's countdown timer hydrates from on load, so a
    refresh, logout/login, or switching devices always shows the real
    in-progress break (or lack of one), never stale local-only state."""
    if current_user.employee_id is None:
        return schemas.BreakStatusOut(
            has_attendance_record=False, checked_out=False, in_progress=False,
            breaks_taken=0, max_breaks=0, total_break_minutes_used=0,
            allowed_break_minutes=0, remaining_break_minutes=0,
        )
    status = crud.get_break_status(
        db, current_user.employee_id, crud.company_today(db, current_user.company_id)
    )
    return schemas.BreakStatusOut(**status)


@router.post(
    "/attendance/breaks/start",
    response_model=schemas.BreakStatusOut,
)
def start_break(
    payload: schemas.BreakActionRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "team_attendance", "attendance_record", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only start a break for yourself")
    today = crud.company_today(db, company.id)
    try:
        brk = crud.start_break(db, payload.employee_id, today, datetime.now(timezone.utc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # API-01: document_id is the break record, not the employee.
    crud.create_audit_log(db, company.id, current_user.id, "update", "break_record", brk.id)
    db.commit()
    return schemas.BreakStatusOut(**crud.get_break_status(db, payload.employee_id, today))


@router.post(
    "/attendance/breaks/end",
    response_model=schemas.BreakStatusOut,
)
def end_break(
    payload: schemas.BreakActionRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "team_attendance", "attendance_record", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only end a break for yourself")
    today = crud.company_today(db, company.id)
    try:
        brk = crud.end_break(db, payload.employee_id, today, datetime.now(timezone.utc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # API-01: document_id is the break record, not the employee.
    crud.create_audit_log(db, company.id, current_user.id, "update", "break_record", brk.id)
    db.commit()
    return schemas.BreakStatusOut(**crud.get_break_status(db, payload.employee_id, today))


@router.patch(
    "/attendance/records/{record_id}/work-mode",
    response_model=schemas.AttendanceRecordOut,
)
def update_attendance_record_work_mode(
    record_id: uuid.UUID,
    payload: schemas.AttendanceWorkModeUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Lets an employee correct/set the manually-selected Work Mode (WFO /
    WFH / Client Site) on one of their own existing attendance records, or
    lets anyone with Team Attendance edit rights do it for an employee they
    manage -- same self-or-permitted rule clock_in_out and regularization
    creation already use. Never touches check-in/out, hours, or status."""
    company = db.get(models.Company, current_user.company_id)
    record = crud.get_attendance_record_for_update(db, record_id, company.id)
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    if not crud.can_submit_self_service_request(
        db, current_user, record.employee_id, "team_attendance", "attendance_record", "update"
    ):
        raise HTTPException(
            status_code=403,
            detail="You can only update your own Work Mode",
        )
    record.work_mode = payload.work_mode
    crud.create_audit_log(db, company.id, current_user.id, "update", "attendance_record", record.id)
    db.commit()
    db.refresh(record)
    return crud.serialize_attendance_record(record)


def _as_company_time(db: Session, company_id: uuid.UUID, t: datetime | None) -> datetime | None:
    """A naive datetime is company local time."""
    if t is None or t.tzinfo is not None:
        return t
    return t.replace(tzinfo=crud.company_tzinfo(db, company_id))


@router.patch("/attendance/records/{record_id}", response_model=schemas.AttendanceRecordOut)
def edit_attendance_record(
    record_id: uuid.UUID,
    payload: schemas.AttendanceRecordEdit,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Edit Attendance: a reporting manager corrects the attendance of an
    employee in their reporting line (attendance admins: anyone in the
    company) -- date, check-in, check-out, work mode and status. Never one's
    own records (that is a regularization request). Every change is kept in
    the record's edit history (core_audit_logs: who, when, before -> after,
    reason), the employee is notified, and open Attendance screens update
    live."""
    company_id = current_user.company_id
    record = crud.get_attendance_record_for_update(db, record_id, company_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    is_own = current_user.employee_id is not None and record.employee_id == current_user.employee_id
    if is_own and not crud.can_edit_own_attendance(db, current_user):
        raise HTTPException(status_code=403, detail="You can't edit your own attendance -- request a regularization instead.")
    if not crud.can_edit_attendance(db, current_user, record.employee_id):
        raise HTTPException(status_code=403, detail="You can only edit the attendance of employees in your reporting line.")

    fields = payload.model_fields_set
    old_date = record.attendance_date
    new_date = payload.attendance_date or old_date
    today = crud.company_today(db, company_id)
    if new_date > today:
        raise HTTPException(status_code=400, detail="Attendance can't be dated in the future.")
    if new_date != old_date:
        clash = db.scalar(select(models.AttendanceRecord.id).where(
            models.AttendanceRecord.employee_id == record.employee_id,
            models.AttendanceRecord.attendance_date == new_date,
            models.AttendanceRecord.id != record.id,
        ))
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=f"This employee already has an attendance record on {new_date.strftime('%d %b %Y')}.",
            )
    # Moving the date without new times moves the punches with it.
    shift_by = new_date - old_date
    new_in = (_as_company_time(db, company_id, payload.check_in) if "check_in" in fields
              else (record.check_in + shift_by if record.check_in else None))
    new_out = (_as_company_time(db, company_id, payload.check_out) if "check_out" in fields
               else (record.check_out + shift_by if record.check_out else None))
    error = attendance_rules.regularization_time_error(
        db, company_id, record.employee_id, new_date, new_in, new_out, None)
    if error:
        raise HTTPException(status_code=400, detail=error.replace("corrected ", ""))

    auto_status, arrival = crud.attendance_status_for(db, company_id, record.employee_id, new_date, new_in, new_out)
    punches_changed = (new_date, new_in, new_out) != (old_date, record.check_in, record.check_out)
    if "status" not in fields and not punches_changed:
        # Only e.g. the work mode changed: keep the status (also one set by
        # hand earlier).
        status, arrival = record.status, record.arrival_status
    else:
        status = (payload.status or "auto").strip().lower()
        if status == "auto":
            status = auto_status
        elif status not in crud.ATTENDANCE_EDIT_STATUSES:
            raise HTTPException(status_code=422, detail=f"Unknown attendance status: {payload.status}.")

    before = crud.attendance_edit_snapshot(record)
    record.attendance_date = new_date
    record.check_in = new_in
    record.check_out = new_out
    record.work_hours = (round((new_out - new_in).total_seconds() / 3600, 2)
                         if new_in is not None and new_out is not None else None)
    if "work_mode" in fields:
        record.work_mode = (payload.work_mode or "").strip() or None
    record.status = status
    record.arrival_status = arrival
    db.flush()
    if punches_changed:
        # The edited times ARE the day now: one session matching them.
        crud.reset_attendance_sessions(db, record)
    after = crud.attendance_edit_snapshot(record)
    changed = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if not changed:
        db.rollback()
        record = crud.get_attendance_record_for_update(db, record_id, company_id)
        return {**crud.serialize_attendance_record(record), "can_edit": True}
    # An edited day is a real record now, never an auto-absent placeholder
    # the absence job may clear.
    record.source = crud.ATTENDANCE_EDIT_SOURCE
    reason = payload.reason.strip()
    crud.create_audit_log(
        db, company_id, current_user.id, "update", "attendance_record", record.id,
        changes={"source": "attendance_edit", "reason": reason, "fields": changed},
    )
    # Tell the employee (bell + live push) -- not a manager editing their own.
    employee_user = crud.get_core_user_by_employee_id(db, record.employee_id)
    if employee_user is not None and not is_own:
        editor = current_user.full_name or current_user.email
        crud.create_notification(
            db, company_id, employee_user.id, "Attendance updated",
            f"{editor} updated your attendance for {new_date.strftime('%d %b %Y')}. Reason: {reason}",
            entity_type="attendance_record", entity_id=record.id,
        )
    for day in {old_date, new_date}:
        crud.queue_attendance_change_push(db, company_id, record.employee_id, day)
    db.commit()
    db.refresh(record)
    return {**crud.serialize_attendance_record(record), "can_edit": True}


@router.get("/attendance/hours-summary", response_model=schemas.AttendanceHoursSummaryOut)
def attendance_hours_summary(
    from_date: date,
    to_date: date,
    employee_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Days worked, total / net hours and Avg Hours / Day over a date range
    -- the caller's own (employee_id = self), one visible employee, or (no
    employee_id) everyone the caller may see. Same visibility as the
    attendance list."""
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="To date can't be before From date.")
    if (to_date - from_date).days > 366:
        raise HTTPException(status_code=422, detail="The date range can't span more than a year.")
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    if employee_id is not None:
        allowed = employee_id == current_user.employee_id or visible_ids is None or employee_id in visible_ids
        ids = [employee_id] if allowed else []
    else:
        ids = None if visible_ids is None else list(visible_ids)
    return crud.attendance_hours_summary(db, current_user.company_id, ids, from_date, to_date)


@router.get("/attendance/records/{record_id}/history", response_model=list[schemas.AttendanceEditHistoryOut])
def attendance_record_history(
    record_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Edit Attendance history of one record, newest first: who changed it,
    when, why, and each field before -> after. Visible to whoever may see
    the record (the employee, their managers, attendance admins)."""
    company_id = current_user.company_id
    record = crud.get_attendance_record_for_update(db, record_id, company_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    visible = crud.get_visible_employee_ids_for_docs(db, current_user)
    if not (record.employee_id == current_user.employee_id or visible is None
            or record.employee_id in visible or crud.can_edit_attendance(db, current_user, record.employee_id)):
        raise HTTPException(status_code=404, detail="Attendance record not found")
    logs = db.scalars(
        select(models.AuditLog).where(
            models.AuditLog.company_id == company_id,
            models.AuditLog.doctype == "attendance_record",
            models.AuditLog.document_id == record.id,
        ).order_by(models.AuditLog.created_at.desc())
    ).all()
    out = []
    names: dict = {}
    for log in logs:
        changes = log.changes or {}
        if not isinstance(changes, dict) or changes.get("source") != "attendance_edit":
            continue
        if log.user_id not in names:
            u = db.get(models.User, log.user_id) if log.user_id else None
            names[log.user_id] = (u.full_name or u.email) if u else "—"
        out.append(schemas.AttendanceEditHistoryOut(
            at=log.created_at, edited_by=names[log.user_id], reason=changes.get("reason"),
            changes=changes.get("fields") or {},
        ))
    return out


@router.get("/attendance/records", response_model=schemas.AttendanceRecordPage)
def list_attendance_records(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),  # API-05: was unbounded (negative accepted)
    # F7: one employee's records (e.g. the caller's own, for Clock In/Out
    # state) -- still intersected with the caller's visibility.
    employee_id: uuid.UUID | None = None,
    # Drill-down from a dashboard / attendance-report figure: only that
    # date range (inclusive), optionally active employees only -- the same
    # rows crud.dashboard_attendance_summary counts.
    from_date: date | None = None,
    to_date: date | None = None,
    active_only: bool = False,
    # Daily Attendance column sorting (crud.ATTENDANCE_SORT_KEYS); omitted =
    # the default newest-first order. sort_dir defaults to asc when a
    # sort_by is given, desc otherwise.
    sort_by: str | None = None,
    sort_dir: Literal["asc", "desc"] | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if sort_by is not None and sort_by not in crud.ATTENDANCE_SORT_KEYS:
        raise HTTPException(
            status_code=422,
            detail=f"sort_by must be one of: {', '.join(crud.ATTENDANCE_SORT_KEYS)}.",
        )
    # Daily Attendance "Custom Range": an inverted or over-a-year range is
    # rejected rather than answered with an empty / unbounded page. A lone
    # from_date or to_date (dashboard drill-downs) is unaffected.
    if from_date is not None and to_date is not None:
        if from_date > to_date:
            raise HTTPException(status_code=422, detail="from_date must be on or before to_date.")
        if (to_date - from_date).days + 1 > ATTENDANCE_RANGE_MAX_DAYS:
            raise HTTPException(
                status_code=422,
                detail=f"The date range can't span more than {ATTENDANCE_RANGE_MAX_DAYS} days.",
            )
    company = db.get(models.Company, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    if employee_id is not None:
        allowed = employee_id == current_user.employee_id or visible_ids is None or employee_id in visible_ids
        visible_ids = [employee_id] if allowed else []
    try:
        page = crud.list_attendance_records(
            db, company.id, limit=limit, cursor=cursor, employee_ids=visible_ids,
            from_date=from_date, to_date=to_date, active_only=active_only,
            sort_by=sort_by, sort_dir=sort_dir,
        )
    except crud.InvalidCursor as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Edit Attendance: which of these rows the caller may edit (the screen
    # shows the Edit button exactly where the PATCH would be accepted).
    # Every listed row is in the caller's company already, so no per-row
    # lookup: one's own rows only for reporting managers, otherwise inside
    # the edit scope.
    scope = crud.attendance_edit_scope(db, current_user)
    me = current_user.employee_id
    own_ok = None  # computed once, only if an own row is listed
    for item in page["items"]:
        emp = item["employee_id"]
        if emp == me:
            if own_ok is None:
                own_ok = crud.can_edit_own_attendance(db, current_user, scope)
            item["can_edit"] = own_ok
        else:
            item["can_edit"] = scope is None or emp in scope
    return page


@router.get("/dashboard/summary", response_model=schemas.DashboardSummaryOut)
def dashboard_summary(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    # RPT-9: scoped like the attendance list -- org-wide only for callers
    # who may see everyone (Owner/HR people admins, admin-tier roles).
    visible_ids = (
        None if crud._is_people_or_payroll_admin(db, current_user)
        else crud.get_visible_employee_ids_for_docs(db, current_user)
    )
    return crud.dashboard_attendance_summary(db, company.id, visible_ids)


def _regularization_out(
    db: Session,
    r: models.AttendanceRegularization,
    names: dict[uuid.UUID, str] | None = None,
) -> schemas.RegularizationOut:
    """names, when given, is a bulk-resolved lookup for a whole result set
    (see list_regularizations) -- performance only, same output either
    way. Falls back to the original one-row-at-a-time helper when omitted
    (the single-row decide/create call sites)."""
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    return schemas.RegularizationOut(
        id=r.id,
        employee_id=r.employee_id,
        attendance_date=r.attendance_date,
        reason=r.reason,
        status=r.status,
        requested_in=r.requested_in,
        requested_out=r.requested_out,
        approver_id=r.approver_id,
        approver_name=name(r.approver_id),
        employee_name=name(r.employee_id),
        decision_notes=r.decision_notes,
        decided_at=r.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, r.employee_id), r.approver_id
        ),
    )


@router.get("/attendance/regularizations", response_model=list[schemas.RegularizationOut])
def list_regularizations(
    status: str | None = None,
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_requests(db, current_user, "attendance_regularization")
    rows = crud.list_regularizations(
        db, company.id, status=status, employee_ids=visible_ids, limit=limit, offset=offset,
    )
    name_ids = {r.employee_id for r in rows} | {r.approver_id for r in rows if r.approver_id}
    names = crud.employee_display_names_bulk(db, name_ids)
    crud.preload_employees(db, {r.employee_id for r in rows})  # PERF-09: no per-row db.get
    return [_regularization_out(db, r, names=names) for r in rows]


@router.post(
    "/attendance/regularizations",
    response_model=schemas.RegularizationOut,
    status_code=201,
)
def create_regularization(
    payload: schemas.RegularizationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "team_attendance", "regularization", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")

    # WF-03: a requester with no Reporting Manager (e.g. the CEO) is no
    # longer refused -- crud.notify_new_request routes the request to the
    # fallback approvers (Owner / System Settings RBAC holders) who can
    # decide it; self-approval stays blocked.
    # requested_in / requested_out are both optional: with neither, an
    # approval simply marks the day regularized (crud.apply_regularization_
    # to_attendance_record); any time given is still validated below.
    if employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    # A regularization corrects a day that has happened (no future date),
    # within the company's backdate window and not before joining (M-12).
    if (err := attendance_rules.regularization_date_error(db, company.id, employee, payload.attendance_date)):
        raise HTTPException(status_code=400, detail=err)
    record = db.scalar(select(models.AttendanceRecord).where(
        models.AttendanceRecord.employee_id == employee.id,
        models.AttendanceRecord.attendance_date == payload.attendance_date))
    # H-18: both times on the attendance date in company time (+1 day for a
    # night shift), check-out after check-in, nothing in the future.
    if (err := attendance_rules.regularization_time_error(
            db, company.id, employee.id, payload.attendance_date,
            payload.requested_in, payload.requested_out, record)):
        raise HTTPException(status_code=400, detail=err)
    # M-11: one pending / approved regularization per date.
    crud._advisory_lock(db, "regularization", str(employee.id), payload.attendance_date.isoformat())
    if (dup := attendance_rules.open_regularization(db, employee.id, payload.attendance_date)) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A regularization for {payload.attendance_date.strftime('%d %b %Y')} is already {dup.status.replace('_', ' ')}.",
        )
    regularization = crud.create_regularization(
        db,
        payload.employee_id,
        payload.attendance_date,
        payload.reason,
        payload.requested_in,
        payload.requested_out,
    )
    crud.create_audit_log(db, company.id, current_user.id, "create", "regularization", regularization.id)
    db.commit()
    db.refresh(regularization)
    crud.notify_new_request(
        db, company.id, employee, "Attendance Regularization", "regularization", regularization.id
    )
    db.commit()
    return _regularization_out(db, regularization)


@router.patch(
    "/attendance/regularizations/{regularization_id}",
    response_model=schemas.RegularizationOut,
)
def update_regularization(
    regularization_id: uuid.UUID,
    payload: schemas.RegularizationUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py -- crud.can_decide_request
    # is the sole authorization check, not a role-column gate.
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    regularization = crud.get_regularization_for_update(db, regularization_id)
    if regularization is None:
        raise HTTPException(status_code=404, detail="Regularization request not found")
    old_status = regularization.status
    # Checked before the permission gate: with an independent/parallel
    # approval step (e.g. Reporting Manager and Dotted-Line Manager both
    # authorized at once), whichever one decides first closes the request
    # -- the other's still-pending action is no longer needed, and should
    # see a clear "already decided" message rather than a confusing
    # permission error once can_decide_request_configurable (correctly)
    # stops recognizing them as the current decider.
    if old_status in ("approved", "rejected"):
        decider = crud.employee_display_name(db, regularization.approver_id)
        decider_type = crud.decided_by_manager_type(
            db.get(models.Employee, regularization.employee_id), regularization.approver_id
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
    if not crud.can_decide_request_configurable(
        db, current_user, regularization.employee_id, "attendance_regularization", regularization.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's assigned Reporting Manager can act on this request",
        )
    if payload.status == "approved":
        # M-11 / H-18: never overwrite another approved correction of the
        # same date, and re-check times (rows created before these rules).
        dup = attendance_rules.open_regularization(
            db, regularization.employee_id, regularization.attendance_date, exclude_id=regularization.id)
        if dup is not None and dup.status == "approved":
            raise HTTPException(status_code=409, detail="Another regularization for this date is already approved.")
        record = db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == regularization.employee_id,
            models.AttendanceRecord.attendance_date == regularization.attendance_date))
        if (err := attendance_rules.regularization_time_error(
                db, company.id, regularization.employee_id, regularization.attendance_date,
                regularization.requested_in, regularization.requested_out, record)):
            raise HTTPException(status_code=409, detail=f"Can't approve: {err}")
    effective_status = crud.decide_configurable_request(
        db, current_user, regularization.employee_id, "attendance_regularization",
        regularization.id, payload.status, payload.decision_notes,
    )
    regularization.status = effective_status
    regularization.approver_id = current_user.employee_id
    regularization.decision_notes = payload.decision_notes
    if effective_status != "pending":
        regularization.decided_at = datetime.now(timezone.utc)
    if old_status != "approved" and effective_status == "approved":
        crud.apply_regularization_to_attendance_record(db, company.id, regularization)
    crud.create_audit_log(
        db, company.id, current_user.id, payload.status, "regularization", regularization.id
    )
    db.commit()
    db.refresh(regularization)
    if effective_status != "pending":
        crud.notify_decision(
            db, company.id, regularization.employee_id, "Attendance Regularization",
            effective_status, payload.decision_notes, "regularization", regularization.id,
        )
        db.commit()
    return _regularization_out(db, regularization)
