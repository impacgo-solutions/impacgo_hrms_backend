"""QA fixes -- Employees & Organization, roles, exit, N-01/N-02
(H-01, H-05, H-21, M-01..M-05, M-10, M-37, L-05..L-10, N-01, N-02).

    cd backend && venv/Scripts/python -m unittest tests.test_qa_employees_org -v

Real routes (FastAPI TestClient) against the dev DB inside a transaction
that is ALWAYS rolled back. Data a tenant lacks is created inside it.
"""

from __future__ import annotations

import datetime
import io
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException, UploadFile  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app import config, crud, database, deps, main, media_access, models, provision_tenant, role_tiers, schemas, storage  # noqa: E402
from app.rbac_columns import DEFAULT_MATRIX  # noqa: E402
from tests.test_employee_access_lifecycle import _Base  # noqa: E402

HR = "HR / Recruitment Staff"
IC = "Professional / IC Employee"
INTERN = "Associate / Intern"
PASSWORD = "Qa!Strong-Pass-2026"
FRESH_TENANT = os.environ.get("QA_FRESH_TENANT", "qa-chain-approval")  # template role set


class _QABase(_Base):
    tenant = "acme"

    def setUp(self):
        super().setUp()
        mock.patch.object(config.settings, "rate_limit_enabled", False).start()
        self.owner = self._owner()
        self.company_id = self.owner.company_id
        self.actor = self.owner
        main.app.dependency_overrides[database.get_db] = lambda: self.db
        main.app.dependency_overrides[deps.get_current_user] = lambda: self.actor
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)
        self.template_emp = self.db.scalar(select(models.Employee).where(
            models.Employee.company_id == self.company_id, models.Employee.is_active.is_(True),
            models.Employee.branch_id.is_not(None), models.Employee.department_id.is_not(None),
            models.Employee.designation_id.is_not(None)).limit(1))
        if self.template_emp is None:
            self.skipTest("no employee with branch/department/designation")

    # ── fixtures ──
    def as_(self, user):
        self.actor = user
        crud.clear_rbac_memo(self.db)
        return user

    def new_employee(self, **kw) -> models.Employee:
        t = self.template_emp
        fields = dict(
            id=uuid.uuid4(), company_id=self.company_id, branch_id=t.branch_id, department_id=t.department_id,
            designation_id=t.designation_id, employee_code="QA" + uuid.uuid4().hex[:8].upper(),
            first_name="Qa", last_name="Tester",
            work_email=f"qa.{uuid.uuid4().hex[:10]}@example.com", date_of_joining=datetime.date(2022, 1, 3),
            employment_type="Full-time", status="Active", is_active=True)
        fields.update(kw)
        e = models.Employee(**fields)
        self.db.add(e)
        self.db.flush()
        return e

    def login(self, employee: models.Employee, role_name: str) -> models.User:
        role = crud.get_role_by_name(self.db, self.company_id, role_name)
        if role is None:
            self.skipTest(f"no role {role_name!r} in {self.tenant}")
        u = models.User(id=uuid.uuid4(), company_id=self.company_id, email=employee.work_email,
                        password_hash="x", employee_id=employee.id, status="active",
                        full_name=employee.first_name)
        self.db.add(u)
        self.db.flush()
        self.db.add(models.UserRole(user_id=u.id, role_id=role.id))
        self.db.flush()
        return u

    def names(self):
        t = self.template_emp
        return {"branch_name": t.branch.name, "department_name": t.department.name,
                "designation_name": t.designation.name}

    def create_body(self, **over):
        body = {"first_name": "Nova", "last_name": "Hire", "work_email": f"nova.{uuid.uuid4().hex[:8]}@example.com",
                "role_name": IC, "password": PASSWORD, "date_of_joining": datetime.date.today().isoformat(),
                "employment_type": "Full-time", "status": "Active", "work_mode": "Office",
                "date_of_birth": "1995-05-17", **self.names()}
        body.update(over)
        return body


