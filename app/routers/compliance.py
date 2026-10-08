"""Missing Attendance & Work Compliance: tenant settings, a manual run and
the Reports & Analytics exception report (app/compliance.py does the work)."""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import compliance, crud, models, shift_reminders
from ..database import get_db
from ..deps import get_current_user, has_column_access

router = APIRouter(prefix="/api", tags=["compliance"])

# M-17: company-wide compliance settings / manual runs are an admin action --
# System Settings (Edit/Admin), the Owner (has_column_access), or the HR role.
# team_attendance used to be accepted here, which let every Manager change
# the tenant's EOD cutoff and trigger company-wide runs.
_EDIT_COLUMNS = ("system_settings_rbac",)
_EDIT_ROLE_NAMES = frozenset({"HR / Recruitment Staff"})


def _can_edit(db: Session, user: models.User) -> bool:
    if has_column_access(db, user, *_EDIT_COLUMNS):
        return True
    role = crud.get_user_primary_role(db, user.id)
    return role is not None and role.name in _EDIT_ROLE_NAMES


class ComplianceSettingsIO(BaseModel):
    enabled: bool = True
    eod_cutoff: datetime.time = datetime.time(23, 0)
    check_attendance: bool = True
    check_overtime: bool = True
    check_work_entry: bool = True
    check_timesheet: bool = True
    notify_employee: bool = True
    reminders_enabled: bool = True
    clock_in_reminder_minutes: int = 15
    end_reminder_minutes: int = 15
    can_edit: bool = False
    updated_at: datetime.datetime | None = None


class ComplianceRowOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str | None = None
    department: str | None = None
    reporting_manager_name: str | None = None
    exception_date: datetime.date
    exception_type: str
    exception_label: str
    expected: str | None = None
    expected_at: datetime.datetime | None = None
    actual_at: datetime.datetime | None = None
    details: str | None = None
    regularization_status: str | None = None
    notification_status: str | None = None
    manager_notified_at: datetime.datetime | None = None
    employee_notified_at: datetime.datetime | None = None
    status: str
    resolution: str | None = None
    resolved_at: datetime.datetime | None = None


class ComplianceReportOut(BaseModel):
    rows: list[ComplianceRowOut]
    total: int
    open: int
    resolved: int
    by_type: dict[str, int]


def _settings_out(db: Session, user: models.User) -> ComplianceSettingsIO:
    s = compliance.get_settings(db, user.company_id)
    return ComplianceSettingsIO(
        enabled=s.enabled, eod_cutoff=s.eod_cutoff, check_attendance=s.check_attendance,
        check_overtime=s.check_overtime, check_work_entry=s.check_work_entry, check_timesheet=s.check_timesheet,
        notify_employee=s.notify_employee, reminders_enabled=s.reminders_enabled,
        clock_in_reminder_minutes=s.clock_in_reminder_minutes, end_reminder_minutes=s.end_reminder_minutes,
        can_edit=_can_edit(db, user), updated_at=s.updated_at)


@router.get("/compliance/settings", response_model=ComplianceSettingsIO)
def get_compliance_settings(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return _settings_out(db, current_user)


@router.put("/compliance/settings", response_model=ComplianceSettingsIO)
def save_compliance_settings(payload: ComplianceSettingsIO, db: Session = Depends(get_db),
                             current_user: models.User = Depends(get_current_user)):
    """Tenant-specific EOD cutoff and checks."""
    if not _can_edit(db, current_user):
        raise HTTPException(status_code=403, detail="You don't have permission to change compliance settings.")
    row = db.get(models.ComplianceSettings, current_user.company_id)
    if row is None:
        row = models.ComplianceSettings(company_id=current_user.company_id)
        db.add(row)
    if not (1 <= payload.clock_in_reminder_minutes <= 240 and 1 <= payload.end_reminder_minutes <= 240):
        raise HTTPException(status_code=422, detail="Reminder minutes must be between 1 and 240.")
    for f in ("enabled", "eod_cutoff", "check_attendance", "check_overtime", "check_work_entry",
              "check_timesheet", "notify_employee", "reminders_enabled", "clock_in_reminder_minutes",
              "end_reminder_minutes"):
        setattr(row, f, getattr(payload, f))
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    row.updated_by = current_user.id
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "compliance_settings",
                          current_user.company_id)
    db.commit()
    return _settings_out(db, current_user)


class ShiftReminderOut(BaseModel):
    id: uuid.UUID
    kind: str
    reminder_date: datetime.date
    title: str
    message: str
    due_at: datetime.datetime
    shift_label: str | None = None
    acknowledged_at: datetime.datetime | None = None


def _reminder_out(r: models.ShiftReminder) -> ShiftReminderOut:
    return ShiftReminderOut(id=r.id, kind=r.kind, reminder_date=r.reminder_date, title=r.title, message=r.message,
                            due_at=r.due_at, shift_label=r.shift_label, acknowledged_at=r.acknowledged_at)


