"""Attendance module sorting -- GET /api/attendance/records sort_by/sort_dir.

    cd backend && venv/Scripts/python -m unittest tests.test_attendance_sorting -v

Real users in tenant acme (PeopleTestBase), router functions called
directly, inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app import crud, models  # noqa: E402
from app.routers import attendance as att_api  # noqa: E402
from tests.test_qa_attendance_work import DAY, _QABase  # noqa: E402


class AttendanceSortingTests(_QABase):
    def setUp(self):
        super().setUp()
        self.emps = list(self.db.scalars(
            select(models.Employee).where(
                models.Employee.company_id == self.company_id, models.Employee.is_active.is_(True),
            ).order_by(models.Employee.employee_code).limit(3)
        ).all())
        if len(self.emps) < 3:
            self.skipTest("needs 3 active employees")

    # -- helpers --
    def page(self, **kw):
        kw.setdefault("cursor", None)
        kw.setdefault("limit", 50)
        kw.setdefault("employee_id", None)
        kw.setdefault("from_date", None)
        kw.setdefault("to_date", None)
        kw.setdefault("active_only", False)
        kw.setdefault("sort_by", None)
        kw.setdefault("sort_dir", None)
        return att_api.list_attendance_records(db=self.db, current_user=self.owner, **kw)

    def all_pages(self, limit=7, **kw):
        items, cursor = [], None
        for _ in range(500):
            p = self.page(cursor=cursor, limit=limit, **kw)
            items += p["items"]
            if not p["has_more"]:
                break
            cursor = p["next_cursor"]
        return items

    def seed_day(self):
        """Three records on one past day with known values."""
        day = self.today - 350 * DAY
        ids = [e.id for e in self.emps]
        self.db.execute(delete(models.BreakRecord).where(models.BreakRecord.attendance_record_id.in_(
            select(models.AttendanceRecord.id).where(models.AttendanceRecord.attendance_date == day))))
        self.db.execute(delete(models.AttendanceRecord).where(models.AttendanceRecord.attendance_date == day))
        a, b, c = ids
        rows = {
            a: dict(check_in=self.local(day, 10), check_out=self.local(day, 18), work_hours=8.0,
                    status="present", work_mode="WFO"),
            b: dict(check_in=self.local(day, 9), check_out=self.local(day, 19, 30), work_hours=10.5,
                    status="late", work_mode="WFH", overtime_hours=1.5),
            c: dict(check_in=self.local(day, 11, 15), check_out=None, work_hours=None,
                    status="present", work_mode=None),
        }
        recs = {}
        for emp_id, v in rows.items():
            r = models.AttendanceRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=emp_id,
                                        attendance_date=day, source="web", **v)
            self.db.add(r)
            recs[emp_id] = r
        self.db.flush()
        # 45 min of breaks for A.
        self.db.add(models.BreakRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=a,
                                       attendance_record_id=recs[a].id, break_start=self.local(day, 13),
                                       break_end=self.local(day, 13, 45)))
        self.db.flush()
        return day, a, b, c

    def order(self, day, sort_by, sort_dir):
        return [i["employee_id"] for i in self.page(from_date=day, to_date=day, sort_by=sort_by,
                                                     sort_dir=sort_dir)["items"]]

    # -- tests --
    def test_seeded_rows_sort_both_ways_with_empty_values_last(self):
        day, a, b, c = self.seed_day()
        cases = {
            "check_in": ([b, a, c], [c, a, b]),       # 09:00 < 10:00 < 11:15
            "check_out": ([a, b, c], [b, a, c]),      # C has no check-out: last both ways
            "work_hours": ([a, b, c], [b, a, c]),     # 8 < 10.5, C still open: last
            "status": ([b, c, a], [a, c, b]),         # Late Check-In < Missing Check-Out < On Time
            "work_mode": ([b, a, c], [a, b, c]),      # WFH < WFO, unset last
            "net_hours": ([a, b, c], [b, a, c]),      # 7.25 < 10.5, open record last
        }
        for key, (asc, desc) in cases.items():
            with self.subTest(sort_by=key):
                self.assertEqual(self.order(day, key, "asc"), asc)
                self.assertEqual(self.order(day, key, "desc"), desc)
        # Overtime 0 / 1.5 / 0 and breaks 45 / 0 / 0 min: ties keep the
        # newest-first fallback, so only the distinct row's place is fixed.
        self.assertEqual(self.order(day, "overtime_hours", "desc")[0], b)
        self.assertEqual(self.order(day, "overtime_hours", "asc")[-1], b)
        self.assertEqual(self.order(day, "total_break", "desc")[0], a)
        self.assertEqual(self.order(day, "total_break", "asc")[-1], a)

    def test_text_columns_follow_the_employee_record(self):
        day, a, b, c = self.seed_day()
        emps = {e.id: e for e in self.emps}

        def name(i):
            e = emps[i]
            return f"{e.first_name} {e.last_name or ''}".lower()

        def dept(i):
            d = self.db.get(models.Department, emps[i].department_id) if emps[i].department_id else None
            return d.name.lower() if d else None

        def desig(i):
            d = self.db.get(models.Designation, emps[i].designation_id) if emps[i].designation_id else None
            return d.name.lower() if d else None

        for key, fn in (("employee_name", name), ("employee_code", lambda i: emps[i].employee_code.lower()),
                        ("department", dept), ("designation", desig)):
            with self.subTest(sort_by=key):
                for direction in ("asc", "desc"):
                    got = [fn(i) for i in self.order(day, key, direction)]
                    present = [v for v in got if v is not None]
                    self.assertEqual(present, sorted(present, reverse=direction == "desc"))
                    # Empty values after every filled one.
                    self.assertEqual(got[:len(present)], present)

    def test_paging_a_sorted_list_returns_every_row_once(self):
        frm, to = self.today - 14 * DAY, self.today
        default = {i["id"] for i in self.all_pages(limit=200, from_date=frm, to_date=to)}
        for key in crud.ATTENDANCE_SORT_KEYS:
            for direction in ("asc", "desc"):
                with self.subTest(sort_by=key, sort_dir=direction):
                    got = [i["id"] for i in self.all_pages(limit=40, from_date=frm, to_date=to,
                                                           sort_by=key, sort_dir=direction)]
                    self.assertEqual(len(got), len(set(got)), "a row was repeated across pages")
                    self.assertEqual(set(got), default)

    def test_date_sort_and_combined_filters(self):
        day, a, b, c = self.seed_day()
        rows = self.all_pages(limit=5, from_date=day, to_date=day + 40 * DAY, sort_by="date", sort_dir="asc")
        dates = [r["attendance_date"] for r in rows]
        self.assertEqual(dates, sorted(dates))
        self.assertTrue(all(day <= d <= day + 40 * DAY for d in dates))
        # employee_id filter + sort.
        only_b = self.page(from_date=day, to_date=day, employee_id=b, sort_by="work_hours", sort_dir="asc")
        self.assertEqual([i["employee_id"] for i in only_b["items"]], [b])

    @staticmethod
    def _default_key(i):
        """(date DESC, check-in DESC with none last, id DESC) as an ascending key."""
        ci = i["check_in"]
        return (-i["attendance_date"].toordinal(), ci is None, -ci.timestamp() if ci else 0, -uuid.UUID(str(i["id"])).int)

    def test_default_order_is_newest_day_then_latest_check_in_first(self):
        day, a, b, c = self.seed_day()
        # Seeded day: C 11:15, A 10:00, B 09:00 -- plus a row with no check-in.
        d = self.db.scalar(select(models.Employee.id).where(
            models.Employee.company_id == self.company_id, models.Employee.id.notin_([a, b, c])).limit(1))
        if d is not None:
            self.db.add(models.AttendanceRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=d,
                                                attendance_date=day, status="absent", source="auto_absent"))
            self.db.flush()
        got = [i["employee_id"] for i in self.page(from_date=day, to_date=day)["items"]]
        self.assertEqual(got[:3], [c, a, b])
        if d is not None:
            self.assertEqual(got[-1], d)

        # Across pages (keyset cursor, small pages): every row once, in order.
        frm = self.today - 14 * DAY
        rows = self.all_pages(limit=7, from_date=frm, to_date=self.today)
        ids = [str(i["id"]) for i in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([self._default_key(i) for i in rows], sorted(self._default_key(i) for i in rows))
        one_page = self.page(limit=500, from_date=frm, to_date=self.today)["items"]
        self.assertEqual(ids, [str(i["id"]) for i in one_page])

        # The cursor is a (date|check-in, id) keyset, not an offset.
        p1 = self.page(limit=2, from_date=frm, to_date=self.today)
        if p1["has_more"]:
            key, _ = crud.decode_cursor(p1["next_cursor"])
            date_part, sep, _ = key.partition("|")
            self.assertEqual(sep, "|")
            datetime.date.fromisoformat(date_part)
        # sort_by=date + desc is exactly the default path.
        self.assertEqual(self.page(limit=5, sort_by="date", sort_dir="desc")["items"],
                         self.page(limit=5)["items"])

    def test_check_in_queues_a_live_attendance_push_for_the_company(self):
        day, a, *_ = self.seed_day()
        self.db.info.pop("_pending_ws_pushes", None)
        crud.queue_attendance_change_push(self.db, self.company_id, a, day)
        pending = self.db.info.get("_pending_ws_pushes") or []
        company_users = set(self.db.scalars(select(models.User.id).where(
            models.User.company_id == self.company_id, models.User.status == "active")).all())
        self.assertEqual({uid for uid, _ in pending}, company_users)
        payload = pending[0][1]
        self.assertEqual(payload, {"event": "attendance_changed", "employee_id": str(a),
                                   "attendance_date": day.isoformat()})
        self.db.info.pop("_pending_ws_pushes", None)

    def test_legacy_date_cursor_continues_with_the_next_older_day(self):
        day, *_ = self.seed_day()
        legacy = crud.encode_cursor(day.isoformat(), uuid.uuid4())
        items = self.page(cursor=legacy, limit=20)["items"]
        self.assertTrue(all(i["attendance_date"] < day for i in items))

    def test_check_in_sort_keeps_days_together(self):
        day, a, b, c = self.seed_day()
        nxt = day + DAY
        self.db.execute(delete(models.AttendanceRecord).where(models.AttendanceRecord.attendance_date == nxt))
        # Next day: C first (07:30), A later (12:00).
        for emp_id, hh, mm in ((c, 7, 30), (a, 12, 0)):
            self.db.add(models.AttendanceRecord(
                id=uuid.uuid4(), company_id=self.company_id, employee_id=emp_id, attendance_date=nxt,
                check_in=self.local(nxt, hh, mm), check_out=self.local(nxt, 18), work_hours=8,
                status="present", source="web"))
        self.db.flush()

        def rows(direction):
            return [(i["attendance_date"], i["employee_id"]) for i in
                    self.page(from_date=day, to_date=nxt, sort_by="check_in", sort_dir=direction)["items"]]

        self.assertEqual(rows("asc"), [(nxt, c), (nxt, a), (day, b), (day, a), (day, c)])
        self.assertEqual(rows("desc"), [(nxt, a), (nxt, c), (day, c), (day, a), (day, b)])

    def test_bad_sort_or_cursor_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            self.page(sort_by="salary")
        self.assertEqual(ctx.exception.status_code, 422)
        date_cursor = crud.encode_cursor(self.today.isoformat(), uuid.uuid4())
        with self.assertRaises(HTTPException) as ctx:
            self.page(sort_by="employee_name", cursor=date_cursor)
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