# ═══════════════════════════════════════════════════════════════════════════
class H01FreshTenantHrCanOnboard(_QABase):
    """H-01 on a tenant still carrying the code-template role set."""
    tenant = FRESH_TENANT

    def setUp(self):
        super().setUp()
        self.hr = self.login(self.new_employee(first_name="Hira"), HR)

    def test_tier_rule_self_only_does_not_exceed(self):
        hr_role = crud.get_role_by_name(self.db, self.company_id, HR)
        self.assertEqual(crud.build_role_matrix(hr_role)["timesheet_approval"], "n")
        for name in (IC, INTERN):
            role = crud.get_role_by_name(self.db, self.company_id, name)
            self.assertIsNone(role_tiers.role_assignment_error(self.db, self.hr, role), name)
        # Still blocked above own level (v/e/a compared strictly) and Owner.
        fin = crud.get_role_by_name(self.db, self.company_id, "Finance / Payroll Staff")
        self.assertIn("more access", role_tiers.role_assignment_error(self.db, self.hr, fin))
        owner_role = crud.get_role_by_name(self.db, self.company_id, role_tiers.OWNER_ROLE_NAME)
        self.assertIsNotNone(role_tiers.role_assignment_error(self.db, self.hr, owner_role))

    def test_first_exceeding_column_unit(self):
        self.assertIsNone(role_tiers._first_exceeding_column({"timesheet_approval": "s"}, {}))
        self.assertEqual(role_tiers._first_exceeding_column({"timesheet_approval": "v"}, {}), "timesheet_approval")

    def test_hr_creates_ic_and_intern_via_people(self):
        self.as_(self.hr)
        for name in (IC, INTERN):
            r = self.client.post("/api/employees", json=self.create_body(role_name=name))
            self.assertEqual(r.status_code, 201, r.text)

    def test_hr_hire_step_uses_same_rule(self):
        # The recruitment hire step (recruitment_onboarding.create_employee)
        # calls role_tiers.role_assignment_error with the same arguments.
        import inspect

        from app import recruitment_onboarding
        self.assertIn("role_tiers.role_assignment_error(ctx.db, ctx.user, requested_role)",
                      inspect.getsource(recruitment_onboarding.create_employee))

    def test_n01_template_hr_has_expense_view(self):
        hr_role = crud.get_role_by_name(self.db, self.company_id, HR)
        self.assertEqual(crud.build_role_matrix(hr_role)["travel_expense_approval"], "v")
        self.assertEqual(DEFAULT_MATRIX[HR].split(",")[13], "v")


