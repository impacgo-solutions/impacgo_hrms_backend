"""Employee Permissions (Administration > Roles & Permissions > Employee
Permissions): per-employee access levels that replace the role, enforced
by the real APIs across modules.

    cd backend && venv/Scripts/python -m unittest tests.test_employee_permissions -v

Runs inside a transaction that is always rolled back."""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, deps, models  # noqa: E402
from app.database import get_db  # noqa: E402
from app.deps import _OWNER_ROLE_NAME, get_current_user  # noqa: E402
from app.main import app  # noqa: E402

TENANT = os.environ.get("EMPLOYEE_PERMISSIONS_TEST_TENANT", "impacgo-solutions")
IC = "Professional / IC Employee"


class EmployeePermissionsTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None),
                                                          models.User.status == "active")).all()
        role_of = {u.id: crud.get_user_primary_role(self.db, u.id) for u in users}
        self.owner = next((u for u in users if role_of[u.id] and role_of[u.id].name == _OWNER_ROLE_NAME), None)
        self.ic = next((u for u in users if role_of[u.id] and role_of[u.id].name == IC), None)
        self.hr = next((u for u in users if role_of[u.id] and role_of[u.id].name == "HR / Recruitment Staff"), None)
        if not (self.owner and self.ic and self.hr):
            self.skipTest("tenant needs an Owner, an HR user and an IC employee")
        # Start the IC from their role alone -- the shared test DB may give
        # them real overrides / grants (rolled back with everything else).
        self.db.execute(delete(models.EmployeePermission).where(
            models.EmployeePermission.employee_id == self.ic.employee_id))
        self.db.execute(delete(models.EmployeeAccessGrant).where(
            models.EmployeeAccessGrant.employee_id == self.ic.employee_id))
        self.db.flush()
        crud.clear_rbac_memo(self.db)
        self.user = self.owner
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # helpers
    def set_levels(self, target, levels, *, as_=None, expect=200):
        self.user = as_ or self.owner
        r = self.client.put(f"/api/employee-permissions/{target.employee_id}", json={"permissions": levels})
        self.assertEqual(r.status_code, expect, r.text)
        crud.clear_rbac_memo(self.db)
        self.db.info.pop("_recruitment_level", None)
        return r.json() if r.status_code == 200 else r

    def get(self, path, as_, expect=200):
        self.user = as_
        crud.clear_rbac_memo(self.db)
        r = self.client.get(path)
        self.assertEqual(r.status_code, expect, f"GET {path} as {as_.email}: {r.status_code} {r.text[:300]}")
        return r

    # ── tests ────────────────────────────────────────────────────────────
    def test_raise_an_ic_employee_to_admin_independent_of_role(self):
        self.get("/api/recruitment/job-openings", self.ic, expect=403)
        detail = self.set_levels(self.ic, {"recruitment": "admin", "approvals": "view", "reports": "view"})
        rec = next(m for m in detail["modules"] if m["key"] == "recruitment")
        self.assertEqual((rec["role_level"], rec["level"], rec["effective"]), ("none", "admin", "admin"))
        me = self.get("/api/auth/me", self.ic).json()
        self.assertEqual(me["matrix"]["recruitment"], "a")
        self.assertEqual(me["employee_permissions"]["recruitment"], "admin")
        self.assertIn("approvals.view", me["actions"])
        self.get("/api/recruitment/job-openings", self.ic)
        self.user = self.ic
        r = self.client.put("/api/recruitment/settings", json={"offer_expiry_days": 9})  # admin-only
        self.assertEqual(r.status_code, 200, r.text)
        today = datetime.date.today()
        self.get(f"/api/reports/attendance?from_date={today.replace(day=1)}&to_date={today}", self.ic)

    def test_lower_an_hr_user_to_no_access(self):
        self.get("/api/recruitment/job-openings", self.hr)
        self.set_levels(self.hr, {"recruitment": "none", "people": "none"})
        self.get("/api/recruitment/job-openings", self.hr, expect=403)
        self.get("/api/employees/full", self.hr, expect=403)
        me = self.get("/api/auth/me", self.hr).json()
        self.assertEqual(me["matrix"]["recruitment"], "n")
        self.assertEqual(me["people_actions"], [])
        # back to the role
        self.set_levels(self.hr, {"recruitment": None, "people": None})
        self.get("/api/recruitment/job-openings", self.hr)

    def test_self_only_vs_view_record_scope(self):
        self.set_levels(self.ic, {"leave": "view", "documents": "view", "people": "view"})
        self.assertIsNone(crud.get_visible_employee_ids_for_requests(self.db, self.ic, "leave_request"))
        self.assertIsNone(crud.get_visible_employee_ids_for_docs(self.db, self.ic))
        self.assertIsNone(crud.get_people_directory_visible_ids(self.db, self.ic))
        rows = self.get("/api/employees/full", self.ic).json()
        self.assertGreater(len(rows), 1)  # the whole directory, not just self
        self.set_levels(self.ic, {"leave": "self", "documents": "self", "people": "self"})
        self.assertEqual(crud.get_visible_employee_ids_for_requests(self.db, self.ic, "leave_request"), [self.ic.employee_id])
        self.assertEqual(crud.get_visible_employee_ids_for_docs(self.db, self.ic), [self.ic.employee_id])
        leave = self.get("/api/leave-requests", self.ic).json()
        items = leave["items"] if isinstance(leave, dict) else leave
        self.assertTrue(all(str(i.get("employee_id")) == str(self.ic.employee_id) for i in items))

    def test_payroll_scope_and_approvals(self):
        self.assertEqual(deps.get_payroll_scope(self.ic, self.db), "self")
        self.set_levels(self.ic, {"payroll": "edit"})
        self.assertEqual(deps.get_payroll_scope(self.ic, self.db), "org")
        self.set_levels(self.ic, {"payroll": "self"})
        self.assertEqual(deps.get_payroll_scope(self.ic, self.db), "self")
        other = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.ic.company_id, models.Employee.id != self.ic.employee_id,
            models.Employee.reporting_manager_id != self.ic.employee_id)).first()
        self.assertFalse(crud.can_decide_request(self.db, self.ic, other.id))
        self.set_levels(self.ic, {"approvals": "approve"})
        self.assertTrue(crud.can_decide_request(self.db, self.ic, other.id))
        self.assertFalse(crud.can_decide_request(self.db, self.ic, self.ic.employee_id))  # never self-approval

    def test_guards_audit_and_tenant_isolation(self):
        r = self.set_levels(self.owner, {"recruitment": "none"}, expect=422)  # Owner can't be restricted
        self.set_levels(self.ic, {"recruitment": "bogus"}, expect=422)
        self.set_levels(self.ic, {"nope": "view"}, expect=422)
        self.user = self.owner
        self.assertEqual(self.client.get(f"/api/employee-permissions/{uuid.uuid4()}").status_code, 404)
        self.get(f"/api/employee-permissions/{self.hr.employee_id}", self.ic, expect=403)  # needs RBAC settings
        # A non-Owner admin can't grant beyond their own level.
        hr_me = crud.effective_user_matrix(self.db, self.hr)
        if hr_me.get("system_settings_rbac") in ("e", "a") and hr_me.get("payroll_process") not in ("a",):
            self.set_levels(self.ic, {"payroll": "admin"}, as_=self.hr, expect=403)
        self.set_levels(self.ic, {"recruitment": "view"})
        n = self.db.scalar(text("SELECT count(*) FROM core_audit_logs WHERE doctype = 'employee_permissions' "
                                "AND document_id = :e"), {"e": self.ic.employee_id})
        self.assertGreaterEqual(n, 1)
        listed = self.client.get("/api/employee-permissions").json()
        self.assertTrue(any(x["employee_id"] == str(self.ic.employee_id) for x in listed))
        self.assertEqual(self.client.delete(f"/api/employee-permissions/{self.ic.employee_id}").status_code, 204)
        crud.clear_rbac_memo(self.db)
        self.assertEqual(crud.effective_user_matrix(self.db, self.ic).get("recruitment"), "n")


if __name__ == "__main__":
    unittest.main()
