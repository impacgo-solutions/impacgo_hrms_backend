"""Unlimited Clock In / Clock Out per day (hcm_attendance_sessions):

  - any number of In -> Out sessions a day, at any time (no shift windows)
  - the day's record = first In, last Out, hours = SUM of the sessions
  - double taps / retries never make a second session or an unwanted Out
  - a night session closes on the day it started
  - a session left open too long becomes "missing clock-out" (0 h) and
    never blocks the next Clock In
  - a running break is ended by Clock Out
  - approved full-day leave still blocks Clock In (never Clock Out)
  - regularization / Edit Attendance reset the day to one matching session
  - hours summary: days worked, net hours, Avg Hours / Day

    cd backend && venv/Scripts/python -m unittest tests.test_attendance_sessions -v
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete, select  # noqa: E402

from app import attendance_rules, crud, models  # noqa: E402
from tests.test_qa_attendance_work import DAY, _QABase  # noqa: E402


class AttendanceSessionTests(_QABase):
    def setUp(self):
        super().setUp()
        self.db.execute(delete(models.ShiftAssignment).where(models.ShiftAssignment.employee_id == self.emp.id))
        # Isolate from the employee's real punches (rolled back after).
        self.db.execute(delete(models.AttendanceSession).where(
            models.AttendanceSession.employee_id == self.emp.id))
        self.d1, self.d2, self.d3 = (self.today - 80 * DAY, self.today - 79 * DAY, self.today - 78 * DAY)
        self.clear_attendance(self.d1, self.d2, self.d3)

    def punch(self, day, hh, mm, action, punch_day=None, work_mode=None):
        return crud.clock_in_out(self.db, self.company_id, self.emp.id, punch_day or day,
                                 self.local(day, hh, mm), action, work_mode=work_mode)

    def sessions(self, record):
        return crud.attendance_sessions(self.db, record)

    # ── the basic flow ──────────────────────────────────────────────────

    def test_many_sessions_a_day_sum_the_hours(self):
        r = self.punch(self.d1, 9, 0, "check_in")
        self.assertIsNone(r.check_out)
        r = self.punch(self.d1, 13, 0, "check_out")            # 4 h
        self.assertEqual(r.check_out, self.local(self.d1, 13, 0))
        r = self.punch(self.d1, 14, 0, "check_in")             # clock in AGAIN after clocking out
        self.assertIsNone(r.check_out, "clocked in again: the day is open")
        self.assertAlmostEqual(float(r.work_hours), 4.0, places=2, msg="closed sessions still count")
        r = self.punch(self.d1, 17, 30, "check_out")           # 3.5 h
        r = self.punch(self.d1, 20, 0, "check_in")
        r = self.punch(self.d1, 20, 45, "check_out")           # 0.75 h
        self.assertEqual(r.check_in, self.local(self.d1, 9, 0), "first In")
        self.assertEqual(r.check_out, self.local(self.d1, 20, 45), "last Out")
        self.assertAlmostEqual(float(r.work_hours), 8.25, places=2, msg="gaps between sessions are not work")
        self.assertEqual(len(self.sessions(r)), 3)
        out = crud.serialize_attendance_record(r)
        self.assertEqual(out["session_count"], 3)
        self.assertEqual([s["hours"] for s in out["sessions"]], [4.0, 3.5, 0.75])
        self.assertFalse(out["clocked_in"])
        self.assertEqual(out["net_work_hours"], 8.25)

    def test_no_time_windows_even_with_a_shift(self):
        sh = self.shift("09:00", "18:00")
        sh.grace_minutes = 10
        crud.create_shift_assignment(self.db, self.emp.id, sh.id, self.d1 - 5 * DAY)
        self.db.flush()
        r = self.punch(self.d1, 5, 0, "check_in")               # hours before the shift: allowed
        self.assertEqual(r.status, "early_in")                 # ...and only labelled
        r = self.punch(self.d1, 6, 0, "check_out")              # long before the shift end: allowed
        self.assertEqual(r.status, "early_in")
        r = self.punch(self.d1, 9, 30, "check_in")              # second In keeps the first verdict
        self.assertEqual(r.arrival_status, "early_in")
        r = self.punch(self.d1, 18, 30, "check_out")            # after end + grace
        self.assertEqual(r.status, "off_shift")
        r = self.punch(self.d1, 19, 0, "check_in")              # back at work again
        self.assertEqual(r.status, "early_in")
        self.punch(self.d1, 20, 0, "check_out")
        r = self.punch(self.d2, 9, 25, "check_in")              # late first In -> Late, stays Late
        self.assertEqual(r.status, "late")
        r = self.punch(self.d2, 19, 0, "check_out")
        self.assertEqual(r.status, "late")

    # ── duplicates / races ─────────────────────────────────────────────

    def test_double_clock_in_is_a_no_op(self):
        a = self.punch(self.d1, 9, 0, "check_in")
        b = self.punch(self.d1, 9, 0, "check_in")
        c = self.punch(self.d1, 9, 1, "check_in", work_mode="WFH")
        self.assertEqual(a.id, b.id)
        self.assertEqual(len(self.sessions(c)), 1)
        self.assertEqual(c.check_in, self.local(self.d1, 9, 0))
        self.assertEqual(c.work_mode, "WFH", "a repeated Clock In still applies the Work Mode")

    def test_clock_out_when_not_clocked_in_is_refused_clearly(self):
        with self.assertRaisesRegex(ValueError, "not clocked in"):
            self.punch(self.d1, 9, 0, "check_out")
        self.punch(self.d1, 9, 0, "check_in")
        self.punch(self.d1, 10, 0, "check_out")
        with self.assertRaisesRegex(ValueError, "not clocked in"):     # double Out
            self.punch(self.d1, 10, 0, "check_out")
        r = self.db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.emp.id, models.AttendanceRecord.attendance_date == self.d1))
        self.assertAlmostEqual(float(r.work_hours), 1.0, places=2)

    def test_only_one_open_session_per_employee(self):
        self.punch(self.d1, 9, 0, "check_in")
        self.punch(self.d1, 9, 5, "check_in")
        open_ = self.db.scalars(select(models.AttendanceSession).where(
            models.AttendanceSession.employee_id == self.emp.id,
            models.AttendanceSession.session_end.is_(None))).all()
        self.assertEqual(len(open_), 1)

    # ── overnight / forgotten ──────────────────────────────────────────

    def test_night_session_closes_on_the_day_it_started(self):
        self.punch(self.d1, 22, 0, "check_in")
        # 06:00 next morning: the company "today" is d2, the session is d1's.
        r = self.punch(self.d2, 6, 0, "check_out", punch_day=self.d2)
        self.assertEqual(r.attendance_date, self.d1)
        self.assertAlmostEqual(float(r.work_hours), 8.0, places=2)
        d2 = self.db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.emp.id, models.AttendanceRecord.attendance_date == self.d2))
        self.assertIsNone(d2, "no stray record on the next day")

    def test_forgotten_clock_out_never_blocks_the_next_clock_in(self):
        self.punch(self.d1, 9, 0, "check_in")                   # never clocked out
        hours = attendance_rules.MAX_OPEN_SESSION_HOURS
        later = self.d1 + datetime.timedelta(days=2)
        self.clear_attendance(later)
        r = self.punch(later, 9, 0, "check_in")                 # 48 h later: allowed
        self.assertEqual(r.attendance_date, later)
        old = self.db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.emp.id, models.AttendanceRecord.attendance_date == self.d1))
        old_sessions = self.sessions(old)
        self.assertTrue(old_sessions[0].missing_out)
        self.assertIsNone(old.check_out, "still shows Missing Check-Out for regularization")
        self.assertIsNone(old.work_hours, "a forgotten session counts 0 h, never 48 h")
        self.assertGreater(hours, 8)
        out = crud.serialize_attendance_record(old)
        self.assertTrue(out["sessions"][0]["missing_clock_out"])
        self.assertFalse(out["clocked_in"])

    def test_stale_open_session_is_not_clocked_in(self):
        rec = models.AttendanceRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                      attendance_date=self.d1, check_in=self.local(self.d1, 9), status="present")
        self.db.add(rec)
        self.db.flush()
        crud.reset_attendance_sessions(self.db, rec)            # one open session, days old
        self.assertFalse(crud.serialize_attendance_record(rec)["clocked_in"])
        with self.assertRaisesRegex(ValueError, "not clocked in"):
            crud.clock_in_out(self.db, self.company_id, self.emp.id, self.today,
                              datetime.datetime.now(datetime.timezone.utc), "check_out")

    # ── breaks / leave ─────────────────────────────────────────────────

    def test_clock_out_ends_a_running_break(self):
        r = self.punch(self.d1, 9, 0, "check_in")
        brk = models.BreakRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                 attendance_record_id=r.id, break_start=self.local(self.d1, 12, 0))
        self.db.add(brk)
        self.db.flush()
        r = self.punch(self.d1, 12, 30, "check_out")            # not refused any more
        self.assertEqual(brk.break_end, self.local(self.d1, 12, 30))
        out = crud.serialize_attendance_record(r)
        self.assertEqual(out["net_work_hours"], 3.0)            # 3.5 h - 0.5 h break

    def test_net_hours_never_negative(self):
        r = self.punch(self.d1, 9, 0, "check_in")
        self.db.add(models.BreakRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                       attendance_record_id=r.id, break_start=self.local(self.d1, 6, 0),
                                       break_end=self.local(self.d1, 8, 0)))
        self.db.flush()
        r = self.punch(self.d1, 9, 30, "check_out")
        self.db.expire(r, ["breaks"])
        self.assertEqual(crud.serialize_attendance_record(r)["net_work_hours"], 0.0)

    def test_full_day_leave_blocks_clock_in_only(self):
        r = self.punch(self.d1, 9, 0, "check_in")
        original = crud.get_blocking_leave_window
        crud.get_blocking_leave_window = lambda *a, **k: {"full_day": True, "reason": "On leave today."}
        try:
            r = self.punch(self.d1, 10, 0, "check_out")         # Out is never blocked
            self.assertIsNotNone(r.check_out)
            with self.assertRaisesRegex(ValueError, "On leave today."):
                self.punch(self.d1, 11, 0, "check_in")
        finally:
            crud.get_blocking_leave_window = original

    # ── direct corrections keep sessions in sync ───────────────────────

    def test_regularization_resets_to_one_session(self):
        self.punch(self.d1, 9, 0, "check_in")
        self.punch(self.d1, 10, 0, "check_out")
        self.punch(self.d1, 11, 0, "check_in")
        self.punch(self.d1, 12, 0, "check_out")
        reg = models.AttendanceRegularization(
            id=uuid.uuid4(), employee_id=self.emp.id, attendance_date=self.d1,
            requested_in=self.local(self.d1, 9, 0), requested_out=self.local(self.d1, 18, 0),
            reason="test", status="approved")
        crud.apply_regularization_to_attendance_record(self.db, self.company_id, reg)
        r = self.db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.emp.id, models.AttendanceRecord.attendance_date == self.d1))
        sessions = self.sessions(r)
        self.assertEqual(len(sessions), 1)
        self.assertEqual((sessions[0].session_start, sessions[0].session_end),
                         (self.local(self.d1, 9), self.local(self.d1, 18)))
        self.assertAlmostEqual(float(r.work_hours), 9.0, places=2)
        # And the employee can keep clocking in afterwards.
        r = self.punch(self.d1, 19, 0, "check_in")
        r = self.punch(self.d1, 20, 0, "check_out")
        self.assertAlmostEqual(float(r.work_hours), 10.0, places=2)

    def test_day_punched_before_sessions_keeps_its_time(self):
        rec = models.AttendanceRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                      attendance_date=self.d1, check_in=self.local(self.d1, 9),
                                      check_out=self.local(self.d1, 12), work_hours=3, status="present")
        self.db.add(rec)
        self.db.flush()                                         # legacy: no session rows
        self.punch(self.d1, 13, 0, "check_in")
        r = self.punch(self.d1, 15, 0, "check_out")
        self.assertEqual(r.check_in, self.local(self.d1, 9))
        self.assertAlmostEqual(float(r.work_hours), 5.0, places=2)
        self.assertEqual(len(self.sessions(r)), 2)

    # ── averages ───────────────────────────────────────────────────────

    def test_hours_summary_average_per_worked_day(self):
        self.punch(self.d1, 9, 0, "check_in")
        self.punch(self.d1, 13, 0, "check_out")
        self.punch(self.d1, 14, 0, "check_in")
        self.punch(self.d1, 18, 0, "check_out")                 # d1: 8 h in 2 sessions
        self.punch(self.d2, 10, 0, "check_in")
        self.punch(self.d2, 16, 0, "check_out")                 # d2: 6 h
        self.punch(self.d3, 9, 0, "check_in")                   # d3: still open -> not a worked day yet
        s = crud.attendance_hours_summary(self.db, self.company_id, [self.emp.id], self.d1, self.d3)
        self.assertEqual(s["days_worked"], 2)
        self.assertEqual(s["sessions"], 4)
        self.assertEqual(s["total_hours"], 14.0)
        self.assertEqual(s["avg_hours_per_day"], 7.0)
        empty = crud.attendance_hours_summary(self.db, self.company_id, [], self.d1, self.d3)
        self.assertEqual(empty["avg_hours_per_day"], 0.0)


if __name__ == "__main__":
    unittest.main()
