"""Re-audit of the access fixes (C-01, L-01, L-02, H-03, H-04): gaps found
and closed in the second pass.

  * a public.users row whose hash isn't bcrypt (public.users is shared with
    other applications) is a normal refusal, never a 500;
  * logins / tokens of non-HRMS tenants (retail, fin/scm) never open an
    HRMS session;
  * exit requests (reason, decision notes), the Offboarding list and
    Transfers & Promotions only show employees the caller may see.

    cd backend && venv/Scripts/python -m unittest tests.test_qa_reaudit -v

Real DB, inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app import auth_state, config, crud, models, schemas, security  # noqa: E402
from app.routers import auth as auth_api  # noqa: E402
from tests.test_employee_access_lifecycle import PASSWORD, _Base  # noqa: E402
from tests.test_people_visibility import PeopleTestBase  # noqa: E402


class PasswordHashRobustnessTests(unittest.TestCase):
    def test_non_bcrypt_hash_is_a_refusal_not_an_error(self):
        for stored in ("not-a-hash", "pbkdf2_sha256$260000$abc$def", "$argon2id$v=19$m=65536"):
            self.assertFalse(security.verify_password("Whatever#123", stored))
            self.assertFalse(security.verify_password_or_dummy("Whatever#123", stored))
            self.assertFalse(security.matches_legacy_truncated_hash("x" * 80, stored))


class NonHrmsTenantLoginTests(_Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(auth_state._memory_failures.clear)
        auth_state.invalidate()
        self.addCleanup(auth_state.invalidate)
        self.owner = self._owner()
        self.employee, self.user = self._pick_employee(self.owner)
        self.email = self._set_password(self.user)

    def _login(self):
        try:
            auth_api.login(schemas.LoginRequest(email=self.email, password=PASSWORD), self.db)
        except HTTPException as exc:
            return exc.status_code, exc.detail
        return 200, None

    def test_hrms_login_still_works(self):
        self.assertEqual(self._login()[0], 200)

    def test_login_of_a_non_hrms_tenant_is_refused(self):
        with mock.patch.object(auth_state, "is_hrms_tenant", return_value=False):
            self.assertEqual(self._login(), (403, auth_state.NOT_HRMS_TENANT_DETAIL))

    def test_wrong_password_on_non_hrms_tenant_is_still_401(self):
        # The tenant check comes after the password: guessers learn nothing.
        with mock.patch.object(auth_state, "is_hrms_tenant", return_value=False):
            try:
                auth_api.login(schemas.LoginRequest(email=self.email, password="Wrong#Pass987"), self.db)
            except HTTPException as exc:
                self.assertEqual(exc.status_code, 401)
            else:
                self.fail("wrong password accepted")

    def test_token_of_a_non_hrms_tenant_is_refused(self):
        row = self.db.execute(text(
            "SELECT u.id, u.tenant_slug FROM public.users u WHERE NOT EXISTS (SELECT 1 FROM public.tenant_modules tm "
            "WHERE tm.tenant_slug = u.tenant_slug AND tm.module_code = 'hcm' AND tm.is_enabled) LIMIT 1")).first()
        if row is None:
            self.skipTest("no non-HRMS login in this database")
        state = auth_state.load_auth_state(
            self.db, security.TokenData(user_id=row.id, tenant_slug=row.tenant_slug, public_user_id=row.id))
        self.assertEqual(state.tenant_block_reason, auth_state.NOT_HRMS_TENANT_DETAIL)
        own = auth_state.load_auth_state(self.db, security.TokenData(
            user_id=self.user.id, tenant_slug=self.tenant, public_user_id=self._public(self.user).id))
        self.assertNotEqual(own.tenant_block_reason, auth_state.NOT_HRMS_TENANT_DETAIL)

    def test_malformed_stored_hash_gives_401_not_500(self):
        self.db.execute(text("UPDATE public.users SET password_hash = 'legacy-plain' WHERE id = :id"),
                        {"id": self._public(self.user).id})
        try:
            auth_api.login(schemas.LoginRequest(email=self.email, password=PASSWORD), self.db)
        except HTTPException as exc:
            self.assertEqual(exc.status_code, 401)
        else:
            self.fail("login accepted a malformed hash")


class ExitAndLifecycleVisibilityTests(PeopleTestBase):
    def setUp(self):
        super().setUp()
        self._rl = mock.patch.object(config.settings, "rate_limit_enabled", False)
        self._rl.start()
        self.addCleanup(self._rl.stop)

    def _outsider_for(self, key):
        """An employee of the actor's company the actor may NOT see."""
        user = self.users[key]
        visible = crud.get_people_directory_visible_ids(self.db, user)
        deciders = crud.get_visible_employee_ids_for_requests(self.db, user, "exit_request")
        if visible is None or deciders is None:
            self.skipTest(f"{key} sees the whole company")
        hidden = set(visible) | set(deciders) | {user.employee_id}
        emp = self.db.scalar(select(models.Employee).where(
            models.Employee.company_id == user.company_id, models.Employee.is_active.is_(True),
            models.Employee.id.not_in(list(hidden))).limit(1))
        if emp is None:
            self.skipTest("no employee outside the caller's visibility")
        return emp

    def _owner_of(self, company_id):
        """The Owner of `company_id` (acme holds several companies)."""
        for u in self.db.scalars(select(models.User).where(
                models.User.company_id == company_id, models.User.status == "active")).all():
            role = crud.get_user_primary_role(self.db, u.id)
            if role is not None and role.name == "Organization Owner / CEO":
                return u
        self.skipTest("no owner in this company")

    def _as_owner_of(self, user):
        self.actor = self._owner_of(user.company_id)
        crud.clear_rbac_memo(self.db)

    def _make_exit(self, emp):
        now = datetime.datetime.now(datetime.timezone.utc)
        row = models.ExitRequestModel(
            id=uuid.uuid4(), employee_id=emp.id, status="submitted", reason="Confidential reason",
            resignation_date=datetime.date.today(), last_working_day=datetime.date.today() + datetime.timedelta(days=30),
            created_at=now, updated_at=now)
        self.db.add(row)
        self.db.flush()
        return row

    def _record_ids(self):
        resp = self.client.get("/api/employees/exit-requests/records", params={"limit": 500})
        self.assertEqual(resp.status_code, 200, resp.text)
        return {r["id"] for r in resp.json()}

    def test_exit_records_hidden_from_ic(self):
        for key in ("no_people", "employee"):
            with self.subTest(role=key):
                outsider = self._outsider_for(key)
                exit_row = self._make_exit(outsider)
                self.as_(key)
                self.assertNotIn(str(exit_row.id), self._record_ids())
                self._as_owner_of(self.users[key])
                self.assertIn(str(exit_row.id), self._record_ids())

    def test_own_exit_record_is_listed(self):
        me = self.users["no_people"]
        exit_row = self._make_exit(self.db.get(models.Employee, me.employee_id))
        self.as_("no_people")
        self.assertIn(str(exit_row.id), self._record_ids())

    def test_offboarding_list_follows_people_visibility(self):
        outsider = self._outsider_for("manager")
        exit_row = self._make_exit(outsider)
        self.as_("manager")
        resp = self.client.get("/api/employees/exit-requests", params={"limit": 500})
        if resp.status_code == 403:
            self.skipTest("manager has no People access")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn(str(exit_row.id), {r["id"] for r in resp.json()})
        self._as_owner_of(self.users["manager"])
        resp = self.client.get("/api/employees/exit-requests", params={"limit": 500})
        self.assertIn(str(exit_row.id), {r["id"] for r in resp.json()})

    def test_lifecycle_events_follow_people_visibility(self):
        outsider = self._outsider_for("manager")
        event = models.EmployeeLifecycleEvent(
            id=uuid.uuid4(), employee_id=outsider.id, event_type="transfer", event_date=datetime.date.today(),
            from_department_id=outsider.department_id, to_department_id=outsider.department_id)
        self.db.add(event)
        self.db.flush()
        self.as_("manager")
        resp = self.client.get("/api/employees/lifecycle-events", params={"limit": 500})
        if resp.status_code == 403:
            self.skipTest("manager has no People access")
        self.assertNotIn(str(event.id), {r["id"] for r in resp.json()})
        self.assertEqual(self.client.get(f"/api/employees/lifecycle-events/{event.id}").status_code, 404)
        self._as_owner_of(self.users["manager"])
        self.assertIn(str(event.id), {r["id"] for r in self.client.get(
            "/api/employees/lifecycle-events", params={"limit": 500}).json()})


if __name__ == "__main__":
    unittest.main()
