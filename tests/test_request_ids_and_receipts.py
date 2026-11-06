"""Configurable Project ID, unique Travel / Expense request IDs, and
expense-wise receipts. Rolled back.

    cd backend && venv/Scripts/python -m unittest tests.test_request_ids_and_receipts -v
"""

from __future__ import annotations

import datetime
import io
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import UploadFile  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import crud, models, request_numbers, schemas  # noqa: E402
from app.routers import settings as settings_api  # noqa: E402
from app.routers import travel as api  # noqa: E402
from tests import test_travel_expense as base  # noqa: E402

TODAY, claim_payload = base.TODAY, base.claim_payload


def _file(name="bill.pdf"):
    return UploadFile(file=io.BytesIO(b"%PDF-1.4 test"), filename=name, headers={"content-type": "application/pdf"})


class RequestIdAndReceiptTests(base.TravelExpenseTests):
    def setUp(self):
        super().setUp()
        # No real files on disk: the upload/delete of the stored file is faked.
        mock.patch.object(api, "save_uploaded_file",
                          side_effect=lambda f, **k: (f"/media/expense_report/{uuid.uuid4()}.pdf", 13)).start()
        mock.patch("app.storage.delete_uploaded_file", lambda *a, **k: None).start()

    # ── Project ID ──────────────────────────────────────────────────────────
    def test_project_ids_follow_the_configured_format(self):
        settings_api.update_project_id_format(
            schemas.ProjectIdFormatUpdate(prefix="QA-", suffix="-P", padding=3, next_number=7),
            db=self.db, current_user=self.owner)
        fmt = settings_api.get_project_id_format_admin(db=self.db, current_user=self.owner)
        self.assertEqual((fmt.next_number, fmt.next_code), (7, "QA-007-P"))
        a = crud.create_project(self.db, self.company_id, f"QA proj {uuid.uuid4().hex[:6]}", "planning")
        b = crud.create_project(self.db, self.company_id, f"QA proj {uuid.uuid4().hex[:6]}", "planning")
        self.assertEqual((a.code, b.code), ("QA-007-P", "QA-008-P"))
        # An ID already in use is skipped, never duplicated.
        settings_api.update_project_id_format(schemas.ProjectIdFormatUpdate(next_number=8), db=self.db, current_user=self.owner)
        c = crud.create_project(self.db, self.company_id, f"QA proj {uuid.uuid4().hex[:6]}", "planning")
        self.assertEqual(c.code, "QA-009-P")
        # A hand-given ID that exists is refused.
        with self.assertRaises(ValueError):
            crud.create_project(self.db, self.company_id, f"QA proj {uuid.uuid4().hex[:6]}", "planning", code="qa-009-p")

    def test_too_long_project_id_is_refused(self):
        self.raises(422, settings_api.update_project_id_format,
                    schemas.ProjectIdFormatUpdate(prefix="P" * 20, suffix="S" * 5, padding=10),
                    db=self.db, current_user=self.owner)

    # ── Travel / Expense request IDs ────────────────────────────────────────
    def test_travel_requests_get_sequential_unique_ids(self):
        t1, _ = self.new_trip(save_as_draft=True)
        t2, _ = self.new_trip(save_as_draft=True, itinerary=[base.leg(days=40), base.leg("Bengaluru", "Hyderabad", days=42)])
        prefix = f"HR-TR-{datetime.date.today().year}-"
        self.assertTrue(t1.request_no.startswith(prefix) and t2.request_no.startswith(prefix))
        self.assertEqual(int(t2.request_no[len(prefix):]), int(t1.request_no[len(prefix):]) + 1)
        self.assertEqual(t1.company_id, self.company_id)
        out = api.get_travel_request(t1.id, db=self.db, current_user=self.me)
        self.assertEqual(out.request_no, t1.request_no)
        # The older create path is numbered too.
        old = crud.create_travel_request(self.db, self.worker.id, "x", "Delhi", TODAY + datetime.timedelta(days=60),
                                         TODAY + datetime.timedelta(days=61), "Flight", None)
        self.db.flush()
        self.assertTrue(old.request_no and old.request_no.startswith(prefix))
        self.assertEqual(len({t1.request_no, t2.request_no, old.request_no}), 3)

    def test_expense_reports_get_sequential_unique_numbers(self):
        a, _ = self.new_claim(save_as_draft=True)
        b, _ = self.new_claim(save_as_draft=True, items=[
            {"date": TODAY - datetime.timedelta(days=8), "category": "Food", "description": "Dinner", "amount": 300}])
        prefix = f"HR-EXP-{TODAY.year}-"
        self.assertTrue(a.claim_no.startswith(prefix))
        self.assertEqual(int(b.claim_no[len(prefix):]), int(a.claim_no[len(prefix):]) + 1)

    # ── expense-wise receipts ───────────────────────────────────────────────
    def _upload(self, claim, line_index=None, name="bill.pdf"):
        return api.upload_expense_attachment(claim.id, file=_file(name), line_index=line_index,
                                             db=self.db, current_user=self.me)

    def test_receipt_is_linked_to_its_own_expense_row(self):
        claim, _ = self.new_claim(save_as_draft=True)
        r1 = self._upload(claim, line_index=1, name="cab.pdf")
        general = self._upload(claim, name="itinerary.pdf")
        out = api.get_expense_report(claim.id, db=self.db, current_user=self.me)
        self.assertIsNone(out.items[0].receipt)
        self.assertEqual(out.items[1].receipt.id, r1.id)
        self.assertEqual(out.items[1].receipt.file_name, "cab.pdf")
        # The general document stays a report-level attachment.
        self.assertNotIn(general.id, {i.receipt.id for i in out.items if i.receipt})

    def test_receipt_survives_an_edit_and_goes_with_its_row(self):
        claim, _ = self.new_claim(save_as_draft=True)
        r0 = self._upload(claim, line_index=0, name="lunch.pdf")
        r1 = self._upload(claim, line_index=1, name="cab.pdf")
        items = [
            {"date": TODAY - datetime.timedelta(days=3), "category": "Food", "description": "Team lunch",
             "amount": 600, "receipt_attachment_id": r0.id},
        ]
        edit = schemas.ExpenseReportEdit(submitted_date=TODAY, save_as_draft=True, items=items)
        out = api.edit_expense_report(claim.id, edit, db=self.db, current_user=self.me)
        self.assertEqual(out.items[0].receipt.id, r0.id, "kept through the edit")
        left = {a.id for a in crud.list_attachments_for_entity(self.db, "expense_report", claim.id)}
        self.assertIn(r0.id, left)
        self.assertNotIn(r1.id, left, "the removed row's receipt is deleted with it")

    def test_new_receipt_replaces_the_previous_one(self):
        claim, _ = self.new_claim(save_as_draft=True)
        old = self._upload(claim, line_index=0, name="v1.pdf")
        new = self._upload(claim, line_index=0, name="v2.pdf")
        out = api.get_expense_report(claim.id, db=self.db, current_user=self.me)
        self.assertEqual(out.items[0].receipt.id, new.id)
        left = {a.id for a in crud.list_attachments_for_entity(self.db, "expense_report", claim.id)}
        self.assertNotIn(old.id, left)

    def test_receipt_rules(self):
        claim, _ = self.new_claim(save_as_draft=True)
        self.raises(422, self._upload, claim, line_index=5)
        r0 = self._upload(claim, line_index=0)
        other, _ = self.new_claim(save_as_draft=True, items=[
            {"date": TODAY - datetime.timedelta(days=12), "category": "Food", "description": "Snacks", "amount": 90}])
        # Another report's file can't be borrowed...
        bad = schemas.ExpenseReportEdit(submitted_date=TODAY, save_as_draft=True, items=[
            {"date": TODAY - datetime.timedelta(days=12), "category": "Food", "description": "Snacks", "amount": 90,
             "receipt_attachment_id": r0.id}])
        self.raises(422, api.edit_expense_report, other.id, bad, db=self.db, current_user=self.me)
        # ...and one file can't be the receipt of two rows.
        twice = schemas.ExpenseReportEdit(submitted_date=TODAY, save_as_draft=True, items=[
            {"date": TODAY - datetime.timedelta(days=3), "category": "Food", "description": "a", "amount": 10,
             "receipt_attachment_id": r0.id},
            {"date": TODAY - datetime.timedelta(days=2), "category": "Travel", "description": "b", "amount": 20,
             "receipt_attachment_id": r0.id}])
        self.raises(422, api.edit_expense_report, claim.id, twice, db=self.db, current_user=self.me)

    def test_older_clients_without_receipts_still_work(self):
        claim, _ = self.new_claim(save_as_draft=True)
        out = api.get_expense_report(claim.id, db=self.db, current_user=self.me)
        self.assertTrue(all(i.receipt is None for i in out.items))
        self.assertIsNotNone(request_numbers.TRAVEL_SERIES_PREFIX)

    # ── configurable Travel / Expense formats ───────────────────────────────
    def test_default_formats_match_the_previous_ones(self):
        out = {f.doctype: f for f in settings_api.list_id_formats(db=self.db, current_user=self.owner)}
        self.assertEqual(set(out), {"project", "travel_request", "expense_report"})
        year = TODAY.year
        self.assertTrue(out["travel_request"].next_code.startswith(f"HR-TR-{year}-"))
        self.assertTrue(out["expense_report"].next_code.startswith(f"HR-EXP-{year}-"))
        self.assertTrue(out["travel_request"].include_year and out["expense_report"].include_year)
        prev = settings_api.preview_id("expense_report", db=self.db, current_user=self.me)
        self.assertEqual(prev.next_code, out["expense_report"].next_code)

    def test_expense_ids_follow_a_custom_format(self):
        f = settings_api.update_id_format(
            "expense_report", schemas.IdFormatUpdate(prefix="EXP/", suffix="/IN", padding=4, next_number=50,
                                                     include_year=False),
            db=self.db, current_user=self.owner)
        self.assertEqual(f.next_code, "EXP/0050/IN")
        a, _ = self.new_claim(save_as_draft=True)
        b, _ = self.new_claim(save_as_draft=True, items=[
            {"date": TODAY - datetime.timedelta(days=8), "category": "Food", "description": "Dinner", "amount": 300}])
        self.assertEqual((a.claim_no, b.claim_no), ("EXP/0050/IN", "EXP/0051/IN"))
        # Deleting the newest never frees its number (no year: the floor moved on).
        api.delete_expense_report(b.id, db=self.db, current_user=self.me)
        c, _ = self.new_claim(save_as_draft=True, items=[
            {"date": TODAY - datetime.timedelta(days=9), "category": "Food", "description": "Tea", "amount": 30}])
        self.assertEqual(c.claim_no, "EXP/0052/IN")

    def test_travel_ids_follow_a_custom_format_with_year(self):
        settings_api.update_id_format(
            "travel_request", schemas.IdFormatUpdate(prefix="TRV-", suffix="", padding=3, next_number=1,
                                                     include_year=True),
            db=self.db, current_user=self.owner)
        t1, _ = self.new_trip(save_as_draft=True)
        t2, _ = self.new_trip(save_as_draft=True, itinerary=[base.leg(days=40), base.leg("Bengaluru", "Hyderabad", days=42)])
        self.assertEqual(t1.request_no, f"TRV-{TODAY.year}-001")
        self.assertEqual(t2.request_no, f"TRV-{TODAY.year}-002")

    def test_id_format_limits(self):
        self.raises(422, settings_api.update_id_format, "travel_request",
                    schemas.IdFormatUpdate(prefix="P" * 20, suffix="S" * 5, padding=10, include_year=True),
                    db=self.db, current_user=self.owner)
        self.raises(404, settings_api.update_id_format, "payslip", schemas.IdFormatUpdate(prefix="X"),
                    db=self.db, current_user=self.owner)

    # ── IDs shown everywhere ────────────────────────────────────────────────
    def test_notifications_name_the_request_id(self):
        trip, _ = self.new_trip()  # submitted: the manager is notified
        note = self.db.scalar(select(models.Notification).where(
            models.Notification.entity_id == trip.id).order_by(models.Notification.created_at.desc()).limit(1))
        self.assertIsNotNone(note)
        self.assertIn(trip.request_no, note.title)
        self.assertIn(trip.request_no, note.body)
        crud.notify_decision(self.db, self.company_id, self.worker.id, "Travel Request", "approved", None,
                             "travel_request", trip.id)
        self.db.flush()
        decided = self.db.scalars(select(models.Notification).where(
            models.Notification.entity_id == trip.id)).all()
        self.assertTrue(any(trip.request_no in n.title and "approved" in n.title for n in decided))
        claim, _ = self.new_claim(save_as_draft=True)
        self.assertEqual(crud.request_reference(self.db, "expense_report", claim.id), claim.claim_no)
        self.assertIsNone(crud.request_reference(self.db, "leave_request", claim.id))

    def test_payroll_uses_the_real_travel_request_id(self):
        trip, _ = self.new_trip()
        self.assertEqual(crud._reimbursement_reference("travel_request", trip), trip.request_no)

    def test_reports_carry_a_project_id_column(self):
        from app.routers import reports as reports_api

        access = crud.report_access(self.db, self.owner)
        out = reports_api.project_report(from_date=TODAY - datetime.timedelta(days=365), to_date=TODAY,
                                         branch_id=None, db=self.db, current_user=self.owner, access=access)
        self.assertEqual(out.columns[:2], ["Project ID", "Project"])
        codes = set(self.db.scalars(select(models.Project.code).where(models.Project.company_id == self.company_id)))
        for row in out.rows:
            self.assertIn(row[0], codes | {"—"})
            self.assertEqual(len(row), len(out.columns))


# The inherited TravelExpenseTests test_* methods run in their own module.
for _name in [n for n in dir(base.TravelExpenseTests) if n.startswith("test_")]:
    setattr(RequestIdAndReceiptTests, _name, None)


if __name__ == "__main__":
    unittest.main()
