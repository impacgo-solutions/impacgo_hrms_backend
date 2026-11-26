"""The "Add custom..." row of a dropdown: any signed-in user can add an option
to a list that accepts them; rules, limits and the admin switch.
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

from app import crud, database, form_config as fc, models  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import form_config as api  # noqa: E402

TENANT = os.environ.get("TEST_TENANT", "impacgo-solutions")


class QuickAddTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User)).all()
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                           and r.name == _OWNER_ROLE_NAME), None)
        self.plain = next((u for u in users if self.owner is not None and u.id != self.owner.id
                           and u.company_id == self.owner.company_id
                           and crud.effective_user_matrix(self.db, u).get("system_settings_rbac") not in ("e", "a")), None)
        if self.owner is None or self.plain is None:
            self.skipTest("need an owner and a non-admin user")
        self.cid = self.owner.company_id

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def add(self, key, label, user=None):
        return api.quick_add_option(api.QuickAddIn(field_key=key, label=label), db=self.db, current_user=user or self.plain)

    def status_of(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code

    def test_a_plain_user_can_add_and_everyone_then_sees_it(self):
        cfg = api.get_form_config(db=self.db, current_user=self.plain)
        self.assertIn("travel.mode", cfg.quick_add)
        self.assertNotIn("salary_component.calculation", cfg.quick_add)   # fixed lists are never addable
        out = self.add("travel.mode", "  Metro   Rail ")
        self.assertEqual((out.label, out.value, out.created), ("Metro Rail", "Metro Rail", True))
        # validated by the module like any other option, visible to others, at the end of the list
        self.assertEqual(fc.require_option(self.db, self.cid, "travel.mode", "metro rail"), "Metro Rail")
        labels = [o.label for o in api.get_form_config(db=self.db, current_user=self.owner).options["travel.mode"]]
        self.assertEqual(labels[-1], "Metro Rail")
        # audited with who added it
        row = next(r for r in fc._rows(self.db, self.cid)["travel.mode"] if r.label == "Metro Rail")
        self.assertEqual(row.created_by, self.plain.id)
        self.assertFalse(row.is_system)

    def test_existing_name_is_reused_not_duplicated(self):
        a = self.add("travel.mode", "Ferry")
        b = self.add("travel.mode", "FERRY")
        c = self.add("travel.mode", "Flight")      # a built-in one
        self.assertEqual((a.created, b.created, b.label, c.created, c.label), (True, False, "Ferry", False, "Flight"))
        self.assertEqual(sum(1 for r in fc._rows(self.db, self.cid)["travel.mode"] if r.label.lower() == "ferry"), 1)

    def test_hidden_option_is_not_silently_revived(self):
        row = next(r for r in fc._rows(self.db, self.cid)["travel.mode"] if r.label == "Bus")
        api.update_option(row.id, api.OptionUpdate(is_active=False), db=self.db, current_user=self.owner)
        self.assertEqual(self.status_of(self.add, "travel.mode", "bus"), 409)

    def test_fixed_unknown_and_foreign_fields_are_refused(self):
        self.assertEqual(self.status_of(self.add, "salary_component.calculation", "Magic"), 403)
        self.assertEqual(self.status_of(self.add, "task.column", "Blocked"), 403)
        self.assertEqual(self.status_of(self.add, "nope.nothing", "x"), 403)
        self.assertEqual(self.status_of(self.add, f"custom.{uuid.uuid4()}", "x"), 403)

    def test_label_cleaning(self):
        for bad in ("   ", "!!!", "a\x07b", "x" * 121):
            self.assertIn(self.status_of(self.add, "travel.mode", bad), (403, 422), repr(bad))
        self.assertEqual(self.status_of(self.add, "travel.mode", "!!!"), 422)
        self.assertEqual(self.status_of(self.add, "travel.mode", "x" * 121), 422)
        self.assertEqual(self.add("travel.mode", "a\tb\n c").label, "a b c")

    def test_admin_can_switch_a_list_off_and_on(self):
        api.set_field_setting(api.FieldSettingIn(field_key="travel.mode", allow_user_add=False), db=self.db, current_user=self.owner)
        self.assertNotIn("travel.mode", api.get_form_config(db=self.db, current_user=self.plain).quick_add)
        self.assertEqual(self.status_of(self.add, "travel.mode", "Tram"), 403)
        adm = api.get_admin_config(db=self.db, current_user=self.owner)
        self.assertFalse(next(f for f in adm.fields if f.key == "travel.mode").allow_user_add)
        self.assertTrue(next(f for f in adm.fields if f.key == "travel.type").allow_user_add)
        # admins can still add through Form Configuration itself
        api.create_option(api.OptionCreate(field_key="travel.mode", label="Tram"), db=self.db, current_user=self.owner)
        api.set_field_setting(api.FieldSettingIn(field_key="travel.mode", allow_user_add=True), db=self.db, current_user=self.owner)
        self.assertIn("travel.mode", api.get_form_config(db=self.db, current_user=self.plain).quick_add)
        self.assertEqual(self.add("travel.mode", "Tram").created, False)
        self.assertEqual(self.status_of(api.set_field_setting, api.FieldSettingIn(field_key="nope.x", allow_user_add=False),
                                        db=self.db, current_user=self.owner), 404)

    def test_custom_dropdown_fields_accept_user_options_text_fields_do_not(self):
        dd = api.create_custom_field(api.CustomFieldCreate(form_key="asset", label="Vendor", field_type="dropdown",
                                                           options=["Dell", "HP"]), db=self.db, current_user=self.owner)
        tx = api.create_custom_field(api.CustomFieldCreate(form_key="asset", label="Note", field_type="text"),
                                     db=self.db, current_user=self.owner)
        self.assertIn(f"custom.{dd.id}", api.get_form_config(db=self.db, current_user=self.plain).quick_add)
        self.assertNotIn(f"custom.{tx.id}", api.get_form_config(db=self.db, current_user=self.plain).quick_add)
        self.add(f"custom.{dd.id}", "Lenovo")
        self.assertEqual(fc.validate_custom_values(self.db, self.cid, "asset", {"vendor": "lenovo"}), {"vendor": "Lenovo"})
        self.assertEqual(self.status_of(self.add, f"custom.{tx.id}", "x"), 403)
        # hiding the custom field takes it out of the addable list
        api.update_custom_field(dd.id, api.CustomFieldUpdate(is_active=False), db=self.db, current_user=self.owner)
        self.assertNotIn(f"custom.{dd.id}", api.get_form_config(db=self.db, current_user=self.plain).quick_add)

    def test_rate_limit_per_user(self):
        fc.QUICK_ADD_PER_USER_PER_HOUR, old = 3, fc.QUICK_ADD_PER_USER_PER_HOUR
        try:
            for i in range(3):
                self.add("travel.mode", f"Mode {i}")
            self.assertEqual(self.status_of(self.add, "travel.mode", "Mode 4"), 429)
            self.add("travel.mode", "Mode 0")                        # re-using an existing one is free
            self.add("travel.mode", "Mode owner", user=self.owner)   # the limit is per user
        finally:
            fc.QUICK_ADD_PER_USER_PER_HOUR = old


if __name__ == "__main__":
    unittest.main()
