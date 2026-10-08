"""Leave policy: server-side rules for leave days, balances, leave types,
allocations and who administers / decides leave.

QA fixes (Impacgo HRMS issue list, section 5 + N-04/N-05/N-07):

* H-06  Requested days are computed here from the dates -- the company's
        working week (core_company_settings.working_days_per_week: 6 =
        Sunday off, otherwise Saturday + Sunday off, the same rule payroll
        and overtime use), mandatory holidays (company-wide or the
        employee's branch; optional holidays stay working days) and half
        day (0.5). A client-sent `days` that disagrees is rejected (422).
* H-07  Remaining balance = allocated + carried forward - used - pending
        (pending / l1_approved / sent_back requests of the same type in the
        same fiscal year). Re-checked when a request is finally approved.
        Negative balances are reported as negative, never clamped to 0.
* H-08  No allocation row: a capped type (max_days_per_year) is capped per
        fiscal year by approved usage in that year.
* H-09  Leave requests must name an EXISTING leave type (id or exact name);
        unknown -> 422. Admin-created types get a unique generated code.
* H-10  Leave types / allocations are configured only by a leave
        administrator (is_leave_admin) and never for oneself.
* M-08  Owner (or anyone) with nobody able to decide their leave: the
        fallback approvers are the usual company-wide approvers
        (crud.is_fallback_approver -- Owner, System Settings/RBAC editors,
        anyone given Approvals = Approve/Admin in Employee Permissions,
        which is the configurable "fallback approver" setting) PLUS the
        leave administrators. Only if nobody at all exists is the leave
        auto-approved, with an 'auto_approve' audit entry.
* N-04  ensure_allocations(): every active, eligible employee gets a
        current-fiscal-year allocation of each capped leave type (on
        employee create and from the reminders job, which also covers a
        new fiscal year). used_days starts at the approved usage already
        in that year; carry-forward types carry the previous year's
        positive balance.
* N-05  leave_slot(): Employee profile "CL/SL/EL/CO/LOP" buckets are
        recognised by code OR name, never by one hardcoded code.
* N-07  A granular leave approve/reject action alone no longer decides
        anyone's leave: outside the reporting chain only a leave
        administrator (Owner, or Admin on the Leave Approval column -- by
        role, Employee Permissions or Module Access) may decide.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Iterable

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from . import models

LEAVE_DOCTYPES = ("leave_request", "leave_withdrawal")
# Statuses that hold days without having consumed them yet.
PENDING_STATUSES = ("pending", "l1_approved", "sent_back")
# Statuses that never block re-applying for the same dates (M-06).
NON_BLOCKING_STATUSES = ("rejected", "cancelled", "withdrawn")

_EPS = 1e-9


class LeaveTypeNotFound(LookupError):
    """The named / given leave type doesn't exist in this company (-> 422)."""


# ── Who administers leave ────────────────────────────────────────────────

def is_leave_admin(db: Session, user: models.User | None) -> bool:
    """Org-wide leave administrator: Organization Owner, or Admin ('a') on
    the Leave Approval RBAC column (role matrix, Employee Permissions
    Leave = Admin, or an Employee Module Access grant)."""
    if user is None:
        return False
    from . import crud

    role = crud.get_user_primary_role(db, user.id)
    if role is not None and role.name == crud.BUILTIN_ROLES[0]:
        return True
    if role is None and user.employee_id is None:
        return False
    return crud.effective_user_matrix(db, user).get("leave_approval") == "a"


def leave_admin_employee_ids(
    db: Session, company_id: uuid.UUID, exclude_employee_id: uuid.UUID | None = None
) -> set[uuid.UUID]:
    users = db.scalars(select(models.User).where(
        models.User.company_id == company_id,
        models.User.employee_id.is_not(None),
        models.User.status == "active",
    )).all()
    return {u.employee_id for u in users if u.employee_id != exclude_employee_id and is_leave_admin(db, u)}


# ── Leave types ──────────────────────────────────────────────────────────

