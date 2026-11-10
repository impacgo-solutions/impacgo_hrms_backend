"""Approvals > Recent Decisions: GET /api/approvals/decisions search, filters,
sorting and pagination over every decided request (current and migrated),
with the inbox's visibility rule.

    cd backend && venv/Scripts/python -m unittest tests.test_recent_decisions_filters -v

Read-only, inside a transaction that is ALWAYS rolled back (tenant
impacgo-solutions by default, DECISIONS_TEST_TENANT to override).
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models  # noqa: E402
from app.routers import approval_inbox as api  # noqa: E402

TENANT = os.environ.get("DECISIONS_TEST_TENANT", "impacgo-solutions")
NONE = dict(q=None, types=None, statuses=None, employee_ids=None, department_ids=None, details=None,
            decided_by=None, date_from=None, date_to=None, sort="decided_at", order="desc", page=1, page_size=200)


class RecentDecisionsTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User).where(
            models.User.status == "active", models.User.employee_id.is_not(None))).all()
        self.owner = next((u for u in users if crud.is_fallback_approver(self.db, u)), None)
        if self.owner is None:
            self.skipTest("no fallback approver in tenant")
        self.users = users
        self.all = self.call()
        if self.all["total"] == 0:
            self.skipTest("no decided requests in tenant")

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def call(self, user=None, **kw):
        args = dict(NONE)
        args.update(kw)
        return api.recent_decisions(db=self.db, current_user=user or self.owner, **args)

    def test_rows_are_decided_complete_and_exclude_own_requests(self):
        for r in self.all["items"]:
            self.assertIn(r["status"], ("Approved", "Rejected", "Sent Back"))
            self.assertTrue(r["type"] and r["employee_name"] and r["id"])
            self.assertTrue(r["decided_at"], f"migrated row without a date: {r['id']}")
            self.assertNotEqual(r["employee_id"], str(self.owner.employee_id))
        dates = [r["decided_at"] for r in self.all["items"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_each_filter_narrows_correctly_and_filters_combine(self):
        first = self.all["items"][0]
        by_type = self.call(types=[first["type"]])
        self.assertTrue(by_type["items"] and all(r["type"] == first["type"] for r in by_type["items"]))
        by_emp = self.call(employee_ids=[first["employee_id"]])
        self.assertTrue(all(r["employee_id"] == first["employee_id"] for r in by_emp["items"]))
        by_status = self.call(statuses=[first["status"]])
        self.assertTrue(all(r["status"] == first["status"] for r in by_status["items"]))
        if first["department_id"]:
            by_dept = self.call(department_ids=[first["department_id"]])
            self.assertTrue(all(r["department_id"] == first["department_id"] for r in by_dept["items"]))
        both = self.call(types=[first["type"]], employee_ids=[first["employee_id"]], statuses=[first["status"]])
        expected = {r["id"] for r in by_type["items"]} & {r["id"] for r in by_emp["items"]} & {
            r["id"] for r in by_status["items"]}
        self.assertEqual({r["id"] for r in both["items"]}, expected)
        self.assertIn(first["id"], expected)
        day = datetime.date.fromisoformat(first["decided_at"][:10])
        in_range = self.call(date_from=day, date_to=day)
        self.assertIn(first["id"], {r["id"] for r in in_range["items"]})
        self.assertTrue(all(r["decided_at"][:10] == day.isoformat() for r in in_range["items"]))
        self.assertEqual(self.call(date_from=datetime.date(1990, 1, 1), date_to=datetime.date(1990, 1, 2))["total"], 0)

    def test_search_by_name_code_request_id_and_details(self):
        first = self.all["items"][0]
        for q in filter(None, [first["employee_name"].split()[0], first["employee_code"], first["request_no"],
                               first["id"][:8], first["details"].split(" · ")[0]]):
            ids = {r["id"] for r in self.call(q=q)["items"]}
            self.assertIn(first["id"], ids, f"search {q!r}")
        self.assertIn(first["id"], {r["id"] for r in self.call(q=first["employee_name"].upper())["items"]})
        self.assertEqual(self.call(q="zz-no-such-thing-zz")["total"], 0)
        self.assertIn(first["id"], {r["id"] for r in self.call(details=first["details"][:6])["items"]})

    def test_pagination_covers_everything_once(self):
        total, seen, page = self.all["total"], [], 1
        while True:
            res = self.call(page=page, page_size=3)
            self.assertEqual(res["total"], total)
            if not res["items"]:
                break
            seen += [r["id"] for r in res["items"]]
            page += 1
        self.assertEqual(len(seen), total)
        self.assertEqual(seen, [r["id"] for r in self.all["items"]][:total])

    def test_other_users_never_see_more_than_the_approver(self):
        owner_ids = {r["id"] for r in self.all["items"]}
        for u in self.users[:6]:
            if u.id == self.owner.id:
                continue
            mine = self.call(user=u)
            self.assertTrue(all(r["employee_id"] != str(u.employee_id) for r in mine["items"]))
            if not crud.is_fallback_approver(self.db, u):
                # everything they see the approver sees too (or it's the approver's own request)
                extra = {r["id"] for r in mine["items"]} - owner_ids
                for r in mine["items"]:
                    if r["id"] in extra:
                        self.assertEqual(r["employee_id"], str(self.owner.employee_id))


if __name__ == "__main__":
    unittest.main()
