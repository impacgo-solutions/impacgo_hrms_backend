"""HRMS QA -- Attendance / Shifts / Regularization / Overtime and Work /
Timesheets / Projects (H-17..H-20, M-11..M-16, M-20, M-22..M-26, L-11..L-18,
N-03).

    cd backend && venv/Scripts/python -m unittest tests.test_qa_attendance_work -v

Real users of each role in tenant acme (PeopleTestBase), router functions
called directly, inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import delete, select, text  # noqa: E402

from app import attendance_rules, crud, models, schemas, work_rules  # noqa: E402
from app.routers import attendance as att_api  # noqa: E402
from app.routers import projects as proj_api  # noqa: E402
from app.routers import work as work_api  # noqa: E402
from tests.test_people_visibility import PeopleTestBase  # noqa: E402

DAY = datetime.timedelta(days=1)
UTC = datetime.timezone.utc


class _QABase(PeopleTestBase):
    def setUp(self):
        super().setUp()
        self.today = crud.company_today(self.db, self.company_id)
        self.tz = crud.company_tzinfo(self.db, self.company_id)
        self.owner = self.users["owner"]
        # Plain employees of the OWNER's company (acme holds several).
        self.plain = []
        for u in self.db.scalars(select(models.User).where(
                models.User.company_id == self.owner.company_id, models.User.status == "active",
                models.User.employee_id.is_not(None))).all():
            role = crud.get_user_primary_role(self.db, u.id)
            if role is not None and role.name in ("Professional / IC Employee", "Associate / Intern",
                                                  "Employee (ESS)", "Sales Executive", "Support Agent"):
                emp = self.db.get(models.Employee, u.employee_id)
                if emp is not None and emp.is_active:
                    self.plain.append(u)
        if not self.plain:
            self.skipTest("no plain employee in the owner's company")
        self.emp_user = self.plain[0]
        self.emp = self.db.get(models.Employee, self.emp_user.employee_id)
        # Keep tests independent of existing data / the employee's dates.
        self.emp.date_of_joining = self.today - 400 * DAY
        self.db.flush()

    def local(self, day, hh, mm=0):
        return datetime.datetime.combine(day, datetime.time(hh, mm)).replace(tzinfo=self.tz).astimezone(UTC)

    def shift(self, start, end, night=False):
        h1, m1 = map(int, start.split(":")); h2, m2 = map(int, end.split(":"))
        return crud.create_shift(self.db, self.company_id, f"QA {uuid.uuid4().hex[:8]}",
                                 datetime.time(h1, m1), datetime.time(h2, m2), night)

    def clear_attendance(self, *days):
        self.db.execute(delete(models.BreakRecord).where(models.BreakRecord.employee_id == self.emp.id))
        self.db.execute(delete(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.emp.id, models.AttendanceRecord.attendance_date.in_(days)))
        self.db.execute(delete(models.AttendanceRegularization).where(
            models.AttendanceRegularization.employee_id == self.emp.id,
            models.AttendanceRegularization.attendance_date.in_(days)))
        self.db.flush()

    def status(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code


class AttendanceQATests(_QABase):
    # H-17
    def test_h17_checkout_after_midnight_closes_previous_day(self):
        night = self.shift("22:00", "06:00", night=True)
        crud.create_shift_assignment(self.db, self.emp.id, night.id, self.today - 30 * DAY)
        y = self.today - DAY
        self.clear_attendance(y, self.today)
        rec = models.AttendanceRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                      attendance_date=y, check_in=self.local(y, 22), status="present")
        self.db.add(rec); self.db.flush()
        out = crud.clock_in_out(self.db, self.company_id, self.emp.id, self.today, self.local(self.today, 6, 5),
                                "check_out")
        self.assertEqual(out.id, rec.id)
        self.assertEqual(out.check_out, self.local(self.today, 6, 5))
        self.assertAlmostEqual(float(out.work_hours), 8.08, places=2)
        # A session older than 20 h is never auto-closed.
        rec.check_out, rec.check_in = None, self.local(y, 1)
        self.db.flush()
        with self.assertRaises(ValueError):
            crud.clock_in_out(self.db, self.company_id, self.emp.id, self.today, self.local(self.today, 6, 5),
                              "check_out")

    # M-13 + M-14 placeholder
    def test_m13_late_arrival_survives_checkout(self):
        sh = self.shift("09:00", "18:00")
        sh.grace_minutes = 10
        d = self.today - 50 * DAY
        crud.create_shift_assignment(self.db, self.emp.id, sh.id, d - 5 * DAY)
        self.clear_attendance(d)
        self.db.execute(delete(models.LeaveRequest).where(
            models.LeaveRequest.employee_id == self.emp.id, models.LeaveRequest.from_date <= d,
            models.LeaveRequest.to_date >= d))
        # an auto-absent placeholder doesn't block the check-in (M-14)
        self.db.add(models.AttendanceRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                            attendance_date=d, status="absent", source="auto_absent"))
        self.db.flush()
        r = crud.clock_in_out(self.db, self.company_id, self.emp.id, d, self.local(d, 10), "check_in")
        self.assertEqual((r.status, r.arrival_status, r.source), ("late", "late", "web"))
        r = crud.clock_in_out(self.db, self.company_id, self.emp.id, d, self.local(d, 18, 30), "check_out")
        self.assertEqual((r.status, r.arrival_status), ("late", "late"))
        self.assertEqual(crud.serialize_attendance_record(r)["arrival_status"], "late")

    # H-18 / M-11 / M-12 / L-13
    def reg(self, day, i=None, o=None, reason="Forgot to punch"):
        return att_api.create_regularization(
            schemas.RegularizationCreate(employee_id=self.emp.id, attendance_date=day, reason=reason,
                                         requested_in=i, requested_out=o),
            db=self.db, current_user=self.emp_user)

    def test_h18_regularization_times_must_be_on_the_date(self):
        d = self.today - 3 * DAY
        self.clear_attendance(d)
        self.assertEqual(self.status(self.reg, d, self.local(d - DAY, 9), self.local(d, 18)), 400)
        self.assertEqual(self.status(self.reg, d, self.local(d, 9), self.local(d + DAY, 2)), 400)  # day shift
        self.assertEqual(self.status(self.reg, d, self.local(d, 18), self.local(d, 9)), 400)
        out = self.reg(d, self.local(d, 9), self.local(d, 18))
        self.assertEqual(out.status, "pending")

    def test_h18_night_shift_may_end_next_day(self):
        night = self.shift("22:00", "06:00", night=True)
        d = self.today - 3 * DAY
        crud.create_shift_assignment(self.db, self.emp.id, night.id, d - 10 * DAY)
        self.clear_attendance(d)
        out = self.reg(d, self.local(d, 22), self.local(d + DAY, 6))
        self.assertEqual(out.attendance_date, d)

    def test_m11_duplicate_regularization_409(self):
        d = self.today - 2 * DAY
        self.clear_attendance(d)
        self.reg(d, self.local(d, 9), self.local(d, 18))
        self.assertEqual(self.status(self.reg, d, self.local(d, 9, 30), self.local(d, 18)), 409)

    def test_m12_backdate_window_and_joining_date(self):
        self.assertEqual(self.status(self.reg, self.today - 40 * DAY, self.local(self.today - 40 * DAY, 9)), 400)
        self.emp.date_of_joining = self.today - 5 * DAY
        self.db.flush()
        d = self.today - 6 * DAY
        self.assertEqual(self.status(self.reg, d, self.local(d, 9)), 400)

    def test_l13_blank_reason_is_optional(self):
        # The reason is optional now: blank / missing is stored as "".
        for reason in ("   ", None):
            self.assertEqual(schemas.RegularizationCreate(employee_id=self.emp.id, attendance_date=self.today,
                                                          reason=reason).reason, "")
        with self.assertRaises(ValidationError):  # the date stays essential
            schemas.RegularizationCreate(employee_id=self.emp.id, reason="x")

    # H-20 / M-15 / L-11 / M-16 / L-12
    def test_h20_manager_cannot_set_up_shifts_or_holidays(self):
        mgr = self.users["manager"]
        self.assertEqual(self.status(att_api.require_attendance_admin, db=self.db, current_user=mgr), 403)
        self.assertIs(att_api.require_attendance_admin(db=self.db, current_user=self.owner), self.owner)

    def test_h20_manager_assigns_only_reports(self):
        mgr = self.users["manager"]
        sh = self.shift("09:00", "18:00")
        report = self.db.scalars(select(models.Employee).where(
            models.Employee.reporting_manager_id == mgr.employee_id, models.Employee.is_active.is_(True))).first()
        stranger = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == mgr.company_id, models.Employee.is_active.is_(True),
            models.Employee.id.notin_(crud._reporting_subtree_ids(self.db, mgr.employee_id, mgr.company_id)),
            models.Employee.id != mgr.employee_id)).first()
        if mgr.company_id != self.company_id:
            sh = crud.create_shift(self.db, mgr.company_id, f"QA {uuid.uuid4().hex[:8]}",
                                   datetime.time(9), datetime.time(18), False)
        body = lambda e: schemas.ShiftAssignmentBulkCreate(employee_ids=[e.id])  # noqa: E731
        self.assertEqual(self.status(att_api.bulk_assign_employees_to_shift, sh.id, body(stranger),
                                     db=self.db, current_user=mgr), 403)
        report.date_of_joining = self.today - 400 * DAY
        res = att_api.bulk_assign_employees_to_shift(sh.id, body(report), db=self.db, current_user=mgr)
        self.assertIn(report.id, res.assigned_employee_ids)

    def test_m15_l11_shift_validation(self):
        mk = lambda name, s, e, night=False: att_api.create_shift(  # noqa: E731
            schemas.ShiftCreate(name=name, start_time=s, end_time=e, is_night=night),
            db=self.db, current_user=self.owner)
        self.assertEqual(self.status(mk, "QA day wrap", "22:00", "06:00"), 422)
        name = f"QA Night {uuid.uuid4().hex[:6]}"
        mk(name, "22:00", "06:00", True)
        self.assertEqual(self.status(mk, f"  {name.upper()} ", "09:00", "17:00"), 409)
        with self.assertRaises(ValidationError):
            schemas.ShiftCreate(name="    ", start_time="09:00", end_time="17:00")

    def test_m16_unknown_branch_holiday(self):
        code = self.status(att_api.create_holiday, schemas.HolidayCreate(
            holiday_date=self.today + 100 * DAY, name="QA Day", branch_name="No Such Branch"),
            db=self.db, current_user=self.owner)
        self.assertEqual(code, 422)

    def test_l12_assignment_backdate_window(self):
        sh = self.shift("09:00", "18:00")
        self.assertEqual(self.status(att_api.bulk_assign_employees_to_shift, sh.id, schemas.ShiftAssignmentBulkCreate(
            employee_ids=[self.emp.id], from_date=self.today - 400 * DAY), db=self.db, current_user=self.owner), 422)
        self.emp.date_of_joining = self.today - 3 * DAY
        self.db.flush()
        self.assertEqual(self.status(att_api.bulk_assign_employees_to_shift, sh.id, schemas.ShiftAssignmentBulkCreate(
            employee_ids=[self.emp.id], from_date=self.today - 10 * DAY), db=self.db, current_user=self.owner), 422)

    # H-19
    def test_h19_overtime_needs_window_and_checks(self):
        with self.assertRaises(ValidationError):
            schemas.OvertimeRequestCreate(employee_id=self.emp.id, work_date=self.today + 5 * DAY, hours=2)
        from app import overtime
        settings = overtime.get_settings(self.db, self.company_id)
        if settings not in self.db:
            self.db.add(settings)
        settings.enabled = False
        self.db.flush()
        req = schemas.OvertimeRequestCreate(employee_id=self.emp.id, work_date=self.today + 5 * DAY,
                                            start_time=datetime.time(20), end_time=datetime.time(22))
        self.assertEqual(self.status(work_api.create_overtime_request, req, db=self.db, current_user=self.emp_user), 409)
        settings.enabled = True
        self.db.flush()
        past = schemas.OvertimeRequestCreate(employee_id=self.emp.id, work_date=self.today - 5 * DAY,
                                             start_time=datetime.time(20), end_time=datetime.time(22))
        self.assertEqual(self.status(work_api.create_overtime_request, past, db=self.db, current_user=self.emp_user), 400)

    # M-14
    def test_m14_absence_job_marks_and_clears(self):
        from app import compliance
        cs = compliance.get_settings(self.db, self.company_id)
        if cs not in self.db:
            self.db.add(cs)
        cs.enabled, cs.check_attendance = True, True
        days = [self.today - i * DAY for i in range(1, attendance_rules.ABSENCE_LOOKBACK_DAYS + 1)]
        work_day = next((d for d in days if d.weekday() < 5 and not self.db.scalar(select(models.Holiday.id).where(
            models.Holiday.company_id == self.company_id, models.Holiday.holiday_date == d))), None)
        if work_day is None or not crud.is_module_enabled(self.db, self.company_id, "attendance"):
            self.skipTest("no working day in the lookback window")
        self.clear_attendance(*days)
        self.db.execute(delete(models.LeaveRequest).where(
            models.LeaveRequest.employee_id == self.emp.id, models.LeaveRequest.from_date <= days[0],
            models.LeaveRequest.to_date >= days[-1]))
        self.db.flush()
        attendance_rules.mark_absences(self.db, self.company_id)
        get = lambda: self.db.scalar(select(models.AttendanceRecord).where(  # noqa: E731
            models.AttendanceRecord.employee_id == self.emp.id, models.AttendanceRecord.attendance_date == work_day))
        rec = get()
        self.assertIsNotNone(rec)
        self.assertEqual((rec.status, rec.source), ("absent", "auto_absent"))
        again = attendance_rules.mark_absences(self.db, self.company_id)
        self.assertEqual(self.db.scalar(select(text("count(*)")).select_from(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.emp.id,
            models.AttendanceRecord.attendance_date == work_day)), 1, "idempotent")
        self.assertGreaterEqual(again["marked"], 0)
        # Approved leave decided later clears the auto-absent row.
        lt = self.db.scalars(select(models.LeaveType).where(models.LeaveType.company_id == self.company_id)).first()
        self.db.add(models.LeaveRequest(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                        leave_type_id=lt.id, from_date=work_day, to_date=work_day, days=1,
                                        status="approved"))
        self.db.flush()
        attendance_rules.mark_absences(self.db, self.company_id)
        self.assertIsNone(get())

    # N-03
    def test_n03_default_office_hours(self):
        rows = self.db.execute(text(
            "SELECT working_hours_start, working_hours_end FROM _template.core_company_settings "
            "WHERE company_id = '11111111-1111-1111-1111-111111111111'")).all()
        self.assertEqual([(r[0], r[1]) for r in rows], [(datetime.time(9), datetime.time(17))])
        missing = self.db.scalar(text(
            "SELECT count(*) FROM core_companies c LEFT JOIN core_company_settings s ON s.company_id = c.id "
            "WHERE s.working_hours_start IS NULL"))
        self.assertEqual(missing, 0)


class WorkQATests(_QABase):
    def setUp(self):
        super().setUp()
        settings_row = crud.get_company_settings(self.db, self.company_id)
        if settings_row is not None:
            settings_row.work_entry_backdate_days = None
        self.project = crud.create_project(self.db, self.company_id, f"QA Proj {uuid.uuid4().hex[:6]}", "active")
        self.day = self.today - DAY if self.today.weekday() > 0 else self.today
        self.week = work_rules.week_start_of(self.day)
        # a clean week for the employee
        ts_ids = select(models.Timesheet.id).where(models.Timesheet.employee_id == self.emp.id,
                                                   models.Timesheet.week_start == self.week)
        self.db.execute(delete(models.WorkEntry).where(models.WorkEntry.timesheet_id.in_(ts_ids)))
        self.db.execute(delete(models.Timesheet).where(models.Timesheet.employee_id == self.emp.id,
                                                       models.Timesheet.week_start == self.week))
        self.db.flush()

    def allocate(self, pct=0):
        return crud.create_project_allocation(self.db, self.project.id, self.emp.id, pct,
                                              start_date=self.today - 30 * DAY)

    def entry(self, hours=2, start=None, end=None, task="QA task", day=None):
        return work_api.create_work_entry(schemas.WorkEntryCreate(
            employee_id=self.emp.id, entry_date=day or self.day, project_id=self.project.id, task=task,
            category="Development", hours=hours, start_time=start, end_time=end), db=self.db, current_user=self.emp_user)

    def test_m25_requires_allocation(self):
        self.assertEqual(self.status(self.entry), 403)
        self.allocate()
        self.assertEqual(self.entry().hours, 2)

    def test_l16_m20_entry_validation(self):
        self.allocate()
        t = datetime.time
        self.assertEqual(self.status(self.entry, 2, t(12), t(10)), 400)   # end before start
        self.assertEqual(self.status(self.entry, 3, t(10), t(12)), 400)   # hours > window
        self.entry(2, t(10), t(12))
        self.assertEqual(self.status(self.entry, 1, t(11), t(12)), 400)   # overlap
        self.entry(5, task="other")
        self.assertEqual(self.status(self.entry, 5, task="other"), 400)   # duplicate
        self.entry(16, task="long")
        self.assertEqual(self.status(self.entry, 2, task="too much"), 400)  # 2+5+16+2 > 24

    def submit(self, d):
        return work_api.create_timesheet(
            schemas.TimesheetCreate(employee_id=self.emp.id, week_start=d), db=self.db, current_user=self.emp_user)

    def test_l17_m22_week_rules_no_lock(self):
        self.allocate()
        self.assertEqual(self.status(self.submit, self.week), 400)  # L-17 empty week
        e = self.entry()
        ts = self.submit(self.day)  # M-22: any day -> that week's Monday
        self.assertEqual(ts.week_start, self.week)
        self.assertEqual(ts.status, "approved")  # no approval step
        # A submitted week stays fully editable: add, edit, delete, resubmit.
        late = self.entry(1, task="late add")
        self.assertEqual(late.status, "approved")
        edited = work_api.edit_work_entry(e.id, schemas.WorkEntryFieldsUpdate(hours=3),
                                          db=self.db, current_user=self.emp_user)
        self.assertEqual(edited.hours, 3)
        work_api.delete_work_entry(late.id, db=self.db, current_user=self.emp_user)
        again = self.submit(self.week)
        self.assertEqual((again.status, again.total_hours), ("approved", 3))

    def test_no_date_restrictions(self):
        self.allocate()
        settings_row = crud.get_company_settings(self.db, self.company_id)
        if settings_row is not None:
            settings_row.work_entry_backdate_days = 1  # no longer enforced
        future = self.today + 45 * DAY
        past = self.today - 400 * DAY  # long before the allocation starts
        self.assertEqual(self.entry(2, task="future", day=future).hours, 2)
        old = self.entry(2, task="past", day=past)
        self.assertEqual(old.entry_date, past)
        self.assertEqual(self.submit(future).status, "approved")
        self.assertEqual(self.submit(past).status, "approved")
        # Move an entry to another (future) date and delete one -- no lock.
        moved = work_api.edit_work_entry(old.id, schemas.WorkEntryFieldsUpdate(entry_date=future + DAY),
                                         db=self.db, current_user=self.emp_user)
        self.assertEqual(moved.entry_date, future + DAY)
        work_api.delete_work_entry(old.id, db=self.db, current_user=self.emp_user)

    def test_rejected_week_resubmits_and_override_endpoints_gone(self):
        self.allocate()
        self.entry()
        ts = self.submit(self.week)
        row = self.db.get(models.Timesheet, ts.id)
        row.status, row.decision_notes = "rejected", "legacy override"
        self.db.flush()
        again = self.submit(self.week)
        self.assertEqual((again.status, again.decision_notes), ("approved", None))
        for name in ("update_timesheet", "unlock_timesheet", "update_work_entry"):
            self.assertFalse(hasattr(work_api, name), name)
        routes = {(r.path, m) for r in work_api.router.routes for m in getattr(r, "methods", ())}
        self.assertNotIn(("/api/timesheets/{timesheet_id}", "PATCH"), routes)
        self.assertNotIn(("/api/timesheets/{timesheet_id}/unlock", "POST"), routes)
        self.assertNotIn(("/api/work-entries/{work_entry_id}", "PATCH"), routes)

    def test_projects_scoped_to_assigned_employee(self):
        other = crud.create_project(self.db, self.company_id, f"QA Other {uuid.uuid4().hex[:6]}", "active")
        self.allocate()
        if crud.project_scope_employee_id(self.db, self.emp_user) is None:
            self.skipTest("test employee's role has company-wide Projects access")
        self.assertIsNone(crud.project_scope_employee_id(self.db, self.owner))
        mine = {p.id for p in proj_api.list_projects(employee_id=None, limit=None, offset=0,
                                                       db=self.db, current_user=self.emp_user)}
        self.assertIn(self.project.id, mine)
        self.assertNotIn(other.id, mine)
        # Asking for someone else's / everyone's projects still yields only one's own.
        everyone = {p.id for p in proj_api.list_projects(employee_id=self.owner.employee_id, limit=None, offset=0,
                                                           db=self.db, current_user=self.emp_user)}
        self.assertEqual(everyone, mine)
        owner_sees = {p.id for p in proj_api.list_projects(employee_id=None, limit=None, offset=0,
                                                             db=self.db, current_user=self.owner)}
        self.assertTrue({self.project.id, other.id} <= owner_sees)
        # Per-project reads: 404 for a project that isn't assigned.
        self.assertEqual(self.status(proj_api.list_project_tasks, other.id, db=self.db, current_user=self.emp_user), 404)
        self.assertEqual(proj_api.list_project_tasks(self.project.id, db=self.db, current_user=self.emp_user), [])
        self.assertEqual(self.status(proj_api.list_project_allocations, other.id, db=self.db,
                                     current_user=self.emp_user), 404)
        # Work entry only on an assigned project.
        bad = schemas.WorkEntryCreate(employee_id=self.emp.id, entry_date=self.day, project_id=other.id,
                                      task="x", category="Development", hours=1)
        self.assertEqual(self.status(work_api.create_work_entry, bad, db=self.db, current_user=self.emp_user), 403)

    def test_archived_projects_are_hidden_but_keep_their_history(self):
        archived = crud.create_project(self.db, self.company_id, f"QA Archived {uuid.uuid4().hex[:6]}", "active")
        archived.is_active = False
        self.db.flush()
        owner_sees = {p.id for p in proj_api.list_projects(employee_id=None, limit=None, offset=0,
                                                             db=self.db, current_user=self.owner)}
        self.assertNotIn(archived.id, owner_sees)
        # Still readable directly (old work entries / timesheets point at it).
        self.assertEqual(self.db.get(models.Project, archived.id).name, archived.name)

    def test_project_allocations_endpoint(self):
        self.allocate()
        if crud.project_scope_employee_id(self.db, self.emp_user) is None:
            self.skipTest("test employee's role has company-wide Projects access")
        rows = proj_api.list_allocations(employee_id=None, db=self.db, current_user=self.emp_user)
        self.assertTrue(rows)
        self.assertEqual({r.employee_id for r in rows}, {self.emp.id})
        self.assertIn(self.project.id, {r.project_id for r in rows})
        # Someone else's allocations are never returned to an IC employee.
        self.assertEqual(proj_api.list_allocations(employee_id=self.owner.employee_id, db=self.db,
                                                   current_user=self.emp_user), [])
        # An admin sees the employee's rows too.
        admin_rows = proj_api.list_allocations(employee_id=self.emp.id, db=self.db, current_user=self.owner)
        self.assertIn(self.project.id, {r.project_id for r in admin_rows})

    def test_m23_l18_visibility(self):
        self.allocate()
        e = self.entry()
        other = next((u for u in self.plain[1:] if (v := crud.get_visible_employee_ids_for_docs(self.db, u))
                      is not None and self.emp.id not in v), None)
        if other is None:
            self.skipTest("need an unrelated colleague")
        self.assertEqual(self.status(work_api.upload_work_entry_attachment, e.id, None,
                                     db=self.db, current_user=other), 403)
        self.assertEqual(self.status(work_api.list_work_entry_attachments, e.id, db=self.db, current_user=other), 403)
        self.assertEqual(work_api.list_work_entry_attachments(e.id, db=self.db, current_user=self.emp_user), [])
        self.assertEqual(work_api.list_work_entry_attachments(e.id, db=self.db, current_user=self.owner), [])
        self.assertEqual(self.status(work_api.preview_timesheet_hours, self.emp.id, self.week,
                                     db=self.db, current_user=other), 403)
        own = work_api.preview_timesheet_hours(self.emp.id, self.day, db=self.db, current_user=self.emp_user)
        self.assertEqual(own.total_hours, 2)

    def test_allocations_have_no_capacity_cap_and_readd_is_idempotent(self):
        self.db.execute(text("UPDATE pm_resource_allocations SET is_active = false WHERE employee_id = :e"),
                        {"e": self.emp.id})
        first = self.allocate(100)
        # No 100% cap: more full-time allocations on other projects are fine.
        for n in range(3):
            p = crud.create_project(self.db, self.company_id, f"QA Cap{n} {uuid.uuid4().hex[:6]}", "active")
            crud.create_project_allocation(self.db, p.id, self.emp.id, 100)
        self.assertEqual(work_rules.allocated_pct(self.db, self.emp.id), 400)
        # Re-adding the same member returns the existing allocation (no error, no duplicate).
        again = crud.create_project_allocation(self.db, self.project.id, self.emp.id, 100)
        self.assertEqual(again.id, first.id)
        # Same through the API endpoint used by Edit Project > Team Members.
        out = proj_api.create_project_allocation(schemas.ProjectAllocationCreate(
            project_name=self.project.name, employee_id=self.emp.id), db=self.db, current_user=self.owner)
        self.assertEqual(out.id, first.id)
        # Team lead tag (0%) still works alongside 400% of allocations.
        out = proj_api.create_project(schemas.ProjectCreate(
            name=f"QA Proj3 {uuid.uuid4().hex[:6]}", client="QA", type="Internal", pm="x", team_lead_id=self.emp.id,
            start=self.today.isoformat(), end="—", budget="—"),
            db=self.db, current_user=self.owner)
        self.assertEqual(out.team_lead_id, self.emp.id)

    def test_l14_l15_project_and_sprint_validation(self):
        bad = schemas.ProjectCreate(name=f"QA bad {uuid.uuid4().hex[:6]}", client="QA", type="Internal", pm="x",
                                    team_lead_id=self.emp.id, start="31/31/2026", end="—", budget="—")
        self.assertEqual(self.status(proj_api.create_project, bad, db=self.db, current_user=self.owner), 400)
        with self.assertRaises(ValidationError):
            schemas.SprintCreate(project_name=self.project.name, name="S", start_date=self.today,
                                 end_date=self.today - DAY)
        sp = lambda n: proj_api.create_sprint(schemas.SprintCreate(  # noqa: E731
            project_name=self.project.name, name=n, start_date=self.today, end_date=self.today + 14 * DAY),
            db=self.db, current_user=self.owner)
        sp("Sprint QA")
        self.assertEqual(self.status(sp, " sprint qa "), 409)


if __name__ == "__main__":
    unittest.main()
