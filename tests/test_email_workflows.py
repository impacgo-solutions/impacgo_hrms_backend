"""Email workflows added on top of the Microsoft Graph integration:
"on behalf of" sending + fallback, the recipient-domain allowlist, the
permission diagnostics, holiday / birthday reminders (scheduler pass),
Full & Final Settlement notifications, documents emailed on generation and
bulk payslip emails.

    cd backend && venv/Scripts/python -m unittest tests.test_email_workflows -v

Nothing reaches Microsoft: the Graph HTTP layer is faked, the background
sender is replaced by a recorder, and every database change runs inside a
transaction that is ALWAYS rolled back (tenant Infyq by default,
EMAIL_TEST_TENANT to override). Generated PDFs go to a temporary folder.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import sys
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, email_service, graph_mail, models, reminders, schemas  # noqa: E402
from app.config import settings  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402

TENANT = os.environ.get("EMAIL_TEST_TENANT", "Infyq")
SECRET = "unit-test-secret-value"


def _token(roles):
    claims = base64.urlsafe_b64encode(json.dumps({"tid": "t", "roles": roles}).encode()).decode().rstrip("=")
    return f"h.{claims}.s"


class _Settings(unittest.TestCase):
    """Graph configured with fake credentials; nothing is really sent."""

    overrides = {
        "microsoft_tenant_id": "t", "microsoft_client_id": "c", "microsoft_client_secret": SECRET,
        "microsoft_from_email": "info@impacgo.com", "microsoft_on_behalf_of": "",
        "microsoft_on_behalf_of_name": "", "email_enabled": True, "email_allowed_domains": "*",
        "microsoft_graph_max_retries": 0, "email_global_fallback_tenants": "*",
    }

    def setUp(self):
        for key, value in self.overrides.items():
            mock.patch.object(settings, key, value).start()
        from app.tenant_email import store
        mock.patch.object(store, "CONFIG_TTL_SECONDS", 0).start()  # settings change per test: no cached config
        store.invalidate()
        mock.patch("app.graph_mail.time.sleep", lambda s: None).start()
        graph_mail.invalidate_token()

    def tearDown(self):
        mock.patch.stopall()
        graph_mail.invalidate_token()


class OnBehalfTests(_Settings):
    def test_payload_sends_on_behalf(self):
        with mock.patch.object(settings, "microsoft_on_behalf_of", "soumya@impacgo.com"), \
                mock.patch.object(settings, "microsoft_on_behalf_of_name", "Soumya Tantravahi"):
            msg = graph_mail.build_payload(graph_mail.MailMessage(to=["a@impacgo.com"], subject="S", html_body="<p/>",
                                                                  from_name="Raj Kumar"))["message"]
            self.assertEqual(msg["from"], {"emailAddress": {"address": "soumya@impacgo.com", "name": "Soumya Tantravahi"}})
            self.assertEqual(msg["sender"], {"emailAddress": {"address": "info@impacgo.com"}})
            self.assertEqual(msg["replyTo"], [{"emailAddress": {"address": "soumya@impacgo.com"}}])
            plain = graph_mail.build_payload(graph_mail.MailMessage(to=["a@impacgo.com"], subject="S", html_body="<p/>"),
                                             on_behalf=False)["message"]
            self.assertNotIn("sender", plain)
            self.assertEqual(plain["replyTo"], [{"emailAddress": {"address": "soumya@impacgo.com"}}])

    def test_off_when_blank_or_same_as_sender(self):
        for value in ("", "INFO@impacgo.com"):
            with mock.patch.object(settings, "microsoft_on_behalf_of", value):
                msg = graph_mail.build_payload(graph_mail.MailMessage(to=["a@impacgo.com"], subject="S", html_body="<p/>"))["message"]
                self.assertNotIn("sender", msg)
                self.assertNotIn("replyTo", msg)

    def test_denied_on_behalf_falls_back_to_mailbox(self):
        bodies = []
        script = [
            (200, {}, json.dumps({"access_token": _token(["Mail.Send"]), "expires_in": 3600}).encode()),
            (403, {}, json.dumps({"error": {"code": "ErrorSendAsDenied", "message": "denied"}}).encode()),
            (202, {}, b""),
        ]

        def fake(host, method, path, body, headers):
            if path.endswith("/sendMail"):
                bodies.append(json.loads(body))
                self.assertTrue(path.startswith("/v1.0/users/info@impacgo.com/"))
            return script.pop(0)

        with mock.patch.object(settings, "microsoft_on_behalf_of", "soumya@impacgo.com"), \
                mock.patch("app.graph_mail._request", side_effect=fake):
            status = graph_mail.send_mail(graph_mail.MailMessage(to=["a@impacgo.com"], subject="S", html_body="<p/>"))
        self.assertEqual(status, 202)
        self.assertEqual(bodies[0]["message"]["from"]["emailAddress"]["address"], "soumya@impacgo.com")
        self.assertNotIn("sender", bodies[1]["message"])  # retried from the mailbox itself
        self.assertEqual(bodies[1]["message"]["replyTo"][0]["emailAddress"]["address"], "soumya@impacgo.com")


class DiagnosticsTests(_Settings):
    def _diagnose(self, roles):
        reply = (200, {}, json.dumps({"access_token": _token(roles), "expires_in": 3600}).encode())
        with mock.patch("app.graph_mail._request", return_value=reply):
            return graph_mail.diagnose()

    def test_reports_missing_mail_send(self):
        out = self._diagnose([])
        self.assertTrue(out["token_ok"])
        self.assertFalse(out["mail_send_granted"])
        self.assertIn("Grant admin consent", out["error"])
        self.assertNotIn(SECRET, json.dumps(out))

    def test_reports_granted(self):
        out = self._diagnose(["Mail.Send"])
        self.assertTrue(out["mail_send_granted"])
        self.assertIsNone(out["error"])

    def test_auth_failure_is_explained(self):
        reply = (401, {}, json.dumps({"error": "invalid_client", "error_codes": [7000215]}).encode())
        with mock.patch("app.graph_mail._request", return_value=reply):
            out = graph_mail.diagnose()
        self.assertFalse(out["token_ok"])
        self.assertEqual(out["error_code"], "invalid_client_secret")


class DbCase(_Settings):
    def setUp(self):
        super().setUp()
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.sent = []  # jobs handed to the background sender
        mock.patch.object(email_service._executor, "submit",
                          side_effect=lambda fn, job, *a: self.sent.append(job)).start()
        self.tmp = tempfile.TemporaryDirectory()
        mock.patch("app.storage.uploads_root", lambda: Path(self.tmp.name)).start()
        self.owner = next((u for u in self.db.scalars(select(models.User)).all()
                           if (r := crud.get_user_primary_role(self.db, u.id)) is not None and r.name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("no Owner in this tenant")
        self.company = self.db.get(models.Company, self.owner.company_id)

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()
        self.tmp.cleanup()
        super().tearDown()

    def logs(self, **where):
        q = select(models.EmailLog).where(models.EmailLog.company_id == self.company.id)
        for k, v in where.items():
            q = q.where(getattr(models.EmailLog, k) == v)
        return self.db.scalars(q).all()


class AllowlistTests(DbCase):
    def test_automatic_email_outside_domain_is_logged_skipped(self):
        with mock.patch.object(settings, "email_allowed_domains", "impacgo.com"):
            log = email_service.send_email(self.db, to="someone@example.com", subject="S", html_body="<p/>",
                                           company_id=self.company.id, email_type="ALLOWLIST_TEST")
            self.assertIsNone(log)
            self.db.commit()
        self.assertEqual(self.sent, [])
        skipped = self.logs(email_type="ALLOWLIST_TEST")
        self.assertEqual([(l.status, l.error_code) for l in skipped], [("SKIPPED", "domain_not_allowed")])

    def test_mixed_recipients_keep_only_allowed(self):
        with mock.patch.object(settings, "email_allowed_domains", "impacgo.com"):
            email_service.send_email(self.db, to=["a@impacgo.com", "b@example.com"], subject="S", html_body="<p/>",
                                     company_id=self.company.id)
            self.db.commit()
        self.assertEqual(self.sent[0].to, ["a@impacgo.com"])

    def test_manual_send_outside_domain_is_refused(self):
        with mock.patch.object(settings, "email_allowed_domains", "impacgo.com"):
            with self.assertRaises(email_service.EmailError) as ctx:
                email_service.send_test_email(self.db, to="x@example.com", requested_by="t",
                                              company_id=self.company.id, created_by=None)
        self.assertEqual(ctx.exception.code, "domain_not_allowed")


class ReminderTests(DbCase):
    def _holiday(self, days_ahead=1, branch_id=None, optional=False):
        today = crud.company_today(self.db, self.company.id)
        h = models.Holiday(id=uuid.uuid4(), company_id=self.company.id, branch_id=branch_id,
                           holiday_date=today + datetime.timedelta(days=days_ahead),
                           name="Test Festival", is_optional=optional)
        self.db.add(h)
        self.db.flush()
        return h, today

    def test_holiday_reminder_once_to_every_active_employee(self):
        h, today = self._holiday()
        employees = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.company.id, models.Employee.is_active.is_(True))).all()
        with_email = {e.work_email.lower() for e in employees if e.work_email and email_service.is_valid_email(e.work_email)}
        self.assertEqual(reminders.holiday_reminders(self.db, self.company.id, today), 1)
        self.db.commit()
        got = {job.to[0].lower() for job in self.sent if job.email_type == email_service.HOLIDAY_REMINDER}
        self.assertEqual(got, with_email)
        job = next(j for j in self.sent if j.email_type == email_service.HOLIDAY_REMINDER)
        self.assertIn("Holiday Tomorrow: Test Festival", job.subject)
        self.assertIn(today.strftime("%Y") if False else "Test Festival", job.html_body)
        self.assertGreater(self.db.scalar(select(text("count(*)")).select_from(models.Notification).where(
            models.Notification.entity_type == "holiday", models.Notification.entity_id == h.id)), 0)
        # Second pass the same day: nothing new.
        self.sent.clear()
        self.assertEqual(reminders.holiday_reminders(self.db, self.company.id, today), 0)
        self.db.commit()
        self.assertEqual(self.sent, [])

    def test_branch_holiday_only_that_branch(self):
        branch_emp = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.company.id, models.Employee.is_active.is_(True),
            models.Employee.branch_id.is_not(None))).first()
        if branch_emp is None:
            self.skipTest("no employee with a branch")
        _h, today = self._holiday(branch_id=branch_emp.branch_id)
        reminders.holiday_reminders(self.db, self.company.id, today)
        self.db.commit()
        allowed = {e.work_email.lower() for e in self.db.scalars(select(models.Employee).where(
            models.Employee.branch_id == branch_emp.branch_id, models.Employee.is_active.is_(True))).all() if e.work_email}
        self.assertTrue({j.to[0].lower() for j in self.sent} <= allowed)

    def test_scheduler_pass_runs_holidays_and_birthdays(self):
        _h, today = self._holiday()
        birthday = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.company.id, models.Employee.is_active.is_(True))).first()
        birthday.date_of_birth = today.replace(year=1990)
        self.db.flush()
        crud._CELEBRATIONS_CHECKED.clear()
        with mock.patch("app.crud.open_tenant_session", return_value=self.db), \
                mock.patch.object(self.db, "close", lambda: None):
            result = reminders.run_tenant(TENANT, force=True)
        company_result = result[str(self.company.id)]
        self.assertEqual(company_result["holidays"], 1)
        self.assertTrue(company_result["celebrations"])
        types = {j.email_type for j in self.sent}
        self.assertIn(email_service.HOLIDAY_REMINDER, types)
        self.assertIn(email_service.CELEBRATION, types)
        crud._CELEBRATIONS_CHECKED.clear()

    def test_send_hour_respected(self):
        self._holiday()
        with mock.patch("app.crud.open_tenant_session", return_value=self.db), \
                mock.patch.object(self.db, "close", lambda: None), \
                mock.patch.object(settings, "reminder_send_hour", 24):
            self.assertEqual(reminders.run_tenant(TENANT), {})
        self.assertEqual(self.sent, [])


class FnfAndDocumentTests(DbCase):
    def setUp(self):
        super().setUp()
        from app.routers import exit_letters as letters_api
        from app.routers import fnf as fnf_api
        self.letters_api, self.fnf_api = letters_api, fnf_api
        exits = [e for e in self.db.scalars(select(models.ExitRequestModel)).all()
                 if (emp := self.db.get(models.Employee, e.employee_id)) is not None and emp.company_id == self.company.id]
        self.exit = next((e for e in exits if e.status in crud.FNF_EXIT_STATUSES), None)
        if self.exit is None:
            self.skipTest("no approved exit")
        old = crud.get_final_settlement(self.db, self.exit.id)
        if old is not None:
            self.db.execute(delete(models.FinalSettlementLine).where(models.FinalSettlementLine.settlement_id == old.id))
            self.db.delete(old)
            self.db.flush()
        self.employee = self.db.get(models.Employee, self.exit.employee_id)
        self.employee.personal_email = "leaver.personal@impacgo.com"
        self.db.flush()

    def _prepare_and_approve(self):
        self.fnf_api.save_fnf(self.exit.id, schemas.FnfSaveRequest(lines=[
            {"line_type": "earning", "component": "Salary for days worked", "amount": Decimal("10000")}]),
            db=self.db, current_user=self.owner)
        self.fnf_api.approve_fnf(self.exit.id, schemas.FnfApproveRequest(), db=self.db, current_user=self.owner)

    def test_paid_emails_employee_with_statement(self):
        dart = (Path(__file__).resolve().parents[2] / "lib" / "models" / "exit_letter_models.dart").read_text(encoding="utf-8")
        import re
        html = re.search(r"kFnfStatementStarterHtml = r'''(.*?)''';", dart, re.S).group(1)
        self.letters_api.save_exit_letter_template(
            schemas.ExitLetterTemplateUpdate(name="F&F", html_body=html), letter_type="fnf_statement",
            db=self.db, current_user=self.owner)
        self._prepare_and_approve()
        self.letters_api.generate_exit_letter(self.exit.id, schemas.ExitLetterGenerateRequest(),
                                              letter_type="fnf_statement", db=self.db, current_user=self.owner)
        self.sent.clear()
        self.fnf_api.mark_fnf_paid(self.exit.id, schemas.FnfMarkPaidRequest(
            payment_date=crud.company_today(self.db, self.company.id), payment_mode="Bank transfer",
            payment_reference="UTR1"), db=self.db, current_user=self.owner)
        job = next(j for j in self.sent if j.related_entity_type == "final_settlement")
        self.assertEqual(job.to, ["leaver.personal@impacgo.com"])  # personal email first
        self.assertEqual(len(job.attachments), 1)
        self.assertTrue(job.attachments[0].content.startswith(b"%PDF"))
        self.assertIn("UTR1", job.html_body)

    def test_submitted_goes_to_approvers_not_preparer(self):
        approvers = crud.fnf_approver_employee_ids(self.db, self.company.id, exclude_employee_id=self.owner.employee_id)
        self.fnf_api.save_fnf(self.exit.id, schemas.FnfSaveRequest(lines=[
            {"line_type": "earning", "component": "Gratuity", "amount": Decimal("5000")}]),
            db=self.db, current_user=self.owner)
        notified = {n.user_id for n in self.db.scalars(select(models.Notification).where(
            models.Notification.entity_type == "final_settlement", models.Notification.entity_id == self.exit.id))}
        expected = {crud.get_user_id_for_employee(self.db, a) for a in approvers} - {None}
        self.assertEqual(notified, expected)
        self.assertNotIn(self.owner.id, notified)
        # A second save the same day does not notify again.
        before = len(self.sent)
        self.fnf_api.save_fnf(self.exit.id, schemas.FnfSaveRequest(lines=[
            {"line_type": "earning", "component": "Gratuity", "amount": Decimal("6000")}]),
            db=self.db, current_user=self.owner)
        self.assertEqual(len([j for j in self.sent[before:] if j.related_entity_type == "final_settlement"]), 0)

    def test_generate_can_email_the_pdf(self):
        self.letters_api.save_exit_letter_template(
            schemas.ExitLetterTemplateUpdate(name="E&R", html_body="<p>{{employee_name}} {{letter_number}}</p>"),
            letter_type="experience_relieving", db=self.db, current_user=self.owner)
        if not crud.exit_letter_eligibility(self.db, self.company.id, self.exit)[0]:
            self.skipTest("exit not eligible for the letter yet")
        out = self.letters_api.generate_exit_letter(
            self.exit.id, schemas.ExitLetterGenerateRequest(email_employee=True),
            letter_type="experience_relieving", db=self.db, current_user=self.owner)
        self.assertEqual(out.emailed_to, "leaver.personal@impacgo.com")
        job = next(j for j in self.sent if j.related_entity_type == "exit_letter")
        self.assertTrue(job.attachments[0].name.startswith("Experience_and_Relieving_Letter_"))
        self.assertIn(out.letter_number, job.html_body)


class BulkPayslipTests(DbCase):
    def _run(self, run, resend=False):
        from fastapi import BackgroundTasks
        from app.routers import email as email_api
        tasks = BackgroundTasks()
        out = email_api.email_run_payslips(run.id, email_api.RunPayslipsEmailIn(resend=resend), tasks,
                                           db=self.db, current_user=self.owner)
        # The background task opens its own tenant session: use this one.
        with mock.patch("app.crud.open_tenant_session", return_value=self.db),                 mock.patch.object(self.db, "close", lambda: None):
            for task in tasks.tasks:
                task.func(*task.args, **task.kwargs)
        return out

    def test_run_payslips_emailed_once_with_pdf_built_at_send_time(self):
        run = self.db.scalars(select(models.PayrollRun).where(models.PayrollRun.company_id == self.company.id)).first()
        if run is None or not self.db.scalars(select(models.SalarySlip).where(models.SalarySlip.payroll_run_id == run.id)).first():
            self.skipTest("no payroll run with payslips")
        out = self._run(run)
        self.assertGreater(out["queued"], 0)
        jobs = [j for j in self.sent if j.email_type == email_service.PAYSLIP]
        self.assertEqual(len(jobs), out["queued"])
        self.assertEqual(jobs[0].attachments, [])  # not rendered in the request
        self.assertIsNotNone(jobs[0].attachments_factory)
        with mock.patch("app.crud.open_tenant_session", return_value=self.db),                 mock.patch.object(self.db, "close", lambda: None):
            pdf = jobs[0].attachments_factory()
        self.assertTrue(pdf[0].content.startswith(b"%PDF"))
        # Second click: everyone already emailed.
        self.sent.clear()
        again = self._run(run)
        self.assertEqual(again["queued"], 0)
        self.assertEqual(len(again["skipped_already_sent"]), out["queued"])
        self.assertEqual(self.sent, [])
        # Resend on request -- really queued again, not swallowed as a duplicate.
        self.assertEqual(self._run(run, resend=True)["queued"], out["queued"])
        self.assertEqual(len([j for j in self.sent if j.email_type == email_service.PAYSLIP]), out["queued"])


if __name__ == "__main__":
    unittest.main()
