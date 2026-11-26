"""Work entry / timesheet / project allocation rules (HRMS QA M-20, M-22,
M-25, M-26, L-16). Each check returns an error message (or None) so the
routers decide the HTTP status.

Work entries and timesheets have no date locks and no approval step: an
employee may add, edit or delete their own entries for any past, present
or future date, and a week stays editable after it is submitted. What is
still checked is data integrity (time window, overlaps, duplicates, at most
24 h a day), project membership (allocated or project manager) and that the
project isn't Completed."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import crud, models

MAX_DAILY_HOURS = 24.0


COMPLETED_PROJECT_MESSAGE = (
    "This project has been completed. Work entries and timesheet submissions are not allowed."
)


def completed_project_error(project: "models.Project | None") -> str | None:
    """No new or changed work on a project whose status is Completed (entries
    already recorded stay as they are). Checked by every write path --
    HRMS Work and WorkTrack -- so a direct API call can't bypass it."""
    if project is not None and (project.status or "").strip().lower() == "completed":
        return COMPLETED_PROJECT_MESSAGE
    return None


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


def allocation_error(db: Session, employee_id: uuid.UUID, project: "models.Project") -> str | None:
    """M-25: time only on a project the employee is allocated to (or
    manages). Any active allocation counts, whatever its start / end dates
    -- work entries carry no date-based restriction, so an entry may be
    logged, moved or corrected for any day."""
    if project.project_manager_id == employee_id:
        return None
    found = db.scalar(select(func.count()).select_from(models.ProjectAllocation).where(
        models.ProjectAllocation.project_id == project.id,
        models.ProjectAllocation.employee_id == employee_id,
        models.ProjectAllocation.is_active.is_(True)))
    if not found:
        return f"You aren't allocated to project '{project.name}'."
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


