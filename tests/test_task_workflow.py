"""Task Management review workflow (app/tasks.py, routers/tasks.py):
create + assign -> board summary for everyone / details only for the
assignee, their Reporting Manager and the reviewer -> Start -> Submit
(description + files + reviewer) -> Request Changes -> Resubmit -> Approve
-> Completed; validation, permission, duplicate, re-assignment, unassigned
(legacy) and tenant-isolation guards; notifications, history, file access.

    cd backend && venv/Scripts/python -m unittest tests.test_task_workflow -v

Runs inside a transaction that is ALWAYS rolled back (tenant
impacgo-solutions by default, TASK_TEST_TENANT to override); uploaded test
files are deleted in tearDown.
"""

from __future__ import annotations

import datetime
import io
import os
import shutil
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException, UploadFile  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from starlette.datastructures import Headers  # noqa: E402

from app import crud, database, email_service, media_access, models, schemas  # noqa: E402
from app import tasks as tw  # noqa: E402
from app.routers import projects as papi  # noqa: E402
from app.routers import tasks as api  # noqa: E402
from app.storage import uploads_root  # noqa: E402

TENANT = os.environ.get("TASK_TEST_TENANT", "impacgo-solutions")


def _file(name: str, data: bytes = b"%PDF-1.4 qa", ctype: str = "application/pdf") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name, headers=Headers({"content-type": ctype}))


class TaskWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        mock.patch.object(email_service._executor, "submit", lambda *a, **k: None).start()
        self.sub_dirs: list[uuid.UUID] = []

        def user_of(emp_id):
            uid = crud.get_user_id_for_employee(self.db, emp_id)
            return self.db.get(models.User, uid) if uid else None

        emps = [e for e in self.db.scalars(select(models.Employee).where(models.Employee.is_active.is_(True))).all()
                if tw.active_employee(self.db, e.company_id, e.id) and user_of(e.id)]
        self.emp = next((e for e in emps if e.reporting_manager_id and user_of(e.reporting_manager_id)), None)
        if self.emp is None:
            self.skipTest("no employee with a login and a Reporting Manager with a login")
        self.company_id = self.emp.company_id
        self.user = user_of(self.emp.id)
        self.manager = user_of(self.emp.reporting_manager_id)
        others = [e for e in emps if e.company_id == self.company_id
                  and e.id not in (self.emp.id, self.emp.reporting_manager_id)]
        if len(others) < 2:
            self.skipTest("need two more employees with logins")
        self.reviewer, self.outsider = user_of(others[0].id), user_of(others[1].id)
        self.creator = next(u for u in self.db.scalars(select(models.User).where(
            models.User.company_id == self.company_id)).all() if tw.can_edit(self.db, u))
        self.project = self.db.scalars(select(models.Project).where(
            models.Project.company_id == self.company_id)).first()
        if self.project is None:
            self.skipTest("no project")

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()
        for sid in self.sub_dirs:
            shutil.rmtree(uploads_root() / tw.SUBMISSION_ENTITY_TYPE / str(sid), ignore_errors=True)

    # ── helpers ──
    def deadline(self, days=3):
        return (datetime.datetime.now(tw.IST) + datetime.timedelta(days=days)).replace(second=0, microsecond=0)

    def create(self, assignee=True, **kw):
        body = dict(project_name=self.project.name, column_key="Backlog", title=f"QA task {uuid.uuid4().hex[:6]}",
                    description="Secret details for the assignee")
        if assignee:
            body.update(assignee_employee_id=self.emp.id, assigned_date=tw.today_ist(), deadline_at=self.deadline())
        body.update(kw)
        return papi.create_task_board_card(schemas.TaskBoardCardCreate(**body), db=self.db, current_user=self.creator)

    def detail(self, task_id, user):
        return papi.get_task_board_card(task_id, db=self.db, current_user=user)

    def submit(self, task_id, user=None, description="Implemented the fix and verified it end to end.",
               reviewer=None, files=None):
        out = api.submit_task(task_id, description=description, reviewer_employee_id=str(reviewer) if reviewer else None,
                              files=files if files is not None else [_file("result.pdf")], db=self.db,
                              current_user=user or self.user)
        self.sub_dirs += [s["id"] for s in out["submissions"]]
        return out

    def review(self, task_id, decision, user, comments=None):
        return api.review_task(task_id, schemas.TaskReviewDecision(decision=decision, comments=comments),
                               db=self.db, current_user=user)

    def status_of(self, code, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        self.assertEqual(ctx.exception.status_code, code, ctx.exception.detail)
        return ctx.exception.detail

    def notes(self, user, task_id):
        return self.db.scalars(select(models.Notification).where(
            models.Notification.user_id == user.id, models.Notification.entity_id == task_id)).all()

    def board_card(self, user, task_id):
        board = papi.get_task_board(db=self.db, current_user=user)
        return next(c for col in board.values() for c in col if c.id == task_id)

    # ── tests ──
    def test_full_flow_submit_changes_resubmit_approve(self):
        card = self.create()
        self.assertEqual((card.workflow_status, card.column_key), ("assigned", "Backlog"))
        # Everyone sees the summary; only the assignee / RM open the details.
        s = self.board_card(self.outsider, card.id)
        self.assertEqual(s.assignee, crud.employee_display_name(self.db, self.emp.id))
        self.assertEqual((s.assigned_date, s.status_label, s.can_view_details), (tw.today_ist(), "Assigned", False))
        self.assertIsNotNone(s.deadline_at)
        self.assertFalse(hasattr(s, "description"))
        self.status_of(403, self.detail, card.id, self.outsider)
        self.assertTrue(self.board_card(self.user, card.id).can_view_details)
        self.assertTrue(self.board_card(self.manager, card.id).can_view_details)
        d = self.detail(card.id, self.user)
        self.assertEqual(d["description"], "Secret details for the assignee")
        self.assertTrue(d["permissions"]["can_start"])
        self.assertEqual(self.detail(card.id, self.manager)["reporting_manager_id"], self.emp.reporting_manager_id)
        self.assertTrue(self.notes(self.user, card.id), "assignee notified")
        self.assertTrue(self.notes(self.manager, card.id), "reporting manager notified")
        # The column follows the workflow, not "Move to".
        self.status_of(409, papi.move_task_board_card, card.id, schemas.TaskBoardCardMove(column_key="Done"),
                       db=self.db, current_user=self.creator)
        # Submit before Start / by someone else.
        self.status_of(409, self.submit, card.id)
        self.status_of(403, api.start_task, card.id, db=self.db, current_user=self.manager)
        d = api.start_task(card.id, db=self.db, current_user=self.user)
        self.assertEqual((d["workflow_status"], d["column_key"]), ("in_progress", "In Progress"))
        self.status_of(409, api.start_task, card.id, db=self.db, current_user=self.user)
        self.status_of(403, self.submit, card.id, user=self.manager)
        self.status_of(422, self.submit, card.id, description="done")
        self.status_of(422, self.submit, card.id, reviewer=self.emp.id)  # not yourself
        self.status_of(400, self.submit, card.id, files=[_file("evil.exe", b"MZ", "application/octet-stream")])
        # Eligible reviewers: everyone active but the assignee; the RM flagged.
        opts = api.eligible_reviewers(card.id, db=self.db, current_user=self.user)
        self.assertNotIn(self.emp.id, [o.id for o in opts])
        self.assertTrue(any(o.is_reporting_manager for o in opts))
        self.status_of(403, api.eligible_reviewers, card.id, db=self.db, current_user=self.outsider)
        # Submit with a chosen reviewer + a file.
        d = self.submit(card.id, reviewer=self.reviewer.employee_id, files=[_file("result.pdf"), _file("shot.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "image/png")])
        self.assertEqual((d["workflow_status"], d["status_label"], d["column_key"]), ("submitted", "Submitted for Review", "In Review"))
        self.assertEqual(len(d["submissions"]), 1)
        sub = d["submissions"][0]
        self.assertEqual([f["file_name"] for f in sub["files"]], ["result.pdf", "shot.png"])
        self.assertTrue(all(os.path.exists(uploads_root() / f["file_url"].split("/media/", 1)[-1]) for f in sub["files"]))
        self.status_of(409, self.submit, card.id)  # no duplicate submission
        self.assertTrue(self.notes(self.reviewer, card.id), "reviewer notified")
        # The reviewer can now open the task and its files; the outsider can't.
        self.assertTrue(self.detail(card.id, self.reviewer)["permissions"]["can_review"])
        rel = sub["files"][0]["file_url"].split("/media/", 1)[-1]
        self.assertIsNone(media_access.authorize(self.db, self.reviewer, rel))
        self.assertIsNone(media_access.authorize(self.db, self.manager, rel))
        self.assertEqual(media_access.authorize(self.db, self.outsider, rel), media_access.FORBIDDEN)
        # Only the reviewer / RM review; never the assignee.
        self.status_of(403, self.review, card.id, "approve", self.user)
        self.status_of(403, self.review, card.id, "approve", self.outsider)
        self.status_of(422, self.review, card.id, "changes_requested", self.reviewer)  # comments required
        d = self.review(card.id, "changes_requested", self.reviewer, "Please add the test evidence.")
        self.assertEqual((d["workflow_status"], d["status_label"], d["column_key"]), ("changes_requested", "Changes Requested", "In Progress"))
        self.assertEqual(d["submissions"][0]["review_comments"], "Please add the test evidence.")
        self.status_of(409, self.review, card.id, "approve", self.reviewer)  # already decided
        # Resubmit (no reviewer: the Reporting Manager reviews) -> approve.
        d = self.submit(card.id, description="Added the test evidence as requested.", files=[])
        self.assertEqual(([s["round"] for s in d["submissions"]], d["reviewer_employee_id"]), ([2, 1], None))
        self.assertFalse(self.detail(card.id, self.reviewer)["permissions"]["can_review"], "past reviewer: view only")
        d = self.review(card.id, "approve", self.manager, "Good work")
        self.assertEqual((d["workflow_status"], d["column_key"], d["progress_pct"]), ("completed", "Done", 100))
        self.assertIsNotNone(d["completed_at"])
        task = self.db.get(models.TaskBoardCard, card.id)
        self.assertEqual((task.status, task.completed_by), ("done", self.manager.employee_id))
        self.assertEqual(
            [h["action"] for h in reversed(d["history"])],
            ["assigned", "started", "submitted", "changes_requested", "submitted", "approved"])
        self.assertTrue(any("approved" in n.body for n in self.notes(self.user, card.id)))
        # Completed tasks are final.
        self.status_of(409, papi.update_task_board_card, card.id, schemas.TaskCardUpdate(title="x"),
                       db=self.db, current_user=self.creator if tw.can_view_details(self.db, self.creator, task) else self.manager)
        self.status_of(409, api.start_task, card.id, db=self.db, current_user=self.user)

    def test_schedule_and_assignee_validation(self):
        today = tw.today_ist()
        self.status_of(422, self.create, deadline_at=None)
        self.status_of(422, self.create, assigned_date=None)
        self.status_of(422, self.create, assigned_date=today + datetime.timedelta(days=1),
                       deadline_at=self.deadline(5))  # assigned date in the future
        self.status_of(422, self.create, deadline_at=datetime.datetime.combine(today, datetime.time(0, 0), tw.IST)
                       - datetime.timedelta(hours=1))  # before the assigned date
        self.status_of(422, self.create, deadline_at=datetime.datetime.now(tw.IST) - datetime.timedelta(minutes=5))
        self.status_of(422, self.create, assignee_employee_id=uuid.uuid4())
        self.status_of(422, self.create, assignee=False, deadline_at=self.deadline())
        # A naive deadline is local (IST) time.
        naive = self.deadline().replace(tzinfo=None)
        card = self.create(deadline_at=naive)
        self.assertEqual(card.deadline_at.astimezone(tw.IST).replace(tzinfo=None), naive)
        self.assertEqual(self.db.get(models.TaskBoardCard, card.id).due_date, naive.date())

    def test_unassigned_task_behaves_as_before(self):
        card = self.create(assignee=False)
        self.assertIsNone(card.workflow_status)
        self.assertTrue(self.board_card(self.outsider, card.id).can_view_details)
        self.assertEqual(self.detail(card.id, self.outsider)["description"], "Secret details for the assignee")
        moved = papi.move_task_board_card(card.id, schemas.TaskBoardCardMove(column_key="Done"),
                                          db=self.db, current_user=self.creator)
        self.assertEqual(moved.column_key, "Done")

    def test_reassign_and_change_reviewer(self):
        card = self.create()
        api.start_task(card.id, db=self.db, current_user=self.user)
        d = self.submit(card.id, reviewer=self.reviewer.employee_id)
        api_change = api.change_reviewer
        self.status_of(403, api_change, card.id, schemas.TaskReviewerChange(reviewer_employee_id=self.outsider.employee_id),
                       db=self.db, current_user=self.manager)
        d = api_change(card.id, schemas.TaskReviewerChange(reviewer_employee_id=self.outsider.employee_id),
                       db=self.db, current_user=self.user)
        self.assertEqual(d["reviewer_employee_id"], self.outsider.employee_id)
        self.assertTrue(self.notes(self.outsider, card.id), "new reviewer notified")
        self.assertTrue(self.detail(card.id, self.outsider)["permissions"]["can_review"])
        # The replaced reviewer never reviewed it: no access any more.
        self.status_of(403, self.detail, card.id, self.reviewer)
        # Re-assigning while under review is refused; after review it restarts.
        editor = self.manager if tw.can_edit(self.db, self.manager) else self.creator
        if not tw.can_view_details(self.db, editor, self.db.get(models.TaskBoardCard, card.id)):
            self.skipTest("no user who can both edit and open this task")
        new = schemas.TaskCardUpdate(assignee_employee_id=self.reviewer.employee_id)
        self.status_of(409, papi.update_task_board_card, card.id, new, db=self.db, current_user=editor)
        self.review(card.id, "changes_requested", self.outsider, "Not yet")
        # Deadline change is validated and recorded.
        self.status_of(422, papi.update_task_board_card, card.id,
                       schemas.TaskCardUpdate(deadline_at=datetime.datetime.now(tw.IST) - datetime.timedelta(hours=1)),
                       db=self.db, current_user=editor)
        d = papi.update_task_board_card(card.id, schemas.TaskCardUpdate(deadline_at=self.deadline(7)),
                                        db=self.db, current_user=editor)
        self.assertIn("deadline_changed", [h["action"] for h in d["history"]])
        self.assertEqual(d["workflow_status"], "changes_requested", "a reschedule keeps the status")
        d = papi.update_task_board_card(card.id, new, db=self.db, current_user=editor)
        self.assertEqual((d["workflow_status"], d["assignee_employee_id"]), ("assigned", self.reviewer.employee_id))
        self.assertIn("reassigned", [h["action"] for h in d["history"]])
        self.assertTrue(any(n.title == "New task assigned to you" for n in self.notes(self.reviewer, card.id)))
        # The previous assignee no longer opens it (unless they review/manage it).
        if self.db.get(models.Employee, self.reviewer.employee_id).reporting_manager_id != self.user.employee_id:
            self.status_of(403, self.detail, card.id, self.user)

    def test_tenant_isolation(self):
        card = self.create()
        foreign = models.User(id=uuid.uuid4(), company_id=uuid.uuid4(), employee_id=self.emp.id, email="x@y.z")
        self.status_of(404, tw.require_view, self.db, foreign, self.db.get(models.TaskBoardCard, card.id))
        self.assertFalse(tw.can_view_details(self.db, foreign, self.db.get(models.TaskBoardCard, card.id)))


if __name__ == "__main__":
    unittest.main()
