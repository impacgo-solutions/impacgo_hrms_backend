"""bcrypt's 72-byte limit: passwords longer than 72 UTF-8 bytes are
SHA-256 pre-hashed (app/security.py), so they're never truncated and two
long passwords sharing their first 72 bytes never match.

    cd backend && venv/Scripts/python -m unittest tests.test_password_hashing -v

The unit tests need no database. PasswordLifecycleTests runs against the
configured database inside a transaction that is ALWAYS rolled back (tenant
acme by default, ACCESS_TEST_TENANT to override).
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bcrypt  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import schemas, security  # noqa: E402
from app.routers import auth as auth_api  # noqa: E402
from app.routers import user_roles as user_roles_api  # noqa: E402

LIMIT = security.BCRYPT_MAX_PASSWORD_BYTES
_real_gensalt = bcrypt.gensalt


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


class PasswordHashingTests(unittest.TestCase):
    """Cost 4 for speed here; cost is asserted separately."""

    def setUp(self):
        p = mock.patch.object(bcrypt, "gensalt", lambda rounds=4, prefix=b"2b": _real_gensalt(4, prefix))
        p.start()
        self.addCleanup(p.stop)

    def _ok(self, password, attempt):
        return security.verify_password(attempt, security.hash_password(password))

    def test_lengths_below_at_and_above_limit(self):
        for n in (1, 8, 71, 72, 73, 100, 500):
            with self.subTest(bytes=n):
                pw = "p" * n
                self.assertEqual(_nbytes(pw), n)
                self.assertTrue(self._ok(pw, pw))
                self.assertFalse(self._ok(pw, pw[:-1]))
                self.assertFalse(self._ok(pw, pw + "x"))

    def test_no_truncation_collision(self):
        prefix = "A" * LIMIT
        h = security.hash_password(prefix + "-secret-tail")
        self.assertFalse(security.verify_password(prefix, h))
        self.assertFalse(security.verify_password(prefix + "-other-tail", h))
        self.assertFalse(security.verify_password(prefix + "-secret-tai", h))
        self.assertTrue(security.verify_password(prefix + "-secret-tail", h))
        # ...and the 72-byte password itself is distinct from every longer one.
        h72 = security.hash_password(prefix)
        self.assertTrue(security.verify_password(prefix, h72))
        self.assertFalse(security.verify_password(prefix + "-secret-tail", h72))

    def test_multibyte_unicode(self):
        cases = {
            "2-byte exactly 72": "é" * 36,        # 72 bytes
            "2-byte above": "é" * 37,             # 74 bytes
            "3-byte below": "密" * 23,            # 69 bytes
            "3-byte straddling 72": "密" * 25,    # 75 bytes, char 24 spans byte 70-72
            "4-byte exactly 72": "🔑" * 18,       # 72 bytes
            "4-byte above": "🔑" * 19,            # 76 bytes
            "mixed": "Pässwörd-密码-🔑" * 4,
        }
        for name, pw in cases.items():
            with self.subTest(name, bytes=_nbytes(pw)):
                h = security.hash_password(pw)
                self.assertTrue(security.verify_password(pw, h))
                self.assertFalse(security.verify_password(pw[:-1], h))
                self.assertFalse(security.verify_password(pw[:-1] + "x", h))
        # Same 72 leading bytes, different tail (multibyte).
        base = "é" * 36
        h = security.hash_password(base + "ü")
        self.assertFalse(security.verify_password(base + "ö", h))
        self.assertFalse(security.verify_password(base, h))

    def test_bcrypt_input_never_exceeds_limit(self):
        for pw in ("x" * 72, "x" * 73, "🔑" * 1000, "é" * 37):
            with self.subTest(bytes=_nbytes(pw)):
                self.assertLessEqual(len(security._bcrypt_input(pw)), LIMIT)
                self.assertNotIn(b"\x00", security._bcrypt_input(pw))

    def test_existing_hashes_still_verify(self):
        """Hashes made before this change (plain bcrypt of the password) keep
        working for every password of up to 72 bytes -- all of them."""
        for pw in ("Welcome@123", "é" * 36, "p" * 72, "🔑" * 18):
            with self.subTest(bytes=_nbytes(pw)):
                legacy = bcrypt.hashpw(pw.encode("utf-8"), _real_gensalt(4)).decode()
                self.assertTrue(security.verify_password(pw, legacy))
                self.assertFalse(security.verify_password(pw + "!", legacy))

    def test_legacy_truncated_hash_is_not_accepted(self):
        """A pre-fix hash of a >72-byte password was really a hash of its
        first 72 bytes. It is NOT matched by the prefix or any same-prefix
        variant any more (that is the vulnerability); such an account
        needs a password reset."""
        long_pw = "A" * LIMIT + "tail"
        legacy = bcrypt.hashpw(long_pw.encode("utf-8"), _real_gensalt(4)).decode()
        self.assertFalse(security.verify_password("A" * LIMIT + "other", legacy))
        self.assertFalse(security.verify_password(long_pw, legacy))


class CostTests(unittest.TestCase):
    def test_cost_factor_unchanged(self):
        for pw in ("short", "x" * 100):
            with self.subTest(bytes=len(pw)):
                self.assertTrue(security.hash_password(pw).startswith("$2b$12$"))
        self.assertTrue(security._DUMMY_PASSWORD_HASH.startswith(b"$2b$12$"))


try:
    from tests.test_employee_access_lifecycle import PASSWORD, _Base
except Exception:  # pragma: no cover -- no database configured
    _Base = None

if _Base is not None:
    class PasswordLifecycleTests(_Base):
        """Reset (admin) -> login, through the real endpoints, plus an
        existing account's untouched hash still logging in."""

        def setUp(self):
            super().setUp()
            self.owner = self._owner()
            self.employee, self.user = self._pick_employee(self.owner)
            self.email = self._public(self.user).email

        def _login(self, password):
            try:
                auth_api.login(schemas.LoginRequest(email=self.email, password=password), self.db)
            except HTTPException as exc:
                return exc.status_code
            return 200

        def _reset(self, password):
            user_roles_api.reset_user_password(
                self.user.id, schemas.PasswordResetRequest(new_password=password), self.db, self.owner
            )
            self.db.execute(text("UPDATE public.users SET failed_login_attempts = 0, locked_until = NULL "
                                 "WHERE id = :id"), {"id": self._public(self.user).id})

        def test_existing_user_still_logs_in(self):
            self._set_password(self.user)  # plain bcrypt, as every stored hash is
            self.assertEqual(self._login(PASSWORD), 200)

        def test_long_passwords_through_reset_and_login(self):
            prefix = "Zz9!" * 18  # 72 bytes
            for pw, wrong in (
                (prefix + "-tail-one", [prefix, prefix + "-tail-two"]),
                ("Aa1-密码🔑" * 7, ["Aa1-密码🔑" * 6 + "Aa1-密码", "Aa1-密码🔑" * 7 + "x"]),  # 98 bytes
                ("Aa1!" + "é" * 34, ["Aa1!" + "é" * 33, "Aa1!" + "é" * 33 + "e"]),        # exactly 72
            ):
                with self.subTest(bytes=_nbytes(pw)):
                    self._reset(pw)
                    stored = self.db.execute(text("SELECT password_hash FROM public.users WHERE id = :id"),
                                             {"id": self._public(self.user).id}).scalar()
                    self.assertTrue(stored.startswith("$2b$12$"))
                    self.assertNotIn(pw, stored)
                    self.assertEqual(self._login(pw), 200)
                    for attempt in wrong:
                        self.assertEqual(self._login(attempt), 401)
                    self._reset(pw)  # clear the failed-attempt counter before the next case

        def test_exactly_72_byte_password_plus_extra_reads_as_legacy(self):
            """Known, unavoidable ambiguity: the hash of an exactly-72-byte
            password is identical to an old truncated hash of any longer
            password starting with it. Typing such a password with extra
            characters is treated as a legacy long password (refused with
            the reset message, never a login). Only someone who already
            knows the full 72-byte password can trigger it."""
            pw = "Aa1!" + "é" * 34  # exactly 72 bytes
            self._reset(pw)
            self.assertEqual(self._login(pw + "e"), 403)
            self._reset(pw)  # an admin reset clears any flag it set
            self.assertEqual(self._login(pw), 200)


if __name__ == "__main__":
    unittest.main()
