"""Sequential / Chain vs Parallel approval for leave (approval_engine +
crud.notify_new_request / decide_configurable_request + routers/leave.py +
GET /api/config/approval-history).

    cd backend && <venv>/Scripts/python -m unittest tests.test_sequential_approval -v

Real dev schema of APPROVAL_TEST_TENANT (default impacgo-solutions), inside a
transaction that is ALWAYS rolled back. No email leaves the machine.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import approval_engine, crud, database, email_service, models  # noqa: E402
from app.database import get_db  # noqa: E402
from app.deps import get_current_user  # noqa: E402
from app.main import app  # noqa: E402

TENANT = os.environ.get("APPROVAL_TEST_TENANT", "impacgo-solutions")


class _Base(unittest.TestCase):
    """E1 with Reporting Manager R1 and Second Reporting Manager R2, all
    with active logins (R2 is assigned inside the rolled-back transaction
    if the tenant has no such employee)."""

    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        mock.patch.object(email_service._executor, "submit", lambda *a, **k: None).start()
        self.notified: list[uuid.UUID] = []
        real_notify = crud.create_notification

        def record(db, company_id, user_id, *a, **k):
            self.notified.append(user_id)
            return real_notify(db, company_id, user_id, *a, **k)

        mock.patch.object(crud, "create_notification", record).start()

        users = {u.employee_id: u for u in self.db.scalars(select(models.User).where(
            models.User.employee_id.is_not(None), models.User.status == "active"))}
        e1 = r1 = r2 = None
        for emp in self.db.scalars(select(models.Employee).where(models.Employee.reporting_manager_id.is_not(None))):
            if emp.id in users and emp.reporting_manager_id in users and emp.reporting_manager_id != emp.id:
                e1, r1 = emp, users[emp.reporting_manager_id]
                r2 = next((u for eid, u in users.items() if eid not in (emp.id, emp.reporting_manager_id)), None)
                if r2 is not None:
                    break
        if e1 is None or r2 is None:
            self.skipTest("tenant needs an employee + 2 other users with logins")
        e1.dotted_line_manager_id = r2.employee_id
        self.db.commit()
        self.e1, self.r1, self.r2 = e1, r1, r2
        self.e1_user = users[e1.id]
        self.leave_type = self.db.scalars(select(models.LeaveType).where(
            models.LeaveType.company_id == e1.company_id)).first()
        if self.leave_type is None:
            self.skipTest("tenant has no leave type")
        self.user = None
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def configure(self, *steps: tuple[int, str]):
        crud.upsert_approval_workflow(self.db, self.e1.company_id, "leave_request", "Leave Request", [
            {"step_order": o, "approver_type": t, "role_id": None, "user_id": None,
             "min_amount": None, "max_amount": None} for o, t in steps])
        self.db.commit()
        self.db.info.pop(crud._RBAC_MEMO_KEY, None)

    def submit(self) -> models.LeaveRequest:
        day = datetime.date.today() + datetime.timedelta(days=50)
        lr = models.LeaveRequest(id=uuid.uuid4(), company_id=self.e1.company_id, employee_id=self.e1.id,
                                 leave_type_id=self.leave_type.id, from_date=day, to_date=day, days=1,
                                 is_half_day=False, reason="Chain test", status="pending")
        self.db.add(lr)
        self.db.flush()
        self.notified.clear()
        crud.notify_new_request(self.db, self.e1.company_id, self.e1, "Leave Request", "leave_request", lr.id)
        self.db.commit()
        return lr

    def decide(self, actor, lr, status="approved", notes=None):
        self.user = actor
        self.notified.clear()
        return self.client.patch(f"/api/leave-requests/{lr.id}", json={"status": status, "decision_notes": notes})

    def state(self, lr):
        self.db.refresh(lr)
        req = self.db.scalar(select(models.ApprovalRequest).where(
            models.ApprovalRequest.doctype == "leave_request", models.ApprovalRequest.document_id == lr.id))
        return lr.status, (req.current_step if req else None)


class SequentialTests(_Base):
    def setUp(self):
        super().setUp()
        self.configure((1, "reporting_manager"), (2, "dotted_line_manager"))

    def test_full_chain_approve(self):
        adjust = mock.patch.object(crud, "adjust_leave_allocation_used", wraps=crud.adjust_leave_allocation_used).start()
        lr = self.submit()
        # 2/3: only R1 (step 1) is told at submission.
        self.assertIn(self.r1.id, self.notified)
        self.assertNotIn(self.r2.id, self.notified)
        # 9: R2 can't act while step 1 is pending.
        self.assertEqual(self.decide(self.r2, lr).status_code, 403)
        self.assertEqual(self.state(lr), ("pending", 1))
        # 4: R1 approves -> step 2 activates, R2 notified now, balance untouched.
        r = self.decide(self.r1, lr, notes="ok from R1")
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(self.state(lr), ("pending", 2))
        self.assertIn(self.r2.id, self.notified)
        adjust.assert_not_called()
        # 10: R1 can't approve twice / skip ahead.
        self.assertEqual(self.decide(self.r1, lr).status_code, 403)
        # 5/6: R2 approves the last step -> fully approved, balance applied once.
        r = self.decide(self.r2, lr, notes="ok from R2")
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(self.state(lr)[0], "approved")
        self.assertEqual(adjust.call_count, 1)
        # 10: nothing more on a completed request.
        self.assertEqual(self.decide(self.r2, lr).status_code, 409)
        # 8: history shows both steps, approvers, actions, times, comments.
        self.user = self.e1_user
        h = self.client.get(f"/api/config/approval-history/leave_request/{lr.id}").json()
        self.assertEqual(h["mode"], "sequential")
        self.assertEqual([s["state"] for s in h["steps"]], ["approved", "approved"])
        self.assertEqual([s["comments"] for s in h["steps"]], ["ok from R1", "ok from R2"])
        self.assertTrue(all(s["acted_by"] and s["acted_at"] for s in h["steps"]))

    def test_reject_at_step_one_stops_the_chain(self):
        lr = self.submit()
        r = self.decide(self.r1, lr, status="rejected", notes="busy week")
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(self.state(lr)[0], "rejected")
        self.assertNotIn(self.r2.id, self.notified, "step 2 is never activated after a rejection")
        self.assertEqual(self.decide(self.r2, lr).status_code, 409)
        self.user = self.e1_user
        h = self.client.get(f"/api/config/approval-history/leave_request/{lr.id}").json()
        self.assertEqual([s["state"] for s in h["steps"]], ["rejected", "not_reached"])

    def test_leave_approver_role_cannot_skip_steps(self):
        lr = self.submit()
        with mock.patch.object(crud, "user_has_action", lambda db, uid, mod, act: uid == self.r2.id):
            self.assertEqual(self.decide(self.r2, lr).status_code, 403)
        self.assertEqual(self.state(lr), ("pending", 1))


class ParallelTests(_Base):
    def test_default_parallel_unchanged(self):
        """No workflow configured: either manager decides, first one is final."""
        # Turn the tenant's own leave workflow off (rolled back afterwards)
        # so this exercises the untouched default path.
        wf = approval_engine.get_active_workflow(self.db, self.e1.company_id, "leave_request")
        if wf is not None:
            wf.is_active = False
            self.db.commit()
        self.db.info.pop(crud._RBAC_MEMO_KEY, None)
        self.assertFalse(crud._has_custom_approval_workflow(self.db, self.e1.company_id, "leave_request"))
        lr = self.submit()
        self.assertIn(self.r1.id, self.notified)
        self.assertIn(self.r2.id, self.notified)
        r = self.decide(self.r2, lr)
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(self.state(lr)[0], "approved")
        self.assertEqual(self.decide(self.r1, lr).status_code, 409)

    def test_configured_parallel_first_decision_final(self):
        self.configure((1, "reporting_manager"), (1, "dotted_line_manager"))
        lr = self.submit()
        self.assertIn(self.r1.id, self.notified)
        self.assertIn(self.r2.id, self.notified)
        r = self.decide(self.r2, lr)
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(self.state(lr)[0], "approved")
        self.assertEqual(self.decide(self.r1, lr).status_code, 409)
        self.user = self.e1_user
        h = self.client.get(f"/api/config/approval-history/leave_request/{lr.id}").json()
        self.assertEqual(h["mode"], "parallel")


if __name__ == "__main__":
    unittest.main()


class CompanyModeTests(_Base):
    """Administration > Approval Workflows > Company approval mode."""

    def _set(self, mode, steps, doctypes=None):
        from app.routers import approvals_config as ac
        payload = {"mode": mode, "steps": steps}
        if doctypes:
            payload["doctypes"] = doctypes
        out = ac.set_approval_mode(payload, db=self.db, current_user=self.r1)
        self.db.info.pop(crud._RBAC_MEMO_KEY, None)
        return out

    def test_chain_applies_to_every_request_type_then_reset(self):
        from app.routers import approvals_config as ac
        out = self._set("sequential", [{"approver_type": "reporting_manager"},
                                       {"approver_type": "dotted_line_manager"}])
        self.assertEqual(out["mode"], "sequential")
        self.assertEqual({t["mode"] for t in out["types"]}, {"sequential"})
        self.assertEqual(len(out["types"]), len(ac.APPROVAL_DOCTYPES))
        # Parallel with no approvers = back to each employee's reporting managers.
        out = self._set("parallel", [])
        self.assertEqual(out["mode"], "parallel")
        self.assertEqual({t["mode"] for t in out["types"]}, {"default"})
        self.assertFalse(crud._has_custom_approval_workflow(self.db, self.e1.company_id, "travel_request"))

    def test_chain_on_another_module_travel(self):
        self._set("sequential", [{"approver_type": "reporting_manager"},
                                 {"approver_type": "dotted_line_manager"}], ["travel_request"])
        doc = uuid.uuid4()
        args = (self.e1.id, "travel_request", doc)
        self.assertTrue(crud.can_decide_request_configurable(self.db, self.r1, *args))
        self.assertFalse(crud.can_decide_request_configurable(self.db, self.r2, *args))
        self.notified.clear()
        self.assertEqual(crud.decide_configurable_request(self.db, self.r1, *args, "approved"), "pending")
        self.assertIn(self.r2.id, self.notified, "next step notified on another module too")
        self.db.info.pop(crud._RBAC_MEMO_KEY, None)
        self.assertFalse(crud.can_decide_request_configurable(self.db, self.r1, *args))
        self.assertTrue(crud.can_decide_request_configurable(self.db, self.r2, *args))
        self.assertEqual(crud.decide_configurable_request(self.db, self.r2, *args, "approved"), "approved")

    def test_missing_approver_step_is_skipped(self):
        self.e1.dotted_line_manager_id = None
        self.db.commit()
        self.configure((1, "reporting_manager"), (2, "dotted_line_manager"))
        lr = self.submit()
        r = self.decide(self.r1, lr)
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(self.state(lr)[0], "approved", "no second manager -> step 2 skipped, not sent to the Owner")
        self.user = self.e1_user
        h = self.client.get(f"/api/config/approval-history/leave_request/{lr.id}").json()
        self.assertEqual([s["state"] for s in h["steps"]], ["approved", "skipped"])

    def test_same_person_never_asked_twice(self):
        self.configure((1, "reporting_manager"), (2, "reporting_manager"), (3, "dotted_line_manager"))
        lr = self.submit()
        r = self.decide(self.r1, lr)
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(self.state(lr), ("pending", 3), "repeat step for R1 skipped; now R2's turn")
        self.assertIn(self.r2.id, self.notified)


class EveryModuleChainTests(_Base):
    """The same chain on every request type the engine serves: only step 1
    may act, approving notifies step 2, a rejection ends it."""

    def test_every_doctype(self):
        from app.routers import approvals_config as ac
        ac.set_approval_mode({"mode": "sequential", "steps": [
            {"approver_type": "reporting_manager"}, {"approver_type": "dotted_line_manager"}]},
            db=self.db, current_user=self.r1)
        for doctype, label in ac.APPROVAL_DOCTYPES:
            with self.subTest(doctype=doctype):
                self.db.info.pop(crud._RBAC_MEMO_KEY, None)
                doc = uuid.uuid4()
                args = (self.e1.id, doctype, doc)
                self.assertTrue(crud.can_decide_request_configurable(self.db, self.r1, *args))
                self.assertFalse(crud.can_decide_request_configurable(self.db, self.r2, *args))
                self.notified.clear()
                self.assertEqual(crud.decide_configurable_request(self.db, self.r1, *args, "approved"), "pending")
                if doctype not in crud._SELF_NOTIFYING_DOCTYPES:
                    self.assertIn(self.r2.id, self.notified, f"{doctype}: step 2 notified")
                self.db.info.pop(crud._RBAC_MEMO_KEY, None)
                self.assertFalse(crud.can_decide_request_configurable(self.db, self.r1, *args))
                self.assertTrue(crud.can_decide_request_configurable(self.db, self.r2, *args))
                self.assertEqual(crud.decide_configurable_request(self.db, self.r2, *args, "approved"), "approved")
                # rejection path on a fresh document
                doc2 = uuid.uuid4()
                self.db.info.pop(crud._RBAC_MEMO_KEY, None)
                self.assertTrue(crud.can_decide_request_configurable(self.db, self.r1, self.e1.id, doctype, doc2))
                self.assertEqual(crud.decide_configurable_request(
                    self.db, self.r1, self.e1.id, doctype, doc2, "rejected"), "rejected")
                self.db.info.pop(crud._RBAC_MEMO_KEY, None)
                self.assertFalse(crud.can_decide_request_configurable(self.db, self.r2, self.e1.id, doctype, doc2))

    def test_submit_notifies_only_step_one_for_every_module(self):
        """notify_new_request is called with each module's own entity name."""
        from app.routers import approvals_config as ac
        ac.set_approval_mode({"mode": "sequential", "steps": [
            {"approver_type": "reporting_manager"}, {"approver_type": "dotted_line_manager"}]},
            db=self.db, current_user=self.r1)
        for entity_type, label in [("leave_request", "Leave Request"), ("regularization", "Attendance Regularization"),
                                   ("overtime_request", "Overtime Request"), ("travel_request", "Travel Request"),
                                   ("reimbursement", "Reimbursement"), ("expense_report", "Expense Report"),
                                   ("salary_revision_request", "Salary Revision Request"),
                                   ("asset_request", "Asset Request"), ("exit_request", "Exit Request")]:
            with self.subTest(entity_type=entity_type):
                self.db.info.pop(crud._RBAC_MEMO_KEY, None)
                self.notified.clear()
                crud.notify_new_request(self.db, self.e1.company_id, self.e1, label, entity_type, uuid.uuid4())
                self.assertIn(self.r1.id, self.notified)
                self.assertNotIn(self.r2.id, self.notified, f"{entity_type}: step 2 must not be asked yet")
