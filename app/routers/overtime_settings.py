"""Overtime Settings and the Overtime Report (app/overtime.py).

    GET /api/overtime-settings     the company's rules (+ leave types to pick)
    PUT /api/overtime-settings     save -- Owner, or Edit/Admin on System
                                   Settings / RBAC or Payroll (Process)
    GET /api/reports/overtime      requests with session actuals and
                                   compensation; scoped to the employees the
                                   caller may see overtime requests for
"""

from __future__ import annotations

import calendar
import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import crud, models, overtime, schemas
from ..database import get_db
from ..deps import get_current_user, has_column_access

router = APIRouter(prefix="/api", tags=["overtime"])

_EDIT_COLUMNS = ("system_settings_rbac", "payroll_process")


def _can_edit(db: Session, user: models.User) -> bool:
    return has_column_access(db, user, *_EDIT_COLUMNS)


def _settings_out(db: Session, user: models.User) -> schemas.OvertimeSettingsOut:
    s = overtime.get_settings(db, user.company_id)
    leave_type = overtime.comp_off_leave_type(db, user.company_id, s)
    types = db.scalars(select(models.LeaveType).where(models.LeaveType.company_id == user.company_id)
                       .order_by(models.LeaveType.name)).all()
    can_edit = _can_edit(db, user)
    pending = db.execute(select(func.count(), func.coalesce(func.sum(models.OvertimeCompensation.amount), 0)).where(
        models.OvertimeCompensation.company_id == user.company_id,
        models.OvertimeCompensation.status == "pending_payroll")).one() if can_edit else (0, 0)
    return schemas.OvertimeSettingsOut(
        pending_payroll_count=int(pending[0]), pending_payroll_amount=float(pending[1]),
        enabled=bool(s.enabled), compensation_mode=s.compensation_mode, rate_basis=s.rate_basis,
        rate_multiplier=float(s.rate_multiplier), monthly_days_divisor=int(s.monthly_days_divisor),
        hours_per_day=float(s.hours_per_day),
        fixed_hourly_rate=float(s.fixed_hourly_rate) if s.fixed_hourly_rate is not None else None,
        comp_off_leave_type_id=leave_type.id if leave_type else None,
        comp_off_leave_type_name=leave_type.name if leave_type else None,
        comp_off_full_day_hours=float(s.comp_off_full_day_hours),
        comp_off_half_day_hours=float(s.comp_off_half_day_hours),
        min_minutes=int(s.min_minutes), rounding=s.rounding, updated_at=s.updated_at,
        saved=db.get(models.OvertimeSettings, user.company_id) is not None,
        can_edit=can_edit,
        leave_types=[{"id": str(t.id), "name": t.name, "code": t.code} for t in types],
    )


