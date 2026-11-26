"""Asset ID Format (Administration > General Settings > Document ID Formats >
Asset ID) generating the Asset Tag of every new asset.

    cd backend && venv/Scripts/python -m unittest tests.test_asset_ids -v

Real routes against the dev DB inside a transaction that is always rolled back.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

from app import document_numbers, models  # noqa: E402
from tests.test_qa_employees_org import _QABase  # noqa: E402


class AssetIdFormatTests(_QABase):
    def create(self, **extra):
        r = self.client.post("/api/asset-inventory", json={"type": "Laptop", "category": "office", **extra})
        return r

    def preview(self):
        r = self.client.get("/api/settings/id-formats/asset/preview")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["next_code"]

    def test_listed_with_the_other_formats_default_ast_three_digits(self):
        formats = {f["doctype"]: f for f in self.client.get("/api/settings/id-formats").json()}
        self.assertIn("asset", formats)
        self.assertEqual(formats["asset"]["label"], "Asset ID")
        self.assertTrue({"project", "travel_request", "expense_report"} <= set(formats))
        series = document_numbers.get_series(self.db, self.company_id, "asset")
        if (series.prefix, series.padding, series.suffix) == ("AST-", 3, ""):
            self.assertRegex(formats["asset"]["next_code"], r"^AST-\d{3,}$")

    def test_new_assets_get_sequential_unique_tags_from_the_format(self):
        expected = self.preview()
        a = self.create()
        self.assertEqual(a.status_code, 201, a.text)
        self.assertEqual(a.json()["tag"], expected)
        b = self.create().json()["tag"]
        c = self.create().json()["tag"]
        self.assertEqual(len({a.json()["tag"], b, c}), 3)
        n = lambda t: int(t.removeprefix(document_numbers.get_series(self.db, self.company_id, "asset").prefix))  # noqa: E731
        self.assertEqual([n(b), n(c)], [n(a.json()["tag"]) + 1, n(a.json()["tag"]) + 2])
        listed = {x["tag"] for x in self.client.get("/api/asset-inventory?limit=500").json()}
        self.assertTrue({a.json()["tag"], b, c} <= listed)

    def test_admin_customised_format_is_used(self):
        r = self.client.patch("/api/settings/id-formats/asset",
                              json={"prefix": "IT-AST-", "suffix": "", "padding": 4, "next_number": 500,
                                    "include_year": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["next_code"], "IT-AST-0500")
        self.assertEqual(self.preview(), "IT-AST-0500")
        self.assertEqual(self.create().json()["tag"], "IT-AST-0500")
        self.assertEqual(self.create().json()["tag"], "IT-AST-0501")
        # a tag already in use is skipped, never duplicated
        self.assertEqual(self.create(tag="IT-AST-0502").status_code, 201)
        self.assertEqual(self.create().json()["tag"], "IT-AST-0503")

    def test_existing_assets_and_other_formats_unaffected(self):
        before_tags = dict(self.db.execute(select(models.AssetInventoryItem.id, models.AssetInventoryItem.asset_tag)
                                           .where(models.AssetInventoryItem.company_id == self.company_id)).all())
        before_series = {s.doctype: (s.prefix, s.suffix, s.padding, s.fiscal_infix, s.current_no)
                         for s in self.db.scalars(select(models.NumberSeries).where(
                             models.NumberSeries.company_id == self.company_id,
                             models.NumberSeries.doctype != "asset"))}
        self.client.patch("/api/settings/id-formats/asset", json={"prefix": "QA-", "padding": 3, "next_number": 1})
        self.create()
        self.create()
        after_tags = dict(self.db.execute(select(models.AssetInventoryItem.id, models.AssetInventoryItem.asset_tag)
                                          .where(models.AssetInventoryItem.id.in_(list(before_tags)))).all())
        self.assertEqual(after_tags, before_tags)
        self.db.expire_all()
        after_series = {s.doctype: (s.prefix, s.suffix, s.padding, s.fiscal_infix, s.current_no)
                        for s in self.db.scalars(select(models.NumberSeries).where(
                            models.NumberSeries.company_id == self.company_id,
                            models.NumberSeries.doctype != "asset"))}
        self.assertEqual(after_series, before_series)

    def test_given_tag_still_checked_for_duplicates(self):
        tag = self.create().json()["tag"]
        self.assertEqual(self.create(tag=tag).status_code, 409)


if __name__ == "__main__":
    unittest.main()
