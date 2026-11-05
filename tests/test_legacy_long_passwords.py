"""Accounts hashed from a >72-byte password before the bcrypt fix: never
accepted (no fallback), flagged for an admin reset at their first sign-in
attempt, listed for admins, cleared by a reset -- and every new / reset
password goes through security.hash_password.

    cd backend && venv/Scripts/python -m unittest tests.test_legacy_long_passwords -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant acme by default, ACCESS_TEST_TENANT to override). The
flag tests need db/add_password_reset_required.sql to have been run; without
it they are skipped and the rest still runs.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bcrypt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app import auth_state, crud, database, deps, main, models, password_admin, schemas, security  # noqa: E402
from app.routers import auth as auth_api  # noqa: E402
from tests.test_employee_access_lifecycle import _Base  # noqa: E402

LONG = "Harbor-Lights-" * 6 + "Tail-One!"       # 93 bytes
SAME_PREFIX = LONG[:72] + "Other-Tail?"         # shares the first 72 bytes
NEW_PASSWORD = "Gl@ss.Kettle.88"


def legacy_hash(password: str) -> str:
    """Exactly what security.hash_password produced before the fix."""
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(4)).decode()


class LegacyLongPasswordTests(_Base):
    def setUp(self):
        super().setUp()
        auth_state._memory_failures.clear()
        self.addCleanup(auth_state._memory_failures.clear)
        self.owner = self._owner()
        self.employee, self.user = self._pick_employee(self.owner)
        self.public = self._public(self.user)
        self.db.execute(text("UPDATE public.users SET password_hash = :h, failed_login_attempts = 0, "
                             "locked_until = NULL WHERE id = :id"),
                        {"h": legacy_hash(LONG), "id": self.public.id})
        self.db.flush()
        main.app.dependency_overrides[database.get_db] = lambda: self.db
        main.app.dependency_overrides[deps.get_current_user] = lambda: self.owner
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)
        self.has_flag = auth_state.has_reset_required_column(self.db, self.tenant)

    def _login(self, password):
        try:
            auth_api.login(schemas.LoginRequest(email=self.public.email, password=password), self.db)
        except Exception as exc:  # HTTPException
            return exc.status_code, exc.detail
        return 200, None

    def _flagged(self):
        return self.db.execute(text("SELECT password_reset_required FROM core_users WHERE id = :id"),
                               {"id": self.user.id}).scalar()

    def _need_flag(self):
        if not self.has_flag:
            self.skipTest("run db/add_password_reset_required.sql to test the flag")

    # ── refusal, no fallback ──
    def test_long_legacy_password_is_refused_with_reset_message(self):
        for attempt in (LONG, SAME_PREFIX):
            with self.subTest(attempt=attempt[-10:]):
                self.assertEqual(self._login(attempt), (403, auth_api.PASSWORD_RESET_REQUIRED))

    def test_other_wrong_passwords_stay_generic(self):
        self.assertEqual(self._login("Wrong-Password-1"), (401, auth_api._INVALID_CREDENTIALS))
        self.assertEqual(self._login("X" * 90), (401, auth_api._INVALID_CREDENTIALS))

    def test_prefix_is_refused_once_flagged(self):
        """Before the account is identified, its old hash IS a hash of the
        first 72 bytes, so that prefix is indistinguishable from a real
        72-byte password. After the first long-password attempt flags the
        account, the prefix is refused too, until an admin reset."""
        self._need_flag()
        self.assertEqual(self._login(LONG)[0], 403)
        self.assertEqual(self._login(LONG[:72]), (403, auth_api.PASSWORD_RESET_REQUIRED))

    def test_unknown_email_takes_the_same_bcrypt_path(self):
        calls = []
        real = bcrypt.checkpw

        def counting(pw, h):
            calls.append(h)
            return real(pw, h)

        with mock.patch.object(bcrypt, "checkpw", counting):
            self._login(SAME_PREFIX)
            known = len(calls)
            calls.clear()
            try:
                auth_api.login(schemas.LoginRequest(email="nobody-long@nowhere.invalid", password=SAME_PREFIX),
                               self.db)
            except Exception as exc:
                self.assertEqual(exc.status_code, 401)
        self.assertEqual(known, 2)
        self.assertEqual(len(calls), 2)

    # ── flag, listing, reset ──
    def test_attempt_flags_account_and_admin_sees_it(self):
        self._need_flag()
        self.assertFalse(self._flagged())
        self._login(LONG)
        self.assertTrue(self._flagged())
        listed = self.client.get("/api/users/password-reset-required").json()
        self.assertIn(str(self.user.id), [r["user_id"] for r in listed])

    def test_admin_reset_clears_flag_and_new_password_works(self):
        if self.has_flag:
            self._login(LONG)
            self.assertTrue(self._flagged())
        for new in (NEW_PASSWORD, LONG):  # short, and the same long password, now hashed safely
            with self.subTest(len=len(new)):
                resp = self.client.post(f"/api/users/{self.user.id}/reset-password", json={"new_password": new})
                self.assertEqual(resp.status_code, 204, resp.text)
                stored = self.db.execute(text("SELECT password_hash FROM public.users WHERE id = :id"),
                                         {"id": self.public.id}).scalar()
                self.assertTrue(stored.startswith("$2b$12$"))
                self.assertTrue(security.verify_password(new, stored))
                self.assertFalse(security.matches_legacy_truncated_hash(new, stored))
                self.assertEqual(self._login(new)[0], 200)
                if len(new) > 72:
                    self.assertEqual(self._login(SAME_PREFIX)[0], 401)  # no truncation collision
                if self.has_flag:
                    self.assertFalse(self._flagged())
        if self.has_flag:
            self.assertNotIn(str(self.user.id), [r["user_id"] for r in
                                                 self.client.get("/api/users/password-reset-required").json()])

    def test_listing_is_empty_without_migration(self):
        if self.has_flag:
            self.skipTest("migration present")
        self.assertEqual(self.client.get("/api/users/password-reset-required").json(), [])

    def test_reset_updates_the_row_login_reads(self):
        """public.users found the way auth finds it (not only by equal id)."""
        pid = auth_state.public_user_id_for_core_user(self.db, self.user)
        self.assertTrue(crud.reset_user_password(self.db, self.user.id, security.hash_password(NEW_PASSWORD)))
        stored = self.db.execute(text("SELECT password_hash FROM public.users WHERE id = :id"),
                                 {"id": pid}).scalar()
        self.assertTrue(security.verify_password(NEW_PASSWORD, stored))

    # ── platform accounts ──
    def test_platform_admin_with_long_legacy_password_is_refused(self):
        admins = [a for a in self.db.scalars(select(models.AdminUser)).all()
                  if crud.is_platform_super_admin(self.db, a)]
        if not admins:
            self.skipTest("no platform admin")
        admin = admins[0]
        admin.password_hash = legacy_hash(LONG)
        self.db.flush()
        resp = self.client.post("/api/super-admin/login", json={"email": admin.email, "password": LONG})
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertIn("must be reset", resp.json()["detail"])
        self.assertEqual(self.client.post("/api/super-admin/login",
                                          json={"email": admin.email, "password": "Wrong-Password-1"}).status_code, 401)


class HrmsOnlyScopeTests(unittest.TestCase):
    """Nothing here may reach a tenant of another application (e.g. the
    retail-only etorgroups) or the shared public.users table."""

    def setUp(self):
        self.db = database.SessionLocal()
        self.addCleanup(self.db.close)
        self.non_hrms = [r for r in self.db.execute(text(
            "SELECT slug FROM public.tenants t WHERE NOT EXISTS (SELECT 1 FROM public.tenant_modules m "
            "WHERE m.tenant_slug = t.slug AND m.module_code = 'hcm' AND m.is_enabled)")).scalars()]
        if not self.non_hrms:
            self.skipTest("no non-HRMS tenant in this database")

    def test_flag_never_on_shared_or_other_tenants(self):
        self.assertEqual(self.db.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'users' AND column_name LIKE 'password_reset%'")).scalar(), 0)
        for slug in self.non_hrms:
            with self.subTest(slug):
                self.assertFalse(auth_state.is_hrms_tenant(self.db, slug))
                self.assertFalse(auth_state.has_reset_required_column(self.db, slug))

    def test_flagging_a_non_hrms_login_is_a_no_op(self):
        slug = self.non_hrms[0]
        fake = mock.Mock(tenant_slug=slug, employee_id=None, id=None)
        self.assertFalse(auth_state.flag_password_reset_required(self.db, fake))
        self.assertFalse(auth_state.is_password_reset_required(self.db, fake))

    def test_exit_job_skips_non_hrms_tenants(self):
        from app import access_lifecycle, reminders

        slug = self.non_hrms[0]
        with mock.patch.object(access_lifecycle, "deactivate_due_exits") as job, \
                mock.patch("app.overtime.advance_sessions", lambda *a: False), \
                mock.patch("app.shift_reminders.run_company", lambda *a: None), \
                mock.patch("app.compliance.run_company", lambda *a: False), \
                mock.patch.object(reminders.crud, "is_module_enabled", lambda *a: False), \
                mock.patch.object(reminders.crud, "company_now",
                                  lambda *a: __import__("datetime").datetime(2026, 1, 1, 0, 0)):
            reminders.run_tenant(slug)
        job.assert_not_called()


class PasswordAdminCliTests(unittest.TestCase):
    def test_hash_uses_app_hashing_and_policy(self):
        answers = iter(["weak", "weak", NEW_PASSWORD, NEW_PASSWORD])  # weak one is re-prompted
        out = io.StringIO()
        with mock.patch("getpass.getpass", lambda prompt="": next(answers)), redirect_stdout(out), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(password_admin.main(["hash"]), 0)
        printed = out.getvalue().strip()
        self.assertTrue(printed.startswith("$2b$12$"))
        self.assertTrue(security.verify_password(NEW_PASSWORD, printed))
        self.assertNotIn(NEW_PASSWORD, printed)
        self.assertIn("at least 8", err.getvalue())

    def test_long_password_hash_has_no_truncation(self):
        answers = iter([LONG, LONG])
        out = io.StringIO()
        with mock.patch("getpass.getpass", lambda prompt="": next(answers)), redirect_stdout(out):
            password_admin.main(["hash"])
        h = out.getvalue().strip()
        self.assertTrue(security.verify_password(LONG, h))
        self.assertFalse(security.verify_password(SAME_PREFIX, h))


if __name__ == "__main__":
    unittest.main()