def find_leave_type(
    db: Session, company_id: uuid.UUID, *, leave_type_id: uuid.UUID | None = None,
    name: str | None = None, code: str | None = None,
) -> models.LeaveType | None:
    LT = models.LeaveType
    if leave_type_id is not None:
        return db.scalar(select(LT).where(LT.company_id == company_id, LT.id == leave_type_id))
    if name and name.strip():
        found = db.scalar(select(LT).where(
            LT.company_id == company_id, func.lower(func.trim(LT.name)) == " ".join(name.split()).lower()
        ).order_by(LT.id).limit(1))
        if found is not None:
            return found
    if code and code.strip():
        return db.scalar(select(LT).where(
            LT.company_id == company_id, func.upper(LT.code) == code.strip().upper()
        ).order_by(LT.id).limit(1))
    return None


def require_leave_type(
    db: Session, company_id: uuid.UUID, name: str | None = None,
    leave_type_id: uuid.UUID | None = None, code: str | None = None,
) -> models.LeaveType:
    lt = find_leave_type(db, company_id, leave_type_id=leave_type_id, name=name, code=code)
    if lt is None:
        label = name or code or (str(leave_type_id) if leave_type_id else "")
        raise LeaveTypeNotFound(
            f"Unknown leave type '{label}'. Pick one of the company's configured leave types."
        )
    return lt


