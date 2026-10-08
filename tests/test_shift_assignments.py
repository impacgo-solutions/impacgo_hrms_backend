"""Shift Management: one active shift per employee, reassignment, return to
a previous shift, unassign, history kept, and attendance using the latest
shift immediately.

    cd backend && venv/Scripts/python -m unittest tests.test_shift_assignments -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant impacgo-solutions by default, SHIFT_TEST_TENANT to
override). Clock times are simulated with explicit `now` values.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas, ws_manager  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import attendance as api  # noqa: E402

TENANT = os.environ.get("SHIFT_TEST_TENANT", "impacgo-solutions")
DAY = datetime.timedelta(days=1)


class ShiftAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User)).all()
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                           and r.name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("no Owner")
        self.company_id = self.owner.company_id
        self.today = crud.company_today(self.db, self.company_id)
        self.tz = crud.company_tzinfo(self.db, self.company_id)
        # Two active employees with no attendance and no leave today, so
        # check-in is a real first check-in for them.
        picked = []
        for e in self.db.scalars(select(models.Employee).where(models.Employee.company_id == self.company_id,
                                                                 models.Employee.is_active.is_(True))).all():
            has_record = self.db.scalar(select(func.count()).select_from(models.AttendanceRecord).where(
                models.AttendanceRecord.employee_id == e.id, models.AttendanceRecord.attendance_date == self.today))
            if not has_record and crud.get_blocking_leave_window(self.db, e.id, self.today) is None:
                picked.append(e)
            if len(picked) == 2:
                break
        if len(picked) < 2:
            self.skipTest("need two employees free today")
        self.emp, self.emp2 = picked
        tag = uuid.uuid4().hex[:6]
        # Distinguishable timings: A starts 09:00, B starts 14:00.
        self.a = self.make_shift(f"QA Shift A {tag}", "09:00", "18:00", breaks=(60, 2))
        self.b = self.make_shift(f"QA Shift B {tag}", "14:00", "23:00", breaks=(30, 1))

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # ── helpers ────────────────────────────────────────────────────────
    def make_shift(self, name, start, end, breaks):
        out = api.create_shift(schemas.ShiftCreate(name=name, start_time=start, end_time=end,
                                                   break_minutes=breaks[0], max_breaks=breaks[1]),
                               db=self.db, current_user=self.owner)
        return uuid.UUID(str(out.id))

    def assign(self, shift_id, *employees, from_date=None):
        return api.bulk_assign_employees_to_shift(
            shift_id, schemas.ShiftAssignmentBulkCreate(employee_ids=[e.id for e in employees], from_date=from_date),
            db=self.db, current_user=self.owner)

    def unassign(self, shift_id, *employees):
        return api.bulk_unassign_employees_from_shift(
            shift_id, schemas.ShiftAssignmentBulkUnassign(employee_ids=[e.id for e in employees]),
            db=self.db, current_user=self.owner)

    def active_rows(self, employee, on=None):
        on = on or self.today
        return self.db.scalars(select(models.ShiftAssignment).where(
            models.ShiftAssignment.employee_id == employee.id,
            crud._shift_assignment_active_on(on))).all()

    def members(self, shift_id):
        return {a.employee_id for a in crud.list_active_shift_assignments(self.db, shift_id, self.today)}

    def current_map(self):
        rows = api.list_current_shift_assignments(db=self.db, current_user=self.owner)
        return {r.employee_id: r.shift_id for r in rows}

    def at(self, hh, mm):
        return datetime.datetime.combine(self.today, datetime.time(hh, mm), tzinfo=self.tz)

    def count_shift(self, shift_id):
        return crud.count_active_shift_assignments(self.db, shift_id)

    # ── tests ──────────────────────────────────────────────────────────
    def test_reassign_moves_employee_and_keeps_one_active(self):
        self.assign(self.a, self.emp)
        self.assertEqual(self.members(self.a), {self.emp.id})
        res = self.assign(self.b, self.emp)
        self.assertEqual(res.assigned_employee_ids, [self.emp.id])
        rows = self.active_rows(self.emp)
        self.assertEqual(len(rows), 1, "exactly one active assignment after reassigning")
        self.assertEqual(rows[0].shift_id, self.b)
        self.assertEqual(self.members(self.a), set(), "removed from the previous shift")
        self.assertEqual(self.members(self.b), {self.emp.id})
        self.assertEqual((self.count_shift(self.a), self.count_shift(self.b)), (0, 1))
        self.assertEqual(self.current_map()[self.emp.id], self.b)
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today).id, self.b)
        self.assertEqual(crud.get_employee_professional(self.db, self.emp.id)["shift"],
                         self.db.get(models.Shift, self.b).name)

    def test_return_to_previous_shift_same_day(self):
        self.assign(self.a, self.emp)
        self.assign(self.b, self.emp)
        self.assign(self.a, self.emp)  # back to A via A's own checklist
        rows = self.active_rows(self.emp)
        self.assertEqual([r.shift_id for r in rows], [self.a], "the latest assignment (A) is the one active")
        self.assertEqual(self.members(self.a), {self.emp.id})
        self.assertEqual(self.members(self.b), set())
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today).id, self.a)
        # Every step is kept as a row (history), none of the superseded ones active.
        mine = self.db.scalars(select(models.ShiftAssignment).where(
            models.ShiftAssignment.employee_id == self.emp.id,
            models.ShiftAssignment.shift_id.in_([self.a, self.b]))).all()
        self.assertEqual(len(mine), 3)

    def test_history_rows_are_kept_and_only_end_the_day_before(self):
        # A row that started in the past (seeded directly: the API refuses past starts).
        past = crud.create_shift_assignment(self.db, self.emp.id, self.a, self.today - 10 * DAY)
        earlier_ended = [(r.id, r.from_date, r.to_date) for r in self.db.scalars(select(models.ShiftAssignment).where(
            models.ShiftAssignment.employee_id == self.emp.id, models.ShiftAssignment.to_date < past.from_date))]
        self.assign(self.b, self.emp)
        self.db.refresh(past)
        self.assertEqual(past.from_date, self.today - 10 * DAY, "start of history unchanged")
        self.assertEqual(past.to_date, self.today - DAY, "previous shift ends the day before -- no overlap today")
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today - 5 * DAY).id, self.a,
                         "past dates still resolve to the shift that applied then")
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today).id, self.b)
        for rid, f, t in earlier_ended:  # older history untouched
            row = self.db.get(models.ShiftAssignment, rid)
            self.assertEqual((row.from_date, row.to_date), (f, t))

    def test_unassign_takes_effect_today(self):
        self.assign(self.a, self.emp, self.emp2)
        res = self.unassign(self.a, self.emp)
        self.assertEqual(res.unassigned_employee_ids, [self.emp.id])
        self.assertEqual(self.members(self.a), {self.emp2.id})
        self.assertEqual(self.active_rows(self.emp), [])
        self.assertNotIn(self.emp.id, self.current_map())
        self.assertIsNone(crud._get_active_shift(self.db, self.emp.id, self.today))
        self.assertEqual(crud.get_employee_professional(self.db, self.emp.id)["shift"], "—",
                         "profile no longer shows an ended shift as current")
        # Unassigning from a shift they're not on is a no-op, never touches the other shift.
        self.assign(self.b, self.emp2)
        res = self.unassign(self.a, self.emp2)
        self.assertEqual(res.unassigned_employee_ids, [])
        self.assertEqual(self.members(self.b), {self.emp2.id})

    def test_duplicate_assign_is_skipped(self):
        self.assign(self.a, self.emp)
        res = self.assign(self.a, self.emp, self.emp)
        self.assertEqual(res.already_assigned_employee_ids, [self.emp.id])
        self.assertEqual(len(self.active_rows(self.emp)), 1)

    def test_future_assignment_waits_for_its_start(self):
        self.assign(self.a, self.emp)
        self.assign(self.b, self.emp, from_date=self.today + 3 * DAY)
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today).id, self.a,
                         "a scheduled change isn't effective before its start date")
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today + 3 * DAY).id, self.b)
        self.assertEqual(self.members(self.a), {self.emp.id})
        self.assertEqual(self.members(self.b), set())
        for d in (0, 2, 3, 10):
            self.assertEqual(len(self.active_rows(self.emp, self.today + d * DAY)), 1)

    def test_inactive_shift_is_refused(self):
        api.update_shift(self.b, schemas.ShiftUpdate(is_active=False), db=self.db, current_user=self.owner)
        with self.assertRaises(HTTPException) as ctx:
            self.assign(self.b, self.emp)
        self.assertEqual(ctx.exception.status_code, 409)

    # ── effective dates ────────────────────────────────────────────────
    def test_past_effective_date_backdates_without_touching_attendance(self):
        crud.create_shift_assignment(self.db, self.emp.id, self.a, self.today - 20 * DAY)
        # an attendance record judged under A five days ago (any real punch
        # on that date is removed first -- inside the rolled-back transaction)
        self.db.execute(text("DELETE FROM hcm_attendance_records WHERE employee_id = :e AND attendance_date = :d"),
                        {"e": self.emp.id, "d": self.today - 5 * DAY})
        rec = models.AttendanceRecord(id=uuid.uuid4(), company_id=self.company_id, employee_id=self.emp.id,
                                      attendance_date=self.today - 5 * DAY, status="late")
        self.db.add(rec); self.db.flush()
        self.assign(self.b, self.emp, from_date=self.today - 7 * DAY)
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today).id, self.b)
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today - 5 * DAY).id, self.b, "backdated")
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today - 10 * DAY).id, self.a, "before it: A")
        self.db.refresh(rec)
        self.assertEqual(rec.status, "late", "historical attendance record unchanged")
        new = crud.get_active_shift_assignment(self.db, self.emp.id, self.today)
        self.assertEqual(new.previous_shift_id, self.a)

    def test_future_effective_date_keeps_previous_shift_until_then(self):
        self.assign(self.a, self.emp)
        self.assign(self.b, self.emp, from_date=self.today + 2 * DAY)
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today).id, self.a)
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today + DAY).id, self.a)
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today + 2 * DAY).id, self.b, "switches on its date")
        # check-in today is judged by A (09:00): 10:00 is accepted (late), not refused by B's 14:00 window
        rec = crud.clock_in_out(self.db, self.company_id, self.emp.id, self.today, self.at(10, 0), "check_in")
        self.assertEqual(rec.status, "late")
        cur = {r.employee_id: r for r in api.list_current_shift_assignments(db=self.db, current_user=self.owner)}
        self.assertEqual((cur[self.emp.id].shift_id, cur[self.emp.id].upcoming_shift_id), (self.a, self.b))
        self.assertEqual(cur[self.emp.id].upcoming_from_date, self.today + 2 * DAY)

    def test_cancelling_a_scheduled_move_keeps_the_previous_shift(self):
        self.assign(self.a, self.emp)
        self.assign(self.b, self.emp, from_date=self.today + 3 * DAY)
        self.unassign(self.b, self.emp)  # untick in B's checklist = cancel the scheduled move
        for d in (0, 3, 30):
            self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today + d * DAY).id, self.a,
                             f"still on A at +{d} days")
            self.assertEqual(len(self.active_rows(self.emp, self.today + d * DAY)), 1)
        hist = api.get_shift_assignment_history(employee_id=self.emp.id, db=self.db, current_user=self.owner)
        statuses = sorted(h.status for h in hist if h.shift_id in (self.a, self.b))
        self.assertIn("cancelled", statuses)

    def test_removal_with_future_effective_date(self):
        self.assign(self.a, self.emp)
        api.bulk_unassign_employees_from_shift(
            self.a, schemas.ShiftAssignmentBulkUnassign(employee_ids=[self.emp.id], effective_date=self.today + 4 * DAY),
            db=self.db, current_user=self.owner)
        self.assertEqual(crud._get_active_shift(self.db, self.emp.id, self.today + 3 * DAY).id, self.a)
        self.assertIsNone(crud._get_active_shift(self.db, self.emp.id, self.today + 4 * DAY))

    def test_history_records_previous_shift_effective_dates_and_timestamps(self):
        self.assign(self.a, self.emp)
        self.assign(self.b, self.emp, from_date=self.today + DAY)
        hist = [h for h in api.get_shift_assignment_history(employee_id=self.emp.id, db=self.db, current_user=self.owner)
                if h.shift_id in (self.a, self.b)]
        by_shift = {h.shift_id: h for h in hist}
        b_row, a_row = by_shift[self.b], by_shift[self.a]
        self.assertEqual((b_row.status, b_row.effective_from, b_row.previous_shift_name),
                         ("scheduled", self.today + DAY, self.db.get(models.Shift, self.a).name))
        self.assertEqual((a_row.status, a_row.effective_from, a_row.effective_to), ("active", self.today, self.today))
        for h in (a_row, b_row):
            self.assertIsNotNone(h.assigned_at)
            self.assertIsNotNone(h.updated_at)

    def test_attendance_uses_latest_shift_immediately(self):
        self.assign(self.a, self.emp)
        self.assign(self.b, self.emp)  # B starts 14:00
        # Under A (09:00) a 10:00 check-in would be accepted as Late; under B
        # check-in only opens at 13:30.
        with self.assertRaises(ValueError) as ctx:
            crud.clock_in_out(self.db, self.company_id, self.emp.id, self.today, self.at(10, 0), "check_in")
        self.assertIn("1:30 PM", str(ctx.exception))
        rec = crud.clock_in_out(self.db, self.company_id, self.emp.id, self.today, self.at(14, 5), "check_in")
        self.assertEqual(rec.status, "present", "on time within B's grace period")
        policy = crud.get_break_policy(crud._get_active_shift(self.db, self.emp.id, self.today))
        self.assertEqual(policy, {"allowed_minutes": 30.0, "max_breaks": 1}, "B's break rules apply")
        # Back to A: a 15:00 check-out is now judged by A's end (18:00).
        self.assign(self.a, self.emp)
        with self.assertRaises(ValueError) as ctx:
            crud.clock_in_out(self.db, self.company_id, self.emp.id, self.today, self.at(15, 0), "check_out")
        self.assertIn("5:30 PM", str(ctx.exception))
        out = crud.clock_in_out(self.db, self.company_id, self.emp.id, self.today, self.at(18, 1), "check_out")
        self.assertEqual(out.status, "off_shift")
        self.assertEqual(crud.get_break_policy(crud._get_active_shift(self.db, self.emp.id, self.today)),
                         {"allowed_minutes": 60.0, "max_breaks": 2})

    def test_check_in_window_for_a_shift_starting_at_midnight(self):
        # Regression: 00:00 - 30 min wrapped to 23:30 and refused 00:01.
        mid = self.make_shift(f"QA midnight {uuid.uuid4().hex[:6]}", "00:00", "08:00", breaks=(10, 1))
        self.assign(mid, self.emp)
        rec = crud.clock_in_out(self.db, self.company_id, self.emp.id, self.today, self.at(0, 1), "check_in")
        self.assertEqual(rec.status, "present")

    def test_shift_changes_are_pushed_live_after_commit(self):
        sent = []
        with mock.patch.object(ws_manager.manager, "push", lambda uid, payload: sent.append((uid, payload))):
            self.assign(self.a, self.emp)
            self.assign(self.b, self.emp)  # move
            api.update_shift(self.b, schemas.ShiftUpdate(start_time="14:30"), db=self.db, current_user=self.owner)
        events = [p for _, p in sent if p.get("event") == "shift_changed"]
        company_users = set(self.db.scalars(select(models.User.id).where(
            models.User.company_id == self.company_id, models.User.status == "active")).all())
        self.assertEqual({uid for uid, _ in sent}, company_users, "every signed-in company user is told")
        per_change = len(company_users)
        self.assertEqual(len(events), 3 * per_change)
        assign_a, move_b, edit_b = events[0], events[per_change], events[2 * per_change]
        self.assertEqual((assign_a["shift_id"], assign_a["employee_ids"]), (str(self.a), [str(self.emp.id)]))
        self.assertEqual((move_b["shift_id"], move_b["employee_ids"]), (str(self.b), [str(self.emp.id)]))
        self.assertEqual((edit_b["shift_id"], edit_b["employee_ids"]), (str(self.b), None),
                         "an edited shift may affect everyone on it")
        for e in events:  # no shift values travel -- clients re-read the DB
            self.assertEqual(set(e), {"event", "shift_id", "employee_ids"})
        # A change that rolls back is never pushed.
        sent.clear()
        with mock.patch.object(ws_manager.manager, "push", lambda uid, payload: sent.append((uid, payload))):
            crud.create_shift_assignment(self.db, self.emp2.id, self.a, self.today)
            crud.queue_shift_change_push(self.db, self.company_id, self.a, [self.emp2.id])
            self.db.rollback()
            self.db.commit()
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
