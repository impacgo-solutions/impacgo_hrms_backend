import datetime
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from .. import crud, models, report_export, report_helpers as rh, report_pdf_renderer, schemas
from ..database import get_db
from ..deps import (
    get_payroll_scope,
    get_reports_scope,
    require_module_enabled,
    require_permission_or_action,
)

router = APIRouter(
    prefix="/api/reports", tags=["reports"],
    # L-23: every report here takes from_date/to_date -- an inverted range is
    # a 422, not an empty 200.
    dependencies=[Depends(require_module_enabled("reports")), Depends(rh.reject_inverted_range)],
)


def _employee_dimension_filters(
    stmt,
    employee_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
    designation_id: uuid.UUID | None,
    manager_id: uuid.UUID | None,
    branch_id: uuid.UUID | None,
):
    """Applies the common People-dimension filters (Employee/Department/
    Designation/Manager/Branch) to any statement already selecting FROM
    models.Employee -- shared across every report below so "Employee,
    Department, Branch, Designation, Manager" behave identically wherever
    they're offered."""
    if employee_id is not None:
        stmt = stmt.where(models.Employee.id == employee_id)
    if department_id is not None:
        stmt = stmt.where(models.Employee.department_id == department_id)
    if designation_id is not None:
        stmt = stmt.where(models.Employee.designation_id == designation_id)
    if manager_id is not None:
        stmt = stmt.where(models.Employee.reporting_manager_id == manager_id)
    if branch_id is not None:
        stmt = stmt.where(models.Employee.branch_id == branch_id)
    return stmt

_MONTH_NAMES = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _fmt_inr(value: float | None) -> str:
    """Format a numeric value as Indian rupees with commas, or '—' for zero/None."""
    if value is None or value == 0:
        return "—"
    # Indian comma grouping: last 3 digits, then groups of 2
    s = str(int(round(value)))
    if len(s) <= 3:
        return f"₹{s}"
    last3 = s[-3:]
    rest = s[:-3]
    parts = []
    while len(rest) > 2:
        parts.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.append(rest)
    formatted = ",".join(reversed(parts)) + "," + last3
    return f"₹{formatted}"


def _fmt_num(value: float | None) -> str:
    if value is None:
        return "0"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def _attendance_totals(
    db: Session,
    company_id: uuid.UUID,
    from_date: datetime.date,
    to_date: datetime.date,
    visible_ids: list[uuid.UUID] | None,
    employee_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
    designation_id: uuid.UUID | None,
    manager_id: uuid.UUID | None,
    branch_id: uuid.UUID | None,
):
    """Runs the attendance aggregate for [from_date, to_date] under the
    given filters and returns (rows_raw, totals) -- rows_raw is the
    per-employee breakdown (used for the detail table + dept-wise bar
    chart), totals is the company/team-wide numeric summary (used for the
    headline stat cards, the status donut, and -- called a second time for
    the previous period -- period-over-period comparison)."""
    # Attended days other than "late" (late has its own column/slice); the
    # same attended-status set the dashboard uses (crud._PRESENT_STATUSES).
    present_count = func.coalesce(
        func.sum(case((models.AttendanceRecord.status.in_(
            [s for s in crud._PRESENT_STATUSES if s != "late"]
        ), 1), else_=0)), 0
    ).label("present_count")
    absent_count = func.coalesce(
        func.sum(case((models.AttendanceRecord.status == "absent", 1), else_=0)), 0
    ).label("absent_count")
    late_count = func.coalesce(
        func.sum(case((models.AttendanceRecord.status == "late", 1), else_=0)), 0
    ).label("late_count")
    # The employee's own manually-selected Work Mode for the day (see
    # AttendanceRecord.work_mode) -- attendance `status` never actually
    # takes the value "wfh" (see crud.clock_in_out), so this used to always
    # report zero regardless of real WFH days.
    wfh_count = func.coalesce(
        func.sum(case((models.AttendanceRecord.work_mode == "WFH", 1), else_=0)), 0
    ).label("wfh_count")
    hours_sum = func.coalesce(func.sum(models.AttendanceRecord.work_hours), 0).label("hours_sum")

    stmt = (
        select(
            models.Employee.id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Branch.name.label("branch_name"),
            models.Department.name.label("dept_name"),
            present_count,
            absent_count,
            late_count,
            wfh_count,
            hours_sum,
        )
        .outerjoin(
            models.AttendanceRecord,
            (models.AttendanceRecord.employee_id == models.Employee.id)
            & (models.AttendanceRecord.attendance_date >= from_date)
            & (models.AttendanceRecord.attendance_date <= to_date),
        )
        .outerjoin(models.Branch, models.Branch.id == models.Employee.branch_id)
        .outerjoin(models.Department, models.Department.id == models.Employee.department_id)
        .where(models.Employee.company_id == company_id)
        .group_by(
            models.Employee.id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Branch.name,
            models.Department.name,
        )
        .order_by(models.Employee.first_name)
    )
    stmt = _employee_dimension_filters(
        stmt, employee_id, department_id, designation_id, manager_id, branch_id
    )
    if visible_ids is not None:
        stmt = stmt.where(models.Employee.id.in_(visible_ids))

    rows_raw = db.execute(stmt).all()

    totals = {
        "present": sum(int(r.present_count) for r in rows_raw),
        "absent": sum(int(r.absent_count) for r in rows_raw),
        "late": sum(int(r.late_count) for r in rows_raw),
        "wfh": sum(int(r.wfh_count) for r in rows_raw),
        "hours": sum(float(r.hours_sum) for r in rows_raw),
        "employees": len(rows_raw),
    }
    return rows_raw, totals


