"""Login gives no way to tell which emails exist: unknown emails and wrong
passwords take the same path (one bcrypt check -- against a dummy hash when
there is no user -- the same lockout statements, the same generic 401), and
both lock out after the same number of attempts.

    cd backend && venv/Scripts/python -m unittest tests.test_login_enumeration -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant acme by default, ACCESS_TEST_TENANT to override).
"""

from __future__ import annotations

import os
import sys
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bcrypt  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import access_lifecycle, auth_state, security  # noqa: E402
from app.config import settings  # noqa: E402
from app.routers import auth as auth_api  # noqa: E402
from tests.test_employee_access_lifecycle import PASSWORD, _Base  # noqa: E402

GENERIC = auth_api._INVALID_CREDENTIALS


class LoginEnumerationTests(_Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(auth_state._memory_failures.clear)
        auth_state._memory_failures.clear()
        self.owner = self._owner()
        self.employee, self.user = self._pick_employee(self.owner)
        self.email = self._set_password(self.user)

    def _attempt(self, email, password):
        from app import schemas

        try:
            auth_api.login(schemas.LoginRequest(email=email, password=password), self.db)
        except HTTPException as exc:
            return exc.status_code, exc.detail
        return 200, None

    def _unknown(self):
        return f"nobody-{uuid.uuid4().hex}@nowhere.invalid"

    def _counting_checkpw(self):
        calls = []
        real = bcrypt.checkpw

        def counting(pw, hashed):
            calls.append(hashed)
            return real(pw, hashed)

        return calls, mock.patch.object(bcrypt, "checkpw", counting)

    def test_dummy_hash_has_real_cost(self):
        self.assertEqual(security._DUMMY_PASSWORD_HASH[:7], bcrypt.gensalt()[:7])
        stored = self.db.execute(text("SELECT password_hash FROM public.users WHERE id = :id"),
                                 {"id": self._public(self.user).id}).scalar()
        self.assertEqual(security._DUMMY_PASSWORD_HASH[:7].decode(), stored[:7])

    def test_valid_login(self):
        self.assertEqual(self._attempt(self.email, PASSWORD)[0], 200)

    def test_wrong_password_and_unknown_email_look_identical(self):
        calls, patch = self._counting_checkpw()
        with patch:
            wrong = self._attempt(self.email, "Wrong-Password-1")
            unknown = self._attempt(self._unknown(), "Wrong-Password-1")
        self.assertEqual(wrong, (401, GENERIC))
        self.assertEqual(unknown, (401, GENERIC))
        # Exactly one bcrypt check each; the unknown one ran against the dummy.
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], security._DUMMY_PASSWORD_HASH)

    def test_same_database_statements(self):
        statements = {"known": [], "unknown": []}
        for key, email in (("known", self.email), ("unknown", self._unknown())):
            real = self.db.execute

            def recording(stmt, *a, _key=key, _real=real, **k):
                statements[_key].append(str(stmt).split()[0].upper())
                return _real(stmt, *a, **k)

            with mock.patch.object(self.db, "execute", recording):
                self._attempt(email, "Wrong-Password-1")
            self.db.execute(text("UPDATE public.users SET failed_login_attempts = 0, locked_until = NULL "
                                 "WHERE id = :id"), {"id": self._public(self.user).id})
        self.assertEqual(statements["known"], statements["unknown"])

    def test_inactive_user(self):
        self.db.execute(text("UPDATE public.users SET is_active = false WHERE id = :id"),
                        {"id": self._public(self.user).id})
        self.assertEqual(self._attempt(self.email, "Wrong-Password-1"), (401, GENERIC))
        self.assertEqual(self._attempt(self.email, PASSWORD),
                         (403, access_lifecycle.ACCOUNT_INACTIVE_DETAIL))

    def test_terminated_user(self):
        self._set_status(self.employee, "Terminated", self.owner)
        self.assertEqual(self._attempt(self.email, "Wrong-Password-1"), (401, GENERIC))
        self.assertEqual(self._attempt(self.email, PASSWORD),
                         (403, access_lifecycle.ACCOUNT_INACTIVE_DETAIL))

    def test_repeated_attempts_lock_both_alike(self):
        unknown = self._unknown()
        n = max(1, settings.login_max_failed_attempts)
        for email in (self.email, unknown):
            with self.subTest(email="known" if email == self.email else "unknown"):
                results = [self._attempt(email, "Wrong-Password-1")[0] for _ in range(n)]
                self.assertEqual(results, [401] * (n - 1) + [429])
                # Locked: even the right password is refused for now.
                self.assertEqual(self._attempt(email, PASSWORD)[0], 429)

    def test_timing_gap_is_small(self):
        """Interleaved medians: unknown vs wrong password within 30% of a
        bcrypt check (the gap was ~700 ms before the fix)."""
        known, unknown = [], []
        for _ in range(6):
            for bucket, email in ((known, self.email), (unknown, self._unknown())):
                self.db.execute(text("UPDATE public.users SET failed_login_attempts = 0, locked_until = NULL "
                                     "WHERE id = :id"), {"id": self._public(self.user).id})
                auth_state._memory_failures.clear()
                t = time.perf_counter()
                self._attempt(email, "Wrong-Password-1")
                bucket.append(time.perf_counter() - t)
        t = time.perf_counter()
        bcrypt.checkpw(b"x", security._DUMMY_PASSWORD_HASH)
        one_bcrypt = time.perf_counter() - t
        gap = abs(sorted(known)[3] - sorted(unknown)[3])
        self.assertLess(gap, 0.3 * one_bcrypt + 0.15, f"gap {gap * 1000:.0f} ms")


if __name__ == "__main__":
    unittest.main()
