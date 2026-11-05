"""Edit Attendance (PATCH /api/attendance/records/{id}, GET .../history):
reporting managers edit the attendance of employees in their reporting line
(attendance admins: anyone in the company), never their own; every edit is
kept in the record's history (who, when, why, before -> after), the
employee is notified and open screens update live.

    cd backend && venv/Scripts/python -m unittest tests.test_attendance_edit -v
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
from sqlalchemy import delete, select  # noqa: E402

from app import crud, models, schemas  # noqa: E402
from app.routers import attendance as att_api  # noqa: E402
from tests.test_qa_attendance_work import DAY, _QABase  # noqa: E402


class AttendanceEditTests(_QABase):
    def setUp(self):
        super().setUp()
        if len(self.plain) < 3:
            self.skipTest("needs 3 plain employees")
        # emp reports to mgr (a plain employee -- a reporting manager by the
        # reporting line, not by role); outsider is in nobody's line here.
        self.mgr_user = self.plain[1]
        self.mgr = self.db.get(models.Employee, self.mgr_user.employee_id)
        self.outsider = self.db.get(models.Employee, self.plain[2].employee_id)
        self.emp.reporting_manager_id = self.mgr.id
        self.emp.dotted_line_manager_id = None
        if self.outsider.reporting_manager_id == self.mgr.id:
            self.outsider.reporting_manager_id = None
        if self.outsider.dotted_line_manager_id == self.mgr.id:
            self.outsider.dotted_line_manager_id = None
        for e in (self.emp, self.outsider, self.mgr):
            self.db.execute(delete(models.ShiftAssignment).where(models.ShiftAssignment.employee_id == e.id))
        self.db.flush()
        crud.clear_rbac_memo(self.db)
        self.day = self.today - 40 * DAY
        self.rec = self.make_record(self.emp, self.day)
        self.out_rec = self.make_record(self.outsider, self.day)
        self.mgr_rec = self.make_record(self.mgr, self.day)

    def make_record(self, emp, day, hh=10):
        self.db.execute(delete(models.BreakRecord).where(models.BreakRecord.employee_id == emp.id))
        self.db.execute(delete(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == emp.id, models.AttendanceRecord.attendance_date == day))
        r = models.AttendanceRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=emp.id,
                                    attendance_date=day, check_in=self.local(day, hh), status="present",
                                    source="web")
        self.db.add(r)
        self.db.flush()
        return r

    def edit(self, rec, user, **kw):
        kw.setdefault("reason", "Biometric was down")
        return att_api.edit_attendance_record(rec.id, schemas.AttendanceRecordEdit(**kw), db=self.db,
                                              current_user=user)

    def code(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code

    # -- editing --
    def test_manager_edits_a_report_with_history_notification_and_push(self):
        with mock.patch.object(crud, "queue_attendance_change_push") as push:
            out = self.edit(self.rec, self.mgr_user, check_in=self.local(self.day, 9),
                            check_out=self.local(self.day, 18), work_mode="WFH")
        push.assert_called_with(self.db, self.company_id, self.emp.id, self.day)
        self.assertEqual((out["check_in"], out["check_out"]), (self.local(self.day, 9), self.local(self.day, 18)))
        self.assertEqual(float(out["work_hours"]), 9.0)
        self.assertEqual((out["work_mode"], out["status"], out["can_edit"]), ("WFH", "present", True))
        self.assertEqual(self.db.get(models.AttendanceRecord, self.rec.id).source, "edited")

        hist = att_api.attendance_record_history(self.rec.id, db=self.db, current_user=self.emp_user)
        self.assertEqual(len(hist), 1)
        h = hist[0]
        self.assertEqual(h.reason, "Biometric was down")
        self.assertIn(self.mgr_user.full_name or self.mgr_user.email, h.edited_by)
        self.assertEqual(set(h.changes), {"check_in", "check_out", "work_mode", "work_hours"})
        self.assertEqual(h.changes["work_mode"], [None, "WFH"])

        note = self.db.scalar(select(models.Notification).where(
            models.Notification.user_id == self.emp_user.id,
            models.Notification.entity_id == self.rec.id))
        self.assertIsNotNone(note)

    def test_scope_outsiders_and_own_records_are_refused(self):
        self.assertEqual(self.code(self.edit, self.out_rec, self.mgr_user, work_mode="WFH"), 403)
        # The report can't edit their manager, nor -- not being a reporting
        # manager -- their own record.
        self.assertEqual(self.code(self.edit, self.mgr_rec, self.emp_user, work_mode="WFH"), 403)
        self.assertEqual(self.code(self.edit, self.rec, self.emp_user, work_mode="WFH"), 403)

    def test_reporting_manager_edits_own_record_without_self_notification(self):
        out = self.edit(self.mgr_rec, self.mgr_user, check_out=self.local(self.day, 18), work_mode="WFH")
        self.assertEqual((out["work_mode"], out["can_edit"]), ("WFH", True))
        self.assertEqual(len(att_api.attendance_record_history(
            self.mgr_rec.id, db=self.db, current_user=self.mgr_user)), 1)
        self.assertIsNone(self.db.scalar(select(models.Notification).where(
            models.Notification.user_id == self.mgr_user.id,
            models.Notification.entity_id == self.mgr_rec.id)))
        # Employee Permissions View switches it off, own records included.
        self.db.add(models.EmployeePermission(company_id=self.company_id, employee_id=self.mgr.id,
                                              module="attendance", level="view"))
        self.db.flush()
        crud.clear_rbac_memo(self.db)
        self.assertEqual(self.code(self.edit, self.mgr_rec, self.mgr_user, work_mode="WFO"), 403)
        # Another company's record is simply not found.
        foreign = self.db.scalar(select(models.AttendanceRecord).where(
            models.AttendanceRecord.company_id != self.company_id).limit(1))
        if foreign is not None:
            self.assertEqual(self.code(self.edit, foreign, self.owner, work_mode="WFH"), 404)

    def test_owner_edits_anyone_and_list_marks_editable_rows(self):
        out = self.edit(self.out_rec, self.owner, work_mode="Client Site")
        self.assertEqual(out["work_mode"], "Client Site")
        page = att_api.list_attendance_records(
            cursor=None, limit=500, employee_id=None, from_date=self.day, to_date=self.day,
            active_only=False, sort_by=None, sort_dir=None, db=self.db, current_user=self.mgr_user)
        flags = {i["employee_id"]: i["can_edit"] for i in page["items"]}
        self.assertTrue(flags[self.emp.id])
        self.assertTrue(flags[self.mgr.id])  # a reporting manager's own row
        self.assertFalse(flags.get(self.outsider.id, False))

    def test_employee_permissions_decide(self):
        perm = models.EmployeePermission(company_id=self.company_id, employee_id=self.mgr.id,
                                         module="attendance", level="view")
        self.db.add(perm)
        self.db.flush()
        crud.clear_rbac_memo(self.db)
        self.assertEqual(self.code(self.edit, self.rec, self.mgr_user, work_mode="WFH"), 403)
        perm.level = "edit"
        self.db.flush()
        crud.clear_rbac_memo(self.db)
        self.assertEqual(self.edit(self.out_rec, self.mgr_user, work_mode="WFH")["work_mode"], "WFH")

    def test_date_move_validation_and_status(self):
        new_day = self.day - DAY
        self.db.execute(delete(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == self.emp.id, models.AttendanceRecord.attendance_date == new_day))
        self.db.flush()
        out = self.edit(self.rec, self.mgr_user, attendance_date=new_day)
        self.assertEqual(out["attendance_date"], new_day)
        self.assertEqual(out["check_in"], self.local(new_day, 10))     # punches move with the date
        # A clash with another record of the same employee.
        other = self.make_record(self.emp, self.day)
        self.assertEqual(self.code(self.edit, other, self.mgr_user, attendance_date=new_day), 409)
        # Future date, check-out before check-in, a time on another day.
        self.assertEqual(self.code(self.edit, other, self.mgr_user, attendance_date=self.today + DAY), 400)
        self.assertEqual(self.code(self.edit, other, self.mgr_user, check_out=self.local(self.day, 9)), 400)
        self.assertEqual(self.code(self.edit, other, self.mgr_user, check_in=self.local(self.day - 5 * DAY, 9)), 400)
        # Status from the employee's shift; or set explicitly.
        sh = self.shift("09:00", "18:00")
        sh.grace_minutes = 10
        crud.create_shift_assignment(self.db, self.emp.id, sh.id, self.day - 60 * DAY)
        self.db.flush()
        # (stored_status: the list shows an open past day as missing check-out)
        self.assertEqual(self.edit(other, self.mgr_user, check_in=self.local(self.day, 9, 30))["stored_status"], "late")
        self.assertEqual(self.edit(other, self.mgr_user, check_in=self.local(self.day, 8, 50))["stored_status"], "early_in")
        self.assertEqual(self.edit(other, self.mgr_user, status="half_day")["stored_status"], "half_day")
        self.assertEqual(self.code(self.edit, other, self.mgr_user, status="vacation"), 422)
        # A reason is required.
        with self.assertRaises(Exception):
            schemas.AttendanceRecordEdit(work_mode="WFH", reason="")


if __name__ == "__main__":
    unittest.main()