@router.get("/attendance", response_model=schemas.ReportOut)
def attendance_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    designation_id: uuid.UUID | None = None,
    manager_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    compare_previous: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    company_id = current_user.company_id
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    rows_raw, totals = _attendance_totals(
        db, company_id, from_date, to_date, visible_ids,
        employee_id, department_id, designation_id, manager_id, branch_id,
    )

    # Break Management -- total completed-break minutes per employee over
    # the range, one lightweight GROUP BY query (an in-progress break with
    # no break_end yet is excluded, same "reports are about settled
    # history" convention as everything else here). Merged into the
    # per-employee rows below rather than joined directly into the main
    # aggregate query, which would otherwise multiply AttendanceRecord rows
    # per break and corrupt every other sum in that query.
    matched_ids_early = [r.id for r in rows_raw]
    break_minutes_by_employee: dict[uuid.UUID, float] = {}
    if matched_ids_early:
        break_stmt = (
            select(
                models.AttendanceRecord.employee_id,
                func.coalesce(
                    func.sum(
                        func.extract(
                            "epoch", models.BreakRecord.break_end - models.BreakRecord.break_start
                        ) / 60
                    ), 0,
                ).label("break_minutes"),
            )
            .join(models.AttendanceRecord, models.AttendanceRecord.id == models.BreakRecord.attendance_record_id)
            .where(
                models.AttendanceRecord.employee_id.in_(matched_ids_early),
                models.AttendanceRecord.attendance_date >= from_date,
                models.AttendanceRecord.attendance_date <= to_date,
                models.BreakRecord.break_end.is_not(None),
            )
            .group_by(models.AttendanceRecord.employee_id)
        )
        break_minutes_by_employee = {
            row.employee_id: float(row.break_minutes) for row in db.execute(break_stmt).all()
        }

    columns = [
        "Employee", "Branch", "Department", "Present", "Absent", "Late", "WFH",
        "Hours Logged", "Break (min)", "Net Hours",
    ]
    rows: list[list[str]] = []
    dept_present: dict[str, float] = {}
    total_break_minutes_all = 0.0
    total_net_hours = 0.0

    for r in rows_raw:
        full_name = f"{r.first_name} {r.last_name or ''}".strip()
        p = int(r.present_count)
        h = float(r.hours_sum)
        dept_name = r.dept_name or "—"
        dept_present[dept_name] = dept_present.get(dept_name, 0.0) + p
        break_min = break_minutes_by_employee.get(r.id, 0.0)
        net_hours = h - break_min / 60
        total_break_minutes_all += break_min
        total_net_hours += net_hours
        rows.append([
            full_name,
            r.branch_name or "—",
            dept_name,
            str(p),
            str(int(r.absent_count)),
            str(int(r.late_count)),
            str(int(r.wfh_count)),
            f"{h:.1f}",
            f"{break_min:.0f}",
            f"{net_hours:.1f}",
        ])

    total_employees = totals["employees"]
    avg_present = (
        round((totals["present"] + totals["late"]) / total_employees, 1) if total_employees > 0 else 0.0
    )

    summary = {
        "Total Employees": str(total_employees),
        "Avg Present Days": f"{avg_present:.1f}",
        "Total Hours": f"{totals['hours']:.1f}",
        "Total Break Hours": f"{total_break_minutes_all / 60:.1f}",
        "Net Working Hours": f"{total_net_hours:.1f}",
    }

    # Daily present-count trend -- one lightweight query grouped by the raw
    # date, then bucketed daily/weekly/monthly by report_helpers depending
    # on how wide [from_date, to_date] actually is.
    matched_ids = matched_ids_early
    daily_present: dict[datetime.date, float] = {}
    if matched_ids:
        trend_stmt = (
            select(
                models.AttendanceRecord.attendance_date,
                func.count().label("cnt"),
            )
            .where(
                models.AttendanceRecord.employee_id.in_(matched_ids),
                models.AttendanceRecord.attendance_date >= from_date,
                models.AttendanceRecord.attendance_date <= to_date,
                models.AttendanceRecord.status.in_(crud._PRESENT_STATUSES),
            )
            .group_by(models.AttendanceRecord.attendance_date)
        )
        daily_present = {row.attendance_date: float(row.cnt) for row in db.execute(trend_stmt).all()}

    charts = [
        rh.donut_chart(
            "Attendance Status Breakdown", "Days",
            [
                ("Present", totals["present"]),
                ("Absent", totals["absent"]),
                ("Late", totals["late"]),
                ("WFH", totals["wfh"]),
            ],
        ),
        rh.bar_chart(
            "Present Days by Department", "Present Days",
            sorted(dept_present.items(), key=lambda kv: kv[0]),
        ),
        rh.line_chart(
            "Present-Day Trend", "Present",
            rh.bucket_daily_values(daily_present, from_date, to_date),
        ),
    ]

    comparison = None
    if compare_previous:
        prev_from, prev_to = rh.previous_period(from_date, to_date)
        _prev_rows, prev_totals = _attendance_totals(
            db, company_id, prev_from, prev_to, visible_ids,
            employee_id, department_id, designation_id, manager_id, branch_id,
        )
        prev_avg_present = (
            round((prev_totals["present"] + prev_totals["late"]) / prev_totals["employees"], 1)
            if prev_totals["employees"] > 0 else 0.0
        )
        comparison = rh.build_comparison(
            {
                "Total Employees": float(total_employees),
                "Avg Present Days": avg_present,
                "Total Hours": totals["hours"],
            },
            {
                "Total Employees": float(prev_totals["employees"]),
                "Avg Present Days": prev_avg_present,
                "Total Hours": prev_totals["hours"],
            },
        )

    return schemas.ReportOut(
        report_type="attendance",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
        charts=charts,
        comparison=comparison,
    )


@router.get("/timesheet", response_model=schemas.ReportOut)
def timesheet_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    company_id = current_user.company_id
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    billable_sum = func.coalesce(
        func.sum(case((models.WorkEntry.is_billable == True, models.WorkEntry.hours), else_=0)), 0  # noqa: E712
    ).label("billable_sum")
    non_billable_sum = func.coalesce(
        func.sum(case((models.WorkEntry.is_billable == False, models.WorkEntry.hours), else_=0)), 0  # noqa: E712
    ).label("non_billable_sum")
    total_sum = func.coalesce(func.sum(models.WorkEntry.hours), 0).label("total_sum")

    stmt = (
        select(
            models.Employee.id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Branch.name.label("branch_name"),
            models.Project.name.label("project_name"),
            billable_sum,
            non_billable_sum,
            total_sum,
        )
        .outerjoin(
            models.Timesheet,
            models.Timesheet.employee_id == models.Employee.id,
        )
        .outerjoin(
            models.WorkEntry,
            (models.WorkEntry.timesheet_id == models.Timesheet.id)
            & (models.WorkEntry.entry_date >= from_date)
            & (models.WorkEntry.entry_date <= to_date),
        )
        .outerjoin(models.Project, models.Project.id == models.WorkEntry.project_id)
        .outerjoin(models.Branch, models.Branch.id == models.Employee.branch_id)
        .where(models.Employee.company_id == company_id)
        .group_by(
            models.Employee.id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Branch.name,
            models.Project.name,
        )
        .order_by(models.Employee.first_name, models.Project.name)
    )

    if branch_id is not None:
        stmt = stmt.where(models.Employee.branch_id == branch_id)
    if visible_ids is not None:
        stmt = stmt.where(models.Employee.id.in_(visible_ids))

    rows_raw = db.execute(stmt).all()
    with_hours = {r.id for r in rows_raw if r.project_name is not None}
    rows_raw = [r for r in rows_raw if r.project_name is not None or r.id not in with_hours]

    columns = ["Employee", "Branch", "Project", "Billable Hrs", "Non-Billable Hrs", "Total Hrs"]
    rows: list[list[str]] = []
    grand_total = 0.0
    grand_billable = 0.0

    for r in rows_raw:
        full_name = f"{r.first_name} {r.last_name or ''}".strip()
        b = float(r.billable_sum)
        nb = float(r.non_billable_sum)
        t = float(r.total_sum)
        grand_total += t
        grand_billable += b
        rows.append([
            full_name,
            r.branch_name or "—",
            r.project_name or "—",
            f"{b:.1f}",
            f"{nb:.1f}",
            f"{t:.1f}",
        ])

    utilization_pct = round(grand_billable / grand_total * 100, 1) if grand_total > 0 else 0.0

    summary = {
        "Total Hours": f"{grand_total:.1f}",
        "Billable Hours": f"{grand_billable:.1f}",
        "Utilization %": f"{utilization_pct:.1f}%",
    }

    return schemas.ReportOut(
        report_type="timesheet",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
    )


