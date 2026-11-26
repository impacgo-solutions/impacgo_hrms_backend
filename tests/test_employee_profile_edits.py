"""People / Employee Profile Edit workflows: certification removal, lifecycle
record id validation, CTC bounds and the status-change guard on /org.

    cd backend && venv/Scripts/python -m unittest tests.test_employee_profile_edits -v

Runs inside a transaction that is ALWAYS rolled back (tenant impacgo-solutions
by default, EDU_TEST_TENANT to override). Point DATABASE_URL at the test DB.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas  # noqa: E402
from app.routers import employees as employees_api  # noqa: E402

TENANT = os.environ.get("EDU_TEST_TENANT", "impacgo-solutions")


class EmployeeProfileEditTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.emp = self.db.scalars(
            select(models.Employee).where(models.Employee.is_active.is_(True)).order_by(models.Employee.first_name)
        ).first()
        if self.emp is None:
            self.skipTest("no employee")
        self.company_id = self.emp.company_id

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # ── certifications ──────────────────────────────────────────────────────
    def _certs(self):
        return {
            c.name for c in self.db.scalars(
                select(models.Certification).where(models.Certification.employee_id == self.emp.id)
            )
        }

    def test_removing_a_certification_persists_but_detailed_rows_are_kept(self):
        self.db.add(models.Certification(id=uuid.uuid4(), employee_id=self.emp.id, name="ZZ Bare Cert"))
        self.db.add(models.Certification(
            id=uuid.uuid4(), employee_id=self.emp.id, name="ZZ Issued Cert", issuer="Some Body"))
        self.db.flush()
        crud.update_employee_education(self.db, self.emp.id, {"certifications": "ZZ New Cert"})
        self.db.flush()
        names = self._certs()
        self.assertNotIn("ZZ Bare Cert", names)       # dropped from the text -> removed
        self.assertIn("ZZ Issued Cert", names)        # carries Learning detail -> kept
        self.assertIn("ZZ New Cert", names)           # new name added

    def test_blank_certifications_clears_bare_rows(self):
        self.db.add(models.Certification(id=uuid.uuid4(), employee_id=self.emp.id, name="ZZ Bare Cert"))
        self.db.flush()
        crud.update_employee_education(self.db, self.emp.id, {"certifications": ""})
        self.db.flush()
        self.assertNotIn("ZZ Bare Cert", self._certs())

    # ── lifecycle record edits ──────────────────────────────────────────────
    def test_lifecycle_update_rejects_foreign_or_unknown_ids(self):
        with self.assertRaises(HTTPException) as ctx:
            employees_api._validate_lifecycle_refs(
                self.db, self.company_id, {"to_department_id": uuid.uuid4()})
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            employees_api._validate_lifecycle_refs(
                self.db, self.company_id, {"from_reporting_manager_id": uuid.uuid4()})

    def test_lifecycle_update_accepts_own_company_ids_and_nulls(self):
        dept = self.db.scalars(
            select(models.Department).where(models.Department.company_id == self.company_id)
        ).first()
        updates = {"to_ctc": 100, "event_date": None, "from_branch_id": None}
        if dept is not None:
            updates["to_department_id"] = dept.id
        employees_api._validate_lifecycle_refs(self.db, self.company_id, updates)  # no raise
        employees_api._validate_lifecycle_refs(
            self.db, self.company_id, {"to_reporting_manager_id": self.emp.id})

    def test_lifecycle_ctc_must_not_be_negative(self):
        with self.assertRaises(ValidationError):
            schemas.LifecycleEventUpdate(to_ctc=-1)
        self.assertEqual(schemas.LifecycleEventUpdate(from_ctc=0).from_ctc, 0)

    # ── /org status guard ───────────────────────────────────────────────────
    def test_cannot_change_own_employment_status(self):
        me = types.SimpleNamespace(employee_id=self.emp.id, id=uuid.uuid4(), company_id=self.company_id)
        new_status = "Terminated" if (self.emp.status or "").lower() != "terminated" else "Active"
        with self.assertRaises(HTTPException) as ctx:
            employees_api.update_employee_org(
                self.emp.id, schemas.EmployeeOrgUpdate(status=new_status), db=self.db, current_user=me)
        self.assertEqual(ctx.exception.status_code, 403)

    # ── project list order ──────────────────────────────────────────────────
    def test_employee_projects_carry_ids_in_primary_allocation_order(self):
        rows = crud.get_employee_projects(self.db, self.emp.id, self.company_id)
        for r in rows:
            self.assertIn("projectManagerId", r)
            self.assertIn("teamLeadId", r)
        primary = crud.get_primary_project_allocation(self.db, self.emp.id)
        if rows and primary is not None:
            project = self.db.get(models.Project, primary.project_id)
            if project is not None and project.company_id == self.company_id:
                self.assertEqual(rows[0]["name"], project.name)


if __name__ == "__main__":
    unittest.main()
