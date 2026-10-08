"""Shift-based reminders -- never punches anything.

For each employee, from their ACTUAL assigned shift that day (else the
company office hours) and the tenant configuration (hcm_compliance_settings:
reminders_enabled, clock_in_reminder_minutes, end_reminder_minutes):

  clock_in    N min after the shift start, still not checked in
  clock_out   N min before the shift end, checked in but not checked out
  work_entry  N min before the shift end, today's work entry missing or this
              week's timesheet not submitted (employees on a project)

Never on approved full-day leave, holidays, weekly offs or other non-working
days; a half-day leave skips the reminder for the half it covers. Each
reminder is one hcm_shift_reminders row (unique per employee / date / kind)
with one in-app notification (pushed live over the notifications socket),
so it is never repeated; the popup is shown until acknowledged
(acknowledged_at -- shared across refresh, re-login and devices).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import compliance, crud, models, overtime

KINDS = ("clock_in", "clock_out", "work_entry")


def _windows(db: Session, company_id: uuid.UUID, employee: "models.Employee", now: datetime.datetime):
    """(work date, start, end, label) of today's shift and of yesterday's if
    it is a night shift still running -- only on working days."""
    today = crud.company_today(db, company_id)
    out = []
    for day in (today - datetime.timedelta(days=1), today):
        working, _ = overtime._working_day(db, company_id, employee.branch_id, day)
        if not working:
            continue
        w = overtime._shift_window(db, company_id, employee.id, day)
        if w is None:
            continue
        if day != today and w[1] <= now - datetime.timedelta(hours=1):
            continue  # yesterday's shift is long over
        out.append((day, w[0], w[1], w[2]))
    return out


def _leave_covers(db, employee_id, day, start, end, at: datetime.datetime) -> bool:
    """Approved leave on the day: full day, or the half that contains [at]."""
    block = crud.get_blocking_leave_window(db, employee_id, day)
    if block is None:
        return False
    if block.get("full_day"):
        return True
    tz = at.tzinfo
    local = at.astimezone(tz).time() if tz else at.time()
    return block["start"] <= local <= block["end"]


def due_reminders(db: Session, company_id: uuid.UUID, employee: "models.Employee",
                  now: datetime.datetime, settings=None) -> list[dict]:
    """The reminders due for [employee] at [now] that don't exist yet."""
    settings = settings or compliance.get_settings(db, company_id)
    if not settings.reminders_enabled or not employee.is_active:
        return []
    tz = crud.company_tzinfo(db, company_id)
    after_start = datetime.timedelta(minutes=int(settings.clock_in_reminder_minutes or 15))
    before_end = datetime.timedelta(minutes=int(settings.end_reminder_minutes or 15))
    out = []
    for day, start, end, label in _windows(db, company_id, employee, now):
        if employee.date_of_joining and employee.date_of_joining > day:
            continue
        existing = set(db.scalars(select(models.ShiftReminder.kind).where(
            models.ShiftReminder.employee_id == employee.id, models.ShiftReminder.reminder_date == day)).all())
        rec = db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == employee.id, models.AttendanceRecord.attendance_date == day))
        checked_in = rec is not None and rec.check_in is not None
        checked_out = rec is not None and rec.check_out is not None
        window = f"{label} {start.astimezone(tz).strftime('%I:%M %p')} – {end.astimezone(tz).strftime('%I:%M %p')}"
        if ("clock_in" not in existing and now >= start + after_start and now < end and not checked_in
                and not _leave_covers(db, employee.id, day, start, end, start.astimezone(tz))):
            out.append(dict(day=day, kind="clock_in", due_at=start + after_start, label=window,
                            title="Clock In reminder",
                            message=f"Your shift ({window}) started at {start.astimezone(tz).strftime('%I:%M %p')} "
                                    "and you haven't clocked in yet. Clock In from Attendance."))
        in_end_window = end - before_end <= now < end + datetime.timedelta(hours=2)
        end_local = end.astimezone(tz)
        if (in_end_window and "clock_out" not in existing and checked_in and not checked_out
                and not _leave_covers(db, employee.id, day, start, end, end_local)):
            out.append(dict(day=day, kind="clock_out", due_at=end - before_end, label=window,
                            title="Prepare to Clock Out",
                            message=f"Your shift ends at {end_local.strftime('%I:%M %p')}. Finish up and "
                                    "Clock Out from Attendance when you're done."))
        if (in_end_window and "work_entry" not in existing and compliance._on_project(db, employee.id, day)
                and not _leave_covers(db, employee.id, day, start, end, end_local)):
            has_entry = db.scalar(select(func.count()).select_from(models.WorkEntry)
                                  .join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
                                  .where(models.Timesheet.employee_id == employee.id,
                                         models.WorkEntry.entry_date == day)) > 0
            week_start = day - datetime.timedelta(days=day.weekday())
            ts = db.scalar(select(models.Timesheet).where(
                models.Timesheet.employee_id == employee.id, models.Timesheet.week_start == week_start,
                models.Timesheet.is_active.is_(True)).limit(1))
            last_day = compliance._last_working_day_of_week(db, company_id, employee.branch_id, day) == day
            ts_missing = last_day and (ts is None or ts.submitted_at is None or ts.status in ("draft", "sent_back"))
            if not has_entry or ts_missing:
                what = " and ".join(x for x in (
                    "today's Work Entry" if not has_entry else None,
                    "this week's Timesheet" if ts_missing else None) if x)
                out.append(dict(day=day, kind="work_entry", due_at=end - before_end, label=window,
                                title="Complete your Work Entry",
                                message=f"Your shift ends at {end_local.strftime('%I:%M %p')}. Please complete {what} "
                                        "in Work & Timesheet."))
    return out


