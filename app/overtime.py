"""Overtime: request -> approval -> manual punches -> compensation.

Flow: Overtime Request (date, start time, end time; hours = end - start) ->
Approval -> session SCHEDULED -> the employee clocks in (Overtime Clock In,
from the approved start time) and clocks out (Overtime Clock Out) -> actual
worked minutes from the real punches -> Compensation, exactly once:

  * payable  -- an OvertimeCompensation row (pending_payroll) that the next
    payroll run generated for the work date's month or later picks up as an
    "Overtime Pay" earning (crud._compute_and_write_slip); regenerating a
    draft run releases and re-applies it, never doubles it.
  * comp_off -- days credited to the employee's comp-off LeaveAllocation for
    the fiscal year of the work date.

Compensation counts the actual minutes worked, capped at the approved
duration. Nothing is ever clocked in or out automatically: if the employee
hasn't clocked in PUNCH_GRACE_MINUTES after the approved start, the session
becomes MISSED_CLOCK_IN and the employee and their reporting manager(s) get
an in-app reminder + email; likewise MISSED_CLOCK_OUT after the approved
end. A missed punch can still be made late (it is then recorded as it
happened). Settings (hcm_overtime_settings) decide the compensation mode at
the moment a session completes; the compensation row stores what was used,
so later setting changes never rewrite history. hcm_overtime_compensations.
overtime_request_id is UNIQUE.
"""

from __future__ import annotations

import datetime
import math
import uuid
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import crud, models

DEFAULTS = {
    "enabled": True,
    "compensation_mode": "payable",
    "rate_basis": "basic",
    "rate_multiplier": 2.0,
    "monthly_days_divisor": 26,
    "hours_per_day": 8.0,
    "fixed_hourly_rate": None,
    "comp_off_leave_type_id": None,
    "comp_off_full_day_hours": 8.0,
    "comp_off_half_day_hours": 4.0,
    "min_minutes": 30,
    "rounding": "exact",
}

OT_PAY_CODE = "OT-PAY"


# ── settings ────────────────────────────────────────────────────────────────

def get_settings(db: Session, company_id: uuid.UUID) -> "models.OvertimeSettings":
    """The company's saved settings, or an unsaved row holding the defaults."""
    row = db.get(models.OvertimeSettings, company_id)
    if row is None:
        row = models.OvertimeSettings(company_id=company_id, **DEFAULTS)
    return row


def default_comp_off_leave_type(db: Session, company_id: uuid.UUID) -> "models.LeaveType | None":
    """The company's Compensatory Off leave type (codes differ by tenant:
    CO / COMP / COFF, or a name containing "comp")."""
    types = db.scalars(select(models.LeaveType).where(models.LeaveType.company_id == company_id)).all()
    for t in types:
        if (t.code or "").upper() in ("CO", "COMP", "COFF", "COMPOFF"):
            return t
    return next((t for t in types if "comp" in (t.name or "").lower()), None)


def comp_off_leave_type(db: Session, company_id: uuid.UUID, settings) -> "models.LeaveType | None":
    if settings.comp_off_leave_type_id:
        t = db.get(models.LeaveType, settings.comp_off_leave_type_id)
        if t is not None and t.company_id == company_id:
            return t
    return default_comp_off_leave_type(db, company_id)


# ── session window ─────────────────────────────────────────────────────────

SCHEDULED, CLOCKED_IN, MISSED_CLOCK_IN, MISSED_CLOCK_OUT, COMPLETED = (
    "scheduled", "clocked_in", "missed_clock_in", "missed_clock_out", "completed")
OPEN_STATES = (SCHEDULED, CLOCKED_IN, MISSED_CLOCK_IN, MISSED_CLOCK_OUT, "in_progress")
# Minutes after the approved start / end before a punch counts as missed
# and the reminder goes out.
PUNCH_GRACE_MINUTES = 10


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def planned_window(db: Session, company_id: uuid.UUID, work_date: datetime.date,
                   start_time: datetime.time, hours: float) -> tuple[datetime.datetime, datetime.datetime]:
    """Approved window in UTC: start (company local time on the work date)
    and start + the approved duration (an end time past midnight lands on
    the next day)."""
    tz = crud.company_tzinfo(db, company_id)
    start = datetime.datetime.combine(work_date, start_time).replace(tzinfo=tz).astimezone(datetime.timezone.utc)
    return start, start + datetime.timedelta(minutes=round(float(hours) * 60))


