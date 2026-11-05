"""Departments are unique per Business Unit, not per company: the same name
can be created under several units, a duplicate within one unit is refused,
and name lookups (Add Employee, Edit Professional Info) tell them apart.

    cd backend && venv/Scripts/python -m unittest tests.test_departments_per_business_unit -v

Real routes against the dev DB inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crud, models  # noqa: E402
from tests.test_qa_employees_org import _QABase  # noqa: E402


class DepartmentsPerBusinessUnit(_QABase):
    def setUp(self):
        super().setUp()
        tag = uuid.uuid4().hex[:6]
        self.dname = f"QA Shared {tag}"
        self.unit_a = self._unit(f"QA Unit A {tag}")
        self.unit_b = self._unit(f"QA Unit B {tag}")

    def _unit(self, name: str) -> dict:
        r = self.client.post("/api/business-units", json={"name": name})
        self.assertIn(r.status_code, (200, 201), r.text)
        return r.json()

    def post(self, **over):
        return self.client.post("/api/employees", json=self.create_body(**over))

    def _dept(self, name: str, unit: dict | None):
        body = {"name": name}
        if unit is not None:
            body["business_unit_name"] = unit["name"]
        return self.client.post("/api/departments", json=body)

    def test_same_name_in_different_units_is_allowed(self):
        a = self._dept(self.dname, self.unit_a)
        b = self._dept(self.dname, self.unit_b)
        none = self._dept(self.dname, None)
        self.assertEqual((a.status_code, b.status_code, none.status_code), (201, 201, 201), (a.text, b.text, none.text))
        self.assertNotEqual(a.json()["code"], b.json()["code"])
        listed = [d for d in self.client.get("/api/departments").json() if d["name"] == self.dname]
        self.assertEqual(sorted(d["parent"] for d in listed),
                         sorted([self.unit_a["name"], self.unit_b["name"], "—"]))

    def test_duplicate_within_a_unit_is_refused(self):
        self.assertEqual(self._dept(self.dname, self.unit_a).status_code, 201)
        r = self._dept(self.dname.upper() + " ", self.unit_a)
        self.assertEqual(r.status_code, 409)
        self.assertIn(self.unit_a["name"], r.json()["detail"])
        self.assertEqual(self._dept(self.dname, None).status_code, 201)
        self.assertEqual(self._dept(self.dname.lower(), None).status_code, 409)

    def test_update_checks_the_target_unit(self):
        a = self._dept(self.dname, self.unit_a).json()
        b = self._dept(self.dname, self.unit_b).json()
        other = self._dept(f"{self.dname} other", self.unit_a).json()
        # Moving B's copy into unit A, or renaming A's other department to the shared name, clashes.
        r = self.client.patch(f"/api/departments/{b['id']}", json={"business_unit_name": self.unit_a["name"]})
        self.assertEqual(r.status_code, 409, r.text)
        r = self.client.patch(f"/api/departments/{other['id']}", json={"name": self.dname})
        self.assertEqual(r.status_code, 409, r.text)
        # Renaming within its own unit to its own name, or other unrelated edits, still work.
        r = self.client.patch(f"/api/departments/{a['id']}", json={"name": self.dname, "annual_budget": 1000})
        self.assertEqual(r.status_code, 200, r.text)
        # Moving A's other department to unit B (no clash there) works.
        r = self.client.patch(f"/api/departments/{other['id']}", json={"business_unit_name": self.unit_b["name"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["parent"], self.unit_b["name"])

    def test_assigning_departments_to_a_unit_refuses_duplicates(self):
        self._dept(self.dname, self.unit_a)
        b = self._dept(self.dname, self.unit_b).json()
        r = self.client.post(f"/api/business-units/{self.unit_a['id']}/departments", json={"department_ids": [b["id"]]})
        self.assertEqual(r.status_code, 409, r.text)
        r = self.client.put(f"/api/business-units/{self.unit_b['id']}/departments", json={"department_ids": [b["id"]]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_name_lookup_and_labels(self):
        a = self._dept(self.dname, self.unit_a).json()
        b = self._dept(self.dname, self.unit_b).json()
        dept_a = self.db.get(models.Department, uuid.UUID(a["id"]))
        dept_b = self.db.get(models.Department, uuid.UUID(b["id"]))
        with self.assertRaises(ValueError):
            crud.get_department_by_name(self.db, self.company_id, self.dname)
        self.assertEqual(crud.get_department_by_name(self.db, self.company_id, f"{self.dname} ({self.unit_b['name']})"),
                         dept_b)
        self.assertEqual(crud.get_department_by_name(self.db, self.company_id, self.dname, current=dept_a), dept_a)
        self.assertIsNone(crud.get_department_by_name(self.db, self.company_id, f"{self.dname} (No Such Unit)"))
        none = self._dept(self.dname, None).json()
        self.assertEqual(
            crud.get_department_by_name(self.db, self.company_id, f"{self.dname} ({crud.NO_BUSINESS_UNIT_LABEL})").id,
            uuid.UUID(none["id"]))

    def test_employees_are_assigned_to_the_right_unit(self):
        self._dept(self.dname, self.unit_a)
        b = self._dept(self.dname, self.unit_b).json()
        # A bare shared name is ambiguous: refused with the choices, nothing created.
        r = self.post(department_name=self.dname)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn(self.unit_a["name"], r.json()["detail"])
        r = self.post(department_name=f"{self.dname} ({self.unit_b['name']})")
        self.assertEqual(r.status_code, 201, r.text)
        emp = self.db.get(models.Employee, uuid.UUID(r.json()["id"]))
        self.assertEqual(str(emp.department_id), b["id"])
        # Edit Professional Info sends the plain name back: the employee stays in unit B's copy.
        r = self.client.patch(f"/api/employees/{emp.id}/org", json={"department_name": self.dname, "work_mode": "Office"})
        self.assertEqual(r.status_code, 200, r.text)
        self.db.refresh(emp)
        self.assertEqual(str(emp.department_id), b["id"])
        # Picking unit A's copy by its label moves them.
        r = self.client.patch(f"/api/employees/{emp.id}/org",
                              json={"department_name": f"{self.dname} ({self.unit_a['name']})"})
        self.assertEqual(r.status_code, 200, r.text)
        self.db.refresh(emp)
        self.assertEqual(emp.department.business_unit_id, uuid.UUID(self.unit_a["id"]))

    def test_unique_names_behave_as_before(self):
        name = f"{self.dname} solo"
        self.assertEqual(self._dept(name, self.unit_a).status_code, 201)
        self.assertIsNotNone(crud.get_department_by_name(self.db, self.company_id, name.upper()))
        r = self.post(department_name=name)
        self.assertEqual(r.status_code, 201, r.text)


if __name__ == "__main__":
    unittest.main()