@router.get("/payroll", response_model=schemas.ReportOut)
def payroll_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
    payroll_scope: str = Depends(get_payroll_scope),
) -> schemas.ReportOut:
    # H-15: every column of this report is pay data (gross, deductions,
    # net, reimbursements). Same gate as the payslips themselves --
    # org-wide payroll scope (Owner / payroll_process Edit-Admin / an
    # admin-set Payroll level) -- so a Manager can't read team pay here
    # while getting 403 on the payslips.
    if payroll_scope != "org":
        raise HTTPException(
            status_code=403,
            detail="The Payroll report needs org-wide payroll access.",
        )
    company_id = current_user.company_id
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    stmt = (
        select(
            models.SalarySlip.id.label("slip_id"),
            models.SalarySlip.employee_id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Department.name.label("dept_name"),
            models.PayrollRun.period_month,
            models.PayrollRun.period_year,
            models.SalarySlip.gross_pay,
            models.SalarySlip.total_deductions,
            models.SalarySlip.net_pay,
        )
        .join(models.PayrollRun, models.PayrollRun.id == models.SalarySlip.payroll_run_id)
        .join(models.Employee, models.Employee.id == models.SalarySlip.employee_id)
        .outerjoin(models.Department, models.Department.id == models.Employee.department_id)
        .where(models.PayrollRun.company_id == company_id)
        .where(models.PayrollRun.from_date <= to_date)
        .where(models.PayrollRun.to_date >= from_date)
        .order_by(
            models.PayrollRun.period_year,
            models.PayrollRun.period_month,
            models.Employee.first_name,
        )
    )

    if branch_id is not None:
        stmt = stmt.where(models.Employee.branch_id == branch_id)
    if visible_ids is not None:
        stmt = stmt.where(models.Employee.id.in_(visible_ids))

    rows_raw = db.execute(stmt).all()

    # Approved Travel / Expense reimbursements paid with each slip (Payroll
    # > Salary Structure > Reimbursement Components), per source type --
    # reported beside, never inside, gross/net salary.
    reimb_by_slip: dict[uuid.UUID, dict[str, float]] = {}
    slip_ids = [r.slip_id for r in rows_raw]
    if slip_ids:
        for slip_id, source_type, amount in db.execute(
            select(
                models.SalarySlipReimbursementLine.slip_id,
                models.PayrollReimbursementInclusion.source_type,
                func.sum(models.SalarySlipReimbursementLine.amount),
            )
            .join(
                models.PayrollReimbursementInclusion,
                models.PayrollReimbursementInclusion.id == models.SalarySlipReimbursementLine.inclusion_id,
            )
            .where(models.SalarySlipReimbursementLine.slip_id.in_(slip_ids))
            .group_by(models.SalarySlipReimbursementLine.slip_id, models.PayrollReimbursementInclusion.source_type)
        ).all():
            reimb_by_slip.setdefault(slip_id, {})[source_type] = float(amount or 0)

    columns = [
        "Employee", "Department", "Period", "Gross Pay", "Deductions", "Net Pay",
        "Travel Reimbursement", "Expense Reimbursement", "Total Payable",
    ]
    rows: list[list[str]] = []
    total_gross = 0.0
    total_net = 0.0
    total_travel = 0.0
    total_expense = 0.0
    emp_ids: set[str] = set()

    for r in rows_raw:
        full_name = f"{r.first_name} {r.last_name or ''}".strip()
        emp_ids.add(str(r.employee_id))
        month_name = _MONTH_NAMES[r.period_month] if 1 <= r.period_month <= 12 else str(r.period_month)
        period = f"{month_name} {r.period_year}"
        gross = float(r.gross_pay)
        deductions = float(r.total_deductions)
        net = float(r.net_pay)
        reimb = reimb_by_slip.get(r.slip_id, {})
        travel = reimb.get("travel_request", 0.0)
        expense = reimb.get("expense_claim", 0.0)
        total_gross += gross
        total_net += net
        total_travel += travel
        total_expense += expense
        rows.append([
            full_name,
            r.dept_name or "—",
            period,
            _fmt_inr(gross),
            _fmt_inr(deductions),
            _fmt_inr(net),
            _fmt_inr(travel),
            _fmt_inr(expense),
            _fmt_inr(net + travel + expense),
        ])

    summary = {
        "Total Gross Pay": _fmt_inr(total_gross),
        "Total Net Pay": _fmt_inr(total_net),
        "Employee Count": str(len(emp_ids)),
        "Travel Reimbursements": _fmt_inr(total_travel),
        "Expense Reimbursements": _fmt_inr(total_expense),
        "Total Payable": _fmt_inr(total_net + total_travel + total_expense),
    }

    return schemas.ReportOut(
        report_type="payroll",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
    )


@router.get("/project", response_model=schemas.ReportOut)
def project_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    company_id = current_user.company_id
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    # Fetch projects for this company -- "team" scope limits to projects the
    # caller's visible employees either manage or are allocated to.
    proj_stmt = (
        select(models.Project)
        .options(
            selectinload(models.Project.customer),
            selectinload(models.Project.project_manager),
        )
        .where(models.Project.company_id == company_id)
        .order_by(models.Project.name)
    )
    if branch_id is not None:
        proj_stmt = proj_stmt.where(models.Project.branch_id == branch_id)
    if visible_ids is not None:
        allocated_project_ids = select(models.ProjectAllocation.project_id).where(
            models.ProjectAllocation.employee_id.in_(visible_ids)
        )
        proj_stmt = proj_stmt.where(
            models.Project.project_manager_id.in_(visible_ids)
            | models.Project.id.in_(allocated_project_ids)
        )
    projects = db.execute(proj_stmt).scalars().all()

    if not projects:
        return schemas.ReportOut(
            report_type="project",
            from_date=str(from_date),
            to_date=str(to_date),
            columns=["Project", "Client", "Status", "PM", "Budget", "Logged Hrs", "Progress %", "Start", "End"],
            rows=[],
            summary={"Total Projects": "0", "Active": "0", "Avg Progress %": "0.0"},
        )

    project_ids = [p.id for p in projects]

    # Aggregate logged hours per project in date range
    hours_stmt = (
        select(
            models.WorkEntry.project_id,
            func.coalesce(func.sum(models.WorkEntry.hours), 0).label("logged_hours"),
        )
        .where(models.WorkEntry.project_id.in_(project_ids))
        .where(models.WorkEntry.entry_date >= from_date)
        .where(models.WorkEntry.entry_date <= to_date)
        .group_by(models.WorkEntry.project_id)
    )
    hours_by_project: dict[uuid.UUID, float] = {
        row.project_id: float(row.logged_hours)
        for row in db.execute(hours_stmt).all()
    }

    columns = ["Project", "Client", "Status", "PM", "Budget", "Logged Hrs", "Progress %", "Start", "End"]
    rows: list[list[str]] = []
    total_progress = 0
    active_count = 0

    # One grouped query each instead of a budget (+SAVEPOINT) and progress
    # query per project (PERF-02).
    budgets_by_project = crud.get_project_budgets_bulk(db, project_ids)
    progress_by_project = crud.get_project_progress_bulk(db, project_ids)

    for p in projects:
        logged = hours_by_project.get(p.id, 0.0)
        budget_row = budgets_by_project.get(p.id)
        budget_str = crud.format_budget_amount(
            float(budget_row.planned_amount) if budget_row else None
        )
        start_str = p.planned_start_date.isoformat() if p.planned_start_date else "—"
        end_str = p.planned_end_date.isoformat() if p.planned_end_date else "—"
        progress = progress_by_project.get(p.id, 0)
        total_progress += progress
        if p.status == "active":
            active_count += 1
        rows.append([
            p.name,
            p.customer.name if p.customer else "—",
            p.status,
            crud._full_name(p.project_manager) if p.project_manager_id else "—",
            budget_str,
            f"{logged:.1f}",
            f"{progress}%",
            start_str,
            end_str,
        ])

    avg_progress = round(total_progress / len(projects), 1) if projects else 0.0

    summary = {
        "Total Projects": str(len(projects)),
        "Active": str(active_count),
        "Avg Progress %": f"{avg_progress:.1f}",
    }

    return schemas.ReportOut(
        report_type="project",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
    )


