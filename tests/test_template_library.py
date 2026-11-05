"""Multi-template Documents & Email Templates (Documents > Templates):
several named HTML+CSS templates per document/email type, exactly one
ACTIVE at a time, the active one picked up automatically by real PDF /
email generation -- with zero behavior change for a company that only
ever had its original single template. Also covers the new Promotion
Announcement congratulations email (People > Employee Profile > Edit
Professional Info -> a real designation change).

    cd backend && venv/Scripts/python -m unittest tests.test_template_library -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant Infyq by default, TEMPLATE_LIBRARY_TEST_TENANT to
override). Nothing is left behind.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas  # noqa: E402
from app import email_service as es  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import payroll, recruitment, exit_letters, employees  # noqa: E402

TENANT = os.environ.get("TEMPLATE_LIBRARY_TEST_TENANT", "Infyq")

PAYSLIP_HTML = "<div class='payslip'>{{employee_name}} net {{net_salary}}</div>"
OFFER_HTML = "<div>{{candidate_name}} offered {{designation}}</div>"
EXIT_HTML = "<div>{{employee_name}} relieved {{relieving_date}}</div>"
EMAIL_HTML = "<p>Hi {{employee_name}}, your {{leave_type}} leave was approved by {{approved_by}}.</p>"


class TemplateLibraryTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.company = self.db.scalars(select(models.Company)).first()
        self.hr = self._user(lambda role, u: role.name == _OWNER_ROLE_NAME)
        if self.hr is None:
            self.skipTest("no Organization Owner user in this tenant")

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def _user(self, pred):
        for u in self.db.scalars(select(models.User)).all():
            role = crud.get_user_primary_role(self.db, u.id)
            if role is not None and pred(role, u):
                return u
        return None

    def _assert_exactly_one_active(self, rows) -> models.Base:
        active = [r for r in rows if r.is_active]
        self.assertEqual(len(active), 1, f"expected exactly one active row, got {len(active)}")
        return active[0]

    # ── Payslip ──────────────────────────────────────────────────────────

    def test_payslip_multi_template_lifecycle(self):
        before = payroll.list_payslip_html_templates(db=self.db, current_user=self.hr)
        had_active_before = any(r.is_active for r in before)

        t1 = payroll.create_payslip_html_template(
            schemas.PayslipHtmlTemplateUpdate(name="Classic", html_body=PAYSLIP_HTML, css_styles="", is_active=True),
            db=self.db, current_user=self.hr,
        )
        self.assertTrue(t1.is_active, "creating with is_active=True must not violate the one-active-row index")

        t2 = payroll.create_payslip_html_template(
            schemas.PayslipHtmlTemplateUpdate(name="Modern", html_body=PAYSLIP_HTML, css_styles="h1{color:red}",
                                              is_active=False),
            db=self.db, current_user=self.hr,
        )
        self.assertFalse(t2.is_active)

        rows = payroll.list_payslip_html_templates(db=self.db, current_user=self.hr)
        self.assertGreaterEqual(len(rows), 2 if not had_active_before else 3)
        active = self._assert_exactly_one_active(rows)
        self.assertEqual(active.id, t1.id)

        active_now = crud.get_payslip_html_template(self.db, self.company.id)
        self.assertEqual(active_now.id, t1.id)

        payroll.activate_payslip_html_template(t2.id, db=self.db, current_user=self.hr)
        rows = payroll.list_payslip_html_templates(db=self.db, current_user=self.hr)
        self._assert_exactly_one_active(rows)
        active_now = crud.get_payslip_html_template(self.db, self.company.id)
        self.assertEqual(active_now.id, t2.id)

        with self.assertRaises(HTTPException) as ctx:
            payroll.delete_payslip_html_template(t2.id, db=self.db, current_user=self.hr)
        self.assertEqual(ctx.exception.status_code, 409)

        payroll.delete_payslip_html_template(t1.id, db=self.db, current_user=self.hr)
        rows = payroll.list_payslip_html_templates(db=self.db, current_user=self.hr)
        self.assertFalse(any(r.id == t1.id for r in rows))

    # ── Offer letter ─────────────────────────────────────────────────────

    def test_offer_letter_multi_template_lifecycle(self):
        t1 = recruitment.create_offer_letter_html_template(
            schemas.OfferLetterHtmlTemplateUpdate(name="Standard Offer", html_body=OFFER_HTML, css_styles="",
                                                   is_active=True),
            db=self.db, current_user=self.hr,
        )
        self.assertTrue(t1.is_active)

        t2 = recruitment.create_offer_letter_html_template(
            schemas.OfferLetterHtmlTemplateUpdate(name="Executive Offer", html_body=OFFER_HTML, css_styles="",
                                                   is_active=True),
            db=self.db, current_user=self.hr,
        )
        self.assertTrue(t2.is_active, "second create with is_active=True must flip over, not conflict")

        rows = recruitment.list_offer_letter_html_templates(db=self.db, current_user=self.hr)
        active = self._assert_exactly_one_active(rows)
        self.assertEqual(active.id, t2.id, "activating a new one deactivates the previous active row")

        active_now = crud.get_offer_letter_html_template(self.db, self.company.id)
        self.assertEqual(active_now.id, t2.id)

        with self.assertRaises(HTTPException) as ctx:
            recruitment.delete_offer_letter_html_template(t2.id, db=self.db, current_user=self.hr)
        self.assertEqual(ctx.exception.status_code, 409)

        recruitment.activate_offer_letter_html_template(t1.id, db=self.db, current_user=self.hr)
        active_now = crud.get_offer_letter_html_template(self.db, self.company.id)
        self.assertEqual(active_now.id, t1.id)

    # ── Exit letter (keyed by letter_type) ───────────────────────────────

    def test_exit_letter_multi_template_lifecycle_per_letter_type(self):
        letter_type = "experience_relieving"
        t1 = exit_letters.create_exit_letter_template(
            schemas.ExitLetterTemplateUpdate(name="Standard Relieving", html_body=EXIT_HTML, css_styles="",
                                             is_active=True, signatory_name="Priya Sharma",
                                             signatory_designation="HR Manager"),
            letter_type=letter_type, db=self.db, current_user=self.hr,
        )
        self.assertTrue(t1.is_active)

        t2 = exit_letters.create_exit_letter_template(
            schemas.ExitLetterTemplateUpdate(name="Alternate Relieving", html_body=EXIT_HTML, css_styles="",
                                             is_active=True, signatory_name="Priya Sharma",
                                             signatory_designation="HR Manager"),
            letter_type=letter_type, db=self.db, current_user=self.hr,
        )
        self.assertTrue(t2.is_active)

        rows = exit_letters.list_exit_letter_templates(letter_type=letter_type, db=self.db, current_user=self.hr)
        active = self._assert_exactly_one_active(rows)
        self.assertEqual(active.id, t2.id)

        active_now = crud.get_exit_letter_template(self.db, self.company.id, letter_type)
        self.assertEqual(active_now.id, t2.id)

        other_type = "fnf_statement"
        other = exit_letters.create_exit_letter_template(
            schemas.ExitLetterTemplateUpdate(name="FnF Statement", html_body=EXIT_HTML, css_styles="",
                                             is_active=True, signatory_name="Priya Sharma",
                                             signatory_designation="HR Manager"),
            letter_type=other_type, db=self.db, current_user=self.hr,
        )
        self.assertTrue(other.is_active)
        still_active = crud.get_exit_letter_template(self.db, self.company.id, letter_type)
        self.assertEqual(still_active.id, t2.id, "activating a row for one letter_type must not touch the other")

        with self.assertRaises(HTTPException) as ctx:
            exit_letters.delete_exit_letter_template(t2.id, letter_type=letter_type, db=self.db, current_user=self.hr)
        self.assertEqual(ctx.exception.status_code, 409)

    # ── Email templates ──────────────────────────────────────────────────

    def test_email_template_custom_overrides_static_file_until_removed(self):
        kind = "leave_approved"
        baseline = es.render_email(
            kind, {"employee_name": "Asha Rao", "leave_type": "Earned Leave", "approved_by": "Priya Sharma"},
            db=self.db, company_id=self.company.id,
        )
        self.assertNotIn("your Earned Leave leave was approved by Priya Sharma", baseline)

        row = crud.create_email_template(
            self.db, self.company.id, kind, name="Friendly Approval", html_body=EMAIL_HTML, css_styles="",
            is_active=True, created_by=self.hr.employee_id,
        )
        self.db.flush()
        self.assertTrue(row.is_active)

        customized = es.render_email(
            kind, {"employee_name": "Asha Rao", "leave_type": "Earned Leave", "approved_by": "Priya Sharma"},
            db=self.db, company_id=self.company.id,
        )
        self.assertIn("Hi Asha Rao, your Earned Leave leave was approved by Priya Sharma.", customized)

        row2 = crud.create_email_template(
            self.db, self.company.id, kind, name="Formal Approval",
            html_body="<p>Dear {{employee_name}}, approved.</p>", css_styles="", is_active=True,
            created_by=self.hr.employee_id,
        )
        self.db.flush()
        self.assertTrue(row2.is_active)
        self.db.refresh(row)
        self.assertFalse(row.is_active, "activating the new template deactivates the previous one")

        rows = crud.list_email_templates(self.db, self.company.id, kind)
        active = self._assert_exactly_one_active(rows)
        self.assertEqual(active.id, row2.id)

        crud.delete_email_template(self.db, self.company.id, row.id)
        with self.assertRaises(ValueError):
            crud.delete_email_template(self.db, self.company.id, row2.id)
        crud.activate_email_template(self.db, self.company.id, row2.id)

    def test_email_kinds_catalog_matches_render_email(self):
        from app.routers import email_templates as et
        self.assertEqual(set(et.EMAIL_KIND_TOKENS.keys()), set(es.EMAIL_KINDS))
        self.assertEqual(set(et._SAMPLE_CONTEXT.keys()), set(es.EMAIL_KINDS))
        self.assertIn("leave_withdrawal", es.EMAIL_KINDS)
        self.assertIn("promotion_announcement", es.EMAIL_KINDS)
        for kind in ("birthday", "work_anniversary", "candidate_rejected", "candidate_next_step",
                     "candidate_interview_scheduled"):
            self.assertIn(kind, es.EMAIL_KINDS)
        self.assertNotIn("celebration", es.EMAIL_KINDS)
        for kind in es.EMAIL_KINDS:
            self.assertIn("company_name", et.EMAIL_KIND_TOKENS[kind])

    def test_every_kind_has_a_company_branded_default(self):
        """A brand-new tenant with no custom templates still gets a complete,
        professional email for every kind, branded with its own name."""
        from app.routers import email_templates as et
        for kind in es.EMAIL_KINDS:
            default = es._template(kind)
            self.assertIn('data-tpl="default"', default, kind)
            html_out = es.render_email(kind, et._SAMPLE_CONTEXT[kind], db=self.db, company_id=self.company.id)
            self.assertNotIn("{{", html_out, f"{kind}: unfilled placeholder")
            self.assertIn(self.company.name, html_out, f"{kind}: company name missing")
        self.assertNotEqual(es._template("birthday"), es._template("work_anniversary"))

    # ── Promotion congratulations email (new) ───────────────────────────

    def test_promotion_triggers_congratulations_email(self):
        """People > Employee Profile > Edit Professional Info: changing an
        employee's designation to a genuinely different one must queue a
        Promotion Announcement email -- a no-op save (same designation, or
        no designation_name at all) must NOT queue one."""
        employee = self.db.scalars(
            select(models.Employee).where(
                models.Employee.company_id == self.company.id,
                models.Employee.designation_id.isnot(None),
            )
        ).first()
        if employee is None:
            self.skipTest("no employee with a designation in this tenant")
        current_desig = self.db.get(models.Designation, employee.designation_id)
        other_desig = self.db.scalars(
            select(models.Designation).where(models.Designation.id != employee.designation_id)
        ).first()
        if other_desig is None:
            self.skipTest("no second designation available to promote into")

        before = self.db.scalars(
            select(models.EmailLog).where(models.EmailLog.related_entity_type == "employee_promotion")
        ).all()

        from app.routers.employees import update_employee_org
        update_employee_org(
            employee.id,
            schemas.EmployeeOrgUpdate(designation_name=other_desig.name),
            db=self.db, current_user=self.hr,
        )

        after = self.db.scalars(
            select(models.EmailLog).where(models.EmailLog.related_entity_type == "employee_promotion")
        ).all()
        self.assertEqual(len(after), len(before) + 1, "a real designation change must queue exactly one promotion email")
        new_log = [r for r in after if r.id not in {b.id for b in before}][0]
        self.assertEqual(new_log.email_type, es.PROMOTION)
        self.assertIn("Congratulations", new_log.subject)

        # No-op save (same designation again): must NOT queue a second email.
        update_employee_org(
            employee.id,
            schemas.EmployeeOrgUpdate(designation_name=other_desig.name),
            db=self.db, current_user=self.hr,
        )
        after2 = self.db.scalars(
            select(models.EmailLog).where(models.EmailLog.related_entity_type == "employee_promotion")
        ).all()
        self.assertEqual(len(after2), len(after), "re-saving the same designation must not queue another email")


if __name__ == "__main__":
    unittest.main()