@router.get("/overtime-settings", response_model=schemas.OvertimeSettingsOut)
def get_overtime_settings(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return _settings_out(db, current_user)


@router.put("/overtime-settings", response_model=schemas.OvertimeSettingsOut)
def save_overtime_settings(
    payload: schemas.OvertimeSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not _can_edit(db, current_user):
        raise HTTPException(status_code=403, detail="Only HR / payroll administrators can change Overtime Settings.")
    if payload.comp_off_leave_type_id is not None:
        leave_type = db.get(models.LeaveType, payload.comp_off_leave_type_id)
        if leave_type is None or leave_type.company_id != current_user.company_id:
            raise HTTPException(status_code=422, detail="Choose one of your organization's leave types.")
    if payload.compensation_mode == "comp_off" and payload.comp_off_leave_type_id is None \
            and overtime.default_comp_off_leave_type(db, current_user.company_id) is None:
        raise HTTPException(status_code=422, detail="Choose the leave type to credit compensatory leave to.")
    row = db.get(models.OvertimeSettings, current_user.company_id)
    if row is None:
        row = models.OvertimeSettings(company_id=current_user.company_id)
        db.add(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    row.updated_by = current_user.employee_id
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "overtime_settings",
                          current_user.company_id, changes={"mode": payload.compensation_mode,
                                                            "enabled": str(payload.enabled)})
    db.commit()
    return _settings_out(db, current_user)


@router.get("/reports/overtime", response_model=schemas.OvertimeReportOut)
def overtime_report(
    from_date: datetime.date | None = Query(default=None),
    to_date: datetime.date | None = Query(default=None),
    employee_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(pending|approved|rejected|sent_back)$"),
    session_status: str | None = Query(
        default=None, pattern="^(scheduled|clocked_in|missed_clock_in|missed_clock_out|completed)$"),
    manager_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company_id = current_user.company_id
    if overtime.advance_sessions(db, company_id):
        db.commit()
    visible = crud.get_visible_employee_ids_for_requests(db, current_user, "overtime_request")
    q = (select(models.OvertimeRequest, models.Employee)
         .join(models.Employee, models.Employee.id == models.OvertimeRequest.employee_id)
         .where(models.Employee.company_id == company_id)
         .order_by(models.OvertimeRequest.work_date.desc(), models.OvertimeRequest.created_at.desc()))
    if visible is not None:
        q = q.where(models.OvertimeRequest.employee_id.in_(visible or [uuid.UUID(int=0)]))
    if from_date:
        q = q.where(models.OvertimeRequest.work_date >= from_date)
    if to_date:
        q = q.where(models.OvertimeRequest.work_date <= to_date)
    if employee_id:
        q = q.where(models.OvertimeRequest.employee_id == employee_id)
    if status:
        q = q.where(models.OvertimeRequest.status == status)
    if session_status:
        wanted = (session_status, "in_progress") if session_status == "clocked_in" else (session_status,)
        q = q.where(models.OvertimeRequest.session_status.in_(wanted))
    if manager_id:
        q = q.where(or_(models.Employee.reporting_manager_id == manager_id,
                        models.Employee.dotted_line_manager_id == manager_id))
    rows = db.execute(q.limit(2000)).all()
    comps = {c.overtime_request_id: c for c in db.scalars(select(models.OvertimeCompensation).where(
        models.OvertimeCompensation.overtime_request_id.in_([r.id for r, _e in rows])))} if rows else {}
    names = crud.employee_display_names_bulk(
        db, {r.approver_id for r, _e in rows if r.approver_id} | {e.reporting_manager_id for _r, e in rows if e.reporting_manager_id})
    tz = crud.company_tzinfo(db, company_id)

    def missed(r):
        parts = []
        if r.missed_in_notified_at:
            parts.append(f"Missed Clock In (reminded {r.missed_in_notified_at.astimezone(tz).strftime('%d %b %I:%M %p')})")
        if r.missed_out_notified_at:
            parts.append(f"Missed Clock Out (reminded {r.missed_out_notified_at.astimezone(tz).strftime('%d %b %I:%M %p')})")
        return "; ".join(parts) or None
    runs = {}
    out = []
    for r, e in rows:
        c = comps.get(r.id)
        period = None
        if c is not None and c.applied_payroll_run_id:
            run = runs.get(c.applied_payroll_run_id) or db.get(models.PayrollRun, c.applied_payroll_run_id)
            runs[c.applied_payroll_run_id] = run
            if run is not None:
                period = f"{calendar.month_name[run.period_month]} {run.period_year}"
        out.append(schemas.OvertimeReportRowOut(
            request_id=r.id, employee_id=e.id, employee_name=f"{e.first_name} {e.last_name or ''}".strip(),
            employee_code=e.employee_code, department=e.department.name if e.department else None,
            work_date=r.work_date, requested_hours=float(r.hours),
            approved_hours=float(r.hours) if r.status == "approved" else None, status=r.status,
            approver_name=names.get(r.approver_id, "—") if r.approver_id else "—",
            reporting_manager_name=names.get(e.reporting_manager_id) if e.reporting_manager_id else None,
            decided_at=r.decided_at, decision_notes=r.decision_notes, reason=r.reason,
            day_type=overtime.day_context(db, company_id, e, r.work_date)["day_type"],
            start_time=r.start_time, end_time=overtime.end_time_of(db, r),
            missed_clock_in_at=r.missed_in_notified_at, missed_clock_out_at=r.missed_out_notified_at,
            missed_punches=missed(r),
            planned_start=r.planned_start, planned_end=r.planned_end, session_status=overtime.session_state(r),
            actual_start=r.actual_start, actual_end=r.actual_end, actual_minutes=r.actual_minutes,
            actual_duration=overtime.fmt_minutes(r.actual_minutes) if r.actual_minutes is not None else None,
            compensation_mode=c.mode if c else None, compensation_status=c.status if c else None,
            amount=float(c.amount) if c and c.amount is not None else None,
            leave_days=float(c.leave_days) if c and c.leave_days is not None else None,
            payroll_period=period,
        ))
    worked = sum(x.actual_minutes or 0 for x in out if x.session_status == "completed")
    return schemas.OvertimeReportOut(
        rows=out, total_requests=len(out), approved_requests=sum(1 for x in out if x.status == "approved"),
        total_requested_hours=round(sum(x.requested_hours for x in out), 2),
        total_approved_hours=round(sum(x.approved_hours or 0 for x in out), 2),
        missed_clock_ins=sum(1 for x in out if x.missed_clock_in_at),
        missed_clock_outs=sum(1 for x in out if x.missed_clock_out_at),
        total_worked_minutes=worked, total_worked=overtime.fmt_minutes(worked),
        total_amount=round(sum(x.amount or 0 for x in out if x.compensation_status != "skipped"), 2),
        total_leave_days=sum(x.leave_days or 0 for x in out if x.compensation_status == "credited"),
    )