@router.get("/performance", response_model=schemas.ReportOut)
def performance_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    company_id = current_user.company_id
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    # 1. Employees with recognition count in date range
    rec_stmt = (
        select(
            models.Employee.id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Department.name.label("dept_name"),
            models.Branch.name.label("branch_name"),
            func.coalesce(func.count(models.Recognition.id), 0).label("rec_count"),
        )
        .outerjoin(
            models.Recognition,
            (models.Recognition.employee_id == models.Employee.id)
            & (models.Recognition.given_on >= from_date)
            & (models.Recognition.given_on <= to_date),
        )
        .outerjoin(models.Department, models.Department.id == models.Employee.department_id)
        .outerjoin(models.Branch, models.Branch.id == models.Employee.branch_id)
        .where(models.Employee.company_id == company_id)
        .group_by(
            models.Employee.id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Department.name,
            models.Branch.name,
        )
        .order_by(models.Employee.first_name)
    )

    if branch_id is not None:
        rec_stmt = rec_stmt.where(models.Employee.branch_id == branch_id)
    if visible_ids is not None:
        rec_stmt = rec_stmt.where(models.Employee.id.in_(visible_ids))

    emp_rows = db.execute(rec_stmt).all()

    # 2. OKRs for the company — group by owner_name, average progress
    okr_stmt = (
        select(
            models.CompanyOkr.owner_name,
            func.avg(models.CompanyOkr.progress_pct).label("avg_progress"),
        )
        .where(models.CompanyOkr.company_id == company_id)
        .group_by(models.CompanyOkr.owner_name)
    )
    okr_by_name: dict[str, float] = {
        row.owner_name: float(row.avg_progress)
        for row in db.execute(okr_stmt).all()
    }

    # 3. Appraisal cycle review status
    cycle_stmt = (
        select(models.AppraisalCycle.status)
        .where(models.AppraisalCycle.company_id == company_id)
        .order_by(models.AppraisalCycle.from_date.desc())
        .limit(1)
    )
    latest_cycle = db.execute(cycle_stmt).scalar_one_or_none()
    review_status = "In Progress" if latest_cycle == "active" else "Completed"

    columns = ["Employee", "Department", "Branch", "OKR Progress", "Recognitions", "Review Status"]
    rows: list[list[str]] = []
    total_okr_progress = 0.0
    okr_count = 0
    total_recognitions = 0

    for r in emp_rows:
        full_name = f"{r.first_name} {r.last_name or ''}".strip()
        okr_val = okr_by_name.get(full_name)
        okr_str = f"{okr_val:.1f}%" if okr_val is not None else "—"
        if okr_val is not None:
            total_okr_progress += okr_val
            okr_count += 1
        rec = int(r.rec_count)
        total_recognitions += rec
        rows.append([
            full_name,
            r.dept_name or "—",
            r.branch_name or "—",
            okr_str,
            str(rec),
            review_status,
        ])

    avg_okr = round(total_okr_progress / okr_count, 1) if okr_count > 0 else 0.0

    summary = {
        "Total Employees": str(len(emp_rows)),
        "Avg OKR Progress": f"{avg_okr:.1f}%",
        "Total Recognitions": str(total_recognitions),
    }

    return schemas.ReportOut(
        report_type="performance",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
    )


@router.get("/utilization", response_model=schemas.ReportOut)
def utilization_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    company_id = current_user.company_id
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    billable_sum = func.coalesce(
        func.sum(case((models.WorkEntry.is_billable == True, models.WorkEntry.hours), else_=0)), 0  # noqa: E712
    ).label("billable_sum")
    total_sum = func.coalesce(func.sum(models.WorkEntry.hours), 0).label("total_sum")

    stmt = (
        select(
            models.Employee.id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Department.name.label("dept_name"),
            billable_sum,
            total_sum,
        )
        .outerjoin(
            models.Timesheet,
            models.Timesheet.employee_id == models.Employee.id,
        )
        .outerjoin(
            models.WorkEntry,
            (models.WorkEntry.timesheet_id == models.Timesheet.id)
            & (models.WorkEntry.entry_date >= from_date)
            & (models.WorkEntry.entry_date <= to_date),
        )
        .outerjoin(models.Department, models.Department.id == models.Employee.department_id)
        .where(models.Employee.company_id == company_id)
        .group_by(
            models.Employee.id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Department.name,
        )
        .order_by(models.Employee.first_name)
    )

    if branch_id is not None:
        stmt = stmt.where(models.Employee.branch_id == branch_id)
    if visible_ids is not None:
        stmt = stmt.where(models.Employee.id.in_(visible_ids))

    rows_raw = db.execute(stmt).all()

    columns = ["Employee", "Department", "Total Hrs", "Billable Hrs", "Utilization %"]
    rows: list[list[str]] = []
    grand_total = 0.0
    grand_billable = 0.0
    util_sum = 0.0
    util_count = 0

    for r in rows_raw:
        full_name = f"{r.first_name} {r.last_name or ''}".strip()
        total = float(r.total_sum)
        billable = float(r.billable_sum)
        grand_total += total
        grand_billable += billable
        if total > 0:
            util_pct = round(billable / total * 100, 1)
            util_str = f"{util_pct:.1f}%"
            util_sum += util_pct
            util_count += 1
        else:
            util_str = "—"
        rows.append([
            full_name,
            r.dept_name or "—",
            f"{total:.1f}",
            f"{billable:.1f}",
            util_str,
        ])

    avg_util = round(util_sum / util_count, 1) if util_count > 0 else 0.0

    summary = {
        "Avg Utilization %": f"{avg_util:.1f}%",
        "Total Billable Hours": f"{grand_billable:.1f}",
        "Total Logged Hours": f"{grand_total:.1f}",
    }

    return schemas.ReportOut(
        report_type="utilization",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
    )


