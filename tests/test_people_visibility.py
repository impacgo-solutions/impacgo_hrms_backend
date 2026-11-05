"""Employee lists and profiles only expose who the caller may see, and pay
data (CTC, salary, PAN, bank) only where they may see pay:
GET /api/employees, /employees/full, /employees/directory and the
per-employee profile routes -- for Manager, HR, Payroll, Owner, Employee
and cross-tenant callers, by direct HTTP request.

    cd backend && venv/Scripts/python -m unittest tests.test_people_visibility -v

Real routes (FastAPI TestClient), real users of each role in tenant acme
(PEOPLE_TEST_TENANT to override), inside a transaction that is ALWAYS
rolled back.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402

from app import crud, database, deps, main, models  # noqa: E402
from tests.test_employee_access_lifecycle import _Base  # noqa: E402

TENANT = os.environ.get("PEOPLE_TEST_TENANT", "acme")
OTHER_TENANT = os.environ.get("PEOPLE_TEST_OTHER_TENANT", "domz-solutions")
ROLES = {
    "manager": ["Manager", "Team Lead", "Sales Manager"],
    "hr": ["HR Manager", "HR / Recruitment Staff", "HR Executive"],
    "payroll": ["Payroll Officer", "Finance / Payroll Staff", "Accountant"],
    "owner": ["Organization Owner / CEO"],
    "employee": ["Employee (ESS)", "Sales Executive", "Support Agent"],
    "no_people": ["Professional / IC Employee", "Associate / Intern"],
}


class PeopleTestBase(_Base):
    """Real users of each role (ROLES) + TestClient as that user."""

    tenant = TENANT

    def setUp(self):
        super().setUp()
        users = self.db.scalars(select(models.User).where(
            models.User.status == "active", models.User.employee_id.is_not(None))).all()
        by_role: dict[str, list[models.User]] = {}
        for u in users:
            role = crud.get_user_primary_role(self.db, u.id)
            if role is not None:
                by_role.setdefault(role.name, []).append(u)
        self.users = {}
        for key, names in ROLES.items():
            pick = None
            for n in names:
                for u in by_role.get(n, []):
                    if key != "manager" or self._has_reports(u):
                        pick = u
                        break
                if pick:
                    break
            if pick is None:
                self.skipTest(f"no {key} user in {TENANT}")
            self.users[key] = pick
        self.company_id = self.users["owner"].company_id
        self.actor = None
        main.app.dependency_overrides[database.get_db] = lambda: self.db
        main.app.dependency_overrides[deps.get_current_user] = lambda: self.actor
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)

    def ids_of(self, user) -> set[str]:
        """Active employees of the caller's OWN company (a tenant can hold
        several companies -- acme does)."""
        return {str(i) for i in self.db.scalars(select(models.Employee.id).where(
            models.Employee.company_id == user.company_id, models.Employee.is_active.is_(True)))}

    @property
    def company_ids(self) -> set[str]:
        return self.ids_of(self.actor)

    def _has_reports(self, u):
        return bool(self.db.scalar(select(func.count()).select_from(models.Employee).where(
            models.Employee.reporting_manager_id == u.employee_id, models.Employee.is_active.is_(True))))

    def as_(self, key):
        self.actor = self.users[key]
        crud.clear_rbac_memo(self.db)
        return self.users[key]

    def list_all(self, path, limit):
        """Every row across pages; asserts no empty page while has_more."""
        rows, cursor = [], None
        while True:
            params = {"limit": limit} | ({"cursor": cursor} if cursor else {})
            resp = self.client.get(path, params=params)
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            if body["has_more"]:
                self.assertTrue(body["items"], "empty page while has_more")
            rows += body["items"]
            if not body["has_more"]:
                return rows
            cursor = body["next_cursor"]

    def subtree_and_self(self, user):
        visible = crud.get_people_directory_visible_ids(self.db, user)
        self.assertIsNotNone(visible, "expected a scoped caller")
        return {str(v) for v in visible}


class PeopleVisibilityTests(PeopleTestBase):
    # ── Manager ──
    def test_manager_list_only_team_and_no_team_ctc(self):
        me = self.as_("manager")
        allowed = self.subtree_and_self(me)
        for path, limit in (("/api/employees", 3), ("/api/employees/full", 3)):
            with self.subTest(path):
                rows = self.list_all(path, limit)
                ids = {r["id"] for r in rows}
                self.assertEqual(ids, allowed)
                self.assertLess(len(ids), len(self.company_ids))
                for r in rows:
                    if r["id"] != str(me.employee_id):
                        self.assertEqual(r["ctc"], 0, f"team member's CTC exposed in {path}")
                        if "payrollInfo" in r:
                            self.assertEqual(r["payrollInfo"]["pan"], "—")
                            self.assertEqual(r["payrollInfo"]["account"], "—")
                            self.assertEqual(r["payrollInfo"]["gross"], 0)

    def test_manager_sees_own_pay(self):
        me = self.as_("manager")
        own = self.db.get(models.Employee, me.employee_id)
        rows = {r["id"]: r for r in self.list_all("/api/employees/full", 50)}
        self.assertEqual(rows[str(me.employee_id)]["ctc"], own.annual_ctc or 0)

    def test_manager_directory_is_team_plus_own_managers(self):
        me = self.as_("manager")
        allowed = self.subtree_and_self(me)
        rows = self.client.get("/api/employees/directory", params={"limit": 500}).json()
        ids = {r["id"] for r in rows}
        self.assertTrue(allowed <= ids)
        chain = {str(i) for i in crud._management_chain_ids(self.db, me.employee_id, me.company_id)}
        self.assertEqual(ids, allowed | (chain & self.company_ids))
        self.assertLess(len(ids), len(self.company_ids))
        for r in rows:
            self.assertEqual(set(r), {"id", "name", "department", "designation", "branch",
                                      "reporting_manager_id", "dotted_line_manager_id"})

    def test_manager_cannot_open_outsiders_by_id(self):
        me = self.as_("manager")
        allowed = self.subtree_and_self(me)
        outsider = next(i for i in self.company_ids if i not in allowed)
        for tab in ("overview", "personal", "professional", "education", "payroll", "benefits"):
            with self.subTest(tab):
                self.assertEqual(self.client.get(f"/api/employees/{outsider}/{tab}").status_code, 403)
        member = next(i for i in allowed if i != str(me.employee_id))
        resp = self.client.get(f"/api/employees/{member}/overview")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["ctc"], 0)
        self.assertEqual(self.client.get(f"/api/employees/{member}/payroll").status_code, 403)

    # ── HR / Payroll / Owner: whole company with pay ──
    def test_admins_see_everyone_with_pay(self):
        for key in ("hr", "payroll", "owner"):
            with self.subTest(key):
                me = self.as_(key)
                with_ctc = self.db.scalar(select(models.Employee).where(
                    models.Employee.company_id == me.company_id, models.Employee.is_active.is_(True),
                    models.Employee.annual_ctc > 0).limit(1))
                rows = self.list_all("/api/employees", 200)
                self.assertEqual({r["id"] for r in rows}, self.company_ids)
                if with_ctc is not None:
                    row = next(r for r in rows if r["id"] == str(with_ctc.id))
                    self.assertEqual(row["ctc"], float(with_ctc.annual_ctc))
                    resp = self.client.get(f"/api/employees/{with_ctc.id}/overview")
                    self.assertEqual(resp.json()["ctc"], with_ctc.annual_ctc)
                dir_ids = {r["id"] for r in self.client.get("/api/employees/directory",
                                                               params={"limit": 500}).json()}
                self.assertEqual(dir_ids, self.company_ids)

    # ── Employees ──
    def test_employee_sees_only_self(self):
        me = self.as_("employee")
        rows = self.list_all("/api/employees", 50)
        self.assertEqual({r["id"] for r in rows}, {str(me.employee_id)})
        rows = self.client.get("/api/employees/full").json()
        self.assertEqual({r["id"] for r in rows}, {str(me.employee_id)})
        other = next(i for i in self.company_ids if i != str(me.employee_id))
        self.assertEqual(self.client.get(f"/api/employees/{other}/overview").status_code, 403)
        self.assertEqual(self.client.get(f"/api/employees/{me.employee_id}/overview").status_code, 200)

    def test_employee_without_people_access(self):
        me = self.as_("no_people")
        self.assertEqual(self.client.get("/api/employees").status_code, 403)
        self.assertEqual(self.client.get("/api/employees/full").status_code, 403)
        ids = {r["id"] for r in self.client.get("/api/employees/directory", params={"limit": 500}).json()}
        chain = {str(i) for i in crud._management_chain_ids(self.db, me.employee_id, me.company_id)}
        self.assertEqual(ids, {str(me.employee_id)} | (chain & self.company_ids))
        own = self.db.get(models.Employee, me.employee_id)
        if own.reporting_manager_id:  # can still see who their manager is
            self.assertIn(str(own.reporting_manager_id), ids)

    # ── Cross-tenant ──
    def test_cross_tenant_ids_are_not_reachable(self):
        foreign = self.db.execute(text(
            f'SELECT id FROM "{OTHER_TENANT}".core_employees LIMIT 1')).scalar()
        if foreign is None:
            self.skipTest(f"no employees in {OTHER_TENANT}")
        for key in ("owner", "hr", "manager"):
            with self.subTest(key):
                self.as_(key)
                for tab in ("overview", "payroll"):
                    self.assertIn(self.client.get(f"/api/employees/{foreign}/{tab}").status_code, (403, 404))
                rows = self.list_all("/api/employees", 200)
                self.assertNotIn(str(foreign), {r["id"] for r in rows})
                self.assertTrue({r["id"] for r in rows} <= self.company_ids)

    def test_other_company_in_same_tenant_is_invisible(self):
        """Company isolation inside one tenant schema."""
        me = self.as_("hr")
        others = {str(i) for i in self.db.scalars(select(models.Employee.id).where(
            models.Employee.company_id != me.company_id, models.Employee.is_active.is_(True)))}
        if not others:
            self.skipTest("single-company tenant")
        for path in ("/api/employees",):
            ids = {r["id"] for r in self.list_all(path, 200)}
            self.assertFalse(ids & others)
        ids = {r["id"] for r in self.client.get("/api/employees/directory", params={"limit": 500}).json()}
        self.assertFalse(ids & others)
        self.assertEqual(self.client.get(f"/api/employees/{next(iter(others))}/overview").status_code, 404)

    def test_bulk_pay_rule_matches_per_employee_rule(self):
        """get_payroll_visible_ids is the list twin of can_view_employee_payroll."""
        for key in ("manager", "employee", "hr", "payroll"):
            with self.subTest(key):
                user = self.as_(key)
                bulk = crud.get_payroll_visible_ids(self.db, user)
                for eid in list(self.company_ids)[:25]:
                    expected = crud.can_view_employee_payroll(self.db, user, uuid.UUID(eid))
                    self.assertEqual(bulk is None or uuid.UUID(eid) in bulk, expected, eid)


if __name__ == "__main__":
    unittest.main()
