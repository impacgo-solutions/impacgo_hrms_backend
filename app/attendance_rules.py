"""Attendance / shift / regularization rules (HRMS QA H-17..H-20, M-11..M-16,
L-11..L-13, N-03) kept out of the shared crud module.

  * Who may set up shifts and holidays (Admin level) and whom a manager may
    (re)assign shifts to (their reporting subtree).
  * Regularization validation: times on the attendance date in company time
    (+1 day for night shifts), one pending/approved request per date, a
    configurable backdate window and the joining date.
  * Shift-assignment effective-date window.
  * The daily absence job (mark_absences): a working day with no punch, no
    leave, no holiday and no regularization becomes an 'absent' record --
    the status payroll's LOP proration already reads.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import crud, models

DEFAULT_REGULARIZATION_BACKDATE_DAYS = 30
# L-12: an assignment may be backdated at most this many days (never before
# the employee's joining date); future dates schedule the change.
SHIFT_ASSIGNMENT_BACKDATE_DAYS = 31
# H-17: an open check-in older than this is never auto-closed by a check-out.
MAX_OPEN_SESSION_HOURS = 20
# M-14: how many past days each pass (re)evaluates -- covers scheduler
# downtime and leave / regularization decided a few days late.
ABSENCE_LOOKBACK_DAYS = 3
AUTO_ABSENT_SOURCE = "auto_absent"


# ── who may do what ─────────────────────────────────────────────────────────

def is_attendance_admin(db: Session, user: "models.User") -> bool:
    """H-20: Admin level for attendance setup (shifts, holidays, assigning
    anyone): the Owner, Admin ('a') on Team Attendance (role matrix or the
    employee's own Attendance level), or an HR people administrator."""
    from .rbac_columns import SELF_SERVICE_ROLES

    names = {r.name for r in crud.get_user_roles(db, user.id)}
    if crud.BUILTIN_ROLES[0] in names:
        return True
    if crud.effective_user_matrix(db, user).get("team_attendance") == "a":
        return True
    role = crud.get_user_primary_role(db, user.id)
    return bool(names & crud._PEOPLE_ADMIN_ROLES) and (role is None or role.name not in SELF_SERVICE_ROLES)


def employees_outside_scope(db: Session, user: "models.User", employee_ids) -> list[uuid.UUID]:
    """H-20: the ids a (non-admin) manager may NOT assign/unassign -- anyone
    outside their reporting subtree (Reporting / Dotted-Line, any depth)."""
    ids = list(dict.fromkeys(employee_ids))
    if is_attendance_admin(db, user):
        return []
    if user.employee_id is None:
        return ids
    subtree = crud._reporting_subtree_ids(db, user.employee_id, user.company_id)
    return [i for i in ids if i not in subtree]


# ── shift assignment window (L-12) ─────────────────────────────────────────

def assignment_start_error(db: Session, company_id: uuid.UUID, employee_ids, from_date: datetime.date) -> str | None:
    today = crud.company_today(db, company_id)
    earliest = today - datetime.timedelta(days=SHIFT_ASSIGNMENT_BACKDATE_DAYS)
    if from_date < earliest:
        return (f"The effective date can be at most {SHIFT_ASSIGNMENT_BACKDATE_DAYS} days in the past "
                f"(earliest {earliest.strftime('%d %b %Y')}).")
    ids = list(employee_ids)
    if not ids:
        return None
    rows = db.execute(select(models.Employee.first_name, models.Employee.last_name, models.Employee.date_of_joining)
                      .where(models.Employee.id.in_(ids))).all()
    late = [f"{r.first_name} {r.last_name or ''}".strip() for r in rows
            if r.date_of_joining is not None and r.date_of_joining > from_date]
    if late:
        return f"The effective date is before the joining date of: {', '.join(sorted(late)[:5])}."
    return None


# ── regularization (H-18, M-11, M-12) ──────────────────────────────────────

def regularization_backdate_days(db: Session, company_id: uuid.UUID) -> int:
    row = crud.get_company_settings(db, company_id)
    value = getattr(row, "regularization_backdate_days", None) if row else None
    return int(value) if value is not None else DEFAULT_REGULARIZATION_BACKDATE_DAYS


def _local(db: Session, company_id: uuid.UUID, t: datetime.datetime) -> datetime.datetime:
    tz = crud.company_tzinfo(db, company_id)
    return (t if t.tzinfo else t.replace(tzinfo=tz)).astimezone(tz)


def regularization_time_error(db: Session, company_id: uuid.UUID, employee_id: uuid.UUID,
                              day: datetime.date, requested_in, requested_out,
                              record: "models.AttendanceRecord | None" = None) -> str | None:
    """H-18: corrected times must fall on [day] in company time; a night
    shift (assigned that day) may check out on the next day. The resulting
    check-out must follow the check-in (with the day's punch filling the
    side not being corrected) and nothing may lie in the future."""
    shift = crud._get_active_shift(db, employee_id, day)
    night = bool(shift is not None and shift.is_night)
    next_day = day + datetime.timedelta(days=1)
    now = datetime.datetime.now(datetime.timezone.utc)
    for label, t in (("check-in", requested_in), ("check-out", requested_out)):
        if t is None:
            continue
        local_day = _local(db, company_id, t).date()
        allowed = {day, next_day} if (night and label == "check-out") else {day}
        if night and label == "check-in":
            allowed = {day, next_day}  # a night shift may start just after midnight
        if local_day not in allowed:
            extra = " or the next morning (night shift)" if night else ""
            return (f"The corrected {label} must be on {day.strftime('%d %b %Y')}{extra} "
                    f"(company time); got {local_day.strftime('%d %b %Y')}.")
        if t.tzinfo is not None and t > now:
            return f"The corrected {label} can't be in the future."
    final_in = requested_in if requested_in is not None else (record.check_in if record else None)
    final_out = requested_out if requested_out is not None else (record.check_out if record else None)
    if final_in is not None and final_out is not None:
        a, b = _local(db, company_id, final_in), _local(db, company_id, final_out)
        if b <= a:
            return "The check-out must be after the check-in."
        if (b - a) > datetime.timedelta(hours=24):
            return "A check-in to check-out span can't exceed 24 hours."
    return None


def regularization_date_error(db: Session, company_id: uuid.UUID, employee: "models.Employee",
                              day: datetime.date) -> str | None:
    """M-12: within the company's backdate window and not before joining."""
    today = crud.company_today(db, company_id)
    if day > today:
        return "Attendance can only be regularized for today or a past date."
    window = regularization_backdate_days(db, company_id)
    if day < today - datetime.timedelta(days=window):
        return f"Attendance can only be regularized up to {window} days back."
    if employee.date_of_joining is not None and day < employee.date_of_joining:
        return "Attendance can't be regularized for a date before the joining date."
    return None


def open_regularization(db: Session, employee_id: uuid.UUID, day: datetime.date,
                        exclude_id: uuid.UUID | None = None) -> "models.AttendanceRegularization | None":
    """M-11: a pending or approved regularization already covering the date."""
    q = select(models.AttendanceRegularization).where(
        models.AttendanceRegularization.employee_id == employee_id,
        models.AttendanceRegularization.attendance_date == day,
        models.AttendanceRegularization.status.in_(("pending", "approved", "l1_approved")),
    )
    if exclude_id is not None:
        q = q.where(models.AttendanceRegularization.id != exclude_id)
    return db.scalars(q.limit(1)).first()


# ── night-shift / overnight check-out (H-17) ───────────────────────────────

def open_previous_record(db: Session, employee_id: uuid.UUID, today: datetime.date,
                         now: datetime.datetime) -> "models.AttendanceRecord | None":
    """The previous day's still-open session this check-out should close:
    checked in (not out) on today-1, at most MAX_OPEN_SESSION_HOURS ago."""
    rec = db.scalar(select(models.AttendanceRecord).where(
        models.AttendanceRecord.employee_id == employee_id,
        models.AttendanceRecord.attendance_date == today - datetime.timedelta(days=1),
        models.AttendanceRecord.check_in.is_not(None),
        models.AttendanceRecord.check_out.is_(None),
    ).with_for_update())
    if rec is None or now - rec.check_in > datetime.timedelta(hours=MAX_OPEN_SESSION_HOURS):
        return None
    return rec


# ── daily absence job (M-14) ───────────────────────────────────────────────

def _working_days_per_week(db: Session, company_id: uuid.UUID) -> int:
    row = crud.get_company_settings(db, company_id)
    return (row.working_days_per_week if row else None) or 5


def clear_auto_absences(db: Session, employee_id: uuid.UUID,
                        from_date: datetime.date, to_date: datetime.date) -> int:
    """Removes the job's own auto-absent rows in [from_date, to_date] for one
    employee -- called the moment leave covering those days is approved, so
    payroll LOP never sees an absence the leave now explains (the nightly
    pass would only catch it within its lookback window). Real punches and
    manual records are never touched. Caller commits."""
    rows = db.scalars(select(models.AttendanceRecord).where(
        models.AttendanceRecord.employee_id == employee_id,
        models.AttendanceRecord.attendance_date >= from_date,
        models.AttendanceRecord.attendance_date <= to_date,
        models.AttendanceRecord.source == AUTO_ABSENT_SOURCE,
        models.AttendanceRecord.status == "absent",
    )).all()
    for rec in rows:
        db.delete(rec)
    return len(rows)


def mark_absences(db: Session, company_id: uuid.UUID, now: datetime.datetime | None = None) -> dict:
    """Marks 'absent' (source auto_absent) every past working day in the
    lookback window (each date only after the company's EOD cutoff) on which
    an active, already-joined employee has no attendance record at all, no
    leave request that isn't rejected / cancelled, no pending or approved
    regularization and no company / branch holiday. Idempotent; an earlier
    auto-absent row is removed again if leave / a holiday / a regularization
    now covers it. Gated by the attendance module and the company's
    Missing-Attendance compliance switch (Administration > Compliance)."""
    from . import compliance

    now = now or datetime.datetime.now(datetime.timezone.utc)
    result = {"marked": 0, "cleared": 0}
    if not crud.is_module_enabled(db, company_id, "attendance"):
        return result
    cs = compliance.get_settings(db, company_id)
    if not cs.enabled or not cs.check_attendance:
        return result
    crud._advisory_lock(db, "absence_job", str(company_id))
    today = crud.company_today(db, company_id)
    days = [today - datetime.timedelta(days=i) for i in range(ABSENCE_LOOKBACK_DAYS, 0, -1)]
    days = [d for d in days if now >= compliance.cutoff_at(db, company_id, d, cs)]
    if not days:
        return result
    first, last = days[0], days[-1]
    per_week = _working_days_per_week(db, company_id)

    employees = db.scalars(select(models.Employee).where(
        models.Employee.company_id == company_id, models.Employee.is_active.is_(True))).all()
    emp_ids = [e.id for e in employees]
    if not emp_ids:
        return result
    holidays = db.execute(select(models.Holiday.holiday_date, models.Holiday.branch_id).where(
        models.Holiday.company_id == company_id, models.Holiday.holiday_date >= first,
        models.Holiday.holiday_date <= last)).all()
    records = {(r.employee_id, r.attendance_date): r for r in db.scalars(select(models.AttendanceRecord).where(
        models.AttendanceRecord.employee_id.in_(emp_ids),
        models.AttendanceRecord.attendance_date >= first, models.AttendanceRecord.attendance_date <= last)).all()}
    leaves = db.execute(select(models.LeaveRequest.employee_id, models.LeaveRequest.from_date,
                               models.LeaveRequest.to_date).where(
        models.LeaveRequest.employee_id.in_(emp_ids),
        models.LeaveRequest.status.notin_(("rejected", "cancelled", "withdrawn")),
        models.LeaveRequest.from_date <= last, models.LeaveRequest.to_date >= first)).all()
    regs = {(r.employee_id, r.attendance_date) for r in db.execute(
        select(models.AttendanceRegularization.employee_id, models.AttendanceRegularization.attendance_date).where(
            models.AttendanceRegularization.employee_id.in_(emp_ids),
            models.AttendanceRegularization.status.in_(("pending", "approved", "l1_approved")),
            models.AttendanceRegularization.attendance_date >= first,
            models.AttendanceRegularization.attendance_date <= last)).all()}

    def excused(e, d) -> bool:
        weekend = d.weekday() == 6 if per_week >= 6 else d.weekday() >= 5
        if weekend:
            return True
        if any(h.holiday_date == d and (h.branch_id is None or h.branch_id == e.branch_id) for h in holidays):
            return True
        if any(lv.employee_id == e.id and lv.from_date <= d <= lv.to_date for lv in leaves):
            return True
        return (e.id, d) in regs

    for e in employees:
        for d in days:
            if e.date_of_joining is not None and d < e.date_of_joining:
                continue
            rec = records.get((e.id, d))
            is_auto = (rec is not None and rec.source == AUTO_ABSENT_SOURCE and rec.status == "absent"
                       and rec.check_in is None and rec.check_out is None)
            if excused(e, d):
                if is_auto and not float(rec.overtime_hours or 0):
                    db.delete(rec)
                    result["cleared"] += 1
                continue
            if rec is None:
                db.add(models.AttendanceRecord(
                    id=uuid.uuid4(), company_id=company_id, employee_id=e.id, attendance_date=d,
                    status="absent", source=AUTO_ABSENT_SOURCE))
                result["marked"] += 1
    db.flush()
    return result
