"""Regression tests for the Edit-workflow audit fixes (projects status,
claim-type name cap, task estimate bounds, salary-structure tag clearing).

    cd backend && venv/Scripts/python -m unittest tests.test_edit_workflow_fixes -v

SchemaFixTests need no database. SalaryStructureTagTests run in a transaction
that is ALWAYS rolled back; point DATABASE_URL at impacgo_test_dev, never
production.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas  # noqa: E402

TENANT = os.environ.get("TASK_TEST_TENANT", "impacgo-solutions")


class SchemaFixTests(unittest.TestCase):
    def test_project_status_must_be_a_db_status(self):
        for ok in ("draft", "planning", "active", "on_hold", "completed", "cancelled"):
            self.assertEqual(schemas.ProjectUpdate(status=ok).status, ok)
        with self.assertRaises(ValidationError):
            schemas.ProjectUpdate(status="in_progress")  # the old, CHECK-violating value

    def test_claim_type_name_capped_at_line_category_width(self):
        schemas.ExpenseClaimTypeIn(name="x" * 40)
        with self.assertRaises(ValidationError):
            schemas.ExpenseClaimTypeIn(name="x" * 41)
        with self.assertRaises(ValidationError):
            schemas.ExpenseClaimTypeUpdate(name="x" * 41)

    def test_task_estimate_bounds(self):
        self.assertEqual(schemas.TaskCardUpdate(estimated_hours=0).estimated_hours, 0)
        for bad in (-1, 10000):
            with self.assertRaises(ValidationError):
                schemas.TaskCardUpdate(estimated_hours=bad)
        with self.assertRaises(ValidationError):
            schemas.TaskCardUpdate(description="x" * 5001)

    def test_structure_update_distinguishes_unset_from_null(self):
        self.assertNotIn("department_id", schemas.SalaryStructureUpdate().model_dump(exclude_unset=True))
        self.assertIn(
            "department_id",
            schemas.SalaryStructureUpdate(department_id=None).model_dump(exclude_unset=True),
        )


class SalaryStructureTagTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def test_explicit_none_clears_tags_and_omitted_keeps_them(self):
        company = self.db.scalars(select(models.Company)).first()
        comp = self.db.scalars(select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == company.id)).first()
        dept = self.db.scalars(select(models.Department).where(
            models.Department.company_id == company.id)).first()
        if comp is None or dept is None:
            self.skipTest("tenant has no salary component / department")
        st = crud.create_salary_structure(
            self.db, company.id, "QA tag clear", datetime.date(2020, 1, 1),
            [{"component_id": comp.id, "amount": 1}], department_id=dept.id, grade="G1")
        crud.update_salary_structure(self.db, st.id, name="QA tag clear 2")  # omitted
        self.assertEqual(st.department_id, dept.id)
        crud.update_salary_structure(self.db, st.id, department_id=None)  # explicit clear
        self.assertIsNone(st.department_id)


if __name__ == "__main__":
    unittest.main()
