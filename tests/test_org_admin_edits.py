"""Organization / Administration edit-workflow fixes:

  * hierarchy bindings: an explicit null clears a bound rung;
  * approval workflow saved with zero steps deactivates (no empty active flow);
  * designation edit: rename (409 on duplicate) and explicit null role_id clears;
  * company settings: explicit null clears the Work Entry backdate limit;
  * company profile: email_domain validated, blank founded_date clears;
  * branch edit: null state/city stored as NULL.

    cd backend && venv/Scripts/python -m unittest tests.test_org_admin_edits -v

Runs inside a transaction that is always rolled back (DATABASE_URL must be the
test database, never production)."""

from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import approval_engine, crud, database, models  # noqa: E402
from app.database import get_db  # noqa: E402
from app.deps import _OWNER_ROLE_NAME, get_current_user  # noqa: E402
from app.main import app  # noqa: E402

TENANT = os.environ.get("RBAC_TEST_TENANT", "impacgo-solutions")


class OrgAdminEditTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        self.owner = next(
            (u for u in users
             if (r := crud.get_user_primary_role(self.db, u.id)) and r.name == _OWNER_ROLE_NAME),
            None,
        )
        if self.owner is None:
            self.skipTest("tenant needs an Owner")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.owner
        self.client = TestClient(app)
        self.company_id = self.owner.company_id

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def test_hierarchy_binding_null_clears(self):
        role = self.db.scalar(select(models.Role).where(models.Role.company_id == self.company_id))
        r = self.client.put("/api/config/hierarchy-bindings", json={"bindings": {"team_lead": str(role.id)}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([b["rung_key"] for b in r.json()["bindings"]], ["team_lead"])
        r = self.client.put("/api/config/hierarchy-bindings", json={"bindings": {"team_lead": None}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["bindings"], [])

    def test_empty_approval_workflow_deactivates(self):
        step = {"step_order": 1, "approver_type": "reporting_manager"}
        r = self.client.put("/api/config/approval-workflows",
                            json={"doctype": "leave", "name": "Leave", "steps": [step]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(approval_engine.get_active_workflow(self.db, self.company_id, "leave"))
        r = self.client.put("/api/config/approval-workflows",
                            json={"doctype": "leave", "name": "Leave", "steps": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["is_active"])
        self.assertIsNone(approval_engine.get_active_workflow(self.db, self.company_id, "leave"))

    def test_designation_rename_and_clear_role(self):
        role = self.db.scalar(select(models.Role).where(models.Role.company_id == self.company_id))
        band = self.db.scalar(select(models.Band).where(models.Band.company_id == self.company_id))
        if band is None:
            self.skipTest("tenant needs a band")
        a = self.client.post("/api/designations", json={
            "name": f"QA-A-{uuid.uuid4().hex[:6]}", "band_id": str(band.id), "role_id": str(role.id)})
        b = self.client.post("/api/designations", json={
            "name": f"QA-B-{uuid.uuid4().hex[:6]}", "band_id": str(band.id)})
        self.assertEqual(a.status_code, 201, a.text)
        self.assertEqual(b.status_code, 201, b.text)
        aid, bname = a.json()["id"], b.json()["name"]
        r = self.client.patch(f"/api/designations/{aid}", json={"name": bname.lower()})
        self.assertEqual(r.status_code, 409, r.text)
        new_name = f"QA-Renamed-{uuid.uuid4().hex[:6]}"
        r = self.client.patch(f"/api/designations/{aid}", json={"name": new_name, "role_id": None})
        self.assertEqual(r.status_code, 200, r.text)
        row = self.db.get(models.Designation, uuid.UUID(aid))
        self.db.refresh(row)
        self.assertEqual(row.name, new_name)
        self.assertIsNone(row.role_id)
        # an omitted role_id leaves the mapping alone
        self.client.patch(f"/api/designations/{aid}", json={"role_id": str(role.id)})
        r = self.client.patch(f"/api/designations/{aid}", json={"is_active": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.db.refresh(row)
        self.assertEqual(row.role_id, role.id)
        self.assertFalse(row.is_active)

    def test_backdate_limit_can_be_cleared_but_other_nulls_are_ignored(self):
        r = self.client.patch("/api/company-settings", json={"work_entry_backdate_days": 7})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["work_entry_backdate_days"], 7)
        r = self.client.patch("/api/company-settings",
                              json={"default_currency": None, "pf_wage_ceiling": 15000})
        self.assertEqual(r.json()["work_entry_backdate_days"], 7)
        r = self.client.patch("/api/company-settings", json={"work_entry_backdate_days": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["work_entry_backdate_days"])

    def test_company_profile_domain_validation_and_founded_clear(self):
        r = self.client.patch("/api/company-profile", json={"email_domain": "not a domain"})
        self.assertEqual(r.status_code, 422, r.text)
        r = self.client.patch("/api/company-profile",
                              json={"email_domain": "Example.COM", "founded_date": "2015-04-01"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["email_domain"], "example.com")
        self.assertEqual(r.json()["founded_date"], "2015-04-01")
        r = self.client.patch("/api/company-profile", json={"founded_date": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["founded_date"], "—")

    def test_branch_state_null_is_stored_as_null(self):
        branch = self.db.scalar(select(models.Branch).where(models.Branch.company_id == self.company_id))
        if branch is None:
            self.skipTest("tenant needs a branch")
        r = self.client.patch(f"/api/branches/{branch.id}",
                              json={"state": None, "city": None, "country": None, "tz": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.db.refresh(branch)
        self.assertIsNone(branch.state)
        self.assertIsNone(branch.city)


if __name__ == "__main__":
    unittest.main()
