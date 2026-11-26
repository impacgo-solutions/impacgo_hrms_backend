"""Asset images (Create Asset > Upload Asset Images, Asset Details gallery).

    cd backend && venv/Scripts/python -m unittest tests.test_asset_images -v

Real routes against the dev DB inside a transaction that is always rolled
back; files go to a temporary uploads dir.
"""

from __future__ import annotations

import datetime
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

from app import config, media_access, models  # noqa: E402
from tests.test_qa_employees_org import IC, _QABase  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class AssetImageTests(_QABase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="asset_images_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        mock.patch.object(config.settings, "uploads_dir", self.tmp).start()
        self.addCleanup(mock.patch.stopall)
        r = self.client.post("/api/asset-inventory", json={
            "tag": "IMG-" + uuid.uuid4().hex[:8], "type": "Laptop", "category": "office"})
        self.assertEqual(r.status_code, 201, r.text)
        self.asset = r.json()
        self.assertEqual(self.asset["images"], [])

    def upload(self, *files):
        return self.client.post(f"/api/asset-inventory/{self.asset['id']}/images",
                                files=[("files", f) for f in files])

    def on_disk(self, url: str) -> Path:
        return Path(self.tmp) / url.split("?")[0].removeprefix("/media/")

    def test_upload_several_at_once_stored_and_listed(self):
        r = self.upload(("front.png", PNG, "image/png"), ("back.jpg", JPG, "image/jpeg"),
                        ("side.jpeg", JPG, "image/jpeg"))
        self.assertEqual(r.status_code, 200, r.text)
        imgs = r.json()["images"]
        self.assertEqual([i["name"] for i in imgs], ["front.png", "back.jpg", "side.jpeg"])
        for i in imgs:
            self.assertTrue(i["url"].startswith(f"/media/asset_image/{self.asset['id']}/"))
            self.assertTrue(self.on_disk(i["url"]).is_file())
        rows = self.db.scalars(select(models.AssetImage).where(
            models.AssetImage.asset_id == uuid.UUID(self.asset["id"]))).all()
        self.assertEqual(len(rows), 3)
        listed = next(a for a in self.client.get("/api/asset-inventory?limit=500").json()
                      if a["id"] == self.asset["id"])
        self.assertEqual([i["name"] for i in listed["images"]], ["front.png", "back.jpg", "side.jpeg"])
        # more can be added later, appended in order
        r2 = self.upload(("extra.png", PNG, "image/png"))
        self.assertEqual([i["name"] for i in r2.json()["images"]][-1], "extra.png")

    def test_documents_accepted_alongside_images(self):
        r = self.upload(("front.png", PNG, "image/png"), ("invoice.pdf", b"%PDF-1.4 invoice", "application/pdf"),
                        ("manual.docx", b"PK\x03\x04" + b"\x00" * 40, "application/vnd.openxmlformats"),
                        ("notes.txt", b"warranty until 2028", "text/plain"))
        self.assertEqual(r.status_code, 200, r.text)
        kinds = {i["name"]: (i["kind"], i["ext"]) for i in r.json()["images"]}
        self.assertEqual(kinds, {"front.png": ("image", "png"), "invoice.pdf": ("document", "pdf"),
                                 "manual.docx": ("document", "docx"), "notes.txt": ("document", "txt")})
        for i in r.json()["images"]:
            self.assertTrue(self.on_disk(i["url"]).is_file())
        # same secured media rule as images
        rel = next(i for i in r.json()["images"] if i["name"] == "invoice.pdf")["url"].split("?")[0].removeprefix("/media/")
        self.assertIsNone(media_access.authorize(self.db, self.owner, rel))

    def test_rejects_unsupported_and_fakes_all_or_nothing(self):
        self.assertEqual(self.upload(("tool.exe", b"MZ\x90\x00", "application/octet-stream")).status_code, 400)
        self.assertEqual(self.upload(("fake.pdf", b"<html>not a pdf</html>", "application/pdf")).status_code, 400)
        # an HTML page renamed .png -- content check; the valid first file isn't kept either
        r = self.upload(("ok.png", PNG, "image/png"), ("evil.png", b"<html>hi</html>", "image/png"))
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.db.scalars(select(models.AssetImage).where(
            models.AssetImage.asset_id == uuid.UUID(self.asset["id"]))).all(), [])
        self.assertEqual(list(Path(self.tmp).rglob("*.png")), [])

    def test_size_and_count_limits(self):
        big = PNG + b"\x00" * (5 * 1024 * 1024)
        self.assertEqual(self.upload(("big.png", big, "image/png")).status_code, 400)       # images: 5MB
        doc7 = b"%PDF-1.4 " + b"\x00" * (7 * 1024 * 1024)
        self.assertEqual(self.upload(("seven.pdf", doc7, "application/pdf")).status_code, 200)  # docs: up to 10MB
        doc11 = b"%PDF-1.4 " + b"\x00" * (11 * 1024 * 1024)
        self.assertEqual(self.upload(("eleven.pdf", doc11, "application/pdf")).status_code, 400)
        self.assertEqual(self.upload(*[(f"{i}.png", PNG, "image/png") for i in range(20)]).status_code, 400)
        self.assertEqual(self.upload(*[(f"{i}.png", PNG, "image/png") for i in range(19)]).status_code, 200)
        self.assertEqual(self.upload(("one-more.png", PNG, "image/png")).status_code, 400)  # 20 per asset

    def test_remove_image_deletes_row_and_file(self):
        imgs = self.upload(("a.png", PNG, "image/png"), ("b.png", PNG, "image/png")).json()["images"]
        r = self.client.delete(f"/api/asset-inventory/{self.asset['id']}/images/{imgs[0]['id']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([i["name"] for i in r.json()["images"]], ["b.png"])
        self.assertFalse(self.on_disk(imgs[0]["url"]).exists())
        self.assertEqual(self.client.delete(
            f"/api/asset-inventory/{self.asset['id']}/images/{imgs[0]['id']}").status_code, 404)

    def test_deleting_asset_removes_its_image_files(self):
        imgs = self.upload(("a.png", PNG, "image/png")).json()["images"]
        self.assertEqual(self.client.delete(f"/api/asset-inventory/{self.asset['id']}").status_code, 204)
        self.assertFalse(self.on_disk(imgs[0]["url"]).exists())

    def test_upload_requires_asset_management_edit(self):
        ic = self.as_(self.login(self.new_employee(), IC))
        self.assertIn(self.upload(("a.png", PNG, "image/png")).status_code, (403, 404))
        self.assertIsNotNone(ic)

    def test_media_access(self):
        url = self.upload(("a.png", PNG, "image/png")).json()["images"][0]["url"].split("?")[0]
        rel = url.removeprefix("/media/")
        self.assertIsNone(media_access.authorize(self.db, self.owner, rel))       # owner / asset admin
        emp = self.new_employee()
        ic = self.login(emp, IC)
        from app import crud
        crud.clear_rbac_memo(self.db)
        if crud.effective_user_matrix(self.db, ic).get("asset_management", "n") == "n":
            self.assertEqual(media_access.authorize(self.db, ic, rel), media_access.FORBIDDEN)
            # ...until the asset is assigned to them
            self.db.add(models.AssetAssignment(id=uuid.uuid4(), asset_id=uuid.UUID(self.asset["id"]),
                                               employee_id=emp.id, assigned_on=datetime.date.today()))
            self.db.flush()
            self.assertIsNone(media_access.authorize(self.db, ic, rel))
        # another company / unknown asset -> not found
        self.assertEqual(media_access.authorize(self.db, self.owner, f"asset_image/{uuid.uuid4()}/x.png"),
                         media_access.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
