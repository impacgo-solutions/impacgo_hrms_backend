"""Travel & Expenses on the Form Configuration platform: the four dropdowns
(travel.type / funding / id_document_type / mode), the Claim Types behind
Expense Type + Expense Claim Type, and the custom fields of the
'travel_request' and 'expense_claim' forms.

    cd backend && venv/Scripts/python -m unittest tests.test_travel_form_config -v

Rolled-back transactions on the configured (test) database.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, email_service, form_config as fc, models, schemas, travel_expense as te  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import form_config as fapi  # noqa: E402
from app.routers import travel as api  # noqa: E402
from app.routers import travel_expense_config as cfg  # noqa: E402
from tests.test_travel_expense import TENANT, TODAY, claim_payload, leg, travel_payload  # noqa: E402


class TravelFormConfigTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        mock.patch.object(email_service._executor, "submit", lambda *a, **k: None).start()
        self.addCleanup(mock.patch.stopall)
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        by_emp = {u.employee_id: u for u in users}
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                           and r.name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("no Owner")
        emps = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.owner.company_id, models.Employee.is_active.is_(True))).all()
        self.worker = next((e for e in emps if e.id in by_emp and e.reporting_manager_id in by_emp
                            and e.id != self.owner.employee_id), None)
        if self.worker is None:
            self.skipTest("no employee with a login and a manager with a login")
        self.me = by_emp[self.worker.id]
        self.manager = by_emp[self.worker.reporting_manager_id]
        self.cid = self.owner.company_id

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # ── helpers ─────────────────────────────────────────────────────────────
    def raises(self, code, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        self.assertEqual(ctx.exception.status_code, code, ctx.exception.detail)
        return ctx.exception.detail

    def add_option(self, field_key, label):
        return fapi.create_option(fapi.OptionCreate(field_key=field_key, label=label), db=self.db, current_user=self.owner)

    def hide_option(self, field_key, value):
        row = next(r for r in fc._rows(self.db, self.cid)[field_key] if r.value == value)
        return fapi.delete_option(row.id, db=self.db, current_user=self.owner)

    def make_field(self, form, label, ftype, **kw):
        return fapi.create_custom_field(
            fapi.CustomFieldCreate(form_key=form, label=label, field_type=ftype, **kw),
            db=self.db, current_user=self.owner)

    def trip(self, **kw):
        out = api.create_travel_request(travel_payload(self.worker.id, **kw), db=self.db, current_user=self.me)
        return self.db.get(models.TravelRequestModel, uuid.UUID(out["id"]))

    def edit_trip(self, trip, **kw):
        body = travel_payload(self.worker.id, **kw).model_dump(exclude={"employee_id"})
        return api.edit_travel_request(trip.id, schemas.TravelRequestEdit(**body), db=self.db, current_user=self.me)

    def claim(self, **kw):
        out = api.create_expense_report(claim_payload(self.worker.id, **kw), db=self.db, current_user=self.me)
        return self.db.get(models.ExpenseClaim, uuid.UUID(out["id"]))

    def edit_claim(self, claim, **kw):
        body = claim_payload(self.worker.id, **kw).model_dump()
        return api.edit_expense_report(claim.id, schemas.ExpenseReportEdit(**body), db=self.db, current_user=self.me)

    # ── dropdowns: admin-added accepted, hidden/removed refused, legacy kept ─
    def test_admin_added_options_are_accepted_by_the_travel_endpoint(self):
        self.add_option("travel.type", "Regional")
        self.add_option("travel.funding", "Grant")
        self.add_option("travel.id_document_type", "Employee Badge")
        self.add_option("travel.mode", "Ferry")
        trip = self.trip(travel_type="regional", travel_funding="grant", id_document_type="employee badge",
                         id_document_number="B-77", itinerary=[leg(mode="ferry")], costing=[])
        self.assertEqual((trip.travel_type, trip.travel_funding, trip.id_document_type), ("Regional", "Grant", "Employee Badge"))
        self.assertEqual(trip.itinerary[0].mode_of_travel, "Ferry")
        self.assertEqual(trip.travel_mode, "Ferry")
        # an added (non built-in) funding option is NOT sponsored: no sponsor details needed
        self.assertIsNone(trip.sponsor_details)
        # sponsor details are optional, built-in sponsored values included
        travel_payload(self.worker.id, travel_funding="Partially Sponsored")
        # the options endpoint serves the company's lists
        opts = cfg.travel_expense_options(db=self.db, current_user=self.me)
        self.assertIn("Regional", opts["travelTypes"])
        self.assertIn("Ferry", opts["travelModes"])

    def test_unlisted_hidden_and_removed_options_are_refused(self):
        for kw in (dict(travel_type="Orbital"), dict(travel_funding="Lottery"),
                   dict(id_document_type="Library Card", id_document_number="1"),
                   dict(itinerary=[leg(mode="Teleport")])):
            with self.subTest(kw=list(kw)):
                self.raises(422, api.create_travel_request, travel_payload(self.worker.id, **kw),
                            db=self.db, current_user=self.me)
        # hidden built-in
        self.hide_option("travel.type", "Domestic")
        self.hide_option("travel.mode", "Flight")
        self.raises(422, api.create_travel_request, travel_payload(self.worker.id, travel_type="Domestic"),
                    db=self.db, current_user=self.me)
        self.raises(422, api.create_travel_request, travel_payload(self.worker.id, itinerary=[leg(mode="Flight")]),
                    db=self.db, current_user=self.me)
        # removed (admin-added, then deleted)
        added = self.add_option("travel.funding", "Grant")
        fapi.delete_option(added.id, db=self.db, current_user=self.owner)
        self.raises(422, api.create_travel_request, travel_payload(self.worker.id, travel_funding="Grant"),
                    db=self.db, current_user=self.me)

    def test_a_legacy_value_still_saves_when_unchanged_on_edit(self):
        trip = self.trip(save_as_draft=True, travel_type="Domestic", travel_funding="Company",
                         id_document_type="Passport", id_document_number="P1")
        for key, value in (("travel.type", "Domestic"), ("travel.funding", "Company"),
                           ("travel.id_document_type", "Passport"), ("travel.mode", "Flight"), ("travel.mode", "Bus")):
            self.hide_option(key, value)
        # unchanged hidden values (incl. the 'Flight' leg) are accepted ...
        out = self.edit_trip(trip, save_as_draft=True, travel_type="Domestic", travel_funding="Company",
                             id_document_type="Passport", id_document_number="P1", purpose="Edited")
        self.assertEqual((out.travel_type, out.purpose), ("Domestic", "Edited"))
        # ... but a NEW leg using the hidden mode, or switching to another hidden value, is not
        self.raises(422, api.edit_travel_request, trip.id, schemas.TravelRequestEdit(
            **travel_payload(self.worker.id, save_as_draft=True,
                             itinerary=[leg(mode="Flight"), leg("Pune", "Goa", "Bus", days=14)])
            .model_dump(exclude={"employee_id"})), db=self.db, current_user=self.me)

    def test_renamed_option_keeps_its_stored_value(self):
        row = next(r for r in fc._rows(self.db, self.cid)["travel.mode"] if r.value == "Train")
        fapi.update_option(row.id, fapi.OptionUpdate(label="Rail"), db=self.db, current_user=self.owner)
        trip = self.trip(itinerary=[leg(mode="Train")], costing=[])
        self.assertEqual(trip.itinerary[0].mode_of_travel, "Train")
        self.assertEqual(fc.option_label(self.db, self.cid, "travel.mode", "Train"), "Rail")

    # ── Claim Types behind Expense Type (costing) and Expense Claim Type ────
    def test_claim_types_drive_both_dropdowns(self):
        def served():
            return cfg.travel_expense_options(db=self.db, current_user=self.me)["claimTypes"]

        made = cfg.create_expense_claim_type(schemas.ExpenseClaimTypeIn(name="Lodging"), db=self.db, current_user=self.owner)
        self.assertIn("Lodging", served())
        trip = self.trip(itinerary=[leg()], costing=[{"expense_type": "lodging", "funded_amount": 100}])
        self.assertEqual(trip.costing[0].expense_type, "Lodging")
        claim = self.claim(items=[{"date": TODAY, "category": "Lodging", "description": "Hotel", "amount": 900}])
        self.assertEqual(claim.lines[0].category, "Lodging")
        # rename: dropdown follows, filed rows follow
        cfg.update_expense_claim_type(made.id, schemas.ExpenseClaimTypeUpdate(name="Stay"), db=self.db, current_user=self.owner)
        self.assertIn("Stay", served())
        self.assertNotIn("Lodging", served())
        self.db.refresh(claim)
        self.db.refresh(trip)
        self.assertEqual((claim.lines[0].category, trip.costing[0].expense_type), ("Stay", "Stay"))
        # deactivate: gone from the dropdown and refused by both endpoints
        cfg.update_expense_claim_type(made.id, schemas.ExpenseClaimTypeUpdate(is_active=False), db=self.db, current_user=self.owner)
        self.assertNotIn("Stay", served())
        self.raises(400, api.create_travel_request,
                    travel_payload(self.worker.id, itinerary=[leg(days=40)], costing=[{"expense_type": "Stay", "funded_amount": 1}]),
                    db=self.db, current_user=self.me)
        self.raises(400, api.create_expense_report,
                    claim_payload(self.worker.id, items=[{"date": TODAY, "category": "Stay", "description": "x", "amount": 5}]),
                    db=self.db, current_user=self.me)
        # a used type cannot be deleted; an unused one can and disappears
        self.raises(409, cfg.delete_expense_claim_type, made.id, db=self.db, current_user=self.owner)
        spare = cfg.create_expense_claim_type(schemas.ExpenseClaimTypeIn(name="Spare"), db=self.db, current_user=self.owner)
        self.assertIn("Spare", served())
        cfg.delete_expense_claim_type(spare.id, db=self.db, current_user=self.owner)
        self.assertNotIn("Spare", served())
        # re-activating brings it back
        cfg.update_expense_claim_type(made.id, schemas.ExpenseClaimTypeUpdate(is_active=True), db=self.db, current_user=self.owner)
        self.assertIn("Stay", served())

    # ── custom fields: travel_request ───────────────────────────────────────
    def travel_fields(self):
        self.make_field("travel_request", "Cost Code", "text", is_required=True)
        self.make_field("travel_request", "Budget", "number")
        self.make_field("travel_request", "Priority", "dropdown", options=["High", "Low"])
        self.make_field("travel_request", "Visa Needed", "checkbox")
        self.make_field("travel_request", "Briefing Date", "date")

    def test_travel_custom_fields_validation(self):
        self.travel_fields()
        mk = lambda cf, **k: api.create_travel_request(  # noqa: E731
            travel_payload(self.worker.id, custom_fields=cf, **k), db=self.db, current_user=self.me)
        # required: refused on submit (missing or blank) ...
        self.raises(422, mk, {"budget": "5"})
        self.raises(422, mk, {"cost_code": "  "})
        # type / choice / unknown-key errors
        for bad in ({"cost_code": "A", "budget": "abc"}, {"cost_code": "A", "priority": "Medium"},
                    {"cost_code": "A", "briefing_date": "31/01/2027"}, {"cost_code": "A", "visa_needed": "maybe"},
                    {"cost_code": "A", "ghost": "1"}):
            with self.subTest(bad=bad):
                self.raises(422, mk, bad)
        # ... but a draft may be incomplete
        out = mk({"budget": "7"}, save_as_draft=True)
        self.assertEqual(out["status"], "draft")

    def test_travel_custom_values_persist_and_read_back_everywhere(self):
        self.travel_fields()
        vals = {"cost_code": " CC-9 ", "budget": "1250.50", "priority": "high", "visa_needed": True,
                "briefing_date": "2027-02-01"}
        trip = self.trip(custom_fields=vals)
        expect = {"cost_code": "CC-9", "budget": "1250.5", "priority": "High", "visa_needed": "true",
                  "briefing_date": "2027-02-01"}
        self.assertEqual(te.travel_request_out(self.db, trip, viewer=self.me).custom_fields, expect)
        self.assertEqual(api.get_travel_request(trip.id, db=self.db, current_user=self.me).custom_fields, expect)
        listed = api.list_travel_requests(status=None, limit=100, offset=0, db=self.db, current_user=self.me)
        self.assertEqual(next(r for r in listed if r.id == trip.id).custom_fields, expect)

    def test_travel_custom_values_survive_draft_edit_submit_and_send_back(self):
        self.travel_fields()
        trip = self.trip(save_as_draft=True, custom_fields={"cost_code": "CC-1", "budget": "10"})
        # a draft edit may clear a required value (nothing is raised) ...
        out = self.edit_trip(trip, save_as_draft=True, custom_fields={"cost_code": "", "budget": "11"})
        self.assertEqual(out.custom_fields, {"budget": "11"})
        # ... but submitting without it is refused, by the edit and by the submit endpoint
        self.raises(422, api.edit_travel_request, trip.id, schemas.TravelRequestEdit(
            **travel_payload(self.worker.id, custom_fields={"budget": "11"}).model_dump(exclude={"employee_id"})),
            db=self.db, current_user=self.me)
        self.raises(422, api.submit_travel_request, trip.id, db=self.db, current_user=self.me)
        self.assertEqual(self.db.get(models.TravelRequestModel, trip.id).status, "draft")
        # a payload that omits custom_fields leaves stored values alone
        self.edit_trip(trip, save_as_draft=True, custom_fields={"cost_code": "CC-2"})
        out = self.edit_trip(trip, save_as_draft=True)
        self.assertEqual(out.custom_fields.get("cost_code"), "CC-2")
        # the standalone submit works now the required value is stored
        self.assertEqual(api.submit_travel_request(trip.id, db=self.db, current_user=self.me).status, "pending")
        # manager sends it back; the values are still there and a re-submit keeps/changes them
        api.update_travel_request(trip.id, schemas.TravelRequestUpdate(status="sent_back", decision_notes="fix"),
                                  db=self.db, current_user=self.manager)
        self.assertEqual(api.get_travel_request(trip.id, db=self.db, current_user=self.me).custom_fields["cost_code"], "CC-2")
        out = self.edit_trip(trip, custom_fields={"cost_code": "CC-3", "priority": "Low"})
        self.assertEqual((out.status, out.custom_fields["cost_code"], out.custom_fields["priority"]), ("pending", "CC-3", "Low"))

    def test_hidden_custom_field_is_neither_required_nor_returned(self):
        f = self.make_field("travel_request", "Cost Code", "text", is_required=True)
        trip = self.trip(custom_fields={"cost_code": "X"})
        fapi.update_custom_field(f.id, fapi.CustomFieldUpdate(is_active=False), db=self.db, current_user=self.owner)
        self.assertEqual(api.get_travel_request(trip.id, db=self.db, current_user=self.me).custom_fields, {})
        self.assertEqual(self.trip(itinerary=[leg(days=50)], costing=[]).status, "pending")

    def test_custom_values_are_tenant_scoped_and_deleted_with_the_record(self):
        self.travel_fields()
        trip = self.trip(custom_fields={"cost_code": "CC-1"})
        other_company = uuid.uuid4()
        self.assertEqual(fc.load_custom_values(self.db, other_company, "travel_request", [trip.id]), {})
        self.assertEqual(self.raises(422, fc.validate_custom_values, self.db, other_company, "travel_request",
                                     {"cost_code": "x"}) is not None, True)
        draft = self.trip(save_as_draft=True, itinerary=[leg(days=70)], costing=[], custom_fields={"cost_code": "D"})
        api.delete_travel_request(draft.id, db=self.db, current_user=self.me)
        left = self.db.scalars(select(models.CustomFieldValue).where(models.CustomFieldValue.entity_id == draft.id)).all()
        self.assertEqual(left, [])

    # ── custom fields: expense_claim ────────────────────────────────────────
    def claim_fields(self):
        self.make_field("expense_claim", "Receipt No", "text", is_required=True)
        self.make_field("expense_claim", "Notes", "textarea")
        self.make_field("expense_claim", "Category", "dropdown", options=["Client", "Internal"])

    def test_claim_custom_fields_validate_persist_and_flow(self):
        self.claim_fields()
        mk = lambda cf, **k: api.create_expense_report(  # noqa: E731
            claim_payload(self.worker.id, custom_fields=cf, **k), db=self.db, current_user=self.me)
        self.raises(422, mk, {"notes": "n"})
        self.raises(422, mk, {"receipt_no": "R", "category": "Other"})
        self.raises(422, mk, {"receipt_no": "R", "ghost": "1"})
        other_items = [{"date": TODAY, "category": "Food", "description": "Snack run", "amount": 55}]
        self.assertEqual(mk({"notes": "only"}, save_as_draft=True, items=other_items)["status"], "draft")
        claim = self.claim(custom_fields={"receipt_no": " R-1 ", "category": "client"})
        expect = {"receipt_no": "R-1", "category": "Client"}
        self.assertEqual(te.expense_report_out(self.db, claim, viewer=self.me).custom_fields, expect)
        self.assertEqual(api.get_expense_report(claim.id, db=self.db, current_user=self.me).custom_fields, expect)
        listed = api.list_expense_reports(limit=100, offset=0, db=self.db, current_user=self.me)
        self.assertEqual(next(r for r in listed if r.id == claim.id).custom_fields, expect)
        # send back -> edit/resubmit keeps and updates the values
        api.update_expense_report(claim.id, schemas.ExpenseReportUpdate(status="sent_back", decision_notes="fix"),
                                  db=self.db, current_user=self.manager)
        self.assertEqual(api.get_expense_report(claim.id, db=self.db, current_user=self.me).custom_fields, expect)
        out = self.edit_claim(claim, custom_fields={"receipt_no": "R-2", "notes": "ok"})
        self.assertEqual((out.status, out.custom_fields["receipt_no"], out.custom_fields["notes"]), ("pending", "R-2", "ok"))

    def test_claim_draft_incomplete_then_submit_is_enforced(self):
        self.claim_fields()
        claim = self.claim(save_as_draft=True, custom_fields={"receipt_no": "R-1"})
        out = self.edit_claim(claim, save_as_draft=True, custom_fields={"receipt_no": ""})
        self.assertNotIn("receipt_no", out.custom_fields)
        self.raises(422, api.submit_expense_report, claim.id, db=self.db, current_user=self.me)
        self.raises(422, self.edit_claim, claim, custom_fields={"notes": "n"})
        self.edit_claim(claim, save_as_draft=True, custom_fields={"receipt_no": "R-9"})
        self.assertEqual(api.submit_expense_report(claim.id, db=self.db, current_user=self.me).status, "pending")


if __name__ == "__main__":
    unittest.main()
