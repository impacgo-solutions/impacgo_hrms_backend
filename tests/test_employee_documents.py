"""Employee Educational / Professional / Other Documents: several uploads per category,
nothing overwritten, metadata + employee mapping stored, and who may upload /
view (the employee, HR / admins, Add Employee access, Reporting Manager,
department head) -- unrelated people may not.

    cd backend && venv/Scripts/python -m unittest tests.test_employee_documents -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant impacgo-solutions by default, DOCS_TEST_TENANT to
override). Uploaded test files are removed again. Needs
backend/db/add_employee_document_meta.sql applied.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException, UploadFile  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, media_access, models, storage  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import employees as employees_api  # noqa: E402

TENANT = os.environ.get("DOCS_TEST_TENANT", "impacgo-solutions")
EDU, PRO, OTHER = "Education Documents", "Professional Documentation", "Other Documents"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _upload(name: str, data: bytes = PDF) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name, headers={"content-type": "application/pdf"})


class EmployeeDocumentTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.saved_files: list[str] = []
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        self.user_by_emp = {u.employee_id: u for u in users}
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                           and r.name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("no Owner")
        emps = self.db.scalars(select(models.Employee).where(models.Employee.company_id == self.owner.company_id,
                                                              models.Employee.is_active.is_(True))).all()
        self.emps = {e.id: e for e in emps}
        # An employee who has a reporting manager that can log in.
        self.worker = next((e for e in emps if e.id in self.user_by_emp and e.reporting_manager_id in self.user_by_emp
                            and e.id != self.owner.employee_id), None)
        if self.worker is None:
            self.skipTest("no employee with a login and a manager with a login")
        self.manager_user = self.user_by_emp[self.worker.reporting_manager_id]
        self.worker_user = self.user_by_emp[self.worker.id]

    def tearDown(self):
        for url in self.saved_files:
            try:
                storage.delete_uploaded_file(url)
            except Exception:
                pass
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def _do_upload(self, user, doc_type, name="cert.pdf", employee=None):
        rec = employees_api.upload_employee_document(
            employee_id=(employee or self.worker).id, document_type=doc_type, file=_upload(name),
            db=self.db, current_user=user)
        self.saved_files.append(rec.file_url)
        return rec

    def _unrelated_user(self):
        """A login that is neither the employee, in their management chain, nor
        their department head, and has no org-wide document visibility."""
        for emp_id, u in self.user_by_emp.items():
            if emp_id == self.worker.id or u.id == self.owner.id:
                continue
            emp = self.emps.get(emp_id)
            if emp is None or crud._is_in_reporting_chain(self.db, emp_id, self.worker.id):
                continue
            if crud._is_department_head_of(self.db, u, self.worker):
                continue
            if crud.get_visible_employee_ids_for_docs(self.db, u) is None:
                continue
            if self.worker.id in (crud.get_visible_employee_ids_for_docs(self.db, u) or []):
                continue
            return u
        return None

    # ── saving, metadata, multiple files ────────────────────────────────────
    def test_many_files_in_each_category_are_all_kept_and_mapped_to_the_employee(self):
        a = self._do_upload(self.owner, EDU, "degree.pdf")
        b = self._do_upload(self.owner, EDU, "marksheet.pdf")
        c = self._do_upload(self.owner, PRO, "offer.pdf")
        d = self._do_upload(self.owner, PRO, "relieving.pdf")
        self.db.flush()
        docs = crud.get_employee_documents(self.db, self.worker.id)
        by_id = {x["id"]: x for x in docs}
        for rec in (a, b, c, d):
            self.assertIn(str(rec.id), by_id)
            self.assertEqual(rec.employee_id, self.worker.id)
        self.assertEqual({by_id[str(r.id)]["category"] for r in (a, b)}, {"education"})
        self.assertEqual({by_id[str(r.id)]["category"] for r in (c, d)}, {"professional"})
        self.assertEqual(by_id[str(a.id)]["fileName"], "degree.pdf")
        self.assertEqual(by_id[str(a.id)]["sizeBytes"], len(PDF))
        self.assertTrue(by_id[str(a.id)]["file_url"].startswith("/media/employee_document/"))
        self.assertIn(str(self.worker.id), by_id[str(a.id)]["file_url"])
        self.assertIsNotNone(by_id[str(a.id)]["uploadedBy"])
        self.assertIsNotNone(by_id[str(a.id)]["uploadedAt"])
        # four distinct stored files, none overwritten
        self.assertEqual(len({r.file_url for r in (a, b, c, d)}), 4)

    def test_adding_more_later_keeps_what_was_uploaded_before(self):
        first = self._do_upload(self.worker_user, EDU, "first.pdf")
        before = {x["id"] for x in crud.get_employee_documents(self.db, self.worker.id)}
        self.assertIn(str(first.id), before)
        later = self._do_upload(self.worker_user, EDU, "later.pdf")
        after = {x["id"] for x in crud.get_employee_documents(self.db, self.worker.id)}
        self.assertTrue(before <= after)
        self.assertIn(str(later.id), after)
        for url in (first.file_url, later.file_url):
            self.assertTrue(storage.winlong_path(Path(storage.uploads_root()) / url.replace("/media/", "", 1)).exists())

    def test_same_file_name_twice_makes_two_records(self):
        one = self._do_upload(self.owner, PRO, "same.pdf")
        two = self._do_upload(self.owner, PRO, "same.pdf")
        self.assertNotEqual(one.id, two.id)
        self.assertNotEqual(one.file_url, two.file_url)

    def test_bad_files_are_refused_and_nothing_is_saved(self):
        count = len(crud.get_employee_documents(self.db, self.worker.id))
        with self.assertRaises(HTTPException) as ctx:
            employees_api.upload_employee_document(
                employee_id=self.worker.id, document_type=EDU,
                file=_upload("fake.pdf", b"MZ\x90\x00 this is not a pdf"), db=self.db, current_user=self.owner)
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            employees_api.upload_employee_document(
                employee_id=self.worker.id, document_type=EDU, file=_upload("run.exe"), db=self.db, current_user=self.owner)
        self.assertEqual(len(crud.get_employee_documents(self.db, self.worker.id)), count)

    # ── who may do what ─────────────────────────────────────────────────────
    def test_the_employee_can_upload_and_view_their_own(self):
        acc = crud.employee_document_access(self.db, self.worker_user, self.worker.id)
        self.assertEqual(acc, {"canView": True, "canUpload": True})
        rec = self._do_upload(self.worker_user, EDU)
        self.assertEqual(rec.employee_id, self.worker.id)

    def test_reporting_manager_can_upload_on_behalf_and_view(self):
        acc = crud.employee_document_access(self.db, self.manager_user, self.worker.id)
        self.assertEqual(acc, {"canView": True, "canUpload": True})
        rec = self._do_upload(self.manager_user, PRO, "from_manager.pdf")
        docs = {x["id"]: x for x in crud.get_employee_documents(self.db, self.worker.id)}
        self.assertEqual(docs[str(rec.id)]["fileName"], "from_manager.pdf")
        # and can open the file through /media
        self.assertIsNone(media_access.authorize(self.db, self.manager_user, rec.file_url.replace("/media/", "", 1)))

    def test_owner_can_upload_for_anyone(self):
        self.assertTrue(crud.employee_document_access(self.db, self.owner, self.worker.id)["canUpload"])

    def test_department_head_can_upload_for_their_department(self):
        dept = self.db.scalar(select(models.Department).where(
            models.Department.id == self.worker.department_id)) if self.worker.department_id else None
        head_user = None
        for d in self.db.scalars(select(models.Department).where(models.Department.head_employee_id.is_not(None))):
            member = next((e for e in self.emps.values() if e.department_id == d.id and e.id != d.head_employee_id), None)
            if member is not None and d.head_employee_id in self.user_by_emp:
                head_user, self.worker, self.worker_user = self.user_by_emp[d.head_employee_id], member, self.user_by_emp.get(member.id)
                break
        if head_user is None:
            self.skipTest("no department with a head who can log in and a member")
        acc = crud.employee_document_access(self.db, head_user, self.worker.id)
        self.assertEqual(acc, {"canView": True, "canUpload": True})
        rec = self._do_upload(head_user, EDU, "dept_head.pdf")
        self.assertEqual(rec.employee_id, self.worker.id)

    def test_an_unrelated_employee_can_neither_view_nor_upload(self):
        other = self._unrelated_user()
        if other is None:
            self.skipTest("no unrelated login available")
        acc = crud.employee_document_access(self.db, other, self.worker.id)
        self.assertEqual(acc, {"canView": False, "canUpload": False})
        with self.assertRaises(HTTPException) as ctx:
            self._do_upload(other, EDU)
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(HTTPException) as ctx:
            employees_api.get_employee_documents(employee_id=self.worker.id, db=self.db, current_user=other)
        self.assertEqual(ctx.exception.status_code, 403)
        rec = self._do_upload(self.owner, EDU)
        self.assertEqual(media_access.authorize(self.db, other, rec.file_url.replace("/media/", "", 1)), media_access.FORBIDDEN)

    def test_another_companys_employee_is_not_found(self):
        self.assertEqual(crud.employee_document_access(self.db, self.owner, __import__("uuid").uuid4()),
                         {"canView": False, "canUpload": False})

    def test_access_endpoint_reports_the_callers_rights(self):
        r = employees_api.get_employee_document_access(employee_id=self.worker.id, db=self.db, current_user=self.manager_user)
        self.assertEqual(r, {"canView": True, "canUpload": True})

    def test_category_mapping(self):
        self.assertEqual(crud.employee_document_category(EDU), "education")
        self.assertEqual(crud.employee_document_category(PRO), "professional")
        self.assertEqual(crud.employee_document_category("PAN Card"), "other")
        self.assertEqual(crud.employee_document_category(OTHER), "other")

    def test_documents_without_metadata_still_list(self):
        rec = crud.create_document_record(self.db, self.worker.id, "Offer Letter", "Verified",
                                          crud.company_today(self.db, self.owner.company_id), None)
        docs = {x["id"]: x for x in crud.get_employee_documents(self.db, self.worker.id)}
        self.assertEqual(docs[str(rec.id)]["category"], "other")
        self.assertIsNone(docs[str(rec.id)]["fileName"])

    # ── Other Documents section ─────────────────────────────────────────────
    def test_other_documents_take_several_files_without_form_configuration(self):
        a = self._do_upload(self.worker_user, OTHER, "aadhaar.pdf")
        b = self._do_upload(self.manager_user, OTHER, "pan.pdf")
        docs = {x["id"]: x for x in crud.get_employee_documents(self.db, self.worker.id)}
        for rec, name in ((a, "aadhaar.pdf"), (b, "pan.pdf")):
            self.assertEqual(rec.document_type, OTHER)
            self.assertEqual(docs[str(rec.id)]["category"], "other")
            self.assertEqual(docs[str(rec.id)]["fileName"], name)

    # ── View / Download ─────────────────────────────────────────────────────
    def _file(self, user, rec, download=True):
        return employees_api.get_employee_document_file(
            employee_id=self.worker.id, document_id=rec.id, download=download, db=self.db, current_user=user)

    def test_view_and_download_return_the_file_under_its_original_name(self):
        for doc_type in (EDU, PRO, OTHER):
            rec = self._do_upload(self.worker_user, doc_type, f"{doc_type[:3].lower()}_file.pdf")
            for user in (self.worker_user, self.manager_user, self.owner):
                dl = self._file(user, rec)
                self.assertTrue(Path(dl.path).is_file())
                self.assertEqual(dl.media_type, "application/pdf")
                self.assertIn("attachment", dl.headers["content-disposition"])
                self.assertIn(f"{doc_type[:3].lower()}_file.pdf", dl.headers["content-disposition"])
                self.assertIn("inline", self._file(user, rec, download=False).headers["content-disposition"])

    def test_unrelated_user_cannot_view_or_download(self):
        other = self._unrelated_user()
        if other is None:
            self.skipTest("no unrelated login available")
        rec = self._do_upload(self.owner, OTHER)
        with self.assertRaises(HTTPException) as ctx:
            self._file(other, rec)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_download_of_a_record_without_a_file_or_of_another_employee_is_404(self):
        bare = crud.create_document_record(self.db, self.worker.id, "Offer Letter", "Verified",
                                           crud.company_today(self.db, self.owner.company_id), None)
        with self.assertRaises(HTTPException) as ctx:
            self._file(self.owner, bare)
        self.assertEqual(ctx.exception.status_code, 404)
        someone_else = next(e for e in self.emps.values() if e.id != self.worker.id)
        rec = self._do_upload(self.owner, EDU, employee=someone_else)
        with self.assertRaises(HTTPException) as ctx:
            self._file(self.owner, rec)  # right document id, wrong employee in the URL
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
