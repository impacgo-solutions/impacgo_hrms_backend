"""GET /api/work-entries and /api/timesheets ?employee_id=: one employee's rows
(Work > employee detail screen), still limited to what the caller may see.

    cd backend && venv/Scripts/python -m unittest tests.test_work_employee_filter -v

Real routes against the dev DB inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

from app import crud, models  # noqa: E402
from tests.test_qa_employees_org import IC, _QABase  # noqa: E402

DAY = datetime.date(2026, 9, 23)


class WorkEmployeeFilter(_QABase):
    def setUp(self):
        super().setUp()
        self.project = self.db.scalar(select(models.Project).where(models.Project.company_id == self.company_id).limit(1))
        if self.project is None:
            self.skipTest("no project")
        self.alice = self.new_employee(first_name="Alice")
        self.bob = self.new_employee(first_name="Bob")
        for emp, hours in ((self.alice, 3), (self.bob, 5)):
            ts = crud.create_timesheet(self.db, self.company_id, emp.id, DAY - datetime.timedelta(days=DAY.weekday()),
                                       0.0, 0.0, status="draft")
            crud.create_work_entry(self.db, ts.id, self.project.id, DAY, category="General", hours=hours,
                                   description=f"{emp.first_name} work", task_name=f"{emp.first_name} task")
        self.db.flush()

    def get(self, path, **params):
        params.setdefault("from_date", str(DAY))
        params.setdefault("to_date", str(DAY))
        r = self.client.get(path, params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_owner_gets_only_that_employee(self):
        rows = self.get("/api/work-entries", employee_id=str(self.alice.id))
        self.assertEqual({r["employee_id"] for r in rows}, {str(self.alice.id)})
        self.assertEqual([r["description"] for r in rows], ["Alice work"])
        sheets = self.get("/api/timesheets", employee_id=str(self.bob.id))
        self.assertEqual({s["employee_id"] for s in sheets}, {str(self.bob.id)})
        # Without the filter both are there, as before.
        both = {r["employee_id"] for r in self.get("/api/work-entries")}
        self.assertTrue({str(self.alice.id), str(self.bob.id)} <= both)

    def test_self_service_user_never_sees_someone_else(self):
        self.as_(self.login(self.alice, IC))
        self.assertEqual(self.get("/api/work-entries", employee_id=str(self.bob.id)), [])
        self.assertEqual(self.get("/api/timesheets", employee_id=str(self.bob.id)), [])
        own = self.get("/api/work-entries", employee_id=str(self.alice.id))
        self.assertEqual([r["description"] for r in own], ["Alice work"])
        self.assertEqual(self.get("/api/work-entries", employee_id=str(uuid.uuid4())), [])


if __name__ == "__main__":
    unittest.main()
