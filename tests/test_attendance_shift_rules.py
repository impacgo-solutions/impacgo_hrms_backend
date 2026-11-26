"""Dynamic attendance rules: an employee WITHOUT an assigned shift has
unrestricted attendance (Clock In / Out at any time, status 'present', no
company working-hours window); with a shift, the shift only LABELS the day
from its first Clock In (Early / On Time / Late, Off Shift after the end) --
it never refuses a punch. The shift is read on every punch.

    cd backend && venv/Scripts/python -m unittest tests.test_attendance_shift_rules -v
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete  # noqa: E402

from app import crud, models, overtime  # noqa: E402
from tests.test_qa_attendance_work import DAY, _QABase  # noqa: E402


class AttendanceShiftRulesTests(_QABase):
    def setUp(self):
        super().setUp()
        # Start with no shift at all, and company working hours set -- which
        # must no longer gate anyone.
        self.db.execute(delete(models.ShiftAssignment).where(models.ShiftAssignment.employee_id == self.emp.id))
        settings_row = crud.get_company_settings(self.db, self.company_id)
        if settings_row is not None:
            settings_row.working_hours_start = datetime.time(9)
            settings_row.working_hours_end = datetime.time(17)
        self.db.flush()
        self.d1, self.d2, self.d3 = (self.today - 70 * DAY, self.today - 69 * DAY, self.today - 68 * DAY)
        self.clear_attendance(self.d1, self.d2, self.d3)

    def punch(self, day, hh, mm, action):
        return crud.clock_in_out(self.db, self.company_id, self.emp.id, day, self.local(day, hh, mm), action)

    def test_no_shift_is_unrestricted(self):
        self.assertIsNone(crud._get_active_shift(self.db, self.emp.id, self.d1))
        r = self.punch(self.d1, 3, 0, "check_in")          # 03:00 -- hours before any office hours
        self.assertEqual((r.status, r.arrival_status), ("present", "present"))
        r = self.punch(self.d1, 4, 30, "check_out")        # 04:30 -- long before office end
        self.assertEqual(r.status, "present")
        self.assertAlmostEqual(float(r.work_hours), 1.5, places=2)
        out = crud.serialize_attendance_record(r)
        self.assertEqual(out["status"], "present")
        self.assertEqual(out["net_work_hours"], 1.5)
        # Late evening too: still on time, no off-shift.
        r = self.punch(self.d2, 21, 0, "check_in")
        r = self.punch(self.d2, 23, 30, "check_out")
        self.assertEqual(r.status, "present")

    def test_assigned_shift_labels_the_day_but_never_refuses(self):
        sh = self.shift("09:00", "18:00")
        sh.grace_minutes = 10
        crud.create_shift_assignment(self.db, self.emp.id, sh.id, self.d1 - 5 * DAY)
        self.db.flush()
        r = self.punch(self.d1, 8, 0, "check_in")           # long before the start: allowed
        self.assertEqual(r.status, "early_in")
        r = self.punch(self.d1, 17, 0, "check_out")         # before the end: allowed
        self.assertEqual(r.status, "early_in")
        self.punch(self.d1, 17, 30, "check_in")             # and again
        r = self.punch(self.d1, 18, 15, "check_out")        # after end + grace
        self.assertEqual(r.status, "off_shift")
        r = self.punch(self.d2, 9, 25, "check_in")          # after start + grace
        self.assertEqual(r.status, "late")

    def test_rules_switch_when_a_shift_is_assigned_or_removed(self):
        r = self.punch(self.d1, 3, 0, "check_in")           # no shift: allowed, On Time
        self.assertEqual(r.status, "present")
        self.punch(self.d1, 4, 0, "check_out")
        sh = self.shift("09:00", "18:00")
        assignment = crud.create_shift_assignment(self.db, self.emp.id, sh.id, self.d2)
        self.db.flush()
        r = self.punch(self.d2, 3, 0, "check_in")           # shift on d2: allowed, labelled Early
        self.assertEqual(r.status, "early_in")
        self.punch(self.d2, 4, 0, "check_out")
        self.db.delete(assignment)
        self.clear_attendance(self.d2)
        self.db.flush()
        r = self.punch(self.d2, 3, 0, "check_in")           # removed: On Time again
        self.assertEqual(r.status, "present")

    def test_no_shift_means_no_regular_hours_for_overtime_and_reminders(self):
        self.assertIsNone(overtime._shift_window(self.db, self.company_id, self.emp.id, self.d3))
        sh = self.shift("09:00", "18:00")
        crud.create_shift_assignment(self.db, self.emp.id, sh.id, self.d3)
        self.db.flush()
        window = overtime._shift_window(self.db, self.company_id, self.emp.id, self.d3)
        self.assertIsNotNone(window)
        self.assertEqual(window[2], sh.name)


if __name__ == "__main__":
    unittest.main()
