"""Approve / Reject from approval emails (app/email_actions.py,
app/routers/email_actions.py).

    cd backend && <venv>/Scripts/python -m unittest tests.test_email_actions -v

Runs against the real dev schema of one tenant (EMAIL_TEST_TENANT, default
Infyq) inside a transaction that is ALWAYS rolled back; the background email
sender is replaced by a no-op, so nothing is sent.
"""

from __future__ import annotations

import datetime
import os
import sys
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, email_actions as ea, email_service, models  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

TENANT = os.environ.get("EMAIL_TEST_TENANT", "Infyq")


class _NoClose:
    """The test session, handed to the router in place of SessionLocal()."""

    def __init__(self, db):
        self._db = db

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._db, name)


class EmailActionTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        for key, value in {"email_actions_enabled": True, "email_action_ttl_hours": 72,
                           "public_api_base_url": "http://api.test", "email_enabled": False}.items():
            mock.patch.object(settings, key, value).start()
        mock.patch.object(email_service._executor, "submit", lambda *a, **k: None).start()
        mock.patch("app.email_actions.database.SessionLocal", lambda: _NoClose(self.db)).start()
        self.client = TestClient(app)

        # An employee whose Reporting Manager has an active login.
        self.employee = None
        for emp in self.db.scalars(select(models.Employee).where(models.Employee.reporting_manager_id.is_not(None))):
            uid = crud.get_user_id_for_employee(self.db, emp.reporting_manager_id)
            user = crud.get_user_by_id(self.db, uid) if uid else None
            if user is not None and user.status == "active" and user.employee_id != emp.id:
                self.employee, self.manager_user = emp, user
                break
        if self.employee is None:
            self.skipTest("tenant needs an employee whose reporting manager has a login")
        self.leave_type = self.db.scalars(select(models.LeaveType).where(
            models.LeaveType.company_id == self.employee.company_id)).first()
        if self.leave_type is None:
            self.skipTest("tenant has no leave type")

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def _leave(self) -> models.LeaveRequest:
        day = datetime.date.today() + datetime.timedelta(days=40)
        lr = models.LeaveRequest(
            id=uuid.uuid4(), company_id=self.employee.company_id, employee_id=self.employee.id,
            leave_type_id=self.leave_type.id, from_date=day, to_date=day, days=1, is_half_day=False,
            reason="Family function", status="pending",
        )
        self.db.add(lr)
        # Commits only this test's savepoint -- the outer transaction is
        # still rolled back in tearDown.
        self.db.commit()
        return lr

    def _token(self, lr, action, **kw):
        return ea.make_token(tenant_slug=TENANT, entity_type="leave_request", entity_id=lr.id,
                             user_id=self.manager_user.id, action=action, **kw)

    # ── token ──────────────────────────────────────────────────────────────

    def test_token_round_trip_tamper_and_expiry(self):
        lr = self._leave()
        tok = self._token(lr, "approve")
        payload = ea.read_token(tok)
        self.assertEqual((payload["i"], payload["u"], payload["a"], payload["s"]),
                         (lr.id, self.manager_user.id, "approve", TENANT))
        body, sig = tok.split(".")
        self.assertIsNone(ea.read_token(body + "." + sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")))
        self.assertIsNone(ea.read_token(self._token(lr, "reject").split(".")[0] + "." + sig))
        later = time.time() + 73 * 3600
        with mock.patch.object(ea.time, "time", lambda: later):
            self.assertIsNone(ea.read_token(tok))
        self.assertIsNone(ea.read_token("garbage"))

    # ── pages ──────────────────────────────────────────────────────────────

    def test_get_only_shows_confirmation_and_changes_nothing(self):
        lr = self._leave()
        r = self.client.get(f"/api/email-actions/{self._token(lr, 'approve')}")
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertIn("Confirm approval", r.text)
        self.assertIn("Family function", r.text)
        self.db.refresh(lr)
        self.assertEqual(lr.status, "pending", "opening the link (e.g. a mail scanner) must not decide anything")

    def test_approve_applies_through_the_app_logic_then_link_is_spent(self):
        lr = self._leave()
        tok = self._token(lr, "approve")
        r = self.client.post(f"/api/email-actions/{tok}", data={"notes": "Enjoy"})
        self.assertEqual(r.status_code, 200, r.text[:400])
        self.db.refresh(lr)
        self.assertIn(lr.status, ("approved", "l1_approved", "pending"))
        if lr.status == "approved":
            self.assertIn("Leave approved", r.text)
            self.assertEqual(lr.approver_id, self.manager_user.employee_id)
            self.assertEqual(lr.decision_notes, "Enjoy")
            # Same link again: nothing happens twice.
            again = self.client.post(f"/api/email-actions/{tok}", data={"notes": ""})
            self.assertIn("Already handled", again.text)

    def test_reject_requires_reason_and_records_it(self):
        lr = self._leave()
        tok = self._token(lr, "reject")
        r = self.client.post(f"/api/email-actions/{tok}", data={"notes": "  "})
        self.assertEqual(r.status_code, 400)
        self.db.refresh(lr)
        self.assertEqual(lr.status, "pending")
        r = self.client.post(f"/api/email-actions/{tok}", data={"notes": "Release week"})
        self.assertEqual(r.status_code, 200, r.text[:400])
        self.db.refresh(lr)
        self.assertEqual((lr.status, lr.decision_notes), ("rejected", "Release week"))
        self.assertIn("Leave rejected", r.text)

    def test_someone_else_cannot_decide_with_their_own_link(self):
        """The requester's own login (self-approval) is refused by the app rule."""
        lr = self._leave()
        own_uid = crud.get_user_id_for_employee(self.db, self.employee.id)
        if own_uid is None:
            self.skipTest("employee has no login")
        tok = ea.make_token(tenant_slug=TENANT, entity_type="leave_request", entity_id=lr.id,
                            user_id=own_uid, action="approve")
        r = self.client.post(f"/api/email-actions/{tok}", data={"notes": ""})
        self.assertNotEqual(r.status_code, 200)
        self.db.refresh(lr)
        self.assertEqual(lr.status, "pending")

    def test_disabled_feature_and_bad_link(self):
        lr = self._leave()
        tok = self._token(lr, "approve")
        with mock.patch.object(settings, "email_actions_enabled", False):
            r = self.client.post(f"/api/email-actions/{tok}", data={"notes": ""})
            self.assertEqual(r.status_code, 404)
            self.assertEqual(ea.action_buttons_html(self.db, entity_type="leave_request", entity_id=lr.id,
                                                    approver_employee_id=self.employee.reporting_manager_id), "")
        self.assertEqual(self.client.get("/api/email-actions/not-a-token").status_code, 404)
        self.db.refresh(lr)
        self.assertEqual(lr.status, "pending")

    # ── email ──────────────────────────────────────────────────────────────

    def test_leave_email_carries_personal_buttons_only_for_the_approver(self):
        lr = self._leave()
        common = dict(employee_name="Test", employee_code="E1", leave_type="Casual", start_date="x", end_date="y",
                      number_of_days="1", reason="r", status="Pending Approval", leave_request_id=lr.id,
                      company_id=self.employee.company_id)
        captured = []
        with mock.patch.object(email_service, "queue_email", lambda db, job: captured.append(job)):
            email_service.send_leave_applied_email(self.db, to="mgr@example.com",
                                                   approver_employee_id=self.employee.reporting_manager_id, **common)
            email_service.send_leave_applied_email(self.db, to="hr@example.com", **common)
        self.assertIn("http://api.test/api/email-actions/", captured[0].html_body)
        self.assertIn("Approve", captured[0].html_body)
        self.assertNotIn("/api/email-actions/", captured[1].html_body, "HR inbox copy has no personal buttons")


if __name__ == "__main__":
    unittest.main()
