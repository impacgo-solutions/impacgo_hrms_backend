"""Reports & Analytics access per role / permission / reporting line
(crud.report_access, deps.require_report, GET /api/reports/access).

    cd backend && venv/Scripts/python -m unittest tests.test_report_access -v

Real users of each role in tenant acme (PeopleTestBase) over the HTTP API,
inside a transaction that is ALWAYS rolled back.
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

from sqlalchemy import select  # noqa: E402

from app import crud, models, report_pdf_renderer  # noqa: E402
from tests.test_people_visibility import PeopleTestBase  # noqa: E402

TODAY = datetime.date.today()
RANGE = {"from_date": (TODAY - datetime.timedelta(days=60)).isoformat(), "to_date": TODAY.isoformat()}
PERSONAL = ["attendance", "leave", "regularization", "work-entry", "timesheet", "utilization"]
REQUEST_REPORTS = ["overtime", "leave-withdrawals", "compliance"]
RESTRICTED = ["people", "payroll", "project", "performance", "recruitment"]


class ReportAccessTests(PeopleTestBase):
    # -- helpers --
    def get(self, path, expect=200, **params):
        r = self.client.get(path, params={**RANGE, **params})
        self.assertEqual(r.status_code, expect, f"GET {path} as {self.actor.email}: {r.status_code} {r.text[:300]}")
        return r.json()

    def access(self):
        return crud.report_access(self.db, self.actor)

    def names_of(self, ids) -> set[str]:
        return {f"{e.first_name} {e.last_name or ''}".strip() for e in self.db.scalars(
            select(models.Employee).where(models.Employee.id.in_(list(ids))))}

    def employee_rows(self, report, **params):
        """Employee names in a ReportOut report (first column)."""
        return {row[0] for row in self.get(f"/api/reports/{report}", **params)["rows"]}

    def set_reports_level(self, user, level):
        self.db.add(models.EmployeePermission(company_id=user.company_id, employee_id=user.employee_id,
                                              module="reports", level=level))
        self.db.flush()
        crud.clear_rbac_memo(self.db)

    def other_employee(self, outside):
        return self.db.scalar(select(models.Employee).where(
            models.Employee.company_id == self.company_id, models.Employee.is_active.is_(True),
            models.Employee.id.notin_(list(outside))).limit(1))

    # -- employees without the Reports permission --
    def test_employee_gets_personal_reports_on_own_data_only(self):
        me = self.as_("no_people")
        acc = self.access()
        self.assertIn(acc.scope, ("self", "team"))
        self.assertIn(me.employee_id, acc.employee_ids)
        info = self.get("/api/reports/access")
        self.assertEqual(sorted(info["report_types"]), sorted(crud.REPORT_TYPES_PERSONAL))
        self.assertEqual(info["employee_count"], len(acc.employee_ids))

        allowed = self.names_of(acc.employee_ids)
        for report in ("attendance", "utilization"):
            with self.subTest(report=report):
                self.assertTrue(self.employee_rows(report) <= allowed)
        for report in PERSONAL + REQUEST_REPORTS:
            with self.subTest(report=report):
                self.get(f"/api/reports/{report}")
        for report in RESTRICTED:
            with self.subTest(report=report):
                self.get(f"/api/reports/{report}", expect=403)

    def test_employee_cannot_filter_their_way_into_someone_else(self):
        self.as_("no_people")
        acc = self.access()
        other = self.other_employee(acc.employee_ids)
        if other is None:
            self.skipTest("no employee outside the caller's scope")
        rows = self.get("/api/reports/attendance", employee_id=str(other.id))["rows"]
        self.assertEqual(rows, [])
        for report in REQUEST_REPORTS:
            body = self.get(f"/api/reports/{report}", employee_id=str(other.id))
            self.assertEqual(body.get("rows", []), [], report)

    def test_pdf_export_follows_the_same_rule(self):
        self.as_("no_people")
        payload = {"title": "X", "report_type": "people", "params": RANGE}
        self.assertEqual(self.client.post("/api/reports/export/pdf", json=payload).status_code, 403)
        captured = {}

        def fake_render(doc, company, generated_by):
            captured.update(doc)
            return b"%PDF-1.4 fake"

        with mock.patch.object(report_pdf_renderer, "render_report_pdf", side_effect=fake_render):
            r = self.client.post("/api/reports/export/pdf", json={**payload, "report_type": "attendance"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(captured["rows"], self.get("/api/reports/attendance")["rows"])

    # -- admin-set Employee Permissions on Reports --
    def test_employee_permission_no_access_and_self_only(self):
        mgr = self.as_("manager")
        self.set_reports_level(mgr, "self")
        acc = self.access()
        self.assertEqual((acc.scope, acc.employee_ids), ("self", [mgr.employee_id]))
        self.assertEqual(self.employee_rows("attendance"), self.names_of([mgr.employee_id]))
        self.get("/api/reports/people", expect=403)

        emp = self.as_("no_people")
        self.set_reports_level(emp, "none")
        self.assertEqual(self.get("/api/reports/access")["report_types"], [])
        for report in ("attendance", "overtime", "compliance"):
            self.get(f"/api/reports/{report}", expect=403)

    def test_employee_permission_view_opens_everything(self):
        emp = self.as_("no_people")
        self.set_reports_level(emp, "view")
        acc = self.access()
        self.assertEqual((acc.scope, acc.employee_ids), ("org", None))
        self.get("/api/reports/people")

    # -- roles holding the Reports permission --
    def test_manager_sees_their_reporting_line(self):
        mgr = self.as_("manager")
        acc = self.access()
        self.assertEqual(acc.scope, "team")
        expected = {mgr.employee_id, *crud._reporting_subtree_ids(self.db, mgr.employee_id, mgr.company_id)}
        self.assertEqual(set(acc.employee_ids), expected)
        self.assertTrue(self.employee_rows("attendance") <= self.names_of(expected))
        self.get("/api/reports/people")

    def test_owner_and_hr_see_the_whole_company(self):
        for key in ("owner", "hr"):
            with self.subTest(role=key):
                self.as_(key)
                acc = self.access()
                self.assertEqual((acc.scope, acc.employee_ids), ("org", None))
                self.assertEqual(sorted(acc.report_types), sorted(crud.REPORT_TYPES_ALL))

    def _as_role(self, user, role_name):
        """Treat [user] as holding [role_name] with the Reports permission."""
        fake = SimpleNamespace(name=role_name, id=uuid.uuid4())
        return [
            mock.patch.object(crud, "get_user_primary_role", return_value=fake),
            mock.patch.object(crud, "permission_satisfied", return_value=True),
        ]

    def test_branch_manager_sees_their_branch(self):
        user = self.users["manager"]
        branch = self.db.scalar(select(models.Branch).where(models.Branch.company_id == user.company_id).limit(1))
        if branch is None:
            self.skipTest("no branch")
        self.db.execute(models.Branch.__table__.update().where(
            models.Branch.company_id == user.company_id, models.Branch.branch_manager_id == user.employee_id
        ).values(branch_manager_id=None))
        branch.branch_manager_id = user.employee_id
        self.db.flush()
        in_branch = set(self.db.scalars(select(models.Employee.id).where(
            models.Employee.company_id == user.company_id, models.Employee.branch_id == branch.id)).all())
        own = {user.employee_id, *crud._reporting_subtree_ids(self.db, user.employee_id, user.company_id)}
        patches = self._as_role(user, "Branch Manager")
        for p in patches:
            p.start()
        try:
            acc = crud.report_access(self.db, user)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(acc.scope, "branch")
        self.assertEqual(set(acc.employee_ids), in_branch | own)

    def test_project_manager_sees_their_project_members(self):
        user = self.users["manager"]
        member = self.other_employee({user.employee_id})
        outsider = self.other_employee({user.employee_id, member.id})
        project = models.Project(id=uuid.uuid4(), company_id=user.company_id, code=f"RA{uuid.uuid4().hex[:6]}",
                                 name="Report access QA", project_manager_id=user.employee_id)
        self.db.add(project)
        self.db.flush()
        self.db.add(models.ProjectAllocation(id=uuid.uuid4(), project_id=project.id, employee_id=member.id,
                                             allocation_pct=50, start_date=TODAY, is_active=True))
        self.db.flush()
        patches = self._as_role(user, "Project Manager")
        for p in patches:
            p.start()
        try:
            acc = crud.report_access(self.db, user)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(acc.scope, "project")
        self.assertIn(member.id, acc.employee_ids)
        self.assertIn(user.employee_id, acc.employee_ids)
        own = {user.employee_id, *crud._reporting_subtree_ids(self.db, user.employee_id, user.company_id)}
        others_projects = set(self.db.scalars(select(models.ProjectAllocation.employee_id).join(
            models.Project, models.Project.id == models.ProjectAllocation.project_id).where(
            models.Project.project_manager_id == user.employee_id, models.ProjectAllocation.is_active.is_(True))))
        if outsider.id not in own | others_projects:
            self.assertNotIn(outsider.id, acc.employee_ids)

    def test_scope_never_leaves_the_callers_company(self):
        for key in ("no_people", "manager", "hr", "owner"):
            with self.subTest(role=key):
                user = self.as_(key)
                acc = self.access()
                if acc.employee_ids is None:
                    continue
                companies = set(self.db.scalars(select(models.Employee.company_id).where(
                    models.Employee.id.in_(acc.employee_ids))).all())
                self.assertEqual(companies, {user.company_id})


if __name__ == "__main__":
    unittest.main()
