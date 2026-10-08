"""Payroll period control and the batch payslip engine.

Run lifecycle (H-11), stored in hcm_payroll_runs.status:

    draft --Generate--> processed --Approve--> approved --Lock--> locked --Mark paid--> paid
                  ^ regenerate (resets an approval) |

* Approve: strict two-person rule -- the approver must not be the user who
  last generated the run (no Owner exception).
* Lock: freezes the period. Loan EMIs recovered on the run reduce each
  loan's outstanding balance now (M-31).
* A locked / paid run (or any run with a 'paid' slip) is never regenerated
  or resynced: salary, leave, attendance, reimbursement or variable-pay
  changes after the lock are posted as arrears in a later run (H-12).
* Resync (a salary change landing on an open run) only touches runs that
  were actually generated -- never a 'draft' run (H-13).

Generation (generate_run) recomputes slips IN PLACE: an employee's slip
keeps its id across regenerations (H-12); only its lines are rewritten.
All per-employee inputs are batch-loaded once per run (M-36). Each slip is
prorated to the payable window -- joining date, approved exit's last
working day and salary-assignment start, by working days (H-14) -- and net
pay never goes below zero: deductions are capped (statutory first), the
non-statutory shortfall is carried to the next run and the slip / run are
flagged (H-16). TDS is statutory (app/tax_engine.py, M-30).
"""

from __future__ import annotations

import datetime
import uuid
from collections import defaultdict

from sqlalchemy import delete, false, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from . import crud, models, tax_engine

RUN_DRAFT = "draft"
RUN_GENERATED = "processed"
RUN_APPROVED = "approved"
RUN_LOCKED = "locked"
RUN_PAID = "paid"
GENERATED_STATES = frozenset({RUN_GENERATED, RUN_APPROVED, RUN_LOCKED, RUN_PAID})
FROZEN_STATES = frozenset({RUN_LOCKED, RUN_PAID})
_EXIT_DONE_STATUSES = ("approved", "in_clearance", "completed")

ARREARS_CODE = "ARREARS"
ARREARS_RECOVERY_CODE = "ARR-REC"
LOAN_EMI_CODE = "LOAN-EMI"
CARRY_FORWARD_CODE = "DED-CF"
TDS_CODE = "TDS-EST"
ESI_EMPLOYEE_RATE = 0.0075
ESI_WAGE_CEILING = 21_000.0

FLAG_CAPPED = "deductions_capped"
FLAG_STATUTORY_SHORT = "statutory_shortfall"


