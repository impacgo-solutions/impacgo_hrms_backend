"""Work Entry > Project > "Other": log time on an active company project the
employee isn't assigned to; flagged, counted in timesheets and reports, and
the assigned-project rule (M-25) unchanged for everything else.

    cd backend && venv/Scripts/python -m unittest tests.test_work_entry_other_project -v

Same setup as test_qa_attendance_work.WorkQATests (tenant acme, real users,
transaction always rolled back).
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crud, models, schemas  # noqa: E402
from app.routers import reports as reports_api  # noqa: E402
from app.routers import work as work_api  # noqa: E402
from tests.test_qa_attendance_work import WorkQATests  # noqa: E402

DAY = datetime.timedelta(days=1)


class OtherProjectTests(WorkQATests):
    # The inherited WorkQATests cases also run here (assigned-project behaviour unchanged).

    def other_entry(self, project=None, hours=3, task="Helped another team", other=True):
        return work_api.create_work_entry(schemas.WorkEntryCreate(
            employee_id=self.emp.id, entry_date=self.day, project_id=(project or self.project).id, task=task,
            category="Development", hours=hours, description="Extra project work", other_project=other),
            db=self.db, current_user=self.emp_user)

    def options(self):
        return {o.id for o in work_api.other_projects(db=self.db, current_user=self.emp_user)}

    def test_other_projects_lists_only_unassigned_active_projects(self):
        assigned = crud.create_project(self.db, self.company_id, "QA Assigned", "active")
        crud.create_project_allocation(self.db, assigned.id, self.emp.id, 0, start_date=self.today - 30 * DAY)
        managed = crud.create_project(self.db, self.company_id, "QA Managed", "active")
        managed.project_manager_id = self.emp.id
        done = crud.create_project(self.db, self.company_id, "QA Done", "completed")
        archived = crud.create_project(self.db, self.company_id, "QA Archived", "active")
        archived.is_active = False
        self.db.flush()
        opts = self.options()
        self.assertIn(self.project.id, opts)            # unassigned + active -> offered
        self.assertNotIn(assigned.id, opts)             # already assigned
        self.assertNotIn(managed.id, opts)              # they manage it
        self.assertNotIn(done.id, opts)                 # completed
        self.assertNotIn(archived.id, opts)             # inactive
        other_company = [p for p in opts if self.db.get(models.Project, p).company_id != self.company_id]
        self.assertEqual(other_company, [])

    def test_entry_on_unassigned_project_via_other_is_saved_and_flagged(self):
        self.assertEqual(self.status(self.entry), 403)  # without "Other": unchanged M-25 rule
        out = self.other_entry()
        self.assertTrue(out.is_other_project)
        self.assertEqual((out.project, out.hours, out.task), (self.project.name, 3, "Helped another team"))
        row = self.db.get(models.WorkEntry, out.id)
        self.assertTrue(row.is_other_project)
        # weekly timesheet picks it up (same week's timesheet, hours counted)
        ts = self.db.get(models.Timesheet, row.timesheet_id)
        self.assertEqual((ts.employee_id, ts.week_start), (self.emp.id, self.week))
        listed = [e for e in work_api.list_work_entries(
            employee_id=self.emp.id, from_date=self.day, to_date=self.day, status=None, limit=500, offset=0,
            db=self.db, current_user=self.emp_user) if e.id == out.id]
        self.assertTrue(listed and listed[0].is_other_project)

    def test_other_on_an_assigned_project_is_just_an_assigned_entry(self):
        self.allocate()
        out = self.other_entry()
        self.assertFalse(out.is_other_project)

    def test_other_cannot_target_completed_or_inactive_or_foreign_project(self):
        done = crud.create_project(self.db, self.company_id, "QA Done 2", "completed")
        self.assertEqual(self.status(self.other_entry, done), 409)
        inactive = crud.create_project(self.db, self.company_id, "QA Inactive", "active")
        inactive.is_active = False
        self.db.flush()
        self.assertEqual(self.status(self.other_entry, inactive), 409)

    def test_daily_limits_still_apply_to_other_entries(self):
        self.other_entry(hours=20, task="big")
        self.assertEqual(self.status(self.other_entry, None, 5, "too much"), 400)  # > 24 h in a day

    def test_edit_moves_entry_to_an_other_project_and_back(self):
        self.allocate()
        e = self.entry()
        second = crud.create_project(self.db, self.company_id, "QA Second", "active")
        self.db.flush()
        self.assertEqual(self.status(work_api.edit_work_entry, e.id,
                                     schemas.WorkEntryFieldsUpdate(project_id=second.id),
                                     db=self.db, current_user=self.emp_user), 403)
        moved = work_api.edit_work_entry(e.id, schemas.WorkEntryFieldsUpdate(project_id=second.id, other_project=True),
                                         db=self.db, current_user=self.emp_user)
        self.assertTrue(moved.is_other_project)
        back = work_api.edit_work_entry(e.id, schemas.WorkEntryFieldsUpdate(project_id=self.project.id),
                                        db=self.db, current_user=self.emp_user)
        self.assertFalse(back.is_other_project)

    def test_timesheet_report_marks_other_project_hours(self):
        self.other_entry(hours=3)
        rep = reports_api.timesheet_report(
            from_date=self.day, to_date=self.day, branch_id=None, db=self.db, current_user=self.owner,
            access=crud.ReportAccess(scope="org", employee_ids=None, report_types=("timesheet",)),
            payroll_scope="none")
        proj_col = rep.columns.index("Project")
        mine = [r for r in rep.rows if r[proj_col].startswith(self.project.name)]
        self.assertTrue(mine, "project row present")
        self.assertIn("(Other project)", mine[0][proj_col])


if __name__ == "__main__":
    unittest.main()