# ═══════════════════════════════════════════════════════════════════════════
class EmployeeValidation(_QABase):
    def post(self, **over):
        return self.client.post("/api/employees", json=self.create_body(**over))

    def test_m01_rejects_bad_values(self):
        cases = {
            "status": {"status": "Bogus"}, "type": {"employment_type": "slave"}, "mode": {"work_mode": "moon"},
            "neg_ctc": {"annual_ctc": -1}, "huge_ctc": {"annual_ctc": 10 ** 15}, "band": {"band": 999},
            "future": {"date_of_joining": "2099-01-01"}, "dob_after": {"date_of_birth": "2030-01-01"},
            "age11": {"date_of_birth": (datetime.date.today() - datetime.timedelta(days=11 * 365)).isoformat()},
            "email": {"work_email": "not-an-email"}, "email2": {"work_email": "a@b"},
        }
        for label, over in cases.items():
            self.assertEqual(self.post(**over).status_code, 422, label)

    def test_m01_normalises_known_variants(self):
        r = self.post(status="active", employment_type="full_time", work_mode="On-site")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((r.json()["status"], r.json()["employment_type"], r.json()["work_mode"]),
                         ("Active", "Full-time", "Office"))
        for status in ("Terminated", "absconded", "Relieved", "inactive"):  # access_lifecycle statuses
            self.assertEqual(schemas.EmployeeOrgUpdate(status=status).status.lower(), status.lower())

    def test_m01_band_must_exist_in_company(self):
        numbers = {b.band_number for b in crud.list_bands(self.db, self.company_id, active_only=False)}
        missing = next(n for n in range(1, 99) if n not in numbers)
        if numbers:
            self.assertEqual(self.post(band=missing).status_code, 422)

    def test_l08_formats(self):
        for over in ({"bank_ifsc": "HDFC123"}, {"pan": "12345ABCDE"}, {"bank_account_no": "12ab"},
                     {"personal_email": "x@"}):
            self.assertEqual(self.post(**over).status_code, 422, over)
        r = self.post(bank_ifsc="hdfc0001234", pan="abcde1234f", bank_account_no="123456789012")
        self.assertEqual(r.status_code, 201, r.text)
        emp = self.db.get(models.Employee, uuid.UUID(r.json()["id"]))
        self.assertEqual((emp.bank_ifsc, emp.pan), ("HDFC0001234", "ABCDE1234F"))
        target = self.new_employee()
        self.assertEqual(self.client.patch(f"/api/employees/{target.id}/payroll",
                                           json={"bank_ifsc": "BAD"}).status_code, 422)
        self.assertEqual(self.client.post("/api/branches", json={"name": "QA GST " + uuid.uuid4().hex[:6],
                                                                  "tax": "NOTAGSTIN"}).status_code, 422)

    def test_l10_unknown_designation(self):
        typo = "Sofware Enginer " + uuid.uuid4().hex[:6]
        r = self.post(designation_name=typo)
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIsNone(self.db.scalar(select(models.Designation).where(models.Designation.name == typo)))
        self.assertEqual(self.post(designation_name=typo, create_designation=True).status_code, 201)

    def test_m02_invalid_dob_is_422_and_keeps_value(self):
        e = self.new_employee(date_of_birth=datetime.date(1990, 1, 1))
        r = self.client.patch(f"/api/employees/{e.id}/profile", json={"date_of_birth": "31/31/1990"})
        self.assertEqual(r.status_code, 422, r.text)
        self.db.refresh(e)
        self.assertEqual(e.date_of_birth, datetime.date(1990, 1, 1))
        self.assertEqual(self.client.patch(f"/api/employees/{e.id}/org",
                                           json={"confirmation_date": "not a date"}).status_code, 422)

    def test_m03_org_unknown_names(self):
        e = self.new_employee()
        for field in ("department_name", "branch_name", "designation_name"):
            r = self.client.patch(f"/api/employees/{e.id}/org", json={field: "No Such " + uuid.uuid4().hex})
            self.assertEqual(r.status_code, 422, field)
        r = self.client.patch(f"/api/employees/{e.id}/org",
                              json={"department_name": self.template_emp.department.name.upper()})
        self.assertEqual(r.status_code, 200, r.text)

    def test_h21_manager_references(self):
        stranger = uuid.uuid4()
        self.assertEqual(self.post(reporting_manager_id=str(stranger)).status_code, 422)
        exited = self.new_employee(status="Exited")
        self.assertEqual(self.post(reporting_manager_id=str(exited.id)).status_code, 422)
        other_co = self.db.scalar(select(models.Employee.id).where(models.Employee.company_id != self.company_id))
        e = self.new_employee()
        paths = [("patch", f"/api/employees/{e.id}/reporting", {"reporting_manager_id": str(stranger)}),
                 ("post", f"/api/employees/{e.id}/transfer", {"reporting_manager_id": str(stranger)}),
                 ("patch", f"/api/employees/{e.id}/hierarchy", {"branch_manager_id": str(stranger)}),
                 ("patch", f"/api/branches/{self.template_emp.branch_id}", {"branch_manager_id": str(stranger)})]
        if other_co:
            paths.append(("patch", f"/api/employees/{e.id}/reporting", {"reporting_manager_id": str(other_co)}))
        for method, path, body in paths:
            self.assertEqual(getattr(self.client, method)(path, json=body).status_code, 422, path)
        good = self.new_employee()
        self.assertEqual(self.client.patch(f"/api/employees/{e.id}/reporting",
                                           json={"reporting_manager_id": str(good.id)}).status_code, 200)

    def test_h21_foreign_keys_exist(self):
        for schema in ("_template", self.tenant):
            n = self.db.execute(text(
                "SELECT count(*) FROM pg_constraint WHERE contype='f' AND connamespace = CAST(:s AS regnamespace) "
                "AND conname IN ('fk_core_employees_reporting_manager_id','fk_core_employees_dotted_line_manager_id',"
                "'fk_core_branches_branch_manager_id')"), {"s": schema}).scalar()
            self.assertEqual(n, 3, schema)
        e = self.new_employee()
        m = self.new_employee()
        e.reporting_manager_id = m.id
        self.db.flush()
        self.db.delete(m)
        self.db.flush()
        self.db.expire(e)
        self.assertIsNone(e.reporting_manager_id)  # ON DELETE SET NULL

    def test_l09_search_wildcards_literal(self):
        r = self.client.get("/api/employees", params={"search": "%", "limit": 50})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])
        self.new_employee(first_name="Per%cent")
        r = self.client.get("/api/employees", params={"search": "r%c", "limit": 50})
        self.assertEqual([i["name"].split()[0] for i in r.json()["items"]], ["Per%cent"])