@router.get("/recruitment", response_model=schemas.ReportOut)
def recruitment_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    company_id = current_user.company_id

    # Fetch job openings posted within the date range -- "team" scope limits
    # to openings in the caller's visible employees' own departments.
    opening_stmt = (
        select(models.JobOpening)
        .where(models.JobOpening.company_id == company_id)
        .where(models.JobOpening.posted_date >= from_date)
        .where(models.JobOpening.posted_date <= to_date)
        .order_by(models.JobOpening.posted_date.desc())
    )
    if branch_id is not None:
        opening_stmt = opening_stmt.where(models.JobOpening.branch_id == branch_id)
    if scope == "team":
        visible_ids = crud.get_team_scope_employee_ids(db, current_user)
        dept_ids = select(models.Employee.department_id).where(
            models.Employee.id.in_(visible_ids)
        )
        opening_stmt = opening_stmt.where(models.JobOpening.department_id.in_(dept_ids))
    openings = db.execute(opening_stmt).scalars().all()

    if not openings:
        return schemas.ReportOut(
            report_type="recruitment",
            from_date=str(from_date),
            to_date=str(to_date),
            columns=["Role", "Department", "Status", "Vacancies", "Applications", "Interviews", "Offers"],
            rows=[],
            summary={"Open Positions": "0", "Total Applications": "0", "Conversion Rate": "0.0%"},
        )

    opening_ids = [o.id for o in openings]

    # Count applications per opening
    app_stmt = (
        select(
            models.JobApplication.opening_id,
            func.count(models.JobApplication.id).label("app_count"),
        )
        .where(models.JobApplication.opening_id.in_(opening_ids))
        .group_by(models.JobApplication.opening_id)
    )
    apps_by_opening: dict[uuid.UUID, int] = {
        row.opening_id: int(row.app_count)
        for row in db.execute(app_stmt).all()
    }

    # Collect all application ids for this set of openings (for interview/offer counts)
    all_app_stmt = (
        select(models.JobApplication.id, models.JobApplication.opening_id)
        .where(models.JobApplication.opening_id.in_(opening_ids))
    )
    all_apps = db.execute(all_app_stmt).all()
    app_id_to_opening: dict[uuid.UUID, uuid.UUID] = {row.id: row.opening_id for row in all_apps}
    all_app_ids = list(app_id_to_opening.keys())

    # Count interviews per application_id, then map to opening
    interviews_by_opening: dict[uuid.UUID, int] = {}
    offers_by_opening: dict[uuid.UUID, int] = {}
    hires_by_opening: dict[uuid.UUID, int] = {}

    if all_app_ids:
        iv_stmt = (
            select(
                models.Interview.application_id,
                func.count(models.Interview.id).label("iv_count"),
            )
            .where(models.Interview.application_id.in_(all_app_ids))
            .group_by(models.Interview.application_id)
        )
        for row in db.execute(iv_stmt).all():
            oid = app_id_to_opening[row.application_id]
            interviews_by_opening[oid] = interviews_by_opening.get(oid, 0) + int(row.iv_count)

        # M-39: an offer is an application that has an hcm_offers row, or
        # that ever reached the offer stage (offer / preboarding / hired --
        # a hired application moved on from 'offer', which is why counting
        # stage == 'offer' showed 0 after a hire). Counted once per application.
        offered_apps = select(models.Offer.application_id).where(
            models.Offer.application_id.in_(all_app_ids)
        )
        _hired = func.coalesce(models.JobApplication.status, models.JobApplication.stage) == "hired"
        offer_stmt = (
            select(
                models.JobApplication.opening_id,
                func.count(models.JobApplication.id).label("offer_count"),
                func.count(models.JobApplication.id).filter(_hired).label("hire_count"),
            )
            .where(models.JobApplication.opening_id.in_(opening_ids))
            .where(models.JobApplication.deleted_at.is_(None))
            .where(
                models.JobApplication.id.in_(offered_apps)
                | func.coalesce(models.JobApplication.status, models.JobApplication.stage).in_(
                    ("offer", "preboarding", "hired")
                )
            )
            .group_by(models.JobApplication.opening_id)
        )
        for row in db.execute(offer_stmt).all():
            offers_by_opening[row.opening_id] = int(row.offer_count)
            hires_by_opening[row.opening_id] = int(row.hire_count)

    # Department name lookup
    dept_ids = list({o.department_id for o in openings if o.department_id is not None})
    dept_names: dict[uuid.UUID, str] = {}
    if dept_ids:
        dept_stmt = (
            select(models.Department.id, models.Department.name)
            .where(models.Department.id.in_(dept_ids))
        )
        for row in db.execute(dept_stmt).all():
            dept_names[row.id] = row.name

    columns = ["Role", "Department", "Status", "Vacancies", "Applications", "Interviews", "Offers"]
    rows: list[list[str]] = []
    total_applications = 0
    total_offers = 0
    open_positions = 0

    for o in openings:
        app_count = apps_by_opening.get(o.id, 0)
        iv_count = interviews_by_opening.get(o.id, 0)
        offer_count = offers_by_opening.get(o.id, 0)
        dept_name = dept_names.get(o.department_id, "—") if o.department_id else "—"
        total_applications += app_count
        total_offers += offer_count
        if o.status == "open":
            open_positions += 1
        rows.append([
            o.title,
            dept_name,
            o.status,
            str(o.vacancies),
            str(app_count),
            str(iv_count),
            str(offer_count),
        ])

    # M-39: conversion = hires / applications (offers reported separately).
    total_hires = sum(hires_by_opening.values())
    conversion_rate = (
        round(total_hires / total_applications * 100, 1) if total_applications > 0 else 0.0
    )

    summary = {
        "Open Positions": str(open_positions),
        "Total Applications": str(total_applications),
        "Offers": str(total_offers),
        "Hires": str(total_hires),
        "Conversion Rate": f"{conversion_rate:.1f}%",
    }

    return schemas.ReportOut(
        report_type="recruitment",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
    )


