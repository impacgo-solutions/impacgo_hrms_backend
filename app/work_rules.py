"""Work entry / timesheet / project allocation rules (HRMS QA M-20, M-22,
M-24, M-25, M-26, L-16, L-17). Each check returns an error message (or
None) so the routers decide the HTTP status."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import crud, models

MAX_DAILY_HOURS = 24.0
LOCKED_TIMESHEET_STATUSES = ("approved", "rejected")


def week_start_of(day: datetime.date) -> datetime.date:
    """M-22: every timesheet week starts on a Monday."""
    return day - datetime.timedelta(days=day.weekday())


def _entries_on(db: Session, employee_id: uuid.UUID, day: datetime.date, exclude_id: uuid.UUID | None):
    q = (select(models.WorkEntry).join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
         .where(models.Timesheet.employee_id == employee_id, models.WorkEntry.entry_date == day))
    if exclude_id is not None:
        q = q.where(models.WorkEntry.id != exclude_id)
    return db.scalars(q).all()


def _minutes(t: datetime.time) -> int:
    return t.hour * 60 + t.minute


def entry_error(db: Session, employee_id: uuid.UUID, day: datetime.date, *, project_id: uuid.UUID,
                task_id, task_name, start_time, end_time, hours: float,
                exclude_id: uuid.UUID | None = None) -> str | None:
    """L-16 + M-20: end after start, hours within the window, no overlapping
    / duplicate entry, and at most 24 h logged for the day."""
    if (start_time is None) != (end_time is None):
        return "Give both the start and the end time, or neither."
    if start_time is not None:
        if end_time <= start_time:
            return "The end time must be after the start time."
        window = (_minutes(end_time) - _minutes(start_time)) / 60
        if float(hours) > window + 0.01:
            return f"Hours ({float(hours):g}) can't exceed the time window ({window:g} h)."
    others = _entries_on(db, employee_id, day, exclude_id)
    total = sum(float(o.hours) for o in others) + float(hours)
    if total > MAX_DAILY_HOURS + 1e-9:
        logged = total - float(hours)
        return (f"At most {MAX_DAILY_HOURS:g} hours can be logged for one day "
                f"({logged:g} h already logged on {day.strftime('%d %b %Y')}).")
    task_key = (str(task_id) if task_id else "", (task_name or "").strip().lower())
    for o in others:
        if start_time is not None and o.start_time is not None and o.end_time is not None:
            if start_time < o.end_time and o.start_time < end_time:
                return (f"This overlaps another work entry on {day.strftime('%d %b %Y')} "
                        f"({o.start_time.strftime('%H:%M')}–{o.end_time.strftime('%H:%M')}).")
        same_task = (str(o.task_id) if o.task_id else "", (o.task_name or "").strip().lower()) == task_key
        if (o.project_id == project_id and same_task and float(o.hours) == float(hours)
                and o.start_time == start_time and o.end_time == end_time):
            return "An identical work entry already exists for this day."
    return None


def locked_week_error(db: Session, employee_id: uuid.UUID, day: datetime.date) -> str | None:
    """M-24: a submitted (approved) or rejected week is locked until an
    admin unlocks it (POST /timesheets/{id}/unlock)."""
    ws = week_start_of(day)
    status = db.scalar(select(models.Timesheet.status).where(
        models.Timesheet.employee_id == employee_id, models.Timesheet.week_start == ws).limit(1))
    if status in LOCKED_TIMESHEET_STATUSES:
        return (f"The week of {ws.strftime('%d %b %Y')} is {status} and locked -- "
                "ask an administrator to unlock it first.")
    return None


def allocation_error(db: Session, employee_id: uuid.UUID, project: "models.Project",
                     day: datetime.date) -> str | None:
    """M-25: time only on a project the employee is actively allocated to on
    that day (or manages)."""
    if project.project_manager_id == employee_id:
        return None
    found = db.scalar(select(func.count()).select_from(models.ProjectAllocation).where(
        models.ProjectAllocation.project_id == project.id,
        models.ProjectAllocation.employee_id == employee_id,
        models.ProjectAllocation.is_active.is_(True),
        models.ProjectAllocation.start_date <= day,
        or_(models.ProjectAllocation.end_date.is_(None), models.ProjectAllocation.end_date >= day)))
    if not found:
        return f"You aren't allocated to project '{project.name}' on {day.strftime('%d %b %Y')}."
    return None


def allocated_pct(db: Session, employee_id: uuid.UUID, on: datetime.date | None = None,
                  exclude_id: uuid.UUID | None = None) -> float:
    """M-26: the employee's summed active allocation % across active
    projects (allocations that haven't ended)."""
    on = on or crud.employee_company_today(db, employee_id)
    q = (select(func.coalesce(func.sum(models.ProjectAllocation.allocation_pct), 0))
         .join(models.Project, models.Project.id == models.ProjectAllocation.project_id)
         .where(models.ProjectAllocation.employee_id == employee_id,
                models.ProjectAllocation.is_active.is_(True),
                or_(models.ProjectAllocation.end_date.is_(None), models.ProjectAllocation.end_date >= on),
                models.Project.is_active.is_(True),
                models.Project.status.notin_(("completed", "cancelled"))))
    if exclude_id is not None:
        q = q.where(models.ProjectAllocation.id != exclude_id)
    return float(db.scalar(q) or 0)


def capacity_error(db: Session, employee_id: uuid.UUID, pct: float) -> str | None:
    if float(pct) <= 0:
        return None  # a 0% tag (e.g. Team Lead) never adds load
    current = allocated_pct(db, employee_id)
    if current + float(pct) > 100 + 1e-9:
        return (f"This would allocate the employee {current + float(pct):g}% across active projects "
                f"(already {current:g}%); the total can't exceed 100%.")
    return None
