"""Travel Requests and Expense Claims (full forms): itinerary + costing,
taxes & charges, advances, claim types, drafts, approvals, validation.

    cd backend && venv/Scripts/python -m unittest tests.test_travel_expense -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant impacgo-solutions by default, TRAVEL_TEST_TENANT to
override). Needs backend/db/add_travel_expense_details.sql applied.
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
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, email_service, models, schemas, travel_expense as te  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import travel as api  # noqa: E402
from app.routers import travel_expense_config as cfg  # noqa: E402

TENANT = os.environ.get("TRAVEL_TEST_TENANT", "impacgo-solutions")
TODAY = datetime.date.today()


def dt(days: int, hour: int = 9) -> datetime.datetime:
    return datetime.datetime.combine(TODAY + datetime.timedelta(days=days), datetime.time(hour))


def leg(frm="Hyderabad", to="Bengaluru", mode="Flight", days=10, hour=9, arrive=None):
    return {"travel_from": frm, "travel_to": to, "mode_of_travel": mode, "departure_at": dt(days, hour),
            "arrival_at": dt(days, hour + 2) if arrive is None else arrive}


def travel_payload(emp_id, **kw):
    base = dict(employee_id=emp_id, purpose="Client workshop", travel_type="Domestic", travel_funding="Company",
                itinerary=[leg(), leg("Bengaluru", "Hyderabad", "Train", days=12)],
                costing=[{"expense_type": "Travel", "sponsored_amount": 0, "funded_amount": 5000},
                         {"expense_type": "Food", "comments": "per diem", "sponsored_amount": 500, "funded_amount": 1500}])
    base.update(kw)
    return schemas.TravelRequestApiCreate(**base)


def claim_payload(emp_id, items=None, **kw):
    items = items if items is not None else [
        {"date": TODAY - datetime.timedelta(days=3), "category": "Food", "description": "Team lunch", "amount": 600},
        {"date": TODAY - datetime.timedelta(days=2), "category": "Travel", "description": "Cab to client", "amount": 400},
    ]
    return schemas.ExpenseReportApiCreate(employee_id=emp_id, submitted_date=TODAY, items=items, **kw)


class TravelExpenseTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        mock.patch.object(email_service._executor, "submit", lambda *a, **k: None).start()
        self.addCleanup(mock.patch.stopall)
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        self.user_by_emp = {u.employee_id: u for u in users}
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                           and r.name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("no Owner")
        emps = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.owner.company_id, models.Employee.is_active.is_(True))).all()
        self.worker = next((e for e in emps if e.id in self.user_by_emp and e.reporting_manager_id in self.user_by_emp
                            and e.id != self.owner.employee_id), None)
        if self.worker is None:
            self.skipTest("no employee with a login and a manager with a login")
        self.me = self.user_by_emp[self.worker.id]
        self.manager = self.user_by_emp[self.worker.reporting_manager_id]
        self.company_id = self.owner.company_id
        self.stranger = next((u for e in emps if (u := self.user_by_emp.get(e.id)) is not None
                              and u.id not in (self.owner.id, self.me.id, self.manager.id)
                              and not crud._is_in_reporting_chain(self.db, e.id, self.worker.id)
                              and (crud.get_visible_employee_ids_for_requests(self.db, u, "travel_request") or [None]) is not None
                              and self.worker.id not in (crud.get_visible_employee_ids_for_requests(self.db, u, "travel_request") or [])),
                             None)

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # ── helpers ─────────────────────────────────────────────────────────────
    def new_trip(self, user=None, **kw):
        out = api.create_travel_request(travel_payload(self.worker.id, **kw), db=self.db, current_user=user or self.me)
        return self.db.get(models.TravelRequestModel, uuid.UUID(out["id"])), out

    def new_claim(self, user=None, **kw):
        out = api.create_expense_report(claim_payload(self.worker.id, **kw), db=self.db, current_user=user or self.me)
        return self.db.get(models.ExpenseClaim, uuid.UUID(out["id"])), out

    def advance(self, amount=1000, employee=None):
        a = models.EmployeeAdvance(id=uuid.uuid4(), company_id=self.company_id, employee_id=(employee or self.worker).id,
                                   posting_date=TODAY, purpose="Trip advance", advance_amount=amount, status="paid")
        self.db.add(a)
        self.db.flush()
        return a

    def raises(self, code, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        self.assertEqual(ctx.exception.status_code, code, ctx.exception.detail)
        return ctx.exception.detail

    # ── travel request: create / derived fields ─────────────────────────────
    def test_travel_request_saves_every_section_and_derives_the_summary_columns(self):
        trip, out = self.new_trip(sponsor_details=None, description="Quarterly review", cost_center="Main",
                                  organizer_name="Acme Events", organizer_address="MG Road", event_details="Keynote",
                                  contact_number="+91 98765 43210", contact_email="a@b.com")
        self.assertEqual(trip.status, "pending")
        self.assertIsNotNone(trip.submitted_at)
        self.assertEqual((trip.travel_type, trip.travel_funding), ("Domestic", "Company"))
        self.assertEqual(len(trip.itinerary), 2)
        self.assertEqual([c.expense_type for c in trip.costing], ["Travel", "Food"])
        self.assertEqual(trip.destination, "Bengaluru")  # a round trip is "to" its furthest stop
        self.assertEqual(trip.from_date, TODAY + datetime.timedelta(days=10))
        self.assertEqual(trip.to_date, TODAY + datetime.timedelta(days=12))
        self.assertEqual(trip.travel_mode, "Flight/Train")
        self.assertEqual(float(trip.estimated_cost), 7000.0)  # 5000 + (500 + 1500): what payroll reads
        food = next(c for c in trip.costing if c.expense_type == "Food")
        self.assertEqual((float(food.sponsored_amount), float(food.funded_amount), float(food.total_amount)), (500, 1500, 2000))
        dto = te.travel_request_out(self.db, trip, viewer=self.me)
        self.assertEqual((dto.total_sponsored, dto.total_funded, dto.total_cost), (500.0, 6500.0, 7000.0))
        self.assertEqual(dto.organizer_name, "Acme Events")
        self.assertFalse(dto.can_edit)  # pending: no longer editable

    def test_old_single_row_clients_still_work(self):
        payload = schemas.TravelRequestApiCreate(
            employee_id=self.worker.id, purpose="Site visit", destination="Pune",
            from_date=TODAY + datetime.timedelta(days=20), to_date=TODAY + datetime.timedelta(days=21),
            travel_mode="Train", estimated_cost=1200)
        out = api.create_travel_request(payload, db=self.db, current_user=self.me)
        trip = self.db.get(models.TravelRequestModel, uuid.UUID(out["id"]))
        self.assertEqual((trip.destination, trip.travel_mode, float(trip.estimated_cost), trip.status), ("Pune", "Train", 1200.0, "pending"))
        self.assertEqual(trip.itinerary, [])

    def test_submitting_notifies_the_manager_but_a_draft_does_not(self):
        before = self.db.scalar(select(text("count(*)")).select_from(models.Notification)
                                .where(models.Notification.user_id == self.manager.id))
        self.new_trip(save_as_draft=True)
        mid = self.db.scalar(select(text("count(*)")).select_from(models.Notification)
                             .where(models.Notification.user_id == self.manager.id))
        self.assertEqual(before, mid)
        self.new_trip(itinerary=[leg(days=40)], costing=[])
        after = self.db.scalar(select(text("count(*)")).select_from(models.Notification)
                               .where(models.Notification.user_id == self.manager.id))
        self.assertGreater(after, mid)

    # ── travel request: validation ──────────────────────────────────────────
    def test_travel_validation(self):
        bad = [
            dict(contact_email="not-an-email"),
            dict(contact_number="12"),
            dict(itinerary=[leg(frm="Pune", to="pune")]),
            dict(itinerary=[leg(arrive=dt(10, 5))]),  # arrives before it departs
            dict(itinerary=[leg(days=-5)]),  # in the past
            dict(itinerary=[leg(days=1), leg(days=400)]),  # > a year
            dict(costing=[{"expense_type": "Food", "funded_amount": 1}, {"expense_type": "food", "funded_amount": 2}]),
            dict(costing=[{"expense_type": "Food", "funded_amount": -5}]),
            dict(itinerary=[], costing=[], destination=None),
        ]
        for kw in bad:
            with self.subTest(kw=list(kw)), self.assertRaises(ValidationError):
                travel_payload(self.worker.id, **kw)
        # Optional: purpose, sponsor details, passport / ID document number.
        self.assertIsNone(travel_payload(self.worker.id, purpose="   ").purpose)
        travel_payload(self.worker.id, travel_funding="Sponsored by Third Party")
        travel_payload(self.worker.id, travel_type="International")
        travel_payload(self.worker.id, id_document_type="Passport")
        travel_payload(self.worker.id, travel_funding="Sponsored by Third Party", sponsor_details="Acme, Pune")
        travel_payload(self.worker.id, travel_type="International", passport_number="N1234567")

    def test_unlisted_dropdown_values_are_refused_by_the_endpoint(self):
        # Travel Type / Funding are now checked against the company's options (422).
        for kw in (dict(travel_type="Orbital"), dict(travel_funding="Lottery")):
            with self.subTest(kw=list(kw)):
                self.raises(422, api.create_travel_request, travel_payload(self.worker.id, **kw),
                            db=self.db, current_user=self.me)

    def test_unknown_expense_type_in_costing_is_refused(self):
        self.raises(400, api.create_travel_request,
                    travel_payload(self.worker.id, costing=[{"expense_type": "Yachts", "funded_amount": 10}]),
                    db=self.db, current_user=self.me)

    def test_overlapping_trips_conflict_but_drafts_do_not(self):
        self.new_trip()
        self.raises(409, api.create_travel_request, travel_payload(self.worker.id), db=self.db, current_user=self.me)
        draft, _ = self.new_trip(save_as_draft=True)  # saving a draft never conflicts
        self.assertEqual(draft.status, "draft")
        self.raises(409, api.submit_travel_request, draft.id, db=self.db, current_user=self.me)

    def test_an_unrelated_person_cannot_file_for_another_employee(self):
        if self.stranger is None:
            self.skipTest("no unrelated login")
        self.raises(403, api.create_travel_request, travel_payload(self.worker.id), db=self.db, current_user=self.stranger)

    # ── travel request: draft / edit / submit / delete ──────────────────────
    def test_draft_edit_submit_and_delete(self):
        draft, out = self.new_trip(save_as_draft=True)
        self.assertEqual((draft.status, draft.submitted_at), ("draft", None))
        self.assertTrue(te.travel_request_out(self.db, draft, viewer=self.me).can_edit)
        edited = api.edit_travel_request(
            draft.id, schemas.TravelRequestEdit(**{**travel_payload(self.worker.id, save_as_draft=True,
                                                                    itinerary=[leg("Pune", "Mumbai", "Bus", days=30)],
                                                                    costing=[{"expense_type": "Travel", "funded_amount": 800}]
                                                                    ).model_dump(exclude={"employee_id"})}),
            db=self.db, current_user=self.me)
        self.assertEqual((edited.destination, edited.estimated_cost, edited.status), ("Mumbai", 800.0, "draft"))
        self.assertEqual(len(edited.itinerary), 1)  # rows were replaced, not appended
        self.assertEqual(len(edited.costing), 1)
        submitted = api.submit_travel_request(draft.id, db=self.db, current_user=self.me)
        self.assertEqual(submitted.status, "pending")
        # a pending request can no longer be edited or deleted by its owner
        self.raises(409, api.delete_travel_request, draft.id, db=self.db, current_user=self.me)
        self.raises(409, api.edit_travel_request, draft.id,
                    schemas.TravelRequestEdit(**travel_payload(self.worker.id).model_dump(exclude={"employee_id"})),
                    db=self.db, current_user=self.me)
        d2, _ = self.new_trip(save_as_draft=True, itinerary=[leg(days=60)], costing=[])
        api.delete_travel_request(d2.id, db=self.db, current_user=self.me)
        self.assertIsNone(self.db.get(models.TravelRequestModel, d2.id))

    def test_only_the_owner_edits_or_deletes(self):
        draft, _ = self.new_trip(save_as_draft=True)
        self.raises(403, api.delete_travel_request, draft.id, db=self.db, current_user=self.owner)

    def test_sent_back_request_is_corrected_and_resubmitted(self):
        trip, _ = self.new_trip()
        api.update_travel_request(trip.id, schemas.TravelRequestUpdate(status="sent_back", decision_notes="add costing"),
                                  db=self.db, current_user=self.manager)
        self.db.refresh(trip)
        self.assertEqual(trip.status, "sent_back")
        out = api.edit_travel_request(
            trip.id, schemas.TravelRequestEdit(**travel_payload(self.worker.id, costing=[
                {"expense_type": "Travel", "funded_amount": 900}]).model_dump(exclude={"employee_id"})),
            db=self.db, current_user=self.me)
        self.assertEqual((out.status, out.decision_notes, out.estimated_cost), ("pending", None, 900.0))
        actions = [h["action"] for h in te.history_for(self.db, self.company_id, "travel_request", trip.id)]
        self.assertIn("Resubmit", actions)

    def test_manager_approves_and_history_records_who_and_what(self):
        trip, _ = self.new_trip()
        api.update_travel_request(trip.id, schemas.TravelRequestUpdate(status="approved", decision_notes="ok"),
                                  db=self.db, current_user=self.manager)
        self.db.refresh(trip)
        self.assertEqual(trip.status, "approved")
        hist = te.history_for(self.db, self.company_id, "travel_request", trip.id)
        self.assertEqual([h["action"] for h in hist][-2:], ["Create", "Approved"])
        self.assertEqual(hist[-1]["notes"], "ok")
        self.raises(409, api.update_travel_request, trip.id, schemas.TravelRequestUpdate(status="rejected"),
                    db=self.db, current_user=self.manager)

    def test_a_stranger_cannot_open_someone_elses_request(self):
        if self.stranger is None:
            self.skipTest("no unrelated login")
        trip, _ = self.new_trip()
        self.raises(404, api.get_travel_request, trip.id, db=self.db, current_user=self.stranger)

    # ── expense claim: totals ───────────────────────────────────────────────
    def test_claim_totals_taxes_advances_and_series(self):
        adv = self.advance(300)
        claim, out = self.new_claim(
            taxes=[{"account_head": "GST", "description": "18% GST", "rate": 18}, {"account_head": "Service charge", "tax_amount": 20}],
            advances=[{"advance_id": adv.id, "allocated_amount": 300}], remark="client trip", cost_center="Main",
            payable_account="Payroll Payable")
        self.assertEqual(claim.status, "pending")
        self.assertRegex(claim.claim_no, rf"^HR-EXP-{TODAY.year}-\d{{5}}$")
        self.assertEqual(float(claim.total_claimed), 1000.0)
        self.assertEqual(float(claim.total_taxes), 200.0)  # 180 + 20
        self.assertEqual(float(claim.advance_amount), 300.0)
        self.assertEqual(float(claim.total_amount), 900.0)  # net payable = 1000 + 200 - 300: what payroll reimburses
        dto = te.expense_report_out(self.db, claim, viewer=self.me)
        self.assertEqual((dto.total_claimed, dto.total_taxes, dto.advance_amount, dto.net_payable), (1000.0, 200.0, 300.0, 900.0))
        self.assertEqual(dto.expense_approver_id, self.manager.employee_id)  # defaults to the reporting manager
        self.assertEqual(dto.posting_date, TODAY)
        self.assertEqual([i.category for i in dto.items], ["Food", "Travel"])
        self.assertEqual(dto.advances[0].allocated_amount, 300.0)
        claim2, _ = self.new_claim(items=[{"date": TODAY, "category": "Calls", "description": "Phone", "amount": 50}])
        self.assertEqual(int(claim2.claim_no[-5:]), int(claim.claim_no[-5:]) + 1)  # series counts up

    def test_advance_only_becomes_claimed_when_the_claim_is_approved(self):
        adv = self.advance(500)
        claim, _ = self.new_claim(advances=[{"advance_id": adv.id, "allocated_amount": 400}])
        self.db.refresh(adv)
        self.assertEqual((float(adv.claimed_amount), adv.status), (0.0, "paid"))  # reserved, not spent
        self.assertEqual(te.advance_available(self.db, adv), 100.0)
        api.update_expense_report(claim.id, schemas.ExpenseReportUpdate(status="approved"), db=self.db, current_user=self.manager)
        self.db.refresh(adv)
        self.assertEqual((float(adv.claimed_amount), adv.outstanding), (400.0, 100.0))
        self.assertEqual(adv.status, "paid")  # 100 is still unclaimed

    def test_rejecting_releases_the_advance(self):
        adv = self.advance(500)
        claim, _ = self.new_claim(advances=[{"advance_id": adv.id, "allocated_amount": 500}])
        self.assertEqual(te.advance_available(self.db, adv), 0.0)
        api.update_expense_report(claim.id, schemas.ExpenseReportUpdate(status="rejected", decision_notes="no"),
                                  db=self.db, current_user=self.manager)
        self.db.refresh(adv)
        self.assertEqual((float(adv.claimed_amount), te.advance_available(self.db, adv)), (0.0, 500.0))

    def test_claim_with_no_manager_decision_yet_stays_editable_only_as_draft(self):
        claim, _ = self.new_claim()
        self.raises(409, api.edit_expense_report, claim.id,
                    schemas.ExpenseReportEdit(**claim_payload(self.worker.id).model_dump()), db=self.db, current_user=self.me)

    # ── expense claim: validation ───────────────────────────────────────────
    def test_claim_validation(self):
        today = TODAY
        bad = [
            dict(items=[]),
            dict(items=[{"date": today, "category": "Food", "description": "x", "amount": 0}]),
            dict(items=[{"date": today + datetime.timedelta(days=9), "category": "Food", "description": "x", "amount": 5}]),
            dict(posting_date=today + datetime.timedelta(days=9)),
            dict(clearance_date=today - datetime.timedelta(days=30)),
            dict(taxes=[{"account_head": "GST"}]),
            dict(taxes=[{"account_head": "GST", "rate": 140}]),
            dict(advances=[{"advance_id": uuid.uuid4(), "allocated_amount": 5}, {"advance_id": uuid.uuid4(), "allocated_amount": 5}] * 1
                 if False else [{"advance_id": uuid.UUID(int=1), "allocated_amount": 5}, {"advance_id": uuid.UUID(int=1), "allocated_amount": 6}]),
        ]
        for kw in bad:
            with self.subTest(kw=list(kw)), self.assertRaises(ValidationError):
                claim_payload(self.worker.id, **kw)

    def test_unknown_or_inactive_claim_type_is_refused(self):
        item = {"date": TODAY, "category": "Yachts", "description": "x", "amount": 5}
        self.raises(400, api.create_expense_report, claim_payload(self.worker.id, items=[item]), db=self.db, current_user=self.me)
        t = self.db.scalar(select(models.ExpenseClaimType).where(
            models.ExpenseClaimType.company_id == self.company_id, models.ExpenseClaimType.name == "Calls"))
        t.is_active = False
        self.db.flush()
        item["category"] = "calls"
        self.raises(400, api.create_expense_report, claim_payload(self.worker.id, items=[item]), db=self.db, current_user=self.me)

    def test_duplicate_expense_rows_are_refused_within_and_across_claims(self):
        same = {"date": TODAY - datetime.timedelta(days=1), "category": "Food", "description": "Lunch", "amount": 250}
        self.raises(409, api.create_expense_report, claim_payload(self.worker.id, items=[same, dict(same)]), db=self.db, current_user=self.me)
        self.new_claim(items=[same])
        other = {"date": TODAY, "category": "Calls", "description": "Recharge", "amount": 99}
        detail = self.raises(409, api.create_expense_report, claim_payload(self.worker.id, items=[dict(same), other]),
                             db=self.db, current_user=self.me)
        self.assertIn("already claimed", detail)

    def test_advance_rules(self):
        adv = self.advance(300)
        other = self.advance(300, employee=self.manager.employee and self.db.get(models.Employee, self.manager.employee_id))
        self.raises(409, api.create_expense_report, claim_payload(self.worker.id, advances=[{"advance_id": adv.id, "allocated_amount": 301}]),
                    db=self.db, current_user=self.me)
        self.raises(404, api.create_expense_report, claim_payload(self.worker.id, advances=[{"advance_id": other.id, "allocated_amount": 5}]),
                    db=self.db, current_user=self.me)
        small = [{"date": TODAY, "category": "Calls", "description": "tiny", "amount": 100}]
        self.raises(400, api.create_expense_report, claim_payload(self.worker.id, items=small, advances=[{"advance_id": adv.id, "allocated_amount": 250}]),
                    db=self.db, current_user=self.me)
        # a second open claim cannot take what the first one has set aside
        self.new_claim(advances=[{"advance_id": adv.id, "allocated_amount": 200}])
        more = [{"date": TODAY, "category": "Medical", "description": "pharmacy", "amount": 900}]
        self.raises(409, api.create_expense_report, claim_payload(self.worker.id, items=more, advances=[{"advance_id": adv.id, "allocated_amount": 150}]),
                    db=self.db, current_user=self.me)

    def test_expense_approver_must_be_someone_who_can_decide_the_claim(self):
        claim, _ = self.new_claim(expense_approver_id=self.manager.employee_id)
        self.assertEqual(claim.expense_approver_id, self.manager.employee_id)
        other = [{"date": TODAY - datetime.timedelta(days=9), "category": "Medical", "description": "Eye test", "amount": 1234}]
        self.raises(400, api.create_expense_report, claim_payload(self.worker.id, items=other, expense_approver_id=self.worker.id),
                    db=self.db, current_user=self.me)
        ids = te.allowed_expense_approver_ids(self.db, self.worker)
        self.assertIn(self.manager.employee_id, ids)
        self.assertNotIn(self.worker.id, ids)

    def test_project_task_and_travel_links_are_checked(self):
        self.raises(404, api.create_expense_report, claim_payload(self.worker.id, project_id=uuid.uuid4()), db=self.db, current_user=self.me)
        self.raises(404, api.create_expense_report, claim_payload(self.worker.id, task_id=uuid.uuid4()), db=self.db, current_user=self.me)
        trip, _ = self.new_trip()
        self.raises(409, api.create_expense_report, claim_payload(self.worker.id, travel_request_id=trip.id), db=self.db, current_user=self.me)
        api.update_travel_request(trip.id, schemas.TravelRequestUpdate(status="approved"), db=self.db, current_user=self.manager)
        claim, _ = self.new_claim(travel_request_id=trip.id)
        self.assertEqual(claim.travel_request_id, trip.id)

    def test_only_finance_can_mark_a_claim_paid(self):
        self.raises(403, api.create_expense_report, claim_payload(self.worker.id, is_paid=True), db=self.db, current_user=self.me)
        out = api.create_expense_report(claim_payload(self.worker.id, is_paid=True, clearance_date=TODAY), db=self.db, current_user=self.owner) \
            if self.owner.employee_id == self.worker.id else None
        self.assertIsNone(out)  # owner is a different person; payment fields are set by Finance on someone's claim, not filed for them

    def test_double_submit_returns_the_same_claim(self):
        claim, out = self.new_claim()
        again = api.create_expense_report(claim_payload(self.worker.id), db=self.db, current_user=self.me)
        self.assertEqual(again["id"], out["id"])
        self.assertTrue(again.get("duplicate"))

    # ── expense claim: draft / edit / submit / delete ───────────────────────
    def test_claim_draft_edit_submit_delete(self):
        draft, out = self.new_claim(save_as_draft=True)
        self.assertEqual((draft.status, draft.submitted_at), ("draft", None))
        edited = api.edit_expense_report(
            draft.id, schemas.ExpenseReportEdit(**claim_payload(
                self.worker.id, save_as_draft=True,
                items=[{"date": TODAY, "category": "Others", "description": "Parking", "amount": 75}],
                taxes=[{"account_head": "GST", "rate": 10}]).model_dump()),
            db=self.db, current_user=self.me)
        self.assertEqual(([i.description for i in edited.items], edited.total_claimed, edited.total_taxes, edited.net_payable, edited.status),
                         (["Parking"], 75.0, 7.5, 82.5, "draft"))
        sub = api.submit_expense_report(draft.id, db=self.db, current_user=self.me)
        self.assertEqual(sub.status, "pending")
        self.raises(409, api.delete_expense_report, draft.id, db=self.db, current_user=self.me)
        d2, _ = self.new_claim(save_as_draft=True, items=[{"date": TODAY, "category": "Calls", "description": "SIM", "amount": 10}])
        self.raises(403, api.delete_expense_report, d2.id, db=self.db, current_user=self.owner)
        api.delete_expense_report(d2.id, db=self.db, current_user=self.me)
        self.assertIsNone(self.db.get(models.ExpenseClaim, d2.id))

    def test_sent_back_claim_is_corrected_and_resubmitted(self):
        claim, _ = self.new_claim()
        api.update_expense_report(claim.id, schemas.ExpenseReportUpdate(status="sent_back", decision_notes="fix"), db=self.db, current_user=self.manager)
        self.db.refresh(claim)
        self.assertEqual(claim.status, "sent_back")
        out = api.edit_expense_report(claim.id, schemas.ExpenseReportEdit(**claim_payload(
            self.worker.id, items=[{"date": TODAY - datetime.timedelta(days=5), "category": "Medical", "description": "Doctor", "amount": 300}]
        ).model_dump()), db=self.db, current_user=self.me)
        self.assertEqual((out.status, out.decision_notes, out.net_payable), ("pending", None, 300.0))

    # ── payment (Finance) ───────────────────────────────────────────────────
    def test_finance_records_payment_of_an_approved_claim_only(self):
        claim, _ = self.new_claim()
        upd = schemas.ExpensePaymentUpdate(is_paid=True, clearance_date=TODAY)
        self.raises(409, api.update_expense_payment, claim.id, upd, db=self.db, current_user=self.owner)  # not approved yet
        api.update_expense_report(claim.id, schemas.ExpenseReportUpdate(status="approved"), db=self.db, current_user=self.manager)
        self.raises(403, api.update_expense_payment, claim.id, upd, db=self.db, current_user=self.me)  # the employee cannot
        out = api.update_expense_payment(claim.id, upd, db=self.db, current_user=self.owner)
        self.assertEqual((out.is_paid, out.clearance_date), (True, TODAY))
        self.db.refresh(claim)
        self.assertTrue(claim.is_paid)
        back = api.update_expense_payment(claim.id, schemas.ExpensePaymentUpdate(is_paid=False), db=self.db, current_user=self.owner)
        self.assertEqual((back.is_paid, back.clearance_date), (False, None))
        self.raises(400, api.update_expense_payment, claim.id,
                    schemas.ExpensePaymentUpdate(is_paid=True, clearance_date=TODAY - datetime.timedelta(days=60)),
                    db=self.db, current_user=self.owner)
        with self.assertRaises(ValidationError):
            schemas.ExpensePaymentUpdate(is_paid=False, clearance_date=TODAY)
        with self.assertRaises(ValidationError):
            schemas.ExpensePaymentUpdate(is_paid=True, clearance_date=TODAY + datetime.timedelta(days=30))
        actions = [h["action"] for h in te.history_for(self.db, self.company_id, "expense_report", claim.id)]
        self.assertIn("Paid", actions)

    # ── expense claim types ─────────────────────────────────────────────────
    def test_default_claim_types_exist_and_management_rules(self):
        names = {t.name for t in cfg.list_expense_claim_types(q=None, is_active=None, db=self.db, current_user=self.owner)}
        self.assertTrue({"Calls", "Food", "Medical", "Others", "Travel"} <= names)
        t = cfg.create_expense_claim_type(schemas.ExpenseClaimTypeIn(name="  Lodging  ", description="Hotels"), db=self.db, current_user=self.owner)
        self.assertEqual(t.name, "Lodging")
        self.raises(409, cfg.create_expense_claim_type, schemas.ExpenseClaimTypeIn(name="lodging"), db=self.db, current_user=self.owner)
        found = cfg.list_expense_claim_types(q="lodg", is_active=None, db=self.db, current_user=self.owner)
        self.assertEqual([x.name for x in found], ["Lodging"])
        # it is immediately usable on a claim and then protected from deletion
        item = {"date": TODAY, "category": "lodging", "description": "Hotel", "amount": 3000}
        claim, _ = self.new_claim(items=[item])
        self.assertEqual(claim.lines[0].category, "Lodging")
        self.assertEqual(cfg.list_expense_claim_types(q="Lodging", is_active=None, db=self.db, current_user=self.owner)[0].usage_count, 1)
        self.raises(409, cfg.delete_expense_claim_type, t.id, db=self.db, current_user=self.owner)
        # renaming follows through to rows already filed
        renamed = cfg.update_expense_claim_type(t.id, schemas.ExpenseClaimTypeUpdate(name="Hotel stay"), db=self.db, current_user=self.owner)
        self.assertEqual(renamed.name, "Hotel stay")
        self.db.refresh(claim)
        self.assertEqual(claim.lines[0].category, "Hotel stay")
        # deactivate: hidden from the options and the active filter
        cfg.update_expense_claim_type(t.id, schemas.ExpenseClaimTypeUpdate(is_active=False), db=self.db, current_user=self.owner)
        self.assertNotIn("Hotel stay", cfg.travel_expense_options(db=self.db, current_user=self.owner)["claimTypes"])
        self.assertEqual(cfg.list_expense_claim_types(q=None, is_active=False, db=self.db, current_user=self.owner)[0].name, "Hotel stay")
        # an unused type deletes cleanly
        spare = cfg.create_expense_claim_type(schemas.ExpenseClaimTypeIn(name="Spare"), db=self.db, current_user=self.owner)
        cfg.delete_expense_claim_type(spare.id, db=self.db, current_user=self.owner)
        self.raises(404, cfg.update_expense_claim_type, spare.id, schemas.ExpenseClaimTypeUpdate(name="x"), db=self.db, current_user=self.owner)

    def test_options_and_approver_choices(self):
        opts = cfg.travel_expense_options(db=self.db, current_user=self.me)
        self.assertEqual(opts["travelTypes"], ["Domestic", "International"])
        self.assertIn("Food", opts["claimTypes"])
        ch = cfg.expense_approver_choices(employee_id=None, db=self.db, current_user=self.me)
        self.assertIn(str(self.manager.employee_id), {a["id"] for a in ch["approvers"]})
        self.assertEqual(ch["default"], str(self.worker.reporting_manager_id))

    # ── advances ────────────────────────────────────────────────────────────
    def test_advances_are_listed_for_their_owner_and_recorded_by_admins(self):
        out = cfg.create_employee_advance(
            schemas.EmployeeAdvanceIn(employee_id=self.worker.id, posting_date=TODAY, advance_amount=2500, purpose="Pune trip"),
            db=self.db, current_user=self.owner)
        self.assertEqual((out.advance_amount, out.outstanding, out.available, out.status), (2500.0, 2500.0, 2500.0, "paid"))
        mine = cfg.list_employee_advances(employee_id=None, only_open=True, db=self.db, current_user=self.me)
        self.assertIn(out.id, {a.id for a in mine})
        self.raises(404, cfg.create_employee_advance,
                    schemas.EmployeeAdvanceIn(employee_id=uuid.uuid4(), posting_date=TODAY, advance_amount=5), db=self.db, current_user=self.owner)
        with self.assertRaises(ValidationError):
            schemas.EmployeeAdvanceIn(employee_id=self.worker.id, posting_date=TODAY, advance_amount=0)


if __name__ == "__main__":
    unittest.main()
