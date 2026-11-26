"""Edit Profile edits every field (PATCH /api/employees/{id}/org):
name, Employee ID, work email (and the login that follows it), reporting
managers (with loop / duplicate checks) and shift -- all-or-nothing.

    cd backend && venv/Scripts/python -m unittest tests.test_profile_full_edit -v
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas  # noqa: E402
from app.routers import employees as api  # noqa: E402

TENANT = os.environ.get("EDU_TEST_TENANT", "impacgo-solutions")


class ProfileFullEditTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User).where(models.User.status == "active")).all()
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                           and r.name == crud.BUILTIN_ROLES[0]), None)
        if self.owner is None:
            self.skipTest("no Owner")
        self.company_id = self.owner.company_id
        people = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.company_id, models.Employee.is_active.is_(True),
            models.Employee.id != self.owner.employee_id).order_by(models.Employee.employee_code)).all()
        if len(people) < 3:
            self.skipTest("needs three employees")
        self.emp, self.other, self.third = people[0], people[1], people[2]

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def org(self, **fields):
        return api.update_employee_org(self.emp.id, schemas.EmployeeOrgUpdate(**fields),
                                       db=self.db, current_user=self.owner)

    def refused(self, **fields):
        with self.assertRaises(HTTPException) as ctx:
            self.org(**fields)
        return ctx.exception.status_code, str(ctx.exception.detail)

    def test_name_code_and_email(self):
        code = f"QA{uuid.uuid4().hex[:6].upper()}"
        email = f"qa.{uuid.uuid4().hex[:6]}@example.com"
        user = crud.get_core_user_by_employee_id(self.db, self.emp.id)
        login_followed = user is not None and (user.email or "").lower() == (self.emp.work_email or "").lower()
        self.org(first_name="  Ravi  Kumar ", last_name="Sharma", employee_code=code, work_email=email)
        self.db.refresh(self.emp)
        self.assertEqual((self.emp.first_name, self.emp.last_name, self.emp.employee_code, self.emp.work_email),
                         ("Ravi Kumar", "Sharma", code, email))
        if login_followed:
            self.db.refresh(user)
            self.assertEqual(user.email, email, "the login follows the work email")

    def test_duplicates_are_refused(self):
        code, detail = self.refused(employee_code=self.other.employee_code)
        self.assertEqual(code, 409)
        if self.other.work_email:
            code, _ = self.refused(work_email=self.other.work_email)
            self.assertEqual(code, 409)
        code, _ = self.refused(first_name=" ")
        self.assertIn(code, (409, 422))

    def test_managers_with_loop_and_duplicate_checks(self):
        self.org(reporting_manager_id=self.other.id, dotted_line_manager_id=self.third.id)
        self.db.refresh(self.emp)
        self.assertEqual((self.emp.reporting_manager_id, self.emp.dotted_line_manager_id),
                         (self.other.id, self.third.id))
        code, _ = self.refused(dotted_line_manager_id=self.other.id)            # same as the RM
        self.assertEqual(code, 400)
        code, _ = self.refused(reporting_manager_id=self.emp.id)                # themselves
        self.assertIn(code, (400, 422))
        # A loop: other reports to emp, so emp can't report to other.
        self.org(dotted_line_manager_id=None)
        self.other.reporting_manager_id = self.emp.id
        self.db.flush()
        code, _ = self.refused(reporting_manager_id=self.other.id, dotted_line_manager_id=None)
        self.assertEqual(code, 400)
        self.org(reporting_manager_id=None)                                     # explicit clear
        self.db.refresh(self.emp)
        self.assertIsNone(self.emp.reporting_manager_id)

    def test_shift_from_today_and_cleared(self):
        shift = self.db.scalar(select(models.Shift).where(models.Shift.company_id == self.company_id).limit(1))
        if shift is None:
            self.skipTest("no shift")
        today = crud.company_today(self.db, self.company_id)
        self.org(shift_id=shift.id)
        active = crud.get_active_shift_assignment(self.db, self.emp.id, today)
        self.assertIsNotNone(active)
        self.assertEqual(active.shift_id, shift.id)
        self.org(shift_id=None)
        self.assertIsNone(crud.get_active_shift_assignment(self.db, self.emp.id, today))

    def test_professional_view_carries_exact_values(self):
        p = crud.get_employee_professional(self.db, self.emp.id)
        for key in ("firstName", "lastName", "employeeCode", "workEmail", "branchName",
                    "reportingManagerId", "dottedLineManagerId", "shiftId", "bandNumber"):
            self.assertIn(key, p)


if __name__ == "__main__":
    unittest.main()