class PayrollStateError(ValueError):
    """A run lifecycle rule was violated -- routers map it to status_code."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _key(year: int, month: int) -> int:
    return int(year) * 100 + int(month)


def is_frozen(db: Session, run: models.PayrollRun) -> bool:
    return run.status in FROZEN_STATES or crud.run_has_paid_slip(db, run.id)


def is_resyncable(db: Session, run: models.PayrollRun) -> bool:
    """H-13: only a generated, not yet locked run is refreshed in place."""
    return run.status in (RUN_GENERATED, RUN_APPROVED) and not is_frozen(db, run)


# ── lifecycle ───────────────────────────────────────────────────────────────

def _generator_of(db: Session, run: models.PayrollRun) -> uuid.UUID | None:
    if run.generated_by is not None:
        return run.generated_by
    return db.scalar(
        select(models.AuditLog.user_id)
        .where(models.AuditLog.doctype == "payroll_run", models.AuditLog.action == "generate",
               models.AuditLog.document_id == run.id)
        .order_by(models.AuditLog.created_at.desc())
        .limit(1)
    )


def _audit(db, run, user_id, action, before, after, extra=None):
    changes = {"status": f"{before} -> {after}"}
    if extra:
        changes.update({k: str(v) for k, v in extra.items()})
    crud.create_audit_log(db, run.company_id, user_id, action, "payroll_run", run.id, changes=changes)


def reset_approval(db: Session, run: models.PayrollRun, user_id: uuid.UUID | None, reason: str) -> None:
    """Any change to an approved (not yet locked) run's slips voids the
    approval -- the checker must approve the figures that will be paid."""
    if run.status != RUN_APPROVED:
        return
    run.status = RUN_GENERATED
    run.approved_by = None
    run.approved_at = None
    _audit(db, run, user_id, "approval_reset", RUN_APPROVED, RUN_GENERATED, {"reason": reason})


def approve_run(db: Session, run: models.PayrollRun, user: models.User) -> models.PayrollRun:
    if run.status in (RUN_APPROVED, RUN_LOCKED, RUN_PAID):
        raise PayrollStateError(f"This payroll run is already {run.status}.")
    if run.status != RUN_GENERATED:
        raise PayrollStateError("Generate the payroll run before approving it.")
    slip_count = db.scalar(select(func.count(models.SalarySlip.id)).where(models.SalarySlip.payroll_run_id == run.id))
    if not slip_count:
        raise PayrollStateError("This payroll run has no payslips to approve.")
    generator = _generator_of(db, run)
    if generator is None:
        raise PayrollStateError("It is not recorded who generated this run -- regenerate it, then have "
                                "another authorised user approve it.")
    if generator == user.id:
        raise PayrollStateError("Two-person rule: the user who generated this payroll run cannot approve it. "
                                "Another authorised user must approve it.", status_code=403)
    run.status = RUN_APPROVED
    run.approved_by = user.id
    run.approved_at = _now()
    _audit(db, run, user.id, "approve", RUN_GENERATED, RUN_APPROVED, {"slips": slip_count})
    db.flush()
    return run


def lock_run(db: Session, run: models.PayrollRun, user: models.User) -> models.PayrollRun:
    if run.status in FROZEN_STATES:
        raise PayrollStateError(f"This payroll run is already {run.status}.")
    if run.status != RUN_APPROVED:
        raise PayrollStateError("Only an approved payroll run can be locked.")
    now = _now()
    # M-31: the EMIs recovered on this run now reduce the loans.
    recoveries = db.scalars(select(models.LoanRecovery).where(
        models.LoanRecovery.payroll_run_id == run.id, models.LoanRecovery.applied_at.is_(None))).all()
    loans = {l.id: l for l in db.scalars(select(models.Loan).where(
        models.Loan.id.in_([r.loan_id for r in recoveries])))} if recoveries else {}
    for rec in recoveries:
        loan = loans.get(rec.loan_id)
        if loan is None:
            continue
        loan.outstanding_balance = max(0.0, round(float(loan.outstanding_balance or 0) - float(rec.amount), 2))
        if loan.outstanding_balance <= 0:
            loan.status = "closed"
        rec.applied_at = now
    run.status = RUN_LOCKED
    run.locked_by = user.id
    run.locked_at = now
    _audit(db, run, user.id, "lock", RUN_APPROVED, RUN_LOCKED, {"loan_recoveries": len(recoveries)})
    db.flush()
    return run


def mark_paid(db: Session, run: models.PayrollRun, user: models.User) -> models.PayrollRun:
    if run.status == RUN_PAID:
        raise PayrollStateError("This payroll run is already marked paid.")
    if run.status != RUN_LOCKED:
        raise PayrollStateError("Lock the payroll run before marking it paid.")
    db.execute(update(models.SalarySlip).where(models.SalarySlip.payroll_run_id == run.id).values(status="paid"))
    run.status = RUN_PAID
    run.paid_by = user.id
    run.paid_at = _now()
    _audit(db, run, user.id, "mark_paid", RUN_LOCKED, RUN_PAID)
    db.flush()
    return run


# ── batch context ───────────────────────────────────────────────────────────

def _months_between(a: datetime.date, b: datetime.date) -> int:
    """Whole months from a's month to b's month (b after a -> positive)."""
    return (b.year - a.year) * 12 + (b.month - a.month)


class _Ctx:
    """Everything one run's slips need, loaded with a fixed number of
    queries whatever the head count (M-36)."""

    def __init__(self, db: Session, company_id: uuid.UUID, run: models.PayrollRun,
                 employee_ids: "set[uuid.UUID] | None" = None, structure_only: bool = False):
        self.db, self.company_id, self.run = db, company_id, run
        self.structure_only = structure_only
        self.company = db.get(models.Company, company_id)
        settings_row = db.get(models.CompanySettings, company_id)
        self.pf_ceiling = float(settings_row.pf_wage_ceiling) if settings_row is not None and settings_row.pf_wage_ceiling is not None else 15000.0
        self.auto_tds = bool(getattr(settings_row, "auto_tds_estimate_enabled", False)) if settings_row else False
        self.lop_enabled = bool(getattr(settings_row, "lop_deduction_enabled", False)) if settings_row else False
        self.asset_enabled = bool(getattr(settings_row, "asset_deduction_enabled", False)) if settings_row else False
        self.wdpw = (settings_row.working_days_per_week if settings_row is not None else None) or 5

        # Employees: active ones, plus anyone whose approved exit's last
        # working day falls in (or after the start of) this period.
        lwd_rows = db.execute(
            select(models.ExitRequestModel.employee_id, func.max(models.ExitRequestModel.last_working_day))
            .join(models.Employee, models.Employee.id == models.ExitRequestModel.employee_id)
            .where(models.Employee.company_id == company_id,
                   models.ExitRequestModel.status.in_(_EXIT_DONE_STATUSES),
                   models.ExitRequestModel.last_working_day.is_not(None))
            .group_by(models.ExitRequestModel.employee_id)
        ).all()
        self.lwd: dict[uuid.UUID, datetime.date] = {e: d for e, d in lwd_rows}
        leaving_now = [e for e, d in self.lwd.items() if d >= run.from_date]
        q = select(models.Employee).where(
            models.Employee.company_id == company_id,
            or_(models.Employee.is_active.is_(True), models.Employee.id.in_(leaving_now) if leaving_now else false()),
        )
        if employee_ids is not None:
            q = q.where(models.Employee.id.in_(list(employee_ids) or [uuid.uuid4()]))
        self.employees = list(db.scalars(q).all())
        ids = [e.id for e in self.employees] or [uuid.uuid4()]
        self.ids = ids

        # Salary assignments, structures, overrides.
        self.assignments: dict[uuid.UUID, list] = defaultdict(list)
        for a in db.scalars(select(models.SalaryStructureAssignment).where(
                models.SalaryStructureAssignment.employee_id.in_(ids),
                models.SalaryStructureAssignment.from_date <= run.to_date,
        ).order_by(models.SalaryStructureAssignment.from_date, models.SalaryStructureAssignment.created_at)):
            self.assignments[a.employee_id].append(a)
        all_assignments = [a for lst in self.assignments.values() for a in lst]
        structure_ids = {a.structure_id for a in all_assignments} or {uuid.uuid4()}
        self.lines: dict[uuid.UUID, list] = defaultdict(list)
        for line in db.scalars(select(models.SalaryStructureLine)
                               .options(selectinload(models.SalaryStructureLine.component))
                               .where(models.SalaryStructureLine.structure_id.in_(structure_ids))):
            self.lines[line.structure_id].append(line)
        self.overrides: dict[uuid.UUID, dict] = defaultdict(dict)
        self.override_rows: list = []
        if all_assignments:
            for o in db.scalars(select(models.SalaryStructureAssignmentOverride).where(
                    models.SalaryStructureAssignmentOverride.assignment_id.in_([a.id for a in all_assignments]))):
                self.overrides[o.assignment_id][o.component_id] = o
                self.override_rows.append(o)

        # Calendar + attendance.
        self.holidays = db.execute(select(models.Holiday.holiday_date, models.Holiday.branch_id).where(
            models.Holiday.company_id == company_id,
            models.Holiday.holiday_date >= run.from_date, models.Holiday.holiday_date <= run.to_date)).all()
        self._wd_cache: dict = {}
        self.absent: dict[uuid.UUID, set] = defaultdict(set)
        for emp_id, d in db.execute(select(models.AttendanceRecord.employee_id, models.AttendanceRecord.attendance_date).where(
                models.AttendanceRecord.employee_id.in_(ids),
                models.AttendanceRecord.attendance_date >= run.from_date,
                models.AttendanceRecord.attendance_date <= run.to_date,
                models.AttendanceRecord.status == "absent")):
            self.absent[emp_id].add(d)

        self.components = {c.id: c for c in db.scalars(select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == company_id))}
        if structure_only:
            return
        self._load_full()

    # -- full (non dry-run) inputs --------------------------------------------
    def _load_full(self):
        db, run, ids, company_id = self.db, self.run, self.ids, self.company_id
        self.payouts: dict = defaultdict(dict)
        for emp_id, comp_id, total in db.execute(
                select(models.VariablePayPayout.employee_id, models.VariablePayPayout.component_id,
                       func.sum(models.VariablePayPayout.amount))
                .where(models.VariablePayPayout.employee_id.in_(ids), models.VariablePayPayout.payroll_run_id == run.id)
                .group_by(models.VariablePayPayout.employee_id, models.VariablePayPayout.component_id)):
            self.payouts[emp_id][comp_id] = float(total or 0)

        self.fy_start, self.fy_end = crud._fiscal_year_window(self.company, run.to_date)
        fy_label = f"{self.fy_start.year}-{str(self.fy_start.year + 1)[-2:]}"
        self.declarations = {}
        for d in db.scalars(select(models.TaxDeclaration).where(
                models.TaxDeclaration.employee_id.in_(ids)).order_by(models.TaxDeclaration.id)):
            if d.fiscal_year in (fy_label, f"{self.fy_start.year}-{self.fy_start.year + 1}"):
                self.declarations[d.employee_id] = d

        # Year-to-date per employee+component from earlier runs of this FY.
        self.ytd: dict = defaultdict(lambda: defaultdict(float))
        cur = _key(run.period_year, run.period_month)
        start = _key(self.fy_start.year, self.fy_start.month)
        for emp_id, comp_id, total in db.execute(
                select(models.SalarySlip.employee_id, models.SalarySlipLine.component_id, func.sum(models.SalarySlipLine.amount))
                .join(models.SalarySlip, models.SalarySlip.id == models.SalarySlipLine.slip_id)
                .join(models.PayrollRun, models.PayrollRun.id == models.SalarySlip.payroll_run_id)
                .where(models.PayrollRun.company_id == company_id, models.SalarySlip.employee_id.in_(ids),
                       models.PayrollRun.id != run.id,
                       models.PayrollRun.period_year * 100 + models.PayrollRun.period_month >= start,
                       models.PayrollRun.period_year * 100 + models.PayrollRun.period_month < cur)
                .group_by(models.SalarySlip.employee_id, models.SalarySlipLine.component_id)):
            self.ytd[emp_id][comp_id] += float(total or 0)

        self.lop_leaves: dict = defaultdict(list)
        if self.lop_enabled:
            for req in db.scalars(select(models.LeaveRequest)
                                  .join(models.LeaveType, models.LeaveType.id == models.LeaveRequest.leave_type_id)
                                  .where(models.LeaveRequest.employee_id.in_(ids), models.LeaveRequest.status == "approved",
                                         models.LeaveType.code == "LOP", models.LeaveRequest.from_date <= run.to_date,
                                         models.LeaveRequest.to_date >= run.from_date)):
                self.lop_leaves[req.employee_id].append(req)

        self.asset_recoveries: dict = defaultdict(list)
        if self.asset_enabled:
            for r in db.scalars(select(models.AssetRecoveryDeduction).where(
                    models.AssetRecoveryDeduction.employee_id.in_(ids),
                    models.AssetRecoveryDeduction.status == "approved",
                    models.AssetRecoveryDeduction.target_period_month == run.period_month,
                    models.AssetRecoveryDeduction.target_period_year == run.period_year,
                    or_(models.AssetRecoveryDeduction.applied_payroll_run_id.is_(None),
                        models.AssetRecoveryDeduction.applied_payroll_run_id == run.id))):
                self.asset_recoveries[r.employee_id].append(r)

        self.overtime: dict = defaultdict(list)
        for item in db.scalars(select(models.OvertimeCompensation).where(
                models.OvertimeCompensation.employee_id.in_(ids),
                models.OvertimeCompensation.mode == "payable",
                models.OvertimeCompensation.status.in_(("pending_payroll", "in_payroll")),
                (models.OvertimeCompensation.target_period_year * 100 + models.OvertimeCompensation.target_period_month) <= cur,
                or_(models.OvertimeCompensation.applied_payroll_run_id.is_(None),
                    models.OvertimeCompensation.applied_payroll_run_id == run.id))):
            self.overtime[item.employee_id].append(item)

        self.reimbursement_components = crud.get_reimbursement_components(db, company_id)
        enabled = [st for st, c in self.reimbursement_components.items() if c["is_enabled"]]
        self.reimbursements: dict = defaultdict(list)
        if enabled:
            for inc in db.scalars(select(models.PayrollReimbursementInclusion).where(
                    models.PayrollReimbursementInclusion.employee_id.in_(ids),
                    models.PayrollReimbursementInclusion.target_period_month == run.period_month,
                    models.PayrollReimbursementInclusion.target_period_year == run.period_year,
                    models.PayrollReimbursementInclusion.status == "included",
                    models.PayrollReimbursementInclusion.source_type.in_(enabled),
                    or_(models.PayrollReimbursementInclusion.applied_payroll_run_id.is_(None),
                        models.PayrollReimbursementInclusion.applied_payroll_run_id == run.id),
            ).order_by(models.PayrollReimbursementInclusion.created_at)):
                self.reimbursements[inc.employee_id].append(inc)

        # Loans (M-31): active, with an EMI and something outstanding; less
        # what other open runs already earmarked.
        self.loans: dict = defaultdict(list)
        for loan in db.scalars(select(models.Loan).where(
                models.Loan.employee_id.in_(ids), models.Loan.status == "active",
                models.Loan.emi_amount > 0, models.Loan.outstanding_balance > 0).order_by(models.Loan.id)):
            self.loans[loan.employee_id].append(loan)
        loan_ids = [l.id for lst in self.loans.values() for l in lst]
        self.loan_pending: dict = defaultdict(float)
        if loan_ids:
            for loan_id, total in db.execute(
                    select(models.LoanRecovery.loan_id, func.sum(models.LoanRecovery.amount))
                    .where(models.LoanRecovery.loan_id.in_(loan_ids), models.LoanRecovery.applied_at.is_(None),
                           models.LoanRecovery.payroll_run_id != run.id)
                    .group_by(models.LoanRecovery.loan_id)):
                self.loan_pending[loan_id] = float(total or 0)

        # H-16 carry-forward: the shortfall left on the previous generated run.
        prev = db.scalar(select(models.PayrollRun).where(
            models.PayrollRun.company_id == company_id,
            models.PayrollRun.status.in_(GENERATED_STATES),
            models.PayrollRun.period_year * 100 + models.PayrollRun.period_month < cur,
        ).order_by(models.PayrollRun.period_year.desc(), models.PayrollRun.period_month.desc()).limit(1))
        self.carry_in: dict = {}
        if prev is not None:
            for emp_id, amt in db.execute(select(models.SalarySlip.employee_id, models.SalarySlip.deduction_carry_forward).where(
                    models.SalarySlip.payroll_run_id == prev.id, models.SalarySlip.employee_id.in_(ids),
                    models.SalarySlip.deduction_carry_forward > 0)):
                self.carry_in[emp_id] = float(amt)

        self.arrears, self.arrear_rows = self._load_arrears()

    def _load_arrears(self):
        """H-12: a salary assignment / override created after an earlier run
        was locked, effective on or before that run's period, makes that
        period's difference payable (or recoverable) here."""
        db, run = self.db, self.run
        cur = _key(run.period_year, run.period_month)
        frozen = db.scalars(select(models.PayrollRun).where(
            models.PayrollRun.company_id == self.company_id,
            models.PayrollRun.status.in_(FROZEN_STATES),
            models.PayrollRun.period_year * 100 + models.PayrollRun.period_month < cur,
            models.PayrollRun.period_year * 100 + models.PayrollRun.period_month >= cur - 100,
        )).all()
        totals: dict = defaultdict(float)
        rows: list = []
        if not frozen:
            return totals, rows
        assignment_by_id = {a.id: a for lst in self.assignments.values() for a in lst}
        changes = [(a.employee_id, a.from_date, a.created_at) for a in assignment_by_id.values()]
        for o in self.override_rows:
            a = assignment_by_id.get(o.assignment_id)
            if a is not None:
                changes.append((a.employee_id, a.from_date, o.created_at))
        pairs: dict = defaultdict(set)
        for s in frozen:
            frozen_at = s.locked_at or s.paid_at or s.updated_at or s.created_at
            if frozen_at is None:
                continue
            for emp_id, from_date, created in changes:
                if created is not None and created > frozen_at and from_date <= s.to_date:
                    pairs[s.id].add(emp_id)
        if not pairs:
            return totals, rows
        excluded = {ARREARS_CODE, ARREARS_RECOVERY_CODE, "OT-PAY", "CONTRACT-FEE"}
        original: dict = defaultdict(float)
        has_slip: set = set()
        for run_id, emp_id, comp_id, amount in db.execute(
                select(models.SalarySlip.payroll_run_id, models.SalarySlip.employee_id,
                       models.SalarySlipLine.component_id, models.SalarySlipLine.amount)
                .join(models.SalarySlipLine, models.SalarySlipLine.slip_id == models.SalarySlip.id, isouter=True)
                .where(models.SalarySlip.payroll_run_id.in_(list(pairs)),
                       models.SalarySlip.employee_id.in_(self.ids))):
            has_slip.add((run_id, emp_id))
            comp = self.components.get(comp_id)
            if comp is not None and comp.component_type == "earning" and comp.calc_type != "var_annual" \
                    and (comp.code or "").upper() not in excluded:
                original[(run_id, emp_id)] += float(amount or 0)
        posted: dict = defaultdict(float)
        for src, emp_id, total in db.execute(
                select(models.PayrollArrear.source_run_id, models.PayrollArrear.employee_id, func.sum(models.PayrollArrear.amount))
                .where(models.PayrollArrear.source_run_id.in_(list(pairs)), models.PayrollArrear.target_run_id != run.id,
                       models.PayrollArrear.employee_id.in_(self.ids))
                .group_by(models.PayrollArrear.source_run_id, models.PayrollArrear.employee_id)):
            posted[(src, emp_id)] = float(total or 0)
        frozen_by_id = {s.id: s for s in frozen}
        for src_id, emp_ids in pairs.items():
            emp_ids = {e for e in emp_ids if (src_id, e) in has_slip}
            if not emp_ids:
                continue
            sub = _Ctx(db, self.company_id, frozen_by_id[src_id], employee_ids=emp_ids, structure_only=True)
            for emp in sub.employees:
                part = _structure_part(sub, emp)
                recomputed = part["structure_gross"] if part else 0.0
                delta = round(recomputed - original[(src_id, emp.id)] - posted[(src_id, emp.id)], 2)
                if abs(delta) >= 1:
                    totals[emp.id] += delta
                    rows.append((emp.id, src_id, delta))
        return totals, rows

    def working_dates(self, branch_id) -> list:
        if branch_id in self._wd_cache:
            return self._wd_cache[branch_id]
        off = {d for d, b in self.holidays if b is None or b == branch_id}
        out = []
        d = self.run.from_date
        while d <= self.run.to_date:
            weekend = d.weekday() == 6 if self.wdpw >= 6 else d.weekday() >= 5
            if not weekend and d not in off:
                out.append(d)
            d += datetime.timedelta(days=1)
        self._wd_cache[branch_id] = out
        return out

    def component(self, code: str, name: str, component_type: str, is_taxable: bool) -> models.SalaryComponent:
        for c in self.components.values():
            if (c.code or "").upper() == code.upper():
                return c
        c = crud.create_salary_component(self.db, self.company_id, name, code, component_type, "flat", is_taxable)
        self.components[c.id] = c
        return c


