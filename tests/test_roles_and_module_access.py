"""Administration > Roles & Permissions / Employee Module Access:

  * the role editor shows the levels authorization actually applies, and a
    built-in self-service role (IC, Associate / Intern) can't be saved above
    its template -- previously the save "worked" but had no effect;
  * editing a normal role changes what its members may do;
  * individual grants take effect (Documents visibility, Approvals in
    /api/auth/me for the navigation).

    cd backend && venv/Scripts/python -m unittest tests.test_roles_and_module_access -v

Runs inside a transaction that is always rolled back."""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models  # noqa: E402
from app.database import get_db  # noqa: E402
from app.deps import _OWNER_ROLE_NAME, get_current_user  # noqa: E402
from app.main import app  # noqa: E402

TENANT = os.environ.get("RBAC_TEST_TENANT", "impacgo-solutions")
IC = "Professional / IC Employee"


class RolesAndModuleAccessTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        role_of = {u.id: crud.get_user_primary_role(self.db, u.id) for u in users}
        self.owner = next((u for u in users if role_of[u.id] and role_of[u.id].name == _OWNER_ROLE_NAME), None)
        self.ic = next((u for u in users if role_of[u.id] and role_of[u.id].name == IC), None)
        if not (self.owner and self.ic):
            self.skipTest("tenant needs an Owner and an IC employee")
        self.ic_role = role_of[self.ic.id]
        # Start the IC from their role alone -- the shared test DB may give
        # them real overrides / grants (rolled back with everything else).
        self.db.execute(delete(models.EmployeePermission).where(
            models.EmployeePermission.employee_id == self.ic.employee_id))
        self.db.execute(delete(models.EmployeeAccessGrant).where(
            models.EmployeeAccessGrant.employee_id == self.ic.employee_id))
        self.db.flush()
        crud.clear_rbac_memo(self.db)
        self.user = self.owner
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def test_self_service_role_editor_shows_effective_levels_and_refuses_above_cap(self):
        # Simulate the stored over-cap value the editor used to accept.
        crud.apply_matrix_update(self.db, self.ic_role, {"recruitment": "a"})
        self.db.flush()
        r = self.client.get(f"/api/roles/{self.ic_role.id}/matrix")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["matrix"]["recruitment"], "n")  # what authorization applies
        self.assertEqual(body["caps"]["recruitment"], "n")
        r = self.client.put(f"/api/roles/{self.ic_role.id}/matrix", json={"matrix": {"recruitment": "a"}})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("self-service role", r.json()["detail"])
        self.assertIn("Employee Module Access", r.json()["detail"])
        # Saving the row as the editor now shows it (capped) succeeds and
        # clears the stale over-cap value.
        r = self.client.put(f"/api/roles/{self.ic_role.id}/matrix", json={"matrix": body["matrix"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.db.refresh(self.ic_role)
        self.assertEqual(crud.build_role_matrix(self.ic_role)["recruitment"], "n")

    def test_editing_a_normal_role_takes_effect_for_its_members(self):
        role = self.db.scalars(select(models.Role).where(models.Role.name == "Team Lead")).first()
        if role is None:
            self.skipTest("no Team Lead role")
        r = self.client.get(f"/api/roles/{role.id}/matrix")
        self.assertIsNone(r.json()["caps"])
        r = self.client.put(f"/api/roles/{role.id}/matrix", json={"matrix": {"recruitment": "v"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["matrix"]["recruitment"], "v")
        self.db.refresh(role)
        self.assertEqual(crud.effective_role_matrix(role)["recruitment"], "v")

    def _grant(self, resource, actions):
        r = self.client.put(f"/api/module-access/grants/{resource}",
                            json={"assignments": [{"employee_id": str(self.ic.employee_id), "actions": actions}]})
        self.assertEqual(r.status_code, 200, r.text)
        crud.clear_rbac_memo(self.db)

    def test_documents_grant_opens_document_visibility(self):
        before = crud.get_visible_employee_ids_for_docs(self.db, self.ic)
        self.assertIsNotNone(before)  # an IC sees only their own subtree
        self._grant("documents", ["view"])
        self.assertIsNone(crud.get_visible_employee_ids_for_docs(self.db, self.ic))  # whole company

    def test_approvals_and_module_grants_reach_the_session(self):
        self._grant("approvals", ["view"])
        self._grant("recruitment", ["view"])
        self.user = self.ic
        me = self.client.get("/api/auth/me").json()
        self.assertIn("approvals.view", me["actions"])  # the app shows Approvals for this
        self.assertEqual(me["matrix"]["recruitment"], "v")  # raised by the grant, on top of the IC cap
        r = self.client.get("/api/recruitment/job-openings")
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