def hours_between(start_time: datetime.time, end_time: datetime.time) -> float:
    """Requested hours from Start / End Time; an end at or before the start
    means the next day (e.g. 22:00 -> 01:00 = 3 h)."""
    a = start_time.hour * 60 + start_time.minute
    b = end_time.hour * 60 + end_time.minute
    minutes = b - a if b > a else b + 24 * 60 - a
    return round(minutes / 60, 2)


def end_time_of(db: Session, request: "models.OvertimeRequest") -> datetime.time | None:
    """The request's End Time; for rows made before End Time was stored, the
    end of its approved window (company local time)."""
    if request.end_time is not None:
        return request.end_time
    if request.planned_end is None:
        return None
    employee = db.get(models.Employee, request.employee_id)
    tz = crud.company_tzinfo(db, employee.company_id) if employee else datetime.timezone.utc
    return request.planned_end.astimezone(tz).time()


def session_state(request: "models.OvertimeRequest") -> str | None:
    """Normalised state (legacy auto sessions' "in_progress" = clocked in)."""
    return CLOCKED_IN if request.session_status == "in_progress" else request.session_status


def start_session_on_approval(db: Session, company_id: uuid.UUID, request: "models.OvertimeRequest",
                              now: datetime.datetime | None = None) -> None:
    """Called once when a request becomes approved: the session is
    SCHEDULED -- it starts only when the employee clocks in. Requests
    without a start time (older clients) keep the previous behaviour: hours
    are added to the day's attendance at once and nothing is compensated."""
    if request.start_time is None:
        crud.apply_overtime_to_attendance_record(db, company_id, request)
        return
    request.planned_start, request.planned_end = planned_window(
        db, company_id, request.work_date, request.start_time, float(request.hours))
    request.session_status = SCHEDULED
    # Approved after the start already passed: the reminder is due now.
    check_request(db, company_id, request, now=now)


def clock_in(db: Session, company_id: uuid.UUID, request: "models.OvertimeRequest",
             now: datetime.datetime | None = None) -> None:
    """Overtime Clock In: from the approved start time until the approved
    end; one open session per employee at a time."""
    now = now or _utcnow()
    state = session_state(request)
    if request.status != "approved" or state not in (SCHEDULED, MISSED_CLOCK_IN):
        raise ValueError("This overtime session can't be clocked in to "
                         f"({(state or request.status).replace('_', ' ')}).")
    if now < request.planned_start:
        local = request.planned_start.astimezone(crud.company_tzinfo(db, company_id))
        raise ValueError(f"Overtime Clock In opens at the approved start time, "
                         f"{local.strftime('%d %b %Y %I:%M %p')}.")
    if now >= request.planned_end:
        raise ValueError("The approved overtime window has ended -- this session can no longer be clocked in to.")
    crud._advisory_lock(db, "overtime_punch", str(request.employee_id))
    other = db.scalar(select(models.OvertimeRequest).where(
        models.OvertimeRequest.employee_id == request.employee_id,
        models.OvertimeRequest.id != request.id,
        models.OvertimeRequest.session_status.in_((CLOCKED_IN, MISSED_CLOCK_OUT, "in_progress")),
    ).limit(1))
    if other is not None:
        raise ValueError(f"You're still clocked in to overtime on {other.work_date.strftime('%d %b %Y')} "
                         "-- clock out of it first.")
    request.actual_start = now
    request.session_status = CLOCKED_IN


def clock_out(db: Session, company_id: uuid.UUID, request: "models.OvertimeRequest",
              actor_employee_id: uuid.UUID | None, now: datetime.datetime | None = None) -> None:
    """Overtime Clock Out: records the real end, worked minutes and the
    compensation (capped at the approved duration)."""
    now = now or _utcnow()
    if session_state(request) not in (CLOCKED_IN, MISSED_CLOCK_OUT):
        raise ValueError("Only an overtime session you are clocked in to can be clocked out.")
    request.ended_early_by = actor_employee_id if now < request.planned_end else None
    _complete(db, company_id, request, now)


