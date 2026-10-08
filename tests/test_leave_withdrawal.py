"""Leave Withdrawal: Apply -> Approve -> Withdraw -> Reject (leave unchanged)
-> Withdraw -> Approve (leave withdrawn, balance restored exactly once) ->
Attendance unblocked -> Report; duplicate / self-approval / re-decision
guards, notifications and audit history.

    cd backend && venv/Scripts/python -m unittest tests.test_leave_withdrawal -v

Runs inside a transaction that is ALWAYS rolled back (tenant
impacgo-solutions by default, LEAVE_TEST_TENANT to override). Uses future
leave dates.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, email_service, models, schemas  # noqa: E402
from app.routers import leave as leave_api  # noqa: E402
from app.routers import leave_withdrawals as api  # noqa: E402

TENANT = os.environ.get("LEAVE_TEST_TENANT", "impacgo-solutions")
DAY = datetime.date(2026, 11, 18)  # Wednesday, future


class LeaveWithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        mock.patch.object(email_service._executor, "submit", lambda *a, **k: None).start()
        self.emp = self.alloc = None
        for e in self.db.scalars(select(models.Employee).where(models.Employee.is_active.is_(True))).all():
            u = crud.get_user_id_for_employee(self.db, e.id)
            m = crud.get_user_id_for_employee(self.db, e.reporting_manager_id) if e.reporting_manager_id else None
            if not (u and m):
                continue
            for a in self.db.scalars(select(models.LeaveAllocation).where(models.LeaveAllocation.employee_id == e.id)).all():
                lt = self.db.get(models.LeaveType, a.leave_type_id)
                if lt and crud.get_remaining_leave_balance(self.db, e.id, lt) >= 2 and not crud.is_earned_only_leave_type(self.db, lt):
                    self.emp, self.lt, self.user, self.manager = e, lt, self.db.get(models.User, u), self.db.get(models.User, m)
                    break
            if self.emp:
                break
        if self.emp is None:
            self.skipTest("no employee with a manager, a login and 2+ days of balance")
        self.company_id = self.emp.company_id

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def balance(self):
        return crud.get_remaining_leave_balance(self.db, self.emp.id, self.lt)

    def apply_and_approve(self, days=2):
        out = leave_api.create_leave_request(schemas.LeaveRequestCreate(
            employee_id=self.emp.id, leave_type_name=self.lt.name, from_date=DAY,
            to_date=DAY + datetime.timedelta(days=days - 1), days=days, reason="QA"),
            db=self.db, current_user=self.user)
        self.approve_leave(out.id)
        leave = self.db.get(models.LeaveRequest, out.id)
        self.assertEqual(leave.status, "approved")
        return leave

    def approve_leave(self, leave_id):
        """Every step of the tenant's leave workflow, by whoever may decide it."""
        users = [self.manager] + [u for u in self.db.scalars(select(models.User).where(
            models.User.company_id == self.company_id, models.User.status == "active")).all()
            if u.id not in (self.manager.id, self.user.id)]
        for _ in range(6):
            leave = self.db.get(models.LeaveRequest, leave_id)
            if leave.status == "approved":
                return
            decider = next((u for u in users if u.employee_id and crud.can_decide_request_configurable(
                self.db, u, self.emp.id, "leave_request", leave_id)), None)
            self.assertIsNotNone(decider, "someone can decide the next step")
            leave_api.update_leave_request(leave_id, schemas.LeaveRequestUpdate(status="approved"),
                                           db=self.db, current_user=decider)

    def withdraw(self, leave, reason="Plans changed"):
        return api.request_leave_withdrawal(leave.id, api.LeaveWithdrawalCreate(reason=reason),
                                            db=self.db, current_user=self.user)

    def decide(self, w, status, user=None):
        return api.decide_leave_withdrawal(w.id, api.LeaveWithdrawalDecision(status=status, decision_notes="QA"),
                                           db=self.db, current_user=user or self.manager)

    def test_full_flow_reject_then_approve_restores_balance_once(self):
        start = self.balance()
        leave = self.apply_and_approve(2)
        self.assertEqual(self.balance(), start - 2)
        # Withdraw -> pending; the leave is still active and still blocks attendance.
        w = self.withdraw(leave)
        self.assertEqual((w.status, w.leave_status_now), ("pending", "approved"))
        self.assertTrue(crud.get_blocking_leave_window(self.db, self.emp.id, DAY)["full_day"])
        self.assertTrue(self.db.scalars(select(models.Notification).where(
            models.Notification.user_id == self.manager.id, models.Notification.entity_id == w.id)).all(),
            "the reporting manager is notified")
        with self.assertRaises(HTTPException) as ctx:  # one pending withdrawal at a time
            self.withdraw(leave)
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(HTTPException) as ctx:  # no self-approval
            self.decide(w, "approved", user=self.user)
        self.assertEqual(ctx.exception.status_code, 403)
        # Reject -> leave unchanged.
        r = self.decide(w, "rejected")
        self.assertEqual((r.status, r.leave_status_now, r.restored_days), ("rejected", "approved", None))
        self.assertEqual(self.balance(), start - 2)
        self.assertTrue(crud.get_blocking_leave_window(self.db, self.emp.id, DAY)["full_day"])
        with self.assertRaises(HTTPException):  # decided once
            self.decide(w, "approved")
        # Withdraw again -> approve -> withdrawn, balance back exactly once.
        w2 = self.withdraw(leave, "Second try")
        a = self.decide(w2, "approved")
        self.assertEqual((a.status, a.leave_status_now, a.restored_days), ("approved", "withdrawn", 2.0))
        self.assertEqual(self.balance(), start)
        with self.assertRaises(HTTPException):
            self.decide(w2, "approved")
        self.assertEqual(self.balance(), start, "never restored twice")
        with self.assertRaises(HTTPException):  # a withdrawn leave can't be withdrawn again
            self.withdraw(leave)
        # Attendance: no longer blocked -> a normal check-in works.
        self.assertIsNone(crud.get_blocking_leave_window(self.db, self.emp.id, DAY))
        tz = crud.company_tzinfo(self.db, self.company_id)
        shift = crud._get_active_shift(self.db, self.emp.id, DAY)
        start_t = shift.start_time if shift and not shift.is_night else datetime.time(9, 0)
        rec = crud.clock_in_out(self.db, self.company_id, self.emp.id, DAY,
                                datetime.datetime.combine(DAY, start_t).replace(tzinfo=tz), "check_in")
        self.assertIsNotNone(rec.check_in)
        # Audit history + decision notification to the employee.
        audits = self.db.scalar(select(func.count()).select_from(models.AuditLog).where(
            models.AuditLog.document_id.in_([leave.id, w.id, w2.id])))
        self.assertGreaterEqual(audits, 4)
        self.assertTrue(self.db.scalars(select(models.Notification).where(
            models.Notification.user_id == self.user.id, models.Notification.entity_id == w2.id)).all())

    def test_untouched_pending_leave_is_withdrawn_at_once(self):
        start = self.balance()
        out = leave_api.create_leave_request(schemas.LeaveRequestCreate(
            employee_id=self.emp.id, leave_type_name=self.lt.name, from_date=DAY, to_date=DAY, days=1),
            db=self.db, current_user=self.user)
        leave = self.db.get(models.LeaveRequest, out.id)
        w = self.withdraw(leave)
        self.assertEqual((w.status, w.leave_status_now, w.restored_days, w.approver_id), ("approved", "withdrawn", 0.0, None))
        self.assertEqual(self.balance(), start)
        with self.assertRaises(HTTPException) as ctx:  # can't be approved any more
            leave_api.update_leave_request(out.id, schemas.LeaveRequestUpdate(status="approved"),
                                           db=self.db, current_user=self.manager)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.balance(), start, "a withdrawn leave never consumes balance")
        # the employee is told; the manager gets a notice
        self.assertTrue(self.db.scalars(select(models.Notification).where(
            models.Notification.user_id == self.user.id, models.Notification.entity_id == w.id)).all())

    def emails(self, wid, kind):
        return self.db.scalars(select(models.EmailLog).where(
            models.EmailLog.related_entity_id == wid,
            models.EmailLog.idempotency_key.like(f"LEAVE_WITHDRAWAL:{kind}:%"))).all()

    def test_leave_style_emails_once_each_and_inbox(self):
        from app.routers import approval_inbox
        # As on a server with email configured (sending itself stays mocked):
        # emails are QUEUED, so the duplicate guard applies.
        mock.patch.object(email_service, "transport", return_value="smtp").start()
        leave = self.apply_and_approve(1)
        w = self.withdraw(leave)
        mgr = self.db.get(models.Employee, self.emp.reporting_manager_id)
        req = self.emails(w.id, "requested")
        self.assertIn(mgr.work_email, {e.recipient for e in req}, "reporting manager emailed")
        self.assertTrue(all(e.email_type == "LEAVE_WITHDRAWAL" and e.subject.startswith("Leave Withdrawal Request - ") for e in req))
        # The manager sees it in the unified Approvals inbox and can decide it.
        inbox = approval_inbox.approval_inbox_sources(db=self.db, current_user=self.manager)
        rows = [r for r in inbox["sources"]["leave_withdrawals"] if r["id"] == str(w.id)]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["can_decide"])
        self.decide(w, "approved")
        dec = self.emails(w.id, "approved")
        self.assertEqual([e.recipient for e in dec], [self.emp.work_email])
        self.assertEqual(dec[0].status, "QUEUED")
        self.assertEqual(dec[0].subject, f"Leave Withdrawal Approved - {crud.employee_display_name(self.db, self.emp.id)}")
        # A repeat of the same notification never produces a second email.
        api._notify_decided(self.db, self.db.get(models.LeaveWithdrawal, w.id), self.emp, "x", self.manager.employee_id)
        self.db.flush()
        self.assertEqual(len(self.emails(w.id, "approved")), 1, "no duplicate email")
        inbox = approval_inbox.approval_inbox_sources(db=self.db, current_user=self.manager)
        row = next(r for r in inbox["sources"]["leave_withdrawals"] if r["id"] == str(w.id))
        self.assertFalse(row["can_decide"], "decided -> closed in the inbox")

    def test_only_the_employee_can_withdraw_and_report_lists_history(self):
        leave = self.apply_and_approve(1)
        with self.assertRaises(HTTPException) as ctx:
            api.request_leave_withdrawal(leave.id, api.LeaveWithdrawalCreate(reason="x"),
                                         db=self.db, current_user=self.manager)
        self.assertEqual(ctx.exception.status_code, 403)
        self.decide(self.withdraw(leave, "first"), "rejected")
        self.decide(self.withdraw(leave, "second"), "approved")
        rep = api.leave_withdrawal_report(from_date=DAY, to_date=DAY, status=None, employee_id=self.emp.id,
                                          manager_id=self.emp.reporting_manager_id, leave_type_id=self.lt.id,
                                          db=self.db, current_user=self.manager)
        mine = [r for r in rep["rows"] if r["leave_request_id"] == str(leave.id)]
        self.assertEqual(sorted(r["status"] for r in mine), ["approved", "rejected"])
        self.assertTrue(all(r["attempt_count"] == 2 for r in mine))
        approved = next(r for r in mine if r["status"] == "approved")
        self.assertEqual((approved["leave_type"], approved["restored_days"], approved["reason"]),
                         (self.lt.name, 1.0, "second"))
        self.assertIsNotNone(approved["decided_at"])
        self.assertIsNotNone(approved["approver_name"])
        only_rejected = api.leave_withdrawal_report(from_date=DAY, to_date=DAY, status="rejected",
                                                    employee_id=self.emp.id, manager_id=None, leave_type_id=None,
                                                    db=self.db, current_user=self.manager)
        self.assertTrue(all(r["status"] == "rejected" for r in only_rejected["rows"]))


if __name__ == "__main__":
    unittest.main()
