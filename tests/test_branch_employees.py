"""Branch > Employees multi-select (PUT/GET /api/branches/{id}/employees).
Runs in a rolled-back transaction."""
from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import database, models, schemas  # noqa: E402
from app.routers import organization as api  # noqa: E402

TENANT = os.environ.get("TEST_TENANT", "impacgo-solutions")


class BranchEmployeesTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.user = self.db.scalars(select(models.User)).first()
        if self.user is None:
            self.skipTest("no user")
        cid = self.user.company_id
        emps = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == cid, models.Employee.is_active.is_(True))).all()
        if len(emps) < 3:
            self.skipTest("need three active employees")
        self.e1, self.e2, self.e3 = emps[:3]
        self.b1 = models.Branch(id=uuid.uuid4(), company_id=cid, code=f"QA{uuid.uuid4().hex[:6]}", name=f"QA A {uuid.uuid4().hex[:5]}", country="India", tz="IST (UTC+5:30)")
        self.b2 = models.Branch(id=uuid.uuid4(), company_id=cid, code=f"QB{uuid.uuid4().hex[:6]}", name=f"QA B {uuid.uuid4().hex[:5]}", country="India", tz="IST (UTC+5:30)")
        self.db.add_all([self.b1, self.b2])
        self.db.flush()
        self.e1.branch_id = None
        self.e2.branch_id = self.b2.id
        self.e3.branch_id = None
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def put(self, branch, ids):
        return api.set_branch_employees(branch.id, schemas.BranchEmployeesSet(employee_ids=ids),
                                        db=self.db, current_user=self.user)

    def test_assign_move_remove_and_duplicates(self):
        out = self.put(self.b1, [self.e1.id, self.e2.id, self.e1.id])  # duplicate ignored
        self.assertEqual((out.added, out.moved_from_other_branches, out.removed), (2, 1, 0))
        self.db.refresh(self.e2)
        self.assertEqual(self.e2.branch_id, self.b1.id)
        got = api.get_branch_employees(self.b1.id, db=self.db, current_user=self.user)
        self.assertEqual(set(got), {self.e1.id, self.e2.id})
        # moving e2 in recorded a transfer from b2 -> b1
        ev = self.db.scalars(select(models.EmployeeLifecycleEvent).where(
            models.EmployeeLifecycleEvent.employee_id == self.e2.id,
            models.EmployeeLifecycleEvent.to_branch_id == self.b1.id)).first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.from_branch_id, self.b2.id)
        # Re-saving the same set is a no-op; dropping e1 removes only e1.
        self.assertEqual(self.put(self.b1, [self.e1.id, self.e2.id]).added, 0)
        out = self.put(self.b1, [self.e2.id])
        self.assertEqual(out.removed, 1)
        self.db.refresh(self.e1)
        self.assertIsNone(self.e1.branch_id)
        self.assertEqual(self.put(self.b1, []).removed, 1)

    def test_rejects_unknown_employee_and_foreign_branch(self):
        with self.assertRaises(HTTPException) as ctx:
            self.put(self.b1, [uuid.uuid4()])
        self.assertEqual(ctx.exception.status_code, 422)
        with self.assertRaises(HTTPException) as ctx:
            api.set_branch_employees(uuid.uuid4(), schemas.BranchEmployeesSet(employee_ids=[]),
                                     db=self.db, current_user=self.user)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
