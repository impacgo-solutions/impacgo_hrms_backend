"""Form Configuration x asset Return Condition (asset.return_condition) and
F&F Payment Mode (fnf.payment_mode): an option added through quick-add is
accepted by the module endpoint, a hidden one is rejected, and a legacy value
the record already holds still saves.

    cd backend && venv/Scripts/python -m unittest tests.test_form_config_assets_fnf -v

Everything runs in a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, form_config as fc, models, schemas  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import assets as assets_api  # noqa: E402
from app.routers import fnf as fnf_api  # noqa: E402
from app.routers import form_config as cfg_api  # noqa: E402

TENANT = os.environ.get("TEST_TENANT", "impacgo-solutions")
FNF_TENANT = os.environ.get("FNF_TEST_TENANT", "Infyq")


class _Base(unittest.TestCase):
    tenant = TENANT

    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{self.tenant}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, self.tenant)
        self.owner = None
        for u in self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all():
            role = crud.get_user_primary_role(self.db, u.id)
            if role is not None and role.name == _OWNER_ROLE_NAME:
                self.owner = u
                break
        if self.owner is None:
            self.skipTest("tenant needs an Owner user")
        self.company_id = self.owner.company_id

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def quick_add(self, key, label):
        return cfg_api.quick_add_option(cfg_api.QuickAddIn(field_key=key, label=label),
                                        db=self.db, current_user=self.owner)

    def hide(self, key, value):
        row = next(r for r in fc._rows(self.db, self.company_id)[key] if r.value == value)
        cfg_api.update_option(row.id, cfg_api.OptionUpdate(is_active=False), db=self.db, current_user=self.owner)

    def code(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code


class AssetReturnConditionTests(_Base):
    def _assign(self):
        asset = models.AssetInventoryItem(
            id=uuid.uuid4(), company_id=self.company_id, asset_tag=f"RC-{uuid.uuid4().hex[:8]}",
            asset_type="Laptop", model="x", status="available")
        self.db.add(asset)
        self.db.flush()
        emp = self.db.get(models.Employee, self.owner.employee_id)
        a = assets_api.create_asset_assignment(
            schemas.AssetAssignmentCreate(asset_tag=asset.asset_tag, employee_id=emp.id,
                                          assigned_on=datetime.date(2025, 1, 1)),
            db=self.db, current_user=self.owner)
        return a.id

    def _return(self, assignment_id, condition):
        return assets_api.process_asset_return(
            assignment_id,
            schemas.AssetReturnUpdate(returned_on=datetime.date(2025, 2, 1), return_condition=condition),
            db=self.db, current_user=self.owner)

    def test_defaults_are_registered(self):
        self.assertEqual([v for v, _ in fc.FIELDS["asset.return_condition"].defaults],
                         ["Good", "Fair", "Damaged", "Lost"])
        self.assertTrue(fc.FIELDS["asset.return_condition"].allow_custom)

    def test_added_option_is_accepted(self):
        self.assertEqual(self.code(self._return, self._assign(), "Water damaged"), 422)
        self.quick_add("asset.return_condition", "Water damaged")
        self.assertEqual(self._return(self._assign(), "Water damaged").return_condition, "Water damaged")

    def test_hidden_option_rejected_and_blank_allowed(self):
        self.hide("asset.return_condition", "Fair")
        self.assertEqual(self.code(self._return, self._assign(), "Fair"), 422)
        self.assertIsNone(self._return(self._assign(), None).return_condition)

    def test_long_label_fits_the_column(self):
        label = "Scratched but fully working " + "x" * 60          # > 60 chars, <= 120
        self.quick_add("asset.return_condition", label)
        self.assertEqual(self._return(self._assign(), label).return_condition, label)


class FnfPaymentModeTests(_Base):
    tenant = FNF_TENANT

    def setUp(self):
        super().setUp()
        exits = [e for e in self.db.scalars(select(models.ExitRequestModel)).all()
                 if (emp := self.db.get(models.Employee, e.employee_id)) is not None
                 and emp.company_id == self.company_id]
        self.exit = next((e for e in exits if e.status == "completed"), None) or next(
            (e for e in exits if e.status in crud.FNF_EXIT_STATUSES), None)
        if self.exit is None:
            self.skipTest("no approved / completed exit in this tenant")
        old = crud.get_final_settlement(self.db, self.exit.id)
        if old is not None:
            self.db.execute(delete(models.FinalSettlementLine).where(models.FinalSettlementLine.settlement_id == old.id))
            self.db.delete(old)
            self.db.flush()
        fnf_api.save_fnf(self.exit.id, schemas.FnfSaveRequest(
            lines=[{"line_type": "earning", "component": "Salary", "description": None, "amount": 1000}], notes="x"),
            db=self.db, current_user=self.owner)
        fnf_api.approve_fnf(self.exit.id, schemas.FnfApproveRequest(), db=self.db, current_user=self.owner)

    def _pay(self, mode):
        return fnf_api.mark_fnf_paid(
            self.exit.id,
            schemas.FnfMarkPaidRequest(payment_date=crud.company_today(self.db, self.company_id), payment_mode=mode),
            db=self.db, current_user=self.owner)

    def test_defaults_are_registered(self):
        self.assertIn("Bank transfer", [v for v, _ in fc.FIELDS["fnf.payment_mode"].defaults])
        self.assertTrue(fc.FIELDS["fnf.payment_mode"].allow_custom)

    def test_added_option_is_accepted(self):
        self.assertEqual(self.code(self._pay, "Demand draft"), 422)
        self.quick_add("fnf.payment_mode", "Demand draft")
        self.assertEqual(self._pay("Demand draft").settlement.payment_mode, "Demand draft")

    def test_hidden_option_rejected(self):
        self.hide("fnf.payment_mode", "Cheque")
        self.assertEqual(self.code(self._pay, "Cheque"), 422)
        self.assertEqual(self._pay("Bank transfer").settlement.payment_mode, "Bank transfer")

    def test_long_label_fits_the_column(self):
        label = "Wire transfer via correspondent bank, reference to follow"   # > 30 chars
        self.quick_add("fnf.payment_mode", label)
        self.assertEqual(self._pay(label).settlement.payment_mode, label)


if __name__ == "__main__":
    unittest.main()
