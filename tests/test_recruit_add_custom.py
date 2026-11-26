"""Recruitment work mode is the configurable employee.work_mode list: an option
added through quick-add is accepted by the recruitment validators, a hidden one
is rejected, an unchanged legacy value still saves.

    cd backend && venv/Scripts/python -m unittest tests.test_recruit_add_custom -v

Runs in a transaction that is ALWAYS rolled back.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, form_config as fc, models, recruitment_workflow as rw  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402

TENANT = os.environ.get("TEST_TENANT", "impacgo-solutions")


class RecruitWorkModeTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        self.owner = next((u for u in users
                           if (crud.get_user_primary_role(self.db, u.id) or models.Role()).name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("tenant needs an Owner user")
        self.ctx = rw.make_ctx(self.db, self.owner)

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def code(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as c:
            fn(*a, **k)
        return c.exception.status_code

    def test_work_mode_custom_option(self):
        self.assertEqual(rw.check_work_mode(self.ctx, "hybrid"), "Hybrid")
        self.assertEqual(self.code(rw.check_work_mode, self.ctx, "Field"), 422)
        self.assertEqual(self.code(rw._validate_common, self.ctx, {"work_mode": "Field"}), 422)
        row, _ = fc.quick_add_option(self.db, self.ctx.company_id, self.owner.id, "employee.work_mode", "Field")
        d = {"work_mode": "field"}
        rw._validate_common(self.ctx, d)
        self.assertEqual(d["work_mode"], "Field")
        # hidden: new use rejected, unchanged legacy value still saves
        row.is_active = False
        self.db.flush()
        self.assertEqual(self.code(rw.check_work_mode, self.ctx, "Field"), 422)
        self.assertEqual(rw.check_work_mode(self.ctx, "Field", current="Field"), "Field")
        rw._validate_common(self.ctx, {"work_mode": "Field"}, "Field")
        # the columns are varchar(120) (add_form_config_work_mode_width.sql): a long label that
        # quick-add allows (<= 120) is accepted, so a user-added mode never fails later on save
        long_label = "X" * 100
        fc.quick_add_option(self.db, self.ctx.company_id, self.owner.id, "employee.work_mode", long_label)
        self.assertEqual(rw.check_work_mode(self.ctx, long_label), long_label)


if __name__ == "__main__":
    unittest.main()
