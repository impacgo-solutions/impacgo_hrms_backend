"""Configurable overtime end to end: Request (start time) -> Approval ->
Session (auto start / end, end early) -> Hours -> Payable (payroll
"Overtime Pay") or Compensatory Leave (leave balance) -> Report; settings
permissions, duplicate prevention, rejected / below-minimum requests.

    cd backend && venv/Scripts/python -m unittest tests.test_overtime -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant impacgo-solutions by default, OVERTIME_TEST_TENANT to
override). Time is simulated with explicit `now` values.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, email_service, models, overtime, schemas  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import overtime_settings as settings_api  # noqa: E402
from app.routers import work as work_api  # noqa: E402

TENANT = os.environ.get("OVERTIME_TEST_TENANT", "impacgo-solutions")
UTC = datetime.timezone.utc
# A Wednesday at least 30 days ahead: overtime windows in the past are now
# refused (H-19), so a fixed date would make the suite fail once it passed.
_base = datetime.date.today() + datetime.timedelta(days=30)
WORK_DATE = _base + datetime.timedelta(days=(2 - _base.weekday()) % 7)  # future date (sessions are simulated)


class OvertimeTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        mock.patch.object(email_service._executor, "submit", lambda *a, **k: None).start()
        users = self.db.scalars(select(models.User)).all()
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                           and r.name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("no Owner")
        self.company_id = self.owner.company_id
        # An employee with a salary structure and a reporting manager who logs in.
        self.employee = self.manager = None
        for e in self.db.scalars(select(models.Employee).where(models.Employee.company_id == self.company_id,
                                                                 models.Employee.is_active.is_(True))).all():
            m_user = crud.get_user_id_for_employee(self.db, e.reporting_manager_id) if e.reporting_manager_id else None
            a = crud._resolve_assignment_as_of(self.db, e.id, WORK_DATE)
            if m_user and a is not None and a.annual_ctc and crud.get_user_id_for_employee(self.db, e.id):
                self.employee = e
                self.manager = self.db.get(models.User, m_user)
                self.employee_user = self.db.get(models.User, crud.get_user_id_for_employee(self.db, e.id))
                break
        if self.employee is None:
            self.skipTest("no employee with salary structure + reporting manager")
        self.tz = crud.company_tzinfo(self.db, self.company_id)
        # A known regular shift (00:30-05:30) so the test sessions (06:00-
        # 23:59) are always outside it -- overtime must never overlap the
        # shift. Rolled back with everything else.
        self.base_shift = self.make_shift("QA OT base", "00:30", "05:30")
        crud.create_shift_assignment(self.db, self.employee.id, self.base_shift, WORK_DATE - datetime.timedelta(days=60))

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # ── helpers ────────────────────────────────────────────────────────
    def make_shift(self, name, start, end):
        h1, m1 = map(int, start.split(":")); h2, m2 = map(int, end.split(":"))
        sh = crud.create_shift(self.db, self.company_id, f"{name} {uuid.uuid4().hex[:6]}",
                               datetime.time(h1, m1), datetime.time(h2, m2),
                               # M-15: only a night shift may cross midnight.
                               datetime.time(h2, m2) < datetime.time(h1, m1))
        return sh.id

    def save_settings(self, **kw):
        base = dict(enabled=True, compensation_mode="payable", rate_basis="basic", rate_multiplier=2,
                    monthly_days_divisor=26, hours_per_day=8, fixed_hourly_rate=None, comp_off_leave_type_id=None,
                    comp_off_full_day_hours=8, comp_off_half_day_hours=4, min_minutes=30, rounding="exact")
        base.update(kw)
        return settings_api.save_overtime_settings(schemas.OvertimeSettingsUpdate(**base), db=self.db,
                                                   current_user=self.owner)

    def request(self, start=datetime.time(18, 0), hours=2.5, day=WORK_DATE):
        return work_api.create_overtime_request(
            schemas.OvertimeRequestCreate(employee_id=self.employee.id, work_date=day, start_time=start,
                                          hours=hours, reason="Release support"),
            db=self.db, current_user=self.employee_user)

    def approve(self, out):
        return work_api.update_overtime_request(out.id, schemas.OvertimeRequestUpdate(status="approved"),
                                                db=self.db, current_user=self.manager)

    def local(self, day, hh, mm=0):
        return datetime.datetime.combine(day, datetime.time(hh, mm)).replace(tzinfo=self.tz).astimezone(UTC)

    def row(self, out):
        return self.db.get(models.OvertimeRequest, out.id)

    def work(self, out, minutes=None):
        """The employee clocks in at the approved start and out at the end
        (or after [minutes]) -- manual punches; nothing is automatic."""
        r = self.row(out)
        overtime.clock_in(self.db, self.company_id, r, now=r.planned_start)
        end = r.planned_end if minutes is None else r.planned_start + datetime.timedelta(minutes=minutes)
        overtime.clock_out(self.db, self.company_id, r, self.employee.id, now=end)
        return r

    def comp(self, out):
        return self.db.scalar(select(models.OvertimeCompensation).where(
            models.OvertimeCompensation.overtime_request_id == out.id))

    # ── settings ───────────────────────────────────────────────────────
    def test_settings_defaults_and_permissions(self):
        out = settings_api.get_overtime_settings(db=self.db, current_user=self.employee_user)
        self.assertEqual(out.compensation_mode, "payable")
        self.assertEqual(out.rate_multiplier, 2.0)
        self.assertIsNotNone(out.comp_off_leave_type_id)  # the company's Comp-Off type
        if not out.can_edit:
            with self.assertRaises(HTTPException) as ctx:
                settings_api.save_overtime_settings(
                    schemas.OvertimeSettingsUpdate(enabled=True, compensation_mode="comp_off", rate_basis="basic",
                                                   rate_multiplier=2, monthly_days_divisor=26, hours_per_day=8,
                                                   comp_off_full_day_hours=8, comp_off_half_day_hours=4,
                                                   min_minutes=30, rounding="exact"),
                    db=self.db, current_user=self.employee_user)
            self.assertEqual(ctx.exception.status_code, 403)
        saved = self.save_settings(compensation_mode="comp_off")
        self.assertTrue(saved.saved)
        self.assertEqual(saved.compensation_mode, "comp_off")
        with self.assertRaises(ValidationError):  # fixed basis needs a rate
            schemas.OvertimeSettingsUpdate(enabled=True, compensation_mode="payable", rate_basis="fixed",
                                           rate_multiplier=1, monthly_days_divisor=26, hours_per_day=8,
                                           comp_off_full_day_hours=8, comp_off_half_day_hours=4,
                                           min_minutes=0, rounding="exact")

    # ── payable flow ───────────────────────────────────────────────────
    def test_payable_flow_into_payroll_once(self):
        self.save_settings()
        req = self.request()
        r = self.row(req)
        self.assertEqual(r.planned_start, self.local(WORK_DATE, 18))
        self.assertEqual(r.planned_end, self.local(WORK_DATE, 20, 30))
        self.approve(req)
        r = self.row(req)
        self.assertEqual(r.session_status, "scheduled")  # work date is in the future

        # Never automatic: at 19:00 without a clock-in it is NOT in progress.
        overtime.advance_sessions(self.db, self.company_id, now=self.local(WORK_DATE, 17, 59))
        self.assertEqual(r.session_status, "scheduled")
        overtime.clock_in(self.db, self.company_id, r, now=self.local(WORK_DATE, 18))
        self.assertEqual(r.session_status, "clocked_in")
        self.assertIsNone(self.comp(req))
        attendance_before = self.db.scalar(select(models.AttendanceRecord.overtime_hours).where(
            models.AttendanceRecord.employee_id == self.employee.id,
            models.AttendanceRecord.attendance_date == WORK_DATE)) or 0
        overtime.advance_sessions(self.db, self.company_id, now=self.local(WORK_DATE, 20, 35))  # within grace
        self.assertEqual(r.session_status, "clocked_in", "never clocked out automatically")
        overtime.clock_out(self.db, self.company_id, r, self.employee.id, now=self.local(WORK_DATE, 20, 30))
        self.assertEqual(r.session_status, "completed")
        self.assertEqual(r.actual_minutes, 150)
        self.assertEqual(r.actual_start, r.planned_start)
        self.assertEqual(r.actual_end, r.planned_end)
        attendance = self.db.scalar(select(models.AttendanceRecord.overtime_hours).where(
            models.AttendanceRecord.employee_id == self.employee.id,
            models.AttendanceRecord.attendance_date == WORK_DATE))
        self.assertAlmostEqual(float(attendance) - float(attendance_before), 2.5)

        c = self.comp(req)
        self.assertEqual((c.mode, c.status, c.counted_minutes), ("payable", "pending_payroll", 150))
        rate, _basis = overtime.hourly_rate(self.db, self.company_id, self.employee.id, WORK_DATE,
                                            overtime.get_settings(self.db, self.company_id))
        self.assertAlmostEqual(float(c.amount), round(rate * 2 * 2.5, 2), places=2)
        # Never twice.
        overtime.advance_sessions(self.db, self.company_id, now=self.local(WORK_DATE, 23))
        overtime.process_compensation(self.db, self.company_id, r)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(models.OvertimeCompensation).where(
            models.OvertimeCompensation.overtime_request_id == req.id)), 1)

        # Payroll: November run carries one "Overtime Pay" line; regenerate -> still one.
        run = self.db.scalar(select(models.PayrollRun).where(
            models.PayrollRun.company_id == self.company_id, models.PayrollRun.period_month == WORK_DATE.month,
            models.PayrollRun.period_year == WORK_DATE.year)) or crud.create_payroll_run(
            self.db, self.company_id, WORK_DATE.month, WORK_DATE.year)
        for _ in range(2):
            crud.generate_payroll_slips(self.db, self.company_id, run.id)
            slip = self.db.scalar(select(models.SalarySlip).where(
                models.SalarySlip.payroll_run_id == run.id, models.SalarySlip.employee_id == self.employee.id))
            lines = self.db.execute(select(models.SalarySlipLine, models.SalaryComponent)
                                    .join(models.SalaryComponent, models.SalaryComponent.id == models.SalarySlipLine.component_id)
                                    .where(models.SalarySlipLine.slip_id == slip.id,
                                           models.SalaryComponent.code == overtime.OT_PAY_CODE)).all()
            self.assertEqual(len(lines), 1)
            self.assertAlmostEqual(float(lines[0][0].amount), float(c.amount), places=2)
        self.assertEqual((c.status, c.applied_payroll_run_id), ("in_payroll", run.id))
        out = work_api._overtime_request_out(self.db, r, viewer=self.employee_user)
        self.assertEqual(out.compensation.payroll_period, "November 2026")
        self.assertEqual(out.actual_duration, "2h 30m")

    def test_end_early_and_rounding(self):
        self.save_settings(rounding="nearest_15")
        req = self.request(hours=3)
        self.approve(req)
        r = self.row(req)
        overtime.clock_in(self.db, self.company_id, r, now=self.local(WORK_DATE, 18))
        with self.assertRaises(HTTPException):  # someone else cannot end it
            other = next(u for u in self.db.scalars(select(models.User)).all()
                         if u.employee_id not in (r.employee_id, r.approver_id) and u.company_id == self.company_id)
            work_api.end_overtime_session(req.id, db=self.db, current_user=other)
        overtime.clock_out(self.db, self.company_id, r, self.employee.id, now=self.local(WORK_DATE, 19, 52))
        self.assertEqual(r.session_status, "completed")
        self.assertEqual(r.actual_minutes, 112)          # 18:00 -> 19:52
        self.assertEqual(self.comp(req).counted_minutes, 105)  # nearest 15
        with self.assertRaises(ValueError):
            overtime.end_early(self.db, self.company_id, r, self.employee.id)

    # ── comp-off flow ──────────────────────────────────────────────────
    def test_comp_off_credits_leave_balance(self):
        settings = self.save_settings(compensation_mode="comp_off")
        leave_type = self.db.get(models.LeaveType, settings.comp_off_leave_type_id)
        fy = crud.get_or_create_fiscal_year(self.db, self.company_id, WORK_DATE)

        def balance():
            a = self.db.scalar(select(models.LeaveAllocation).where(
                models.LeaveAllocation.employee_id == self.employee.id,
                models.LeaveAllocation.leave_type_id == leave_type.id,
                models.LeaveAllocation.fiscal_year_id == fy.id))
            return float(a.allocated_days) if a else 0.0

        before = balance()
        req = self.request(start=datetime.time(9, 0), hours=9)  # 9 h -> 1 day (8 h) + remainder 1 h
        self.approve(req)
        self.work(req)
        c = self.comp(req)
        self.assertEqual((c.mode, c.status, float(c.leave_days)), ("comp_off", "credited", 1.0))
        self.assertEqual(balance(), before + 1.0)
        # 5 h -> half day
        req2 = self.request(start=datetime.time(9, 0), hours=5, day=WORK_DATE + datetime.timedelta(days=1))
        self.approve(req2)
        self.work(req2)
        self.assertEqual(float(self.comp(req2).leave_days), 0.5)
        self.assertEqual(balance(), before + 1.5)
        # Comp-off is never paid in payroll.
        self.assertEqual(overtime.payroll_items(self.db, self.employee.id, models.PayrollRun(
            id=uuid.uuid4(), period_month=12, period_year=2026)), [])

    # ── guards ─────────────────────────────────────────────────────────
    def test_overlap_rejected_and_rejected_never_compensated(self):
        self.save_settings()
        req = self.request(start=datetime.time(18, 0), hours=2)
        with self.assertRaises(HTTPException) as ctx:
            self.request(start=datetime.time(19, 0), hours=1)
        self.assertEqual(ctx.exception.status_code, 409)
        work_api.update_overtime_request(req.id, schemas.OvertimeRequestUpdate(status="rejected"),
                                         db=self.db, current_user=self.manager)
        r = self.row(req)
        self.assertIsNone(r.session_status)
        overtime.advance_sessions(self.db, self.company_id, now=self.local(WORK_DATE, 23))
        self.assertIsNone(overtime.process_compensation(self.db, self.company_id, r))
        self.assertIsNone(self.comp(req))
        # The slot is free again after the rejection.
        self.request(start=datetime.time(19, 0), hours=1)

    def test_below_minimum_is_skipped_and_disabled_blocks_requests(self):
        self.save_settings(min_minutes=60)
        req = self.request(hours=0.5)
        self.approve(req)
        self.work(req)
        c = self.comp(req)
        self.assertEqual((c.status, c.counted_minutes), ("skipped", 0))
        self.assertIn("minimum", c.calculation)
        self.assertEqual(self.row(req).compensation_status, "skipped")
        self.save_settings(enabled=False)
        with self.assertRaises(HTTPException) as ctx:
            self.request(start=datetime.time(7, 0), hours=1, day=WORK_DATE + datetime.timedelta(days=3))
        self.assertEqual(ctx.exception.status_code, 409)

    def _payslip_html(self, run):
        from app import payslip_html_renderer as renderer
        slip = self.db.scalar(select(models.SalarySlip).where(
            models.SalarySlip.payroll_run_id == run.id, models.SalarySlip.employee_id == self.employee.id))
        detail = crud.get_salary_slip_detail(self.db, slip.id)
        company = self.db.get(models.Company, self.company_id)
        tpl = self.db.scalars(select(models.PayslipHtmlTemplate).where(
            models.PayslipHtmlTemplate.company_id == self.company_id)).first()
        html_body = tpl.html_body if tpl else "<table>{{earnings_rows_ytd}}</table>"
        return renderer.render_payslip_html(html_body, tpl.css_styles if tpl else "",
                                            renderer.build_payslip_placeholders(detail, company))

    def _run(self):
        return self.db.scalar(select(models.PayrollRun).where(
            models.PayrollRun.company_id == self.company_id, models.PayrollRun.period_month == WORK_DATE.month,
            models.PayrollRun.period_year == WORK_DATE.year)) or crud.create_payroll_run(
            self.db, self.company_id, WORK_DATE.month, WORK_DATE.year)

    def test_payslip_shows_overtime_only_when_payable(self):
        # Payable: an "Overtime Pay" earning with the exact amount.
        self.save_settings()
        req = self.request()
        self.approve(req)
        self.work(req)
        run = self._run()
        crud.generate_payroll_slips(self.db, self.company_id, run.id)
        html = self._payslip_html(run)
        self.assertIn("Overtime Pay", html)
        self.assertIn(f"{float(self.comp(req).amount):,.2f}", html)

        # Comp-off (a separate request): leave credited, nothing new on the payslip.
        self.save_settings(compensation_mode="comp_off")
        req2 = self.request(start=datetime.time(8, 0), hours=9, day=WORK_DATE + datetime.timedelta(days=2))
        self.approve(req2)
        self.work(req2)
        self.assertEqual(self.comp(req2).status, "credited")
        crud.generate_payroll_slips(self.db, self.company_id, run.id)
        lines = self.db.execute(
            select(models.SalarySlipLine.amount)
            .join(models.SalarySlip, models.SalarySlip.id == models.SalarySlipLine.slip_id)
            .join(models.SalaryComponent, models.SalaryComponent.id == models.SalarySlipLine.component_id)
            .where(models.SalarySlip.payroll_run_id == run.id, models.SalarySlip.employee_id == self.employee.id,
                   models.SalaryComponent.code == overtime.OT_PAY_CODE)).scalars().all()
        self.assertEqual([float(a) for a in lines], [float(self.comp(req).amount)])  # only the payable one

    def test_no_overtime_means_no_overtime_line(self):
        self.save_settings()
        run = self._run()
        crud.generate_payroll_slips(self.db, self.company_id, run.id)
        self.assertNotIn("Overtime Pay", self._payslip_html(run))

    def test_automatic_components_cannot_be_structure_lines(self):
        from app.payroll_rules import automatic_reason
        blocked = crud.create_salary_component(self.db, self.company_id, "Overtime Allowance", "OTALW",
                                               "earning", "flat", True)
        travel = crud.create_salary_component(self.db, self.company_id, "Travel expense claim", "TRX",
                                              "earning", "flat", True)
        basic = self.db.scalar(select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == self.company_id, models.SalaryComponent.code == "BASIC"))
        for comp in (blocked, travel):
            with self.assertRaises(ValueError) as ctx:
                crud.create_salary_structure(self.db, self.company_id, f"Bad {comp.code}", WORK_DATE, [
                    {"component_id": basic.id, "percent_of": "ctc", "percent": 90},
                    {"component_id": comp.id, "percent_of": "balance"},
                ])
            self.assertIn("cannot be part of a salary structure", str(ctx.exception))
        self.assertIsNone(automatic_reason("Travel Allowance", "TA"))  # real allowances stay allowed
        self.assertIsNone(automatic_reason("Leave Travel Allowance", "LTA"))
        out = schemas.SalaryComponentOut.model_validate(blocked)
        self.assertIsNotNone(out.automatic_reason)

    def test_comp_off_is_earned_only(self):
        """The comp-off leave type can only be used up to what overtime earned:
        0 before (never "unlimited"), then exactly the credited days; a longer
        request is not granted as comp-off. Other uncapped types unchanged."""
        settings = self.save_settings(compensation_mode="comp_off")
        leave_type = self.db.get(models.LeaveType, settings.comp_off_leave_type_id)
        leave_type.max_days_per_year = None  # the risky configuration
        # Start from nothing earned (rolled back).
        for a in self.db.scalars(select(models.LeaveAllocation).where(
                models.LeaveAllocation.employee_id == self.employee.id,
                models.LeaveAllocation.leave_type_id == leave_type.id)):
            self.db.delete(a)
        self.db.flush()
        self.assertTrue(crud.is_earned_only_leave_type(self.db, leave_type))
        self.assertEqual(crud.get_remaining_leave_balance(self.db, self.employee.id, leave_type), 0)

        req = self.request(start=datetime.time(9, 0), hours=13)  # 13 h -> 1 day + 5 h -> 1.5 days
        self.approve(req)
        self.work(req)
        self.assertEqual(crud.get_remaining_leave_balance(self.db, self.employee.id, leave_type), 1.5)

        # Asking for 3 days of comp-off: only 1.5 are comp-off, the rest is not.
        lop = crud.get_lop_leave_type(self.db, self.company_id)
        if lop is not None:
            primary, primary_days, legs = crud.resolve_leave_request_split(
                self.db, self.company_id, self.employee.id, leave_type.name, 3.0)
            self.assertEqual((primary.id, primary_days), (leave_type.id, 1.5))
            self.assertEqual(sum(days for _t, days in legs), 1.5)
            self.assertNotIn(leave_type.id, [t.id for t, _d in legs])

        # Any other uncapped leave type keeps its old behaviour (no limit).
        other = next((t for t in self.db.scalars(select(models.LeaveType).where(
            models.LeaveType.company_id == self.company_id)).all()
            if t.id != leave_type.id and t.max_days_per_year is None and (t.code or "").upper() != "LOP"), None)
        if other is not None and not self.db.scalar(select(models.LeaveAllocation.id).where(
                models.LeaveAllocation.employee_id == self.employee.id,
                models.LeaveAllocation.leave_type_id == other.id)):
            self.assertFalse(crud.is_earned_only_leave_type(self.db, other))
            self.assertIsNone(crud.get_remaining_leave_balance(self.db, self.employee.id, other))

    def _comp_off_type(self, max_days):
        lt = overtime.default_comp_off_leave_type(self.db, self.company_id)
        lt.max_days_per_year = max_days
        for a in self.db.scalars(select(models.LeaveAllocation).where(
                models.LeaveAllocation.employee_id == self.employee.id,
                models.LeaveAllocation.leave_type_id == lt.id)):
            self.db.delete(a)
        self.db.flush()
        return lt

    def test_payable_mode_comp_off_is_an_ordinary_leave_type(self):
        """With Payable Overtime nothing earns comp-off: Max days per year
        (e.g. 3) applies like any other leave type."""
        self.save_settings(compensation_mode="payable")
        lt = self._comp_off_type(3)
        self.assertFalse(crud.is_earned_only_leave_type(self.db, lt))
        self.assertEqual(crud.get_remaining_leave_balance(self.db, self.employee.id, lt), 3)

    def test_comp_off_mode_max_days_caps_what_overtime_earns(self):
        self.save_settings(compensation_mode="comp_off")
        lt = self._comp_off_type(3)
        self.assertEqual(crud.get_remaining_leave_balance(self.db, self.employee.id, lt), 0)  # earned only
        credited = []
        for i, hours in enumerate((13, 16, 8)):  # 1.5 + 2 (only 1.5 fits) + 1 (none fits)
            day = WORK_DATE + datetime.timedelta(days=i)
            req = self.request(start=datetime.time(6, 0), hours=hours, day=day)
            self.approve(req)
            self.work(req)
            c = self.comp(req)
            credited.append((c.status, float(c.leave_days or 0)))
        self.assertEqual(credited, [("credited", 1.5), ("credited", 1.5), ("skipped", 0.0)])
        self.assertIn("yearly limit of 3", self.comp(req).calculation)
        self.assertEqual(crud.get_remaining_leave_balance(self.db, self.employee.id, lt), 3)

    def test_report(self):
        self.save_settings()
        req = self.request()
        self.approve(req)
        self.work(req)
        rep = settings_api.overtime_report(from_date=WORK_DATE, to_date=WORK_DATE, employee_id=None, status=None,
                                           session_status=None, manager_id=None,
                                           db=self.db, current_user=self.owner)
        mine = [r for r in rep.rows if r.request_id == req.id]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0].actual_duration, mine[0].compensation_status), ("2h 30m", "pending_payroll"))
        self.assertGreaterEqual(rep.total_worked_minutes, 150)
        self.assertGreater(rep.total_amount, 0)


    # ── manual punches, reminders (no automatic clock-in / out) ─────────
    def managers(self):
        """User ids of the employee's configured reporting manager(s)."""
        return {u for u in (crud.get_user_id_for_employee(self.db, self.employee.reporting_manager_id),
                            crud.get_user_id_for_employee(self.db, self.employee.dotted_line_manager_id)
                            if self.employee.dotted_line_manager_id else None) if u}

    def notes(self, out, title):
        return self.db.scalars(select(models.Notification).where(
            models.Notification.entity_id == out.id, models.Notification.title == title)).all()

    def emails(self, out, subject_start):
        return self.db.scalars(select(models.EmailLog).where(
            models.EmailLog.related_entity_id == out.id, models.EmailLog.subject.like(subject_start + "%"))).all()

    def test_end_time_is_stored_hours_calculated_and_must_be_after_start(self):
        self.save_settings()
        out = work_api.create_overtime_request(
            schemas.OvertimeRequestCreate(employee_id=self.employee.id, work_date=WORK_DATE,
                                          start_time=datetime.time(19, 15), end_time=datetime.time(22, 45)),
            db=self.db, current_user=self.employee_user)
        self.assertEqual((out.hours, out.start_time, out.end_time), (3.5, datetime.time(19, 15), datetime.time(22, 45)))
        r = self.row(out)
        self.assertEqual(r.end_time, datetime.time(22, 45), "persisted")
        self.assertEqual((r.planned_start, r.planned_end), (self.local(WORK_DATE, 19, 15), self.local(WORK_DATE, 22, 45)))
        for bad_end in (datetime.time(19, 15), datetime.time(18, 0)):  # equal / earlier
            with self.assertRaises(ValueError):
                schemas.OvertimeRequestCreate(employee_id=self.employee.id, work_date=WORK_DATE,
                                              start_time=datetime.time(19, 15), end_time=bad_end)
        # Older rows without a stored End Time show the end of their window.
        r.end_time = None
        self.assertEqual(overtime.end_time_of(self.db, r), datetime.time(22, 45))
        listed = next(x for x in work_api.list_overtime_requests(limit=500, offset=0, db=self.db,
                                                                 current_user=self.employee_user) if x.id == out.id)
        self.assertEqual(listed.end_time, datetime.time(22, 45))

    def test_employee_edit_recalculates_and_rechecks(self):
        self.save_settings()
        out = self.create(datetime.time(18, 0), datetime.time(19, 0))
        edited = work_api.edit_overtime_request(out.id, schemas.OvertimeRequestEdit(
            work_date=WORK_DATE, start_time=datetime.time(18, 30), end_time=datetime.time(21, 0), reason="moved"),
            db=self.db, current_user=self.employee_user)
        self.assertEqual((edited.hours, edited.end_time), (2.5, datetime.time(21, 0)))
        self.assertEqual(self.row(out).planned_end, self.local(WORK_DATE, 21))
        with self.assertRaises(HTTPException):  # someone else
            work_api.edit_overtime_request(out.id, schemas.OvertimeRequestEdit(
                work_date=WORK_DATE, start_time=datetime.time(18, 30), end_time=datetime.time(20, 0)),
                db=self.db, current_user=self.manager)
        with self.assertRaises(ValueError):
            schemas.OvertimeRequestEdit(work_date=WORK_DATE, start_time=datetime.time(20, 0), end_time=datetime.time(19, 0))
        self.approve(out)
        with self.assertRaises(HTTPException) as ctx:  # not after approval
            work_api.edit_overtime_request(out.id, schemas.OvertimeRequestEdit(
                work_date=WORK_DATE, start_time=datetime.time(18, 30), end_time=datetime.time(20, 0)),
                db=self.db, current_user=self.employee_user)
        self.assertEqual(ctx.exception.status_code, 409)
        # Payroll note carries the approved window and the real punches.
        r = self.work(out)
        self.assertIn("approved 18:30–21:00", self.comp(out).calculation)

    def test_missed_clock_in_and_out_remind_employee_and_reporting_manager_once(self):
        self.save_settings()
        req = self.request()  # 18:00 -> 20:30
        self.approve(req)
        r = self.row(req)
        overtime.advance_sessions(self.db, self.company_id, now=self.local(WORK_DATE, 18, 9))
        self.assertEqual(r.session_status, "scheduled", "inside the grace period")
        overtime.advance_sessions(self.db, self.company_id, now=self.local(WORK_DATE, 18, 11))
        self.assertEqual(r.session_status, "missed_clock_in")
        self.assertIsNone(r.actual_start, "never clocked in automatically")
        to_users = {n.user_id for n in self.notes(req, "Overtime Clock In missed")}
        self.assertEqual(to_users, {self.employee_user.id} | self.managers(), "employee + reporting manager(s)")
        mail_to = {e.recipient for e in self.emails(req, "Overtime Clock In missed")}
        manager_emp = self.db.get(models.Employee, self.employee.reporting_manager_id)
        self.assertTrue({self.employee.work_email, manager_emp.work_email} <= mail_to, mail_to)
        overtime.advance_sessions(self.db, self.company_id, now=self.local(WORK_DATE, 19))  # no repeat
        self.assertEqual(len(self.notes(req, "Overtime Clock In missed")), 1 + len(self.managers()), "sent once")
        # A late clock-in is still possible and recorded as it happened.
        overtime.clock_in(self.db, self.company_id, r, now=self.local(WORK_DATE, 18, 40))
        self.assertEqual((r.session_status, r.actual_start), ("clocked_in", self.local(WORK_DATE, 18, 40)))
        overtime.advance_sessions(self.db, self.company_id, now=self.local(WORK_DATE, 20, 41))
        self.assertEqual(r.session_status, "missed_clock_out")
        self.assertIsNone(r.actual_end, "never clocked out automatically")
        self.assertEqual({n.user_id for n in self.notes(req, "Overtime Clock Out missed")},
                         {self.employee_user.id} | self.managers())
        # Late clock-out: worked 18:40 -> 21:00 = 140 min, counted up to the approved 150.
        overtime.clock_out(self.db, self.company_id, r, self.employee.id, now=self.local(WORK_DATE, 21))
        self.assertEqual((r.session_status, r.actual_minutes), ("completed", 140))
        self.assertEqual(self.comp(req).counted_minutes, 140)

    def test_clock_in_window_and_no_overlapping_sessions(self):
        self.save_settings()
        a = self.request(start=datetime.time(18, 0), hours=2)
        b = self.request(start=datetime.time(20, 30), hours=1)
        self.approve(a); self.approve(b)
        ra, rb = self.row(a), self.row(b)
        with self.assertRaises(ValueError):  # not before the approved start
            overtime.clock_in(self.db, self.company_id, ra, now=self.local(WORK_DATE, 17, 59))
        overtime.clock_in(self.db, self.company_id, ra, now=self.local(WORK_DATE, 18, 0))
        with self.assertRaises(ValueError):  # twice
            overtime.clock_in(self.db, self.company_id, ra, now=self.local(WORK_DATE, 18, 5))
        with self.assertRaisesRegex(ValueError, "still clocked in"):  # second session while the first is open
            overtime.clock_in(self.db, self.company_id, rb, now=self.local(WORK_DATE, 20, 31))
        overtime.clock_out(self.db, self.company_id, ra, self.employee.id, now=self.local(WORK_DATE, 20, 0))
        overtime.clock_in(self.db, self.company_id, rb, now=self.local(WORK_DATE, 20, 31))
        self.assertEqual(rb.session_status, "clocked_in")
        with self.assertRaises(ValueError):  # after the window
            c = self.request(start=datetime.time(6, 0), hours=1, day=WORK_DATE + datetime.timedelta(days=1))
            self.approve(c)
            overtime.clock_in(self.db, self.company_id, self.row(c),
                              now=self.local(WORK_DATE + datetime.timedelta(days=1), 7, 1))
        # Only the employee can punch (API).
        with self.assertRaises(HTTPException) as ctx:
            work_api.overtime_clock_out(b.id, db=self.db, current_user=self.manager)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_clock_out_notifies_reporting_manager_and_report_has_everything(self):
        self.save_settings()
        req = self.request()
        self.approve(req)
        r = self.row(req)
        overtime.advance_sessions(self.db, self.company_id, now=self.local(WORK_DATE, 18, 20))  # missed in
        overtime.clock_in(self.db, self.company_id, r, now=self.local(WORK_DATE, 18, 25))
        with mock.patch("app.overtime._utcnow", return_value=self.local(WORK_DATE, 20, 0)):
            work_api.overtime_clock_out(req.id, db=self.db, current_user=self.employee_user)
        self.assertEqual(r.actual_minutes, 95)
        self.assertEqual({n.user_id for n in self.notes(req, "Overtime completed")}, self.managers())
        rep = settings_api.overtime_report(from_date=WORK_DATE, to_date=WORK_DATE, employee_id=self.employee.id,
                                           status="approved", session_status="completed", manager_id=None,
                                           db=self.db, current_user=self.owner)
        row = next(x for x in rep.rows if x.request_id == req.id)
        manager_name = crud.employee_display_name(self.db, self.employee.reporting_manager_id)
        self.assertEqual((row.requested_hours, row.approved_hours, row.reporting_manager_name),
                         (2.5, 2.5, manager_name))
        self.assertEqual((row.actual_start, row.actual_end, row.actual_duration),
                         (self.local(WORK_DATE, 18, 25), self.local(WORK_DATE, 20, 0), "1h 35m"))
        self.assertIn("Missed Clock In", row.missed_punches)
        self.assertIsNotNone(row.decided_at)
        self.assertGreaterEqual(rep.missed_clock_ins, 1)
        by_manager = settings_api.overtime_report(from_date=WORK_DATE, to_date=WORK_DATE, employee_id=None,
                                                  status=None, session_status=None,
                                                  manager_id=self.employee.reporting_manager_id,
                                                  db=self.db, current_user=self.owner)
        self.assertIn(req.id, {x.request_id for x in by_manager.rows})


    # ── overtime is always outside the regular shift ──────────────────
    def create(self, start, end, day=WORK_DATE):
        return work_api.create_overtime_request(
            schemas.OvertimeRequestCreate(employee_id=self.employee.id, work_date=day, start_time=start, end_time=end),
            db=self.db, current_user=self.employee_user)

    def test_overtime_cannot_overlap_the_assigned_shift_but_pre_and_post_shift_is_fine(self):
        self.save_settings()
        day_shift = self.make_shift("QA OT day", "09:00", "18:00")
        crud.create_shift_assignment(self.db, self.employee.id, day_shift, WORK_DATE - datetime.timedelta(days=1))
        for start, end in ((datetime.time(17, 0), datetime.time(19, 0)),   # overlaps the end
                           (datetime.time(8, 0), datetime.time(9, 30)),    # overlaps the start
                           (datetime.time(10, 0), datetime.time(12, 0))):  # inside
            with self.assertRaises(HTTPException) as ctx:
                self.create(start, end)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("regular shift", ctx.exception.detail)
        pre = self.create(datetime.time(6, 0), datetime.time(9, 0))    # ends exactly at shift start
        post = self.create(datetime.time(18, 0), datetime.time(21, 0))  # starts exactly at shift end
        self.assertEqual((pre.hours, post.hours), (3.0, 3.0))
        ctx = work_api.overtime_day_context(WORK_DATE, None, db=self.db, current_user=self.employee_user)
        self.assertTrue(ctx.working_day)
        self.assertIn(self.local(WORK_DATE, 9), [w.start for w in ctx.windows])

    def test_night_shift_from_the_previous_day_is_respected(self):
        self.save_settings()
        night = self.make_shift("QA OT night", "22:00", "06:00")
        crud.create_shift_assignment(self.db, self.employee.id, night, WORK_DATE - datetime.timedelta(days=3))
        with self.assertRaises(HTTPException):   # 05:00-07:00 overlaps the night shift ending 06:00
            self.create(datetime.time(5, 0), datetime.time(7, 0))
        ok = self.create(datetime.time(6, 0), datetime.time(9, 0))
        self.assertEqual(ok.hours, 3.0)

    def test_holiday_whole_period_is_overtime_and_regular_checkin_still_works(self):
        self.save_settings()
        day_shift = self.make_shift("QA OT day", "09:00", "18:00")
        crud.create_shift_assignment(self.db, self.employee.id, day_shift, WORK_DATE - datetime.timedelta(days=1))
        holiday_day = WORK_DATE + datetime.timedelta(days=5)
        self.db.add(models.Holiday(id=uuid.uuid4(), company_id=self.company_id, holiday_date=holiday_day,
                                   name="QA Test Holiday", is_optional=False))
        self.db.flush()
        ctx = work_api.overtime_day_context(holiday_day, None, db=self.db, current_user=self.employee_user)
        self.assertEqual((ctx.working_day, ctx.day_type), (False, "holiday"))
        out = self.create(datetime.time(10, 0), datetime.time(16, 0), day=holiday_day)  # inside shift hours: fine
        self.assertEqual(out.hours, 6.0)
        self.approve(out)
        r = self.work(out)
        self.assertEqual(r.actual_minutes, 360)
        rec = self.db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.employee.id,
            models.AttendanceRecord.attendance_date == holiday_day))
        self.assertEqual((rec.status, rec.check_in, float(rec.overtime_hours)), ("overtime", None, 6.0),
                         "overtime-only record, not a regular present day")
        self.assertNotIn(rec.status, crud._PRESENT_STATUSES)
        # A regular check-in that day still works on the same record and keeps the overtime.
        checked = crud.clock_in_out(self.db, self.company_id, self.employee.id, holiday_day,
                                    self.local(holiday_day, 17), "check_in")
        self.assertEqual(checked.id, rec.id)
        self.assertIsNotNone(checked.check_in)
        self.assertEqual(float(checked.overtime_hours), 6.0)

    def test_weekly_off_has_no_shift_window(self):
        self.save_settings()
        day_shift = self.make_shift("QA OT day", "09:00", "18:00")
        crud.create_shift_assignment(self.db, self.employee.id, day_shift, WORK_DATE - datetime.timedelta(days=1))
        sunday = WORK_DATE + datetime.timedelta(days=(6 - WORK_DATE.weekday()) % 7 or 7)
        ctx = work_api.overtime_day_context(sunday, None, db=self.db, current_user=self.employee_user)
        self.assertEqual((ctx.working_day, ctx.day_type), (False, "weekly_off"))
        self.assertEqual(self.create(datetime.time(9, 0), datetime.time(13, 0), day=sunday).hours, 4.0)

    def test_approval_rechecks_the_current_shift(self):
        self.save_settings()
        out = self.create(datetime.time(18, 0), datetime.time(20, 0))
        later = self.make_shift("QA OT late", "17:00", "22:00")
        crud.create_shift_assignment(self.db, self.employee.id, later, WORK_DATE - datetime.timedelta(days=1))
        with self.assertRaises(HTTPException) as ctx:
            self.approve(out)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Can't approve", ctx.exception.detail)
        self.assertEqual(self.row(out).status, "pending")


if __name__ == "__main__":
    unittest.main()
