"""Privilege escalation through the RBAC admin APIs: nobody can grant more
than they hold, and a non-Owner can't change their own access by any route
(module-access grants, role actions, role assignment, Employee
Permissions). Legitimate delegation still works.

    cd backend && venv/Scripts/python -m unittest tests.test_rbac_escalation -v

Direct HTTP requests through the real routes (FastAPI TestClient), inside a
transaction that is ALWAYS rolled back. Tenant domz-solutions by default
(RBAC_TEST_TENANT to override) -- it needs core_employee_access_grants.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select, text  # noqa: E402

from app import crud, database, deps, main, models, role_tiers  # noqa: E402
from tests.test_employee_access_lifecycle import _Base  # noqa: E402

TENANT = os.environ.get("RBAC_TEST_TENANT", "domz-solutions")
ROLE_CANDIDATES = {
    "owner": ["Organization Owner / CEO"],
    "it": ["IT / System Admin", "System Administrator"],
    "hr": ["HR Manager", "HR Executive", "HR / Recruitment Staff"],
    "payroll": ["Payroll Officer", "Finance / Payroll Staff", "Accountant"],
    "manager": ["Manager", "Team Lead", "Project Manager"],
    "employee": ["Professional / IC Employee", "Employee (ESS)", "Associate / Intern"],
}
FULL_PAYROLL = ["view", "generate", "edit", "delete", "export"]


class RbacEscalationTests(_Base):
    tenant = TENANT

    def setUp(self):
        super().setUp()
        if self.db.execute(text("SELECT to_regclass('core_employee_access_grants')")).scalar() is None:
            self.skipTest(f"{TENANT} has no core_employee_access_grants table")
        owner = self._owner()
        self.company_id = owner.company_id
        self.roles = {}
        for key, names in ROLE_CANDIDATES.items():
            role = next((r for n in names if (r := crud.get_role_by_name(self.db, self.company_id, n))), None)
            if role is None:
                self.skipTest(f"no {key} role in {TENANT}")
            self.roles[key] = role
        # One fresh account per role: existing employees, re-roled inside the
        # rolled-back transaction, none with prior grants / permissions.
        self.users = {"owner": owner}
        used = {owner.id}
        candidates = self.db.scalars(select(models.User).where(
            models.User.status == "active", models.User.company_id == self.company_id,
            models.User.employee_id.is_not(None))).all()
        pool = [u for u in candidates if u.id not in used and not role_tiers.is_owner(self.db, u.id)]
        needed = ["it", "hr", "payroll", "manager", "employee", "target", "target2"]
        if len(pool) < len(needed):
            self.skipTest("not enough employees")
        for key, user in zip(needed, pool):
            role = self.roles.get(key, self.roles["employee"])
            self.db.execute(delete(models.UserRole).where(models.UserRole.user_id == user.id))
            self.db.add(models.UserRole(user_id=user.id, role_id=role.id))
            self.db.execute(text("DELETE FROM core_employee_access_grants WHERE employee_id = :e"),
                            {"e": user.employee_id})
            self.db.execute(text("DELETE FROM core_employee_permissions WHERE employee_id = :e"),
                            {"e": user.employee_id})
            self.users[key] = user
        self.db.flush()
        crud.clear_rbac_memo(self.db)
        self.actor = owner
        main.app.dependency_overrides[database.get_db] = lambda: self.db
        main.app.dependency_overrides[deps.get_current_user] = lambda: self.actor
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)

    # ── helpers ──
    def as_(self, key):
        self.actor = self.users[key]
        crud.clear_rbac_memo(self.db)
        return self

    def grants(self, resource):
        return {uuid.UUID(g["employee_id"]): g["actions"]
                for g in self.client.get("/api/module-access/grants").json() if g["resource"] == resource}

    def put_grants(self, resource, assignments: dict):
        return self.client.put(f"/api/module-access/grants/{resource}", json={"assignments": [
            {"employee_id": str(emp), "actions": acts} for emp, acts in assignments.items()]})

    def make_payroll_delegator(self):
        """A payroll Admin who may also use the RBAC screens: the Owner
        grants them Administration (the supported way to delegate admin)."""
        self.as_("owner")
        resp = self.put_grants("admin", {self.emp("payroll"): ["view", "configure"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        crud.clear_rbac_memo(self.db)
        if crud.effective_user_matrix(self.db, self.users["payroll"]).get("payroll_process") != "a":
            self.skipTest("payroll role isn't payroll Admin here")

    def emp(self, key):
        return self.users[key].employee_id

    def has_payroll(self, key):
        crud.clear_rbac_memo(self.db)
        user = self.users[key]
        matrix = crud.effective_user_matrix(self.db, user)
        return (matrix.get("payroll_process", "n") not in ("n", "s")
                or crud.user_has_action(self.db, user.id, "payroll", "generate"))

    def assert_403(self, resp, fragment=None):
        self.assertEqual(resp.status_code, 403, resp.text)
        if fragment:
            self.assertIn(fragment, resp.json()["detail"])

    # ── the reported vulnerability ──
    def test_it_admin_cannot_self_grant_payroll(self):
        self.assertFalse(self.has_payroll("it"))
        self.as_("it")
        self.assert_403(self.put_grants("payroll", {self.emp("it"): ["view"]}), "your own access")
        self.assert_403(self.put_grants("payroll", {self.emp("it"): FULL_PAYROLL}), "your own access")
        self.assertFalse(self.has_payroll("it"))

    def test_it_admin_cannot_grant_payroll_to_others(self):
        self.as_("it")
        self.assert_403(self.put_grants("payroll", {self.emp("target"): ["view"]}), "above your own level")
        self.assert_403(self.put_grants("payroll", {self.emp("target"): FULL_PAYROLL}))
        self.assertFalse(self.has_payroll("target"))

    def test_self_grant_hidden_among_legit_rows(self):
        """Indirect: own row slipped into an otherwise-valid full replace."""
        self.make_payroll_delegator()
        self.assertEqual(self.put_grants("payroll", {self.emp("target"): ["view"]}).status_code, 200)
        self.as_("payroll")
        resp = self.put_grants("payroll", {self.emp("target"): ["view"], self.emp("payroll"): ["view"]})
        self.assert_403(resp, "your own access")
        self.assertEqual(set(self.grants("payroll")), {self.emp("target")})

    def test_it_admin_cannot_revoke_payroll_grants(self):
        self.as_("owner")
        self.put_grants("payroll", {self.emp("target"): ["view", "generate"]})
        self.as_("it")
        self.assert_403(self.put_grants("payroll", {}))
        self.assert_403(self.client.delete(f"/api/module-access/grants/payroll/{self.emp('target')}"))
        self.assertIn(self.emp("target"), self.grants("payroll"))

    def test_unchanged_rows_never_block(self):
        self.as_("owner")
        self.put_grants("payroll", {self.emp("target"): ["view"]})
        self.as_("it")
        self.assertEqual(self.put_grants("payroll", {self.emp("target"): ["view"]}).status_code, 200)

    # ── indirect routes ──
    def test_it_admin_cannot_add_payroll_actions_to_a_role(self):
        self.as_("it")
        for role_key in ("it", "employee"):  # own role, and one somebody else holds
            with self.subTest(role=role_key):
                resp = self.client.put(f"/api/roles/{self.roles[role_key].id}/actions",
                                       json={"granted": {"payroll": FULL_PAYROLL}})
                self.assert_403(resp, "above your own level")
        self.assertFalse(self.has_payroll("it"))

    def test_it_admin_cannot_assign_self_a_payroll_role(self):
        self.as_("it")
        resp = self.client.post(f"/api/users/{self.users['it'].id}/roles",
                                json={"role_id": str(self.roles["payroll"].id)})
        self.assert_403(resp)
        # Even a role within their own ceiling -- no self role changes at all.
        resp = self.client.post(f"/api/users/{self.users['it'].id}/roles",
                                json={"role_id": str(self.roles["employee"].id)})
        self.assert_403(resp, "your own access")
        resp = self.client.patch(f"/api/employees/{self.emp('it')}/role",
                                 json={"role_name": self.roles["payroll"].name})
        self.assert_403(resp)
        self.assertFalse(self.has_payroll("it"))

    def test_it_admin_cannot_change_own_employee_permissions(self):
        self.as_("it")
        self.assert_403(self.client.put(f"/api/employee-permissions/{self.emp('it')}",
                                        json={"permissions": {"payroll": "admin"}}), "your own access")
        self.assert_403(self.client.put(f"/api/employee-permissions/{self.emp('it')}",
                                        json={"permissions": {"payroll": None}}), "your own access")
        self.assert_403(self.client.delete(f"/api/employee-permissions/{self.emp('it')}"), "your own access")
        # ...nor give payroll to someone else through Employee Permissions.
        self.assert_403(self.client.put(f"/api/employee-permissions/{self.emp('target')}",
                                        json={"permissions": {"payroll": "view"}}))
        self.assertFalse(self.has_payroll("it"))

    def test_owner_restriction_cannot_be_lifted_by_self(self):
        """Owner restricts HR's payroll; HR can't clear it on their own row."""
        self.as_("owner")
        self.assertEqual(self.client.put(f"/api/employee-permissions/{self.emp('hr')}",
                                         json={"permissions": {"payroll": "none"}}).status_code, 200)
        self.as_("hr")
        self.assert_403(self.client.delete(f"/api/employee-permissions/{self.emp('hr')}"))
        self.assertFalse(self.has_payroll("hr"))

    # ── roles without RBAC admin can't reach the APIs at all ──
    def test_manager_and_employee_are_refused(self):
        for key in ("manager", "employee"):
            with self.subTest(role=key):
                self.as_(key)
                self.assert_403(self.put_grants("payroll", {self.emp(key): FULL_PAYROLL}))
                self.assert_403(self.put_grants("payroll", {self.emp("target"): ["view"]}))
                self.assert_403(self.client.put(f"/api/roles/{self.roles[key].id}/actions",
                                                json={"granted": {"payroll": FULL_PAYROLL}}))
                self.assert_403(self.client.post(f"/api/users/{self.users[key].id}/roles",
                                                 json={"role_id": str(self.roles["payroll"].id)}))
                self.assertFalse(self.has_payroll(key))

    # ── legitimate delegation ──
    def test_owner_delegates_payroll_and_inheritance_applies(self):
        self.as_("owner")
        self.assertFalse(self.has_payroll("target"))
        resp = self.put_grants("payroll", {self.emp("target"): ["view", "generate"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(self.has_payroll("target"))  # grant raises the payroll columns, as before
        self.assertEqual(self.client.delete(
            f"/api/module-access/grants/payroll/{self.emp('target')}").status_code, 204)
        self.assertFalse(self.has_payroll("target"))

    def test_payroll_admin_delegates_within_own_level(self):
        self.make_payroll_delegator()
        self.as_("payroll")
        resp = self.put_grants("payroll", {self.emp("target"): FULL_PAYROLL})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(self.has_payroll("target"))
        self.assertEqual(self.client.delete(
            f"/api/module-access/grants/payroll/{self.emp('target')}").status_code, 204)
        # ...but can't add to their own grants (reducing them is allowed).
        self.assert_403(self.put_grants("admin", {self.emp("payroll"): ["view", "configure", "manage"]}),
                        "your own access")
        self.assertEqual(self.put_grants("admin", {self.emp("payroll"): ["view"]}).status_code, 200)

    def test_it_admin_still_delegates_admin_module(self):
        """IT keeps doing what it legitimately can: Administration access."""
        self.as_("it")
        resp = self.put_grants("admin", {self.emp("target"): ["view", "configure"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.client.put(f"/api/employee-permissions/{self.emp('target')}",
                                         json={"permissions": {"admin": "view"}}).status_code, 200)
        self.assertEqual(self.client.put(f"/api/roles/{self.roles['employee'].id}/actions",
                                         json={"granted": {"admin": []}}).status_code, 200)

    def test_module_without_columns_requires_holding_each_action(self):
        """People ('employee' resource has no matrix column): IT can't hand
        out HR data access it doesn't hold; whoever holds it can."""
        self.as_("it")
        if crud.user_has_action(self.db, self.users["it"].id, "employee", "terminate"):
            self.skipTest("IT role holds employee.terminate here")
        self.assert_403(self.put_grants("employee", {self.emp("target"): ["view", "terminate"]}),
                        "don't hold")
        self.as_("owner")
        self.assertEqual(self.put_grants("employee", {self.emp("target"): ["view"]}).status_code, 200)

    def test_owner_may_change_own_and_any_access(self):
        self.as_("owner")
        self.assertEqual(self.put_grants("payroll", {self.emp("owner"): ["view"]}).status_code, 200)
        resp = self.client.put(f"/api/roles/{self.roles['hr'].id}/actions",
                               json={"granted": {"payroll": ["view"]}})
        self.assertEqual(resp.status_code, 200, resp.text)


if __name__ == "__main__":
    unittest.main()
