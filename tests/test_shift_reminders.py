"""Shift-based reminders (app/shift_reminders.py): Clock In 15 min after the
shift start, prepare-to-Clock-Out and Work Entry 15 min before the end --
from the employee's real shift and the tenant settings; none on leave,
holidays or weekly offs; never twice; popup acknowledged once for all
devices; never punches.

    cd backend && venv/Scripts/python -m unittest tests.test_shift_reminders -v

Always rolled back (tenant impacgo-solutions by default). Time is simulated
with explicit `now` values on a future working day.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import compliance, crud, database, models, shift_reminders  # noqa: E402
from app.routers import compliance as api  # noqa: E402

TENANT = os.environ.get("REMINDER_TEST_TENANT", "impacgo-solutions")
DAY = datetime.date(2026, 11, 18)  # Wednesday
UTC = datetime.timezone.utc


class ShiftReminderTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.emp = next((e for e in self.db.scalars(select(models.Employee).where(models.Employee.is_active.is_(True))).all()
                         if crud.get_user_id_for_employee(self.db, e.id)), None)
        if self.emp is None:
            self.skipTest("no employee with a login")
        self.company_id = self.emp.company_id
        self.user = self.db.get(models.User, crud.get_user_id_for_employee(self.db, self.emp.id))
        self.tz = crud.company_tzinfo(self.db, self.company_id)
        sh = crud.create_shift(self.db, self.company_id, f"QA REM {uuid.uuid4().hex[:6]}",
                               datetime.time(10, 0), datetime.time(19, 0), False)
        crud.create_shift_assignment(self.db, self.emp.id, sh.id, DAY - datetime.timedelta(days=30))
        self.settings = compliance.get_settings(self.db, self.company_id)
        self.settings.reminders_enabled = True
        self.settings.clock_in_reminder_minutes = self.settings.end_reminder_minutes = 15
        mock.patch.object(crud, "company_today", side_effect=lambda *a, **k: self._today).start()
        self._today = DAY

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def at(self, hh, mm=0, day=DAY):
        return datetime.datetime.combine(day, datetime.time(hh, mm)).replace(tzinfo=self.tz).astimezone(UTC)

    def deliver(self, now):
        with mock.patch.object(compliance, "get_settings", return_value=self.settings):
            return [r.kind for r in shift_reminders.deliver(self.db, self.company_id, self.emp, now, self.settings)]

    def test_clock_in_reminder_15_minutes_after_the_shift_start_once(self):
        self.assertEqual(self.deliver(self.at(10, 14)), [], "not before start + 15")
        self.assertEqual(self.deliver(self.at(10, 15)), ["clock_in"])
        self.assertEqual(self.deliver(self.at(10, 30)), [], "never twice")
        row = self.db.scalar(select(models.ShiftReminder).where(models.ShiftReminder.employee_id == self.emp.id,
                                                                models.ShiftReminder.reminder_date == DAY))
        self.assertIn("10:00 AM", row.message)
        note = self.db.get(models.Notification, row.notification_id)
        self.assertEqual((note.user_id, note.entity_type), (self.user.id, "shift_reminder"))
        rec = self.db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.emp.id, models.AttendanceRecord.attendance_date == DAY))
        self.assertIsNone(rec, "never clocks in automatically")

    def test_checked_in_gets_no_clock_in_reminder_but_clock_out_prep_before_end(self):
        crud.clock_in_out(self.db, self.company_id, self.emp.id, DAY, self.at(10, 2), "check_in")
        self.assertEqual(self.deliver(self.at(10, 20)), [])
        self.assertEqual(self.deliver(self.at(18, 44)), [], "not before end - 15")
        self.assertIn("clock_out", self.deliver(self.at(18, 45)))
        self.assertEqual(self.deliver(self.at(18, 50)), [])
        crud.clock_in_out(self.db, self.company_id, self.emp.id, DAY, self.at(19, 0), "check_out")

    def test_work_entry_reminder_for_project_members_without_an_entry(self):
        project = self.db.scalar(select(models.Project).where(models.Project.company_id == self.company_id).limit(1))
        if project is None:
            self.skipTest("no project")
        self.db.add(models.ProjectAllocation(id=uuid.uuid4(), project_id=project.id, employee_id=self.emp.id,
                                             allocation_pct=100, start_date=DAY - datetime.timedelta(days=5), is_active=True))
        crud.clock_in_out(self.db, self.company_id, self.emp.id, DAY, self.at(10, 0), "check_in")
        kinds = self.deliver(self.at(18, 46))
        self.assertEqual(sorted(kinds), ["clock_out", "work_entry"])
        msg = self.db.scalar(select(models.ShiftReminder.message).where(
            models.ShiftReminder.employee_id == self.emp.id, models.ShiftReminder.kind == "work_entry"))
        self.assertIn("Work Entry", msg)

    def test_no_reminders_on_leave_holiday_or_weekly_off(self):
        lt = self.db.scalar(select(models.LeaveType).where(models.LeaveType.company_id == self.company_id).limit(1))
        self.db.add(models.LeaveRequest(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                        leave_type_id=lt.id, from_date=DAY, to_date=DAY, days=1, status="approved"))
        self.db.flush()
        self.assertEqual(self.deliver(self.at(11)), [], "approved leave")
        hol = DAY + datetime.timedelta(days=1)
        self.db.add(models.Holiday(id=uuid.uuid4(), company_id=self.company_id, holiday_date=hol, name="QA Hol",
                                   is_optional=False))
        self.db.flush()
        self._today = hol
        self.assertEqual(self.deliver(self.at(11, day=hol)), [], "holiday")
        sunday = DAY + datetime.timedelta(days=(6 - DAY.weekday()))
        self._today = sunday
        self.assertEqual(self.deliver(self.at(11, day=sunday)), [], "weekly off")

    def test_tenant_settings_drive_minutes_and_switch(self):
        self.settings.clock_in_reminder_minutes = 30
        self.assertEqual(self.deliver(self.at(10, 20)), [])
        self.assertEqual(self.deliver(self.at(10, 30)), ["clock_in"])
        self.settings.reminders_enabled = False
        self.assertEqual(self.deliver(self.at(18, 50)), [])

    def test_api_popup_until_acknowledged_shared_across_sessions(self):
        with mock.patch.object(shift_reminders, "_utc_now_for_tests", create=True), \
                mock.patch("app.shift_reminders.datetime") as dt:
            dt.datetime.now.return_value = self.at(10, 20)
            dt.timedelta, dt.timezone, dt.date = datetime.timedelta, datetime.timezone, datetime.date
            dt.datetime.combine = datetime.datetime.combine
            with mock.patch.object(compliance, "get_settings", return_value=self.settings):
                first = api.my_shift_reminders(db=self.db, current_user=self.user)
                again = api.my_shift_reminders(db=self.db, current_user=self.user)  # e.g. another device / refresh
        self.assertEqual([r.kind for r in first], ["clock_in"])
        self.assertEqual([r.id for r in again], [r.id for r in first], "same popup, not a new one")
        api.acknowledge_shift_reminder(first[0].id, db=self.db, current_user=self.user)
        with mock.patch.object(compliance, "get_settings", return_value=self.settings):
            after = api.my_shift_reminders(db=self.db, current_user=self.user)
        self.assertEqual(after, [], "acknowledged once -> gone everywhere")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(models.ShiftReminder).where(
            models.ShiftReminder.employee_id == self.emp.id, models.ShiftReminder.reminder_date == DAY)), 1)


if __name__ == "__main__":
    unittest.main()
