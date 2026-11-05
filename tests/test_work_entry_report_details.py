"""Work Entry report: every entry's complete task name and description
(Task / Description columns), never shortened -- also through PDF export.

    cd backend && venv/Scripts/python -m unittest tests.test_work_entry_report_details -v
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete, select  # noqa: E402

from app import crud, models, report_export  # noqa: E402
from app.routers import reports as reports_api  # noqa: E402
from tests.test_qa_attendance_work import DAY, _QABase  # noqa: E402

LONG = ("Investigated the D365 posting profile mismatch for the vendor settlement, "
        "re-ran the batch job for 3 legal entities, compared ledger balances line by line, "
        "documented the fix in the runbook and shared it with the finance team for sign-off. ") * 4


class WorkEntryReportDetailsTests(_QABase):
    def setUp(self):
        super().setUp()
        self.day = self.today - 90 * DAY
        week = self.day - datetime.timedelta(days=self.day.weekday())
        self.project = crud.create_project(self.db, self.company_id, f"QA WER {uuid.uuid4().hex[:6]}", "active")
        self.card = models.TaskBoardCard(id=uuid.uuid4(), company_id=self.company_id, project_id=self.project.id,
                                         code=f"T{uuid.uuid4().hex[:6]}", title="Vendor settlement fix")
        self.db.add(self.card)
        old = select(models.Timesheet.id).where(models.Timesheet.employee_id == self.emp.id,
                                                models.Timesheet.week_start == week)
        self.db.execute(delete(models.WorkEntry).where(models.WorkEntry.timesheet_id.in_(old)))
        self.db.execute(delete(models.Timesheet).where(models.Timesheet.employee_id == self.emp.id,
                                                       models.Timesheet.week_start == week))
        ts = models.Timesheet(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                              week_start=week, status="approved")
        self.db.add(ts)
        self.db.flush()
        for task_name, task_id, desc, hours in (
            ("Settlement analysis and documentation for vendor posting profiles", None, LONG, 6),
            (None, self.card.id, "Pair session", 1.5),          # task name from the linked card
            (None, None, None, 0.5),                            # neither: shown as —
        ):
            self.db.add(models.WorkEntry(id=uuid.uuid4(), timesheet_id=ts.id, project_id=self.project.id,
                                         task_id=task_id, task_name=task_name, description=desc,
                                         entry_date=self.day, hours=hours, category="development",
                                         is_billable=True))
        self.db.flush()

    def report(self):
        return reports_api.work_entry_report(
            from_date=self.day, to_date=self.day, branch_id=None, department_id=None, designation_id=None,
            manager_id=None, employee_id=[self.emp.id], project_id=self.project.id, status=None,
            compare_previous=False, db=self.db, current_user=self.owner,
            access=crud.report_access(self.db, self.owner))

    def test_task_and_description_complete(self):
        out = self.report()
        self.assertEqual(out.columns, ["Employee", "Department", "Branch", "Date", "Project", "Task", "Description",
                                       "Category", "Hours", "Billable", "Timesheet Status"])
        t, d = out.columns.index("Task"), out.columns.index("Description")
        by_task = {r[t]: r for r in out.rows}
        self.assertEqual(len(out.rows), 3)
        full = by_task["Settlement analysis and documentation for vendor posting profiles"]
        self.assertEqual(full[d], LONG.strip())                       # every character, not cut
        self.assertEqual(by_task["Vendor settlement fix"][d], "Pair session")
        self.assertEqual(by_task["—"][d], "—")
        # Totals unchanged by the new columns.
        self.assertEqual((out.summary["Total Hours"], out.summary["Entries"]), ("8.0", "3"))

    def test_export_carries_the_full_text(self):
        data = report_export.build_report_data(self.db, self.owner, "work-entry", {
            "from_date": self.day.isoformat(), "to_date": self.day.isoformat(),
            "employee_id": str(self.emp.id), "project_id": str(self.project.id)})
        d = data["columns"].index("Description")
        self.assertIn(LONG.strip(), [r[d] for r in data["rows"]])


if __name__ == "__main__":
    unittest.main()
