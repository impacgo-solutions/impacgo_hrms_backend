"""Inactive / terminated / exited employees lose login access and every open
session (app/access_lifecycle.py): status change, exit approval, the
last-working-day scheduled pass, login, token access, reactivation -- on an
existing tenant and on a freshly provisioned one.

    cd backend && venv/Scripts/python -m unittest tests.test_employee_access_lifecycle -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant acme by default, ACCESS_TEST_TENANT to override; the
fresh tenant is provisioned inside the same transaction). Nothing is left
behind.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import access_lifecycle, auth_state, crud, database, deps, models, provision_tenant, reminders, schemas, security  # noqa: E402
from app.routers import auth as auth_api  # noqa: E402
from app.routers import employees as employees_api  # noqa: E402

TENANT = os.environ.get("ACCESS_TEST_TENANT", "acme")
PASSWORD = "Access@Test123"
EXIT_STATUSES = ["Inactive", "Terminated", "Exited", "Resigned", "Relieved", "Absconded"]
EMPLOYED_STATUSES = ["Active", "Probation", "notice_period", "on_leave"]


class _Base(unittest.TestCase):
    tenant = TENANT

    def setUp(self):
        # Cleanups (not tearDown) so a skipTest in a subclass setUp still
        # rolls back and returns the connection to the pool.
        self.conn = database.engine.connect()
        self.addCleanup(self.conn.close)
        self.outer = self.conn.begin()
        self.addCleanup(self.outer.rollback)
        self.db = self._session(self.tenant)
        self.addCleanup(lambda: self.db.close())
        self.addCleanup(auth_state.invalidate)
        self.addCleanup(mock.patch.stopall)
        auth_state.invalidate()
        # The reminders thread must not run against this transaction.
        mock.patch.object(reminders, "start", lambda: None).start()

    def _session(self, slug: str) -> Session:
        self.conn.execute(text(f'SET LOCAL search_path TO "{slug}", public'))
        db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(db, slug)
        return db

    # ── helpers ──
    def _pick_employee(self, owner: models.User) -> tuple[models.Employee, models.User]:
        """An active employee of the owner's company with a login and a role,
        no exit request, not the owner."""
        with_exit = set(self.db.scalars(select(models.ExitRequestModel.employee_id)).all())
        exclude = {owner.employee_id}
        for user in self.db.scalars(select(models.User).where(
                models.User.status == "active", models.User.company_id == owner.company_id)).all():
            if user.employee_id is None or user.employee_id in with_exit or user.employee_id in exclude:
                continue
            employee = self.db.get(models.Employee, user.employee_id)
            if employee is None or access_lifecycle.employee_access_blocked(employee):
                continue
            if not crud.get_user_roles(self.db, user.id):
                continue
            if auth_state.public_user_id_for_core_user(self.db, user) is None:
                continue
            return employee, user
        self.skipTest(f"no active employee with a login in tenant {self.tenant}")

    def _owner(self) -> models.User:
        for user in self.db.scalars(select(models.User).where(models.User.status == "active")).all():
            if deps.is_owner(self.db, user):
                return user
        self.skipTest("no owner login")

    def _public(self, user: models.User):
        pid = auth_state.public_user_id_for_core_user(self.db, user)
        return self.db.execute(
            text("SELECT id, email, is_active, token_version FROM public.users WHERE id = :id"), {"id": pid}
        ).first()

    def _set_password(self, user: models.User) -> str:
        pub = self._public(user)
        self.db.execute(
            text("UPDATE public.users SET password_hash = :h, failed_login_attempts = 0, locked_until = NULL "
                 "WHERE id = :id"),
            {"h": security.hash_password(PASSWORD), "id": pub.id},
        )
        self.db.flush()
        return pub.email

    def _login(self, email: str):
        return auth_api.login(schemas.LoginRequest(email=email, password=PASSWORD), self.db)

    def _assert_login_rejected(self, email: str):
        with self.assertRaises(HTTPException) as ctx:
            self._login(email)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, access_lifecycle.ACCOUNT_INACTIVE_DETAIL)

    def _assert_token_rejected(self, token: str):
        with self.assertRaises(HTTPException) as ctx:
            deps.authenticate_token(token, self.db)
        self.assertEqual(ctx.exception.status_code, 401)

    def _set_status(self, employee, status, actor):
        return employees_api.update_employee_org(
            employee.id, schemas.EmployeeOrgUpdate(status=status), self.db, actor
        )

    def _assert_deactivated(self, employee, user, tv_before):
        self.db.expire_all()
        self.assertEqual(self.db.get(models.User, user.id).status, "inactive")
        pub = self._public(user)
        self.assertFalse(pub.is_active)
        self.assertGreater(pub.token_version, tv_before)


class StatusChangeTests(_Base):
    def test_terminate_blocks_login_and_existing_session_then_reactivate(self):
        owner = self._owner()
        employee, user = self._pick_employee(owner)
        email = self._set_password(user)
        old_token = self._login(email).access_token
        self.assertEqual(deps.authenticate_token(old_token, self.db).id, user.id)
        tv_before = self._public(user).token_version

        self._set_status(employee, "Terminated", owner)
        self._assert_deactivated(employee, user, tv_before)
        self._assert_token_rejected(old_token)       # existing session / API token
        self._assert_login_rejected(email)           # right password, still refused
        self.assertTrue(self.db.scalar(select(models.AuditLog.id).where(
            models.AuditLog.doctype == "employee", models.AuditLog.document_id == employee.id,
            models.AuditLog.action == "access_revoked")))

        self._set_status(employee, "Active", owner)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.User, user.id).status, "active")
        self.assertTrue(self._public(user).is_active)
        new_token = self._login(email).access_token
        self.assertEqual(deps.authenticate_token(new_token, self.db).id, user.id)
        self._assert_token_rejected(old_token)       # pre-termination tokens stay dead

    def test_every_exit_status_blocks(self):
        owner = self._owner()
        employee, user = self._pick_employee(owner)
        email = self._set_password(user)
        for status in EXIT_STATUSES:
            with self.subTest(status=status):
                token = self._login(email).access_token
                tv_before = self._public(user).token_version
                self._set_status(employee, status, owner)
                self._assert_deactivated(employee, user, tv_before)
                self._assert_token_rejected(token)
                self._assert_login_rejected(email)
                self._set_status(employee, "Active", owner)

    def test_employed_statuses_keep_access(self):
        owner = self._owner()
        employee, user = self._pick_employee(owner)
        email = self._set_password(user)
        token = self._login(email).access_token
        for status in EMPLOYED_STATUSES:
            with self.subTest(status=status):
                self._set_status(employee, status, owner)
                self.db.expire_all()
                self.assertEqual(self.db.get(models.User, user.id).status, "active")
                self.assertTrue(self._public(user).is_active)
                self.assertEqual(deps.authenticate_token(token, self.db).id, user.id)
                self._login(email)

    def test_unsynced_status_change_is_still_enforced(self):
        """A status written straight to the table (no sync) still ends the
        session at once, and the next login attempt repairs the login rows."""
        owner = self._owner()
        employee, user = self._pick_employee(owner)
        email = self._set_password(user)
        token = self._login(email).access_token
        self.db.execute(text("UPDATE core_employees SET status = 'exited' WHERE id = :id"), {"id": employee.id})
        self.db.expire_all()
        self._assert_token_rejected(token)
        tv_before = self._public(user).token_version
        self._assert_login_rejected(email)
        self._assert_deactivated(employee, user, tv_before)

    def test_wrong_password_on_inactive_account_is_generic(self):
        owner = self._owner()
        employee, user = self._pick_employee(owner)
        email = self._set_password(user)
        self._set_status(employee, "Terminated", owner)
        with self.assertRaises(HTTPException) as ctx:
            auth_api.login(schemas.LoginRequest(email=email, password="wrong-password"), self.db)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, auth_api._INVALID_CREDENTIALS)


class ExitTests(_Base):
    def setUp(self):
        super().setUp()
        # Authorization / multi-step chains are covered elsewhere; here the
        # decision simply lands.
        mock.patch.object(crud, "can_decide_request_configurable", lambda *a, **k: True).start()
        mock.patch.object(crud, "decide_configurable_request", lambda db, u, e, d, i, decision, c=None: decision).start()
        mock.patch.object(crud, "notify_decision", lambda *a, **k: None).start()
        self.owner = self._owner()
        self.employee, self.user = self._pick_employee(self.owner)
        self.email = self._set_password(self.user)
        self.company_id = self.owner.company_id
        self.today = crud.company_today(self.db, self.company_id)
        # Exits already due in the seed data are processed first, so the
        # counts below are about this test's exit only.
        access_lifecycle.deactivate_due_exits(self.db, self.company_id)

    def _exit(self, lwd):
        request = crud.create_exit_request(
            self.db, self.company_id, self.employee.id, self.today - datetime.timedelta(days=30), lwd, "test"
        )
        self.db.flush()
        return request

    def _approve(self, request):
        return employees_api.decide_exit_request(
            request.id, schemas.ExitRequestDecisionUpdate(status="approved"), self.db, self.owner
        )

    def test_approval_on_last_working_day_deactivates_now(self):
        token = self._login(self.email).access_token
        tv_before = self._public(self.user).token_version
        self._approve(self._exit(self.today))
        self._assert_deactivated(self.employee, self.user, tv_before)
        self.assertEqual(self.db.get(models.Employee, self.employee.id).status, "exited")
        self._assert_token_rejected(token)
        self._assert_login_rejected(self.email)

    def test_future_last_working_day_waits_for_scheduled_pass(self):
        lwd = self.today + datetime.timedelta(days=10)
        token = self._login(self.email).access_token
        request = self._exit(lwd)
        self._approve(request)
        self.db.expire_all()
        # Notice period: still works normally.
        self.assertEqual(deps.authenticate_token(token, self.db).id, self.user.id)
        self.assertEqual(access_lifecycle.deactivate_due_exits(self.db, self.company_id), 0)
        self._login(self.email)

        tv_before = self._public(self.user).token_version
        with mock.patch.object(crud, "company_today", lambda *a, **k: lwd):
            self.assertEqual(access_lifecycle.deactivate_due_exits(self.db, self.company_id), 1)
            self._assert_deactivated(self.employee, self.user, tv_before)
            self._assert_token_rejected(token)
            self._assert_login_rejected(self.email)
            # Idempotent: a second pass does nothing.
            self.assertEqual(access_lifecycle.deactivate_due_exits(self.db, self.company_id), 0)

            # HR reactivates (e.g. exit withdrawn late): the pass must not
            # lock them out again.
            self._set_status(self.db.get(models.Employee, self.employee.id), "Active", self.owner)
            self.assertEqual(access_lifecycle.deactivate_due_exits(self.db, self.company_id), 0)
            new_token = self._login(self.email).access_token
            self.assertEqual(deps.authenticate_token(new_token, self.db).id, self.user.id)

    def test_scheduled_pass_ignores_unapproved_exits(self):
        self._exit(self.today - datetime.timedelta(days=1))  # submitted, never approved
        self.assertEqual(access_lifecycle.deactivate_due_exits(self.db, self.company_id), 0)
        self._login(self.email)

    def test_rejected_exit_keeps_access(self):
        request = self._exit(self.today)
        employees_api.decide_exit_request(
            request.id, schemas.ExitRequestDecisionUpdate(status="rejected"), self.db, self.owner
        )
        self.db.expire_all()
        self.assertEqual(self.db.get(models.User, self.user.id).status, "active")
        self._login(self.email)


@contextmanager
def _test_connection(conn):
    """Hands provision_tenant the test connection with begin() turned into a
    savepoint, so the outer test transaction still rolls everything back."""
    with mock.patch.object(conn, "begin", conn.begin_nested):
        yield conn


class FreshTenantTests(_Base):
    tenant = "public"

    def test_new_tenant_employee_termination(self):
        slug = f"acc-test-{uuid.uuid4().hex[:8]}"
        owner_email = f"owner@{slug}.example.com"
        fake_engine = mock.Mock()
        fake_engine.connect = lambda: _test_connection(self.conn)
        with mock.patch.object(provision_tenant, "engine", fake_engine), \
                mock.patch.object(provision_tenant, "_analyze_tenant_schema", lambda s: None), \
                mock.patch("builtins.print"):
            provision_tenant.provision_tenant(slug, "Access Test Co", "Access Test Co Pvt Ltd",
                                              "Olivia", "Owner", owner_email, PASSWORD)
        self.db.close()
        self.db = self._session(slug)
        owner = self.db.scalar(select(models.User).where(models.User.email == owner_email))
        company_id = owner.company_id
        role = crud.get_role_by_name(self.db, company_id, "Individual Contributor") or crud.get_user_roles(
            self.db, owner.id)[0]
        branch = self.db.scalar(select(models.Branch).where(models.Branch.company_id == company_id))
        dept = self.db.scalar(select(models.Department).where(models.Department.company_id == company_id))
        emp_email = f"ivy@{slug}.example.com"
        employee = crud.create_employee(self.db, company_id, schemas.EmployeeCreate(
            first_name="Ivy", last_name="Employee", work_email=emp_email, role_name=role.name,
            password=PASSWORD, date_of_joining=datetime.date.today(), employment_type="full_time",
            status="active", branch_name=branch.name, department_name=dept.name,
            designation_name="Chief Executive Officer",
        ))
        self.db.flush()
        user = crud.get_core_user_by_employee_id(self.db, employee.id)

        token = self._login(emp_email).access_token
        self.assertEqual(deps.authenticate_token(token, self.db).id, user.id)
        tv_before = self._public(user).token_version
        self._set_status(employee, "Terminated", owner)
        self._assert_deactivated(employee, user, tv_before)
        self._assert_token_rejected(token)
        self._assert_login_rejected(emp_email)
        # The owner is unaffected.
        self._login(owner_email)

        self._set_status(self.db.get(models.Employee, employee.id), "Active", owner)
        self._login(emp_email)


if __name__ == "__main__":
    unittest.main()
