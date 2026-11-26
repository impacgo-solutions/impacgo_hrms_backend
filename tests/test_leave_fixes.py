"""Leave audit fixes:

  - covering a shortfall with the SAME leave type is refused (no double spend)
  - dates: over a year, crossing the leave-year end, too far back -> 422
  - a morning half day + an afternoon half day on the same date both allowed
  - approved leave that has started can't be withdrawn by the employee
  - no fake "L1 approved" in a single-step company
  - the leave list says who may decide (can_decide)
  - Clock In / Out is never blocked by approved leave
  - comp-off is used across years, oldest credit first
  - a leave type's new yearly quota flows to this year's untouched balances
  - holidays can be edited and deleted

    cd backend && venv/Scripts/python -m unittest tests.test_leave_fixes -v
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
from sqlalchemy import select  # noqa: E402

from app import crud, leave_policy, models, schemas  # noqa: E402
from app.routers import attendance as attendance_api  # noqa: E402
from app.routers import leave as leave_api  # noqa: E402
from tests import test_leave_withdrawal as base  # noqa: E402

DAY = base.DAY

D = datetime.timedelta(days=1)


class LeaveFixTests(base.LeaveWithdrawalTests):
    def apply(self, from_date, to_date=None, *, half=False, period=None, overflow=None, lt=None):
        return leave_api.create_leave_request(schemas.LeaveRequestCreate(
            employee_id=self.emp.id, leave_type_name=(lt or self.lt).name, from_date=from_date,
            to_date=to_date or from_date, reason="QA", is_half_day=half, half_day_period=period,
            overflow_leave_type_name=overflow), db=self.db, current_user=self.user)

    def status_of(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code, str(ctx.exception.detail)

    def working_day(self, start):
        """The first working day (not a weekly off / holiday) from `start`."""
        d = start
        for _ in range(30):
            if leave_policy.compute_leave_days(self.db, self.company_id, self.emp, d, d, False) > 0:
                return d
            d += D
        self.skipTest("no working day found")

    # ── balance / overflow ─────────────────────────────────────────────

    def test_overflow_with_the_same_type_is_refused(self):
        remaining = self.balance()
        start = self.working_day(DAY)
        end = start
        need = int(remaining) + 2
        while leave_policy.compute_leave_days(self.db, self.company_id, self.emp, start, end, False) < need:
            end += D
            if (end - start).days > 120:
                self.skipTest("balance too large for this test")
        fy_end = leave_policy.fiscal_window(self.db, self.company_id, start)[1]
        if end > fy_end:
            self.skipTest("range would cross the leave year")
        code, detail = self.status_of(self.apply, start, end, overflow=self.lt.name)
        self.assertEqual(code, 409)
        self.assertIn("different leave type", detail)

    # ── dates ──────────────────────────────────────────────────────────

    def test_date_rules(self):
        code, detail = self.status_of(self.apply, DAY, DAY + 400 * D)
        self.assertEqual(code, 422)
        _, fy_end = leave_policy.fiscal_window(self.db, self.company_id, DAY)
        code, detail = self.status_of(self.apply, fy_end - D, fy_end + 2 * D)
        self.assertEqual(code, 422)
        self.assertIn("leave year", detail)
        long_ago = crud.company_today(self.db, self.company_id) - 900 * D
        code, detail = self.status_of(self.apply, long_ago, long_ago)
        self.assertEqual(code, 422)
        self.assertIn("at the earliest", detail)

    def test_morning_and_afternoon_half_days_on_one_date(self):
        day = self.working_day(DAY + 3 * D)
        self.apply(day, half=True, period="morning")
        out = self.apply(day, half=True, period="afternoon")
        self.assertTrue(out.is_half_day)
        code, _ = self.status_of(self.apply, day, half=True, period="afternoon")   # same half again
        self.assertEqual(code, 409)
        code, _ = self.status_of(self.apply, day)                                 # full day clashes
        self.assertEqual(code, 409)

    # ── decisions / withdrawals ────────────────────────────────────────

    def test_started_approved_leave_cannot_be_withdrawn(self):
        leave = self.apply_and_approve(1)
        leave.from_date = leave.to_date = crud.company_today(self.db, self.company_id) - D
        self.db.flush()
        code, detail = self.status_of(self.withdraw, leave)
        self.assertEqual(code, 409)
        self.assertIn("already started", detail)

    def test_no_fake_second_level_in_a_single_step_company(self):
        if crud._has_custom_approval_workflow(self.db, self.company_id, "leave_request"):
            self.skipTest("this tenant has a multi-step leave workflow")
        out = self.apply(self.working_day(DAY + 5 * D))
        code, _ = self.status_of(leave_api.update_leave_request, out.id,
                                 schemas.LeaveRequestUpdate(status="l1_approved"),
                                 db=self.db, current_user=self.manager)
        self.assertEqual(code, 422)

    def test_edit_after_a_step_approval_restarts_the_chain(self):
        from app import approval_engine
        if not crud._has_custom_approval_workflow(self.db, self.company_id, "leave_request"):
            self.skipTest("single-step company")
        day = self.working_day(DAY + 8 * D)
        out = self.apply(day)
        decider = next((u for u in [self.manager, *self.db.scalars(select(models.User).where(
            models.User.company_id == self.company_id, models.User.status == "active")).all()]
            if u.employee_id and u.id != self.user.id and crud.can_decide_request_configurable(
                self.db, u, self.emp.id, "leave_request", out.id)), None)
        if decider is None:
            self.skipTest("no decider for step 1")
        leave_api.update_leave_request(out.id, schemas.LeaveRequestUpdate(status="approved"),
                                       db=self.db, current_user=decider)
        leave = self.db.get(models.LeaveRequest, out.id)
        if leave.status != "pending":
            self.skipTest("the workflow has a single step for this employee")
        self.assertTrue(approval_engine.approval_started(self.db, self.company_id, "leave_request", out.id))
        new_day = self.working_day(day + D)
        leave_api.edit_leave_request(out.id, schemas.LeaveRequestEdit(
            leave_type_name=self.lt.name, from_date=new_day, to_date=new_day, reason="changed"),
            db=self.db, current_user=self.user)
        self.assertFalse(approval_engine.approval_started(self.db, self.company_id, "leave_request", out.id),
                         "the edited request starts again at step 1")
        self.assertEqual(self.db.get(models.LeaveRequest, out.id).status, "pending")

    def test_engine_restart_forgets_earlier_step_approvals(self):
        from app import approval_engine
        wf = self.db.scalar(select(models.ApprovalWorkflow).where(
            models.ApprovalWorkflow.company_id == self.company_id).limit(1))
        if wf is None:
            self.skipTest("no approval workflow row to attach to")
        doc = uuid.uuid4()
        now = datetime.datetime.now(datetime.timezone.utc)
        req = models.ApprovalRequest(id=uuid.uuid4(), company_id=self.company_id, workflow_id=wf.id,
                                     doctype="leave_request", document_id=doc, status="pending",
                                     current_step=2, requested_by=self.emp.id, created_at=now)
        self.db.add(req)
        self.db.flush()
        self.db.add(models.ApprovalAction(id=uuid.uuid4(), request_id=req.id, step_order=1, actor_id=self.manager.id,
                                          action="approved", acted_at=now))
        self.db.flush()
        self.assertTrue(approval_engine.approval_started(self.db, self.company_id, "leave_request", doc))
        approval_engine.resubmit_request(self.db, self.company_id, "leave_request", doc, self.user,
                                         comments="edited", restart=True)
        self.assertFalse(approval_engine.approval_started(self.db, self.company_id, "leave_request", doc))
        self.assertEqual(req.status, "pending")

    def test_list_says_who_may_decide(self):
        out = self.apply(self.working_day(DAY + 6 * D))
        mine = [r for r in leave_api.list_leave_requests(db=self.db, current_user=self.user, status=None, limit=500, offset=0)
                if r.id == out.id]
        self.assertEqual(len(mine), 1)
        self.assertFalse(mine[0].can_decide, "never your own request")
        theirs = [r for r in leave_api.list_leave_requests(db=self.db, current_user=self.manager, status=None, limit=500, offset=0)
                  if r.id == out.id]
        if theirs:
            self.assertEqual(theirs[0].can_decide, crud.can_decide_request_readonly(
                self.db, self.manager, self.emp.id, "leave_request", out.id)
                or leave_policy.is_leave_admin(self.db, self.manager))

    # ── attendance ─────────────────────────────────────────────────────

    def test_clock_in_allowed_on_approved_leave(self):
        leave = self.apply_and_approve(1)
        self.db.execute(models.AttendanceSession.__table__.delete().where(
            models.AttendanceSession.employee_id == self.emp.id))
        tz = crud.company_tzinfo(self.db, self.company_id)
        at = datetime.datetime.combine(leave.from_date, datetime.time(10)).replace(tzinfo=tz)
        rec = crud.clock_in_out(self.db, self.company_id, self.emp.id, leave.from_date, at, "check_in")
        self.assertIsNotNone(rec.check_in)
        rec = crud.clock_in_out(self.db, self.company_id, self.emp.id, leave.from_date,
                                at + datetime.timedelta(hours=2), "check_out")
        self.assertAlmostEqual(float(rec.work_hours), 2.0, places=2)
        self.assertEqual(self.db.get(models.LeaveRequest, leave.id).status, "approved", "leave untouched")

    # ── comp-off ───────────────────────────────────────────────────────

    def test_comp_off_used_across_years_oldest_first(self):
        years = self.db.scalars(select(models.FiscalYear).where(
            models.FiscalYear.company_id == self.company_id).order_by(models.FiscalYear.start_date)).all()
        if not years:
            self.skipTest("no fiscal year")
        if len(years) < 2:
            first = years[0]
            prev = models.FiscalYear(id=uuid.uuid4(), company_id=self.company_id, name=f"QA{uuid.uuid4().hex[:6]}",
                                     start_date=first.start_date.replace(year=first.start_date.year - 1),
                                     end_date=first.start_date - D)
            self.db.add(prev)
            self.db.flush()
            years = [prev, first]
        lt = models.LeaveType(id=uuid.uuid4(), company_id=self.company_id, name=f"QA CO {uuid.uuid4().hex[:5]}",
                              code=f"Q{uuid.uuid4().hex[:4].upper()}", is_paid=True)
        self.db.add(lt)
        self.db.flush()
        old = models.LeaveAllocation(id=uuid.uuid4(), employee_id=self.emp.id, leave_type_id=lt.id,
                                     fiscal_year_id=years[-2].id, allocated_days=2, carried_forward_days=0, used_days=0)
        new = models.LeaveAllocation(id=uuid.uuid4(), employee_id=self.emp.id, leave_type_id=lt.id,
                                     fiscal_year_id=years[-1].id, allocated_days=3, carried_forward_days=0, used_days=0)
        self.db.add_all([old, new])
        self.db.flush()
        with mock.patch.object(crud, "is_earned_only_leave_type", side_effect=lambda db, t: t is not None and t.id == lt.id):
            crud.adjust_leave_allocation_used(self.db, self.emp.id, lt.id, 3, on_date=years[-1].start_date)
            self.assertEqual((float(old.used_days), float(new.used_days)), (2.0, 1.0), "old credit first")
            crud.adjust_leave_allocation_used(self.db, self.emp.id, lt.id, -1.5, on_date=years[-1].start_date)
            self.assertEqual((float(old.used_days), float(new.used_days)), (1.5, 0.0), "given back newest first")

    # ── admin ──────────────────────────────────────────────────────────

    def test_new_yearly_quota_flows_to_untouched_balances(self):
        if self.lt.max_days_per_year is None:
            self.skipTest("uncapped type")
        old_max = float(self.lt.max_days_per_year)
        today = crud.company_today(self.db, self.company_id)
        alloc = self.db.scalar(select(models.LeaveAllocation)
                               .join(models.FiscalYear, models.FiscalYear.id == models.LeaveAllocation.fiscal_year_id)
                               .where(models.LeaveAllocation.employee_id == self.emp.id,
                                      models.LeaveAllocation.leave_type_id == self.lt.id,
                                      models.FiscalYear.start_date <= today, models.FiscalYear.end_date >= today))
        if alloc is None or abs(float(alloc.allocated_days) - old_max) > 1e-9:
            self.skipTest("this employee's allocation was set by hand")
        crud.update_leave_type(self.db, self.lt.id, max_days_per_year=old_max + 2)
        self.assertEqual(float(alloc.allocated_days), old_max + 2)

    def test_holiday_edit_and_delete(self):
        owner = next((u for u in self.db.scalars(select(models.User).where(
            models.User.company_id == self.company_id, models.User.status == "active")).all()
            if (r := crud.get_user_primary_role(self.db, u.id)) is not None and r.name == crud.BUILTIN_ROLES[0]), None)
        if owner is None:
            self.skipTest("no Owner")
        h = crud.create_holiday(self.db, self.company_id, DAY + 40 * D, f"QA Day {uuid.uuid4().hex[:4]}")
        out = attendance_api.update_holiday(h.id, schemas.HolidayUpdate(name="QA Renamed", is_optional=True),
                                            db=self.db, current_user=owner)
        self.assertEqual((out.name, out.is_optional), ("QA Renamed", True))
        attendance_api.delete_holiday(h.id, db=self.db, current_user=owner)
        self.assertIsNone(self.db.get(models.Holiday, h.id))


# The inherited LeaveWithdrawalTests cases already run in their own module.
for _name in [n for n in dir(base.LeaveWithdrawalTests) if n.startswith("test_")]:
    if _name in base.LeaveWithdrawalTests.__dict__:
        setattr(LeaveFixTests, _name, None)


if __name__ == "__main__":
    unittest.main()
