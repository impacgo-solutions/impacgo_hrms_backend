"""One password policy for every flow that sets a password
(app/password_policy.py): create, change, reset -- short, weak, common /
breached and personal passwords refused with a clear message; strong ones
accepted; rejected passwords never echoed back or logged.

    cd backend && venv/Scripts/python -m unittest tests.test_password_policy -v

No database needed.
"""

from __future__ import annotations

import datetime
import inspect
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

from app import main, password_policy, schemas  # noqa: E402
from app.config import settings  # noqa: E402

STRONG = ["Xk9#mP2$vq", "Blue-Harbor-71", "tR4!nStation", "Gl@ss.Kettle.88", "Пароль-Сложный-7", "Mango$Rive1"]
SHORT = ["Ab1!", "Xk9#mP2", "a"]
WEAK_CLASSES = ["alllowercase", "ALLUPPERCASE", "12345678901", "lowercase123", "UPPERlower"]
COMMON = ["Password1!", "P@ssw0rd123", "Welcome@123", "Qwerty@123", "Admin@1234", "Letmein!2024",
          "IMPACGO@123", "Changeme#1"]
TRIVIAL = ["Aa1Aa1Aa1", "Abcdefgh1"]

# Schema -> (field, extra valid fields) for every model that SETS a password.
# Login / current-password fields are deliberately not policy-checked (an
# existing password must still be accepted for verification).
_EMPLOYEE = dict(first_name="Ivy", last_name="Tester", work_email="ivy.tester@example.com",
                 role_name="Individual Contributor", date_of_joining=datetime.date(2026, 1, 1),
                 employment_type="full_time", status="active", branch_name="HQ",
                 department_name="Ops", designation_name="Engineer")
_TENANT = dict(tenant_slug="acme-two", company_name="Acme Two", contact_name="Olga Owner",
               contact_email="olga@acme-two.example.com")
SETTERS = {
    "EmployeeCreate (create)": (schemas.EmployeeCreate, "password", _EMPLOYEE, 8),
    "PasswordResetRequest (admin reset)": (schemas.PasswordResetRequest, "new_password", {}, 8),
    "HrmsAdminCreate (tenant owner create)": (schemas.HrmsAdminCreate, "owner_password", _TENANT, 8),
    "HrmsAdminPasswordResetRequest (owner reset)": (schemas.HrmsAdminPasswordResetRequest, "new_password", {}, 8),
    "SuperAdminUserCreate (platform create)": (schemas.SuperAdminUserCreate, "password",
                                               dict(full_name="Pat Platform", email="pat@example.com"), 12),
    "SuperAdminChangePasswordRequest (change)": (schemas.SuperAdminChangePasswordRequest, "new_password",
                                                 dict(current_password="anything"), 12),
}
NOT_SETTERS = {"LoginRequest.password", "SuperAdminLoginRequest.password",
               "SuperAdminChangePasswordRequest.current_password"}


def _errors(model, field, extra, password):
    try:
        model(**{**extra, field: password})
    except ValidationError as exc:
        return exc.errors()
    return []


class _Offline(unittest.TestCase):
    """Local rules only -- the online lookup is mocked to "not breached"."""

    def setUp(self):
        p = mock.patch.object(password_policy, "_breached_online", lambda value: False)
        p.start()
        self.addCleanup(p.stop)


