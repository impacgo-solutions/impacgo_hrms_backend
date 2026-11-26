"""Notification dismiss / clear-read: only the caller's own rows are touched.
Runs in a rolled-back transaction."""
from __future__ import annotations

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models  # noqa: E402
from app.routers import notifications as api  # noqa: E402

TENANT = os.environ.get("TEST_TENANT", "impacgo-solutions")


class NotificationDismissTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User)).all()
        by_company: dict = {}
        for u in users:
            by_company.setdefault(u.company_id, []).append(u)
        group = next((g for g in by_company.values() if len(g) >= 2), None)
        if group is None:
            self.skipTest("need two users in one company")
        self.a, self.b = group[0], group[1]

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def make(self, user, title, read=False):
        n = crud.create_notification(self.db, user.company_id, user.id, title, "body")
        if read:
            n.read_at = datetime.datetime.now(datetime.timezone.utc)
        self.db.flush()
        return n

    def titles(self, user):
        return {n.title for n in crud.list_notifications(self.db, user.id)}

    def test_dismiss_own_only(self):
        mine = self.make(self.a, "QA mine")
        theirs = self.make(self.b, "QA theirs")
        with self.assertRaises(HTTPException) as ctx:
            api.dismiss_notification(theirs.id, db=self.db, current_user=self.a)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("QA theirs", self.titles(self.b))
        api.dismiss_notification(mine.id, db=self.db, current_user=self.a)
        self.assertNotIn("QA mine", self.titles(self.a))

    def test_clear_read_keeps_unread_and_other_users(self):
        self.make(self.a, "QA read", read=True)
        self.make(self.a, "QA unread")
        self.make(self.b, "QA other read", read=True)
        out = api.clear_read_notifications(db=self.db, current_user=self.a)
        self.assertGreaterEqual(out["removed"], 1)
        self.assertNotIn("QA read", self.titles(self.a))
        self.assertIn("QA unread", self.titles(self.a))
        self.assertIn("QA other read", self.titles(self.b))


if __name__ == "__main__":
    unittest.main()