# ═══════════════════════════════════════════════════════════════════════════
class OrganizationValidation(_QABase):
    def test_l05_case_insensitive_unique_names(self):
        name = "QA Branch " + uuid.uuid4().hex[:6]
        self.assertEqual(self.client.post("/api/branches", json={"name": name}).status_code, 201)
        self.assertEqual(self.client.post("/api/branches", json={"name": name.upper()}).status_code, 409)
        dname = "QA Dept " + uuid.uuid4().hex[:6]
        r = self.client.post("/api/departments", json={"name": dname})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.client.post("/api/departments", json={"name": dname.lower()}).status_code, 409)
        dep_id = r.json()["id"]
        self.assertEqual(self.client.post(f"/api/departments/{dep_id}/sub-departments",
                                          json={"name": "Alpha"}).status_code, 201)
        self.assertEqual(self.client.post(f"/api/departments/{dep_id}/sub-departments",
                                          json={"name": "ALPHA "}).status_code, 409)

    def test_l06_budget_and_business_unit(self):
        self.assertEqual(self.client.post("/api/departments", json={
            "name": "QA Neg " + uuid.uuid4().hex[:6], "annual_budget": -5}).status_code, 422)
        self.assertEqual(self.client.post("/api/departments", json={
            "name": "QA BU " + uuid.uuid4().hex[:6], "business_unit_name": "Nope " + uuid.uuid4().hex}).status_code, 422)

    def test_m05_band_in_use_by_designation(self):
        r = self.client.post("/api/bands", json={"name": "QA Band " + uuid.uuid4().hex[:4], "code": "Q" + uuid.uuid4().hex[:5]})
        self.assertEqual(r.status_code, 201, r.text)
        band_id = uuid.UUID(r.json()["id"])
        self.template_emp.designation.band_id = band_id
        self.db.flush()
        self.assertEqual(self.client.delete(f"/api/bands/{band_id}").status_code, 409)

    def test_m04_asset_rules(self):
        tag = "QA-" + uuid.uuid4().hex[:8]
        base = {"tag": tag, "type": "Laptop", "model": "X", "status": "available", "purchased": "2024-01-01"}
        self.assertEqual(self.client.post("/api/asset-inventory", json={**base, "value": "-500"}).status_code, 422)
        self.assertEqual(self.client.post("/api/asset-inventory", json={**base, "value": "500",
                                                                        "purchased": "01-2024"}).status_code, 422)
        self.assertEqual(self.client.post("/api/asset-inventory", json={**base, "value": "500"}).status_code, 201)
        e = self.new_employee()
        a = self.client.post("/api/asset-assignments", json={"asset_tag": tag, "employee_id": str(e.id),
                                                             "assigned_on": "2024-06-01"})
        self.assertEqual(a.status_code, 201, a.text)
        aid = a.json()["id"]
        self.assertEqual(self.client.patch(f"/api/asset-assignments/{aid}/return",
                                           json={"returned_on": "2024-05-01"}).status_code, 422)
        self.assertEqual(self.client.patch(f"/api/asset-assignments/{aid}/return",
                                           json={"returned_on": "2024-07-01"}).status_code, 200)
        self.assertEqual(self.client.patch(f"/api/asset-assignments/{aid}/return",
                                           json={"returned_on": "2024-08-01"}).status_code, 409)


# ═══════════════════════════════════════════════════════════════════════════
class UploadContent(unittest.TestCase):
    """L-07 (no DB needed)."""

    def _save(self, name, data):
        return storage.save_uploaded_file(UploadFile(io.BytesIO(data), filename=name),
                                          entity_type="qa_test", entity_id=uuid.uuid4())

    def test_rejects_mismatch_and_empty(self):
        for name, data in (("x.pdf", b"MZ\x90\x00binary"), ("x.png", b"<html><script>1</script></html>"),
                           ("x.pdf", b""), ("x.txt", b"ab\x00cd"), ("x.docx", b"%PDF-1.4")):
            with self.assertRaises(HTTPException, msg=name) as ctx:
                self._save(name, data)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_accepts_real_content(self):
        url, size = self._save("ok.pdf", b"%PDF-1.4\n%test\n")
        self.addCleanup(storage.delete_uploaded_file, url)
        self.assertEqual(size, 15)

    def test_media_nosniff(self):
        self.assertEqual(media_access.MEDIA_RESPONSE_HEADERS["X-Content-Type-Options"], "nosniff")


