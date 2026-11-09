"""Optional fields: records save with only their essential fields, blank
values for optional fields never error, essential fields and format checks
still reject.

    cd backend && venv/Scripts/python -m unittest tests.test_optional_fields -v

Real routes (FastAPI TestClient) against the dev DB inside a transaction
that is ALWAYS rolled back (same base as tests.test_qa_employees_org).
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402

from app import crud, models, schemas  # noqa: E402
from tests.test_qa_employees_org import IC, PASSWORD, _QABase  # noqa: E402


def _minimal_employee(**over):
    body = {"first_name": "Opt", "work_email": f"opt.{uuid.uuid4().hex[:8]}@example.com",
            "role_name": IC, "password": PASSWORD}
    body.update(over)
    return body


class EmployeeCreateOptional(_QABase):
    def post(self, **over):
        return self.client.post("/api/employees", json=_minimal_employee(**over))

    def test_only_essential_fields(self):
        r = self.post()
        self.assertEqual(r.status_code, 201, r.text)
        out = r.json()
        self.assertEqual((out["status"], out["employment_type"]), ("Active", "Full-time"))
        self.assertEqual(out["date_of_joining"], datetime.date.today().isoformat())
        self.assertIsNone(out["branch_name"])
        self.assertIsNone(out["department_name"])
        self.assertIsNone(out["designation_name"])
        emp_id = out["id"]
        # Downstream readers cope with the missing org fields.
        for path in ("overview", "personal", "professional", "payroll", "hierarchy"):
            g = self.client.get(f"/api/employees/{emp_id}/{path}")
            self.assertEqual(g.status_code, 200, f"{path}: {g.text}")
        listing = self.client.get("/api/employees", params={"limit": 200})
        self.assertEqual(listing.status_code, 200, listing.text)

    def test_blank_optional_values_are_accepted(self):
        r = self.post(branch_name="", department_name="", designation_name="", date_of_joining="",
                      status="", employment_type="", date_of_birth="", personal_email="", pan="",
                      bank_ifsc="", work_mode="", band=0, annual_ctc=None, reporting_manager_id="",
                      employee_code="", gender="", marital_status="")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["status"], "Active")
        self.assertIsNone(r.json()["band"])

    def test_essential_fields_still_required(self):
        for field in ("first_name", "work_email", "role_name", "password"):
            body = _minimal_employee()
            body.pop(field)
            self.assertEqual(self.client.post("/api/employees", json=body).status_code, 422, field)
        self.assertEqual(self.post(first_name="").status_code, 422)
        self.assertEqual(self.post(work_email="").status_code, 422)

    def test_format_checks_still_apply_to_given_values(self):
        for over in ({"personal_email": "x@"}, {"annual_ctc": -1}, {"pan": "12345ABCDE"},
                     {"status": "Bogus"}, {"date_of_joining": "2099-01-01"}, {"band": 999}):
            self.assertEqual(self.post(**over).status_code, 422, over)
        self.assertEqual(self.post(branch_name="No Such Branch " + uuid.uuid4().hex).status_code, 409)

    def test_contract_still_needs_its_dates(self):
        self.assertEqual(self.post(employment_type="Contract").status_code, 422)

    def test_admin_configured_mandatory_field_still_enforced(self):
        crud.upsert_field_rules(self.db, self.company_id, "employee",
                                [{"field_key": "branch_name", "state": "mandatory", "role_id": None}],
                                self.owner.id)
        r = self.post()
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("branch_name", r.text)
        self.assertEqual(self.post(**{"branch_name": self.template_emp.branch.name}).status_code, 201)

    def test_built_in_field_rules_only_require_login_identity(self):
        mandatory = {k for k, v in crud.DEFAULT_FIELD_RULES["employee"].items() if v == "mandatory"}
        self.assertEqual(mandatory, {"first_name", "work_email", "role_name", "password"})


class OtherEntitiesOptional(_QABase):
    def test_project_with_only_a_name(self):
        r = self.client.post("/api/projects", json={"name": "Opt Project " + uuid.uuid4().hex[:6]})
        self.assertEqual(r.status_code, 201, r.text)
        r2 = self.client.post("/api/projects", json={"name": "Opt Project " + uuid.uuid4().hex[:6], "client": "",
                                                     "type": "—", "pm": "", "team_lead_id": None, "start": "",
                                                     "end": None, "budget": "—"})
        self.assertEqual(r2.status_code, 201, r2.text)
        self.assertEqual(self.client.post("/api/projects", json={"name": ""}).status_code, 422)
        self.assertEqual(self.client.post("/api/projects", json={"name": "Bad " + uuid.uuid4().hex[:6],
                                                                 "start": "2026-05-01", "end": "2026-04-01"}).status_code, 400)

    def test_asset_with_tag_and_type_only(self):
        tag = "OPT-" + uuid.uuid4().hex[:8]
        r = self.client.post("/api/asset-inventory", json={"tag": tag, "type": "Laptop", "category": "office",
                                                           "model": "", "value": "", "purchased": None, "status": None})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((r.json()["value"], r.json()["purchased"]), ("—", "—"))
        self.assertEqual((r.json()["category"], r.json()["category_key"]), ("Office", "office"))
        e = self.new_employee()
        a = self.client.post("/api/asset-assignments", json={"asset_tag": tag, "employee_id": str(e.id)})
        self.assertEqual(a.status_code, 201, a.text)
        self.assertEqual(a.json()["assigned_on"], crud.company_today(self.db, self.company_id).isoformat())
        ret = self.client.patch(f"/api/asset-assignments/{a.json()['id']}/return", json={})
        self.assertEqual(ret.status_code, 200, ret.text)
        self.assertEqual(self.client.post("/api/asset-inventory", json={"tag": "OPT-" + uuid.uuid4().hex[:8],
                                                                        "type": "Laptop", "category": "office",
                                                                        "value": "-5"}).status_code, 422)

    def test_asset_category_optional_single_choice(self):
        def post(**extra):
            return self.client.post("/api/asset-inventory",
                                    json={"tag": "CAT-" + uuid.uuid4().hex[:8], "type": "Laptop", **extra})
        for missing in (post(), post(category=""), post(category=None)):  # optional now
            self.assertEqual(missing.status_code, 201, missing.text)
            self.assertEqual((missing.json()["category"], missing.json()["category_key"]), ("—", None))
        self.assertEqual(post(category="Vendor").status_code, 422)      # a given one must be an option
        self.assertEqual(post(category=["office", "client"]).status_code, 422)  # only one allowed
        for sent, label, key in (("personal", "Personal", "personal"), ("Office", "Office", "office"),
                                 ("CLIENT", "Client", "client")):
            r = post(category=sent)
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual((r.json()["category"], r.json()["category_key"]), (label, key))
            row = crud.get_asset_inventory_item_by_tag(self.db, self.company_id, r.json()["tag"])
            self.assertEqual(row.asset_category, key)                    # stored
            listed = next(a for a in self.client.get("/api/asset-inventory?limit=500").json()
                          if a["tag"] == r.json()["tag"])
            self.assertEqual(listed["category"], label)                 # listing

    def test_asset_with_no_fields_at_all(self):
        r = self.client.post("/api/asset-inventory", json={})
        self.assertEqual(r.status_code, 201, r.text)
        b = r.json()
        self.assertTrue(b["tag"])                                       # generated from the Asset ID format
        self.assertEqual((b["type"], b["category_key"], b["status_key"], b["name"], b["serial_number"],
                          b["description"], b["value"], b["purchased"]),
                         ("", None, "available", None, None, None, "—", "—"))
        listed = next(a for a in self.client.get("/api/asset-inventory?limit=500").json() if a["tag"] == b["tag"])
        self.assertEqual(listed["type"], "")

    def test_asset_name_serial_description_stored_listed_and_edited(self):
        r = self.client.post("/api/asset-inventory", json={
            "type": "Laptop", "name": "  Design   laptop ", "serial_number": " C02XG0FDH7JY ",
            "description": "Line one\n  Line two  \n"})
        self.assertEqual(r.status_code, 201, r.text)
        b = r.json()
        self.assertEqual((b["name"], b["serial_number"], b["description"]),
                         ("Design laptop", "C02XG0FDH7JY", "Line one\n  Line two"))
        row = crud.get_asset_inventory_item_by_tag(self.db, self.company_id, b["tag"])
        self.assertEqual((row.asset_name, row.serial_number, row.description),
                         ("Design laptop", "C02XG0FDH7JY", "Line one\n  Line two"))
        self.assertEqual(self.client.post("/api/asset-inventory", json={"name": "x" * 201}).status_code, 422)
        # edit: only the fields sent change; blank clears; nothing required
        e = self.client.patch(f"/api/asset-inventory/{b['id']}", json={"name": "Renamed", "serial_number": ""})
        self.assertEqual(e.status_code, 200, e.text)
        self.assertEqual((e.json()["name"], e.json()["serial_number"], e.json()["description"], e.json()["type"]),
                         ("Renamed", None, "Line one\n  Line two", "Laptop"))
        e2 = self.client.patch(f"/api/asset-inventory/{b['id']}",
                               json={"type": "", "category": "client", "value": "45000", "purchased": "2025-01-01"})
        self.assertEqual(e2.status_code, 200, e2.text)
        self.assertEqual((e2.json()["type"], e2.json()["category_key"], e2.json()["purchased"]),
                         ("", "client", "2025-01-01"))
        self.assertEqual(self.client.patch(f"/api/asset-inventory/{b['id']}", json={"value": "-3"}).status_code, 422)
        self.assertEqual(self.client.patch(f"/api/asset-inventory/{b['id']}", json={"category": "Vendor"}).status_code, 422)
        self.assertEqual(self.client.patch(f"/api/asset-inventory/{b['id']}", json={}).status_code, 200)
        self.assertEqual(self.client.patch(f"/api/asset-inventory/{uuid.uuid4()}", json={"name": "x"}).status_code, 404)
        self.assertEqual(crud.get_asset_inventory_item_by_tag(self.db, self.company_id, b["tag"]).asset_tag, b["tag"])

    def test_asset_request_without_type(self):
        e = self.new_employee()
        r = self.client.post("/api/asset-requests", json={"employee_id": str(e.id)})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["asset_type"], "")

    def test_simple_masters_with_only_their_name(self):
        suffix = uuid.uuid4().hex[:6]
        for path, body in (
            ("/api/designations", {"name": f"Opt Designation {suffix}"}),
            ("/api/courses", {"name": f"Opt Course {suffix}", "type": "", "duration": "", "category": None}),
            ("/api/okrs", {"title": f"Opt Goal {suffix}"}),
            ("/api/review-cycles", {"name": f"Opt Cycle {suffix}"}),
            ("/api/job-openings", {"title": f"Opt Opening {suffix}", "department_name": ""}),
            ("/api/benefit-categories", {"name": f"Opt Plan {suffix}", "items": []}),
        ):
            r = self.client.post(path, json=body)
            self.assertEqual(r.status_code, 201, f"{path}: {r.text}")
        self.assertEqual(self.client.post("/api/courses", json={"name": "x", "duration": "-3 hrs"}).status_code, 422)

    def test_sprint_and_release_without_dates(self):
        project = self.db.query(models.Project).filter(models.Project.company_id == self.company_id).first()
        if project is None:
            self.skipTest("no project")
        s = self.client.post("/api/sprints", json={"project_name": project.name, "name": "Opt " + uuid.uuid4().hex[:6]})
        self.assertEqual(s.status_code, 201, s.text)
        self.assertIsNone(s.json()["start_date"])
        r = self.client.post("/api/releases", json={"project_name": project.name, "name": "Opt " + uuid.uuid4().hex[:6]})
        self.assertEqual(r.status_code, 201, r.text)


class SchemaLevelOptional(unittest.TestCase):
    EMP = uuid.uuid4()

    def test_blank_and_null_optional_values(self):
        self.assertEqual(schemas.RegularizationCreate(employee_id=self.EMP, attendance_date="2026-10-01",
                                                      reason=None).reason, "")
        self.assertIsNone(schemas.SalaryRevisionRequestCreate(employee_id=self.EMP, proposed_ctc=10, reason="").reason
                          or None)
        loan = schemas.LoanCreate(employee_id=self.EMP, loan_type=None, principal_amount=500)
        self.assertEqual((loan.loan_type, loan.outstanding_balance), ("General", 500))
        self.assertEqual(schemas.ReimbursementCreate(employee_id=self.EMP, category="", amount=5).category, "General")
        self.assertIsNone(schemas.WorkEntryCreate(employee_id=self.EMP, entry_date="2026-10-01", project_id=None,
                                                  task="", category="", hours=2).category or None)
        self.assertIsNone(schemas.DesignationCreate(name="x", band="").band)
        self.assertIsNone(schemas.BranchUpdate(name="").name)
        self.assertIsNone(schemas.ShiftAssignmentCreate(employee_id=self.EMP, from_date="").from_date)
        self.assertEqual(schemas.TaskBoardCardCreate(project_name="p", title="t", column_key="").column_key, "Backlog")
        self.assertIsNone(schemas.ExpenseLineItemCreate(date="2026-10-01", category=None, description="",
                                                        amount=10).category)
        self.assertEqual(schemas.ReviewCycleCreate(name="c", from_date="2026-04-01").to_date,
                         datetime.date(2027, 3, 31))
        self.assertIsNone(schemas.EmployeeOrgUpdate(band=0).band)

    def test_essential_fields_still_rejected(self):
        bad = [
            lambda: schemas.RegularizationCreate(employee_id=self.EMP),                       # no date
            lambda: schemas.LeaveRequestCreate(employee_id=self.EMP, leave_type_name="CL"),    # no dates
            lambda: schemas.LeaveRequestCreate(employee_id=self.EMP, from_date="2026-10-02",
                                               to_date="2026-10-01", leave_type_name="CL"),   # order
            lambda: schemas.WorkEntryCreate(employee_id=self.EMP, entry_date="2026-10-01"),   # no hours
            lambda: schemas.WorkEntryCreate(employee_id=self.EMP, entry_date="2026-10-01", hours=25),
            lambda: schemas.ProjectCreate(name=""),
            lambda: schemas.ReimbursementCreate(employee_id=self.EMP, category="Food"),       # no amount
            lambda: schemas.LoanCreate(employee_id=self.EMP),                                 # no principal
            lambda: schemas.ShiftCreate(name="", start_time="09:00", end_time="18:00"),
            lambda: schemas.HolidayCreate(name="Diwali"),                                     # no date
            lambda: schemas.EmployeeCreate(**_minimal_employee(work_email="not-an-email")),
            lambda: schemas.SprintCreate(project_name="p", name="s", start_date="2026-05-02", end_date="2026-05-01"),
        ]
        for i, make in enumerate(bad):
            with self.subTest(case=i), self.assertRaises(ValidationError):
                make()


if __name__ == "__main__":
    unittest.main()