@router.get("/me/shift-reminders", response_model=list[ShiftReminderOut])
def my_shift_reminders(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """The caller's reminders still to show as a popup (creating any that are
    due now, once each, with their in-app notification). Polled by the app;
    the scheduler creates them for everyone too."""
    if current_user.employee_id is None:
        return []
    employee = db.get(models.Employee, current_user.employee_id)
    if employee is None:
        return []
    shift_reminders.deliver(db, current_user.company_id, employee)
    db.commit()
    return [_reminder_out(r) for r in shift_reminders.open_popups(db, employee.id, current_user.company_id)]


@router.post("/me/shift-reminders/{reminder_id}/ack", response_model=ShiftReminderOut)
def acknowledge_shift_reminder(reminder_id: uuid.UUID, db: Session = Depends(get_db),
                               current_user: models.User = Depends(get_current_user)):
    """The popup was dismissed -- never shown again (any device / session)."""
    r = db.get(models.ShiftReminder, reminder_id)
    if r is None or r.employee_id != current_user.employee_id:
        raise HTTPException(status_code=404, detail="Reminder not found")
    if r.acknowledged_at is None:
        r.acknowledged_at = datetime.datetime.now(datetime.timezone.utc)
        if r.notification_id:
            note = db.get(models.Notification, r.notification_id)
            if note is not None and note.read_at is None:
                note.read_at = r.acknowledged_at
        db.commit()
    return _reminder_out(r)


@router.post("/compliance/run")
def run_compliance(
    work_date: datetime.date | None = Query(default=None),
    employee_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Admin: process one date now (optionally one employee) -- e.g. after
    the scheduler was down. Same rules and the same duplicate guard as the
    EOD pass; a date whose cutoff hasn't passed is refused."""
    if not _can_edit(db, current_user):
        raise HTTPException(status_code=403, detail="You don't have permission to run the compliance check.")
    company_id = current_user.company_id
    if work_date is None:
        n = compliance.run_company(db, company_id)
        db.commit()
        return {"new_exceptions": n}
    if datetime.datetime.now(datetime.timezone.utc) < compliance.cutoff_at(db, company_id, work_date):
        raise HTTPException(status_code=409, detail="The EOD cutoff for that date hasn't passed yet.")
    if employee_id is not None and crud.employee_ids_outside_company(db, [employee_id], company_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    rows = compliance.process_day(db, company_id, work_date, employee_ids=[employee_id] if employee_id else None)
    if employee_id is None and db.get(models.ComplianceRun, (company_id, work_date)) is None:
        db.add(models.ComplianceRun(company_id=company_id, run_date=work_date,
                                    processed_at=datetime.datetime.now(datetime.timezone.utc), exceptions=len(rows)))
    db.commit()
    return {"new_exceptions": len(rows)}


@router.get("/reports/compliance", response_model=ComplianceReportOut)
def compliance_report(
    from_date: datetime.date | None = Query(default=None),
    to_date: datetime.date | None = Query(default=None),
    exception_type: str | None = Query(default=None, pattern="^(" + "|".join(compliance.TYPES) + ")$"),
    status: str | None = Query(default=None, pattern="^(open|resolved)$"),
    employee_id: uuid.UUID | None = Query(default=None),
    manager_id: uuid.UUID | None = Query(default=None),
    department_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company_id = current_user.company_id
    # L-23: an inverted range is a client error, not an empty report.
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=422, detail="from_date must be on or before to_date.")
    # L-25: read-only. Due dates are processed by the scheduler
    # (reminders.py -> compliance.run_company every pass) or on demand via
    # POST /api/compliance/run -- never as a side effect of viewing.
    visible = crud.get_visible_employee_ids_for_requests(db, current_user, "regularization")
    q = (select(models.ComplianceException, models.Employee)
         .join(models.Employee, models.Employee.id == models.ComplianceException.employee_id)
         .where(models.ComplianceException.company_id == company_id)
         .order_by(models.ComplianceException.exception_date.desc(), models.Employee.first_name))
    if visible is not None:
        q = q.where(models.ComplianceException.employee_id.in_(visible or [uuid.UUID(int=0)]))
    if from_date:
        q = q.where(models.ComplianceException.exception_date >= from_date)
    if to_date:
        q = q.where(models.ComplianceException.exception_date <= to_date)
    if exception_type:
        q = q.where(models.ComplianceException.exception_type == exception_type)
    if status:
        q = q.where(models.ComplianceException.status == status)
    if employee_id:
        q = q.where(models.ComplianceException.employee_id == employee_id)
    if manager_id:
        q = q.where(or_(models.Employee.reporting_manager_id == manager_id,
                        models.Employee.dotted_line_manager_id == manager_id))
    if department_id:
        q = q.where(models.Employee.department_id == department_id)
    rows = db.execute(q.limit(5000)).all()
    names = crud.employee_display_names_bulk(db, {e.reporting_manager_id for _r, e in rows if e.reporting_manager_id})
    out = [ComplianceRowOut(
        id=r.id, employee_id=e.id, employee_name=f"{e.first_name} {e.last_name or ''}".strip(),
        employee_code=e.employee_code, department=e.department.name if e.department else None,
        reporting_manager_name=names.get(e.reporting_manager_id) if e.reporting_manager_id else None,
        exception_date=r.exception_date, exception_type=r.exception_type,
        exception_label=compliance.TYPES.get(r.exception_type, r.exception_type),
        expected=r.expected_label, expected_at=r.expected_at, actual_at=r.actual_at, details=r.details,
        regularization_status=r.regularization_status, notification_status=r.notification_status,
        manager_notified_at=r.manager_notified_at, employee_notified_at=r.employee_notified_at,
        status=r.status, resolution=r.resolution, resolved_at=r.resolved_at,
    ) for r, e in rows]
    by_type: dict[str, int] = {}
    for x in out:
        by_type[x.exception_label] = by_type.get(x.exception_label, 0) + 1
    return ComplianceReportOut(rows=out, total=len(out), open=sum(1 for x in out if x.status == "open"),
                               resolved=sum(1 for x in out if x.status == "resolved"), by_type=by_type)
