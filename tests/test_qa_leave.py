"""QA issue list -- Leave, Approvals & Notifications (H-06..H-10, M-06..M-09,
N-04, N-05, N-07). Real routes (TestClient) and real users of tenant acme
(PEOPLE_TEST_TENANT to override), inside a transaction that is ALWAYS
rolled back.

    cd backend && venv/Scripts/python -m unittest tests.test_qa_leave -v
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete, select  # noqa: E402

from app import approval_engine, config, crud, email_service, leave_policy, models  # noqa: E402
from tests.test_people_visibility import PeopleTestBase  # noqa: E402

MON = datetime.date(2027, 2, 8)  # a Monday, future, same fiscal year (Apr-Mar) as today
D = datetime.timedelta


class LeaveQATests(PeopleTestBase):
    def setUp(self):
        super().setUp()
        mock.patch.object(config.settings, "rate_limit_enabled", False).start()
        mock.patch.object(email_service._executor, "submit", lambda *a, **k: None).start()
        # Everyone in the Owner's company (acme holds several companies).
        self.owner = self.users["owner"]
        self.company_id = self.owner.company_id
        company_users = [u for u in self.db.scalars(select(models.User).where(
            models.User.company_id == self.company_id, models.User.status == "active",
            models.User.employee_id.is_not(None))).all() if u.id != self.owner.id]
        self.manager = self.emp = self.emp_user = None
        for mgr in company_users:
            for e in self.db.scalars(select(models.Employee).where(
                    models.Employee.reporting_manager_id == mgr.employee_id, models.Employee.is_active.is_(True))):
                uid = crud.get_user_id_for_employee(self.db, e.id)
                if uid and uid != self.owner.id:
                    self.manager, self.emp, self.emp_user = mgr, e, self.db.get(models.User, uid)
                    break
            if self.emp is not None:
                break
        if self.emp is None:
            self.skipTest("no manager with a report that has a login")
        chain = {self.manager.employee_id, self.emp.id}
        self.others = [u for u in company_users if u.employee_id not in chain
                       and not crud._is_in_reporting_chain(self.db, u.employee_id, self.emp.id)
                       and not crud.is_fallback_approver(self.db, u)]
        if len(self.others) < 2:
            self.skipTest("need two users outside the employee's reporting chain")
        self.users["owner"] = self.owner
        self.users["manager"] = self.manager
        self.emp.employment_type = "Full Time"
        # A clean slate for the test dates and a 5-day week.
        self.db.execute(delete(models.LeaveRequest).where(
            models.LeaveRequest.employee_id == self.emp.id, models.LeaveRequest.from_date >= MON - D(days=7),
            models.LeaveRequest.from_date <= MON + D(days=60)))
        self.db.execute(delete(models.Holiday).where(
            models.Holiday.company_id == self.company_id, models.Holiday.holiday_date >= MON,
            models.Holiday.holiday_date <= MON + D(days=60)))
        settings_row = crud.get_company_settings(self.db, self.company_id)
        if settings_row is None:
            self.skipTest("company has no settings row")
        settings_row.working_days_per_week = 5
        # Default (reporting-manager) leave flow for this company.
        for wf in self.db.scalars(select(models.ApprovalWorkflow).where(
                models.ApprovalWorkflow.company_id == self.company_id,
                models.ApprovalWorkflow.doctype.in_(leave_policy.LEAVE_DOCTYPES))):
            wf.is_active = False
        self.lt = crud.create_leave_type(self.db, self.company_id, f"QA Leave {uuid.uuid4().hex[:6]}",
                                         leave_policy.unique_code(self.db, self.company_id, "QAL"),
                                         max_days_per_year=10)
        self.db.commit()
        crud.clear_rbac_memo(self.db)

    # ── helpers ──
    def apply(self, frm, to, days=None, half=False, type_name=None, expect=201, **extra):
        self.as_key_user(self.emp_user)
        body = {"employee_id": str(self.emp.id), "leave_type_name": type_name or self.lt.name,
                "from_date": frm.isoformat(), "to_date": to.isoformat(), "reason": "QA",
                "is_half_day": half, **extra}
        if half:
            body["half_day_period"] = "morning"
        if days is not None:
            body["days"] = days
        r = self.client.post("/api/leave-requests", json=body)
        self.assertEqual(r.status_code, expect, r.text)
        return r.json()

    def as_key_user(self, user):
        self.actor = user
        crud.clear_rbac_memo(self.db)

    def allocate(self, days, used=0, on_date=MON):
        fy = crud.get_or_create_fiscal_year(self.db, self.company_id, on_date)
        a = models.LeaveAllocation(id=uuid.uuid4(), employee_id=self.emp.id, leave_type_id=self.lt.id,
                                   fiscal_year_id=fy.id, allocated_days=days, carried_forward_days=0,
                                   used_days=used)
        self.db.add(a)
        self.db.commit()
        return a

    # ── H-06 ──
    def test_h06_server_computes_working_days(self):
        crud.create_holiday(self.db, self.company_id, MON + D(days=2), "QA Holiday")
        self.db.commit()
        # Mon..Fri with a Wednesday holiday = 4 working days, whatever the client says.
        self.apply(MON, MON + D(days=4), days=1, expect=422)
        out = self.apply(MON, MON + D(days=4))
        self.assertEqual(out["days"], 4.0)
        # Weekend only -> nothing to take.
        self.apply(MON + D(days=12), MON + D(days=13), expect=422)
        # Half day.
        out = self.apply(MON + D(days=14), MON + D(days=14), half=True)
        self.assertEqual(out["days"], 0.5)
        # Preview endpoint matches.
        r = self.client.get("/api/leave-requests/working-days",
                            params={"from_date": MON.isoformat(), "to_date": (MON + D(days=6)).isoformat()})
        self.assertEqual(r.json()["days"], 4.0)

    def test_h06_edit_recomputes_days(self):
        out = self.apply(MON, MON)
        r = self.client.patch(f"/api/leave-requests/{out['id']}/edit", json={
            "leave_type_name": self.lt.name, "from_date": MON.isoformat(),
            "to_date": (MON + D(days=6)).isoformat(), "days": 7})
        self.assertEqual(r.status_code, 422, r.text)
        r = self.client.patch(f"/api/leave-requests/{out['id']}/edit", json={
            "leave_type_name": self.lt.name, "from_date": MON.isoformat(),
            "to_date": (MON + D(days=6)).isoformat()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["days"], 5.0)

    # ── H-07 ──
    def test_h07_pending_counted_and_rechecked_at_approval(self):
        alloc = self.allocate(3)
        out = self.apply(MON, MON + D(days=1))  # 2 days pending
        self.assertEqual(crud.get_remaining_leave_balance(self.db, self.emp.id, self.lt, MON), 1.0)
        # Balance cut to 1 by HR before the manager decides -> approval refused.
        alloc.allocated_days = 1
        self.db.commit()
        self.as_key_user(self.manager)
        r = self.client.patch(f"/api/leave-requests/{out['id']}", json={"status": "approved"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.db.get(models.LeaveRequest, uuid.UUID(out["id"])).status, "pending")

    def test_h07_negative_balance_reported(self):
        self.allocate(2, used=5, on_date=crud.company_today(self.db, self.company_id))
        self.as_("owner")
        rows = [b for b in self.client.get("/api/leave-balances", params={"employee_id": str(self.emp.id)}).json()
                if b["leave_type_id"] == str(self.lt.id)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["remaining"], -3.0)

    # ── H-08 ──
    def test_h08_cap_without_allocation(self):
        self.db.add(models.LeaveRequest(
            id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id, leave_type_id=self.lt.id,
            from_date=MON - D(days=7), to_date=MON - D(days=7), days=9, status="approved",
            created_at=datetime.datetime.now(datetime.timezone.utc)))
        self.db.commit()
        self.assertIsNone(leave_policy.allocation_for(self.db, self.emp.id, self.lt.id, MON))
        self.assertEqual(crud.get_remaining_leave_balance(self.db, self.emp.id, self.lt, MON), 1.0)

    # ── H-09 ──
    def test_h09_unknown_type_is_422_and_codes_unique(self):
        before = self.db.scalar(select(models.LeaveType.id).where(models.LeaveType.name == "Totally New Leave"))
        self.assertIsNone(before)
        self.apply(MON, MON, type_name="Totally New Leave", expect=422)
        self.assertIsNone(self.db.scalar(select(models.LeaveType.id).where(
            models.LeaveType.company_id == self.company_id, models.LeaveType.name == "Totally New Leave")))
        out = self.apply(MON, MON, type_name=None, leave_type_id=str(self.lt.id))
        self.assertEqual(out["leave_type_name"], self.lt.name)
        a = crud.get_or_create_leave_type_by_name(self.db, self.company_id, "Specialleave Alpha")
        b = crud.get_or_create_leave_type_by_name(self.db, self.company_id, "Specialleave Beta")
        self.assertNotEqual(a.code, b.code)
        self.assertNotEqual(a.id, b.id)

    # ── H-10 / M-07 ──
    def test_h10_only_leave_admin_configures_and_never_self(self):
        self.as_("manager")
        if leave_policy.is_leave_admin(self.db, self.manager):
            self.skipTest("the picked manager is a leave admin")
        self.assertEqual(self.client.post("/api/leave-types", json={"name": "Mgr Type"}).status_code, 403)
        body = {"employee_id": str(self.emp.id), "leave_type_id": str(self.lt.id), "allocated": 5}
        self.assertEqual(self.client.post("/api/leave-balances", json=body).status_code, 403)
        owner = self.as_("owner")
        r = self.client.post("/api/leave-balances", json={**body, "employee_id": str(owner.employee_id)})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.client.post("/api/leave-balances", json={**body, "allocated": -1}).status_code, 422)
        self.assertEqual(self.client.post("/api/leave-balances", json={**body, "leave_type_id": None,
                                                                       "leave_type_name": "Nope"}).status_code, 422)
        r = self.client.post("/api/leave-balances", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.client.post("/api/leave-balances", json=body).status_code, 409)
        other = self.db.scalar(select(models.Employee).where(models.Employee.company_id != owner.company_id).limit(1))
        if other is not None:
            r = self.client.post("/api/leave-balances", json={**body, "employee_id": str(other.id)})
            self.assertEqual(r.status_code, 404, r.text)
        r = self.client.post("/api/leave-types", json={"name": "QA Generated Code Type", "max_days_per_year": 2})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(r.json()["code"])

    # ── M-06 ──
    def test_m06_withdrawn_leave_does_not_block(self):
        self.db.add(models.LeaveRequest(
            id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id, leave_type_id=self.lt.id,
            from_date=MON, to_date=MON, days=1, status="withdrawn",
            created_at=datetime.datetime.now(datetime.timezone.utc)))
        self.db.commit()
        out = self.apply(MON, MON)
        other = self.apply(MON + D(days=1), MON + D(days=1))
        r = self.client.patch(f"/api/leave-requests/{other['id']}/edit", json={
            "leave_type_name": self.lt.name, "from_date": MON.isoformat(), "to_date": MON.isoformat()})
        self.assertEqual(r.status_code, 409, "the live request on MON still clashes")
        self.assertEqual(out["status"], "pending")

    # ── M-08 ──
    def test_m08_no_approver_auto_approves_with_audit(self):
        with mock.patch.object(leave_policy, "has_any_approver", return_value=False):
            out = self.apply(MON, MON)
        self.assertEqual(out["status"], "approved")
        self.assertTrue(self.db.scalar(select(models.AuditLog).where(
            models.AuditLog.document_id == uuid.UUID(out["id"]), models.AuditLog.action == "auto_approve")))

    def test_m08_leave_admin_is_a_fallback(self):
        hr = self.users["owner"]
        with mock.patch.object(leave_policy, "is_leave_admin", side_effect=lambda db, u: u is not None and u.id == hr.id):
            ids = leave_policy.leave_admin_employee_ids(self.db, self.company_id, exclude_employee_id=self.emp.id)
        self.assertEqual(ids, {hr.employee_id})

    # ── M-09 ──
    def test_m09_next_step_notified(self):
        mgr_role = crud.get_user_primary_role(self.db, self.manager.id)
        step2 = next((u for u in self.others if crud.get_user_primary_role(self.db, u.id) is not None
                      and crud.get_user_primary_role(self.db, u.id).id != getattr(mgr_role, "id", None)), None)
        if step2 is None:
            self.skipTest("no second-step approver candidate")
        role = crud.get_user_primary_role(self.db, step2.id)
        wf = models.ApprovalWorkflow(id=uuid.uuid4(), company_id=self.company_id, doctype="leave_request",
                                     name="QA chain", is_active=True)
        self.db.add(wf)
        self.db.flush()
        self.db.add_all([
            models.ApprovalWorkflowStep(id=uuid.uuid4(), workflow_id=wf.id, step_order=1,
                                        approver_type="reporting_manager"),
            models.ApprovalWorkflowStep(id=uuid.uuid4(), workflow_id=wf.id, step_order=2,
                                        approver_type="role", role_id=role.id),
        ])
        self.db.commit()
        crud.clear_rbac_memo(self.db)
        out = self.apply(MON, MON)
        self.as_key_user(self.manager)
        r = self.client.patch(f"/api/leave-requests/{out['id']}", json={"status": "approved"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "pending")
        self.assertTrue(self.db.scalars(select(models.Notification).where(
            models.Notification.user_id == step2.id,
            models.Notification.entity_id == uuid.UUID(out["id"]))).all(), "step-2 approver notified")

    # ── N-04 ──
    def test_n04_allocations_created_idempotently(self):
        made = leave_policy.ensure_allocations(self.db, self.company_id, [self.emp.id], MON)
        self.assertGreaterEqual(made, 1)
        self.assertIsNotNone(leave_policy.allocation_for(self.db, self.emp.id, self.lt.id, MON))
        self.assertEqual(leave_policy.ensure_allocations(self.db, self.company_id, [self.emp.id], MON), 0)
        self.emp.employment_type = "Contract"  # not eligible by default
        lt2 = crud.create_leave_type(self.db, self.company_id, "QA FT only", "QAFT", max_days_per_year=4)
        self.db.flush()
        leave_policy.ensure_allocations(self.db, self.company_id, [self.emp.id], MON)
        self.assertIsNone(leave_policy.allocation_for(self.db, self.emp.id, lt2.id, MON))

    # ── N-05 ──
    def test_n05_slot_by_code_or_name(self):
        for code, name, slot in (("CASUALLEAV", "Casual Leave", "CL"), ("CL", "Casual", "CL"),
                                 ("XYZ", "Sick Leave", "SL"), ("LOP", "Unpaid", "LOP"), ("QQ", "Fun", None)):
            self.assertEqual(leave_policy.leave_slot(models.LeaveType(code=code, name=name)), slot)

    # ── N-07 ──
    def test_n07_granular_grant_alone_cannot_decide(self):
        out = self.apply(MON, MON)
        outsider = self.others[0]
        self.assertFalse(crud._is_in_reporting_chain(self.db, outsider.employee_id, self.emp.id))
        self.as_key_user(outsider)
        with mock.patch.object(crud, "user_has_action", return_value=True):
            r = self.client.patch(f"/api/leave-requests/{out['id']}", json={"status": "approved"})
        self.assertEqual(r.status_code, 403, r.text)
        with mock.patch.object(leave_policy, "is_leave_admin", return_value=True):
            r = self.client.patch(f"/api/leave-requests/{out['id']}", json={"status": "approved"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "approved")


if __name__ == "__main__":
    unittest.main()
