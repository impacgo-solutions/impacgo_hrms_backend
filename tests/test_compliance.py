"""Missing Attendance & Work Compliance (app/compliance.py): exceptions from
real records, regularization / leave / holiday exemptions, overtime missed
punches, work entry / timesheet, one EOD digest per manager + employee (no
duplicates), self-resolution, tenant cutoff / settings and the report.

    cd backend && venv/Scripts/python -m unittest tests.test_compliance -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant impacgo-solutions by default, COMPLIANCE_TEST_TENANT to
override). Uses a future date, so no real attendance exists for it.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete as sa_delete, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import compliance, crud, database, email_service, models, overtime  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import compliance as api  # noqa: E402

TENANT = os.environ.get("COMPLIANCE_TEST_TENANT", "impacgo-solutions")
DAY = datetime.date(2026, 11, 18)  # a Wednesday, in the future
UTC = datetime.timezone.utc


class ComplianceTests(unittest.TestCase):
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
        self.emp = None
        for e in self.db.scalars(select(models.Employee).where(models.Employee.company_id == self.company_id,
                                                                 models.Employee.is_active.is_(True))).all():
            if e.reporting_manager_id and crud.get_user_id_for_employee(self.db, e.id) \
                    and crud.get_user_id_for_employee(self.db, e.reporting_manager_id):
                self.emp = e
                break
        if self.emp is None:
            self.skipTest("no employee with a login and a reporting manager")
        self.emp_user = crud.get_user_id_for_employee(self.db, self.emp.id)
        self.managers = {u for u in (crud.get_user_id_for_employee(self.db, self.emp.reporting_manager_id),
                                     crud.get_user_id_for_employee(self.db, self.emp.dotted_line_manager_id)
                                     if self.emp.dotted_line_manager_id else None) if u}
        self.tz = crud.company_tzinfo(self.db, self.company_id)
        sh = crud.create_shift(self.db, self.company_id, f"QA CMP {uuid.uuid4().hex[:6]}",
                               datetime.time(9, 0), datetime.time(18, 0), False)
        crud.create_shift_assignment(self.db, self.emp.id, sh.id, DAY - datetime.timedelta(days=30))
        self.settings = compliance.get_settings(self.db, self.company_id)
        self.settings.check_work_entry = self.settings.check_timesheet = False  # enabled per test

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # ── helpers ────────────────────────────────────────────────────────
    def at(self, day, hh, mm=0):
        return datetime.datetime.combine(day, datetime.time(hh, mm)).replace(tzinfo=self.tz).astimezone(UTC)

    def gaps(self, day=DAY):
        return [g["type"] for g in compliance.find_gaps(self.db, self.company_id, self.emp, day, self.settings)]

    def run_day(self, day=DAY):
        with mock.patch.object(compliance, "get_settings", return_value=self.settings):
            return compliance.process_day(self.db, self.company_id, day, employee_ids=[self.emp.id])

    def notes(self, user_id, title_start, entity_ids=None):
        q = select(models.Notification).where(
            models.Notification.user_id == user_id, models.Notification.title.like(title_start + "%"))
        if entity_ids is not None:  # only this test's exceptions, never other data
            q = q.where(models.Notification.entity_id.in_(entity_ids))
        return self.db.scalars(q).all()

    def regularize(self, status, day=DAY):
        r = models.AttendanceRegularization(id=uuid.uuid4(), employee_id=self.emp.id, attendance_date=day,
                                            requested_in=self.at(day, 9), requested_out=self.at(day, 18),
                                            reason="QA", status=status)
        self.db.add(r)
        self.db.flush()
        return r

    # ── tests ──────────────────────────────────────────────────────────
    def test_missing_check_in_alerts_manager_and_employee_once(self):
        rows = self.run_day()
        self.assertEqual([r.exception_type for r in rows], ["missing_check_in"])
        row = rows[0]
        self.assertEqual(row.expected_at, self.at(DAY, 9))
        self.assertIn("QA CMP", row.expected_label)
        self.assertEqual(row.regularization_status, "none")
        self.assertIsNotNone(row.manager_notified_at)
        self.assertIsNotNone(row.employee_notified_at)
        for m in self.managers:
            mine = self.notes(m, "Team attendance exceptions", [r.id for r in rows])
            self.assertEqual(len(mine), 1)
            self.assertIn("Missing Check In", mine[0].body)
            self.assertIn(self.emp.first_name, mine[0].body)
        self.assertEqual(len(self.notes(self.emp_user, "Attendance exceptions", [r.id for r in rows])), 1)
        mails = self.db.scalars(select(models.EmailLog).where(
            models.EmailLog.subject.ilike("%attendance exceptions%"))).all()
        self.assertTrue(any(self.emp.work_email in m.recipient for m in mails))
        # Re-running the same day: nothing new, no second alert.
        self.assertEqual(self.run_day(), [])
        for m in self.managers:
            self.assertEqual(len(self.notes(m, "Team attendance exceptions", [r.id for r in rows])), 1)

    def test_regularization_exemption_and_rejected_still_flags(self):
        self.regularize("pending")
        self.assertEqual(self.gaps(), [])
        self.db.execute(text("UPDATE hcm_attendance_regularizations SET status='rejected' WHERE employee_id=:e"),
                        {"e": self.emp.id})
        self.db.expire_all()
        rows = self.run_day()
        self.assertEqual((rows[0].exception_type, rows[0].regularization_status), ("missing_check_in", "rejected"))

    def test_missing_check_out_resolves_when_recorded(self):
        crud.clock_in_out(self.db, self.company_id, self.emp.id, DAY, self.at(DAY, 9, 5), "check_in")
        rows = self.run_day()
        self.assertEqual([r.exception_type for r in rows], ["missing_check_out"])
        self.assertEqual(rows[0].actual_at, self.at(DAY, 9, 5))
        crud.clock_in_out(self.db, self.company_id, self.emp.id, DAY, self.at(DAY, 18, 30), "check_out")
        compliance.resolve_open(self.db, self.company_id)
        self.assertEqual(rows[0].status, "resolved")
        self.assertIn("Check-out recorded", rows[0].resolution)

    def test_holiday_weekly_off_and_full_day_leave_are_not_flagged(self):
        holiday = DAY + datetime.timedelta(days=1)
        self.db.add(models.Holiday(id=uuid.uuid4(), company_id=self.company_id, holiday_date=holiday,
                                   name="QA Holiday", is_optional=False))
        lt = self.db.scalar(select(models.LeaveType).where(models.LeaveType.company_id == self.company_id).limit(1))
        leave_day = DAY + datetime.timedelta(days=2)
        self.db.add(models.LeaveRequest(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                        leave_type_id=lt.id, from_date=leave_day, to_date=leave_day, days=1,
                                        status="approved"))
        self.db.flush()
        sunday = DAY + datetime.timedelta(days=(6 - DAY.weekday()))
        for d in (holiday, leave_day, sunday):
            self.assertEqual(self.gaps(d), [], d)

    def test_overtime_missed_punches(self):
        ot = models.OvertimeRequest(id=uuid.uuid4(), employee_id=self.emp.id, work_date=DAY, hours=2,
                                    start_time=datetime.time(19, 0), end_time=datetime.time(21, 0),
                                    status="approved", created_at=datetime.datetime.now(UTC))
        self.db.add(ot)
        ot.planned_start, ot.planned_end = self.at(DAY, 19), self.at(DAY, 21)
        ot.session_status = overtime.MISSED_CLOCK_IN
        crud.clock_in_out(self.db, self.company_id, self.emp.id, DAY, self.at(DAY, 9), "check_in")
        crud.clock_in_out(self.db, self.company_id, self.emp.id, DAY, self.at(DAY, 18), "check_out")
        rows = self.run_day()
        self.assertEqual([r.exception_type for r in rows], ["missed_ot_clock_in"])
        self.assertEqual((rows[0].reference_id, rows[0].expected_at), (ot.id, self.at(DAY, 19)))
        overtime.clock_in(self.db, self.company_id, ot, now=self.at(DAY, 19, 30))
        overtime.clock_out(self.db, self.company_id, ot, self.emp.id, now=self.at(DAY, 20, 45))
        compliance.resolve_open(self.db, self.company_id)
        self.assertEqual(rows[0].status, "resolved")

    def test_work_entry_and_timesheet_for_project_members(self):
        project = self.db.scalar(select(models.Project).where(models.Project.company_id == self.company_id).limit(1))
        if project is None:
            self.skipTest("no project")
        self.db.add(models.ProjectAllocation(id=uuid.uuid4(), project_id=project.id, employee_id=self.emp.id,
                                             allocation_pct=100, start_date=DAY - datetime.timedelta(days=10),
                                             is_active=True))
        self.db.flush()
        self.settings.check_work_entry = self.settings.check_timesheet = True
        self.settings.check_attendance = False
        self.assertEqual(self.gaps(), ["missing_work_entry"])
        friday = DAY + datetime.timedelta(days=4 - DAY.weekday())
        self.assertEqual(sorted(self.gaps(friday)), ["missing_timesheet", "missing_work_entry"])
        rows = self.run_day(friday)
        week_start = friday - datetime.timedelta(days=friday.weekday())
        ts = models.Timesheet(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                              week_start=week_start, status="submitted", total_hours=8, billable_hours=8,
                              submitted_at=datetime.datetime.now(UTC), is_active=True)
        self.db.add(ts)
        self.db.flush()
        self.db.add(models.WorkEntry(id=uuid.uuid4(), timesheet_id=ts.id, project_id=project.id, entry_date=friday,
                                     hours=8, status="pending"))
        self.db.flush()
        compliance.resolve_open(self.db, self.company_id)
        self.assertEqual({r.exception_type: r.status for r in rows},
                         {"missing_timesheet": "resolved", "missing_work_entry": "resolved"})

    def test_cutoff_and_run_log_prevent_duplicates(self):
        # "First run" semantics: independent of the live scheduler's real
        # run log (removed inside this always-rolled-back transaction).
        self.db.execute(sa_delete(models.ComplianceRun).where(models.ComplianceRun.company_id == self.company_id))
        self.db.flush()
        self.settings.eod_cutoff = datetime.time(20, 0)
        with mock.patch.object(compliance, "get_settings", return_value=self.settings), \
                mock.patch.object(compliance.crud, "company_today", return_value=DAY):
            self.assertEqual(compliance.due_dates(self.db, self.company_id, now=self.at(DAY, 19, 59)),
                             [DAY - datetime.timedelta(days=1)])
            self.assertIn(DAY, compliance.due_dates(self.db, self.company_id, now=self.at(DAY, 20, 0)))
        self.settings.enabled = False
        with mock.patch.object(compliance, "get_settings", return_value=self.settings):
            self.assertEqual(compliance.run_company(self.db, self.company_id, now=self.at(DAY, 23)), 0, "no new checks when off")
            self.assertFalse(self.db.scalars(select(models.ComplianceRun).where(models.ComplianceRun.company_id == self.company_id, models.ComplianceRun.run_date == DAY)).all())

    def test_report_filters(self):
        self.run_day()
        out = api.compliance_report(from_date=DAY, to_date=DAY, exception_type="missing_check_in", status="open",
                                    employee_id=self.emp.id, manager_id=self.emp.reporting_manager_id,
                                    department_id=None, db=self.db, current_user=self.owner)
        self.assertEqual(out.total, 1)
        row = out.rows[0]
        self.assertEqual((row.exception_label, row.regularization_status, row.status),
                         ("Missing Check In", "none", "open"))
        self.assertIsNotNone(row.reporting_manager_name)
        self.assertIn("manager notified", row.notification_status)
        none = api.compliance_report(from_date=DAY, to_date=DAY, exception_type="missing_timesheet", status=None,
                                     employee_id=self.emp.id, manager_id=None, department_id=None,
                                     db=self.db, current_user=self.owner)
        self.assertEqual(none.total, 0)


if __name__ == "__main__":
    unittest.main()