def deliver(db: Session, company_id: uuid.UUID, employee: "models.Employee",
            now: datetime.datetime | None = None, settings=None) -> list["models.ShiftReminder"]:
    """Creates (once) the reminders due now, each with its in-app notification."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    new = []
    user_id = crud.get_user_id_for_employee(db, employee.id)
    for r in due_reminders(db, company_id, employee, now, settings):
        crud._advisory_lock(db, "shift_reminder", str(employee.id), r["day"].isoformat(), r["kind"])
        if db.scalar(select(models.ShiftReminder.id).where(
                models.ShiftReminder.employee_id == employee.id, models.ShiftReminder.reminder_date == r["day"],
                models.ShiftReminder.kind == r["kind"]).limit(1)) is not None:
            continue
        row = models.ShiftReminder(id=uuid.uuid4(), company_id=company_id, employee_id=employee.id,
                                   reminder_date=r["day"], kind=r["kind"], title=r["title"], message=r["message"],
                                   due_at=r["due_at"], shift_label=r["label"], created_at=now)
        db.add(row)
        db.flush()
        if user_id is not None:
            note = crud.create_notification(db, company_id, user_id, r["title"], r["message"],
                                            entity_type="shift_reminder", entity_id=row.id)
            row.notification_id = note.id if note else None
        new.append(row)
    db.flush()
    return new


def run_company(db: Session, company_id: uuid.UUID, now: datetime.datetime | None = None) -> int:
    """Scheduler pass: every active employee of the company."""
    settings = compliance.get_settings(db, company_id)
    if not settings.reminders_enabled:
        return 0
    n = 0
    for e in db.scalars(select(models.Employee).where(models.Employee.company_id == company_id,
                                                      models.Employee.is_active.is_(True))).all():
        n += len(deliver(db, company_id, e, now, settings))
    return n


def open_popups(db: Session, employee_id: uuid.UUID, company_id: uuid.UUID | None = None) -> list["models.ShiftReminder"]:
    """Reminders not acknowledged yet (today / yesterday's night shift)."""
    since = crud.company_today(db, company_id) - datetime.timedelta(days=1)
    return list(db.scalars(select(models.ShiftReminder).where(
        models.ShiftReminder.employee_id == employee_id, models.ShiftReminder.acknowledged_at.is_(None),
        models.ShiftReminder.reminder_date >= since).order_by(models.ShiftReminder.due_at)).all())
