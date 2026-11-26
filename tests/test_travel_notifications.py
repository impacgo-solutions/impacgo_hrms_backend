"""Travel & Expense notifications: one Administration > Notifications event
per outcome, Email and In-App toggled independently.

    cd backend && <venv>/Scripts/python -m unittest tests.test_travel_notifications -v

The integration tests run against the real dev schema of one tenant
(TRAVEL_TEST_TENANT, default impacgo-solutions) inside a transaction that is
ALWAYS rolled back; outgoing email is captured, never sent.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, email_service, models, notification_settings as ns, schemas  # noqa: E402
from app.routers import travel  # noqa: E402

TENANT = os.environ.get("TRAVEL_TEST_TENANT", "impacgo-solutions")
TRAVEL_EVENTS = [f"travel_request_{s}" for s in ("submitted", "progress", "approved", "rejected", "sent_back")]
EXPENSE_EVENTS = [f"expense_report_{s}" for s in ("submitted", "progress", "approved", "rejected", "sent_back", "paid")]


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    """Answers the two override queries notification_settings runs."""

    def __init__(self, overrides: dict[tuple[str, str], bool]):
        self.overrides = overrides

    def execute(self, _sql, params):
        keys = params.get("keys")
        rows = [SimpleNamespace(event_key=k, channel=c, enabled=v) for (k, c), v in self.overrides.items()
                if (keys is None or k in keys) and ("channel" not in params or c == params["channel"])]
        return _FakeResult(rows)


class CatalogTests(unittest.TestCase):
    def test_travel_and_expense_category_lists_every_event_with_both_channels(self):
        entries = {e["event_key"]: e for e in ns.EVENT_CATALOG if e["category"] == "Travel & Expense"}
        self.assertEqual(sorted(entries), sorted(TRAVEL_EVENTS + EXPENSE_EVENTS))
        for key, entry in entries.items():
            self.assertTrue(entry["channels"]["email"]["available"], key)
            self.assertTrue(entry["channels"]["inapp"]["available"], key)
        # the old catch-all toggles are gone from the screen
        self.assertNotIn("travel_request", ns._CATALOG_BY_KEY)
        self.assertNotIn("expense_report", ns._CATALOG_BY_KEY)

    def test_each_outcome_routes_to_its_own_event(self):
        for et in ("travel_request", "expense_report"):
            self.assertEqual(crud._notify_event_key(et, decision=False), f"{et}_submitted")
            for status in ("approved", "rejected", "sent_back"):
                self.assertEqual(crud._notify_event_key(et, decision=True, status=status), f"{et}_{status}")
        # unrelated modules unchanged
        self.assertEqual(crud._notify_event_key("reimbursement", decision=True, status="approved"), "reimbursement_claim")
        self.assertEqual(crud._notify_event_key("leave_request", decision=True, status="approved"), "leave_decision")

    def test_a_saved_choice_for_the_old_toggle_still_applies(self):
        company = uuid.uuid4()
        db = _FakeDb({("travel_request", "email"): False})
        self.assertFalse(ns.channel_enabled(db, company, "travel_request_approved", "email"))
        self.assertTrue(ns.channel_enabled(db, company, "travel_request_approved", "inapp"))
        # the new event's own setting wins over the old one
        db = _FakeDb({("travel_request", "email"): False, ("travel_request_approved", "email"): True})
        self.assertTrue(ns.channel_enabled(db, company, "travel_request_approved", "email"))
        self.assertFalse(ns.channel_enabled(db, company, "travel_request_rejected", "email"))
        # and the Notification Settings screen shows the same values
        items = {i["event_key"]: {c["channel"]: c["enabled"] for c in i["channels"]} for i in ns.get_settings(db, company)}
        self.assertEqual(items["travel_request_approved"], {"email": True, "inapp": True})
        self.assertEqual(items["travel_request_rejected"], {"email": False, "inapp": True})


class TravelNotificationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.jobs: list[email_service.EmailJob] = []
        mock.patch.object(email_service, "queue_email", lambda db, job: self.jobs.append(job)).start()

        # An employee whose Reporting Manager has an active login, both with a login + email.
        self.employee = self.manager = None
        for emp in self.db.scalars(select(models.Employee).where(models.Employee.reporting_manager_id.is_not(None),
                                                                models.Employee.is_active.is_(True))):
            mgr = self.db.get(models.Employee, emp.reporting_manager_id)
            mgr_uid = crud.get_user_id_for_employee(self.db, emp.reporting_manager_id)
            emp_uid = crud.get_user_id_for_employee(self.db, emp.id)
            if mgr and mgr.is_active and mgr.work_email and emp.work_email and mgr_uid and emp_uid and mgr.id != emp.id:
                self.employee, self.manager = emp, mgr
                self.manager_user = crud.get_user_by_id(self.db, mgr_uid)
                self.employee_user_id = emp_uid
                break
        if self.employee is None:
            self.skipTest("tenant needs an employee + reporting manager who both have logins and emails")
        self.company_id = self.employee.company_id
        # Start from catalog defaults: this company's saved toggles must not decide the test.
        self.db.execute(text("DELETE FROM core_notification_settings WHERE company_id = :c"), {"c": self.company_id})

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # ── helpers ──
    def _travel(self) -> models.TravelRequestModel:
        day = datetime.date.today() + datetime.timedelta(days=60)
        row = models.TravelRequestModel(id=uuid.uuid4(), employee_id=self.employee.id, company_id=self.company_id,
                                        status="pending", from_date=day, to_date=day, purpose="Client visit",
                                        destination="Pune", approver_id=self.manager.id)
        self.db.add(row)
        self.db.flush()
        return row

    def _claim(self, claim_no="HR-EXP-TEST-0001", status="approved") -> models.ExpenseClaim:
        claim = models.ExpenseClaim(id=uuid.uuid4(), company_id=self.company_id, created_at=datetime.datetime.now(datetime.timezone.utc),
                                    claim_no=claim_no, employee_id=self.employee.id, claim_date=datetime.date.today(),
                                    total_amount=1500, status=status)
        self.db.add(claim)
        self.db.flush()
        return claim

    def _inapp(self, user_id, entity_id) -> list[models.Notification]:
        return list(self.db.scalars(select(models.Notification).where(
            models.Notification.user_id == user_id, models.Notification.entity_id == entity_id)))

    def _emails_to(self, address) -> list[email_service.EmailJob]:
        return [j for j in self.jobs if address.lower() in [t.lower() for t in j.to]]

    def _set(self, event_key, channel, enabled):
        ns.set_setting(self.db, self.company_id, event_key, channel, enabled, self.manager_user.id)

    # ── tests ──
    def test_submission_notifies_the_approver_and_respects_each_channel(self):
        row = self._travel()
        crud.notify_new_request(self.db, self.company_id, self.employee, "Travel Request", "travel_request", row.id)
        self.assertEqual(len(self._inapp(self.manager_user.id, row.id)), 1)
        self.assertEqual(len(self._emails_to(self.manager.work_email)), 1)
        link = self._emails_to(self.manager.work_email)[0].html_body
        self.assertIn(f"/#/travel/{row.id}?type=travel_request", link)

        self._set("travel_request_submitted", "email", False)
        crud.notify_new_request(self.db, self.company_id, self.employee, "Travel Request", "travel_request", row.id)
        self.assertEqual(len(self._inapp(self.manager_user.id, row.id)), 2)      # in-app still on
        self.assertEqual(len(self._emails_to(self.manager.work_email)), 1)       # email off

        self._set("travel_request_submitted", "inapp", False)
        crud.notify_new_request(self.db, self.company_id, self.employee, "Travel Request", "travel_request", row.id)
        self.assertEqual(len(self._inapp(self.manager_user.id, row.id)), 2)      # both off

    def test_approve_reject_and_send_back_are_independent_toggles(self):
        self._set("travel_request_rejected", "email", False)
        self._set("travel_request_sent_back", "inapp", False)
        cases = {"approved": (1, 1), "rejected": (1, 0), "sent_back": (0, 1)}
        for status, (inapp, email) in cases.items():
            self.jobs.clear()
            row = self._travel()
            crud.notify_decision(self.db, self.company_id, self.employee.id, "Travel Request", status, "note",
                                 "travel_request", row.id)
            self.assertEqual(len(self._inapp(self.employee_user_id, row.id)), inapp, status)
            self.assertEqual(len(self._emails_to(self.employee.work_email)), email, status)

    def test_expense_decisions_use_expense_events(self):
        self._set("expense_report_approved", "inapp", False)
        claim = self._claim(status="approved")
        crud.notify_decision(self.db, self.company_id, self.employee.id, "Expense Report", "approved", None,
                             "expense_report", claim.id)
        self.assertEqual(self._inapp(self.employee_user_id, claim.id), [])
        self.assertEqual(len(self._emails_to(self.employee.work_email)), 1)

    def test_step_progress_tells_the_employee(self):
        row = self._travel()
        travel._notify_step_passed(self.db, self.company_id, self.employee.id, "Travel Request", "travel_request",
                                   row.id, self.manager.id)
        notes = self._inapp(self.employee_user_id, row.id)
        self.assertEqual(len(notes), 1)
        self.assertIn("next approver", notes[0].body)
        self.assertEqual(self._emails_to(self.employee.work_email), [])          # email off by default
        self._set("travel_request_progress", "email", True)
        travel._notify_step_passed(self.db, self.company_id, self.employee.id, "Travel Request", "travel_request",
                                   row.id, self.manager.id)
        self.assertEqual(len(self._emails_to(self.employee.work_email)), 1)

    def test_marking_an_expense_report_paid_notifies_the_employee_once(self):
        claim = self._claim()
        with mock.patch.object(travel, "_load_claim", lambda db, cid, user, lock=False: claim), \
                mock.patch.object(travel, "_can_set_payment_fields", lambda *a, **k: True):
            pay = schemas.ExpensePaymentUpdate(is_paid=True, clearance_date=datetime.date.today())
            travel.update_expense_payment(claim.id, pay, db=self.db, current_user=self.manager_user)
            notes = self._inapp(self.employee_user_id, claim.id)
            self.assertEqual(len(notes), 1)
            self.assertIn("reimbursement paid", notes[0].title)
            self.assertEqual(len(self._emails_to(self.employee.work_email)), 1)
            # saving the same paid state again (e.g. editing the clearance date) does not repeat it
            travel.update_expense_payment(claim.id, pay, db=self.db, current_user=self.manager_user)
            self.assertEqual(len(self._inapp(self.employee_user_id, claim.id)), 1)

    def test_paid_toggle_switches_it_off(self):
        self._set("expense_report_paid", "inapp", False)
        self._set("expense_report_paid", "email", False)
        claim = self._claim()
        with mock.patch.object(travel, "_load_claim", lambda db, cid, user, lock=False: claim), \
                mock.patch.object(travel, "_can_set_payment_fields", lambda *a, **k: True):
            travel.update_expense_payment(claim.id, schemas.ExpensePaymentUpdate(is_paid=True), db=self.db,
                                          current_user=self.manager_user)
        self.assertEqual(self._inapp(self.employee_user_id, claim.id), [])
        self.assertEqual(self._emails_to(self.employee.work_email), [])

    def test_travel_expense_reports_are_told_apart_from_payroll_reimbursements(self):
        self.assertTrue(crud._is_travel_expense_report(self.db, self._claim("HR-EXP-TEST-0002").id))
        self.assertFalse(crud._is_travel_expense_report(self.db, self._claim("REIM-TEST-0001").id))


if __name__ == "__main__":
    unittest.main()
