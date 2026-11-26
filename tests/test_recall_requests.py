"""Recall: an employee pulls back their own pending expense report / travel
request before any approver has acted.

    cd backend && venv/Scripts/python -m unittest tests.test_recall_requests -v

Runs in a transaction that is ALWAYS rolled back; point DATABASE_URL at
impacgo_test_dev, never production.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import database, models  # noqa: E402
from app.routers import travel  # noqa: E402

TENANT = os.environ.get("TASK_TEST_TENANT", "impacgo-solutions")


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        # An employee with a login AND a reporting manager who has one too.
        pairs = self.db.execute(
            select(models.User, models.Employee)
            .join(models.Employee, models.Employee.id == models.User.employee_id)
            .where(models.Employee.reporting_manager_id.is_not(None))
        ).all()
        self.user = self.employee = None
        for user, employee in pairs:
            manager_user = self.db.scalar(
                select(models.User).where(models.User.employee_id == employee.reporting_manager_id))
            if manager_user is not None:
                self.user, self.employee, self.manager_user = user, employee, manager_user
                break
        if self.user is None:
            self.skipTest("tenant has no employee + manager pair with logins")

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def _claim(self, status: str) -> models.ExpenseClaim:
        claim = models.ExpenseClaim(
            id=uuid.uuid4(), company_id=self.employee.company_id, claim_no=f"HR-EXP-QA-{uuid.uuid4().hex[:6]}",
            employee_id=self.employee.id, claim_date=datetime.date.today(), total_amount=100, status=status,
        )
        self.db.add(claim)
        self.db.flush()
        return claim

    def test_owner_recalls_pending_report_to_draft_and_manager_is_told(self):
        claim = self._claim("pending")
        out = travel.recall_expense_report(claim.id, db=self.db, current_user=self.user)
        self.assertEqual(out.status, "draft")
        self.db.refresh(claim)
        self.assertEqual(claim.status, "draft")
        audit = self.db.scalars(select(models.AuditLog).where(
            models.AuditLog.document_id == claim.id, models.AuditLog.action == "recall")).all()
        self.assertEqual(len(audit), 1)
        notes = self.db.scalars(select(models.Notification).where(
            models.Notification.entity_id == claim.id,
            models.Notification.user_id == self.manager_user.id)).all()
        self.assertTrue(any("recalled" in (n.title or "").lower() for n in notes))

    def test_decided_report_cannot_be_recalled(self):
        for status in ("approved", "rejected", "draft", "sent_back"):
            claim = self._claim(status)
            with self.assertRaises(HTTPException) as ctx:
                travel.recall_expense_report(claim.id, db=self.db, current_user=self.user)
            self.assertEqual(ctx.exception.status_code, 409, status)

    def test_only_the_owner_can_recall(self):
        claim = self._claim("pending")
        with self.assertRaises(HTTPException) as ctx:
            travel.recall_expense_report(claim.id, db=self.db, current_user=self.manager_user)
        self.assertEqual(ctx.exception.status_code, 403)
        self.db.refresh(claim)
        self.assertEqual(claim.status, "pending")

    def test_owner_recalls_pending_travel_request(self):
        row = models.TravelRequestModel(
            id=uuid.uuid4(), employee_id=self.employee.id, from_date=datetime.date.today(),
            to_date=datetime.date.today(), status="pending",
        )
        if hasattr(row, "company_id"):
            row.company_id = self.employee.company_id
        self.db.add(row)
        self.db.flush()
        out = travel.recall_travel_request(row.id, db=self.db, current_user=self.user)
        self.assertEqual(out.status, "draft")


if __name__ == "__main__":
    unittest.main()
