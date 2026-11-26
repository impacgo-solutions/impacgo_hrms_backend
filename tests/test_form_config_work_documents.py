"""Form Configuration x Learning Documents (category) and Employee Documents
(upload type), plus the master-data "Add new ..." creates behind the project
dialogs (business unit, department, project type).

    cd backend && venv/Scripts/python -m unittest tests.test_form_config_work_documents -v

Router functions are called directly in a transaction that is ALWAYS rolled
back; uploaded test files are removed again. Needs
db/add_form_config_work_documents.sql applied.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException, UploadFile  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, deps, form_config as fc, models, schemas, storage  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import employees as employees_api  # noqa: E402
from app.routers import form_config as cfg_api  # noqa: E402
from app.routers import learning_documents as ld_api  # noqa: E402
from app.routers import organization as org_api  # noqa: E402
from app.routers import projects as projects_api  # noqa: E402

TENANT = os.environ.get("TEST_TENANT", "impacgo-solutions")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _file(name: str, data: bytes = PDF) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name, headers={"content-type": "application/pdf"})


class WorkDocumentsFormConfigTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.saved: list[str] = []
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        self.owner = next((u for u in users
                           if (crud.get_user_primary_role(self.db, u.id) or models.Role()).name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("tenant needs an Owner user")
        self.company_id = self.owner.company_id
        self.plain = next((u for u in users if u.id != self.owner.id and u.company_id == self.company_id
                           and crud.effective_user_matrix(self.db, u).get("org_structure_config") not in ("e", "a")
                           and not crud.permission_satisfied(self.db, u, "projects_access", "projects", "edit")), None)
        self.employee = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.company_id, models.Employee.is_active.is_(True))).first()

    def tearDown(self):
        for url in self.saved:
            try:
                storage.delete_uploaded_file(url)
            except Exception:
                pass
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # ── helpers ──
    def quick_add(self, key, label, user=None):
        return cfg_api.quick_add_option(cfg_api.QuickAddIn(field_key=key, label=label),
                                        db=self.db, current_user=user or self.owner)

    def hide(self, key, value):
        row = next(r for r in fc._rows(self.db, self.company_id)[key] if r.value == value)
        cfg_api.update_option(row.id, cfg_api.OptionUpdate(is_active=False), db=self.db, current_user=self.owner)

    def code(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code

    # ── registry ──
    def test_new_fields_are_registered_and_addable(self):
        for key in ("learning_document.category", "employee_document.type"):
            self.assertIn(key, fc.FIELDS)
            self.assertTrue(fc.FIELDS[key].allow_custom)
            self.assertIn(key, fc.quick_add_keys(self.db, self.company_id))

    # ── learning documents ──
    def upload_ld(self, category, name="a.pdf"):
        out = ld_api.upload_document(title="Policy handbook", category=category, description=None,
                                     file=_file(name, PDF + uuid.uuid4().bytes), db=self.db, current_user=self.owner)
        self.saved.append(out["file_url"])
        return out

    def test_learning_document_category_added_via_quick_add_is_accepted(self):
        added = self.quick_add("learning_document.category", "Compliance Basics")
        self.assertTrue(added.created)
        meta = ld_api.documents_meta(db=self.db, current_user=self.owner)
        self.assertIn("Compliance Basics", meta["categories"])
        out = self.upload_ld("compliance basics")
        self.assertEqual(out["category"], "Compliance Basics")

    def test_learning_document_unknown_or_hidden_category_is_refused_but_unchanged_one_saves(self):
        self.assertEqual(self.code(self.upload_ld, "Secret Stuff"), 422)
        doc = self.upload_ld("Training")
        self.hide("learning_document.category", "Training")
        self.assertEqual(self.code(self.upload_ld, "Training", "b.pdf"), 422)
        # an edit that keeps the since-hidden category (and changes the title) still saves
        updated = ld_api.update_document(doc["id"], title="Renamed handbook", category="Training", description=None,
                                         file=None, db=self.db, current_user=self.owner)
        self.assertEqual((updated["title"], updated["category"]), ("Renamed handbook", "Training"))
        # moving it to a hidden category it is NOT in is refused
        self.assertEqual(self.code(ld_api.update_document, doc["id"], title=None, category="Nope", description=None,
                                   file=None, db=self.db, current_user=self.owner), 422)

    def test_long_learning_category_fits_the_widened_column(self):
        label = "L" * 100
        self.quick_add("learning_document.category", label)
        self.assertEqual(self.upload_ld(label)["category"], label)

    # ── employee documents ──
    def upload_doc(self, doc_type, user=None):
        rec = employees_api.upload_employee_document(
            employee_id=self.employee.id, document_type=doc_type, file=_file("d.pdf"),
            db=self.db, current_user=user or self.owner)
        self.saved.append(rec.file_url)
        return rec

    def test_employee_document_type_added_via_quick_add_is_accepted(self):
        self.assertEqual(self.upload_doc("PAN Card").document_type, "PAN Card")
        self.quick_add("employee_document.type", "Bank Passbook")
        self.assertEqual(self.upload_doc("bank passbook").document_type, "Bank Passbook")

    def test_employee_document_hidden_or_unknown_type_is_refused(self):
        self.assertEqual(self.code(self.upload_doc, "Made Up Type"), 422)
        self.hide("employee_document.type", "Offer Letter")
        self.assertEqual(self.code(self.upload_doc, "Offer Letter"), 422)

    def test_employee_profile_section_types_bypass_the_list(self):
        for t in ("Education Documents", "Professional Documentation"):
            self.assertEqual(self.upload_doc(t).document_type, t)

    # ── master-data creates behind the "Add new ..." rows ──
    def test_business_unit_and_department_creates_work_and_are_gated(self):
        name = f"QA BU {uuid.uuid4().hex[:6]}"
        bu = org_api.create_business_unit(schemas.BusinessUnitCreate(name=name), db=self.db, current_user=self.owner)
        self.assertEqual(bu.name, name)
        dept = org_api.create_department(
            schemas.DepartmentCreate(name=f"QA Dept {uuid.uuid4().hex[:6]}", business_unit_name=name),
            db=self.db, current_user=self.owner)
        self.assertEqual(str(dept.business_unit_id), str(bu.id))
        # a duplicate surfaces the server's message (409) -- shown inside the dialog
        self.assertEqual(self.code(org_api.create_business_unit, schemas.BusinessUnitCreate(name=name),
                                   db=self.db, current_user=self.owner), 409)
        if self.plain is None:
            self.skipTest("no plain user for the 403 check")
        checker = deps.require_permission("org_structure_config")
        self.assertEqual(self.code(checker, user=self.plain, db=self.db), 403)

    def test_project_type_create_works_and_is_gated(self):
        name = f"QA Type {uuid.uuid4().hex[:6]}"
        cat = projects_api.create_project_category(schemas.ProjectCategoryCreate(name=name), db=self.db,
                                                   current_user=self.owner)
        self.assertEqual(cat.name, name)
        self.assertEqual(self.code(projects_api.create_project_category, schemas.ProjectCategoryCreate(name=name),
                                   db=self.db, current_user=self.owner), 409)
        if self.plain is None:
            self.skipTest("no plain user for the 403 check")
        checker = deps.require_permission_or_action("projects_access", "projects", "edit")
        self.assertEqual(self.code(checker, user=self.plain, db=self.db), 403)


if __name__ == "__main__":
    unittest.main()
