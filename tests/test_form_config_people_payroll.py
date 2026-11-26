"""Form Configuration wired into People (employee) and Payroll (salary
component): configurable Employment Type / Work Mode / Type / Calculation
options and admin-defined custom fields, through the real routes.

    cd backend && venv/Scripts/python -m unittest tests.test_form_config_people_payroll -v

Rolled-back transaction on the dev DB (tenant "acme" by default).
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

from app import form_config as fc, models  # noqa: E402
from app.routers import form_config as api  # noqa: E402
from tests.test_qa_employees_org import _QABase  # noqa: E402


class _Cfg(_QABase):
    def setUp(self):
        super().setUp()
        self.cid = self.company_id

    # ── admin helpers (the same functions the admin screen calls) ──
    def add_option(self, field_key, label):
        return api.create_option(api.OptionCreate(field_key=field_key, label=label), db=self.db, current_user=self.owner)

    def hide_option(self, field_key, value):
        row = next(r for r in fc._rows(self.db, self.cid)[field_key] if r.value == value)
        api.delete_option(row.id, db=self.db, current_user=self.owner)

    def relabel(self, field_key, value, label):
        row = next(r for r in fc._rows(self.db, self.cid)[field_key] if r.value == value)
        api.update_option(row.id, api.OptionUpdate(label=label), db=self.db, current_user=self.owner)

    def custom(self, form_key, label, ftype, **kw):
        return api.create_custom_field(
            api.CustomFieldCreate(form_key=form_key, label=label, field_type=ftype, **kw),
            db=self.db, current_user=self.owner)

    def post_employee(self, **over):
        return self.client.post("/api/employees", json=self.create_body(**over))


class EmployeeOptionTests(_Cfg):
    def test_registry_matches_what_the_module_stores(self):
        self.assertEqual(
            [v for v, _ in fc.options(self.db, self.cid, "employee.employment_type")],
            ["Full-time", "Part-time", "Contract", "Intern", "Freelancer"])
        self.assertFalse(fc.FIELDS["employee.employment_type"].allow_custom)
        self.assertTrue(fc.FIELDS["employee.work_mode"].allow_custom)

    def test_admin_added_work_mode_is_accepted_everywhere(self):
        self.assertEqual(self.post_employee(work_mode="Field Work").status_code, 422)
        self.add_option("employee.work_mode", "Field Work")
        r = self.post_employee(work_mode="field work")           # canonical value comes back
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["work_mode"], "Field Work")
        emp = self.new_employee(work_mode="Office")
        r = self.client.patch(f"/api/employees/{emp.id}/org", json={"work_mode": "Field Work"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["work_mode"], "Field Work")
        # the directory shows it and filters by it
        page = self.client.get("/api/employees", params={"work_mode": "Field Work", "limit": 50}).json()
        self.assertIn(str(emp.id), [i["id"] for i in page["items"]])
        self.assertTrue(all(i["mode"] == "Field Work" for i in page["items"]))

    def test_hidden_work_mode_rejected_but_unchanged_legacy_value_saves(self):
        emp = self.new_employee(work_mode="Hybrid", employment_type="Intern")
        self.hide_option("employee.work_mode", "Hybrid")
        self.hide_option("employee.employment_type", "Intern")
        self.assertEqual(self.post_employee(work_mode="Hybrid").status_code, 422)
        self.assertEqual(self.post_employee(employment_type="Intern").status_code, 422)
        other = self.new_employee(work_mode="Office")
        self.assertEqual(self.client.patch(f"/api/employees/{other.id}/org", json={"work_mode": "Hybrid"}).status_code, 422)
        self.assertEqual(self.client.patch(f"/api/employees/{other.id}/org", json={"employment_type": "intern"}).status_code, 422)
        # the legacy employee keeps both when only something else is edited
        r = self.client.patch(f"/api/employees/{emp.id}/org",
                              json={"work_mode": "Hybrid", "employment_type": "Intern", "band": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["work_mode"], r.json()["employment_type"]), ("Hybrid", "Intern"))
        # a visible type still works
        self.assertEqual(self.post_employee(employment_type="Part-time").status_code, 201)

    def test_fixed_employment_types_cannot_be_extended(self):
        with self.assertRaises(Exception):
            self.add_option("employee.employment_type", "Gig Worker")
        self.assertEqual(self.post_employee(employment_type="Gig Worker").status_code, 422)

    def test_relabel_keeps_stored_value(self):
        self.relabel("employee.employment_type", "Part-time", "Part Time (20h)")
        r = self.post_employee(employment_type="Part-time")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["employment_type"], "Part-time")
        self.assertEqual(fc.option_label(self.db, self.cid, "employee.employment_type", "Part-time"), "Part Time (20h)")


class EmployeeCustomFieldTests(_Cfg):
    def make_fields(self):
        self.badge = self.custom("employee", "Badge No", "text", is_required=True)
        self.notes = self.custom("employee", "HR Notes", "textarea")
        self.kids = self.custom("employee", "Children", "number")
        self.anniv = self.custom("employee", "Wedding Anniversary", "date")
        self.shirt = self.custom("employee", "Shirt Size", "dropdown", options=["S", "M", "L"])
        self.vet = self.custom("employee", "Veteran", "checkbox")

    def test_create_validates_persists_and_reads_back_everywhere(self):
        self.make_fields()
        base = self.create_body()
        r = self.client.post("/api/employees", json=base)
        self.assertEqual(r.status_code, 422, r.text)                          # required Badge No missing
        for bad in ({"badge_no": "B1", "children": "two"}, {"badge_no": "B1", "wedding_anniversary": "31/01/2020"},
                    {"badge_no": "B1", "shirt_size": "XXL"}, {"badge_no": "B1", "veteran": "maybe"},
                    {"badge_no": "B1", "ghost": "1"}, {"badge_no": "x" * 501}):
            r = self.client.post("/api/employees", json={**base, "work_email": f"a{uuid.uuid4().hex[:8]}@example.com",
                                                         "custom_fields": bad})
            self.assertEqual(r.status_code, 422, bad)
        good = {"badge_no": " B-77 ", "hr_notes": "line1\nline2", "children": "2", "wedding_anniversary": "2018-02-14",
                "shirt_size": "m", "veteran": True}
        r = self.client.post("/api/employees", json={**base, "work_email": f"b{uuid.uuid4().hex[:8]}@example.com",
                                                     "custom_fields": good})
        self.assertEqual(r.status_code, 201, r.text)
        saved = r.json()["custom_fields"]
        self.assertEqual(saved, {"badge_no": "B-77", "hr_notes": "line1\nline2", "children": "2",
                                 "wedding_anniversary": "2018-02-14", "shirt_size": "M", "veteran": "true"})
        eid = r.json()["id"]
        # overview / professional / paginated directory / full roster
        self.assertEqual(self.client.get(f"/api/employees/{eid}/overview").json()["customFields"], saved)
        self.assertEqual(self.client.get(f"/api/employees/{eid}/professional").json()["customFields"], saved)
        page = self.client.get("/api/employees", params={"search": r.json()["employee_code"]}).json()
        self.assertEqual(next(i for i in page["items"] if i["id"] == eid)["customFields"], saved)
        full = self.client.get("/api/employees/full").json()
        self.assertEqual(next(i for i in full if i["id"] == eid)["customFields"], saved)

    def test_edit_is_partial_and_can_clear_optional_values(self):
        self.make_fields()
        emp = self.new_employee()
        fc.save_custom_values(self.db, self.cid, "employee", emp.id, {"badge_no": "B1", "shirt_size": "S", "children": "3"})
        r = self.client.patch(f"/api/employees/{emp.id}/org", json={"custom_fields": {"shirt_size": "L", "children": ""}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["custom_fields"], {"badge_no": "B1", "shirt_size": "L"})
        # required can't be blanked, unknown / invalid values are refused, nothing is half-saved
        for bad in ({"badge_no": ""}, {"shirt_size": "XXL"}, {"nope": "1"}):
            self.assertEqual(self.client.patch(f"/api/employees/{emp.id}/org", json={"custom_fields": bad}).status_code, 422)
        self.assertEqual(fc.load_custom_values(self.db, self.cid, "employee", [emp.id])[emp.id]["badge_no"], "B1")
        # an edit that doesn't send custom_fields leaves them alone
        r = self.client.patch(f"/api/employees/{emp.id}/org", json={"work_mode": "Remote"})
        self.assertEqual(r.json()["custom_fields"]["badge_no"], "B1")

    def test_hidden_field_is_neither_required_nor_shown_and_values_are_tenant_scoped(self):
        self.make_fields()
        api.update_custom_field(self.badge.id, api.CustomFieldUpdate(is_active=False), db=self.db, current_user=self.owner)
        r = self.post_employee()
        self.assertEqual(r.status_code, 201, r.text)
        self.assertNotIn("badge_no", r.json()["custom_fields"])
        emp = self.new_employee()
        fc.save_custom_values(self.db, self.cid, "employee", emp.id, {"shirt_size": "S"})
        self.assertEqual(fc.load_custom_values(self.db, uuid.uuid4(), "employee", [emp.id]), {})   # other company
        # a field of another form is not accepted on an employee
        self.custom("asset", "Asset Only", "text")
        self.assertEqual(self.client.patch(f"/api/employees/{emp.id}/org",
                                           json={"custom_fields": {"asset_only": "x"}}).status_code, 422)


class SalaryComponentTests(_Cfg):
    def body(self, **over):
        b = {"name": "Spot " + uuid.uuid4().hex[:6], "code": "S" + uuid.uuid4().hex[:7].upper(),
             "component_type": "earning", "calc_type": "flat", "is_taxable": True}
        b.update(over)
        return b

    def post(self, **over):
        return self.client.post("/api/payroll/salary-components", json=self.body(**over))

    def test_options_stored_as_keys_labels_configurable(self):
        self.relabel("salary_component.type", "earning", "Income")
        self.assertEqual(fc.option_label(self.db, self.cid, "salary_component.type", "earning"), "Income")
        r = self.post(component_type="Earning", calc_type="PERCENT")      # canonical keys come back
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((r.json()["component_type"], r.json()["calc_type"]), ("earning", "percent"))
        for bad in ({"component_type": "bonus"}, {"calc_type": "magic"}, {"component_type": ""}):
            self.assertEqual(self.post(**bad).status_code, 422, bad)
        # these lists are fixed -- the engine computes from the exact keys
        with self.assertRaises(Exception):
            self.add_option("salary_component.calculation", "Magic")

    def test_hidden_option_rejected_for_new_but_existing_components_stay_editable(self):
        made = self.post(component_type="tax", calc_type="var_annual")
        self.assertEqual(made.status_code, 201, made.text)
        cid = made.json()["id"]
        self.hide_option("salary_component.type", "tax")
        self.hide_option("salary_component.calculation", "var_annual")
        self.assertEqual(self.post(component_type="tax").status_code, 422)
        self.assertEqual(self.post(calc_type="var_annual").status_code, 422)
        r = self.client.patch(f"/api/payroll/salary-components/{cid}",
                              json={"name": "Renamed", "component_type": "tax", "calc_type": "var_annual"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["name"], r.json()["component_type"], r.json()["calc_type"]),
                         ("Renamed", "tax", "var_annual"))
        # moving a component onto a hidden option is refused
        other = self.post().json()["id"]
        self.assertEqual(self.client.patch(f"/api/payroll/salary-components/{other}",
                                           json={"component_type": "tax"}).status_code, 422)
        self.assertEqual(self.client.patch(f"/api/payroll/salary-components/{uuid.uuid4()}",
                                           json={"name": "x"}).status_code, 404)

    def test_custom_fields_validate_persist_and_list(self):
        grade = self.custom("salary_component", "Pay Grade", "dropdown", options=["G1", "G2"], is_required=True)
        self.custom("salary_component", "Min Months", "number")
        self.custom("salary_component", "Effective From", "date")
        self.custom("salary_component", "Auto Revise", "checkbox")
        self.custom("salary_component", "Comment", "textarea")
        self.assertEqual(self.post().status_code, 422)                                   # required missing
        for bad in ({"pay_grade": "G9"}, {"pay_grade": "G1", "min_months": "x"},
                    {"pay_grade": "G1", "effective_from": "soon"}, {"pay_grade": "G1", "auto_revise": "2"},
                    {"pay_grade": "G1", "zzz": "1"}):
            self.assertEqual(self.post(custom_fields=bad).status_code, 422, bad)
        r = self.post(custom_fields={"pay_grade": "g2", "min_months": "6", "effective_from": "2026-04-01",
                                     "auto_revise": False, "comment": "ok"})
        self.assertEqual(r.status_code, 201, r.text)
        want = {"pay_grade": "G2", "min_months": "6", "effective_from": "2026-04-01", "auto_revise": "false", "comment": "ok"}
        self.assertEqual(r.json()["custom_fields"], want)
        comp_id = r.json()["id"]
        listed = next(c for c in self.client.get("/api/payroll/salary-components").json() if c["id"] == comp_id)
        self.assertEqual(listed["custom_fields"], want)
        # partial edit + clear
        r = self.client.patch(f"/api/payroll/salary-components/{comp_id}",
                              json={"custom_fields": {"min_months": "", "pay_grade": "G1"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["custom_fields"],
                         {k: v for k, v in {**want, "pay_grade": "G1"}.items() if k != "min_months"})
        self.assertEqual(self.client.patch(f"/api/payroll/salary-components/{comp_id}",
                                           json={"custom_fields": {"pay_grade": ""}}).status_code, 422)
        self.assertEqual(fc.load_custom_values(self.db, uuid.uuid4(), "salary_component", [uuid.UUID(comp_id)]), {})
        api.delete_custom_field(grade.id, db=self.db, current_user=self.owner)
        self.assertNotIn("pay_grade", fc.load_custom_values(self.db, self.cid, "salary_component",
                                                            [uuid.UUID(comp_id)]).get(uuid.UUID(comp_id), {}))


if __name__ == "__main__":
    unittest.main()
