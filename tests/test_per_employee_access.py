"""Per-employee endpoints (/api/employees/{employee_id}/...: personal,
overview, attendance, leave, documents, payroll, ... and every HR action on
one employee) only serve employees within the caller's visibility: a
Manager their reporting subtree, an Employee themselves, org-wide scopes
anyone in their company -- never another company or tenant. Routes are
discovered from the running app, so a new per-employee route without the
check fails here.

    cd backend && venv/Scripts/python -m unittest tests.test_per_employee_access -v

Direct HTTP requests (FastAPI TestClient) as real users of each role in
tenant acme, inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.routing import APIRoute  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app import crud, main, models  # noqa: E402
from tests.test_people_visibility import OTHER_TENANT, PeopleTestBase  # noqa: E402

# Personal data in each GET tab's response that must never leak.
PERSONAL_KEYS = ("dob", "currentAddr", "permAddr", "personalPhone", "personalEmail", "emergencyName",
                 "emergencyPhone", "date_of_birth", "current_address", "personal_phone")


def _employee_routes():
    out = []
    for r in main.app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api/employees/{employee_id}"):
            for method in sorted(r.methods - {"HEAD"}):
                out.append((method, r.path))
    return out


GET_ROUTES = [p for m, p in _employee_routes() if m == "GET"]
WRITE_ROUTES = [(m, p) for m, p in _employee_routes() if m != "GET"]
OTHER_PER_EMPLOYEE_GETS = ["/api/employees/{employee_id}/exit-letters",
                           "/api/employees/{employee_id}/salary-structure"]


class PerEmployeeAccessTests(PeopleTestBase):
    def setUp(self):
        super().setUp()
        # Hundreds of requests in seconds: the API's rate limiter (a separate
        # protection, unchanged) would answer 429 instead of the auth result.
        from app.config import settings
        p = mock.patch.object(settings, "rate_limit_enabled", False)
        p.start()
        self.addCleanup(p.stop)

    def url(self, path, employee_id):
        return path.replace("{employee_id}", str(employee_id)).replace(
            "{document_id}", "00000000-0000-0000-0000-000000000000")

    def call(self, method, path, employee_id):
        url = self.url(path, employee_id)
        if method == "GET":
            return self.client.get(url)
        return self.client.request(method, url, json={})

    def outsider_and_member(self, me):
        allowed = self.subtree_and_self(me)
        outsider = next(i for i in self.company_ids if i not in allowed)
        members = [i for i in allowed if i != str(me.employee_id)]
        return outsider, (members[0] if members else None)

    def assert_no_personal_data(self, resp):
        if resp.status_code == 200:
            body = resp.text
            for key in PERSONAL_KEYS:
                self.assertNotIn(f'"{key}"', body)

    # ── Manager ──
    def test_manager_every_get_route_refuses_outsiders(self):
        me = self.as_("manager")
        outsider, _ = self.outsider_and_member(me)
        self.assertGreaterEqual(len(GET_ROUTES), 14)
        for path in GET_ROUTES:
            with self.subTest(path):
                resp = self.client.get(self.url(path, outsider))
                self.assertIn(resp.status_code, (403, 404), f"{path} served an outsider: {resp.text[:200]}")

    def test_manager_reaches_team_and_self(self):
        me = self.as_("manager")
        _, member = self.outsider_and_member(me)
        for target in (member, me.employee_id):
            for tab in ("personal", "overview", "professional", "attendance", "leave"):
                with self.subTest(target=str(target)[:8], tab=tab):
                    resp = self.client.get(f"/api/employees/{target}/{tab}")
                    self.assertEqual(resp.status_code, 200, resp.text[:200])

    def test_manager_branch_of_edit_routes_still_works(self):
        """Profile edits: a manager may edit their report (manager branch),
        never an outsider."""
        me = self.as_("manager")
        outsider, member = self.outsider_and_member(me)
        self.assertEqual(self.client.patch(f"/api/employees/{outsider}/profile", json={}).status_code, 403)
        self.assertNotEqual(self.client.patch(f"/api/employees/{member}/profile", json={}).status_code, 403)

    def test_scoped_user_with_people_edit_cannot_act_outside_scope(self):
        """Even WITH People edit, a scoped caller's HR actions stay within
        their visibility (every write route on one employee)."""
        me = self.as_("manager")
        outsider, member = self.outsider_and_member(me)
        with mock.patch.object(crud, "can_access_people_module", lambda *a, **k: True):
            for method, path in WRITE_ROUTES:
                with self.subTest(f"{method} {path}"):
                    resp = self.call(method, path, outsider)
                    self.assertEqual(resp.status_code, 403, f"{method} {path}: {resp.text[:200]}")
            # ...while the same routes pass the gate for their own report.
            resp = self.client.patch(f"/api/employees/{member}/org", json={})
            self.assertNotEqual(resp.status_code, 403, resp.text[:200])

    # ── Employee ──
    def test_employee_only_self(self):
        me = self.as_("employee")
        other = next(i for i in self.company_ids if i != str(me.employee_id))
        for path in GET_ROUTES:
            with self.subTest(path):
                resp = self.client.get(self.url(path, other))
                self.assertIn(resp.status_code, (403, 404))
        for tab in ("personal", "overview", "attendance", "leave"):
            with self.subTest(self_tab=tab):
                self.assertEqual(self.client.get(f"/api/employees/{me.employee_id}/{tab}").status_code, 200)

    def test_self_access_without_people_module(self):
        """Own record regardless of hierarchy or People access."""
        me = self.as_("no_people")
        for tab in ("personal", "overview", "professional", "attendance", "leave"):
            with self.subTest(tab):
                self.assertEqual(self.client.get(f"/api/employees/{me.employee_id}/{tab}").status_code, 200)
        other = next(i for i in self.company_ids if i != str(me.employee_id))
        self.assertEqual(self.client.get(f"/api/employees/{other}/personal").status_code, 403)

    # ── Org-wide ──
    def test_org_wide_roles_reach_anyone_in_company(self):
        for key in ("hr", "payroll", "owner"):
            with self.subTest(key):
                me = self.as_(key)
                target = next(i for i in self.company_ids if i != str(me.employee_id))
                for tab in ("personal", "overview", "attendance", "leave"):
                    self.assertEqual(self.client.get(f"/api/employees/{target}/{tab}").status_code, 200, tab)

    def test_org_wide_view_scope_without_pay(self):
        """General Manager / IT Admin: org-wide by their assigned scope,
        but still no one else's pay."""
        user = next((u for u in self.db.scalars(select(models.User).where(models.User.status == "active"))
                     if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                     and r.name in ("General Manager / Sr. Manager", "IT / System Admin")
                     and u.employee_id is not None), None)
        if user is None:
            self.skipTest("no org-wide non-HR user")
        self.users["orgwide"] = user
        me = self.as_("orgwide")
        self.assertIsNone(crud.get_people_directory_visible_ids(self.db, me))
        target = next(i for i in self.company_ids if i != str(me.employee_id))
        self.assertEqual(self.client.get(f"/api/employees/{target}/personal").status_code, 200)
        self.assertEqual(self.client.get(f"/api/employees/{target}/payroll").status_code, 403)
        self.assertEqual(self.client.get(f"/api/employees/{target}/overview").json()["ctc"], 0)

    # ── Cross-company / cross-tenant ──
    def test_foreign_ids_never_served(self):
        foreign_tenant = self.db.execute(text(f'SELECT id FROM "{OTHER_TENANT}".core_employees LIMIT 1')).scalar()
        other_company = self.db.scalar(select(models.Employee.id).where(
            models.Employee.company_id != self.users["hr"].company_id).limit(1))
        for key in ("owner", "hr", "payroll", "manager", "employee"):
            me = self.as_(key)
            for foreign in (foreign_tenant, other_company):
                if foreign is None or str(foreign) in self.ids_of(me):
                    continue
                for path in GET_ROUTES + OTHER_PER_EMPLOYEE_GETS:
                    with self.subTest(role=key, path=path, id=str(foreign)[:8]):
                        resp = self.client.get(self.url(path, foreign))
                        self.assertIn(resp.status_code, (403, 404), resp.text[:200])
                for method, path in WRITE_ROUTES:
                    with self.subTest(role=key, write=f"{method} {path}"):
                        self.assertIn(self.call(method, path, foreign).status_code, (403, 404, 422))

    def test_salary_structure_is_company_scoped(self):
        me = self.as_("payroll")
        other_company = self.db.scalar(select(models.Employee.id).where(
            models.Employee.company_id != me.company_id).limit(1))
        if other_company is None:
            self.skipTest("single-company tenant")
        resp = self.client.get(f"/api/employees/{other_company}/salary-structure")
        self.assertEqual(resp.status_code, 404, resp.text[:200])


if __name__ == "__main__":
    unittest.main()