def unique_code(db: Session, company_id: uuid.UUID, base: str, max_len: int = 10) -> str:
    """An unused leave type code derived from `base` (letters/digits,
    upper-case, <= max_len): BASE, then BASE2, BASE3, ... truncated so
    the suffix always fits -- names sharing their first 10 characters no
    longer collide."""
    stem = "".join(ch for ch in (base or "").upper() if ch.isalnum())[:max_len] or "LEAVE"
    taken = {c.upper() for c in db.scalars(
        select(models.LeaveType.code).where(models.LeaveType.company_id == company_id))}
    if stem not in taken:
        return stem
    n = 2
    while True:
        suffix = str(n)
        candidate = stem[: max_len - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


def leave_slot(lt: models.LeaveType) -> str | None:
    """N-05: which Employee-profile bucket (CL/SL/EL/CO/LOP) a leave type
    feeds, by code OR name -- 'CASUALLEAV' / 'Casual Leave' and 'CL' all
    map to CL."""
    code = (lt.code or "").strip().upper()
    name = " ".join((lt.name or "").lower().split())
    if code == "LOP" or "loss of pay" in name or name == "lop":
        return "LOP"
    if code in ("CL", "CASUALLEAV") or name.startswith("casual"):
        return "CL"
    if code in ("SL", "SKL") or name.startswith("sick"):
        return "SL"
    if code in ("EL", "PL") or name.startswith("earned") or name.startswith("privilege"):
        return "EL"
    if code in ("CO", "COMP", "COMPOFF") or name.startswith("comp"):
        return "CO"
    return None


def is_lop_type(lt: models.LeaveType | None) -> bool:
    return lt is not None and leave_slot(lt) == "LOP"


# ── Working days (H-06) ──────────────────────────────────────────────────

def _working_days_per_week(db: Session, company_id: uuid.UUID) -> int:
    return db.scalar(select(models.CompanySettings.working_days_per_week).where(
        models.CompanySettings.company_id == company_id)) or 5


def is_weekly_off(day: datetime.date, per_week: int) -> bool:
    return day.weekday() == 6 if per_week >= 6 else day.weekday() >= 5


def working_dates(
    db: Session, company_id: uuid.UUID, branch_id: uuid.UUID | None,
    from_date: datetime.date, to_date: datetime.date,
) -> list[datetime.date]:
    per_week = _working_days_per_week(db, company_id)
    holidays = set(db.scalars(select(models.Holiday.holiday_date).where(
        models.Holiday.company_id == company_id,
        models.Holiday.holiday_date >= from_date,
        models.Holiday.holiday_date <= to_date,
        models.Holiday.is_optional.is_not(True),
        or_(models.Holiday.branch_id.is_(None), models.Holiday.branch_id == branch_id),
    )).all())
    out, d = [], from_date
    while d <= to_date:
        if not is_weekly_off(d, per_week) and d not in holidays:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def compute_leave_days(
    db: Session, company_id: uuid.UUID, employee: models.Employee,
    from_date: datetime.date, to_date: datetime.date, is_half_day: bool,
) -> float:
    n = len(working_dates(db, company_id, employee.branch_id, from_date, to_date))
    if is_half_day:
        return 0.5 if n else 0.0
    return float(n)


def validate_requested_days(
    db: Session, company_id: uuid.UUID, employee: models.Employee,
    from_date: datetime.date, to_date: datetime.date, is_half_day: bool,
    client_days: float | None,
) -> float:
    """The server-computed day count; raises ValueError (-> 422) when the
    range has no working day or the client's figure disagrees."""
    days = compute_leave_days(db, company_id, employee, from_date, to_date, is_half_day)
    if days <= 0:
        raise ValueError(
            "The selected dates fall entirely on weekly offs / holidays -- there are no working days to take leave for."
        )
    if client_days is not None and abs(float(client_days) - days) > _EPS:
        raise ValueError(
            f"These dates cover {days:g} working day(s) (weekly offs and holidays excluded), "
            f"not {float(client_days):g}."
        )
    return days


# ── Fiscal year / balances (H-07, H-08) ──────────────────────────────────

def fiscal_window(db: Session, company_id: uuid.UUID, on_date: datetime.date) -> tuple[datetime.date, datetime.date]:
    from . import crud

    return crud._fiscal_year_window(db.get(models.Company, company_id), on_date)


def _today(db: Session, company_id: uuid.UUID) -> datetime.date:
    from . import crud

    return crud.company_today(db, company_id)


def _requested_days_in_window(
    db: Session, employee_id: uuid.UUID, leave_type_id: uuid.UUID, statuses: Iterable[str],
    start: datetime.date, end: datetime.date, exclude_ids: Iterable[uuid.UUID] = (),
) -> float:
    LR = models.LeaveRequest
    q = select(func.coalesce(func.sum(LR.days), 0)).where(
        LR.employee_id == employee_id, LR.leave_type_id == leave_type_id,
        LR.status.in_(tuple(statuses)), LR.from_date >= start, LR.from_date <= end,
    )
    exclude = [i for i in exclude_ids if i is not None]
    if exclude:
        q = q.where(LR.id.not_in(exclude))
    return float(db.scalar(q) or 0)


def allocation_for(
    db: Session, employee_id: uuid.UUID, leave_type_id: uuid.UUID, on_date: datetime.date,
) -> models.LeaveAllocation | None:
    LA, FY = models.LeaveAllocation, models.FiscalYear
    return db.scalar(
        select(LA).join(FY, FY.id == LA.fiscal_year_id).where(
            LA.employee_id == employee_id, LA.leave_type_id == leave_type_id,
            FY.start_date <= on_date, FY.end_date >= on_date,
        ).order_by(LA.id).limit(1)
    )


def balance(
    db: Session, employee_id: uuid.UUID, leave_type: models.LeaveType,
    on_date: datetime.date | None = None, exclude_request_ids: Iterable[uuid.UUID] = (),
) -> dict:
    """{allocated, carried_forward, used, pending, remaining} for the fiscal
    year covering on_date (default: today). remaining is None for an
    uncapped type, may be negative, and is 0 for a type the employee's
    employment type isn't eligible for."""
    from . import crud

    exclude = list(exclude_request_ids)
    employee = db.get(models.Employee, employee_id)
    company_id = leave_type.company_id
    on_date = on_date or _today(db, company_id)
    start, end = fiscal_window(db, company_id, on_date)
    out = {"allocated": 0.0, "carried_forward": 0.0, "used": 0.0, "pending": 0.0, "remaining": None}
    if employee is not None and not crud.leave_type_applies(employee, leave_type):
        out["remaining"] = 0.0
        return out
    if crud.is_earned_only_leave_type(db, leave_type):
        rows = db.scalars(select(models.LeaveAllocation).where(
            models.LeaveAllocation.employee_id == employee_id,
            models.LeaveAllocation.leave_type_id == leave_type.id)).all()
        pending = _requested_days_in_window(
            db, employee_id, leave_type.id, PENDING_STATUSES, datetime.date.min, datetime.date.max, exclude)
        out.update(
            allocated=sum(float(a.allocated_days or 0) for a in rows),
            carried_forward=sum(float(a.carried_forward_days or 0) for a in rows),
            used=sum(float(a.used_days or 0) for a in rows), pending=pending,
        )
        out["remaining"] = out["allocated"] + out["carried_forward"] - out["used"] - pending
        return out
    pending = _requested_days_in_window(db, employee_id, leave_type.id, PENDING_STATUSES, start, end, exclude)
    alloc = allocation_for(db, employee_id, leave_type.id, on_date)
    if alloc is not None:
        out.update(allocated=float(alloc.allocated_days or 0), carried_forward=float(alloc.carried_forward_days or 0),
                   used=float(alloc.used_days or 0), pending=pending)
        out["remaining"] = out["allocated"] + out["carried_forward"] - out["used"] - pending
        return out
    if leave_type.max_days_per_year is not None:
        used = _requested_days_in_window(db, employee_id, leave_type.id, ("approved",), start, end, exclude)
        out.update(allocated=float(leave_type.max_days_per_year), used=used, pending=pending)
        out["remaining"] = out["allocated"] - used - pending
        return out
    out["pending"] = pending
    return out


def remaining(
    db: Session, employee_id: uuid.UUID, leave_type: models.LeaveType,
    on_date: datetime.date | None = None, exclude_request_ids: Iterable[uuid.UUID] = (),
) -> float | None:
    return balance(db, employee_id, leave_type, on_date, exclude_request_ids)["remaining"]


def approval_shortfall(db: Session, requests: list[models.LeaveRequest]) -> str | None:
    """H-07 re-check at final approval: None if every leg still fits its
    balance (pending days of these very requests excluded), else a message."""
    group_ids = [r.id for r in requests]
    for r in requests:
        lt = r.leave_type or db.get(models.LeaveType, r.leave_type_id)
        if lt is None or is_lop_type(lt):
            continue
        left = remaining(db, r.employee_id, lt, r.from_date, group_ids)
        if left is not None and float(r.days) > left + _EPS:
            return (f"Not enough {lt.name} balance to approve this request: {float(r.days):g} day(s) requested, "
                    f"{max(left, 0):g} available after other approved and pending requests.")
    return None


# ── Allocations (N-04, M-07) ─────────────────────────────────────────────

def _allocatable_types(db: Session, company_id: uuid.UUID) -> list[models.LeaveType]:
    from . import crud

    types = db.scalars(select(models.LeaveType).where(
        models.LeaveType.company_id == company_id, models.LeaveType.max_days_per_year.is_not(None))).all()
    return [lt for lt in types if not is_lop_type(lt) and not crud.is_earned_only_leave_type(db, lt)]


def ensure_allocations(
    db: Session, company_id: uuid.UUID, employee_ids: Iterable[uuid.UUID] | None = None,
    on_date: datetime.date | None = None,
) -> int:
    """Creates the missing current-fiscal-year allocation of every capped
    leave type for every active, eligible employee (or just
    `employee_ids`). Idempotent; returns the number of rows created."""
    from . import crud

    types = _allocatable_types(db, company_id)
    if not types:
        return 0
    on_date = on_date or _today(db, company_id)
    E = models.Employee
    q = select(E).where(E.company_id == company_id, E.is_active.is_(True))
    if employee_ids is not None:
        ids = list(employee_ids)
        if not ids:
            return 0
        q = q.where(E.id.in_(ids))
    employees = db.scalars(q).all()
    if not employees:
        return 0
    fy = crud.get_or_create_fiscal_year(db, company_id, on_date)
    LA = models.LeaveAllocation
    emp_ids = [e.id for e in employees]
    have = set(db.execute(select(LA.employee_id, LA.leave_type_id).where(
        LA.fiscal_year_id == fy.id, LA.employee_id.in_(emp_ids))).all())
    missing = [(e, lt) for e in employees for lt in types
               if (e.id, lt.id) not in have and crud.leave_type_applies(e, lt)]
    if not missing:
        return 0
    need_emp = {e.id for e, _ in missing}
    LR = models.LeaveRequest
    used = {(eid, tid): float(d or 0) for eid, tid, d in db.execute(
        select(LR.employee_id, LR.leave_type_id, func.sum(LR.days)).where(
            LR.employee_id.in_(need_emp), LR.status == "approved",
            LR.from_date >= fy.start_date, LR.from_date <= fy.end_date,
        ).group_by(LR.employee_id, LR.leave_type_id)).all()}
    carry: dict = {}
    cf_type_ids = [lt.id for lt in types if lt.carry_forward]
    if cf_type_ids:
        FY = models.FiscalYear
        prev = db.scalar(select(FY).where(FY.company_id == company_id, FY.end_date < fy.start_date)
                         .order_by(FY.end_date.desc()).limit(1))
        if prev is not None:
            for a in db.scalars(select(LA).where(LA.fiscal_year_id == prev.id, LA.employee_id.in_(need_emp),
                                                 LA.leave_type_id.in_(cf_type_ids))):
                left = float(a.allocated_days or 0) + float(a.carried_forward_days or 0) - float(a.used_days or 0)
                carry[(a.employee_id, a.leave_type_id)] = max(0.0, left)
    created = 0
    for e, lt in missing:
        db.add(LA(
            id=uuid.uuid4(), employee_id=e.id, leave_type_id=lt.id, fiscal_year_id=fy.id,
            allocated_days=float(lt.max_days_per_year), carried_forward_days=carry.get((e.id, lt.id), 0.0),
            used_days=used.get((e.id, lt.id), 0.0),
        ))
        created += 1
    db.flush()
    return created


def allocate_for_new_employee(db: Session, company_id: uuid.UUID, employee: models.Employee) -> None:
    """N-04 hook for crud.create_employee -- never fails the employee insert."""
    import logging

    try:
        with db.begin_nested():
            ensure_allocations(db, company_id, [employee.id])
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Leave allocation for new employee %s failed", employee.id)


def run_company(db: Session, slug: str, company_id: uuid.UUID) -> int:
    """Reminders-job hook (HRMS tenants only): keeps every employee's
    current-fiscal-year allocations in place, which also rolls them over
    at fiscal-year start. Serialized per company by an xact advisory lock."""
    import zlib

    from . import auth_state

    if not auth_state.is_hrms_tenant(db, slug):
        return 0
    key = zlib.crc32(f"hrms-leave-alloc:{slug}:{company_id}".encode()) & 0x7FFFFFFF
    if not db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}).scalar():
        return 0
    return ensure_allocations(db, company_id)


