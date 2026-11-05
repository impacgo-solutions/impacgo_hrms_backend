"""Documents > Templates > Intern Offer Letter: intern offers are generated
from the active intern template, every other offer (and an intern offer
with no active intern template) keeps the standard Offer Letter. Runs
inside a transaction that is always rolled back.

    cd backend && venv/Scripts/python -m unittest tests.test_intern_offer_letter -v
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete, select  # noqa: E402

from app import crud, intern_offer_letter as iol, models, offer_letter_html_renderer as olr  # noqa: E402
from app.routers import recruitment as rec_api  # noqa: E402
from tests.test_employee_access_lifecycle import _Base  # noqa: E402


class InternOfferLetterTests(_Base):
    tenant = "acme"

    def setUp(self):
        super().setUp()
        self.offer = self.db.scalar(select(models.Offer).limit(1))
        if self.offer is None:
            self.skipTest(f"no offers in {self.tenant}")
        opening = self.db.get(models.JobOpening, self.offer.application.opening_id)
        self.company = self.db.get(models.Company, opening.company_id)
        self.db.execute(delete(iol.MODEL).where(iol.MODEL.company_id == self.company.id))
        self.db.flush()
        self.std = crud.get_offer_letter_html_template(self.db, self.company.id)
        if self.std is None:
            self.std = crud.create_offer_letter_html_template(
                self.db, self.company.id, name="Std", html_body="<p>STANDARD {{candidate_name}}</p>",
                css_styles="", is_active=True, created_by=None)
        self.offer.proposed_joining_date = datetime.date(2026, 6, 1)
        self.offer.contract_duration_months = 1
        self.offer.contract_end_date = None
        self.offer.offer_date = datetime.date(2026, 5, 11)
        self.db.flush()

    def _pdf_html(self, employment_type: str) -> str:
        self.offer.employment_type = employment_type
        self.db.flush()
        detail = crud.get_offer_detail(self.db, self.offer.id, self.company.id)
        with mock.patch.object(olr, "render_html_to_pdf", side_effect=lambda html, *a, **k: html.encode()) as pdf:
            rec_api.offer_letter_pdf(self.db, self.company, detail)
        return pdf.call_args[0][0]

    def test_default_seeded_once_and_active(self):
        iol.ensure_default_template(self.db, self.company.id)
        iol.ensure_default_template(self.db, self.company.id)
        rows = iol.list_templates(self.db, self.company.id)
        self.assertEqual([(r.name, r.is_active) for r in rows], [(iol.DEFAULT_NAME, True)])

    def test_intern_offer_uses_intern_template_others_unchanged(self):
        # No intern template yet -> the standard letter, as before.
        standard_intern = self._pdf_html("intern")
        self.assertNotIn("internship offer letter", standard_intern.lower())

        iol.ensure_default_template(self.db, self.company.id)
        html = self._pdf_html("intern")
        self.assertIn("internship offer letter", html.lower())
        self.assertIn("1 June 2026", html)
        self.assertIn("30 June 2026", html)
        self.assertIn("one-month", html)
        self.assertIn("11th May 2026", html)
        self.assertNotIn("{{", html)
        self.assertNotIn(">nan<", html.lower())

        for other in ("full_time", "contract", "part_time"):
            self.assertNotIn("internship offer letter", self._pdf_html(other).lower(), other)

    def test_period_and_crud(self):
        o = self.offer
        o.contract_duration_months = None
        o.contract_end_date = datetime.date(2026, 7, 15)
        self.assertEqual(iol.internship_period(o)[2], "45-day")
        o.contract_end_date = datetime.date(2026, 8, 31)
        self.assertEqual(iol.internship_period(o)[2], "three-month")

        a = iol.create(self.db, self.company.id, name="A", html_body="<p>A</p>", css_styles="",
                       is_active=True, created_by=None)
        b = iol.create(self.db, self.company.id, name="B", html_body="<p>B</p>", css_styles="",
                       is_active=False, created_by=None)
        iol.activate(self.db, self.company.id, b.id)
        self.assertEqual(iol.get_active(self.db, self.company.id).id, b.id)
        with self.assertRaises(ValueError):
            iol.delete(self.db, self.company.id, b.id)  # the active one is kept
        iol.delete(self.db, self.company.id, a.id)
        self.assertEqual([r.id for r in iol.list_templates(self.db, self.company.id)], [b.id])
        # Seeding never touches a company that already has intern templates.
        iol.ensure_default_template(self.db, self.company.id)
        self.assertEqual(len(iol.list_templates(self.db, self.company.id)), 1)


if __name__ == "__main__":
    unittest.main()
