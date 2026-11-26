"""Learning Documents: multi-file + folder upload (structure preserved),
folder browse / create / rename / delete, validation and permissions.

    cd backend && venv/Scripts/python -m unittest tests.test_learning_folders -v

Same setup as test_learning_documents (transaction always rolled back,
stored files removed).
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

from app import models  # noqa: E402
from app.routers import learning_documents as api  # noqa: E402
from app.storage import uploads_root  # noqa: E402
from tests.test_learning_documents import PDF, LearningDocumentTests, _file  # noqa: E402


def pdf() -> bytes:
    return PDF + uuid.uuid4().bytes


class LearningFolderTests(LearningDocumentTests):
    def tearDown(self):
        for f in getattr(self, "bulk_ids", []):
            shutil.rmtree(uploads_root() / api.ENTITY_TYPE / str(f), ignore_errors=True)
        super().tearDown()

    def bulk(self, items, user=None, folder_id=None, category="Learning", description=None):
        out = api.bulk_upload(files=[_file(p.split("/")[-1], data) for p, data in items],
                              paths=[p for p, _ in items], folder_id=folder_id, category=category,
                              description=description, db=self.db, current_user=user or self.uploader)
        self.bulk_ids = getattr(self, "bulk_ids", []) + [d["id"] for d in out["uploaded"]]
        return out

    def browse(self, folder_id=None, user=None):
        return api.browse(folder_id=folder_id, sort="title", order="asc", db=self.db,
                          current_user=user or self.other)

    # ── the tests inherited from LearningDocumentTests still run here too ──

    def test_multiple_files_at_once_at_top_level(self):
        out = self.bulk([("week-1_intro.pdf", pdf()), ("qa.csv", b"q,a\n1,2\n" + uuid.uuid4().hex.encode()),
                         ("notes.txt", b"hello " + uuid.uuid4().hex.encode())])
        self.assertEqual(len(out["uploaded"]), 3)
        self.assertEqual(out["skipped"], [])
        # title = file name tidied; a stem under 3 characters keeps the full name
        self.assertEqual({d["title"] for d in out["uploaded"]}, {"week 1 intro", "qa.csv", "notes"})
        self.assertTrue(all(d["folder_id"] is None for d in out["uploaded"]))
        top = {d["id"] for d in self.browse()["documents"]}
        self.assertTrue({str(d["id"]) for d in out["uploaded"]} <= {str(x) for x in top})

    def test_folder_upload_preserves_structure_and_reuses_folders(self):
        root = f"Onboarding {uuid.uuid4().hex[:6]}"
        out = self.bulk([(f"{root}/welcome.pdf", pdf()), (f"{root}/Week 1/day1.pdf", pdf()),
                         (f"{root}/Week 1/Labs/lab1.pdf", pdf()), (f"{root}/Week 2/day6.pdf", pdf())],
                        category="Training", description="New joiner pack")
        self.assertEqual((len(out["uploaded"]), out["folders_created"]), (4, 4))
        self.assertTrue(all(d["category"] == "Training" and d["description"] == "New joiner pack"
                            for d in out["uploaded"]))
        paths = {d["file_name"]: d["folder_path"] for d in out["uploaded"]}
        self.assertEqual(paths, {"welcome.pdf": root, "day1.pdf": f"{root} / Week 1",
                                 "lab1.pdf": f"{root} / Week 1 / Labs", "day6.pdf": f"{root} / Week 2"})
        # browse: top -> root folder -> Week 1 -> Labs, with breadcrumb + counts
        top = next(f for f in self.browse()["folders"] if f["name"] == root)
        self.assertEqual((top["folder_count"], top["file_count"], top["total_files"]), (2, 1, 4))
        inside = self.browse(top["id"])
        self.assertEqual([f["name"] for f in inside["folders"]], ["Week 1", "Week 2"])
        self.assertEqual([d["file_name"] for d in inside["documents"]], ["welcome.pdf"])
        week1 = inside["folders"][0]
        labs = self.browse(week1["id"])["folders"][0]
        deep = self.browse(labs["id"])
        self.assertEqual([b["name"] for b in deep["breadcrumb"]], [root, "Week 1", "Labs"])
        self.assertEqual([d["file_name"] for d in deep["documents"]], ["lab1.pdf"])
        # a second request of the same upload goes into the same folders
        again = self.bulk([(f"{root}/Week 1/day2.pdf", pdf())])
        self.assertEqual(again["folders_created"], 0)
        self.assertEqual(again["uploaded"][0]["folder_id"], week1["id"])
        # search inside a folder (and below)
        found = self.listing(folder_id=top["id"])
        self.assertEqual(found["total"], 5)

    def test_bad_files_are_skipped_others_uploaded(self):
        dup = pdf()
        self.upload(data=dup, name="already.pdf")
        out = self.bulk([("Pack/ok.pdf", pdf()), ("Pack/virus.exe", b"MZ"), ("Pack/empty.pdf", b""),
                         ("Pack/fake.pdf", b"<html>"), ("Pack/again.pdf", dup), ("Pack/twice-a.pdf", (t := pdf())),
                         ("Pack/twice-b.pdf", t), ("../escape.pdf", pdf())])
        self.assertEqual([d["file_name"] for d in out["uploaded"]], ["ok.pdf", "twice-a.pdf"])
        reasons = {s["path"]: s["reason"] for s in out["skipped"]}
        self.assertIn("not supported", reasons["Pack/virus.exe"])
        self.assertIn("empty", reasons["Pack/empty.pdf"])
        self.assertIn("not a valid", reasons["Pack/fake.pdf"])
        self.assertIn("already been uploaded", reasons["Pack/again.pdf"])
        self.assertIn("already in this upload", reasons["Pack/twice-b.pdf"])
        self.assertIn("Invalid", reasons["../escape.pdf"])

    def test_create_rename_delete_folder_and_permissions(self):
        name = f"Guides {uuid.uuid4().hex[:6]}"
        f = api.create_folder(api.FolderCreate(name=name), db=self.db, current_user=self.uploader)
        self.status_of(409, api.create_folder, api.FolderCreate(name=name.upper()), db=self.db,
                       current_user=self.other)
        self.status_of(422, api.create_folder, api.FolderCreate(name="a/b"), db=self.db, current_user=self.uploader)
        sub = api.create_folder(api.FolderCreate(name="Sub", parent_id=f["id"]), db=self.db,
                                current_user=self.uploader)
        self.assertEqual(sub["parent_id"], f["id"])
        # rename: creator yes, another employee no
        self.status_of(403, api.rename_folder, f["id"], api.FolderRename(name="X"), db=self.db,
                       current_user=self.other)
        renamed = api.rename_folder(f["id"], api.FolderRename(name=name + " v2"), db=self.db,
                                    current_user=self.uploader)
        self.assertEqual(renamed["name"], name + " v2")
        # someone else's file inside -> the creator alone can't delete; HR/Owner can
        theirs = self.bulk([("x.pdf", pdf())], user=self.other, folder_id=sub["id"])["uploaded"][0]
        self.status_of(403, api.delete_folder, f["id"], db=self.db, current_user=self.uploader)
        self.status_of(403, api.delete_folder, f["id"], db=self.db, current_user=self.other)
        api.delete_folder(f["id"], db=self.db, current_user=self.owner)
        self.assertTrue(self.db.get(models.LearningDocument, theirs["id"]).is_deleted)
        self.assertTrue(self.db.get(models.LearningFolder, sub["id"]).is_deleted)
        self.assertNotIn(f["id"], [x["id"] for x in self.browse()["folders"]])
        self.status_of(404, self.browse, f["id"])
        hist = self.db.scalars(select(models.LearningDocumentHistory).where(
            models.LearningDocumentHistory.document_id == theirs["id"])).all()
        self.assertEqual([h.action for h in sorted(hist, key=lambda h: h.created_at)], ["uploaded", "deleted"])

    def test_upload_into_a_folder_and_tenant_scoping(self):
        f = api.create_folder(api.FolderCreate(name=f"Into {uuid.uuid4().hex[:6]}"), db=self.db,
                              current_user=self.uploader)
        doc = api.upload_document(title="Inside doc", category="Learning", description=None, folder_id=f["id"],
                                  file=_file("inside.pdf", pdf()), db=self.db, current_user=self.uploader)
        self.dirs.append(doc["id"])
        self.assertEqual(doc["folder_id"], f["id"])
        self.status_of(404, api.upload_document, title="Bad folder", category="Learning", description=None,
                       folder_id=uuid.uuid4(), file=_file("b.pdf", pdf()), db=self.db, current_user=self.uploader)
        self.status_of(404, self.bulk, [("c.pdf", pdf())], folder_id=uuid.uuid4())

    def test_json_files_single_and_in_folders(self):
        self.assertIn("json", api.documents_meta(db=self.db, current_user=self.uploader)["allowed_extensions"])
        good = ('{"course": "Onboarding", "weeks": [1, 2], "id": "%s"}' % uuid.uuid4()).encode()
        single = api.upload_document(title="Course outline", category="Learning", description=None, folder_id=None,
                                     file=_file("outline.json", good, "application/json"), db=self.db,
                                     current_user=self.uploader)
        self.dirs.append(single["id"])
        self.assertEqual((single["file_type"], single["file_ext"], single["mime_type"]), ("JSON", "json", "application/json"))
        resp = api.download_document(single["id"], download=False, db=self.db, current_user=self.other)
        self.assertEqual(resp.media_type, "application/json")
        out = self.bulk([("Configs/app.json", ('{"v": "%s"}' % uuid.uuid4()).encode()),
                         ("Configs/bom.json", b"\xef\xbb\xbf" + ('[1, "%s"]' % uuid.uuid4()).encode()),
                         ("Configs/broken.json", b'{"v": 1,'), ("Configs/binary.json", b'{"a\x00"}')])
        self.assertEqual(sorted(d["file_name"] for d in out["uploaded"]), ["app.json", "bom.json"])
        reasons = {s["path"]: s["reason"] for s in out["skipped"]}
        self.assertIn("not a valid JSON", reasons["Configs/broken.json"])
        self.assertIn("not a plain-text", reasons["Configs/binary.json"])
        self.assertEqual(self.listing(file_type="JSON")["total"], 3)

    def test_json_stays_refused_by_other_uploads(self):
        from fastapi import HTTPException

        from app import storage
        with self.assertRaises(HTTPException) as ctx:
            storage.save_uploaded_file(_file("x.json", b"{}"), entity_type="employee_document", entity_id=uuid.uuid4())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_stored_file_is_reported_not_crashing(self):
        d = self.upload()
        self.assertTrue(next(x for x in self.browse()["documents"] if x["id"] == d["id"])["file_available"])
        self.path_of(d).unlink()  # e.g. uploaded through another server sharing the DB
        listed = next(x for x in self.listing()["items"] if x["id"] == d["id"])
        self.assertFalse(listed["file_available"])
        detail = self.status_of(404, api.download_document, d["id"], download=False, db=self.db, current_user=self.other)
        self.assertIn("not available on this server", detail)

    def test_existing_top_level_documents_unchanged(self):
        doc = self.upload()
        self.assertIsNone(doc["folder_id"])
        self.assertEqual(doc["folder_path"], "")
        self.assertIn(str(doc["id"]), {str(d["id"]) for d in self.browse()["documents"]})


if __name__ == "__main__":
    unittest.main()
