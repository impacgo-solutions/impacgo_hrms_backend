"""Sliding sessions: token lifetimes, renewal and its maximum age.

    cd backend && venv/Scripts/python -m unittest tests.test_sliding_sessions -v

Pure unit tests -- no database.
"""

from __future__ import annotations

import os
import sys
import time
import types
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402

from app import security  # noqa: E402
from app.config import settings  # noqa: E402
from app.routers import auth  # noqa: E402


def _token(**kw) -> str:
    return security.create_access_token(
        user_id=uuid.uuid4(), tenant_slug="t", company_id=uuid.uuid4(), employee_id=None,
        roles=["Employee"], **kw,
    )


def _refresh(token: str) -> dict:
    data = security.decode_access_token(token)
    db = types.SimpleNamespace(info={"token_data": data})
    return auth.refresh_session(db=db, current_user=types.SimpleNamespace(id=data.user_id))


class SlidingSessionTests(unittest.TestCase):
    def test_normal_session_lives_for_the_idle_window(self):
        data = security.decode_access_token(_token())
        self.assertFalse(data.remember)
        self.assertAlmostEqual(data.expires_at - time.time(), settings.session_idle_minutes * 60, delta=5)

    def test_remember_me_session_lives_for_days(self):
        data = security.decode_access_token(_token(remember=True))
        self.assertTrue(data.remember)
        self.assertAlmostEqual(data.expires_at - time.time(), settings.session_remember_days * 86400, delta=5)

    def test_refresh_keeps_sign_in_time_and_choice(self):
        signed_in = time.time() - 3600
        old = _token(auth_time=signed_in)
        out = _refresh(old)
        new = security.decode_access_token(out["access_token"])
        self.assertAlmostEqual(new.auth_time, signed_in, delta=1)
        self.assertFalse(new.remember)
        self.assertEqual(out["idle_timeout_minutes"], settings.session_idle_minutes)
        self.assertNotEqual(new.jti, security.decode_access_token(old).jti)

    def test_renewal_never_passes_maximum_age(self):
        # Signed in 30 minutes short of the max age: the token is capped.
        max_age = settings.session_max_days * 86400
        signed_in = time.time() - max_age + 1800
        data = security.decode_access_token(_token(auth_time=signed_in))
        self.assertAlmostEqual(data.expires_at, signed_in + max_age, delta=2)
        # ...and renewing it can't extend anything -> 401.
        with self.assertRaises(HTTPException) as ctx:
            _refresh(_token(auth_time=signed_in))
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
