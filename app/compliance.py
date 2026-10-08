"""Missing Attendance & Work Compliance -- the EOD exception check.

At the company's configured EOD cutoff (hcm_compliance_settings, company
local time) each date is checked ONCE per company (hcm_compliance_runs) for
every eligible employee (active, joined by that date), from real data:

  missing_check_in      a working day (company calendar + branch holidays,
                        not on approved full-day leave) with no regular
                        check-in -- unless a pending / approved
                        regularization covers the date
  missing_check_out     checked in, never checked out -- same exemption
  missed_ot_clock_in    an approved overtime session never clocked in
  missed_ot_clock_out   an approved overtime session clocked in, never out
  missing_work_entry    a working day without any work entry, for employees
                        on an active project allocation that day
  missing_timesheet     the week's timesheet not submitted by the cutoff of
                        the week's last working day (same employees)

New exceptions are announced once: one EOD digest per reporting manager
(the employee's reporting + dotted-line manager, whatever their role) and
one to the employee (if enabled) -- in-app + email. Open exceptions resolve
themselves when the gap is closed later (regularization approved, the punch
or entry made, the timesheet submitted). Nothing here ever punches, creates
attendance, or changes Attendance / Overtime / Regularization / Work Entry /
Timesheet / Payroll data -- it only reads them.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import crud, models, overtime

logger = logging.getLogger(__name__)

TYPES = {
    "missing_check_in": "Missing Check In",
    "missing_check_out": "Missing Check Out",
    "missed_ot_clock_in": "Missed Overtime Clock In",
    "missed_ot_clock_out": "Missed Overtime Clock Out",
    "missing_work_entry": "Missing Work Entry",
    "missing_timesheet": "Missing Timesheet",
}
DEFAULTS = dict(enabled=True, eod_cutoff=datetime.time(23, 0), check_attendance=True, check_overtime=True,
                check_work_entry=True, check_timesheet=True, notify_employee=True,
                reminders_enabled=True, clock_in_reminder_minutes=15, end_reminder_minutes=15)
CATCH_UP_DAYS = 7  # a missed scheduler window is caught up, never older history


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def get_settings(db: Session, company_id: uuid.UUID) -> "models.ComplianceSettings":
    row = db.get(models.ComplianceSettings, company_id)
    return row if row is not None else models.ComplianceSettings(company_id=company_id, **DEFAULTS)


def cutoff_at(db: Session, company_id: uuid.UUID, day: datetime.date, settings=None) -> datetime.datetime:
    settings = settings or get_settings(db, company_id)
    tz = crud.company_tzinfo(db, company_id)
    return datetime.datetime.combine(day, settings.eod_cutoff).replace(tzinfo=tz).astimezone(datetime.timezone.utc)


# ── what each employee was expected to do on a date ────────────────────────

def _regularization(db: Session, employee_id: uuid.UUID, day: datetime.date):
    """The most relevant regularization for the date: approved, else pending,
    else the latest other one (rejected / sent back)."""
    rows = db.scalars(select(models.AttendanceRegularization).where(
        models.AttendanceRegularization.employee_id == employee_id,
        models.AttendanceRegularization.attendance_date == day)).all()
    for wanted in ("approved", "pending"):
        for r in rows:
            if r.status == wanted:
                return r
    return rows[-1] if rows else None


def _on_project(db: Session, employee_id: uuid.UUID, day: datetime.date) -> bool:
    return db.scalar(select(func.count()).select_from(models.ProjectAllocation).where(
        models.ProjectAllocation.employee_id == employee_id,
        models.ProjectAllocation.is_active.is_(True),
        models.ProjectAllocation.start_date <= day,
        or_(models.ProjectAllocation.end_date.is_(None), models.ProjectAllocation.end_date >= day))) > 0


def _week_bounds(day: datetime.date) -> tuple[datetime.date, datetime.date]:
    start = day - datetime.timedelta(days=day.weekday())
    return start, start + datetime.timedelta(days=6)


def _last_working_day_of_week(db, company_id, branch_id, day) -> datetime.date | None:
    start, end = _week_bounds(day)
    d = end
    while d >= start:
        if overtime._working_day(db, company_id, branch_id, d)[0]:
            return d
        d -= datetime.timedelta(days=1)
    return None


def _fmt_t(db, company_id, t: datetime.datetime | None) -> str:
    return t.astimezone(crud.company_tzinfo(db, company_id)).strftime("%d %b %Y %I:%M %p") if t else "—"


def find_gaps(db: Session, company_id: uuid.UUID, employee: "models.Employee", day: datetime.date,
              settings) -> list[dict]:
    """The exceptions [employee] has on [day], from real records."""
    gaps: list[dict] = []
    if employee.date_of_joining and employee.date_of_joining > day:
        return gaps
    working, why = overtime._working_day(db, company_id, employee.branch_id, day)
    leave = crud.get_blocking_leave_window(db, employee.id, day) if working else None
    on_full_leave = bool(leave and leave.get("full_day"))
    window = overtime._shift_window(db, company_id, employee.id, day) if working else None
    expected_label = (f"{window[2]} {_fmt_t(db, company_id, window[0])[-8:]} – {_fmt_t(db, company_id, window[1])[-8:]}"
                      if window else "Working day")

    if settings.check_attendance and working and not on_full_leave:
        rec = db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == employee.id,
            models.AttendanceRecord.attendance_date == day))
        reg = _regularization(db, employee.id, day)
        covered = reg is not None and reg.status in ("pending", "approved")
        if (rec is None or rec.check_in is None) and not covered:
            gaps.append(dict(type="missing_check_in", expected_label=expected_label,
                             expected_at=window[0] if window else None, actual_at=None, reg=reg,
                             details=f"No check-in on {day.strftime('%d %b %Y')} ({expected_label})."))
        elif rec is not None and rec.check_in is not None and rec.check_out is None and not covered:
            gaps.append(dict(type="missing_check_out", expected_label=expected_label,
                             expected_at=window[1] if window else None, actual_at=rec.check_in, reg=reg,
                             details=f"Checked in {_fmt_t(db, company_id, rec.check_in)}, never checked out."))

    if settings.check_overtime:
        for r in db.scalars(select(models.OvertimeRequest).where(
                models.OvertimeRequest.employee_id == employee.id,
                models.OvertimeRequest.work_date == day,
                models.OvertimeRequest.status == "approved",
                models.OvertimeRequest.planned_start.is_not(None))).all():
            overtime.check_request(db, company_id, r)  # bring the session state up to date (never punches)
            state = overtime.session_state(r)
            window_label = f"Overtime {_fmt_t(db, company_id, r.planned_start)[-8:]} – {_fmt_t(db, company_id, r.planned_end)[-8:]}"
            if state == overtime.MISSED_CLOCK_IN:
                gaps.append(dict(type="missed_ot_clock_in", ref=r.id, expected_label=window_label,
                                 expected_at=r.planned_start, actual_at=None, reg=None,
                                 details=f"Approved overtime ({float(r.hours):g} h) never clocked in."))
            elif state == overtime.MISSED_CLOCK_OUT:
                gaps.append(dict(type="missed_ot_clock_out", ref=r.id, expected_label=window_label,
                                 expected_at=r.planned_end, actual_at=r.actual_start, reg=None,
                                 details=f"Overtime clocked in {_fmt_t(db, company_id, r.actual_start)}, never clocked out."))

    on_project = (settings.check_work_entry or settings.check_timesheet) and _on_project(db, employee.id, day)
    if settings.check_work_entry and working and not on_full_leave and on_project:
        has_entry = db.scalar(select(func.count()).select_from(models.WorkEntry)
                              .join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
                              .where(models.Timesheet.employee_id == employee.id,
                                     models.WorkEntry.entry_date == day)) > 0
        if not has_entry:
            gaps.append(dict(type="missing_work_entry", expected_label="Work entry for the day", expected_at=None,
                             actual_at=None, reg=None, details=f"No work entry for {day.strftime('%d %b %Y')}."))

    if settings.check_timesheet and on_project and _last_working_day_of_week(db, company_id, employee.branch_id, day) == day:
        week_start, _ = _week_bounds(day)
        ts = db.scalar(select(models.Timesheet).where(
            models.Timesheet.employee_id == employee.id, models.Timesheet.week_start == week_start,
            models.Timesheet.is_active.is_(True)).limit(1))
        if ts is None or ts.submitted_at is None or ts.status in ("draft", "sent_back"):
            gaps.append(dict(type="missing_timesheet", ref_key=week_start.isoformat(), ref=ts.id if ts else None,
                             expected_label=f"Timesheet for week of {week_start.strftime('%d %b %Y')}",
                             expected_at=None, actual_at=None, reg=None,
                             details=f"Week of {week_start.strftime('%d %b %Y')}: timesheet "
                                     f"{'not started' if ts is None else ts.status.replace('_', ' ')}."))
    return gaps


def _reg_status(reg) -> str:
    return "none" if reg is None else reg.status


# ── one EOD pass ───────────────────────────────────────────────────────────

def eligible_employees(db: Session, company_id: uuid.UUID, day: datetime.date,
                       employee_ids: list[uuid.UUID] | None = None) -> list["models.Employee"]:
    q = select(models.Employee).where(models.Employee.company_id == company_id,
                                      models.Employee.is_active.is_(True))
    if employee_ids:
        q = q.where(models.Employee.id.in_(employee_ids))
    return [e for e in db.scalars(q).all() if not e.date_of_joining or e.date_of_joining <= day]


def process_day(db: Session, company_id: uuid.UUID, day: datetime.date, *,
                employee_ids: list[uuid.UUID] | None = None, now: datetime.datetime | None = None) -> list:
    """Records the day's new exceptions and announces them once. Returns the
    new exception rows. Re-running is harmless (unique per employee / date /
    type / reference; alerts only go out for rows created by this call)."""
    now = now or _now()
    settings = get_settings(db, company_id)
    new_rows: list[models.ComplianceException] = []
    for e in eligible_employees(db, company_id, day, employee_ids):
        for g in find_gaps(db, company_id, e, day, settings):
            key = g.get("ref_key") or (str(g["ref"]) if g.get("ref") else "")
            exists = db.scalar(select(models.ComplianceException.id).where(
                models.ComplianceException.employee_id == e.id,
                models.ComplianceException.exception_date == day,
                models.ComplianceException.exception_type == g["type"],
                models.ComplianceException.reference_key == key).limit(1))
            if exists:
                continue
            row = models.ComplianceException(
                id=uuid.uuid4(), company_id=company_id, employee_id=e.id, exception_date=day,
                exception_type=g["type"], reference_id=g.get("ref"), reference_key=key,
                expected_label=g["expected_label"], expected_at=g["expected_at"], actual_at=g["actual_at"],
                details=g["details"], regularization_id=g["reg"].id if g["reg"] else None,
                regularization_status=_reg_status(g["reg"]), status="open", created_at=now)
            db.add(row)
            new_rows.append(row)
    db.flush()
    if new_rows:
        _announce(db, company_id, day, new_rows, settings, now)
    return new_rows


def _announce(db: Session, company_id: uuid.UUID, day: datetime.date, rows: list, settings, now) -> None:
    """One digest per reporting manager and one per employee -- in-app + email."""
    from . import email_service

    by_employee: dict[uuid.UUID, list] = {}
    for r in rows:
        by_employee.setdefault(r.employee_id, []).append(r)
    by_manager: dict[uuid.UUID, list] = {}
    day_txt = day.strftime("%d %b %Y")

    def line(r, name):
        reg = (r.regularization_status or "none").replace("_", " ")
        return (f"{name}: {TYPES[r.exception_type]} — expected {r.expected_label or '—'}"
                f"{'; actual ' + _fmt_t(db, company_id, r.actual_at) if r.actual_at else ''}; regularization: {reg}")

    for emp_id, items in by_employee.items():
        employee = db.get(models.Employee, emp_id)
        name = f"{employee.first_name} {employee.last_name or ''}".strip()
        recipients = overtime.reminder_recipients(db, employee)
        managers = [x for x in recipients if x[2] == "manager"]
        for r in items:
            r.notification_status = "manager notified" if managers else "no reporting manager"
            if managers:
                r.manager_notified_at = now
        for user_id, email, _ in managers:
            by_manager.setdefault(user_id, []).append((email, name, items))
        if settings.notify_employee:
            own = next((x for x in recipients if x[2] == "employee"), None)
            if own is not None:
                body = "; ".join(line(r, "You") for r in items)
                crud.create_notification(db, company_id, own[0], f"Attendance exceptions — {day_txt}", body,
                                         entity_type="compliance_exception", entity_id=items[0].id)
                email_service.send_request_email(
                    own[1], subject=f"Attendance exceptions — {day_txt}", heading="Attendance exceptions",
                    intro_line=f"The following were missing at the end of {day_txt}. Raise a regularization or "
                               "complete the entry if it applies.",
                    details={TYPES[r.exception_type]: f"{r.expected_label or '—'} · regularization: "
                             f"{(r.regularization_status or 'none')}" for r in items},
                    entity_type="compliance_exception", entity_id=items[0].id, db=db, company_id=company_id,
                    email_type=email_service.GENERAL, idempotency_key=f"COMPLIANCE_EMP:{emp_id}:{day.isoformat()}",
                    cta_label="Open Attendance")
                for r in items:
                    r.employee_notified_at = now
                    r.notification_status = (r.notification_status or "") + ", employee notified"
    for manager_user, entries in by_manager.items():
        email = entries[0][0]
        lines = [line(r, name) for _e, name, items in entries for r in items]
        first = entries[0][2][0]
        crud.create_notification(db, company_id, manager_user, f"Team attendance exceptions — {day_txt}",
                                 " | ".join(lines)[:1900], entity_type="compliance_exception", entity_id=first.id)
        email_service.send_request_email(
            email, subject=f"Team attendance exceptions — {day_txt}", heading="Team attendance exceptions",
            intro_line=f"End-of-day exceptions for your reports on {day_txt}.",
            details={f"{i + 1}.": l for i, l in enumerate(lines)},
            entity_type="compliance_exception", entity_id=first.id, db=db, company_id=company_id,
            email_type=email_service.GENERAL, idempotency_key=f"COMPLIANCE_MGR:{manager_user}:{day.isoformat()}",
            cta_label="Open Reports")


def resolve_open(db: Session, company_id: uuid.UUID, now: datetime.datetime | None = None) -> int:
    """Open exceptions whose gap was closed later become resolved (with how)."""
    now = now or _now()
    changed = 0
    for r in db.scalars(select(models.ComplianceException).where(
            models.ComplianceException.company_id == company_id,
            models.ComplianceException.status == "open")).all():
        resolution = None
        reg = _regularization(db, r.employee_id, r.exception_date) if r.exception_type in (
            "missing_check_in", "missing_check_out") else None
        if reg is not None and _reg_status(reg) != r.regularization_status:
            r.regularization_id, r.regularization_status, r.updated_at = reg.id, reg.status, now
            changed += 1
        if r.exception_type in ("missing_check_in", "missing_check_out"):
            rec = db.scalar(select(models.AttendanceRecord).where(
                models.AttendanceRecord.employee_id == r.employee_id,
                models.AttendanceRecord.attendance_date == r.exception_date))
            if reg is not None and reg.status == "approved":
                resolution = f"Regularization approved{' ' + _fmt_t(db, company_id, reg.decided_at) if reg.decided_at else ''}"
            elif r.exception_type == "missing_check_in" and rec is not None and rec.check_in is not None:
                resolution = f"Check-in recorded {_fmt_t(db, company_id, rec.check_in)}"
            elif r.exception_type == "missing_check_out" and rec is not None and rec.check_out is not None:
                resolution = f"Check-out recorded {_fmt_t(db, company_id, rec.check_out)}"
        elif r.exception_type in ("missed_ot_clock_in", "missed_ot_clock_out") and r.reference_id:
            ot = db.get(models.OvertimeRequest, r.reference_id)
            if ot is not None and overtime.session_state(ot) == overtime.COMPLETED:
                resolution = f"Overtime clocked out {_fmt_t(db, company_id, ot.actual_end)} (late punch)"
        elif r.exception_type == "missing_work_entry":
            if db.scalar(select(func.count()).select_from(models.WorkEntry)
                         .join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
                         .where(models.Timesheet.employee_id == r.employee_id,
                                models.WorkEntry.entry_date == r.exception_date)) > 0:
                resolution = "Work entry submitted late"
        elif r.exception_type == "missing_timesheet":
            ts = db.scalar(select(models.Timesheet).where(
                models.Timesheet.employee_id == r.employee_id,
                models.Timesheet.week_start == datetime.date.fromisoformat(r.reference_key),
                models.Timesheet.submitted_at.is_not(None),
                models.Timesheet.status.notin_(("draft", "sent_back"))).limit(1)) if r.reference_key else None
            if ts is not None:
                resolution = f"Timesheet submitted {_fmt_t(db, company_id, ts.submitted_at)}"
        if resolution:
            r.status, r.resolved_at, r.resolution, r.updated_at = "resolved", now, resolution, now
            changed += 1
    db.flush()
    return changed


def due_dates(db: Session, company_id: uuid.UUID, now: datetime.datetime | None = None) -> list[datetime.date]:
    """Dates whose cutoff has passed and that haven't been processed yet
    (at most CATCH_UP_DAYS back; on the very first run only yesterday /
    today -- never a flood of old history)."""
    now = now or _now()
    settings = get_settings(db, company_id)
    today = crud.company_today(db, company_id)
    last = db.scalar(select(func.max(models.ComplianceRun.run_date)).where(
        models.ComplianceRun.company_id == company_id))
    start = (last + datetime.timedelta(days=1)) if last else today - datetime.timedelta(days=1)
    start = max(start, today - datetime.timedelta(days=CATCH_UP_DAYS))
    out = []
    d = start
    while d <= today:
        if now >= cutoff_at(db, company_id, d, settings):
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def run_company(db: Session, company_id: uuid.UUID, now: datetime.datetime | None = None) -> int:
    """Scheduler / on-read pass: resolve what was fixed, then process every
    due date once (guarded by an advisory lock + the run log)."""
    settings = get_settings(db, company_id)
    crud._advisory_lock(db, "compliance_run", str(company_id))
    # Already-recorded exceptions keep resolving even with the check switched off.
    total = resolve_open(db, company_id, now)
    if not settings.enabled:
        return total
    for day in due_dates(db, company_id, now):
        if db.get(models.ComplianceRun, (company_id, day)) is not None:
            continue
        rows = process_day(db, company_id, day, now=now)
        db.add(models.ComplianceRun(company_id=company_id, run_date=day, processed_at=now or _now(),
                                    exceptions=len(rows)))
        total += len(rows)
    db.flush()
    return total