@router.get("/leave", response_model=schemas.ReportOut)
def leave_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    designation_id: uuid.UUID | None = None,
    manager_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    status: str | None = None,
    compare_previous: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    """Leave Requests -- any request OVERLAPPING [from_date, to_date] (not
    just ones starting inside it), so a multi-day leave spanning the range
    boundary is still counted."""
    company_id = current_user.company_id
    company_tz = crud.company_tzinfo(db, company_id)
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    def _compute(f_date: datetime.date, t_date: datetime.date):
        stmt = (
            select(
                models.LeaveRequest,
                models.Employee.first_name,
                models.Employee.last_name,
                models.Branch.name.label("branch_name"),
                models.Department.name.label("dept_name"),
                models.LeaveType.name.label("leave_type_name"),
            )
            .join(models.Employee, models.Employee.id == models.LeaveRequest.employee_id)
            .join(models.LeaveType, models.LeaveType.id == models.LeaveRequest.leave_type_id)
            .outerjoin(models.Branch, models.Branch.id == models.Employee.branch_id)
            .outerjoin(models.Department, models.Department.id == models.Employee.department_id)
            .where(
                models.LeaveRequest.company_id == company_id,
                models.LeaveRequest.from_date <= t_date,
                models.LeaveRequest.to_date >= f_date,
            )
            .order_by(models.LeaveRequest.from_date.desc())
        )
        stmt = _employee_dimension_filters(
            stmt, employee_id, department_id, designation_id, manager_id, branch_id
        )
        if status is not None:
            stmt = stmt.where(models.LeaveRequest.status == status)
        if visible_ids is not None:
            stmt = stmt.where(models.Employee.id.in_(visible_ids))
        return db.execute(stmt).all()

    rows_raw = _compute(from_date, to_date)

    columns = ["Employee", "Department", "Branch", "Leave Type", "From", "To", "Days", "Half Day", "Status"]
    rows: list[list[str]] = []
    status_counts: dict[str, float] = {}
    type_days: dict[str, float] = {}
    dept_days: dict[str, float] = {}
    daily_submitted: dict[datetime.date, float] = {}
    approved_days = 0.0

    for r in rows_raw:
        lr = r.LeaveRequest
        full_name = f"{r.first_name} {r.last_name or ''}".strip()
        days = float(lr.days)
        if lr.status == "approved":
            approved_days += days
        status_counts[lr.status] = status_counts.get(lr.status, 0) + 1
        type_days[r.leave_type_name] = type_days.get(r.leave_type_name, 0) + days
        dept_name = r.dept_name or "—"
        dept_days[dept_name] = dept_days.get(dept_name, 0) + days
        submitted_date = lr.created_at.astimezone(company_tz).date() if lr.created_at else lr.from_date
        daily_submitted[submitted_date] = daily_submitted.get(submitted_date, 0) + 1
        half_day_str = (
            f"{lr.half_day_period.title()} Half" if lr.is_half_day and lr.half_day_period
            else ("Yes" if lr.is_half_day else "No")
        )
        rows.append([
            full_name, dept_name, r.branch_name or "—", r.leave_type_name,
            lr.from_date.isoformat(), lr.to_date.isoformat(), f"{days:.1f}",
            half_day_str, lr.status.replace("_", " ").title(),
        ])

    total_requests = len(rows_raw)
    pending = float(status_counts.get("pending", 0))
    summary = {
        "Total Requests": str(total_requests),
        "Approved Days": f"{approved_days:.1f}",
        "Pending": str(int(pending)),
    }

    charts = [
        rh.donut_chart(
            "Leave Status Breakdown", "Requests",
            [(s.replace("_", " ").title(), c) for s, c in sorted(status_counts.items())],
        ),
        rh.bar_chart("Leave Days by Type", "Days", sorted(type_days.items(), key=lambda kv: kv[0])),
        rh.bar_chart("Leave Days by Department", "Days", sorted(dept_days.items(), key=lambda kv: kv[0])),
        rh.line_chart(
            "Leave Requests Submitted", "Requests",
            rh.bucket_daily_values(daily_submitted, from_date, to_date),
        ),
    ]

    comparison = None
    if compare_previous:
        prev_from, prev_to = rh.previous_period(from_date, to_date)
        prev_rows = _compute(prev_from, prev_to)
        prev_approved_days = sum(
            float(r.LeaveRequest.days) for r in prev_rows if r.LeaveRequest.status == "approved"
        )
        prev_pending = sum(1 for r in prev_rows if r.LeaveRequest.status == "pending")
        comparison = rh.build_comparison(
            {"Total Requests": float(total_requests), "Approved Days": approved_days, "Pending": pending},
            {
                "Total Requests": float(len(prev_rows)),
                "Approved Days": prev_approved_days,
                "Pending": float(prev_pending),
            },
        )

    return schemas.ReportOut(
        report_type="leave",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
        charts=charts,
        comparison=comparison,
    )


@router.get("/regularization", response_model=schemas.ReportOut)
def regularization_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    designation_id: uuid.UUID | None = None,
    manager_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    status: str | None = None,
    compare_previous: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    company_id = current_user.company_id
    company_tz = crud.company_tzinfo(db, company_id)
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    def _compute(f_date: datetime.date, t_date: datetime.date):
        stmt = (
            select(
                models.AttendanceRegularization,
                models.Employee.first_name,
                models.Employee.last_name,
                models.Branch.name.label("branch_name"),
                models.Department.name.label("dept_name"),
            )
            .join(models.Employee, models.Employee.id == models.AttendanceRegularization.employee_id)
            .outerjoin(models.Branch, models.Branch.id == models.Employee.branch_id)
            .outerjoin(models.Department, models.Department.id == models.Employee.department_id)
            .where(
                models.Employee.company_id == company_id,
                models.AttendanceRegularization.attendance_date >= f_date,
                models.AttendanceRegularization.attendance_date <= t_date,
            )
            .order_by(models.AttendanceRegularization.attendance_date.desc())
        )
        stmt = _employee_dimension_filters(
            stmt, employee_id, department_id, designation_id, manager_id, branch_id
        )
        if status is not None:
            stmt = stmt.where(models.AttendanceRegularization.status == status)
        if visible_ids is not None:
            stmt = stmt.where(models.Employee.id.in_(visible_ids))
        return db.execute(stmt).all()

    rows_raw = _compute(from_date, to_date)

    columns = ["Employee", "Department", "Branch", "Date", "Requested In", "Requested Out", "Reason", "Status"]
    rows: list[list[str]] = []
    status_counts: dict[str, float] = {}
    dept_counts: dict[str, float] = {}
    daily_counts: dict[datetime.date, float] = {}

    for r in rows_raw:
        reg = r.AttendanceRegularization
        full_name = f"{r.first_name} {r.last_name or ''}".strip()
        status_counts[reg.status] = status_counts.get(reg.status, 0) + 1
        dept_name = r.dept_name or "—"
        dept_counts[dept_name] = dept_counts.get(dept_name, 0) + 1
        daily_counts[reg.attendance_date] = daily_counts.get(reg.attendance_date, 0) + 1
        rows.append([
            full_name, dept_name, r.branch_name or "—", reg.attendance_date.isoformat(),
            reg.requested_in.astimezone(company_tz).strftime("%H:%M") if reg.requested_in else "—",
            reg.requested_out.astimezone(company_tz).strftime("%H:%M") if reg.requested_out else "—",
            reg.reason, reg.status.replace("_", " ").title(),
        ])

    total_requests = len(rows_raw)
    approved = float(status_counts.get("approved", 0))
    pending = float(status_counts.get("pending", 0))
    summary = {
        "Total Requests": str(total_requests),
        "Approved": str(int(approved)),
        "Pending": str(int(pending)),
    }

    charts = [
        rh.donut_chart(
            "Regularization Status Breakdown", "Requests",
            [(s.replace("_", " ").title(), c) for s, c in sorted(status_counts.items())],
        ),
        rh.bar_chart("Regularizations by Department", "Requests", sorted(dept_counts.items())),
        rh.line_chart(
            "Regularization Requests Trend", "Requests",
            rh.bucket_daily_values(daily_counts, from_date, to_date),
        ),
    ]

    comparison = None
    if compare_previous:
        prev_from, prev_to = rh.previous_period(from_date, to_date)
        prev_rows = _compute(prev_from, prev_to)
        prev_status_counts: dict[str, float] = {}
        for r in prev_rows:
            prev_status_counts[r.AttendanceRegularization.status] = (
                prev_status_counts.get(r.AttendanceRegularization.status, 0) + 1
            )
        comparison = rh.build_comparison(
            {"Total Requests": float(total_requests), "Approved": approved, "Pending": pending},
            {
                "Total Requests": float(len(prev_rows)),
                "Approved": float(prev_status_counts.get("approved", 0)),
                "Pending": float(prev_status_counts.get("pending", 0)),
            },
        )

    return schemas.ReportOut(
        report_type="regularization",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
        charts=charts,
        comparison=comparison,
    )