def end_early(db: Session, company_id: uuid.UUID, request: "models.OvertimeRequest",
              actor_employee_id: uuid.UUID | None, now: datetime.datetime | None = None) -> None:
    """Kept for the existing "End" action: the same as Overtime Clock Out."""
    clock_out(db, company_id, request, actor_employee_id, now=now)


def approved_minutes(request: "models.OvertimeRequest") -> int:
    return round(float(request.hours) * 60)


def _complete(db: Session, company_id: uuid.UUID, request: "models.OvertimeRequest",
              end: datetime.datetime) -> None:
    start = request.actual_start or request.planned_start
    request.actual_end = end
    request.actual_minutes = max(0, int((end - start).total_seconds() // 60))
    request.session_status = COMPLETED
    # Attendance shows the overtime counted (worked, up to the approved hours).
    _add_attendance_overtime(db, company_id, request, min(request.actual_minutes, approved_minutes(request)) / 60)
    comp = process_compensation(db, company_id, request)
    if comp is not None and request.planned_start is not None and comp.calculation:
        tz = crud.company_tzinfo(db, company_id)
        comp.calculation = (f"{comp.calculation} · approved {request.planned_start.astimezone(tz).strftime('%H:%M')}–"
                            f"{request.planned_end.astimezone(tz).strftime('%H:%M')}, worked "
                            f"{request.actual_start.astimezone(tz).strftime('%H:%M') if request.actual_start else '—'}–"
                            f"{request.actual_end.astimezone(tz).strftime('%H:%M')}")
        db.flush()


def _add_attendance_overtime(db: Session, company_id: uuid.UUID, request, hours: float) -> None:
    record = db.scalar(
        select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == request.employee_id,
            models.AttendanceRecord.attendance_date == request.work_date,
        ).with_for_update()
    )
    if record is None:
        # Overtime only (pre-shift, holiday, weekly off ...): an "overtime"
        # record, never a regular present day -- it carries no check-in /
        # work hours, so overtime is never counted as shift working time.
        record = models.AttendanceRecord(
            id=uuid.uuid4(), company_id=company_id, employee_id=request.employee_id,
            attendance_date=request.work_date, status="overtime", source="overtime",
        )
        db.add(record)
    record.overtime_hours = round(float(record.overtime_hours or 0) + hours, 2)
    db.flush()


# ── shift / calendar rule: overtime is always outside the shift ───────────

def _working_day(db: Session, company_id: uuid.UUID, branch_id, day: datetime.date) -> tuple[bool, str | None]:
    """(is a working day, why not) from the real calendar: the company's
    working days per week (same rule as payroll) and company / branch
    holidays."""
    holiday = db.scalar(select(models.Holiday).where(
        models.Holiday.company_id == company_id, models.Holiday.holiday_date == day,
        or_(models.Holiday.branch_id.is_(None), models.Holiday.branch_id == branch_id)).limit(1))
    if holiday is not None:
        return False, f"Holiday — {holiday.name}"
    settings_row = crud.get_company_settings(db, company_id)
    per_week = (settings_row.working_days_per_week if settings_row else None) or 5
    weekend = day.weekday() == 6 if per_week >= 6 else day.weekday() >= 5
    if weekend:
        return False, "Weekly off"
    return True, None


def _shift_window(db: Session, company_id: uuid.UUID, employee_id: uuid.UUID, day: datetime.date):
    """The employee's regular working window on [day] in UTC -- their
    assigned shift active that day (a night shift ends the next day), else
    the company office hours -- with a label; None if neither is set."""
    tz = crud.company_tzinfo(db, company_id)
    shift = crud._get_active_shift(db, employee_id, day)
    if shift is not None:
        start_t, end_t, label = shift.start_time, shift.end_time, shift.name
    else:
        settings_row = crud.get_company_settings(db, company_id)
        if not settings_row or not settings_row.working_hours_start or not settings_row.working_hours_end:
            return None
        start_t, end_t, label = settings_row.working_hours_start, settings_row.working_hours_end, "Office hours"
    start = datetime.datetime.combine(day, start_t).replace(tzinfo=tz)
    end = datetime.datetime.combine(day, end_t).replace(tzinfo=tz)
    if end <= start:
        end += datetime.timedelta(days=1)
    return start.astimezone(datetime.timezone.utc), end.astimezone(datetime.timezone.utc), label


def day_context(db: Session, company_id: uuid.UUID, employee: "models.Employee", work_date: datetime.date) -> dict:
    """What an overtime request on [work_date] is checked against: whether
    it's a working day (holiday / weekly off otherwise) and the regular
    shift windows of the day before, the day and the day after (so a night
    shift or a late session past midnight is covered too)."""
    working, why = _working_day(db, company_id, employee.branch_id, work_date)
    windows = []
    for d in (work_date - datetime.timedelta(days=1), work_date, work_date + datetime.timedelta(days=1)):
        if d != work_date:
            ok, _ = _working_day(db, company_id, employee.branch_id, d)
            if not ok:
                continue
        elif not working:
            continue
        w = _shift_window(db, company_id, employee.id, d)
        if w is not None:
            windows.append({"date": d, "start": w[0], "end": w[1], "label": w[2]})
    return {"working_day": working, "day_type": "working" if working else ("holiday" if why and why.startswith("Holiday") else "weekly_off"),
            "reason": why, "windows": windows}


def shift_overlap_error(db: Session, company_id: uuid.UUID, employee: "models.Employee",
                        work_date: datetime.date, start: datetime.datetime, end: datetime.datetime) -> str | None:
    """None when [start, end) is entirely outside the employee's regular
    shift(s); otherwise a message saying which shift it overlaps."""
    tz = crud.company_tzinfo(db, company_id)
    for w in day_context(db, company_id, employee, work_date)["windows"]:
        if start < w["end"] and w["start"] < end:
            a, b = w["start"].astimezone(tz), w["end"].astimezone(tz)
            return (f"Overtime can't overlap the regular shift — {w['label']} "
                    f"({a.strftime('%I:%M %p')} – {b.strftime('%I:%M %p')}) on {w['date'].strftime('%d %b %Y')}. "
                    f"Choose a time before {a.strftime('%I:%M %p')} or after {b.strftime('%I:%M %p')}.")
    return None


# ── missed punches: reminders, never automatic punches ─────────────────────

def reminder_recipients(db: Session, employee: "models.Employee") -> list[tuple[uuid.UUID, str | None, str]]:
    """(user id, email, role) for the employee and their configured
    reporting manager(s) -- whoever the hierarchy names (reporting_manager_id
    and dotted_line_manager_id), whatever their role."""
    out = []
    seen = set()
    for emp_id, role in ((employee.id, "employee"), (employee.reporting_manager_id, "manager"),
                         (employee.dotted_line_manager_id, "manager")):
        if emp_id is None or emp_id in seen:
            continue
        seen.add(emp_id)
        person = db.get(models.Employee, emp_id)
        user_id = crud.get_user_id_for_employee(db, emp_id)
        if person is None or user_id is None:
            continue
        out.append((user_id, person.work_email, role))
    return out


def _remind(db: Session, company_id: uuid.UUID, request: "models.OvertimeRequest", kind: str) -> None:
    from . import email_service  # local import: email_service imports models

    employee = db.get(models.Employee, request.employee_id)
    if employee is None:
        return
    tz = crud.company_tzinfo(db, company_id)
    name = f"{employee.first_name} {employee.last_name or ''}".strip()
    window = (f"{request.planned_start.astimezone(tz).strftime('%I:%M %p')} – "
              f"{request.planned_end.astimezone(tz).strftime('%I:%M %p')}")
    day = request.work_date.strftime("%d %b %Y")
    if kind == MISSED_CLOCK_IN:
        title = "Overtime Clock In missed"
        own = (f"You haven't clocked in to your approved overtime on {day} ({window}). "
               "Use Overtime Clock In in Attendance if you are working it.")
        mgr = f"{name} hasn't clocked in to approved overtime on {day} ({window})."
    else:
        title = "Overtime Clock Out missed"
        own = (f"You haven't clocked out of your overtime on {day} (approved until "
               f"{request.planned_end.astimezone(tz).strftime('%I:%M %p')}). Use Overtime Clock Out in Attendance.")
        mgr = (f"{name} hasn't clocked out of overtime on {day} (approved until "
               f"{request.planned_end.astimezone(tz).strftime('%I:%M %p')}).")
    details = {"Employee": name, "Work Date": day, "Approved Window": window,
               "Status": "Missed Clock In" if kind == MISSED_CLOCK_IN else "Missed Clock Out"}
    for user_id, email, role in reminder_recipients(db, employee):
        body = own if role == "employee" else mgr
        crud.create_notification(db, company_id, user_id, title, body,
                                 entity_type="overtime_request", entity_id=request.id)
        email_service.send_request_email(
            email, subject=f"{title} — {name}, {day}", heading=title, intro_line=body, details=details,
            entity_type="overtime_request", entity_id=request.id, db=db, company_id=company_id,
            email_type=email_service.GENERAL, cta_label="Open Attendance",
            idempotency_key=f"OVERTIME_{kind.upper()}:{request.id}",
        )


def check_request(db: Session, company_id: uuid.UUID, request: "models.OvertimeRequest",
                  now: datetime.datetime | None = None) -> bool:
    """Marks a missed punch and sends its reminder ONCE. Never punches."""
    if request.status != "approved" or request.planned_start is None:
        return False
    now = now or _utcnow()
    grace = datetime.timedelta(minutes=PUNCH_GRACE_MINUTES)
    state = session_state(request)
    if state == SCHEDULED and now >= request.planned_start + grace:
        request.session_status = MISSED_CLOCK_IN
        if request.missed_in_notified_at is None:
            request.missed_in_notified_at = now
            _remind(db, company_id, request, MISSED_CLOCK_IN)
        return True
    if state == CLOCKED_IN and now >= request.planned_end + grace:
        request.session_status = MISSED_CLOCK_OUT
        if request.missed_out_notified_at is None:
            request.missed_out_notified_at = now
            _remind(db, company_id, request, MISSED_CLOCK_OUT)
        return True
    return False


def advance_sessions(db: Session, company_id: uuid.UUID, now: datetime.datetime | None = None) -> int:
    """Every open session of the company: missed punches + reminders
    (scheduler pass + on read). Idempotent; never clocks in or out."""
    now = now or _utcnow()
    rows = db.scalars(
        select(models.OvertimeRequest)
        .join(models.Employee, models.Employee.id == models.OvertimeRequest.employee_id)
        .where(
            models.Employee.company_id == company_id,
            models.OvertimeRequest.status == "approved",
            models.OvertimeRequest.session_status.in_((SCHEDULED, CLOCKED_IN, "in_progress")),
            models.OvertimeRequest.planned_start <= now,
        )
        .with_for_update(of=models.OvertimeRequest, skip_locked=True)
    ).all()
    return sum(1 for r in rows if check_request(db, company_id, r, now=now))


# ── compensation ───────────────────────────────────────────────────────────

def counted_minutes(worked: int, settings) -> int:
    rule = settings.rounding or "exact"
    if rule == "nearest_15":
        worked = int(round(worked / 15) * 15)
    elif rule == "nearest_30":
        worked = int(round(worked / 30) * 30)
    elif rule == "floor_30":
        worked = int(worked // 30 * 30)
    return worked if worked >= int(settings.min_minutes or 0) else 0


def hourly_rate(db: Session, company_id: uuid.UUID, employee_id: uuid.UUID, on_date: datetime.date,
                settings) -> tuple[float | None, str]:
    """(ordinary hourly rate, how it was worked out) from the employee's
    salary structure in force on [on_date]; None when there is none."""
    if settings.rate_basis == "fixed":
        rate = float(settings.fixed_hourly_rate or 0)
        return (rate if rate > 0 else None), f"fixed rate {crud._inr(rate)}/hour"
    assignment = crud._resolve_assignment_as_of(db, employee_id, on_date)
    if assignment is None or assignment.annual_ctc is None:
        return None, "no salary structure assigned"
    lines = db.scalars(select(models.SalaryStructureLine).where(
        models.SalaryStructureLine.structure_id == assignment.structure_id)).all()
    resolved = crud._resolve_structure_lines_monthly(
        lines, float(assignment.annual_ctc), crud.get_assignment_overrides_map(db, assignment.id),
        crud.get_company_pf_wage_ceiling(db, company_id))
    earnings = [i for i in resolved if i["component"].component_type == "earning"
                and i["component"].calc_type != "var_annual"]
    if settings.rate_basis == "gross":
        monthly, label = sum(float(i["amount"]) for i in earnings), "monthly gross"
    else:
        basic = next((i for i in earnings if (i["component"].code or "").upper() == "BASIC"
                      or "basic" in (i["component"].name or "").lower()), None)
        if basic is None and earnings:
            basic = earnings[0]
        monthly, label = (float(basic["amount"]) if basic else 0.0), "monthly basic"
    divisor = int(settings.monthly_days_divisor) * float(settings.hours_per_day)
    if monthly <= 0 or divisor <= 0:
        return None, f"{label} is zero"
    return monthly / divisor, (f"{label} {crud._inr(monthly)} ÷ ({settings.monthly_days_divisor} days × "
                               f"{float(settings.hours_per_day):g} h)")


def comp_off_days(minutes: int, settings) -> float:
    full = float(settings.comp_off_full_day_hours) * 60
    half = float(settings.comp_off_half_day_hours) * 60
    days = math.floor(minutes / full)
    remainder = minutes - days * full
    return days + (0.5 if remainder >= half else 0.0)


def process_compensation(db: Session, company_id: uuid.UUID, request: "models.OvertimeRequest") -> "models.OvertimeCompensation | None":
    """Compensates one completed, approved session exactly once."""
    if request.status != "approved" or request.session_status != "completed":
        return None
    existing = db.scalar(select(models.OvertimeCompensation).where(
        models.OvertimeCompensation.overtime_request_id == request.id))
    if existing is not None:
        return existing
    settings = get_settings(db, company_id)
    worked = int(request.actual_minutes or 0)
    # Paid / credited up to the approved duration, never beyond it.
    counted = counted_minutes(min(worked, approved_minutes(request)), settings) if settings.enabled else 0
    row = models.OvertimeCompensation(
        id=uuid.uuid4(), company_id=company_id, overtime_request_id=request.id,
        employee_id=request.employee_id, mode=settings.compensation_mode, worked_minutes=worked,
        counted_minutes=counted, created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    hours = counted / 60
    if not settings.enabled:
        row.status, row.calculation = "skipped", "Overtime compensation is turned off in Overtime Settings."
    elif counted == 0:
        row.status = "skipped"
        row.calculation = f"{fmt_minutes(worked)} worked is below the {settings.min_minutes}-minute minimum."
    elif settings.compensation_mode == "payable":
        rate, basis = hourly_rate(db, company_id, request.employee_id, request.work_date, settings)
        if rate is None:
            row.status, row.calculation = "skipped", f"Cannot calculate overtime pay: {basis}."
        else:
            multiplier = 1.0 if settings.rate_basis == "fixed" else float(settings.rate_multiplier)
            amount = Decimal(str(rate * multiplier * hours)).quantize(Decimal("0.01"))
            row.hourly_rate = Decimal(str(rate)).quantize(Decimal("0.01"))
            row.rate_multiplier = multiplier
            row.amount = amount
            row.target_period_month = request.work_date.month
            row.target_period_year = request.work_date.year
            row.status = "pending_payroll"
            row.calculation = (f"{fmt_minutes(counted)} × {crud._inr(rate)}/hour ({basis})"
                               + (f" × {multiplier:g}" if multiplier != 1 else "") + f" = {crud._inr(amount)}")
    else:
        leave_type = comp_off_leave_type(db, company_id, settings)
        days = comp_off_days(counted, settings)
        if leave_type is None:
            row.status, row.calculation = "skipped", "No compensatory leave type is set in Overtime Settings."
        elif days == 0:
            row.status = "skipped"
            row.calculation = (f"{fmt_minutes(counted)} is below the {float(settings.comp_off_half_day_hours):g}-hour "
                               "half-day comp-off threshold.")
        else:
            fiscal_year = crud.get_or_create_fiscal_year(db, company_id, request.work_date)
            allocation = db.scalar(select(models.LeaveAllocation).where(
                models.LeaveAllocation.employee_id == request.employee_id,
                models.LeaveAllocation.leave_type_id == leave_type.id,
                models.LeaveAllocation.fiscal_year_id == fiscal_year.id,
            ).with_for_update())
            if allocation is None:
                allocation = crud.create_leave_allocation(db, request.employee_id, leave_type.id, fiscal_year.id, 0)
            already = float(allocation.allocated_days or 0)
            earned_text = (f"{fmt_minutes(counted)} → {days:g} day(s) of {leave_type.name} "
                           f"({float(settings.comp_off_full_day_hours):g} h = 1 day, "
                           f"{float(settings.comp_off_half_day_hours):g} h = ½ day)")
            # The leave type's "Max days per year" caps what overtime can
            # earn in one fiscal year.
            cap = float(leave_type.max_days_per_year) if leave_type.max_days_per_year is not None else None
            credit = days if cap is None else max(0.0, min(days, cap - already))
            if credit <= 0:
                row.status = "skipped"
                row.calculation = (f"{earned_text}, but the yearly limit of {cap:g} {leave_type.name} day(s) "
                                   f"is already reached ({already:g} credited this year).")
            else:
                allocation.allocated_days = already + credit
                row.leave_type_id, row.leave_days, row.leave_allocation_id = leave_type.id, credit, allocation.id
                row.status = "credited"
                row.calculation = earned_text if credit == days else (
                    f"{earned_text}; {credit:g} credited -- the yearly limit of {cap:g} day(s) is reached.")
    db.add(row)
    request.compensation_status = "skipped" if row.status == "skipped" else "processed"
    db.flush()
    return row


# ── payroll ────────────────────────────────────────────────────────────────

def payroll_items(db: Session, employee_id: uuid.UUID, run: "models.PayrollRun") -> list["models.OvertimeCompensation"]:
    """Payable overtime for [run]: every pending item from the run's month or
    earlier not taken by a DIFFERENT run (this run's own items stay eligible,
    so regenerating a draft run is idempotent)."""
    return db.scalars(select(models.OvertimeCompensation).where(
        models.OvertimeCompensation.employee_id == employee_id,
        models.OvertimeCompensation.mode == "payable",
        models.OvertimeCompensation.status.in_(("pending_payroll", "in_payroll")),
        (models.OvertimeCompensation.target_period_year * 100 + models.OvertimeCompensation.target_period_month)
        <= run.period_year * 100 + run.period_month,
        or_(models.OvertimeCompensation.applied_payroll_run_id.is_(None),
            models.OvertimeCompensation.applied_payroll_run_id == run.id),
    )).all()


def release_run(db: Session, run_id: uuid.UUID) -> None:
    """Before a run is (re)generated: its overtime items become pending again."""
    for item in db.scalars(select(models.OvertimeCompensation).where(
            models.OvertimeCompensation.applied_payroll_run_id == run_id)):
        item.applied_payroll_run_id = None
        item.applied_at = None
        item.status = "pending_payroll"


def ensure_ot_component(db: Session, company_id: uuid.UUID) -> "models.SalaryComponent":
    existing = db.scalar(select(models.SalaryComponent).where(
        models.SalaryComponent.company_id == company_id, models.SalaryComponent.code == OT_PAY_CODE))
    if existing is not None:
        return existing
    return crud.create_salary_component(db, company_id, "Overtime Pay", OT_PAY_CODE, "earning", "flat", True)


# ── display ────────────────────────────────────────────────────────────────

def fmt_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    return f"{minutes // 60}h {minutes % 60:02d}m"