# ═══════════════════════════════════════════════════════════════════════════
class HrScopeAndExit(_QABase):
    def setUp(self):
        super().setUp()
        self.hr = self.login(self.new_employee(first_name="Hari"), HR)

    def test_m10_hr_without_rep_falls_back_to_people_scope(self):
        self.db.execute(text("UPDATE core_departments SET hr_representative_id = NULL "
                             "WHERE hr_representative_id = :e"), {"e": self.hr.employee_id})
        self.assertIsNone(crud.get_visible_employee_ids_for_docs(self.db, self.hr))

    def test_m10_hr_rep_sees_own_and_unrepresented(self):
        depts = self.db.scalars(select(models.Department).where(
            models.Department.company_id == self.company_id)).all()
        if len(depts) < 2:
            self.skipTest("needs two departments")
        mine, other = depts[0], depts[1]
        mine.hr_representative_id = self.hr.employee_id
        other.hr_representative_id = self.owner.employee_id
        for d in depts[2:]:
            d.hr_representative_id = None
        self.db.flush()
        in_mine = self.new_employee(department_id=mine.id)
        in_other = self.new_employee(department_id=other.id)
        in_free = self.new_employee(department_id=depts[2].id) if len(depts) > 2 else None
        crud.clear_rbac_memo(self.db)
        visible = set(crud.get_visible_employee_ids_for_docs(self.db, self.hr))
        self.assertIn(in_mine.id, visible)
        self.assertNotIn(in_other.id, visible)
        if in_free is not None:
            self.assertIn(in_free.id, visible)

    def _exit(self, employee, **over):
        body = {"employee_id": str(employee.id), "resignation_date": "2026-09-01",
                "last_working_day": "2026-10-31", "reason": "QA"}
        body.update(over)
        return self.client.post("/api/employees/exit-requests", json=body)

    def test_m37_exit_date_rules_and_duplicates(self):
        # (the API rolls back its session on a 4xx, so the happy path runs first)
        manager = self.new_employee()
        e = self.new_employee(reporting_manager_id=manager.id)
        self.assertEqual(self._exit(e, last_working_day="2026-08-01").status_code, 422)  # schema-level
        self.assertEqual(self._exit(e).status_code, 201)
        self.assertEqual(self._exit(e).status_code, 409)
        manager2 = self.new_employee()
        e2 = self.new_employee(reporting_manager_id=manager2.id)
        self.assertEqual(self._exit(e2, resignation_date="2021-01-01", last_working_day="2021-02-01").status_code, 422)

    def test_h05_exit_records_scoped(self):
        manager = self.new_employee()
        someone = self.new_employee(reporting_manager_id=manager.id)
        r = self._exit(someone)
        self.assertEqual(r.status_code, 201, r.text)
        exit_id = r.json()["id"]
        outsider = self.login(self.new_employee(), IC)
        self.as_(outsider)
        rows = self.client.get("/api/employees/exit-requests/records", params={"limit": 500}).json()
        self.assertNotIn(exit_id, {x["id"] for x in rows})
        self.as_(self.login(manager, IC))
        rows = self.client.get("/api/employees/exit-requests/records", params={"limit": 500}).json()
        self.assertIn(exit_id, {x["id"] for x in rows})

    def test_m10_exit_letter_outside_scope_is_403(self):
        depts = self.db.scalars(select(models.Department).where(
            models.Department.company_id == self.company_id)).all()
        if len(depts) < 2:
            self.skipTest("needs two departments")
        depts[0].hr_representative_id = self.hr.employee_id
        for d in depts[1:]:
            d.hr_representative_id = self.owner.employee_id
        manager = self.new_employee()
        e = self.new_employee(department_id=depts[1].id, reporting_manager_id=manager.id)
        r = self._exit(e)
        self.assertEqual(r.status_code, 201, r.text)
        self.as_(self.hr)
        resp = self.client.get(f"/api/exit-requests/{r.json()['id']}/letters")
        self.assertEqual(resp.status_code, 403, resp.text)


# ═══════════════════════════════════════════════════════════════════════════
class TenantDefaults(_QABase):
    def test_n02_exit_letter_templates_seeded(self):
        self.assertIn("hcm_exit_letter_html_templates", provision_tenant.CONFIG_TEMPLATE_TABLES)
        for schema in ("_template", self.tenant, FRESH_TENANT):
            types = set(self.db.execute(text(
                f'SELECT letter_type FROM "{schema}".hcm_exit_letter_html_templates WHERE is_active')).scalars())
            self.assertEqual(types, {"experience_relieving", "fnf_statement"}, schema)

    def test_n01_impacgo_solutions_hr_untouched(self):
        level = self.db.execute(text(
            'SELECT p.action FROM "impacgo-solutions".core_roles r '
            'JOIN "impacgo-solutions".core_role_permissions rp ON rp.role_id = r.id '
            'JOIN "impacgo-solutions".core_permissions p ON p.id = rp.permission_id '
            "WHERE r.name = :n AND p.resource = 'travel_expense_approval' AND p.action IN ('v','e','a')"),
            {"n": HR}).scalars().all()
        self.assertEqual(level, ["a"])


if __name__ == "__main__":
    unittest.main()
