"""Form Configuration x Work Entry (category + custom fields) and Task
(column, story points, tag, custom fields).

    cd backend && venv/Scripts/python -m unittest tests.test_form_config_work -v

Router functions called directly in a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app import crud, form_config as fc, models, schemas, work_rules  # noqa: E402
from app.routers import form_config as cfg_api  # noqa: E402
from app.routers import projects as papi  # noqa: E402
from app.routers import tasks as tasks_api  # noqa: E402
from app.routers import work as work_api  # noqa: E402
from tests.test_qa_attendance_work import DAY, _QABase  # noqa: E402


class FormConfigWorkTests(_QABase):
    def setUp(self):
        super().setUp()
        settings_row = crud.get_company_settings(self.db, self.company_id)
        if settings_row is not None:
            settings_row.work_entry_backdate_days = None
        self.project = crud.create_project(self.db, self.company_id, f"FC Proj {uuid.uuid4().hex[:6]}", "active")
        crud.create_project_allocation(self.db, self.project.id, self.emp.id, 0, start_date=self.today - 30 * DAY)
        self.day = self.today - DAY if self.today.weekday() > 0 else self.today
        self.week = work_rules.week_start_of(self.day)
        ts_ids = select(models.Timesheet.id).where(models.Timesheet.employee_id == self.emp.id,
                                                   models.Timesheet.week_start == self.week)
        self.db.execute(delete(models.WorkEntry).where(models.WorkEntry.timesheet_id.in_(ts_ids)))
        self.db.execute(delete(models.Timesheet).where(models.Timesheet.employee_id == self.emp.id,
                                                       models.Timesheet.week_start == self.week))
        self.db.flush()
        self.admin = self.owner
        self.n = 0

    # ── helpers ──
    def cfg(self, fn, *a, **k):
        return fn(*a, db=self.db, current_user=self.admin, **k)

    def add_option(self, field_key, label):
        return self.cfg(cfg_api.create_option, cfg_api.OptionCreate(field_key=field_key, label=label))

    def option_row(self, field_key, value):
        return next(r for r in fc._rows(self.db, self.company_id)[field_key] if r.value == value)

    def make_field(self, form, label, ftype, **kw):
        return self.cfg(cfg_api.create_custom_field,
                        cfg_api.CustomFieldCreate(form_key=form, label=label, field_type=ftype, **kw))

    def entry(self, category="Development", hours=1, custom=None, **kw):
        self.n += 1
        return work_api.create_work_entry(schemas.WorkEntryCreate(
            employee_id=self.emp.id, entry_date=self.day, project_id=self.project.id, task=f"t{self.n}",
            category=category, hours=hours, custom_fields=custom, **kw), db=self.db, current_user=self.emp_user)

    def code(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code

    def listed(self, entry_id):
        rows = work_api.list_work_entries(limit=500, offset=0, db=self.db, current_user=self.emp_user)
        return next(r for r in rows if r.id == entry_id)

    # ── work_entry.category ──
    def test_category_added_option_accepted_hidden_rejected_legacy_edit_ok(self):
        self.assertEqual(self.code(self.entry, "Brainstorming"), 422)
        self.add_option("work_entry.category", "Brainstorming")
        e = self.entry("brainstorming")
        self.assertEqual(e.category, "Brainstorming")
        # rename: stored value stays, label follows
        self.cfg(cfg_api.update_option, self.option_row("work_entry.category", "Brainstorming").id,
                 cfg_api.OptionUpdate(label="Ideation"))
        self.assertEqual(self.listed(e.id).category_label, "Ideation")
        self.assertEqual(self.listed(e.id).category, "Brainstorming")
        # hide: new entries rejected, the existing entry still edits (legacy value kept)
        self.cfg(cfg_api.update_option, self.option_row("work_entry.category", "Brainstorming").id,
                 cfg_api.OptionUpdate(is_active=False))
        self.assertEqual(self.code(self.entry, "Brainstorming"), 422)
        out = work_api.edit_work_entry(e.id, schemas.WorkEntryFieldsUpdate(category="Brainstorming", hours=2),
                                       db=self.db, current_user=self.emp_user)
        self.assertEqual((out.category, out.hours), ("Brainstorming", 2))
        # changing it to something that is not offered is refused
        self.assertEqual(self.code(work_api.edit_work_entry, e.id, schemas.WorkEntryFieldsUpdate(category="Nope"),
                                   db=self.db, current_user=self.emp_user), 422)
        # a built-in option hidden by the admin is refused too
        self.cfg(cfg_api.update_option, self.option_row("work_entry.category", "QA").id,
                 cfg_api.OptionUpdate(is_active=False))
        self.assertEqual(self.code(self.entry, "QA"), 422)

    def test_category_is_optional(self):
        # Category / task are optional now; blank is stored as no category.
        entry = schemas.WorkEntryCreate(employee_id=self.emp.id, entry_date=self.day, project_id=self.project.id,
                                        task="", category="", hours=1)
        self.assertFalse(entry.category)
        with self.assertRaises(ValidationError):  # hours stay essential
            schemas.WorkEntryCreate(employee_id=self.emp.id, entry_date=self.day, project_id=self.project.id)

    # ── work_entry custom fields ──
    def test_work_entry_custom_fields(self):
        ticket = self.make_field("work_entry", "Ticket No", "text", is_required=True)
        kind = self.make_field("work_entry", "Work Kind", "dropdown", options=["Planned", "Ad hoc"])
        self.make_field("work_entry", "Remote", "checkbox")
        self.make_field("work_entry", "Effort", "number")
        self.make_field("work_entry", "Due", "date")
        self.assertEqual(ticket.key, "ticket_no")
        self.assertEqual(self.code(self.entry), 422)                                   # required missing
        self.assertEqual(self.code(self.entry, custom={"ticket_no": " "}), 422)        # required blank
        self.assertEqual(self.code(self.entry, custom={"ticket_no": "A", "work_kind": "Other"}), 422)
        self.assertEqual(self.code(self.entry, custom={"ticket_no": "A", "effort": "abc"}), 422)
        self.assertEqual(self.code(self.entry, custom={"ticket_no": "A", "due": "31/12/2026"}), 422)
        self.assertEqual(self.code(self.entry, custom={"ticket_no": "A", "remote": "maybe"}), 422)
        self.assertEqual(self.code(self.entry, custom={"ticket_no": "A", "ghost": "1"}), 422)
        e = self.entry(custom={"ticket_no": "T-1", "work_kind": "planned", "remote": True, "effort": "2.50",
                               "due": "2026-12-31"})
        self.assertEqual(e.custom_fields, {"ticket_no": "T-1", "work_kind": "Planned", "remote": "true",
                                           "effort": "2.5", "due": "2026-12-31"})
        self.assertEqual(self.listed(e.id).custom_fields["ticket_no"], "T-1")
        # edit: partial (required field not resent is fine), change + clear
        out = work_api.edit_work_entry(e.id, schemas.WorkEntryFieldsUpdate(custom_fields={"work_kind": "Ad hoc", "effort": ""}),
                                       db=self.db, current_user=self.emp_user)
        self.assertEqual(out.custom_fields["work_kind"], "Ad hoc")
        self.assertNotIn("effort", out.custom_fields)
        self.assertEqual(out.custom_fields["ticket_no"], "T-1")
        self.assertEqual(self.code(work_api.edit_work_entry, e.id,
                                   schemas.WorkEntryFieldsUpdate(custom_fields={"ticket_no": ""}),
                                   db=self.db, current_user=self.emp_user), 422)
        self.assertEqual(self.listed(e.id).custom_fields["work_kind"], "Ad hoc")

    def test_work_entry_custom_values_are_tenant_scoped(self):
        f = self.make_field("work_entry", "Ticket", "text")
        e = self.entry(custom={"ticket": "X-9"})
        other_company = uuid.uuid4()
        self.assertEqual(fc.load_custom_values(self.db, other_company, "work_entry", [e.id]), {})
        self.assertEqual(fc.custom_field_defs(self.db, other_company, "work_entry"), [])
        # a field of another form never validates here
        self.assertEqual(self.code(fc.validate_custom_values, self.db, self.company_id, "task", {f.key: "1"}), 422)

    # ── task ──
    def task(self, column="Backlog", **kw):
        self.n += 1
        body = dict(project_name=self.project.name, column_key=column, title=f"FC task {self.n}")
        body.update(kw)
        return papi.create_task_board_card(schemas.TaskBoardCardCreate(**body), db=self.db, current_user=self.admin)

    def test_task_story_points_and_tag_persist_and_validate(self):
        card = self.task(story_points=5, tag="backend")
        self.assertEqual((card.story_points, card.tag), (5, "backend"))
        board = papi.get_task_board(db=self.db, current_user=self.admin)
        shown = next(c for col in board.values() for c in col if c.id == card.id)
        self.assertEqual((shown.story_points, shown.tag), (5, "backend"))
        detail = papi.get_task_board_card(card.id, db=self.db, current_user=self.admin)
        self.assertEqual((detail["story_points"], detail["tag"]), (5, "backend"))
        # blank -> none
        plain = self.task()
        self.assertEqual((plain.story_points, plain.tag), (None, "—"))
        for bad in ({"story_points": -1}, {"story_points": 5000}, {"tag": "x" * 61}):
            with self.assertRaises(ValidationError):
                schemas.TaskBoardCardCreate(project_name="p", column_key="Backlog", title="t", **bad)
            with self.assertRaises(ValidationError):
                schemas.TaskCardUpdate(**bad)
        out = papi.update_task_board_card(card.id, schemas.TaskCardUpdate(story_points=8, tag=" api "),
                                          db=self.db, current_user=self.admin)
        self.assertEqual((out["story_points"], out["tag"]), (8, "api"))
        out = papi.update_task_board_card(card.id, schemas.TaskCardUpdate(story_points=0, tag=""),
                                          db=self.db, current_user=self.admin)
        self.assertEqual((out["story_points"], out["tag"]), (0, None))

    def test_task_column_rename_hide_and_no_new_columns(self):
        # fixed list: admins cannot add a column (status is derived from it)
        self.assertEqual(self.code(self.add_option, "task.column", "Blocked Soon"), 409)
        row = self.option_row("task.column", "In Review")
        self.cfg(cfg_api.update_option, row.id, cfg_api.OptionUpdate(label="QA Review"))
        card = self.task("In Review")
        self.assertEqual((card.column_key, card.column_label), ("In Review", "QA Review"))
        board = papi.get_task_board(db=self.db, current_user=self.admin)
        self.assertEqual(next(c for c in board["In Review"] if c.id == card.id).column_label, "QA Review")
        # hide: not offered for new cards or moves, existing card keeps it
        self.cfg(cfg_api.update_option, row.id, cfg_api.OptionUpdate(is_active=False))
        self.assertEqual(self.code(self.task, "In Review"), 422)
        mover = self.task("Backlog")
        self.assertEqual(self.code(papi.move_task_board_card, mover.id, schemas.TaskBoardCardMove(column_key="In Review"),
                                   db=self.db, current_user=self.admin), 422)
        moved = papi.move_task_board_card(card.id, schemas.TaskBoardCardMove(column_key="In Review"),
                                          db=self.db, current_user=self.admin)
        self.assertEqual(moved.column_key, "In Review")
        self.assertEqual(papi.move_task_board_card(mover.id, schemas.TaskBoardCardMove(column_key="Done"),
                                                   db=self.db, current_user=self.admin).column_key, "Done")

    def test_task_custom_fields(self):
        req = self.make_field("task", "Customer", "text", is_required=True)
        self.make_field("task", "Severity", "dropdown", options=["Low", "High"])
        self.make_field("task", "Billable", "checkbox")
        self.make_field("task", "Estimate", "number")
        self.make_field("task", "Target", "date")
        self.make_field("task", "Notes", "textarea")
        self.assertEqual(req.key, "customer")
        self.assertEqual(self.code(self.task), 422)
        self.assertEqual(self.code(self.task, custom_fields={"customer": "A", "severity": "Mid"}), 422)
        self.assertEqual(self.code(self.task, custom_fields={"customer": "A", "estimate": "x"}), 422)
        self.assertEqual(self.code(self.task, custom_fields={"customer": "A", "target": "2026-13-45"}), 422)
        self.assertEqual(self.code(self.task, custom_fields={"customer": "A", "notes": "n" * 5001}), 422)
        card = self.task(custom_fields={"customer": "Acme", "severity": "high", "billable": False,
                                        "estimate": "3", "target": "2027-01-15", "notes": "line1\nline2"})
        self.assertEqual(card.custom_fields["severity"], "High")
        self.assertEqual(card.custom_fields["billable"], "false")
        board = papi.get_task_board(db=self.db, current_user=self.admin)
        self.assertEqual(next(c for col in board.values() for c in col if c.id == card.id).custom_fields["customer"], "Acme")
        detail = papi.get_task_board_card(card.id, db=self.db, current_user=self.admin)
        self.assertEqual(detail["custom_fields"]["notes"], "line1\nline2")
        out = papi.update_task_board_card(card.id, schemas.TaskCardUpdate(custom_fields={"severity": "Low"}),
                                          db=self.db, current_user=self.admin)
        self.assertEqual((out["custom_fields"]["severity"], out["custom_fields"]["customer"]), ("Low", "Acme"))
        # same value again is "no changes"; invalid / required-cleared are 422
        self.assertEqual(self.code(papi.update_task_board_card, card.id,
                                   schemas.TaskCardUpdate(custom_fields={"severity": "Low"}),
                                   db=self.db, current_user=self.admin), 400)
        self.assertEqual(self.code(papi.update_task_board_card, card.id,
                                   schemas.TaskCardUpdate(custom_fields={"customer": ""}),
                                   db=self.db, current_user=self.admin), 422)
        self.assertEqual(self.code(papi.update_task_board_card, card.id,
                                   schemas.TaskCardUpdate(custom_fields={"severity": "Nope"}),
                                   db=self.db, current_user=self.admin), 422)
        # tenant scoping
        self.assertEqual(fc.load_custom_values(self.db, uuid.uuid4(), "task", [card.id]), {})
        # workflow detail endpoints carry them too
        self.assertEqual(tasks_api.detail_out(self.db, self.admin, card.id)["custom_fields"]["customer"], "Acme")


if __name__ == "__main__":
    unittest.main()