# ── per-employee computation ───────────────────────────────────────────────

def _is_statutory(component: models.SalaryComponent) -> bool:
    if component.component_type == "tax" or crud._is_statutory_pf_esi(component):
        return True
    return (component.code or "").upper() == "PT" or "professional tax" in (component.name or "").lower()


def _is_hra(component: models.SalaryComponent) -> bool:
    """M-29: matched on the component code, not the display name."""
    return (component.code or "").strip().upper() == "HRA"


def _payable_window(ctx: _Ctx, emp: models.Employee):
    start = max(ctx.run.from_date, emp.date_of_joining or ctx.run.from_date)
    end = ctx.run.to_date
    lwd = ctx.lwd.get(emp.id)
    if lwd is not None:
        end = min(end, lwd)
    return start, end


def _structure_part(ctx: _Ctx, emp: models.Employee) -> dict | None:
    """H-14: structure earnings / deductions prorated over the payable
    window, split into one segment per salary assignment in force."""
    run = ctx.run
    start, end = _payable_window(ctx, emp)
    if start > end:
        return None
    by_from: dict = {}
    for a in ctx.assignments.get(emp.id, []):
        if a.annual_ctc is not None and ctx.lines.get(a.structure_id):
            by_from[a.from_date] = a  # same-day: the later-created one wins
    ordered = sorted(by_from.items())
    segments = []
    for i, (from_date, a) in enumerate(ordered):
        seg_end = ordered[i + 1][0] - datetime.timedelta(days=1) if i + 1 < len(ordered) else end
        seg_start, seg_end = max(from_date, start), min(seg_end, end)
        if seg_start <= seg_end:
            segments.append((a, seg_start, seg_end))
    if not segments:
        return None
    contract = crud.is_contract_employee(emp)
    wd = ctx.working_dates(emp.branch_id)
    period_wd = len(wd)
    absent = ctx.absent.get(emp.id, set())
    period_days = (run.to_date - run.from_date).days + 1

    agg: dict = {}  # component_id -> {component, line, amount}
    statutory_lines: dict = {}
    total_basic = 0.0
    basic_full = 0.0  # unprorated monthly Basic of the latest segment
    basic_ids: set = set()  # the structures' Basic (first %-of-CTC earning) components
    payable = 0.0
    lop_total = 0.0
    full_month: list = []
    for a, s, e in segments:
        overrides = ctx.overrides.get(a.id, {})
        resolved = crud._resolve_structure_lines_monthly(ctx.lines[a.structure_id], float(a.annual_ctc), overrides, ctx.pf_ceiling)
        seg_wd = sum(1 for d in wd if s <= d <= e)
        seg_lop = sum(1 for d in absent if s <= d <= e)
        if period_wd:
            frac = max(0, seg_wd - seg_lop) / period_wd
        else:
            frac = ((e - s).days + 1) / period_days
        payable += max(0, seg_wd - seg_lop)
        lop_total += seg_lop
        full_month = resolved
        basic_seen = False
        for r in resolved:
            comp, line, amount = r["component"], r["line"], float(r["amount"] or 0)
            if contract and crud._is_statutory_pf_esi(comp):
                continue
            if line.percent_of == "ctc" and comp.component_type == "earning" and not basic_seen:
                total_basic += amount * frac
                basic_full = amount
                basic_ids.add(comp.id)
                basic_seen = True
            if comp.calc_type == "var_annual":
                value = None  # filled below (confirmed payouts only)
            elif comp.component_type == "earning":
                value = amount * frac
            elif comp.calc_type in ("stat_pf", "stat_esi") and comp.id not in overrides:
                statutory_lines[comp.id] = (comp, line)
                continue
            elif line.percent_of is not None:
                value = amount * frac
            else:
                agg[comp.id] = {"component": comp, "line": line, "amount": amount}  # flat: once
                continue
            slot = agg.setdefault(comp.id, {"component": comp, "line": line, "amount": 0.0})
            if value is not None:
                slot["amount"] += value

    gross_struct = sum(v["amount"] for v in agg.values()
                       if v["component"].component_type == "earning" and v["component"].calc_type != "var_annual")
    full_gross = sum(float(r["amount"] or 0) for r in full_month if r["component"].component_type == "earning")
    for comp_id, (comp, line) in statutory_lines.items():
        if comp.calc_type == "stat_pf":
            amount = min(total_basic, ctx.pf_ceiling) * crud._STATUTORY_PF_RATE
        else:
            amount = gross_struct * ESI_EMPLOYEE_RATE if 0 < full_gross <= ESI_WAGE_CEILING else 0.0
            if amount > 0:
                amount = float(int(-(-amount // 1)))  # ESI is rounded up to the rupee
        agg[comp_id] = {"component": comp, "line": line, "amount": amount}

    lines = []
    for v in agg.values():
        comp, line, amount = v["component"], v["line"], v["amount"]
        if round(amount, 2) == 0 and comp.calc_type != "var_annual" and (
                line.percent_of is not None or comp.calc_type == "stat_esi"):
            continue
        lines.append((comp, amount))
    full_taxable = sum(float(r["amount"] or 0) for r in full_month
                       if r["component"].component_type == "earning" and r["component"].is_taxable
                       and r["component"].calc_type != "var_annual")
    return {
        "lines": lines,
        "structure_gross": round(gross_struct, 2),
        "working_days": float(period_wd),
        "lop_days": float(lop_total),
        "payable_days": float(payable),
        "basic": total_basic,
        "basic_full": basic_full,
        "basic_ids": basic_ids,
        "full_month": full_month,
        "full_taxable": full_taxable,
    }


def _contract_part(ctx: _Ctx, emp: models.Employee) -> dict | None:
    start, end = _payable_window(ctx, emp)
    if start > end:
        return None
    units = crud.contract_payable_units(ctx.db, emp.id, emp.contract_rate_unit, start, end)
    fee = round(units * float(emp.contract_rate_amount), 2)
    fee_comp = ctx.component("CONTRACT-FEE", "Contract Fee", "earning", True)
    wd = len(ctx.working_dates(emp.branch_id))
    return {"lines": [(fee_comp, fee)], "structure_gross": fee, "working_days": float(wd), "lop_days": 0.0,
            "payable_days": float(sum(1 for d in ctx.working_dates(emp.branch_id) if start <= d <= end)),
            "basic": 0.0, "full_month": [], "full_taxable": 0.0, "contract_rate": True}


def _compute_tds(ctx: _Ctx, emp: models.Employee, part: dict, earnings: list, deductions: list) -> float:
    """M-30: statutory TDS for this month -- projected annual tax under the
    employee's regime, less TDS already deducted this FY, spread over the
    months left (including this one)."""
    run = ctx.run
    ytd = ctx.ytd.get(emp.id, {})
    comps = ctx.components

    def ytd_sum(pred):
        return sum(v for cid, v in ytd.items() if cid in comps and pred(comps[cid]))

    lwd = ctx.lwd.get(emp.id)
    horizon = min(ctx.fy_end, lwd) if lwd is not None else ctx.fy_end
    months_after = max(0, _months_between(run.to_date, horizon))
    full = part["full_month"]

    def full_sum(pred):
        return sum(float(r["amount"] or 0) for r in full if pred(r["component"]))

    is_pf = lambda c: c.component_type == "deduction" and crud._is_statutory_pf_esi(c)  # noqa: E731
    is_pt = lambda c: c.component_type == "deduction" and ((c.code or "").upper() == "PT" or "professional tax" in (c.name or "").lower())  # noqa: E731
    basic_ids = part.get("basic_ids") or set()
    is_basic = lambda c: c.component_type == "earning" and (c.id in basic_ids or (c.code or "").upper() == "BASIC")  # noqa: E731
    taxable_earning = lambda c: c.component_type == "earning" and c.is_taxable  # noqa: E731

    current_taxable = sum(a for c, a in earnings if c.is_taxable)
    gross_annual = ytd_sum(taxable_earning) + current_taxable + part["full_taxable"] * months_after
    pf_full = full_sum(is_pf)
    pf_annual = ytd_sum(is_pf) + sum(a for c, a in deductions if is_pf(c)) + pf_full * months_after
    pt_annual = ytd_sum(is_pt) + sum(a for c, a in deductions if is_pt(c)) + full_sum(is_pt) * months_after
    hra_annual = ytd_sum(lambda c: c.component_type == "earning" and _is_hra(c)) \
        + sum(a for c, a in earnings if _is_hra(c)) + full_sum(_is_hra) * months_after
    basic_annual = ytd_sum(is_basic) + part["basic"] + part.get("basic_full", 0.0) * months_after

    decl = ctx.declarations.get(emp.id)
    regime = tax_engine.normalise_regime(decl.tax_regime if decl is not None else emp.tax_regime)
    inputs = tax_engine.TaxInputs(regime=regime, gross_salary=gross_annual, professional_tax=pt_annual)
    if regime == tax_engine.OLD_REGIME:
        inputs.section_80c = pf_annual + (float(decl.section_80c or 0) if decl else 0.0)
        if decl is not None:
            inputs.hra_exemption = tax_engine.hra_exemption(float(decl.hra_claimed or 0), hra_annual, basic_annual)
            inputs.section_80d = float(getattr(decl, "section_80d", 0) or 0)
            inputs.section_80ccd_1b = float(getattr(decl, "section_80ccd_1b", 0) or 0)
            inputs.home_loan_interest = float(getattr(decl, "home_loan_interest", 0) or 0)
    annual = tax_engine.compute_annual_tax(inputs).total_tax
    tds_paid = ytd_sum(lambda c: c.component_type == "tax")
    return tax_engine.monthly_tds(annual, tds_paid, months_after + 1)


def _compute(ctx: _Ctx, emp: models.Employee) -> dict | None:
    contract_rate = (crud.is_contract_employee(emp) and emp.contract_rate_unit in ("hourly", "daily")
                     and emp.contract_rate_amount)
    part = _contract_part(ctx, emp) if contract_rate else _structure_part(ctx, emp)
    if part is None:
        return None
    payouts = ctx.payouts.get(emp.id, {})
    earnings, deductions, benefits = [], [], []
    for comp, amount in part["lines"]:
        if comp.calc_type == "var_annual":
            amount = payouts.get(comp.id, 0.0)
        if comp.component_type == "earning":
            earnings.append((comp, amount))
        elif comp.component_type == "benefit":
            benefits.append((comp, amount))
        else:
            deductions.append((comp, amount))

    arrears = round(ctx.arrears.get(emp.id, 0.0), 2)
    if arrears > 0:
        earnings.append((ctx.component(ARREARS_CODE, "Arrears", "earning", True), arrears))
    overtime_items = ctx.overtime.get(emp.id, []) if not contract_rate else []
    ot_amount = round(sum(float(i.amount or 0) for i in overtime_items), 2)
    if ot_amount > 0:
        from .overtime import OT_PAY_CODE
        earnings.append((ctx.component(OT_PAY_CODE, "Overtime Pay", "earning", True), ot_amount))
    else:
        overtime_items = []
    gross = sum(a for _c, a in earnings)

    # TDS: an explicit non-zero tax line is the admin's fixed figure;
    # otherwise (when auto-TDS is on) the statutory amount, put on the
    # structure's own (zero) tax line if it has one.
    if ctx.auto_tds and not contract_rate and not any(c.component_type == "tax" and round(a, 2) != 0 for c, a in deductions):
        zero_tax = next((c for c, _a in deductions if c.component_type == "tax"), None)
        deductions = [(c, a) for c, a in deductions if c.component_type != "tax"]
        tds = _compute_tds(ctx, emp, part, earnings, deductions)
        if tds > 0:
            deductions.append((zero_tax or ctx.component(TDS_CODE, "Income Tax (TDS)", "tax", True), tds))
        elif zero_tax is not None:
            deductions.append((zero_tax, 0.0))

    other: list = []  # (component, amount, kind) -- applied after statutory
    if ctx.lop_enabled and part["working_days"] > 0 and not contract_rate:
        lop_days = 0.0
        for req in ctx.lop_leaves.get(emp.id, []):
            span = (req.to_date - req.from_date).days + 1
            o_start, o_end = max(req.from_date, ctx.run.from_date), min(req.to_date, ctx.run.to_date)
            overlap = (o_end - o_start).days + 1
            lop_days += float(req.days) if overlap >= span else float(req.days) * overlap / span
        amount = round(part["structure_gross"] / part["working_days"] * lop_days, 2)
        if amount > 0:
            other.append((ctx.component("LOP-DED", "Loss of Pay (LOP)", "deduction", False), amount, "other"))
    recoveries = ctx.asset_recoveries.get(emp.id, [])
    asset_amount = round(sum(float(r.amount) for r in recoveries), 2)
    if asset_amount > 0:
        other.append((ctx.component("ASSET-DED", "Asset Recovery Deduction", "deduction", False), asset_amount, "other"))
    else:
        recoveries = []
    if arrears < 0:
        other.append((ctx.component(ARREARS_RECOVERY_CODE, "Arrears Recovery", "deduction", False), -arrears, "other"))
    carry = round(ctx.carry_in.get(emp.id, 0.0), 2)
    if carry > 0:
        other.append((ctx.component(CARRY_FORWARD_CODE, "Deduction Carried Forward", "deduction", False), carry, "other"))
    loan_plan = []
    for loan in ctx.loans.get(emp.id, []):
        available = float(loan.outstanding_balance) - ctx.loan_pending.get(loan.id, 0.0)
        emi = round(min(float(loan.emi_amount), max(0.0, available)), 2)
        if emi > 0:
            loan_plan.append([loan, emi])

    # H-16: cap deductions at gross -- statutory first, then the rest.
    ordered = [(c, a, "statutory") for c, a in deductions if _is_statutory(c)]
    ordered += [(c, a, "other") for c, a in deductions if not _is_statutory(c)]
    ordered += other
    available = gross
    shortfall_stat = shortfall_carry = 0.0
    final_deductions = []
    for comp, amount, kind in ordered:
        amount = max(0.0, round(amount, 2))
        taken = min(amount, max(0.0, round(available, 2)))
        available -= taken
        if kind == "statutory":
            shortfall_stat += amount - taken
        else:
            shortfall_carry += amount - taken
        final_deductions.append((comp, taken))
    loan_component = ctx.component(LOAN_EMI_CODE, "Loan EMI Recovery", "deduction", False) if loan_plan else None
    loan_total = 0.0
    loan_short = 0.0
    for item in loan_plan:
        taken = min(item[1], max(0.0, round(available, 2)))
        available -= taken
        loan_short += item[1] - taken
        item[1] = round(taken, 2)
        loan_total += taken
    if loan_plan:
        final_deductions.append((loan_component, round(loan_total, 2)))
    total_deductions = round(sum(a for _c, a in final_deductions), 2)
    flags = []
    if shortfall_stat > 0.005 or shortfall_carry > 0.005 or loan_short > 0.005:
        flags.append(FLAG_CAPPED)
    if shortfall_stat > 0.005:
        flags.append(f"{FLAG_STATUTORY_SHORT}:{shortfall_stat:.2f}")

    inclusions = ctx.reimbursements.get(emp.id, [])
    return {
        "lines": earnings + final_deductions + benefits,
        "gross": round(gross, 2),
        "deductions": total_deductions,
        "net": round(max(0.0, gross - total_deductions), 2),
        "working_days": part["working_days"],
        "lop_days": part["lop_days"],
        "payable_days": part["payable_days"],
        "carry_forward": round(shortfall_carry, 2),
        "flags": ",".join(flags) or None,
        "reimbursements": inclusions,
        "reimbursements_total": round(sum(float(i.amount) for i in inclusions), 2),
        "overtime": overtime_items,
        "asset_recoveries": recoveries,
        "loans": [(loan, amt) for loan, amt in loan_plan if amt > 0],
    }


# ── generation ─────────────────────────────────────────────────────────────

def generate_run(db: Session, company_id: uuid.UUID, run: models.PayrollRun,
                 actor_id: uuid.UUID | None = None,
                 employee_ids: "set[uuid.UUID] | None" = None) -> list[models.SalarySlip]:
    """Computes the run's slips in place (or just [employee_ids]'). Raises
    PayrollStateError for a locked / paid run."""
    if is_frozen(db, run):
        raise PayrollStateError("This payroll run is locked or paid and can no longer be regenerated. "
                                "Later corrections are paid as arrears in the next run.")
    if employee_ids is None:
        crud.identify_reimbursements_for_period(db, company_id, run.period_month, run.period_year, actor_id)

    def scoped(stmt, column):
        return stmt.where(column.in_(list(employee_ids))) if employee_ids is not None else stmt

    # Release what this run consumed on a previous Generate; whatever is
    # still eligible is re-applied below.
    db.execute(scoped(update(models.PayrollReimbursementInclusion)
                      .where(models.PayrollReimbursementInclusion.applied_payroll_run_id == run.id),
                      models.PayrollReimbursementInclusion.employee_id)
               .values(applied_payroll_run_id=None, applied_at=None))
    db.execute(scoped(update(models.OvertimeCompensation)
                      .where(models.OvertimeCompensation.applied_payroll_run_id == run.id),
                      models.OvertimeCompensation.employee_id)
               .values(applied_payroll_run_id=None, applied_at=None, status="pending_payroll"))
    db.execute(scoped(delete(models.LoanRecovery).where(models.LoanRecovery.payroll_run_id == run.id),
                      models.LoanRecovery.employee_id))
    db.execute(scoped(delete(models.PayrollArrear).where(models.PayrollArrear.target_run_id == run.id),
                      models.PayrollArrear.employee_id))

    existing: dict = {}
    duplicates: list = []
    for slip in db.scalars(scoped(select(models.SalarySlip).where(models.SalarySlip.payroll_run_id == run.id),
                                  models.SalarySlip.employee_id).order_by(models.SalarySlip.id)):
        if slip.employee_id in existing:
            duplicates.append(slip.id)
        else:
            existing[slip.employee_id] = slip
    slip_ids = [s.id for s in existing.values()] + duplicates
    if slip_ids:
        db.execute(delete(models.SalarySlipLine).where(models.SalarySlipLine.slip_id.in_(slip_ids)))
        db.execute(delete(models.SalarySlipReimbursementLine).where(models.SalarySlipReimbursementLine.slip_id.in_(slip_ids)))
    if duplicates:
        db.execute(delete(models.SalarySlip).where(models.SalarySlip.id.in_(duplicates)))
    db.flush()

    ctx = _Ctx(db, company_id, run, employee_ids=employee_ids)
    results = []
    for emp in ctx.employees:
        res = _compute(ctx, emp)
        if res is not None:
            results.append((emp, res))
    computed_ids = {emp.id for emp, _r in results}
    stale = [s.id for emp_id, s in existing.items() if emp_id not in computed_ids]
    if stale:
        db.execute(delete(models.SalarySlip).where(models.SalarySlip.id.in_(stale)))

    slips = []
    for emp, res in results:
        slip = existing.get(emp.id)
        if slip is None:
            slip = models.SalarySlip(id=uuid.uuid4(), payroll_run_id=run.id, employee_id=emp.id)
            db.add(slip)
        slip.working_days = round(res["working_days"], 1)
        slip.lop_days = round(res["lop_days"], 1)
        slip.payable_days = round(res["payable_days"], 1)
        slip.gross_pay = res["gross"]
        slip.total_deductions = res["deductions"]
        slip.net_pay = res["net"]
        slip.reimbursements_total = res["reimbursements_total"]
        slip.deduction_carry_forward = res["carry_forward"]
        slip.flags = res["flags"]
        slip.status = "draft"
        slips.append((slip, res))
    db.flush()

    now = _now()
    for slip, res in slips:
        db.add_all([models.SalarySlipLine(id=uuid.uuid4(), slip_id=slip.id, component_id=c.id, amount=round(a, 2))
                    for c, a in res["lines"]])
        for r in res["asset_recoveries"]:
            r.applied_payroll_run_id, r.applied_at = run.id, now
        for item in res["overtime"]:
            item.applied_payroll_run_id, item.applied_at, item.status = run.id, now, "in_payroll"
        for inc in res["reimbursements"]:
            label = ctx.reimbursement_components[inc.source_type]["display_name"][:40]
            db.add(models.SalarySlipReimbursementLine(id=uuid.uuid4(), slip_id=slip.id, inclusion_id=inc.id,
                                                      label=label, amount=round(float(inc.amount), 2)))
            inc.applied_payroll_run_id, inc.applied_at = run.id, now
        for loan, amount in res["loans"]:
            db.add(models.LoanRecovery(id=uuid.uuid4(), loan_id=loan.id, employee_id=slip.employee_id,
                                       payroll_run_id=run.id, slip_id=slip.id, amount=amount, created_at=now))
    for emp_id, src_id, amount in ctx.arrear_rows:
        if emp_id in computed_ids:
            db.add(models.PayrollArrear(id=uuid.uuid4(), company_id=company_id, employee_id=emp_id,
                                        source_run_id=src_id, target_run_id=run.id, amount=amount, created_at=now))
    db.flush()
    run.flagged_slips = db.scalar(select(func.count(models.SalarySlip.id)).where(
        models.SalarySlip.payroll_run_id == run.id, models.SalarySlip.flags.is_not(None))) or 0
    db.flush()
    return [s for s, _r in slips]


def record_generated(db: Session, run: models.PayrollRun, user_id: uuid.UUID | None, slip_count: int) -> None:
    before = run.status
    run.status = RUN_GENERATED
    run.generated_by = user_id
    run.generated_at = _now()
    run.approved_by = None
    run.approved_at = None
    _audit(db, run, user_id, "generate", before, RUN_GENERATED,
           {"slips_generated": slip_count, "flagged_slips": run.flagged_slips or 0})


def resync_employee(db: Session, company_id: uuid.UUID, run: models.PayrollRun, employee_id: uuid.UUID,
                    actor_id: uuid.UUID | None = None) -> models.SalarySlip | None:
    """Recompute one employee's slip on an open, generated run (H-13). A
    draft or locked run is left alone. Voids an approval (the figures
    changed)."""
    if not is_resyncable(db, run):
        return None
    slips = generate_run(db, company_id, run, actor_id, employee_ids={employee_id})
    reset_approval(db, run, actor_id, "employee slip recomputed")
    return slips[0] if slips else None


def resync_runs_from(db: Session, company_id: uuid.UUID, employee_id: uuid.UUID,
                     effective_from: datetime.date, actor_id: uuid.UUID | None = None) -> list[models.PayrollRun]:
    runs = db.scalars(select(models.PayrollRun).where(
        models.PayrollRun.company_id == company_id,
        models.PayrollRun.to_date >= effective_from,
        models.PayrollRun.status.in_((RUN_GENERATED, RUN_APPROVED)),
    )).all()
    done = []
    for run in runs:
        if is_resyncable(db, run):
            resync_employee(db, company_id, run, employee_id, actor_id)
            done.append(run)
    return done


def next_open_period_start(db: Session, company_id: uuid.UUID, today: datetime.date) -> datetime.date:
    """First day an approved salary revision can take effect without
    touching a locked period (M-33): this month, or the month after the
    latest locked / paid run."""
    start = today.replace(day=1)
    last = db.scalar(select(func.max(models.PayrollRun.to_date)).where(
        models.PayrollRun.company_id == company_id, models.PayrollRun.status.in_(FROZEN_STATES)))
    if last is not None and last >= start:
        start = last + datetime.timedelta(days=1)
    return start


# ── salary revisions (M-33) ─────────────────────────────────────────────────

def current_assignment(db: Session, employee_id: uuid.UUID, as_of: datetime.date):
    return crud._resolve_assignment_as_of(db, employee_id, as_of) or db.scalar(
        select(models.SalaryStructureAssignment)
        .where(models.SalaryStructureAssignment.employee_id == employee_id)
        .order_by(models.SalaryStructureAssignment.from_date.desc(),
                  models.SalaryStructureAssignment.created_at.desc()).limit(1))


def current_ctc(db: Session, employee: models.Employee) -> int:
    """The employee's current annual CTC, read on the server (never taken
    from the client): the salary assignment in force, else the employee
    record's reference CTC, else 0."""
    today = crud.company_today(db, employee.company_id)
    assignment = current_assignment(db, employee.id, today)
    if assignment is not None and assignment.annual_ctc is not None:
        return int(round(float(assignment.annual_ctc)))
    return int(employee.annual_ctc or 0)


def apply_approved_revision(db: Session, request: models.SalaryRevisionRequest,
                            actor_id: uuid.UUID | None) -> models.SalaryStructureAssignment:
    """An approved revision takes effect: a new assignment on the same
    structure at the proposed CTC, from the first open payroll period, and
    any generated open run from then on is refreshed. Raises ValueError
    when the employee has no structure to revise."""
    employee = db.get(models.Employee, request.employee_id)
    today = crud.company_today(db, employee.company_id)
    base = current_assignment(db, employee.id, today)
    if base is None:
        raise ValueError("This employee has no salary structure assigned -- assign one before approving a "
                         "salary revision.")
    if not request.proposed_ctc or request.proposed_ctc <= 0:
        raise ValueError("The proposed CTC must be greater than zero.")
    effective = next_open_period_start(db, employee.company_id, today)
    structure = db.get(models.SalaryStructure, base.structure_id)
    if structure is not None and not structure.is_active:
        # Revising pay must not fail because the structure was retired.
        structure.is_active = True
        assignment = crud.create_salary_structure_assignment(db, employee.id, base.structure_id, effective,
                                                             float(request.proposed_ctc))
        structure.is_active = False
    else:
        assignment = crud.create_salary_structure_assignment(db, employee.id, base.structure_id, effective,
                                                             float(request.proposed_ctc))
    if crud.get_superseding_assignment(db, employee.id, effective, assignment.id) is None:
        employee.annual_ctc = int(request.proposed_ctc)
    crud.create_audit_log(db, employee.company_id, actor_id, "create", "salary_structure_assignment", assignment.id,
                          changes={"source": "salary_revision_request", "request_id": str(request.id),
                                   "annual_ctc": str(request.proposed_ctc), "from_date": effective.isoformat()})
    resync_runs_from(db, employee.company_id, employee.id, effective, actor_id)
    return assignment
