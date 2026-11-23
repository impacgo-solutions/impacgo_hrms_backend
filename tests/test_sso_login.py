"""Portal single sign-on (POST /api/auth/sso) and proof that the password
login is unchanged by sharing its post-password path with it.

    cd backend && venv/Scripts/python -m unittest tests.test_sso_login -v

Needs public.portal_sso_codes (erp_portal/backend/migrations/001) -- skipped
where it isn't installed. Runs inside a transaction that is ALWAYS rolled
back (tenant acme by default, ACCESS_TEST_TENANT to override).
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import auth_state, schemas, security  # noqa: E402
from app.routers import auth as auth_api  # noqa: E402
from tests.test_employee_access_lifecycle import PASSWORD, _Base  # noqa: E402

GENERIC = auth_api._SSO_INVALID


class SsoLoginTests(_Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(auth_state._memory_failures.clear)
        auth_state._memory_failures.clear()
        if self.db.execute(text("SELECT to_regclass('public.portal_sso_codes')")).scalar() is None:
            self.skipTest("public.portal_sso_codes is not installed")
        self.owner = self._owner()
        self.email = self._set_password(self.owner)
        self.pub = self._public(self.owner)

    # ── helpers ──
    def _code(self, *, app="hrms", seconds=60, user_id=None, slug=None) -> str:
        code = uuid.uuid4().hex + uuid.uuid4().hex
        self.db.execute(
            text(
                "INSERT INTO public.portal_sso_codes (code_hash, user_id, tenant_slug, app, expires_at) "
                "VALUES (:h, :u, :s, :a, now() + make_interval(secs => :n))"
            ),
            {
                "h": hashlib.sha256(code.encode()).hexdigest(),
                "u": user_id or self.pub.id,
                "s": slug or self.tenant,
                "a": app,
                "n": seconds,
            },
        )
        self.db.flush()
        return code

    def _sso(self, code):
        try:
            return 200, auth_api.sso_login(schemas.SsoRequest(code=code), self.db)
        except HTTPException as exc:
            return exc.status_code, exc.detail

    def _assert_refused(self, code):
        self.assertEqual(self._sso(code), (401, GENERIC))

    def _public_set(self, sql, **params):
        self.db.execute(text(f"UPDATE public.users SET {sql} WHERE id = :id"), {"id": self.pub.id, **params})
        self.db.flush()

    # ── success ──
    def test_valid_code_gives_the_same_response_as_password_login(self):
        status, sso = self._sso(self._code())
        self.assertEqual(status, 200)
        login = self._login(self.email)
        self.assertEqual(set(sso.model_dump()), set(login.model_dump()))
        self.assertEqual(sso.role, login.role)
        self.assertEqual(sso.tenant_slug, login.tenant_slug)
        self.assertEqual(sso.company_id, login.company_id)
        self.assertEqual(sso.permissions, login.permissions)
        self.assertEqual(sso.employee.id, login.employee.id)
        self.assertEqual(sso.idle_timeout_minutes, login.idle_timeout_minutes)
        a, b = security.decode_access_token(sso.access_token), security.decode_access_token(login.access_token)
        self.assertEqual((a.user_id, a.public_user_id, a.tenant_slug, a.roles, a.token_version),
                         (b.user_id, b.public_user_id, b.tenant_slug, b.roles, b.token_version))

    def test_success_stamps_last_login_and_clears_failures(self):
        self._public_set("last_login_at = NULL, failed_login_attempts = 3")
        self.assertEqual(self._sso(self._code())[0], 200)
        row = self.db.execute(text("SELECT last_login_at, failed_login_attempts FROM public.users WHERE id = :id"),
                              {"id": self.pub.id}).first()
        self.assertIsNotNone(row.last_login_at)
        self.assertEqual(row.failed_login_attempts, 0)

    def test_token_carries_current_token_version(self):
        self._public_set("token_version = 7")
        _status, sso = self._sso(self._code())
        self.assertEqual(security.decode_access_token(sso.access_token).token_version, 7)

    # ── the code ──
    def test_code_is_single_use(self):
        code = self._code()
        self.assertEqual(self._sso(code)[0], 200)
        self._assert_refused(code)

    def test_expired_wrong_app_and_unknown_codes_look_identical(self):
        self._assert_refused(self._code(seconds=-5))
        self._assert_refused(self._code(app="chain"))
        self._assert_refused("not-a-real-code")

    def test_code_for_another_tenant_slug_is_refused(self):
        self._assert_refused(self._code(slug="some-other-tenant"))

    def test_code_for_unknown_user_is_refused(self):
        self._assert_refused(self._code(user_id=uuid.uuid4()))

    def test_refused_sign_in_still_burns_the_code(self):
        code = self._code()
        self._public_set("is_active = false")
        self._assert_refused(code)
        self._public_set("is_active = true")
        self._assert_refused(code)

    # ── every rule the password login enforces ──
    def test_inactive_user_refused(self):
        self._public_set("is_active = false")
        self._assert_refused(self._code())

    def test_locked_account_refused(self):
        self._public_set("failed_login_attempts = 5, locked_until = now() + interval '10 minutes'")
        self._assert_refused(self._code())

    def test_blocked_tenant_refused(self):
        self.db.execute(text("UPDATE public.tenants SET status = 'blocked' WHERE slug = :s"), {"s": self.tenant})
        self.db.flush()
        self._assert_refused(self._code())

    def test_ended_trial_refused(self):
        self.db.execute(
            text("UPDATE public.tenants SET status = 'trial', trial_ends_at = current_date - 1 WHERE slug = :s"),
            {"s": self.tenant},
        )
        self.db.flush()
        self._assert_refused(self._code())

    def test_tenant_without_hrms_module_refused(self):
        self.db.execute(
            text("UPDATE public.tenant_modules SET is_enabled = false WHERE tenant_slug = :s AND module_code = 'hcm'"),
            {"s": self.tenant},
        )
        self.db.flush()
        self._assert_refused(self._code())

    def test_password_reset_required_refused(self):
        if not auth_state.has_reset_required_column(self.db, self.tenant):
            self.skipTest("password_reset_required column not installed")
        auth_state.flag_password_reset_required(self.db, self.db.get(auth_api.models.PublicUser, self.pub.id))
        self.db.flush()
        self._assert_refused(self._code())

    def test_terminated_employee_refused(self):
        self.db.execute(
            text("UPDATE core_employees SET status = 'Terminated' WHERE id = :id"), {"id": self.owner.employee_id}
        )
        self.db.flush()
        self._assert_refused(self._code())

    def test_core_user_inactive_refused(self):
        self.db.execute(text("UPDATE core_users SET status = 'inactive' WHERE id = :id"), {"id": self.owner.id})
        self.db.flush()
        self._assert_refused(self._code())

    def test_user_without_role_refused(self):
        self.db.execute(text("DELETE FROM core_user_roles WHERE user_id = :id"), {"id": self.owner.id})
        self.db.flush()
        self._assert_refused(self._code())


class PasswordLoginUnchangedTests(_Base):
    """The refactor moved the post-password steps into _complete_sign_in;
    the password login must behave exactly as before."""

    def setUp(self):
        super().setUp()
        self.addCleanup(auth_state._memory_failures.clear)
        auth_state._memory_failures.clear()
        self.owner = self._owner()
        self.email = self._set_password(self.owner)

    def _attempt(self, email, password, remember=False):
        try:
            return 200, auth_api.login(
                schemas.LoginRequest(email=email, password=password, remember_me=remember), self.db)
        except HTTPException as exc:
            return exc.status_code, exc.detail

    def test_valid_login_shape(self):
        status, res = self._attempt(self.email, PASSWORD)
        self.assertEqual(status, 200)
        self.assertEqual(set(res.model_dump()), {
            "access_token", "token_type", "expires_at", "remember_me", "idle_timeout_minutes", "role",
            "employee", "tenant_slug", "company_id", "permissions"})
        self.assertEqual(res.tenant_slug, self.tenant)
        self.assertFalse(res.remember_me)
        self.assertFalse(security.decode_access_token(res.access_token).remember)

    def test_remember_me_still_honoured(self):
        status, res = self._attempt(self.email, PASSWORD, remember=True)
        self.assertEqual(status, 200)
        self.assertTrue(res.remember_me)
        self.assertEqual(res.idle_timeout_minutes, 0)
        self.assertTrue(security.decode_access_token(res.access_token).remember)

    def test_wrong_password_and_unknown_email_generic_401(self):
        self.assertEqual(self._attempt(self.email, "Wrong-Password-1"), (401, auth_api._INVALID_CREDENTIALS))
        self.assertEqual(self._attempt(f"nobody-{uuid.uuid4().hex}@nowhere.invalid", "x"),
                         (401, auth_api._INVALID_CREDENTIALS))

    def test_refusals_keep_their_status_and_message(self):
        pub = self._public(self.owner)
        self.db.execute(text("UPDATE public.users SET is_active = false WHERE id = :id"), {"id": pub.id})
        self.db.flush()
        status, detail = self._attempt(self.email, PASSWORD)
        self.assertEqual(status, 403)
        self.db.execute(text("UPDATE public.users SET is_active = true WHERE id = :id"), {"id": pub.id})
        self.db.execute(text("UPDATE public.tenants SET status = 'blocked' WHERE slug = :s"), {"s": self.tenant})
        self.db.flush()
        status, detail = self._attempt(self.email, PASSWORD)
        self.assertEqual((status, detail),
                         (403, "Your organization's account has been suspended. Please contact support."))

    def test_lockout_still_429_after_repeated_failures(self):
        statuses = [self._attempt(self.email, "Wrong-Password-1")[0]
                    for _ in range(auth_api.settings.login_max_failed_attempts)]
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(self._attempt(self.email, PASSWORD)[0], 429)


if __name__ == "__main__":
    unittest.main()
