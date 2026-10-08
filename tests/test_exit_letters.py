"""Experience & Relieving Letters end to end (template -> eligibility ->
generate -> regenerate -> history -> employee / HR access -> PDF).

    cd backend && venv/Scripts/python -m unittest tests.test_exit_letters -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant Infyq by default, EXIT_LETTER_TEST_TENANT to override);
generated PDFs go to a temporary folder. Nothing is left behind.
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import exit_letters as api  # noqa: E402

TENANT = os.environ.get("EXIT_LETTER_TEST_TENANT", "Infyq")
TEMPLATE_V1 = (
    "<h1>{{company_name}}</h1><p>{{letter_title}} {{letter_number}} dated {{issue_date}}</p>"
    "<p>This is to certify that {{employee_salutation}} {{employee_name}} ({{employee_id}}) worked as "
    "{{designation}} from {{date_of_joining_long}} to {{last_working_day_long}} "
    "({{tenure}}). Relieved on {{relieving_date}}.</p><p>{{authorized_signatory}}, "
    "{{authorized_signatory_designation}}</p>"
)


class ExitLetterTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.tmp = tempfile.TemporaryDirectory()
        mock.patch("app.storage.uploads_root", lambda: Path(self.tmp.name)).start()
        self.company = self.db.scalars(select(models.Company)).first()
        self.hr = self._user(lambda role, u: role.name == _OWNER_ROLE_NAME)
        # Start from "no template saved yet" (tenants now get default exit
        # letter templates seeded) -- removed only inside this transaction.
        for tpl in self.db.scalars(select(models.ExitLetterHtmlTemplate).where(
                models.ExitLetterHtmlTemplate.company_id == self.company.id)).all():
            self.db.delete(tpl)
        self.db.flush()
        exits =self.db.scalars(select(models.ExitRequestModel)).all()
        self.completed = next((e for e in exits if e.status == "completed" and self._emp_ok(e)), None)
        self.not_eligible = next((e for e in exits if e.status in ("submitted", "sent_back", "pending", "in_clearance")), None)
        if self.completed is None:
            # N-10: a new tenant has no finished exit -- make one (rolled back).
            self.completed = self._make_exit("completed", datetime.date.today() - datetime.timedelta(days=3))
        if self.not_eligible is None:
            self.not_eligible = self._make_exit("submitted", datetime.date.today() + datetime.timedelta(days=30))

    def _make_exit(self, status, last_working_day):
        used = set(self.db.scalars(select(models.ExitRequestModel.employee_id)).all())
        hr_emp = self.hr.employee_id if self.hr is not None else None
        emp = next((e for e in self.db.scalars(
            select(models.Employee).where(models.Employee.company_id == self.company.id)
            .order_by(models.Employee.employee_code)).all()
            if e.id not in used and e.id != hr_emp and e.date_of_joining is not None), None)
        if emp is None:
            self.skipTest("tenant has no employee to exit")
        now = datetime.datetime.now(datetime.timezone.utc)
        row = models.ExitRequestModel(
            id=uuid.uuid4(), employee_id=emp.id, status=status, last_working_day=last_working_day,
            resignation_date=last_working_day - datetime.timedelta(days=30), reason="Test fixture",
            approver_id=hr_emp if status == "completed" else None,
            decided_at=now if status == "completed" else None, created_at=now, updated_at=now,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()
        self.tmp.cleanup()

    def _emp_ok(self, e):
        emp = self.db.get(models.Employee, e.employee_id)
        return emp is not None and emp.company_id == self.company.id

    def _is_hr(self, user):
        from app.deps import HR_DOCUMENT_COLUMNS, has_column_access
        return has_column_access(self.db, user, *HR_DOCUMENT_COLUMNS)

    def _user(self, pred):
        for u in self.db.scalars(select(models.User)).all():
            role = crud.get_user_primary_role(self.db, u.id)
            if role is not None and pred(role, u):
                return u
        return None

    def _save_template(self, letter_type="experience_relieving", html=TEMPLATE_V1, signatory="Priya Sharma"):
        return api.save_exit_letter_template(
            schemas.ExitLetterTemplateUpdate(
                name="Standard Experience & Relieving Letter", html_body=html, css_styles="h1{color:#123}",
                signatory_name=signatory, signatory_designation="HR Manager",
            ),
            letter_type=letter_type, db=self.db, current_user=self.hr,
        )

    def test_full_flow_with_immutable_history(self):
        t1 = self._save_template()
        self.assertEqual(t1.version, 1)
        emp = self.db.get(models.Employee, self.completed.employee_id)

        # Live Preview of the draft against the real employee.
        preview = api.preview_exit_letter_template(
            schemas.ExitLetterTemplatePreviewRequest(
                html_body=TEMPLATE_V1, exit_request_id=self.completed.id,
                signatory_name="Priya Sharma", signatory_designation="HR Manager",
            ),
            letter_type="experience_relieving", db=self.db, current_user=self.hr,
        ).rendered_html
        self.assertIn(emp.first_name, preview)
        self.assertIn(emp.employee_code, preview)
        self.assertIn(self.company.name, preview)
        self.assertIn(self.completed.last_working_day.strftime("%B %Y"), preview)
        self.assertNotIn("{{", preview)

        summary = api.get_exit_request_letters(self.completed.id, db=self.db, current_user=self.hr)
        self.assertTrue(summary.eligible, summary.eligibility_reason)

        v1 = api.generate_exit_letter(self.completed.id, schemas.ExitLetterGenerateRequest(notes="first"),
                                      letter_type="experience_relieving", db=self.db, current_user=self.hr)
        self.assertEqual((v1.version, v1.template_version, v1.is_current), (1, 1, True))
        self.assertTrue(v1.letter_number.startswith(f"EXR/{emp.employee_code}/"))
        row1 = self.db.get(models.ExitLetter, v1.id)
        pdf1 = crud.exit_letter_file_bytes(row1)
        self.assertTrue(pdf1.startswith(b"%PDF"), "a real PDF is stored")
        html1 = row1.rendered_html
        self.assertIn("Priya Sharma", html1)

        # Edit the template, regenerate -> new version; v1 untouched.
        t2 = self._save_template(html=TEMPLATE_V1.replace("certify", "CONFIRM"), signatory="New Signatory")
        self.assertEqual(t2.version, 2)
        v2 = api.generate_exit_letter(self.completed.id, schemas.ExitLetterGenerateRequest(),
                                      letter_type="experience_relieving", db=self.db, current_user=self.hr)
        self.assertEqual((v2.version, v2.template_version), (2, 2))
        self.db.refresh(row1)
        self.assertFalse(row1.is_current)
        self.assertEqual(row1.rendered_html, html1, "earlier letter must never change")
        self.assertEqual(crud.exit_letter_file_bytes(row1), pdf1)
        self.assertIn("CONFIRM", self.db.get(models.ExitLetter, v2.id).rendered_html)

        # PDF download serves the stored file.
        resp = api.download_exit_letter_pdf(v1.id, db=self.db, current_user=self.hr)
        self.assertEqual(resp.body, pdf1)

        # HR sees the full history; the employee sees only their current letter.
        hr_list = api.list_employee_exit_letters(emp.id, db=self.db, current_user=self.hr)
        self.assertEqual([l.version for l in hr_list], [2, 1])
        own = self._user(lambda role, u: u.employee_id == emp.id)
        if own is not None:
            mine = api.list_employee_exit_letters(emp.id, db=self.db, current_user=own)
            self.assertEqual([(l.version, l.is_current) for l in mine], [(2, True)])
            self.assertEqual(api.download_exit_letter_pdf(v2.id, db=self.db, current_user=own).body[:4], b"%PDF")

        # Another self-service employee cannot see or download them.
        other = self._user(lambda role, u: u.employee_id not in (None, emp.id) and not self._is_hr(u))
        self.assertIsNotNone(other, "need a non-HR employee to test access control")
        if other is not None:
            with self.assertRaises(HTTPException) as ctx:
                api.list_employee_exit_letters(emp.id, db=self.db, current_user=other)
            self.assertEqual(ctx.exception.status_code, 403)
            with self.assertRaises(HTTPException) as ctx:
                api.download_exit_letter_pdf(v2.id, db=self.db, current_user=other)
            self.assertEqual(ctx.exception.status_code, 404)

    def test_standard_layout_renders_every_placeholder_and_blocks_ineligible(self):
        """The editor's standard layout (lib/models/exit_letter_models.dart)
        rendered with real employee data: every placeholder resolved, real PDF."""
        import re
        dart = (Path(__file__).resolve().parents[2] / "lib" / "models" / "exit_letter_models.dart").read_text(encoding="utf-8")
        html = re.search(r"kExitLetterStarterHtml = r'''(.*?)''';", dart, re.S).group(1)
        css = re.search(r"kExitLetterStarterCss = r'''(.*?)''';", dart, re.S).group(1)
        api.save_exit_letter_template(
            schemas.ExitLetterTemplateUpdate(name="Standard Experience & Relieving Letter", html_body=html,
                                             css_styles=css, signatory_name="Authorised Signatory",
                                             signatory_designation="Head - HR"),
            letter_type="experience_relieving", db=self.db, current_user=self.hr,
        )
        emp = self.db.get(models.Employee, self.completed.employee_id)
        preview = api.preview_exit_letter(self.completed.id, letter_type="experience_relieving",
                                          db=self.db, current_user=self.hr).rendered_html
        self.assertNotIn("{{", preview)
        for absent in ("Department", "Employment Type", "{{department}}", "{{employment_type}}"):
            self.assertNotIn(absent, html)
        for value in (emp.employee_code, emp.first_name, "Experience &amp; Relieving Letter", "Head - HR",
                      self.completed.last_working_day.strftime("%B %Y")):
            self.assertIn(value, preview)
        r = api.generate_exit_letter(self.completed.id, schemas.ExitLetterGenerateRequest(),
                                     letter_type="experience_relieving", db=self.db, current_user=self.hr)
        self.assertTrue(r.letter_number.startswith("EXR/"))
        self.assertEqual(r.letter_title, "Experience & Relieving Letter")
        self.assertTrue(crud.exit_letter_file_bytes(self.db.get(models.ExitLetter, r.id)).startswith(b"%PDF"))
        if self.not_eligible is not None:
            with self.assertRaises(HTTPException) as ctx:
                api.generate_exit_letter(self.not_eligible.id, schemas.ExitLetterGenerateRequest(),
                                         letter_type="experience_relieving", db=self.db, current_user=self.hr)
            self.assertEqual(ctx.exception.status_code, 409)

    def test_company_logo_upload_prints_on_letter(self):
        """Organization > Company Profile > Company Logo: validated upload,
        served only to the same company, embedded in the letter, removable."""
        import base64
        import io
        from fastapi import UploadFile
        from app import media_access
        from app.routers import organization as org_api
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        with self.assertRaises(HTTPException) as ctx:  # not really an image
            org_api.upload_company_logo(UploadFile(io.BytesIO(b"<svg onload=x>"), filename="logo.png"),
                                        db=self.db, current_user=self.hr)
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            org_api.upload_company_logo(UploadFile(io.BytesIO(png), filename="logo.svg"),
                                        db=self.db, current_user=self.hr)
        out = org_api.upload_company_logo(UploadFile(io.BytesIO(png), filename="logo.png"),
                                          db=self.db, current_user=self.hr)
        self.assertTrue(out.logo_url.startswith(f"/media/company_logo/{self.hr.company_id}/"))
        rel = out.logo_url.removeprefix("/media/")
        self.assertIsNone(media_access.authorize(self.db, self.hr, rel))
        self.assertTrue((Path(self.tmp.name) / rel).exists())

        self._save_template(html="<header><img src=\"{{company_logo}}\"></header>" + TEMPLATE_V1)
        preview = api.preview_exit_letter(self.completed.id, letter_type="experience_relieving",
                                          db=self.db, current_user=self.hr).rendered_html
        self.assertIn("data:image/png;base64," + base64.b64encode(png).decode(), preview)

        out = org_api.delete_company_logo(db=self.db, current_user=self.hr)
        self.assertIsNone(out.logo_url)
        self.assertFalse((Path(self.tmp.name) / rel).exists())

    def test_seal_and_signature_images(self):
        """Documents > Templates: company seal + authorized signature images --
        validated, never served by /media, printed on generated letters,
        previewable before saving, removable."""
        import base64
        import io
        from fastapi import UploadFile
        from app import media_access
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        data_url = "data:image/png;base64," + base64.b64encode(png).decode()

        def upload(kind, content=png, name="img.png"):
            return api.upload_exit_letter_image(UploadFile(io.BytesIO(content), filename=name),
                                                letter_type="experience_relieving", kind=kind,
                                                db=self.db, current_user=self.hr)

        if crud.get_exit_letter_template(self.db, self.hr.company_id, "experience_relieving") is None:
            with self.assertRaises(HTTPException) as ctx:  # template must be saved first
                upload("seal")
            self.assertEqual(ctx.exception.status_code, 400)
        html = ("<img src=\"{{authorized_signature}}\"><img src=\"{{company_seal}}\">" + TEMPLATE_V1)
        t = self._save_template(html=html)
        with self.assertRaises(HTTPException):
            upload("seal", content=b"<svg onload=alert(1)>", name="seal.png")
        out = upload("signature")
        self.assertEqual(out.signature_image_data_url, data_url)
        self.assertIsNone(out.seal_image_data_url)
        self.assertEqual(out.version, t.version + 1)
        out = upload("seal")
        row = crud.get_exit_letter_template(self.db, self.hr.company_id, "experience_relieving")
        rel = row.seal_image_path.removeprefix("/media/")
        self.assertEqual(media_access.authorize(self.db, self.hr, rel), media_access.NOT_FOUND)

        letter = api.generate_exit_letter(self.completed.id, schemas.ExitLetterGenerateRequest(),
                                          letter_type="experience_relieving", db=self.db, current_user=self.hr)
        stored = self.db.get(models.ExitLetter, letter.id)
        self.assertEqual(stored.rendered_html.count(data_url), 2)
        self.assertNotIn("company_seal", stored.placeholders)
        self.assertNotIn("authorized_signature", stored.placeholders)

        draft = api.preview_exit_letter_template(
            schemas.ExitLetterTemplatePreviewRequest(html_body=html, exit_request_id=self.completed.id,
                                                     seal_image_data_url=""),
            letter_type="experience_relieving", db=self.db, current_user=self.hr).rendered_html
        self.assertEqual(draft.count(data_url), 1)  # signature kept, seal removed in the draft
        with self.assertRaises(HTTPException):
            api.preview_exit_letter_template(
                schemas.ExitLetterTemplatePreviewRequest(
                    html_body=html, exit_request_id=self.completed.id,
                    seal_image_data_url="data:image/png;base64," + base64.b64encode(b"<svg>").decode()),
                letter_type="experience_relieving", db=self.db, current_user=self.hr)

        seal_file = Path(self.tmp.name) / rel
        self.assertTrue(seal_file.exists())
        out = api.delete_exit_letter_image(letter_type="experience_relieving", kind="seal",
                                           db=self.db, current_user=self.hr)
        self.assertIsNone(out.seal_image_data_url)
        self.assertFalse(seal_file.exists())
        # Issued letters keep their stamped snapshot.
        self.assertEqual(self.db.get(models.ExitLetter, letter.id).rendered_html.count(data_url), 2)

    def test_single_combined_letter_type(self):
        """One combined Experience & Relieving Letter (no separate experience /
        relieving types) -- alongside the F&F Settlement Statement, the only
        types the API path accepts, the renderer knows and the DB allows."""
        import inspect
        from app import exit_letter_html_renderer as renderer
        self.assertEqual(crud.EXIT_LETTER_TYPES, ("experience_relieving", "fnf_statement"))
        self.assertEqual(renderer.LETTER_TYPES, {"experience_relieving": "Experience & Relieving Letter",
                                                 "fnf_statement": "Full & Final Settlement Statement"})
        self.assertIn('pattern="^(experience_relieving|fnf_statement)$"', inspect.getsource(api))
        with self.assertRaises(Exception):  # old separate types violate the DB check
            with self.db.begin_nested():
                self.db.add(models.ExitLetterHtmlTemplate(
                    id=__import__("uuid").uuid4(), company_id=self.company.id, letter_type="experience",
                    name="x", html_body="<p/>", css_styles="", is_active=True, version=1,
                ))
                self.db.flush()

    def test_no_active_template_blocks_generation(self):
        with self.assertRaises(HTTPException) as ctx:
            api.generate_exit_letter(self.completed.id, schemas.ExitLetterGenerateRequest(),
                                     letter_type="experience_relieving", db=self.db, current_user=self.hr)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_eligibility_rules(self):
        e = self.completed
        today = crud.company_today(self.db, self.company.id)
        saved = (e.status, e.last_working_day)
        try:
            e.status, e.last_working_day = "approved", today + datetime.timedelta(days=5)
            self.assertFalse(crud.exit_letter_eligibility(self.db, self.company.id, e)[0])
            e.last_working_day = today
            self.assertTrue(crud.exit_letter_eligibility(self.db, self.company.id, e)[0])
            e.status = "in_clearance"
            self.assertFalse(crud.exit_letter_eligibility(self.db, self.company.id, e)[0])
        finally:
            e.status, e.last_working_day = saved

    def test_templates_require_hr(self):
        from app.deps import require_hr_documents
        employee = self._user(lambda role, u: not self._is_hr(u))
        if employee is None:
            self.skipTest("no self-service user")
        with self.assertRaises(HTTPException) as ctx:
            require_hr_documents(employee, self.db)
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
