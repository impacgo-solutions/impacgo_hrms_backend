"""Every link in an HRMS notification email points at the configured HRMS
application URL (https://hrms.impacgo.com) -- never localhost, a staging host
or an unformatted value -- across approval / rejection / pending-request /
leave / celebration / promotion / holiday emails. No database needed: the
queue is captured, nothing is sent.

    cd backend && venv/Scripts/python -m unittest tests.test_email_links -v
"""
from __future__ import annotations

import logging
import os
import re
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import email_actions, email_service as es  # noqa: E402
from app.config import DEFAULT_FRONTEND_BASE_URL, Settings, settings  # noqa: E402

BASE = "https://hrms.impacgo.com"
APP_DIR = Path(__file__).resolve().parents[1] / "app"
_HREF = re.compile(r'href="([^"]*)"')
_URL = re.compile(r"https?://[^\s\"'<>)]+")
_FORBIDDEN = ("localhost", "127.0.0.1", "0.0.0.0", ":8090", "staging", "example.com", "imapcgo")


class EmailLinkTests(unittest.TestCase):
    def setUp(self):
        self.jobs: list[es.EmailJob] = []
        patches = [
            mock.patch.object(es, "queue_email", side_effect=lambda db, job: self.jobs.append(job)),
            mock.patch.object(es.settings, "frontend_base_url", BASE),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # ── helpers ──
    def links(self, job: es.EmailJob) -> list[str]:
        return _HREF.findall(job.html_body or "")

    def assert_job_links(self, job: es.EmailJob, label: str):
        html, text = job.html_body or "", job.text_body or ""
        links = self.links(job)
        self.assertTrue(links, f"{label}: the email has no link")
        for href in links:
            self.assertTrue(href.startswith("https://"), f"{label}: not https: {href}")
            self.assertTrue(href == BASE or href.startswith(BASE + "/#/"), f"{label}: unexpected link {href}")
            self.assertNotIn(" ", href, f"{label}: link has a space")
        for blob, where in ((html, "html"), (text, "text")):
            for bad in _FORBIDDEN:
                self.assertNotIn(bad, blob.lower(), f"{label}: {where} contains {bad!r}")
            for url in _URL.findall(blob):
                if "w3.org" in url:
                    continue
                self.assertTrue(url.startswith(BASE), f"{label}: {where} links elsewhere: {url}")
        # a real, clickable anchor: <a href="..."> wrapping visible text
        self.assertRegex(html, r'<a href="https://hrms\.impacgo\.com[^"]*"[^>]*>[^<]+</a>', label)

    # ── workflow (approval / rejection / pending) emails, every entity type ──
    def test_request_emails_for_every_workflow_type(self):
        for entity_type, view in es._ENTITY_TYPE_TO_VIEW.items():
            for kind, cta in ((es.REQUEST_SUBMITTED, "Open Now"), (es.REQUEST_DECIDED, "Open Now")):
                self.jobs.clear()
                rid = uuid.uuid4()
                es.send_request_email(
                    "approver@impacgo.com", subject=f"{entity_type} update", heading="Heading",
                    intro_line="Intro line", details={"Employee": "A B", "Status": "Pending"},
                    entity_type=entity_type, entity_id=rid, cta_label=cta, email_type=kind)
                self.assertEqual(len(self.jobs), 1, entity_type)
                self.assert_job_links(self.jobs[0], entity_type)
                # the deep link opens the right module, record and record kind
                link = f"{BASE}/#/{view}/{rid}?type={entity_type}"
                self.assertIn(f'href="{link}"', self.jobs[0].html_body, entity_type)
                self.assertIn(link, self.jobs[0].text_body, entity_type)

    def test_attendance_emails_open_attendance_not_the_approvals_inbox(self):
        for entity_type in ("compliance_exception", "shift_reminder", "regularization", "overtime_request"):
            self.assertEqual(es._deep_link(entity_type, "1"), f"{BASE}/#/attendance/1?type={entity_type}")

    def test_link_without_a_record_has_no_type(self):
        self.assertEqual(es._deep_link("holiday"), f"{BASE}/#/leave")
        self.assertEqual(es._deep_link("leave_request", None), f"{BASE}/#/leave")

    def test_unknown_entity_type_falls_back_to_the_approvals_inbox(self):
        es.send_request_email("a@impacgo.com", subject="s", heading="h", intro_line="i", details={},
                              entity_type="something_new", entity_id=None)
        self.assertIn(f'href="{BASE}/#/approvals"', self.jobs[0].html_body)
        self.assert_job_links(self.jobs[0], "fallback")

    def test_general_notification_without_a_record_links_to_the_app(self):
        es.send_notification(None, to="a@impacgo.com", subject="s", heading="h", message="m")
        self.assertEqual(self.links(self.jobs[0]), [BASE])
        self.assert_job_links(self.jobs[0], "send_notification")

    # ── leave emails (applied / approved / rejected / withdrawal) ──
    def test_leave_emails(self):
        lid, common = uuid.uuid4(), dict(
            employee_name="A B", employee_code="E1", leave_type="Casual", start_date="01 Nov 2026",
            end_date="02 Nov 2026", number_of_days="2", company_id=None)
        es.send_leave_applied_email(None, to="m@impacgo.com", reason="r", status="Pending", leave_request_id=lid, **common)
        es.send_leave_approved_email(None, to="e@impacgo.com", approved_by="M", notes="ok", leave_request_id=lid, **common)
        es.send_leave_rejected_email(None, to="e@impacgo.com", rejected_by="M", rejection_reason="no", leave_request_id=lid, **common)
        for kind in ("requested", "approved", "rejected", "notice"):
            es.send_leave_withdrawal_email(
                None, to="x@impacgo.com", kind=kind, reason="r", status="s", decided_by="M", restored="1", notes="n",
                withdrawal_id=uuid.uuid4(), **common)
        self.assertEqual(len(self.jobs), 7)
        for job in self.jobs:
            self.assert_job_links(job, job.subject)
            self.assertIn(f"{BASE}/#/leave/", job.html_body)

    # ── celebration / promotion / holiday ──
    def test_celebration_promotion_and_holiday_emails(self):
        import datetime
        today = datetime.date(2026, 10, 28)
        for et in ("birthday", "work_anniversary"):
            es.send_notification_email(
                "e@impacgo.com", subject="s", heading="h", body_lines=["hello"], employee_name="A B",
                employee_code="E1", status_label="Birthday", occurred_at=today, entity_type=et)
        es.send_promotion_announcement_email(
            "e@impacgo.com", db=None, company_id=None, employee_id=uuid.uuid4(), employee_name="A B",
            old_designation="Dev", new_designation="Lead")
        es.send_holiday_reminder_email(
            "e@impacgo.com", db=None, company_id=None, holiday_id=uuid.uuid4(), subject="s", heading="h",
            body="b", holiday_name="Diwali", holiday_date="01 Nov 2026", optional=False)
        self.assertEqual(len(self.jobs), 4)
        for job in self.jobs:
            self.assert_job_links(job, job.subject)

    # ── configuration ──
    def test_the_default_application_url_is_the_hrms_domain(self):
        self.assertEqual(DEFAULT_FRONTEND_BASE_URL, BASE)
        self.assertEqual(es.hrms_url(), BASE)

    def test_url_setting_is_cleaned_and_validated(self):
        def make(**kw):
            return Settings(database_url="postgresql://u:p@h/db", _env_file=None, **kw)
        self.assertEqual(make().frontend_base_url, BASE)
        self.assertEqual(make(frontend_base_url="  https://hrms.impacgo.com/  ").frontend_base_url, BASE)
        self.assertEqual(make(frontend_base_url="").frontend_base_url, BASE)  # blank -> the default, never ""
        self.assertEqual(make(public_api_base_url="https://api.impacgo.com/").public_api_base_url, "https://api.impacgo.com")
        for bad in ("hrms.impacgo.com", "ftp://hrms.impacgo.com", "https://hrms impacgo.com", "javascript:alert(1)"):
            with self.assertRaises(Exception, msg=bad):
                make(frontend_base_url=bad)

    def test_production_startup_warns_about_localhost_links(self):
        def make(env, url):
            return Settings(database_url="postgresql://u:p@h/db", _env_file=None, app_env=env, frontend_base_url=url)
        with self.assertLogs("app.config", level="WARNING") as cm:
            make("production", "http://localhost:8090").warn_email_link_urls()
        self.assertTrue(any("points at localhost" in m for m in cm.output))
        with self.assertLogs("app.config", level="WARNING") as cm:
            make("production", "http://hrms.example.org").warn_email_link_urls()
        self.assertTrue(any("plain http" in m for m in cm.output))
        # the real URL, and any dev setup, stay quiet
        for s in (make("production", BASE), make("dev", "http://localhost:8090")):
            with self.assertRaises(AssertionError):
                with self.assertLogs("app.config", level="WARNING"):
                    s.warn_email_link_urls()

    def test_candidate_and_email_action_links_follow_the_same_base(self):
        with mock.patch.object(settings, "public_api_base_url", ""):
            self.assertEqual(email_actions.action_url("tok"), f"{BASE}/api/email-actions/tok")
        with mock.patch.object(settings, "public_api_base_url", "https://api.impacgo.com"):
            self.assertEqual(email_actions.action_url("tok"), "https://api.impacgo.com/api/email-actions/tok")

    # ── nothing outdated is left in the email code or templates ──
    def test_no_hardcoded_local_or_staging_urls_in_email_code_and_templates(self):
        files = sorted((APP_DIR / "email_templates").glob("*.html")) + [
            APP_DIR / n for n in ("email_service.py", "email_actions.py", "recruitment_onboarding.py",
                                  "reminders.py", "template_rendering.py", "graph_mail.py")
            if (APP_DIR / n).exists()
        ]
        self.assertGreaterEqual(len(files), 17)
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            for bad in ("localhost", "127.0.0.1", ":8090", "staging.", "imapcgo"):
                self.assertNotIn(bad, text, f"{path.name} contains {bad!r}")
            for url in _URL.findall(text):
                host = re.sub(r"^https?://", "", url).split("/")[0]
                self.assertTrue(
                    host in ("www.w3.org",) or host.endswith(("microsoft.com", "microsoftonline.com", "windows.net",
                                                              "amazonaws.com", "sendgrid.com", "impacgo.com")),
                    f"{path.name} links to unexpected host {host}")
        # every template that has a button links through the single {{hrms_url}} placeholder
        for path in (APP_DIR / "email_templates").glob("*.html"):
            for href in _HREF.findall(path.read_text(encoding="utf-8")):
                self.assertEqual(href, "{{hrms_url}}", f"{path.name}: hard-coded href {href}")


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