# ── Approvers (M-08) ─────────────────────────────────────────────────────

def has_any_approver(
    db: Session, company_id: uuid.UUID, requester: models.Employee, doctype: str, document_id: uuid.UUID,
) -> bool:
    """Is there anyone other than the requester who can decide this?"""
    from . import approval_engine, crud

    if crud._has_custom_approval_workflow(db, company_id, doctype):
        request = crud._get_or_start_approval_request(db, company_id, doctype, document_id, requester.id)
        if request.status in approval_engine.TERMINAL_STATUSES:
            return True
        if approval_engine.current_step_approver_employee_ids(db, request, requester):
            return True
    else:
        if {requester.reporting_manager_id, requester.dotted_line_manager_id} - {None, requester.id}:
            return True
        if crud.list_fallback_approver_employee_ids(db, company_id, exclude_employee_id=requester.id):
            return True
    return bool(leave_admin_employee_ids(db, company_id, exclude_employee_id=requester.id))


AUTO_APPROVE_NOTE = ("Auto-approved: nobody else can decide this employee's leave "
                     "(no reporting manager, fallback approver or leave administrator is configured).")


def auto_approve(db: Session, requests: list[models.LeaveRequest], actor: models.User) -> None:
    """M-08 last resort: approve the request group, consume the balance and
    leave an 'auto_approve' audit entry per leg."""
    from . import approval_engine, crud

    now = datetime.datetime.now(datetime.timezone.utc)
    for r in requests:
        r.status = "approved"
        r.approver_id = None
        r.decision_notes = AUTO_APPROVE_NOTE
        r.approved_at = now
        crud.adjust_leave_allocation_used(db, r.employee_id, r.leave_type_id, float(r.days), on_date=r.from_date)
        from . import attendance_rules

        attendance_rules.clear_auto_absences(db, r.employee_id, r.from_date, r.to_date)
        approval_engine.close_request(db, r.company_id, "leave_request", r.id, actor,
                                      status="approved", comments=AUTO_APPROVE_NOTE)
        crud.create_audit_log(db, r.company_id, actor.id, "auto_approve", "leave_request", r.id,
                              changes={"reason": AUTO_APPROVE_NOTE})
    db.flush()
