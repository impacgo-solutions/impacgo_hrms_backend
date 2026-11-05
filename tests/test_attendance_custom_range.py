"""Daily Attendance "Custom Range" -- GET /api/attendance/records with
from_date/to_date (inclusive): only that range is returned, an inverted or
over-366-day range is a 422, a lone from_date/to_date keeps working, and the
caller's visibility scoping still applies.

    cd backend && venv/Scripts/python -m pytest tests/test_attendance_custom_range.py -q

Real users in tenant acme (PeopleTestBase), router functions called
directly, inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete  # noqa: E402

from app import models  # noqa: E402
from app.routers import attendance as att_api  # noqa: E402
from tests.test_qa_attendance_work import DAY, _QABase  # noqa: E402


class AttendanceCustomRangeTests(_QABase):
    def setUp(self):
        super().setUp()
        # A stretch far in the past no other test touches.
        self.base = self.today - 600 * DAY
        self.days = [self.base + i * DAY for i in range(6)]
        self.db.execute(delete(models.BreakRecord).where(models.BreakRecord.employee_id == self.emp.id))
        self.db.execute(delete(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.emp.id,
            models.AttendanceRecord.attendance_date.in_(self.days)))
        for d in self.days:
            self.db.add(models.AttendanceRecord(
                id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                attendance_date=d, status="present"))
        self.db.flush()

    def list(self, user, **kw):
        kw.setdefault("cursor", None)
        kw.setdefault("limit", 500)
        kw.setdefault("employee_id", None)
        kw.setdefault("from_date", None)
        kw.setdefault("to_date", None)
        kw.setdefault("active_only", False)
        return att_api.list_attendance_records(db=self.db, current_user=user, **kw)

    @staticmethod
    def dates(page):
        return [str(i["attendance_date"])[:10] for i in page["items"]]

    def test_range_returns_only_records_inside_it(self):
        page = self.list(self.owner, employee_id=self.emp.id,
                         from_date=self.days[1], to_date=self.days[3])
        self.assertEqual(self.dates(page), [d.isoformat() for d in reversed(self.days[1:4])])
        self.assertFalse(page["has_more"])

    def test_single_day_range(self):
        page = self.list(self.owner, employee_id=self.emp.id,
                         from_date=self.days[2], to_date=self.days[2])
        self.assertEqual(self.dates(page), [self.days[2].isoformat()])

    def test_range_paginates_within_bounds(self):
        first = self.list(self.owner, employee_id=self.emp.id, limit=2,
                          from_date=self.days[0], to_date=self.days[4])
        self.assertEqual(self.dates(first), [self.days[4].isoformat(), self.days[3].isoformat()])
        self.assertTrue(first["has_more"])
        rest = self.list(self.owner, employee_id=self.emp.id, limit=500, cursor=first["next_cursor"],
                         from_date=self.days[0], to_date=self.days[4])
        self.assertEqual(self.dates(rest), [d.isoformat() for d in reversed(self.days[0:3])])

    def test_inverted_range_is_422(self):
        code = self.status(self.list, self.owner, from_date=self.days[3], to_date=self.days[1])
        self.assertEqual(code, 422)

    def test_range_capped_at_366_days(self):
        # 366 inclusive days (a whole leap year) is fine ...
        ok = self.list(self.owner, employee_id=self.emp.id,
                       from_date=self.days[5] - 365 * DAY, to_date=self.days[5])
        self.assertEqual(len(ok["items"]), 6)
        # ... one more is not.
        code = self.status(self.list, self.owner,
                           from_date=self.days[5] - 366 * DAY, to_date=self.days[5])
        self.assertEqual(code, 422)

    def test_open_ended_bounds_unchanged(self):
        # Dashboard drill-downs send only one bound -- never capped.
        older = self.list(self.owner, employee_id=self.emp.id,
                          from_date=self.days[0] - 2000 * DAY)
        self.assertTrue({d.isoformat() for d in self.days} <= set(self.dates(older)))
        upto = self.list(self.owner, employee_id=self.emp.id, to_date=self.days[1])
        got = self.dates(upto)
        self.assertIn(self.days[1].isoformat(), got)
        self.assertNotIn(self.days[2].isoformat(), got)

    def test_range_keeps_visibility_scoping(self):
        # The employee sees their own records in the range ...
        own = self.list(self.emp_user, from_date=self.days[0], to_date=self.days[5])
        self.assertEqual(len(self.dates(own)), 6)
        self.assertTrue(all(i["employee_id"] in (self.emp.id, str(self.emp.id)) for i in own["items"]))
        # ... another plain employee sees none of them.
        others = [u for u in self.plain if u.employee_id != self.emp.id]
        if not others:
            self.skipTest("only one plain employee in the owner's company")
        theirs = self.list(others[0], employee_id=self.emp.id,
                           from_date=self.days[0], to_date=self.days[5])
        self.assertEqual(theirs["items"], [])


if __name__ == "__main__":
    import unittest

    unittest.main()
