"""Microsoft Graph email integration tests (app/graph_mail.py, app/email_service.py,
app/routers/email.py).

    cd backend && venv/Scripts/python -m unittest tests.test_graph_email -v

Graph / Entra ID HTTP calls are mocked at graph_mail._request (no network,
no real credentials). The DB tests use the configured database inside a
transaction that is ALWAYS rolled back (tenant impacgo-solutions by default,
override with EMAIL_TEST_TENANT) -- nothing is written.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, email_service, graph_mail, models  # noqa: E402
from app.config import settings  # noqa: E402

SECRET = "TEST-SECRET-VALUE-do-not-log-8Q~xyz"
TOKEN = "eyTEST.ACCESS.TOKEN.never-logged"
TENANT = os.environ.get("EMAIL_TEST_TENANT", "impacgo-solutions")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def _ok_token(expires_in=3600):
    return 200, {}, json.dumps({"access_token": TOKEN, "expires_in": expires_in}).encode()


def _aad(code, desc):
    return 401, {}, json.dumps({"error": "invalid_client", "error_description": f"AADSTS{code}: {desc}", "error_codes": [code]}).encode()


def _graph(status, code="", headers=None):
    return status, headers or {}, json.dumps({"error": {"code": code, "message": "x"}}).encode()


# The tests use example.com addresses and a plain sender, whatever the local
# .env says (EMAIL_ALLOWED_DOMAINS / MICROSOFT_ON_BEHALF_OF have own tests).
_ENV_OVERRIDES = {"email_allowed_domains": "*", "microsoft_on_behalf_of": "", "microsoft_on_behalf_of_name": ""}
_ENV_SAVED: dict = {}


def setUpModule():
    for key, value in _ENV_OVERRIDES.items():
        _ENV_SAVED[key] = getattr(settings, key)
        setattr(settings, key, value)


def tearDownModule():
    for key, value in _ENV_SAVED.items():
        setattr(settings, key, value)


class GraphEnv(unittest.TestCase):
    """Configured Graph settings + a scripted fake HTTP layer."""

    def setUp(self):
        self.patches = [
            mock.patch.object(settings, "microsoft_tenant_id", "00000000-0000-0000-0000-000000000001"),
            mock.patch.object(settings, "microsoft_client_id", "00000000-0000-0000-0000-000000000002"),
            mock.patch.object(settings, "microsoft_client_secret", SECRET),
            mock.patch.object(settings, "microsoft_from_email", "info@impacgo.com"),
            mock.patch.object(settings, "microsoft_graph_max_retries", 3),
            mock.patch.object(settings, "email_enabled", True),
            mock.patch("app.graph_mail.time.sleep", lambda s: None),
        ]
        for p in self.patches:
            p.start()
        graph_mail.invalidate_token()
        self.calls = []
        self.script = []
        self.request = mock.patch("app.graph_mail._request", side_effect=self._fake).start()
        self.log = io.StringIO()
        self.handler = logging.StreamHandler(self.log)
        logging.getLogger("app").addHandler(self.handler)
        logging.getLogger("app").setLevel(logging.DEBUG)

    def tearDown(self):
        mock.patch.stopall()
        logging.getLogger("app").removeHandler(self.handler)
        graph_mail.invalidate_token()

    def _fake(self, host, method, path, body, headers):
        self.calls.append((host, method, path, body, headers))
        if not self.script:
            raise AssertionError(f"unexpected request {host}{path}")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def assertNoSecretsLogged(self):
        logs = self.log.getvalue()
        self.assertNotIn(SECRET, logs)
        self.assertNotIn(TOKEN, logs)
        self.assertNotIn("Bearer", logs)

    def message(self, **kw):
        base = dict(to=["employee@example.com"], subject="Hello", html_body="<p>Hi</p>")
        base.update(kw)
        return graph_mail.MailMessage(**base)


class AuthenticationTests(GraphEnv):
    def test_valid_credentials_token_is_cached_and_reused(self):
        self.script = [_ok_token(), (202, {}, b""), (202, {}, b"")]
        self.assertEqual(graph_mail.send_mail(self.message()), 202)
        self.assertEqual(graph_mail.send_mail(self.message()), 202)
        token_calls = [c for c in self.calls if c[0] == "login.microsoftonline.com"]
        self.assertEqual(len(token_calls), 1, "token must be reused, not fetched per email")
        form = token_calls[0][3].decode()
        self.assertIn("grant_type=client_credentials", form)
        self.assertIn("scope=https%3A%2F%2Fgraph.microsoft.com%2F.default", form)
        self.assertTrue(token_calls[0][2].endswith("/oauth2/v2.0/token"))
        send = self.calls[1]
        self.assertEqual(send[0], "graph.microsoft.com")
        self.assertEqual(send[2], "/v1.0/users/info@impacgo.com/sendMail")
        self.assertNoSecretsLogged()

    def test_token_refreshed_when_near_expiry(self):
        self.script = [_ok_token(expires_in=60), (202, {}, b""), _ok_token(), (202, {}, b"")]
        graph_mail.send_mail(self.message())
        graph_mail.send_mail(self.message())
        self.assertEqual(sum(1 for c in self.calls if c[0] == "login.microsoftonline.com"), 2)

    def test_expired_token_401_refreshes_once(self):
        self.script = [_ok_token(), _graph(401, "InvalidAuthenticationToken"), _ok_token(), (202, {}, b"")]
        self.assertEqual(graph_mail.send_mail(self.message()), 202)

    def _auth_error(self, script, code):
        self.script = script
        with self.assertRaises(graph_mail.GraphMailError) as ctx:
            graph_mail.send_mail(self.message())
        self.assertEqual(ctx.exception.code, code)
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertNoSecretsLogged()

    def test_invalid_client_secret(self):
        self._auth_error([_aad(7000215, "Invalid client secret provided.")], "invalid_client_secret")

    def test_expired_client_secret(self):
        self._auth_error([_aad(7000222, "The provided client secret keys are expired.")], "expired_client_secret")

    def test_invalid_client_id(self):
        self._auth_error([_aad(700016, "Application not found in the directory.")], "invalid_client_id")

    def test_invalid_tenant_id(self):
        self._auth_error([(400, {}, json.dumps({"error": "invalid_request", "error_description": "AADSTS90002: Tenant not found.", "error_codes": [90002]}).encode())], "invalid_tenant_id")

    def test_not_configured(self):
        with mock.patch.object(settings, "microsoft_client_secret", ""):
            self.assertFalse(graph_mail.is_configured())
            with self.assertRaises(graph_mail.GraphMailError) as ctx:
                graph_mail.get_access_token()
            self.assertEqual(ctx.exception.code, "not_configured")

    def test_startup_validation_names_missing_variables_only(self):
        with mock.patch.object(settings, "microsoft_client_id", ""), self.assertLogs("app.config", "ERROR") as cm:
            settings.validate_email_config()
        out = "\n".join(cm.output)
        self.assertIn("Microsoft Graph email configuration is incomplete", out)
        self.assertIn("MICROSOFT_CLIENT_ID", out)
        self.assertNotIn(SECRET, out)


class SendingTests(GraphEnv):
    def _payload(self):
        return json.loads(self.calls[-1][3])["message"]

    def test_one_and_multiple_recipients_cc_bcc_reply_to(self):
        self.script = [_ok_token(), (202, {}, b"")]
        graph_mail.send_mail(self.message(to=["a@example.com", "b@example.com"], cc=["c@example.com"],
                                          bcc=["d@example.com"], reply_to=["hr@impacgo.com"]))
        m = self._payload()
        self.assertEqual([r["emailAddress"]["address"] for r in m["toRecipients"]], ["a@example.com", "b@example.com"])
        self.assertEqual(m["ccRecipients"][0]["emailAddress"]["address"], "c@example.com")
        self.assertEqual(m["bccRecipients"][0]["emailAddress"]["address"], "d@example.com")
        self.assertEqual(m["replyTo"][0]["emailAddress"]["address"], "hr@impacgo.com")

    def test_html_and_plain_text(self):
        self.script = [_ok_token(), (202, {}, b""), (202, {}, b"")]
        graph_mail.send_mail(self.message(html_body="<b>x</b>"))
        self.assertEqual(self._payload()["body"], {"contentType": "HTML", "content": "<b>x</b>"})
        graph_mail.send_mail(self.message(html_body=None, text_body="plain"))
        self.assertEqual(self._payload()["body"], {"contentType": "Text", "content": "plain"})

    def test_pdf_and_multiple_attachments(self):
        self.script = [_ok_token(), (202, {}, b"")]
        a1 = email_service.make_attachment("Offer_Letter.pdf", PDF)
        a2 = email_service.make_attachment("Policy.pdf", PDF)
        graph_mail.send_mail(self.message(attachments=[a1, a2]))
        atts = self._payload()["attachments"]
        self.assertEqual(len(atts), 2)
        self.assertEqual(atts[0]["@odata.type"], "#microsoft.graph.fileAttachment")
        self.assertEqual(atts[0]["contentType"], "application/pdf")
        self.assertEqual(atts[0]["name"], "Offer_Letter.pdf")
        import base64
        self.assertEqual(base64.b64decode(atts[0]["contentBytes"]), PDF)

    def test_throttling_retries_then_succeeds(self):
        self.script = [_ok_token(), _graph(429, "ApplicationThrottled", {"retry-after": "1"}), _graph(503), (202, {}, b"")]
        self.assertEqual(graph_mail.send_mail(self.message()), 202)

    def test_persistent_service_error_gives_up(self):
        self.script = [_ok_token()] + [_graph(503)] * 4
        with self.assertRaises(graph_mail.GraphMailError) as ctx:
            graph_mail.send_mail(self.message())
        self.assertEqual(ctx.exception.code, "service_unavailable")

    def test_grant_while_running_is_picked_up(self):
        """A token issued before Mail.Send was granted is refused (403); the
        retry fetches a new token (which carries the grant) and succeeds."""
        self.script = [_ok_token(), _graph(403, "ErrorAccessDenied"), _ok_token(), (202, {}, b"")]
        self.assertEqual(graph_mail.send_mail(self.message()), 202)
        token_calls = [c for c in self.calls if "/oauth2/v2.0/token" in c[2]]
        self.assertEqual(len(token_calls), 2)

    def test_timeout_retried(self):
        import socket
        self.script = [_ok_token(), socket.timeout(), (202, {}, b"")]
        self.assertEqual(graph_mail.send_mail(self.message()), 202)

    def test_permission_and_mailbox_errors(self):
        # 403 -> one retry with a freshly issued token; still 403 -> permission_denied.
        self.script = [_ok_token(), _graph(403, "ErrorAccessDenied"), _ok_token(), _graph(403, "ErrorAccessDenied")]
        with self.assertRaises(graph_mail.GraphMailError) as ctx:
            graph_mail.send_mail(self.message())
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertEqual(self.script, [])
        self.script = [_graph(404, "ErrorInvalidUser")]  # token still cached
        with self.assertRaises(graph_mail.GraphMailError) as ctx:
            graph_mail.send_mail(self.message())
        self.assertEqual(ctx.exception.code, "mailbox_not_found")
        self.script = [_graph(400, "ErrorInvalidRecipients")]
        with self.assertRaises(graph_mail.GraphMailError) as ctx:
            graph_mail.send_mail(self.message())
        self.assertEqual(ctx.exception.code, "invalid_recipient")
        self.assertNoSecretsLogged()

    def test_attachment_validation(self):
        with self.assertRaises(email_service.EmailError):
            email_service.make_attachment("run.exe", b"MZ")
        with self.assertRaises(email_service.EmailError):
            email_service.make_attachment("fake.pdf", b"not a pdf")
        with self.assertRaises(email_service.EmailError):
            email_service.make_attachment("empty.pdf", b"")
        big = email_service.Attachment("big.pdf", b"x" * (settings.email_max_attachment_bytes + 1))
        with self.assertRaises(email_service.EmailError) as ctx:
            email_service._check_attachment_size([big])
        self.assertEqual(ctx.exception.code, "attachment_too_large")

    def test_recipient_validation(self):
        self.assertTrue(email_service.is_valid_email("hr@impacgo.com"))
        for bad in ["", "not-an-email", "a@b", "a b@c.com", "x@y..com"]:
            self.assertFalse(email_service.is_valid_email(bad), bad)

    def test_deliver_logs_metadata_not_secrets(self):
        self.script = [_ok_token(), (202, {}, b"")]
        job = email_service.EmailJob(email_type="TEST", to=["a@example.com"], subject="S", html_body="<p>body-content-xyz</p>")
        status, code, *_ = email_service._deliver(job)
        self.assertEqual((status, code), ("SENT", 202))
        logs = self.log.getvalue()
        self.assertIn("type=TEST", logs)
        self.assertNotIn("body-content-xyz", logs)
        self.assertNoSecretsLogged()

    def test_status_endpoint_never_returns_secret(self):
        s = email_service.status()
        self.assertEqual(s["transport"], "graph")
        self.assertNotIn(SECRET, json.dumps(s))


class TemplateTests(unittest.TestCase):
    def test_values_are_escaped(self):
        html_out = email_service.render_email("leave_applied", {
            "employee_name": "<script>alert(1)</script>", "employee_id": "E1", "leave_type": "Casual",
            "start_date": "01 Oct 2026", "end_date": "02 Oct 2026", "number_of_days": "2",
            "reason": "Family", "status": "Pending", "hrms_url": "https://hrms.example/#/leave/1",
        })
        self.assertNotIn("<script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)
        self.assertIn("Casual", html_out)


class DbTestCase(unittest.TestCase):
    """A tenant session inside an outer transaction that is always rolled back."""

    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.submitted = []
        mock.patch.object(email_service._executor, "submit", side_effect=lambda fn, *a: self.submitted.append(a)).start()
        mock.patch.object(settings, "microsoft_tenant_id", "t").start()
        mock.patch.object(settings, "microsoft_client_id", "c").start()
        mock.patch.object(settings, "microsoft_client_secret", SECRET).start()
        mock.patch.object(settings, "microsoft_from_email", "info@impacgo.com").start()
        mock.patch.object(settings, "email_enabled", True).start()

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def company(self):
        return self.db.scalars(select(models.Company)).first()


class TransactionSafetyTests(DbTestCase):
    def _queue(self, key=None):
        return email_service.send_email(self.db, to="employee@example.com", subject="S", html_body="<p/>",
                                        company_id=self.company().id, idempotency_key=key)

    def test_not_sent_before_commit_and_sent_after(self):
        log = self._queue()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, "QUEUED")
        self.assertEqual(self.submitted, [], "must not send before commit")
        self.db.commit()
        self.assertEqual(len(self.submitted), 1)

    def test_rollback_discards_email_and_log(self):
        log = self._queue()
        self.db.rollback()
        self.assertEqual(self.submitted, [])
        self.assertIsNone(self.db.get(models.EmailLog, log.id))

    def test_idempotency_suppresses_duplicate(self):
        self.assertIsNotNone(self._queue(key="LEAVE_APPLIED:x:employee@example.com"))
        self.assertIsNone(self._queue(key="LEAVE_APPLIED:x:employee@example.com"))
        self.db.commit()
        self.assertEqual(len(self.submitted), 1)

    def test_invalid_recipient_is_not_queued(self):
        self.assertIsNone(email_service.send_email(self.db, to="bad", subject="S", html_body="<p/>"))


class LeaveWorkflowTests(DbTestCase):
    def _leave_with_manager(self):
        for leave in self.db.scalars(select(models.LeaveRequest)).all():
            emp = self.db.get(models.Employee, leave.employee_id)
            mgr = self.db.get(models.Employee, emp.reporting_manager_id) if emp and emp.reporting_manager_id else None
            if emp and emp.work_email and mgr and mgr.work_email:
                return leave, emp, mgr
        self.skipTest("no leave request with an emailable employee + manager in this tenant")

    def _jobs(self):
        return [a[0] for a in self.submitted]

    def test_leave_applied_approved_rejected(self):
        leave, emp, mgr = self._leave_with_manager()
        name = f"{emp.first_name} {emp.last_name or ''}".strip()
        crud.notify_new_request(self.db, emp.company_id, emp, "Leave Request", "leave_request", leave.id)
        self.db.commit()
        applied = [j for j in self._jobs() if j.email_type == email_service.LEAVE_APPLIED]
        self.assertTrue(applied)
        self.assertEqual(applied[0].subject, f"New Leave Application - {name}")
        # One email per manager (reporting + dotted-line).
        self.assertIn(mgr.work_email, [a for j in applied for a in j.to])
        for field in (name, emp.employee_code or "", "Leave Type", "Number of Days", "Pending Approval"):
            self.assertIn(field, applied[0].html_body)

        crud.notify_decision(self.db, emp.company_id, emp.id, "Leave Request", "approved", None, "leave_request", leave.id)
        self.db.commit()
        approved = [j for j in self._jobs() if j.email_type == email_service.LEAVE_APPROVED]
        self.assertEqual(approved[0].subject, f"Leave Approved - {name}")
        self.assertEqual(approved[0].to, [emp.work_email])
        self.assertIn("Approved By", approved[0].html_body)

        crud.notify_decision(self.db, emp.company_id, emp.id, "Leave Request", "rejected", "Project deadline", "leave_request", leave.id)
        self.db.commit()
        rejected = [j for j in self._jobs() if j.email_type == email_service.LEAVE_REJECTED]
        self.assertEqual(rejected[0].subject, f"Leave Rejected - {name}")
        self.assertIn("Project deadline", rejected[0].html_body)
        self.assertIn("Rejected By", rejected[0].html_body)


class HrDocumentTests(DbTestCase):
    def test_hr_document_kinds_send_pdf_attachment(self):
        company = self.company()
        sent = []
        with mock.patch.object(email_service, "_deliver", side_effect=lambda job: (sent.append(job) or ("SENT", 202, None, None, "graph"))), \
             mock.patch.object(email_service, "_update_log"):
            for kind in ("offer_letter", "appointment_letter", "experience_letter", "relieving_letter", "payslip"):
                result = email_service.send_hr_document(
                    self.db, document_kind=kind, to="candidate@example.com",
                    attachments=[email_service.make_attachment(f"{kind}.pdf", PDF)],
                    recipient_name="Test Person", company_id=company.id, company_name=company.name,
                    sender_name="HR", created_by=None,
                )
                self.assertEqual(result.status, "SENT")
        self.assertEqual(len(sent), 5)
        self.assertEqual({j.attachments[0].content_type for j in sent}, {"application/pdf"})
        self.assertIn("Offer Letter", sent[0].html_body)
        self.assertIn("Relieving Letter", sent[3].subject)


class AuthorizationTests(DbTestCase):
    def _user_with_role(self, role_names):
        for user in self.db.scalars(select(models.User)).all():
            role = crud.get_user_primary_role(self.db, user.id)
            if role is not None and role.name in role_names:
                return user
        self.skipTest(f"no user with role {role_names} in this tenant")

    def test_employee_cannot_send_hr_documents_or_use_admin_email(self):
        from app.deps import require_permission
        from app.routers.email import require_hr_sender
        employee = self._user_with_role({"Professional / IC Employee", "Associate / Intern", "Employee (ESS)"})
        # Role-only access: drop any real per-employee overrides / grants the
        # shared test DB gives this user (rolled back in tearDown).
        self.db.execute(delete(models.EmployeePermission).where(
            models.EmployeePermission.employee_id == employee.employee_id))
        self.db.execute(delete(models.EmployeeAccessGrant).where(
            models.EmployeeAccessGrant.employee_id == employee.employee_id))
        self.db.flush()
        crud.clear_rbac_memo(self.db)
        with self.assertRaises(HTTPException) as ctx:
            require_hr_sender(employee, self.db)
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(HTTPException) as ctx:
            require_permission("system_settings_rbac")(employee, self.db)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_can_send(self):
        from app.deps import _OWNER_ROLE_NAME
        from app.routers.email import require_hr_sender
        owner = self._user_with_role({_OWNER_ROLE_NAME})
        self.assertIs(require_hr_sender(owner, self.db), owner)


if __name__ == "__main__":
    unittest.main()
