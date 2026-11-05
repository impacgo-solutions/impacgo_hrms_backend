"""HRMS enhancements (Employee Dropdowns and Project Currency):
1. assignment dropdowns list every active employee -- any employment type,
   with or without a login;
2. a project budget is an amount + its own currency code (never converted);
3. an employee's Projects follow their ACTIVE assignments, and a Completed
   project refuses new work entries / timesheet submissions (direct API too).

    cd backend && venv/Scripts/python -m unittest tests.test_project_enhancements -v

Real routes against the dev DB inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crud, models, work_rules  # noqa: E402
from tests.test_qa_employees_org import IC, _QABase  # noqa: E402

DAY = datetime.date(2026, 10, 14)  # a Wednesday
WEEK = DAY - datetime.timedelta(days=DAY.weekday())


class ProjectEnhancements(_QABase):
    def new_project(self, **body) -> dict:
        r = self.client.post("/api/projects", json={"name": "QA Proj " + uuid.uuid4().hex[:6], **body})
        self.assertIn(r.status_code, (200, 201), r.text)
        pid = r.json()["id"]
        return next(p for p in self.client.get("/api/projects").json() if p["id"] == pid)

    # ── 1. dropdown candidates ──
    def test_candidates_include_every_active_employment_type(self):
        freelancer = self.new_employee(first_name="Freya", employment_type="Freelancer")   # no login account
        contractor = self.new_employee(first_name="Conrad", employment_type="Contract")
        part_timer = self.new_employee(first_name="Paula", employment_type="Part-time")
        exited = self.new_employee(first_name="Ex", status="Exited", is_active=False)
        r = self.client.get("/api/projects/employee-candidates")
        self.assertEqual(r.status_code, 200, r.text)
        ids = {c["id"] for c in r.json()}
        for e in (freelancer, contractor, part_timer):
            self.assertIn(str(e.id), ids)
        self.assertNotIn(str(exited.id), ids)
        # A Freelancer can be saved as Project Manager.
        proj = self.new_project(project_manager_id=str(freelancer.id))
        self.assertEqual(proj["pm_employee_id"], str(freelancer.id))

    # ── 2. budget currency ──
    def test_budget_amount_and_currency_stored_separately(self):
        proj = self.new_project(budget="12,500", budget_currency="usd")
        self.assertEqual(proj["budget_amount"], 12500)
        self.assertEqual(proj["budget_currency"], "USD")
        self.assertEqual(proj["budget"], "$12,500")
        row = crud.get_project_budget(self.db, uuid.UUID(proj["id"]))
        self.assertEqual((float(row.planned_amount), row.currency_code), (12500.0, "USD"))
        # Changing only the currency relabels the amount -- never converts it.
        r = self.client.patch(f"/api/projects/{proj['id']}", json={"budget_currency": "EUR"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["budget_amount"], r.json()["budget_currency"], r.json()["budget"]),
                         (12500, "EUR", "€12,500"))
        r = self.client.patch(f"/api/projects/{proj['id']}", json={"budget": "4000", "budget_currency": "AED"})
        self.assertEqual(r.json()["budget"], "AED 4,000")

    def test_unknown_currency_rejected_and_none_never_assumed(self):
        r = self.client.post("/api/projects", json={"name": "QA Bad Cur", "budget": "100", "budget_currency": "XYZ"})
        self.assertEqual(r.status_code, 422, r.text)
        proj = self.new_project(budget="100")
        self.assertIsNone(proj["budget_currency"])
        self.assertEqual(proj["budget"], "100")   # no symbol assumed

    # ── 3a. visibility follows active assignments ──
    def test_projects_follow_active_assignments(self):
        emp = self.new_employee(first_name="Ivy")
        proj = self.new_project()
        pid = uuid.UUID(proj["id"])
        listed = lambda: {p["id"] for p in self.client.get("/api/projects", params={"employee_id": str(emp.id)}).json()}
        self.assertNotIn(proj["id"], listed())
        alloc = crud.create_project_allocation(self.db, pid, emp.id, 50, start_date=DAY)
        self.db.flush()
        self.assertIn(proj["id"], listed())
        alloc.is_active = False   # removed from the project
        self.db.flush()
        self.assertNotIn(proj["id"], listed())

    # ── 3b. completed project restriction ──
    def _employee_on(self, proj: dict, pct: int = 100):
        emp = self.new_employee(first_name="Wes")
        crud.create_project_allocation(self.db, uuid.UUID(proj["id"]), emp.id, pct,
                                       start_date=DAY - datetime.timedelta(days=30))
        self.db.flush()
        self.as_(self.login(emp, IC))
        return emp

    def _entry(self, emp, proj, hours=2):
        return self.client.post("/api/work-entries", json={
            "employee_id": str(emp.id), "entry_date": str(DAY), "project_id": proj["id"],
            "hours": hours, "description": "QA work"})

    def _complete(self, proj):
        p = self.db.get(models.Project, uuid.UUID(proj["id"]))
        p.status = "completed"
        self.db.flush()

    def test_completed_project_blocks_new_and_edited_work_entries(self):
        proj = self.new_project()
        emp = self._employee_on(proj)
        r = self._entry(emp, proj)
        self.assertEqual(r.status_code, 201, r.text)
        entry_id = r.json()["id"]
        self._complete(proj)
        r = self._entry(emp, proj, hours=3)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"], work_rules.COMPLETED_PROJECT_MESSAGE)
        r = self.client.patch(f"/api/work-entries/{entry_id}/edit", json={"hours": 4})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"], work_rules.COMPLETED_PROJECT_MESSAGE)
        # The earlier entry is preserved.
        mine = self.client.get("/api/work-entries", params={"employee_id": str(emp.id),
                                                            "from_date": str(DAY), "to_date": str(DAY)}).json()
        self.assertEqual([e["id"] for e in mine], [entry_id])

    def test_completed_project_does_not_block_timesheet_submission(self):
        # Work already logged before the project was completed stays valid:
        # submitting that week saves directly (no restriction). New work on
        # the Completed project is still refused (see the entry tests above).
        proj = self.new_project()
        emp = self._employee_on(proj)
        self.assertEqual(self._entry(emp, proj).status_code, 201)
        self._complete(proj)
        r = self.client.post("/api/timesheets", json={"employee_id": str(emp.id), "week_start": str(WEEK)})
        self.assertIn(r.status_code, (200, 201), r.text)
        self.assertEqual(r.json()["status"], "approved")

    def test_other_projects_unaffected(self):
        done, live = self.new_project(), self.new_project()
        emp = self._employee_on(done, pct=50)
        crud.create_project_allocation(self.db, uuid.UUID(live["id"]), emp.id, 50,
                                       start_date=DAY - datetime.timedelta(days=30))
        self.db.flush()
        self._complete(done)
        self.assertEqual(self._entry(emp, live).status_code, 201)
        r = self.client.post("/api/timesheets", json={"employee_id": str(emp.id), "week_start": str(WEEK)})
        self.assertIn(r.status_code, (200, 201), r.text)


if __name__ == "__main__":
    unittest.main()