@router.get("/work-entry", response_model=schemas.ReportOut)
def work_entry_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    designation_id: uuid.UUID | None = None,
    manager_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    compare_previous: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    """Work Entry -- day-level task/hours log detail (pm_time_entries),
    distinct from the /timesheet report's per-employee×project totals:
    this one surfaces category mix, daily volume trend, and the
    containing Timesheet's submission-workflow status (draft / submitted /
    approved / rejected), not just billable-vs-non-billable hours."""
    company_id = current_user.company_id
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    def _compute(f_date: datetime.date, t_date: datetime.date):
        stmt = (
            select(
                models.WorkEntry,
                models.Employee.first_name,
                models.Employee.last_name,
                models.Branch.name.label("branch_name"),
                models.Department.name.label("dept_name"),
                models.Project.name.label("project_name"),
                models.Timesheet.status.label("timesheet_status"),
            )
            .join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
            .join(models.Employee, models.Employee.id == models.Timesheet.employee_id)
            .outerjoin(models.Project, models.Project.id == models.WorkEntry.project_id)
            .outerjoin(models.Branch, models.Branch.id == models.Employee.branch_id)
            .outerjoin(models.Department, models.Department.id == models.Employee.department_id)
            .where(
                models.Employee.company_id == company_id,
                models.WorkEntry.entry_date >= f_date,
                models.WorkEntry.entry_date <= t_date,
            )
            .order_by(models.WorkEntry.entry_date.desc())
        )
        stmt = _employee_dimension_filters(
            stmt, employee_id, department_id, designation_id, manager_id, branch_id
        )
        if project_id is not None:
            stmt = stmt.where(models.WorkEntry.project_id == project_id)
        if status is not None:
            stmt = stmt.where(models.Timesheet.status == status)
        if visible_ids is not None:
            stmt = stmt.where(models.Employee.id.in_(visible_ids))
        return db.execute(stmt).all()

    rows_raw = _compute(from_date, to_date)

    columns = ["Employee", "Department", "Branch", "Date", "Project", "Category", "Hours", "Billable", "Timesheet Status"]
    rows: list[list[str]] = []
    category_hours: dict[str, float] = {}
    dept_hours: dict[str, float] = {}
    status_counts: dict[str, float] = {}
    daily_hours: dict[datetime.date, float] = {}
    total_hours = 0.0
    billable_hours = 0.0

    for r in rows_raw:
        we = r.WorkEntry
        full_name = f"{r.first_name} {r.last_name or ''}".strip()
        hours = float(we.hours)
        total_hours += hours
        if we.is_billable:
            billable_hours += hours
        category = we.category or "Uncategorized"
        category_hours[category] = category_hours.get(category, 0) + hours
        dept_name = r.dept_name or "—"
        dept_hours[dept_name] = dept_hours.get(dept_name, 0) + hours
        status_counts[r.timesheet_status] = status_counts.get(r.timesheet_status, 0) + 1
        daily_hours[we.entry_date] = daily_hours.get(we.entry_date, 0) + hours
        rows.append([
            full_name, dept_name, r.branch_name or "—", we.entry_date.isoformat(),
            r.project_name or "—", category, f"{hours:.1f}",
            "Yes" if we.is_billable else "No", r.timesheet_status.replace("_", " ").title(),
        ])

    summary = {
        "Total Hours": f"{total_hours:.1f}",
        "Billable Hours": f"{billable_hours:.1f}",
        "Entries": str(len(rows_raw)),
    }

    charts = [
        rh.donut_chart(
            "Work Entry Category Breakdown", "Hours",
            sorted(category_hours.items(), key=lambda kv: -kv[1]),
        ),
        rh.bar_chart("Hours Logged by Department", "Hours", sorted(dept_hours.items())),
        rh.donut_chart(
            "Timesheet Status Breakdown", "Entries",
            [(s.replace("_", " ").title(), c) for s, c in sorted(status_counts.items())],
        ),
        rh.line_chart(
            "Daily Hours Logged Trend", "Hours",
            rh.bucket_daily_values(daily_hours, from_date, to_date),
        ),
    ]

    comparison = None
    if compare_previous:
        prev_from, prev_to = rh.previous_period(from_date, to_date)
        prev_rows = _compute(prev_from, prev_to)
        prev_total = sum(float(r.WorkEntry.hours) for r in prev_rows)
        prev_billable = sum(float(r.WorkEntry.hours) for r in prev_rows if r.WorkEntry.is_billable)
        comparison = rh.build_comparison(
            {"Total Hours": total_hours, "Billable Hours": billable_hours, "Entries": float(len(rows_raw))},
            {"Total Hours": prev_total, "Billable Hours": prev_billable, "Entries": float(len(prev_rows))},
        )

    return schemas.ReportOut(
        report_type="work_entry",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
        charts=charts,
        comparison=comparison,
    )