class PolicyRuleTests(_Offline):
    def _rejects(self, password, fragment):
        with self.assertRaises(Exception) as ctx:
            password_policy.validate_password(password)
        self.assertIn(fragment, str(ctx.exception))
        if len(password) >= 4:  # a 1-letter password trivially "appears" in any message
            self.assertNotIn(password, str(ctx.exception))

    def test_strong_passwords_accepted(self):
        for pw in STRONG:
            with self.subTest(pw=pw):
                self.assertEqual(password_policy.validate_password(pw), pw)

    def test_minimum_length_is_8(self):
        for pw in SHORT:
            with self.subTest(pw=pw):
                self._rejects(pw, "at least 8 characters")
        self.assertEqual(password_policy.validate_password("Xk9#mP2$"), "Xk9#mP2$")  # exactly 8

    def test_maximum_length(self):
        self._rejects("Xk9#" * 26, "at most 100 characters")

    def test_complexity(self):
        for pw in WEAK_CLASSES:
            with self.subTest(pw=pw):
                self._rejects(pw, "at least 3 of")

    def test_common_and_breached(self):
        for pw in COMMON + TRIVIAL:
            with self.subTest(pw=pw):
                self._rejects(pw, "too common or has appeared in a data breach")

    def test_personal_information(self):
        with self.assertRaises(Exception) as ctx:
            password_policy.validate_password("Tester#2026x", context=("ivy.tester@example.com", "Ivy"))
        self.assertIn("name or email", str(ctx.exception))
        # Short name parts (< 4 chars) don't make a password invalid.
        password_policy.validate_password("Ivy#Harbor-71", context=("Ivy",))

    def test_blank(self):
        self._rejects("        ", "required")


