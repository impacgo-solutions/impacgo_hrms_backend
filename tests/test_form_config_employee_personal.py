"""Form Configuration: Gender / Marital Status / Nationality and the previous-
employer Domain / Industry of an employee are option lists users can extend
("Add custom..."), validated by the real employee routes.

    cd backend && venv/Scripts/python -m unittest tests.test_form_config_employee_personal -v

Rolled-back transaction on the dev DB. Labels are kept short on purpose so the
tests also pass before db/add_form_config_employee_personal.sql is applied.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import form_config as fc, models  # noqa: E402
from app.routers import form_config as api  # noqa: E402
from tests.test_qa_employees_org import _QABase  # noqa: E402


class _Cfg(_QABase):
    def setUp(self):
        super().setUp()
        self.cid = self.company_id

    def quick_add(self, key, label):
        return api.quick_add_option(api.QuickAddIn(field_key=key, label=label), db=self.db, current_user=self.owner)

    def hide(self, key, value):
        row = next(r for r in fc._rows(self.db, self.cid)[key] if r.value == value)
        api.delete_option(row.id, db=self.db, current_user=self.owner)

    def patch_profile(self, emp, **body):
        return self.client.patch(f"/api/employees/{emp.id}/profile", json=body)


class RegistryTests(_Cfg):
    def test_fields_are_user_addable_with_the_old_defaults(self):
        for key in ("employee.gender", "employee.marital_status", "employee.nationality",
                    "employee.experience_domain"):
            self.assertTrue(fc.FIELDS[key].allow_custom, key)
        self.assertEqual([v for v, _ in fc.options(self.db, self.cid, "employee.gender")], ["Male", "Female", "Other"])
        self.assertEqual([v for v, _ in fc.options(self.db, self.cid, "employee.marital_status")],
                         ["Single", "Married", "Divorced", "Widowed"])
        cfg = api.get_form_config(db=self.db, current_user=self.owner)
        for key in ("employee.gender", "employee.marital_status", "employee.nationality",
                    "employee.experience_domain"):
            self.assertIn(key, cfg.quick_add)


class PersonalOptionTests(_Cfg):
    def test_quick_added_values_are_accepted_by_create_and_profile_edit(self):
        self.assertEqual(self.client.post("/api/employees", json=self.create_body(gender="Agender")).status_code, 422)
        self.quick_add("employee.gender", "Agender")
        self.quick_add("employee.marital_status", "Separated")
        self.quick_add("employee.nationality", "Nepali")
        r = self.client.post("/api/employees", json=self.create_body(gender="agender", marital_status="separated"))
        self.assertEqual(r.status_code, 201, r.text)
        emp = self.db.get(models.Employee, r.json()["id"])
        self.assertEqual((emp.gender, emp.marital_status), ("Agender", "Separated"))
        other = self.new_employee(gender="Male")
        r = self.patch_profile(other, gender="Agender", marital_status="Separated", nationality="Nepali")
        self.assertEqual(r.status_code, 200, r.text)
        self.db.refresh(other)
        self.assertEqual((other.gender, other.marital_status, other.nationality), ("Agender", "Separated", "Nepali"))

    def test_unknown_values_are_rejected(self):
        emp = self.new_employee(gender="Male")
        for body in ({"gender": "Robot"}, {"marital_status": "Complicated"}, {"nationality": "Martian"}):
            self.assertEqual(self.patch_profile(emp, **body).status_code, 422, body)

    def test_hidden_value_rejected_but_unchanged_legacy_value_saves(self):
        legacy = self.new_employee(gender="Other", marital_status="Widowed", nationality="British")
        self.hide("employee.gender", "Other")
        self.hide("employee.marital_status", "Widowed")
        self.hide("employee.nationality", "British")
        other = self.new_employee(gender="Male")
        self.assertEqual(self.patch_profile(other, gender="Other").status_code, 422)
        self.assertEqual(self.client.post("/api/employees", json=self.create_body(gender="Other")).status_code, 422)
        # the legacy employee keeps its values while another field is edited
        r = self.patch_profile(legacy, gender="Other", marital_status="Widowed", nationality="British",
                               personal_phone="9999999999")
        self.assertEqual(r.status_code, 200, r.text)
        self.db.refresh(legacy)
        self.assertEqual((legacy.gender, legacy.marital_status, legacy.nationality),
                         ("Other", "Widowed", "British"))

    def test_clearing_a_value_is_allowed(self):
        emp = self.new_employee(gender="Male")
        self.assertEqual(self.patch_profile(emp, gender=None).status_code, 200)
        self.db.refresh(emp)
        self.assertIsNone(emp.gender)


class ExperienceDomainTests(_Cfg):
    def record(self, domain):
        return {"company_name": "Acme Old Co", "domain": domain, "job_role": "Analyst", "years_experience": 2}

    def test_options_endpoint_serves_the_configured_list(self):
        self.quick_add("employee.experience_domain", "Space Tourism")
        from app.routers import employees
        out = employees.education_options(db=self.db, current_user=self.owner)
        self.assertIn("Space Tourism", out["domains"])
        self.assertIn("Information Technology", out["domains"])

    def test_added_domain_saves_unknown_is_rejected(self):
        emp = self.new_employee()
        url = f"/api/employees/{emp.id}/education"
        r = self.client.patch(url, json={"experience_records": [self.record("Space Tourism")]})
        self.assertEqual(r.status_code, 422, r.text)
        self.quick_add("employee.experience_domain", "Space Tourism")
        r = self.client.patch(url, json={"experience_records": [self.record("space tourism")]})
        self.assertEqual(r.status_code, 200, r.text)
        rows = self.db.query(models.EmployeePriorExperience).filter_by(employee_id=emp.id).all()
        self.assertEqual([x.domain for x in rows], ["Space Tourism"])

    def test_hidden_domain_rejected_but_an_existing_one_is_kept(self):
        emp = self.new_employee()
        url = f"/api/employees/{emp.id}/education"
        self.assertEqual(self.client.patch(url, json={"experience_records": [self.record("Telecom")]}).status_code, 200)
        self.hide("employee.experience_domain", "Telecom")
        r = self.client.patch(url, json={"experience_records": [self.record("Telecom"), self.record("Insurance")]})
        self.assertEqual(r.status_code, 200, r.text)           # Telecom is already this employee's
        other = self.new_employee()
        r = self.client.patch(f"/api/employees/{other.id}/education", json={"experience_records": [self.record("Telecom")]})
        self.assertEqual(r.status_code, 422)

    def test_blank_domain_is_fine(self):
        emp = self.new_employee()
        r = self.client.patch(f"/api/employees/{emp.id}/education", json={"experience_records": [self.record(None)]})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
