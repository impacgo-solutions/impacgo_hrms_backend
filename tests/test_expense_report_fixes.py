"""Travel & Expenses > Expense Reports -- fixes: who may record a payment,
the can_record_payment flag, private drafts, the cross-company decision
guard, deactivated claim types on a draft, fixed-amount taxes and the
history of Payroll-filed claims. Rolled back.

    cd backend && venv/Scripts/python -m unittest tests.test_expense_report_fixes -v
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

from app import crud, models, schemas  # noqa: E402
from app.routers import travel as api  # noqa: E402
from tests import test_travel_expense as base  # noqa: E402

TODAY, claim_payload = base.TODAY, base.claim_payload


class ExpenseReportFixTests(base.TravelExpenseTests):
    def _submitted(self, **kw):
        claim, _ = self.new_claim(**kw)
        return claim

    def test_payment_only_finance_never_own_claim(self):
        claim = self._submitted()
        api.update_expense_report(claim.id, schemas.ExpenseReportUpdate(status="approved"), db=self.db, current_user=self.manager)
        # An approving manager with Travel & Expense "Edit" is not Finance.
        with mock.patch.object(crud, "effective_user_matrix", return_value={"travel_expense_approval": "e"}), \
                mock.patch.object(crud, "is_fallback_approver", return_value=False):
            self.raises(403, api.update_expense_payment, claim.id, schemas.ExpensePaymentUpdate(is_paid=True),
                        db=self.db, current_user=self.manager)
            out = api.get_expense_report(claim.id, db=self.db, current_user=self.manager)
            self.assertFalse(out.can_record_payment)
        # The employee never pays their own claim.
        self.raises(403, api.update_expense_payment, claim.id, schemas.ExpensePaymentUpdate(is_paid=True),
                    db=self.db, current_user=self.me)
        self.assertFalse(api.get_expense_report(claim.id, db=self.db, current_user=self.me).can_record_payment)
        # Finance (the Owner) does.
        self.assertTrue(api.get_expense_report(claim.id, db=self.db, current_user=self.owner).can_record_payment)
        paid = api.update_expense_payment(claim.id, schemas.ExpensePaymentUpdate(is_paid=True, clearance_date=TODAY),
                                          db=self.db, current_user=self.owner)
        self.assertTrue(paid.is_paid)

    def test_paid_at_creation_refused_for_own_claim(self):
        own = claim_payload(self.owner.employee_id, is_paid=True)
        self.raises(403, api.create_expense_report, own, db=self.db, current_user=self.owner)

    def test_drafts_are_private_to_their_owner(self):
        draft, _ = self.new_claim(save_as_draft=True)
        mine = api.list_expense_reports(limit=500, offset=0, db=self.db, current_user=self.me)
        self.assertIn(draft.id, {r.id for r in mine})
        for viewer in (self.manager, self.owner):
            seen = api.list_expense_reports(limit=500, offset=0, db=self.db, current_user=viewer)
            self.assertNotIn(draft.id, {r.id for r in seen})

    def test_decision_on_another_companys_claim_is_not_found(self):
        claim = self._submitted()
        other_company = self.db.scalar(
            select(models.Company.id).where(models.Company.id != self.company_id).limit(1))
        if other_company is None:
            self.skipTest("single-company tenant")
        claim.company_id = other_company
        self.db.flush()
        self.raises(404, api.update_expense_report, claim.id, schemas.ExpenseReportUpdate(status="approved"),
                    db=self.db, current_user=self.manager)

    def test_deactivated_claim_type_stays_valid_on_its_draft(self):
        draft, _ = self.new_claim(save_as_draft=True)
        food = self.db.scalar(select(models.ExpenseClaimType).where(
            models.ExpenseClaimType.company_id == self.company_id, models.ExpenseClaimType.name == "Food"))
        food.is_active = False
        self.db.flush()
        edit = schemas.ExpenseReportEdit(
            submitted_date=TODAY, save_as_draft=True,
            items=[{"date": TODAY - datetime.timedelta(days=3), "category": "Food", "description": "Team lunch", "amount": 650}])
        out = api.edit_expense_report(draft.id, edit, db=self.db, current_user=self.me)
        self.assertEqual(out.items[0].category, "Food")
        # ...but a new claim can't pick it.
        self.raises(400, api.create_expense_report, claim_payload(self.worker.id, items=[
            {"date": TODAY - datetime.timedelta(days=9), "category": "Food", "description": "x", "amount": 10}],
            save_as_draft=True), db=self.db, current_user=self.me)

    def test_zero_rate_tax_uses_the_fixed_amount(self):
        claim, _ = self.new_claim(save_as_draft=True, taxes=[{"account_head": "Service charge", "rate": 0, "tax_amount": 75}])
        out = api.get_expense_report(claim.id, db=self.db, current_user=self.me)
        self.assertEqual(out.total_taxes, 75.0)
        self.assertEqual(out.net_payable, 1075.0)

    def test_history_includes_payroll_reimbursement_entries(self):
        claim = self._submitted()
        crud.create_audit_log(self.db, self.company_id, self.owner.id, "create", "reimbursement", claim.id)
        hist = api.expense_report_history(claim.id, db=self.db, current_user=self.me)
        self.assertGreaterEqual(len(hist), 2)

    def test_claim_type_description_cleared_is_none(self):
        self.assertIsNone(schemas.ExpenseClaimTypeUpdate(description="   ").description)


# The inherited TravelExpenseTests test_* methods are run by their own
# module; don't run them twice here.
for _name in [n for n in dir(base.TravelExpenseTests) if n.startswith("test_")]:
    setattr(ExpenseReportFixTests, _name, None)


if __name__ == "__main__":
    unittest.main()
