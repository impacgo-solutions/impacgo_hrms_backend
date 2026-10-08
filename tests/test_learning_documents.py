"""Learning & Development > Learning Documents (routers/learning_documents.py):
upload -> visible to every employee of the company (uploader, title, date,
type, description) -> search / filter / sort -> download / media access ->
edit / replace (uploader or HR) -> delete (soft, file removed) -> upload
history; validation (type, size, empty, content/extension mismatch, title,
category), duplicates, permissions and tenant isolation.

    cd backend && venv/Scripts/python -m unittest tests.test_learning_documents -v

Runs inside a transaction that is ALWAYS rolled back (tenant
impacgo-solutions by default, LEARNING_TEST_TENANT to override); stored
test files are deleted in tearDown.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException, UploadFile  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from starlette.datastructures import Headers  # noqa: E402

from app import crud, database, media_access, models  # noqa: E402
from app.routers import learning_documents as api  # noqa: E402
from app.storage import uploads_root, winlong_path  # noqa: E402

TENANT = os.environ.get("LEARNING_TEST_TENANT", "impacgo-solutions")
PDF = b"%PDF-1.4\n% learning qa "


def _file(name: str, data: bytes, ctype: str = "application/octet-stream") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name, headers=Headers({"content-type": ctype}))


class LearningDocumentTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = [u for u in self.db.scalars(select(models.User).where(
            models.User.employee_id.is_not(None), models.User.status == "active")).all()]
        self.owner = next((u for u in users if api._is_owner(self.db, u)), None)
        plain = [u for u in users if not api._is_owner(self.db, u) and api._has(self.db, u, "view")
                 and not api._has(self.db, u, "edit") and not api._has(self.db, u, "delete")]
        if self.owner is None or len(plain) < 2:
            self.skipTest("need an Owner and two employees with plain Learning view access")
        self.uploader, self.other = plain[0], plain[1]
        self.company_id = self.owner.company_id
        self.dirs: list[uuid.UUID] = []
        self.n = 0

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()
        for d in self.dirs:
            shutil.rmtree(uploads_root() / api.ENTITY_TYPE / str(d), ignore_errors=True)

    # ── helpers ──
    def upload(self, user=None, title="Onboarding handbook", data=None, name="handbook.pdf",
               category="Training", description="Read before your first week."):
        self.n += 1
        out = api.upload_document(title=title, category=category, description=description,
                                  file=_file(name, data if data is not None else PDF + str(uuid.uuid4()).encode()),
                                  db=self.db, current_user=user or self.uploader)
        self.dirs.append(out["id"])
        return out

    def listing(self, user=None, **kw):
        params = dict(q=None, category=None, file_type=None, uploaded_by=None, mine=False, date_from=None,
                      date_to=None, sort="uploaded_at", order="desc", limit=200, offset=0)
        params.update(kw)
        return api.list_documents(**params, db=self.db, current_user=user or self.other)

    def status_of(self, code, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        self.assertEqual(ctx.exception.status_code, code, ctx.exception.detail)
        return ctx.exception.detail

    def path_of(self, doc):
        return winlong_path(uploads_root() / doc["file_url"].removeprefix("/media/"))

    # ── tests ──
    def test_upload_visible_to_every_employee_with_metadata(self):
        doc = self.upload()
        self.assertTrue(self.path_of(doc).is_file(), "file stored in upload storage")
        row = self.db.get(models.LearningDocument, doc["id"])
        self.assertEqual((row.company_id, row.title, row.category, row.file_ext), (self.company_id, "Onboarding handbook", "Training", ".pdf"))
        for viewer in (self.other, self.owner, self.uploader):
            items = self.listing(viewer)["items"]
            mine = next(i for i in items if i["id"] == doc["id"])
            self.assertEqual(mine["uploaded_by"], crud.employee_display_name(self.db, self.uploader.employee_id))
            self.assertEqual((mine["title"], mine["file_type"], mine["description"]),
                             ("Onboarding handbook", "PDF", "Read before your first week."))
            self.assertIsNotNone(mine["uploaded_at"])
        other_view = next(i for i in self.listing(self.other)["items"] if i["id"] == doc["id"])
        self.assertEqual((other_view["can_edit"], other_view["can_delete"]), (False, False))
        own_view = next(i for i in self.listing(self.uploader)["items"] if i["id"] == doc["id"])
        self.assertEqual((own_view["can_edit"], own_view["can_delete"], own_view["is_mine"]), (True, True, True))
        # Download: the real file under its original name.
        resp = api.download_document(doc["id"], download=True, db=self.db, current_user=self.other)
        self.assertIn('attachment; filename="handbook.pdf"', resp.headers["content-disposition"])
        inline = api.download_document(doc["id"], download=False, db=self.db, current_user=self.other)
        self.assertTrue(inline.headers["content-disposition"].startswith("inline"))
        rel = doc["file_url"].removeprefix("/media/")
        self.assertIsNone(media_access.authorize(self.db, self.other, rel), "any employee may open it")

    def test_search_filter_sort(self):
        a = self.upload(title="Python basics", name="python.pdf", category="Learning", description="intro course")
        b = self.upload(user=self.owner, title="Advanced SQL deck", name="sql.pptx",
                        data=b"PK\x03\x04" + uuid.uuid4().bytes, category="Development", description="joins and indexes")
        c = self.upload(title="Safety checklist", name="safety.csv", data=b"step,owner\n1,hr\n" + uuid.uuid4().hex.encode(),
                        category="Training", description=None)
        ids = lambda r: [i["id"] for i in r["items"] if i["id"] in (a["id"], b["id"], c["id"])]  # noqa: E731
        self.assertEqual(ids(self.listing(q="sql")), [b["id"]])
        self.assertEqual(ids(self.listing(q="indexes")), [b["id"]], "searches the description")
        self.assertEqual(set(ids(self.listing(q=crud.employee_display_name(self.db, self.uploader.employee_id)))),
                         {a["id"], c["id"]}, "searches the uploader")
        self.assertEqual(ids(self.listing(category="Development")), [b["id"]])
        self.assertEqual(ids(self.listing(file_type="CSV")), [c["id"]])
        self.assertEqual(ids(self.listing(file_type="PowerPoint")), [b["id"]])
        self.assertEqual(set(ids(self.listing(self.uploader, mine=True))), {a["id"], c["id"]})
        self.assertEqual(ids(self.listing(uploaded_by=self.owner.employee_id)), [b["id"]])
        self.assertEqual(ids(self.listing(sort="title", order="asc")), [b["id"], a["id"], c["id"]])
        self.assertEqual(ids(self.listing(sort="uploaded_at", order="desc"))[0], c["id"])
        self.assertIn(str(self.owner.employee_id), [u["id"] for u in self.listing()["uploaders"]])
        self.status_of(422, self.listing, sort="password")

    def test_validation_and_duplicates(self):
        self.status_of(400, self.upload, name="virus.exe", data=b"MZ...")
        self.status_of(422, self.upload, data=b"")
        self.status_of(400, self.upload, name="fake.pdf", data=b"just text, not a pdf")
        self.status_of(400, self.upload, name="fake.docx", data=b"%PDF-1.4 not a docx")
        self.status_of(400, self.upload, name="notes.txt", data=b"abc\x00\x01binary")
        self.status_of(422, self.upload, title="ab")
        self.status_of(422, self.upload, category="Secret")
        with mock.patch.object(api, "_MAX_UPLOAD_BYTES", 32):
            self.status_of(413, self.upload, data=PDF + b"x" * 64)
        same = PDF + b"identical content"
        first = self.upload(data=same, name="a.pdf")
        detail = self.status_of(409, self.upload, user=self.other, data=same, name="renamed.pdf", title="Copy")
        self.assertIn(first["title"], detail)
        # Nothing half-saved by the rejected uploads.
        self.assertEqual(self.db.scalar(select(models.LearningDocument.id).where(
            models.LearningDocument.content_sha256 == self.db.get(models.LearningDocument, first["id"]).content_sha256,
            models.LearningDocument.id != first["id"])), None)
        # Once deleted, the same file may be shared again.
        api.delete_document(first["id"], db=self.db, current_user=self.uploader)
        again = self.upload(data=same, name="a.pdf")
        self.assertNotEqual(again["id"], first["id"])

    def test_edit_replace_delete_permissions_and_history(self):
        doc = self.upload()
        old_path = self.path_of(doc)
        # Another employee: no edit / delete.
        self.status_of(403, api.update_document, doc["id"], title="Hijacked", category=None, description=None, file=None,
                       db=self.db, current_user=self.other)
        self.status_of(403, api.delete_document, doc["id"], db=self.db, current_user=self.other)
        # Uploader edits metadata.
        out = api.update_document(doc["id"], title="Onboarding handbook v2", category="Learning", description="Updated",
                                  file=None, db=self.db, current_user=self.uploader)
        self.assertEqual((out["title"], out["category"], out["description"]), ("Onboarding handbook v2", "Learning", "Updated"))
        self.status_of(400, api.update_document, doc["id"], title="Onboarding handbook v2", category=None,
                       description=None, file=None, db=self.db, current_user=self.uploader)
        # HR / Owner replaces the file -> version 2, old file removed.
        out = api.update_document(doc["id"], title=None, category=None, description=None,
                                  file=_file("handbook-2026.docx", b"PK\x03\x04" + uuid.uuid4().bytes),
                                  db=self.db, current_user=self.owner)
        self.assertEqual((out["version"], out["file_type"], out["file_name"]), (2, "Word", "handbook-2026.docx"))
        self.assertFalse(old_path.exists(), "replaced file removed from storage")
        self.assertTrue(self.path_of(out).is_file())
        # Owner (HR/Admin) deletes -> gone everywhere, file removed, history kept.
        api.delete_document(doc["id"], db=self.db, current_user=self.owner)
        self.assertNotIn(doc["id"], [i["id"] for i in self.listing()["items"]])
        self.status_of(404, api.get_document, doc["id"], db=self.db, current_user=self.uploader)
        self.status_of(404, api.download_document, doc["id"], download=True, db=self.db, current_user=self.uploader)
        self.assertFalse(self.path_of(out).exists())
        self.assertEqual(media_access.authorize(self.db, self.other, out["file_url"].removeprefix("/media/")),
                         media_access.NOT_FOUND)
        hist = api.upload_history(document_id=doc["id"], limit=50, offset=0, db=self.db, current_user=self.other)
        self.assertEqual([h["action"] for h in reversed(hist["items"])], ["uploaded", "updated", "file_replaced", "deleted"])
        self.assertFalse(hist["items"][0]["document_available"])
        self.assertTrue(all(h["actor"] for h in hist["items"]))

    def test_tenant_isolation(self):
        doc = self.upload()
        foreign = models.User(id=uuid.uuid4(), company_id=uuid.uuid4(), employee_id=None, email="x@other.tld")
        with mock.patch.object(api, "_is_owner", return_value=True):
            self.status_of(404, api.get_document, doc["id"], db=self.db, current_user=foreign)
            self.status_of(404, api.download_document, doc["id"], download=True, db=self.db, current_user=foreign)
            self.status_of(404, api.delete_document, doc["id"], db=self.db, current_user=foreign)
            self.assertNotIn(doc["id"], [i["id"] for i in self.listing(foreign)["items"]])
            self.assertEqual(api.upload_history(document_id=None, limit=50, offset=0, db=self.db, current_user=foreign)["total"], 0)
        self.assertEqual(media_access.authorize(self.db, foreign, doc["file_url"].removeprefix("/media/")),
                         media_access.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
