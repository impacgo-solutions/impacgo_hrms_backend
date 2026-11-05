"""Administration > Form Configuration: dropdown options + custom fields.
Runs in a rolled-back transaction (tenant impacgo-solutions by default)."""
from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import database, form_config as fc, models  # noqa: E402
from app.routers import form_config as api  # noqa: E402

TENANT = os.environ.get("TEST_TENANT", "impacgo-solutions")


class FormConfigTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.user = self.db.scalars(select(models.User)).first()
        if self.user is None:
            self.skipTest("no user")
        self.cid = self.user.company_id

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def status_of(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code

    def test_defaults_are_seeded_and_served(self):
        cfg = api.get_form_config(db=self.db, current_user=self.user)
        self.assertEqual([o.label for o in cfg.options["travel.type"]], ["Domestic", "International"])
        self.assertGreaterEqual(len(cfg.options["candidate.source"]), 8)  # 9 on a fresh company (Career Site added)
        self.assertEqual([o.value for o in cfg.options["asset.status"]], ["available", "assigned", "in_repair"])
        # seeded once: a second read creates no duplicates
        rows = self.db.scalars(select(models.FormFieldOption).where(
            models.FormFieldOption.company_id == self.cid, models.FormFieldOption.field_key == "travel.type")).all()
        self.assertEqual(len(rows), 2)

    def test_option_lifecycle_add_rename_hide_delete_reorder(self):
        new = api.create_option(api.OptionCreate(field_key="travel.type", label="Regional"),
                                db=self.db, current_user=self.user)
        self.assertFalse(new.is_system)
        self.assertEqual(fc.require_option(self.db, self.cid, "travel.type", "regional"), "Regional")
        # duplicates (case-insensitive) are refused
        self.assertEqual(self.status_of(api.create_option, api.OptionCreate(field_key="travel.type", label="DOMESTIC"),
                                        db=self.db, current_user=self.user), 409)
        # rename keeps the stored value
        renamed = api.update_option(new.id, api.OptionUpdate(label="Regional Trip"), db=self.db, current_user=self.user)
        self.assertEqual((renamed.label, renamed.value), ("Regional Trip", "Regional"))
        # reorder: put the new one first
        rows = fc._rows(self.db, self.cid)["travel.type"]
        order = [new.id] + [r.id for r in rows if r.id != new.id]
        out = api.reorder_options(api.OptionOrder(field_key="travel.type", option_ids=order),
                                  db=self.db, current_user=self.user)
        self.assertEqual(out[0].id, new.id)
        self.assertEqual(self.status_of(api.reorder_options, api.OptionOrder(field_key="travel.type", option_ids=order[:-1]),
                                        db=self.db, current_user=self.user), 422)
        # a built-in option is hidden, not deleted; a hidden option can't be picked any more
        dom = next(r for r in fc._rows(self.db, self.cid)["travel.type"] if r.value == "Domestic")
        self.assertEqual(api.delete_option(dom.id, db=self.db, current_user=self.user), {"deleted": False, "hidden": True})
        self.assertEqual(self.status_of(fc.require_option, self.db, self.cid, "travel.type", "Domestic"), 422)
        self.assertEqual(fc.require_option(self.db, self.cid, "travel.type", "Domestic", current="Domestic"), "Domestic")
        # an admin-added option is really deleted
        self.assertEqual(api.delete_option(new.id, db=self.db, current_user=self.user), {"deleted": True, "hidden": False})
        # never leave a list empty
        self.assertEqual(self.status_of(api.delete_option,
                                        next(r for r in fc._rows(self.db, self.cid)["travel.type"] if r.is_active).id,
                                        db=self.db, current_user=self.user), 409)

    def test_fixed_lists_cannot_grow_but_can_be_relabelled(self):
        self.assertEqual(self.status_of(api.create_option, api.OptionCreate(field_key="salary_component.calculation", label="Magic"),
                                        db=self.db, current_user=self.user), 409)
        flat = next(r for r in fc.options(self.db, self.cid, "salary_component.calculation") if r[0] == "flat")
        self.assertEqual(flat[1], "Flat amount")
        row = next(r for r in fc._rows(self.db, self.cid)["salary_component.calculation"] if r.value == "flat")
        api.update_option(row.id, api.OptionUpdate(label="Fixed amount"), db=self.db, current_user=self.user)
        self.assertEqual(fc.option_label(self.db, self.cid, "salary_component.calculation", "flat"), "Fixed amount")

    def test_unknown_field_and_foreign_ids(self):
        self.assertEqual(self.status_of(api.create_option, api.OptionCreate(field_key="nope.nothing", label="X"),
                                        db=self.db, current_user=self.user), 404)
        self.assertEqual(self.status_of(api.update_option, uuid.uuid4(), api.OptionUpdate(label="X"),
                                        db=self.db, current_user=self.user), 404)
        self.assertEqual(self.status_of(api.delete_custom_field, uuid.uuid4(), db=self.db, current_user=self.user), 404)

    def test_custom_field_types_validation_and_values(self):
        def make(label, ftype, **kw):
            return api.create_custom_field(api.CustomFieldCreate(form_key="asset", label=label, field_type=ftype, **kw),
                                           db=self.db, current_user=self.user)
        serial = make("Serial No", "text", is_required=True)
        warranty = make("Warranty Until", "date")
        cost = make("Purchase Cost", "number")
        grade = make("Condition", "dropdown", options=["New", "Used"])
        insured = make("Insured", "checkbox")
        self.assertEqual(serial.key, "serial_no")
        # duplicates / bad type / empty dropdown
        self.assertEqual(self.status_of(make, "serial no", "text"), 409)
        self.assertEqual(self.status_of(make, "X", "banana"), 422)
        self.assertEqual(self.status_of(make, "Empty", "dropdown"), 422)

        def vals(**v):
            return fc.validate_custom_values(self.db, self.cid, "asset", v)
        with self.assertRaises(HTTPException):          # required missing on create
            vals(warranty="2027-01-01")
        ok = vals(serial_no=" SN-1 ", warranty_until="2027-01-31", purchase_cost="1250.50",
                  condition="used", insured=True)
        self.assertEqual(ok, {"serial_no": "SN-1", "warranty_until": "2027-01-31", "purchase_cost": "1250.5",
                              "condition": "Used", "insured": "true"})
        for bad in ({"serial_no": "x", "warranty_until": "31/01/2027"}, {"serial_no": "x", "purchase_cost": "abc"},
                    {"serial_no": "x", "condition": "Broken"}, {"serial_no": "x", "insured": "maybe"},
                    {"serial_no": "x", "ghost": "1"}):
            with self.assertRaises(HTTPException) as ctx:
                vals(**bad)
            self.assertEqual(ctx.exception.status_code, 422, bad)
        # persist / read / clear / partial update
        asset_id = uuid.uuid4()
        fc.save_custom_values(self.db, self.cid, "asset", asset_id, ok)
        self.assertEqual(fc.load_custom_values(self.db, self.cid, "asset", [asset_id])[asset_id]["condition"], "Used")
        part = fc.validate_custom_values(self.db, self.cid, "asset", {"purchase_cost": ""}, partial=True)
        fc.save_custom_values(self.db, self.cid, "asset", asset_id, part)
        self.assertNotIn("purchase_cost", fc.load_custom_values(self.db, self.cid, "asset", [asset_id])[asset_id])
        # a different form has no such fields; the config endpoint serves the asset ones
        self.assertEqual(self.status_of(fc.validate_custom_values, self.db, self.cid, "task", {"serial_no": "1"}), 422)
        cfg = api.get_form_config(db=self.db, current_user=self.user)
        self.assertEqual([f.key for f in cfg.custom_fields["asset"]],
                         ["serial_no", "warranty_until", "purchase_cost", "condition", "insured"])
        # hide, rename, make optional
        api.update_custom_field(serial.id, api.CustomFieldUpdate(label="Serial Number", is_required=False, is_active=False),
                                db=self.db, current_user=self.user)
        self.assertEqual(fc.validate_custom_values(self.db, self.cid, "asset", {}), {})
        self.assertNotIn("serial_no", [d.field_key for d in fc.custom_field_defs(self.db, self.cid, "asset")])
        # delete removes values and dropdown options with it
        api.delete_custom_field(grade.id, db=self.db, current_user=self.user)
        self.assertNotIn("condition", fc.load_custom_values(self.db, self.cid, "asset", [asset_id]).get(asset_id, {}))
        left = self.db.scalars(select(models.FormFieldOption).where(
            models.FormFieldOption.field_key == f"custom.{grade.id}")).all()
        self.assertEqual(left, [])

    def test_dropdown_custom_field_options_are_managed_like_any_list(self):
        f = api.create_custom_field(api.CustomFieldCreate(form_key="course", label="Level", field_type="dropdown",
                                                          options=["Basic", "Advanced"]), db=self.db, current_user=self.user)
        added = api.create_option(api.OptionCreate(field_key=f"custom.{f.id}", label="Expert"),
                                  db=self.db, current_user=self.user)
        self.assertEqual([o.label for o in fc.custom_field_defs(self.db, self.cid, "course") and
                          api.get_form_config(db=self.db, current_user=self.user).custom_fields["course"][0].options],
                         ["Basic", "Advanced", "Expert"])
        api.delete_option(added.id, db=self.db, current_user=self.user)
        with self.assertRaises(HTTPException):
            fc.validate_custom_values(self.db, self.cid, "course", {"level": "Expert"})
        # options only make sense on dropdown fields
        txt = api.create_custom_field(api.CustomFieldCreate(form_key="course", label="Note", field_type="text"),
                                      db=self.db, current_user=self.user)
        self.assertEqual(self.status_of(api.create_option, api.OptionCreate(field_key=f"custom.{txt.id}", label="x"),
                                        db=self.db, current_user=self.user), 409)


if __name__ == "__main__":
    unittest.main()