class OnlineBreachCheckTests(unittest.TestCase):
    PW = "Xk9#mP2$vq"

    def _sha_parts(self):
        import hashlib
        sha = hashlib.sha1(self.PW.encode()).hexdigest().upper()
        return sha[:5], sha[5:]

    def test_enabled_by_default(self):
        self.assertTrue(settings.password_breach_check_enabled)

    def test_disabled_makes_no_call(self):
        with mock.patch.object(settings, "password_breach_check_enabled", False), \
                mock.patch("urllib.request.urlopen") as urlopen:
            password_policy.validate_password(self.PW)
        urlopen.assert_not_called()

    def test_every_password_setting_flow_checks_online(self):
        """Create, change and reset all reject a breached password; the
        request carries only the 5-character prefix."""
        prefix, suffix = self._sha_parts()
        for name, (model, field, extra, min_len) in SETTERS.items():
            pw = self.PW if len(self.PW) >= min_len else self.PW + "Zq7!x"
            sha = __import__("hashlib").sha1(pw.encode()).hexdigest().upper()
            seen = []

            def fake(req, timeout, _sha=sha):
                seen.append(req.full_url)
                return io.BytesIO(f"{_sha[5:]}:9\r\n".encode())

            with self.subTest(name), mock.patch("urllib.request.urlopen", fake):
                errs = _errors(model, field, extra, pw)
                self.assertTrue(errs and "data breach" in errs[0]["msg"], errs)
                self.assertEqual(seen, [f"https://api.pwnedpasswords.com/range/{sha[:5]}"])
                self.assertNotIn(pw, seen[0])
                self.assertNotIn(sha[5:], seen[0])

    def test_login_never_checks_online(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            schemas.LoginRequest(email="a@example.com", password=self.PW)
            schemas.SuperAdminLoginRequest(email="a@example.com", password=self.PW)
            schemas.SuperAdminChangePasswordRequest(current_password=self.PW, new_password="Xk9#mP2$vqLw7")
        # Only the NEW password of a change is looked up, never the current one.
        self.assertEqual(urlopen.call_count, 1)

    def test_enabled_rejects_breached_and_sends_only_prefix(self):
        prefix, suffix = self._sha_parts()
        seen = []

        def fake_urlopen(req, timeout):
            seen.append(req.full_url)
            return io.BytesIO(f"0000000000000000000000000000000000A:3\r\n{suffix}:42\r\n".encode())

        with mock.patch.object(settings, "password_breach_check_enabled", True), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(Exception) as ctx:
                password_policy.validate_password(self.PW)
        self.assertIn("data breach", str(ctx.exception))
        self.assertEqual(seen, [f"https://api.pwnedpasswords.com/range/{prefix}"])
        self.assertNotIn(suffix, seen[0])
        self.assertNotIn(self.PW, seen[0])

    def test_enabled_accepts_unbreached_and_padding_rows(self):
        _prefix, suffix = self._sha_parts()
        body = f"{suffix}:0\r\n1111111111111111111111111111111111B:7\r\n".encode()  # :0 = padding
        with mock.patch.object(settings, "password_breach_check_enabled", True), \
                mock.patch("urllib.request.urlopen", lambda req, timeout: io.BytesIO(body)):
            self.assertEqual(password_policy.validate_password(self.PW), self.PW)

    def test_lookup_failure_fails_open_but_local_rules_still_apply(self):
        def boom(req, timeout):
            raise OSError("network down")

        with mock.patch.object(settings, "password_breach_check_enabled", True), \
                mock.patch("urllib.request.urlopen", boom):
            self.assertEqual(password_policy.validate_password(self.PW), self.PW)
            with self.assertRaises(Exception):
                password_policy.validate_password("Welcome@123")


class ConsistencyTests(_Offline):
    def test_every_password_setting_schema_enforces_the_policy(self):
        for name, (model, field, extra, min_len) in SETTERS.items():
            with self.subTest(name):
                for pw in SHORT + WEAK_CLASSES + COMMON + TRIVIAL:
                    errs = _errors(model, field, extra, pw)
                    self.assertTrue(errs, f"{name} accepted weak password {pw!r}")
                    self.assertEqual(errs[0]["type"], "password_policy", errs[0])
                for pw in STRONG:
                    if len(pw) >= min_len:
                        self.assertEqual(_errors(model, field, extra, pw), [], f"{name} refused {pw!r}")
                # 8-11 characters: fine for tenant accounts, too short for platform ones.
                errs = _errors(model, field, extra, "Xk9#mP2$")
                self.assertEqual(bool(errs), min_len == 12)

    def test_no_password_setting_schema_is_missed(self):
        """Every schema field named *password* is either policy-checked
        (listed in SETTERS) or a verification-only field (NOT_SETTERS)."""
        covered = {f"{m.__name__}.{f}" for m, f, _e, _l in SETTERS.values()}
        found = set()
        for name, obj in inspect.getmembers(schemas, inspect.isclass):
            if issubclass(obj, BaseModel) and obj.__module__ == schemas.__name__:
                for field in obj.model_fields:
                    if "password" in field and not field.endswith(("_hash", "_set", "_changed_at")):
                        found.add(f"{name}.{field}")
        self.assertEqual(found - covered - NOT_SETTERS, set())

    def test_reset_and_create_share_minimum(self):
        """The old gap: reset allowed 6, create required 8."""
        for name, (model, field, extra, _l) in SETTERS.items():
            with self.subTest(name):
                self.assertTrue(_errors(model, field, extra, "Ab1!xy"))      # 6
                self.assertTrue(_errors(model, field, extra, "Ab1!xyz"))     # 7

    def test_employee_password_must_not_contain_email_or_name(self):
        errs = _errors(schemas.EmployeeCreate, "password", _EMPLOYEE, "Tester#2026x")
        self.assertEqual(errs[0]["type"], "password_policy")
        self.assertIn("name or email", errs[0]["msg"])
        errs = _errors(schemas.HrmsAdminCreate, "owner_password", _TENANT, "Olga-Owner#9")
        self.assertIn("name or email", errs[0]["msg"])


class NoLeakTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_422_never_echoes_password(self):
        secret = "Zq" * 600  # over the login cap -> 422 on the password field
        with self.assertLogs("app.main", level="WARNING") as logs:
            resp = self.client.post("/api/auth/login", json={"email": "x@example.com", "password": secret})
        self.assertEqual(resp.status_code, 422)
        self.assertNotIn(secret, resp.text)
        self.assertNotIn(secret, "\n".join(logs.output))

    def test_model_level_error_redacts_password_in_body_input(self):
        pw = "Tester#2026x"
        try:
            schemas.EmployeeCreate(**_EMPLOYEE, password=pw)
        except ValidationError as exc:
            cleaned = main._redact_secrets(exc.errors())
        self.assertNotIn(pw, repr(cleaned))
        self.assertIn("name or email", cleaned[0]["msg"])


if __name__ == "__main__":
    unittest.main()


try:
    from tests.test_employee_access_lifecycle import _Base
except Exception:  # pragma: no cover -- no database configured
    _Base = None

if _Base is not None:
    from sqlalchemy import text

    from app import database, deps, models
    from app.routers import auth as auth_api

    class HttpFlowTests(_Base):
        """Through the real routes (rolled back): weak passwords get a 422
        with the policy message and no echo; strong ones work end to end."""

        def setUp(self):
            super().setUp()
            self.owner = self._owner()
            self.employee, self.user = self._pick_employee(self.owner)
            main.app.dependency_overrides[database.get_db] = lambda: self.db
            main.app.dependency_overrides[deps.get_current_user] = lambda: self.owner
            self.addCleanup(main.app.dependency_overrides.clear)
            self.client = TestClient(main.app)

        def _messages(self, resp):
            return [e.get("msg") or e.get("message") for e in resp.json()["detail"]]

        def _can_login(self, email, password):
            self.db.execute(
                text("UPDATE public.users SET failed_login_attempts = 0, locked_until = NULL WHERE lower(email) = :e"),
                {"e": email.lower()})
            try:
                auth_api.login(schemas.LoginRequest(email=email, password=password), self.db)
            except Exception:
                return False
            return True

        def test_reset_password(self):
            url = f"/api/users/{self.user.id}/reset-password"
            for weak, fragment in (("Ab1!xy", "at least 8"), ("lowercase123", "3 of"),
                                   ("Welcome@123", "data breach")):
                with self.subTest(weak=weak):
                    resp = self.client.post(url, json={"new_password": weak})
                    self.assertEqual(resp.status_code, 422)
                    self.assertTrue(any(fragment in m for m in self._messages(resp)), resp.text)
                    self.assertNotIn(weak, resp.text)
            email = self._public(self.user).email
            resp = self.client.post(url, json={"new_password": "Blue-Harbor-71"})
            self.assertEqual(resp.status_code, 204, resp.text)
            self.assertTrue(self._can_login(email, "Blue-Harbor-71"))

        def test_create_employee(self):
            src = self.employee
            branch = self.db.get(models.Branch, src.branch_id) if src.branch_id else None
            dept = self.db.get(models.Department, src.department_id) if src.department_id else None
            desig = self.db.get(models.Designation, src.designation_id) if getattr(src, "designation_id", None) else None
            if not (branch and dept and desig):
                self.skipTest("employee without branch/department/designation")
            from app import crud
            role = crud.get_user_roles(self.db, self.user.id)[0]
            email = f"policy.check.{os.getpid()}@example.com"
            body = dict(first_name="Policy", last_name="Check", work_email=email, role_name=role.name,
                        date_of_joining="2026-01-01", employment_type="full_time", status="active",
                        branch_name=branch.name, department_name=dept.name, designation_name=desig.name)
            resp = self.client.post("/api/employees", json={**body, "password": "Short1!"})
            self.assertEqual(resp.status_code, 422)
            self.assertTrue(any("at least 8" in m for m in self._messages(resp)))
            resp = self.client.post("/api/employees", json={**body, "password": "Policy#Check1"})
            self.assertEqual(resp.status_code, 422)
            self.assertTrue(any("name or email" in m for m in self._messages(resp)), resp.text)
            self.assertNotIn("Policy#Check1", resp.text)
            resp = self.client.post("/api/employees", json={**body, "password": "Gl@ss.Kettle.88"})
            if resp.status_code == 422 and "password" not in resp.text:
                self.skipTest(f"tenant field rules need more fields: {resp.text[:200]}")
            self.assertEqual(resp.status_code, 201, resp.text)
            self.assertTrue(self._can_login(email, "Gl@ss.Kettle.88"))

