"""Documents > Templates > Designs: every style renders every letter type
through the real pipeline with no leftover placeholders, the logo slot
collapses for a company without a logo, and the letter types' defaults use
the shared logo letterhead. Rolled back.

    cd backend && venv/Scripts/python -m unittest tests.test_template_designs -v
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import agreements, intern_offer_letter as iol, models, template_designs as td  # noqa: E402
from app import template_rendering as tr  # noqa: E402
from app.routers import template_designs as td_api  # noqa: E402
from tests.test_employee_access_lifecycle import _Base  # noqa: E402

_LEFTOVER = re.compile(r"\{\{\s*[a-z_]+\s*\}\}")


class TemplateDesignTests(_Base):
    tenant = "acme"

    def setUp(self):
        super().setUp()
        self.user = self.db.scalar(select(models.User).where(
            models.User.status == "active", models.User.employee_id.is_not(None)).limit(1))
        if self.user is None:
            self.skipTest("no user")
        self.company = self.db.get(models.Company, self.user.company_id)

    def test_every_style_renders_every_letter_type(self):
        for doc_type in td.DOC_TYPES:
            d = td_api.get_template_designs(doc_type, db=self.db, current_user=self.user)
            self.assertEqual(len(d["themes"]), 5)
            self.assertIn('class="lh"', d["layout_html"])
            self.assertIn("{{company_logo}}", d["layout_html"])
            self.assertIsNone(tr.validate_template_html(d["layout_html"], ""), doc_type)
            for t in d["themes"]:
                self.assertIsNone(tr.validate_template_html(d["layout_html"], t["css"]), (doc_type, t["key"]))
                self.assertIn("@page", t["css"])
                html = tr.render_template_html(d["layout_html"], t["css"], d["sample"],
                                               verbatim_keys=tuple(d["verbatim_keys"]))
                self.assertIsNone(_LEFTOVER.search(html), (doc_type, t["key"], _LEFTOVER.search(html)))
        self.assertEqual(self.code(td_api.get_template_designs, "payslip", db=self.db, current_user=self.user), 404)

    def code(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code

    def test_logo_state_follows_the_company_logo(self):
        saved = self.company.logo_url
        self.company.logo_url = None
        self.assertEqual(td.logo_values(self.company)["logo_state"], "no-logo")
        self.assertEqual(td.logo_values(self.company)["company_logo"], tr.TRANSPARENT_PIXEL_DATA_URL)
        self.company.logo_url = "https://example.com/logo.png"
        self.assertEqual(td.logo_values(self.company)["logo_state"], "with-logo")
        self.company.logo_url = saved
        for t in td.THEMES:
            self.assertIn(".no-logo .lh-logo { display: none; }", t["css"])

    def test_defaults_use_the_logo_letterhead_and_legacy_rows_upgrade(self):
        self.assertIn("{{company_logo}}", iol.DEFAULT_HTML)
        for b in agreements.BUILTIN_TEMPLATES:
            self.assertIn("{{company_logo}}", b["html"])
            self.assertIn("{{logo_state}}", b["html"])
        # An intern template still equal to the first predefined design is
        # upgraded; an edited one is left alone.
        self.db.query(iol.MODEL).filter(iol.MODEL.company_id == self.company.id).delete()
        legacy = iol.create(self.db, self.company.id, name="Old", html_body=iol._LEGACY_HTML,
                            css_styles=iol._LEGACY_CSS, is_active=True, created_by=None)
        edited = iol.create(self.db, self.company.id, name="Mine", html_body="<p>mine</p>", css_styles="p{}",
                            is_active=False, created_by=None)
        iol.ensure_default_template(self.db, self.company.id)
        self.assertEqual((legacy.html_body, legacy.css_styles), (iol.DEFAULT_HTML, iol.DEFAULT_CSS))
        self.assertEqual(edited.html_body, "<p>mine</p>")


if __name__ == "__main__":
    unittest.main()