@router.get("/people", response_model=schemas.ReportOut)
def people_report(
    from_date: datetime.date,
    to_date: datetime.date,
    branch_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    designation_id: uuid.UUID | None = None,
    manager_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    status: str | None = None,
    compare_previous: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
    scope: str = Depends(get_reports_scope),
) -> schemas.ReportOut:
    """People / Workforce -- Total Headcount is always a CURRENT snapshot
    (this schema has no historical point-in-time headcount table to ask
    "as of to_date" honestly), but New Joiners and Exits are genuinely
    scoped to [from_date, to_date] (date_of_joining / an approved exit
    request's last_working_day, respectively), so those two -- and only
    those two -- are the ones compared period-over-period below."""
    company_id = current_user.company_id
    visible_ids = (
        crud.get_team_scope_employee_ids(db, current_user) if scope == "team" else None
    )

    stmt = (
        select(
            models.Employee.id,
            models.Employee.first_name,
            models.Employee.last_name,
            models.Employee.employee_code,
            models.Employee.gender,
            models.Employee.employment_type,
            models.Employee.status,
            models.Employee.date_of_joining,
            models.Branch.name.label("branch_name"),
            models.Department.name.label("dept_name"),
            models.Designation.name.label("designation_name"),
        )
        .outerjoin(models.Branch, models.Branch.id == models.Employee.branch_id)
        .outerjoin(models.Department, models.Department.id == models.Employee.department_id)
        .outerjoin(models.Designation, models.Designation.id == models.Employee.designation_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.Employee.first_name)
    )
    stmt = _employee_dimension_filters(
        stmt, employee_id, department_id, designation_id, manager_id, branch_id
    )
    if status is not None:
        stmt = stmt.where(func.lower(models.Employee.status) == status.strip().lower())
    if visible_ids is not None:
        stmt = stmt.where(models.Employee.id.in_(visible_ids))
    all_rows = db.execute(stmt).all()

    columns = ["Employee", "Code", "Department", "Branch", "Designation", "Gender", "Employment Type", "Date of Joining", "Status"]
    rows: list[list[str]] = []
    dept_counts: dict[str, float] = {}
    branch_counts: dict[str, float] = {}
    designation_counts: dict[str, float] = {}
    gender_counts: dict[str, float] = {}
    type_counts: dict[str, float] = {}
    daily_joiners: dict[datetime.date, float] = {}

    for r in all_rows:
        full_name = f"{r.first_name} {r.last_name or ''}".strip()
        dept_name = r.dept_name or "—"
        dept_counts[dept_name] = dept_counts.get(dept_name, 0) + 1
        branch_name = r.branch_name or "—"
        branch_counts[branch_name] = branch_counts.get(branch_name, 0) + 1
        designation_name = r.designation_name or "—"
        designation_counts[designation_name] = designation_counts.get(designation_name, 0) + 1
        gender = (r.gender or "Not specified").title()
        gender_counts[gender] = gender_counts.get(gender, 0) + 1
        emp_type = r.employment_type.replace("_", " ").title()
        type_counts[emp_type] = type_counts.get(emp_type, 0) + 1
        if from_date <= r.date_of_joining <= to_date:
            daily_joiners[r.date_of_joining] = daily_joiners.get(r.date_of_joining, 0) + 1
        rows.append([
            full_name, r.employee_code, dept_name, branch_name, designation_name,
            gender, emp_type, r.date_of_joining.isoformat(), r.status.replace("_", " ").title(),
        ])

    def _new_joiner_count(f_date: datetime.date, t_date: datetime.date) -> int:
        s = select(func.count(models.Employee.id)).where(
            models.Employee.company_id == company_id,
            models.Employee.date_of_joining >= f_date,
            models.Employee.date_of_joining <= t_date,
        )
        s = _employee_dimension_filters(s, employee_id, department_id, designation_id, manager_id, branch_id)
        if visible_ids is not None:
            s = s.where(models.Employee.id.in_(visible_ids))
        return int(db.scalar(s) or 0)

    def _exit_count(f_date: datetime.date, t_date: datetime.date) -> int:
        s = (
            select(func.count(models.ExitRequestModel.id))
            .join(models.Employee, models.Employee.id == models.ExitRequestModel.employee_id)
            .where(
                models.Employee.company_id == company_id,
                models.ExitRequestModel.status == "approved",
                models.ExitRequestModel.last_working_day.is_not(None),
                models.ExitRequestModel.last_working_day >= f_date,
                models.ExitRequestModel.last_working_day <= t_date,
            )
        )
        s = _employee_dimension_filters(s, employee_id, department_id, designation_id, manager_id, branch_id)
        if visible_ids is not None:
            s = s.where(models.Employee.id.in_(visible_ids))
        return int(db.scalar(s) or 0)

    new_joiners = _new_joiner_count(from_date, to_date)
    exits_in_period = _exit_count(from_date, to_date)
    total_headcount = len(all_rows)

    summary = {
        "Total Headcount": str(total_headcount),
        "New Joiners (Period)": str(new_joiners),
        "Exits (Period)": str(exits_in_period),
    }

    charts = [
        rh.bar_chart("Headcount by Department", "Employees", sorted(dept_counts.items())),
        rh.bar_chart("Headcount by Branch", "Employees", sorted(branch_counts.items())),
        rh.bar_chart("Headcount by Designation", "Employees", sorted(designation_counts.items())),
        rh.donut_chart("Gender Breakdown", "Employees", sorted(gender_counts.items())),
        rh.donut_chart("Employment Type Breakdown", "Employees", sorted(type_counts.items())),
        rh.line_chart(
            "New Joiners Trend", "Joiners",
            rh.bucket_daily_values(daily_joiners, from_date, to_date),
        ),
    ]

    comparison = None
    if compare_previous:
        prev_from, prev_to = rh.previous_period(from_date, to_date)
        comparison = rh.build_comparison(
            {"New Joiners (Period)": float(new_joiners), "Exits (Period)": float(exits_in_period)},
            {
                "New Joiners (Period)": float(_new_joiner_count(prev_from, prev_to)),
                "Exits (Period)": float(_exit_count(prev_from, prev_to)),
            },
        )

    return schemas.ReportOut(
        report_type="people",
        from_date=str(from_date),
        to_date=str(to_date),
        columns=columns,
        rows=rows,
        summary=summary,
        charts=charts,
        comparison=comparison,
    )


@router.post("/export/pdf")
def export_report_pdf(
    payload: schemas.ReportPdfExportRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("reports_analytics", "reports", "view")
    ),
):
    """Renders the report the caller is viewing into a branded PDF
    (company logo + letterhead, KPI tiles, charts, detail table, page
    numbers). L-24: needs the reports permission, and the columns / rows /
    summary / charts are produced here by re-running [payload.report_type]
    for the caller (report_export.build_report_data) -- client-supplied
    rows are never printed. Title / subtitle / period / filter labels are
    display text only (HTML-escaped by the renderer)."""
    if payload.report_type not in report_export.REPORT_TYPES:
        raise HTTPException(status_code=422, detail="Unknown report type.")
    drill = None
    if payload.drill_down_column and payload.drill_down_value is not None:
        drill = (payload.drill_down_column, payload.drill_down_value)
    data = report_export.build_report_data(db, current_user, payload.report_type, payload.params, drill)
    doc = payload.model_dump(include={"title", "subtitle", "period_label", "generated_at", "filters"})
    doc.update(data)
    company = db.get(models.Company, current_user.company_id)
    generated_by = current_user.full_name or current_user.email
    try:
        pdf_bytes = report_pdf_renderer.render_report_pdf(doc, company, generated_by)
    except Exception as exc:  # noqa: BLE001 - renderer/Chromium failure
        raise HTTPException(status_code=500, detail="Couldn't generate the PDF. Please try again.") from exc
    slug = re.sub(r"[^a-z0-9]+", "-", payload.title.lower()).strip("-") or "report"
    filename = f"{slug}-{datetime.date.today().isoformat()}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
