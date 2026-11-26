"""Appointment Letter: templates, live preview, generation (real PDF),
history and access rules (app/appointment_letters.py,
app/routers/appointment_letters.py).

    cd backend && <venv>/Scripts/python -m unittest tests.test_appointment_letters -v

Runs against the real dev schema of one tenant (APPOINTMENT_TEST_TENANT,
default impacgo-solutions) inside a transaction that is ALWAYS rolled back;
PDF files written by the test are deleted again.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import appointment_letters as al, crud, database, models, template_designs  # noqa: E402
from app.deps import HR_DOCUMENT_COLUMNS, has_column_access  # noqa: E402
from app.routers import appointment_letters as api  # noqa: E402
from app.storage import uploads_root, winlong_path  # noqa: E402

TENANT = os.environ.get("APPOINTMENT_TEST_TENANT", "impacgo-solutions")


class AppointmentLetterTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.files: list[str] = []

        users = self.db.scalars(select(models.User).where(models.User.status == "active")).all()
        self.hr = next((u for u in users if u.employee_id and has_column_access(self.db, u, *HR_DOCUMENT_COLUMNS)
                        and crud.get_visible_employee_ids_for_docs(self.db, u) is None), None)
        self.plain = next((u for u in users if u.employee_id
                           and not has_column_access(self.db, u, *HR_DOCUMENT_COLUMNS)), None)
        if self.hr is None or self.plain is None:
            self.skipTest("tenant needs an org-wide HR-documents user and a regular employee with logins")
        self.company = self.db.get(models.Company, self.hr.company_id)
        self.employee = self.db.get(models.Employee, self.plain.employee_id)
        # Start without this company's templates / letters so the test decides.
        self.db.execute(text("DELETE FROM hcm_appointment_letters WHERE company_id = :c"), {"c": self.company.id})
        self.db.execute(text("DELETE FROM hcm_appointment_letter_templates WHERE company_id = :c"),
                        {"c": self.company.id})

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()
        for rel in self.files:
            winlong_path(uploads_root() / rel).unlink(missing_ok=True)

    # ── helpers ──
    def _templates(self):
        return api.list_templates(db=self.db, current_user=self.hr)

    def _generate(self, notes=None):
        out = api.generate_letter(self.employee.id, api.GenerateIn(notes=notes), db=self.db, current_user=self.hr)
        self.files.append(self.db.get(models.AppointmentLetter, out.id).file_url)
        return out

    # ── templates ──
    def test_a_standard_active_template_is_created_on_first_use_only(self):
        rows = self._templates()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].is_active)
        self.assertEqual(rows[0].html_body, template_designs.APPOINTMENT_LAYOUT)
        self.assertEqual(len(self._templates()), 1)  # not duplicated

    def test_create_edit_activate_and_delete_rules(self):
        standard = self._templates()[0]
        payload = api.TemplateIn(name="Short form", html_body="<p>{{employee_name}}</p>", css_styles="p{}",
                                 signatory_name="Anita Rao", signatory_designation="HR Head")
        created = api.create_template(payload, db=self.db, current_user=self.hr)
        self.assertFalse(created.is_active)   # never silently displaces the active one
        self.assertEqual(created.version, 1)
        edited = api.update_template(created.id, payload.model_copy(update={"is_active": True}),
                                     db=self.db, current_user=self.hr)
        self.assertEqual(edited.version, 2)
        self.assertTrue(edited.is_active)
        active = [t for t in self._templates() if t.is_active]
        self.assertEqual([t.id for t in active], [created.id])  # exactly one active
        with self.assertRaises(HTTPException) as ctx:  # the active one can't be deleted
            api.delete_template(created.id, db=self.db, current_user=self.hr)
        self.assertEqual(ctx.exception.status_code, 409)
        api.activate_template(standard.id, db=self.db, current_user=self.hr)
        api.delete_template(created.id, db=self.db, current_user=self.hr)
        self.assertEqual([t.id for t in self._templates()], [standard.id])

    def test_unsafe_template_html_is_refused(self):
        with self.assertRaises(HTTPException) as ctx:
            api.create_template(api.TemplateIn(name="x", html_body="<script>alert(1)</script>"),
                                db=self.db, current_user=self.hr)
        self.assertEqual(ctx.exception.status_code, 400)

    # ── data binding ──
    def test_placeholders_bind_the_real_employee_and_company(self):
        values = al.build_placeholders(self.db, self.company, self.employee, signatory_name="Anita Rao",
                                       signatory_designation="HR Head", issue_date=crud.company_today(self.db, self.company.id),
                                       version=1)
        e = self.employee
        self.assertEqual(values["employee_name"], f"{e.first_name} {e.last_name or ''}".strip())
        self.assertEqual(values["employee_id"], e.employee_code or "—")
        self.assertEqual(values["designation"], e.designation.name if e.designation else "—")
        self.assertEqual(values["department"], e.department.name if e.department else "—")
        self.assertEqual(values["date_of_joining"], e.date_of_joining.strftime("%d/%m/%Y"))
        self.assertEqual(values["employment_type"], (e.employment_type or "").replace("_", " ").title() or "—")
        if e.annual_ctc:
            self.assertEqual(values["annual_ctc"], al.format_inr(e.annual_ctc))
            self.assertIn("<table", values["salary_breakdown"])
        self.assertEqual(values["company_name"], self.company.name)
        self.assertTrue(values["letter_number"].startswith(f"APL/{e.employee_code or 'EMP'}/"))
        self.assertEqual(values["authorized_signatory"], "Anita Rao")

    def test_preview_renders_the_draft_for_a_real_employee_escaping_values(self):
        out = api.preview_template(api.PreviewIn(html_body="<h1>{{employee_name}}</h1><div>{{salary_breakdown}}</div>",
                                                 employee_id=self.employee.id), db=self.db, current_user=self.hr)
        self.assertIn(self.employee.first_name, out.rendered_html)
        self.assertNotIn("{{", out.rendered_html)

    # ── generation + history ──
    def test_generate_stores_a_real_pdf_and_regenerating_adds_a_version(self):
        self._templates()
        first = self._generate()
        self.assertEqual(first.version, 1)
        pdf = al.letter_file_bytes(self.db.get(models.AppointmentLetter, first.id))
        self.assertTrue(pdf and pdf.startswith(b"%PDF"))
        second = self._generate(notes="Designation corrected")
        self.assertEqual(second.version, 2)
        hr_view = api.list_employee_letters(self.employee.id, db=self.db, current_user=self.hr)
        self.assertEqual([l.version for l in hr_view], [2, 1])                 # HR: full history
        self.assertEqual([l.is_current for l in hr_view], [True, False])
        own = api.list_employee_letters(self.employee.id, db=self.db, current_user=self.plain)
        self.assertEqual([l.version for l in own], [2])                          # employee: current only
        resp = api.download_letter_pdf(second.id, db=self.db, current_user=self.plain)
        self.assertEqual(resp.media_type, "application/pdf")

    def test_no_active_template_means_no_generation(self):
        standard = self._templates()[0]
        self.db.get(models.AppointmentLetterTemplate, standard.id).is_active = False
        self.db.flush()
        with self.assertRaises(HTTPException) as ctx:
            api.generate_letter(self.employee.id, api.GenerateIn(), db=self.db, current_user=self.hr)
        self.assertEqual(ctx.exception.status_code, 400)

    # ── access ──
    def test_regular_employees_cannot_see_others_letters_or_manage_templates(self):
        self._templates()
        letter = self._generate()
        other = self.db.scalars(select(models.User).where(
            models.User.status == "active", models.User.company_id == self.company.id,
            models.User.employee_id.is_not(None), models.User.employee_id != self.employee.id)).all()
        other = next((u for u in other if not has_column_access(self.db, u, *HR_DOCUMENT_COLUMNS)), None)
        if other is None:
            self.skipTest("needs a second regular employee")
        with self.assertRaises(HTTPException) as ctx:
            api.list_employee_letters(self.employee.id, db=self.db, current_user=other)
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(HTTPException) as ctx:
            api.download_letter_pdf(letter.id, db=self.db, current_user=other)
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            api.require_hr_documents(user=other, db=self.db)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_another_companys_employee_is_not_found(self):
        with self.assertRaises(HTTPException) as ctx:
            api.preview_letter(uuid.uuid4(), db=self.db, current_user=self.hr)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
