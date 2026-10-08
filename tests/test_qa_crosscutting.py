"""QA cross-cutting fixes: reports (H-15, M-39, L-23, L-24, L-25), compliance
settings (M-17), email-template kinds (L-04), travel & expenses (M-18, M-19,
M-21, M-27, L-19, L-20), template sanitiser (M-34), config / headers / docs /
email allow-list (M-41, L-36, L-37, L-38) and CLI trial (N-08).

    cd backend && venv/Scripts/python -m unittest tests.test_qa_crosscutting -v

Real routes, real users of tenant acme (see tests/test_people_visibility.py),
inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

import app.config  # noqa: E402
from app import (  # noqa: E402
    compliance,
    config,
    crud,
    email_service,
    main,
    models,
    provision_tenant,
    report_pdf_renderer,
    template_rendering as tr,
)
from app.middleware import report_range  # noqa: E402
from tests.test_people_visibility import PeopleTestBase  # noqa: E402

TODAY = datetime.date.today()


class _Http(PeopleTestBase):
    def setUp(self):
        super().setUp()
        mock.patch.object(app.config.settings, "rate_limit_enabled", False).start()
        # No real emails / pushes from notifications in these flows.
        mock.patch.object(crud, "notify_new_request", lambda *a, **k: None).start()


# ── Reports ────────────────────────────────────────────────────────────────

class ReportsTests(_Http):
    def _range(self, days=30):
        return {"from_date": (TODAY - datetime.timedelta(days=days)).isoformat(), "to_date": TODAY.isoformat()}

    def test_h15_payroll_report_needs_org_payroll_scope(self):
        self.as_("manager")
        r = self.client.get("/api/reports/payroll", params=self._range())
        self.assertEqual(r.status_code, 403, r.text)
        self.as_("owner")
        r = self.client.get("/api/reports/payroll", params=self._range())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Gross Pay", r.json()["columns"])

    def test_l23_inverted_range_is_422_everywhere(self):
        self.as_("owner")
        bad = {"from_date": TODAY.isoformat(), "to_date": (TODAY - datetime.timedelta(days=5)).isoformat()}
        for path in ("/api/reports/attendance", "/api/reports/leave", "/api/reports/people",
                     "/api/reports/compliance", "/api/reports/overtime", "/api/reports/leave-withdrawals"):
            r = self.client.get(path, params=bad)
            self.assertEqual(r.status_code, 422, f"{path}: {r.text}")
        self.assertTrue(report_range.inverted_range("from_date=2026-10-05&to_date=2026-10-01"))
        self.assertFalse(report_range.inverted_range("from_date=2026-10-01&to_date=2026-10-01"))

    def test_l25_compliance_report_is_read_only(self):
        self.as_("owner")
        with mock.patch.object(compliance, "run_company", side_effect=AssertionError("write on GET")):
            r = self.client.get("/api/reports/compliance", params=self._range())
        self.assertEqual(r.status_code, 200, r.text)

    def test_m39_recruitment_counts_offers_and_hires(self):
        self.as_("owner")
        cid = self.actor.company_id
        opening = models.JobOpening(id=uuid.uuid4(), company_id=cid, title="QA Crosscut Role",
                                    vacancies=1, status="open", posted_date=TODAY)
        self.db.add(opening)
        self.db.flush()
        stages = ["hired", "applied", "applied", "applied"]
        apps = []
        for i, stage in enumerate(stages):
            cand = models.Candidate(id=uuid.uuid4(), company_id=cid, name=f"QA Cand {i}")
            self.db.add(cand)
            self.db.flush()
            a = models.JobApplication(id=uuid.uuid4(), opening_id=opening.id, candidate_id=cand.id,
                                      stage=stage, status=stage)
            self.db.add(a)
            apps.append(a)
        self.db.flush()
        # one more application with an offer row but still in 'applied' state
        self.db.add(models.Offer(id=uuid.uuid4(), application_id=apps[1].id, offered_ctc=100000,
                                 offer_date=TODAY, status="sent"))
        self.db.flush()
        r = self.client.get("/api/reports/recruitment", params=self._range(1))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        row = next(x for x in body["rows"] if x[0] == "QA Crosscut Role")
        self.assertEqual(row[-1], "2")  # hired + offered
        self.assertIn("Hires", body["summary"])

    def test_l24_pdf_export_requires_permission_and_prints_server_rows(self):
        self.as_("no_people")
        payload = {"title": "X", "report_type": "people", "params": self._range(),
                   "columns": ["Evil"], "rows": [["<b>forged</b>"]]}
        r = self.client.post("/api/reports/export/pdf", json=payload)
        self.assertEqual(r.status_code, 403, r.text)

        self.as_("owner")
        captured = {}

        def fake_render(doc, company, generated_by):
            captured.update(doc)
            return b"%PDF-1.4 fake"

        with mock.patch.object(report_pdf_renderer, "render_report_pdf", side_effect=fake_render):
            r = self.client.post("/api/reports/export/pdf", json=payload)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertNotIn(["<b>forged</b>"], captured["rows"])
            self.assertNotEqual(captured["columns"], ["Evil"])
            direct = self.client.get("/api/reports/people", params=self._range()).json()
            self.assertEqual(captured["rows"], direct["rows"])
            # unknown report / inverted range
            self.assertEqual(self.client.post("/api/reports/export/pdf",
                                              json={**payload, "report_type": "nope"}).status_code, 422)
            inverted = {"from_date": TODAY.isoformat(),
                        "to_date": (TODAY - datetime.timedelta(days=3)).isoformat()}
            self.assertEqual(self.client.post("/api/reports/export/pdf",
                                              json={**payload, "params": inverted}).status_code, 422)
            # the payroll report through export keeps the H-15 gate
            self.as_("manager")
            r = self.client.post("/api/reports/export/pdf",
                                 json={**payload, "report_type": "payroll"})
            self.assertIn(r.status_code, (403,), r.text)


# ── Compliance settings / email kinds ──────────────────────────────────────

class ComplianceSettingsTests(_Http):
    def test_m17_manager_cannot_change_or_run(self):
        self.as_("manager")
        self.assertFalse(self.client.get("/api/compliance/settings").json()["can_edit"])
        r = self.client.put("/api/compliance/settings", json={"enabled": True})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.client.post("/api/compliance/run").status_code, 403)
        self.as_("owner")
        self.assertTrue(self.client.get("/api/compliance/settings").json()["can_edit"])

    def test_l04_email_kinds_requires_login(self):
        main.app.dependency_overrides.clear()
        anon = TestClient(main.app)
        self.assertIn(anon.get("/api/email-templates/kinds").status_code, (401, 403))


# ── Travel & expenses ──────────────────────────────────────────────────────

class TravelExpenseTests(_Http):
    def _travel(self, **over):
        me = self.actor.employee_id
        body = {"employee_id": str(me), "purpose": "QA trip", "destination": "Pune",
                "from_date": (TODAY + datetime.timedelta(days=200)).isoformat(),
                "to_date": (TODAY + datetime.timedelta(days=202)).isoformat(),
                "travel_mode": "flight", "estimated_cost": 5000}
        body.update(over)
        return self.client.post("/api/travel-requests", json=body)

    def _expense(self, reference="QA exp", items=None, submitted=None):
        me = self.actor.employee_id
        items = items or [{"date": TODAY.isoformat(), "category": "meals", "description": "lunch", "amount": 250}]
        return self.client.post("/api/expense-reports", json={
            "employee_id": str(me), "reference": reference,
            "submitted_date": (submitted or TODAY).isoformat(), "items": items})

    def test_m18_m19_l19_bounds_and_windows(self):
        self.as_("employee")
        self.assertEqual(self._travel(estimated_cost=1e11).status_code, 422)
        self.assertEqual(self._travel(estimated_cost=-100).status_code, 422)
        past = (TODAY - datetime.timedelta(days=10)).isoformat()
        self.assertEqual(self._travel(from_date=past, to_date=past).status_code, 422)
        self.assertEqual(self._expense(items=[{"date": TODAY.isoformat(), "category": "c", "description": "d",
                                               "amount": 1e14}]).status_code, 422)
        big = {"date": TODAY.isoformat(), "category": "c", "description": "d", "amount": 6e11}
        self.assertEqual(self._expense(items=[big, big]).status_code, 422)  # total overflow
        future = (TODAY + datetime.timedelta(days=10)).isoformat()
        self.assertEqual(self._expense(items=[{"date": future, "category": "c", "description": "d",
                                               "amount": 10}]).status_code, 422)
        from pydantic import ValidationError

        from app import schemas
        for amount, day in ((1e14, TODAY), (10, TODAY + datetime.timedelta(days=10))):
            with self.assertRaises(ValidationError):
                schemas.ReimbursementCreate(employee_id=self.actor.employee_id, category="x",
                                            amount=amount, expense_date=day)

    def test_l20_overlapping_travel_is_409(self):
        self.as_("employee")
        first = self._travel()
        self.assertEqual(first.status_code, 201, first.text)
        clash = self._travel(from_date=(TODAY + datetime.timedelta(days=201)).isoformat(),
                             to_date=(TODAY + datetime.timedelta(days=205)).isoformat())
        self.assertEqual(clash.status_code, 409, clash.text)
        later = self._travel(from_date=(TODAY + datetime.timedelta(days=210)).isoformat(),
                             to_date=(TODAY + datetime.timedelta(days=211)).isoformat())
        self.assertEqual(later.status_code, 201, later.text)

    def test_m21_several_reports_per_day_and_double_submit_replay(self):
        self.as_("employee")
        a = self._expense("QA report A")
        b = self._expense("QA report B")
        self.assertEqual((a.status_code, b.status_code), (201, 201), a.text + b.text)
        self.assertNotEqual(a.json()["claim_no"], b.json()["claim_no"])
        again = self._expense("QA report A")
        self.assertIn(again.status_code, (200, 201), again.text)
        self.assertEqual(again.json()["id"], a.json()["id"])
        self.assertTrue(again.json().get("duplicate"))
        rows = self.db.scalars(select(models.ExpenseClaim.id).where(
            models.ExpenseClaim.employee_id == self.actor.employee_id,
            models.ExpenseClaim.purpose == "QA report A")).all()
        self.assertEqual(len(rows), 1)

    def test_m27_travel_expense_level_decides_list_scope(self):
        for key in ("payroll", "hr", "manager"):
            user = self.as_(key)
            level = crud.effective_user_matrix(self.db, user).get("travel_expense_approval")
            ids = crud.get_visible_employee_ids_for_requests(self.db, user, "expense_claim")
            if level in ("a", "v"):
                self.assertIsNone(ids, f"{key} ({level}) should see every expense report")
            if key == "manager" and level == "e":
                self.assertIsNotNone(ids, "Manager keeps the team scope")
        with mock.patch.object(crud, "effective_user_matrix", return_value={"travel_expense_approval": "a"}):
            user = self.as_("manager")
            self.assertIsNone(crud.get_visible_employee_ids_for_requests(self.db, user, "expense_claim"))
            self.assertIsNone(crud.get_visible_employee_ids_for_requests(self.db, user, "travel_request"))


# ── Pure unit tests (no DB) ────────────────────────────────────────────────

class SanitiserTests(unittest.TestCase):
    def test_m34_allowlist(self):
        attacks = [
            '<a href="&#106;avascript:alert(1)">x</a>',
            '<a href="java\tscript:alert(1)">x</a>',
            '<object data="x.swf"></object>',
            '<meta http-equiv="refresh" content="0;url=https://evil">',
            '<img src=x onerror=alert(1)>',
            '<a href="data:text/html,<script>alert(1)</script>">d</a>',
            '<svg><script>alert(1)</script></svg>',
        ]
        for a in attacks:
            self.assertIsNotNone(tr.validate_template_html(a, ""), a)
            out = tr.sanitize_template_html(a).lower()
            for bad in ("javascript", "onerror", "<object", "<meta", "<script", "data:text"):
                self.assertNotIn(bad, out, a)
        ok = ('<div class="payslip" data-tpl="x" style="color:red"><img src="{{company_logo}}" alt="l">'
              '<table cellpadding="4" width="100%"><tr><td colspan="2">{{employee_name}}</td></tr></table></div>')
        self.assertIsNone(tr.validate_template_html(ok, ".a{color:red}"))
        self.assertEqual(tr.sanitize_template_html(ok), ok.replace("<tr>", "<tbody><tr>").replace("</tr>", "</tr></tbody>"))
        rendered = tr.render_template_html(ok, "</style><script>x</script>", {"employee_name": "<b>A</b>"})
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;b&gt;A&lt;/b&gt;", rendered)
        self.assertIn('src="data:image/png', tr.sanitize_template_html('<img src="data:image/png;base64,AAAA">'))


class ConfigTests(unittest.TestCase):
    def _s(self, **kw):
        base = {"database_url": "postgresql://x/y", "jwt_secret": "x" * 40}
        return config.Settings(_env_file=None, **{**base, **kw})

    def test_m41_production_enforces_explicit_origins(self):
        with self.assertRaises(RuntimeError):
            self._s(app_env="production", cors_origins="*").validate_for_startup()
        with self.assertRaises(RuntimeError):
            self._s(app_env="production", cors_origins="https://a.example.com/path").validate_for_startup()
        self._s(app_env="production",
                cors_origins="https://hrms.example.com,https://admin.example.com:8443").validate_for_startup()
        self._s(app_env="dev", cors_origins="*").validate_for_startup()  # dev only warns

    def test_l37_docs_only_in_dev(self):
        self.assertTrue(self._s(app_env="dev").docs_enabled)
        self.assertFalse(self._s(app_env="production").docs_enabled)
        self.assertTrue(self._s(app_env="production", api_docs_enabled=True).docs_enabled)
        self.assertEqual(main.app.docs_url is not None, app.config.settings.docs_enabled)

    def test_l36_security_headers(self):
        client = TestClient(main.app)
        r = client.get("/health")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertIn("default-src 'none'", r.headers.get("content-security-policy", ""))
        self.assertEqual(r.headers.get("referrer-policy"), "no-referrer")
        m = client.get("/media/does/not/exist.png")
        self.assertIn(m.status_code, (401, 404))
        self.assertIn("img-src 'self'", m.headers.get("content-security-policy", ""))
        self.assertEqual(m.headers.get("x-frame-options"), "DENY")
        https = TestClient(main.app, base_url="https://testserver")
        self.assertIn("max-age=", https.get("/health").headers.get("strict-transport-security", ""))

    def test_l38_dev_default_allow_list(self):
        s = app.config.settings
        with mock.patch.object(s, "app_env", "dev"), mock.patch.object(s, "email_allowed_domains", ""), \
                mock.patch.object(s, "email_from_address", "info@impacgo.com"):
            self.assertEqual(email_service.allowed_domains(), {"impacgo.com"})
            self.assertFalse(email_service.is_allowed_recipient("someone@gmail.com"))
        with mock.patch.object(s, "app_env", "dev"), mock.patch.object(s, "email_allowed_domains", "*"):
            self.assertTrue(email_service.is_allowed_recipient("someone@gmail.com"))
        with mock.patch.object(s, "app_env", "production"), mock.patch.object(s, "email_allowed_domains", ""):
            self.assertTrue(email_service.is_allowed_recipient("someone@gmail.com"))

    def test_n08_cli_trial(self):
        db = mock.MagicMock()
        with mock.patch.object(crud, "get_or_create_platform_settings",
                               return_value=mock.Mock(trial_days=14)):
            f = provision_tenant.cli_plan_fields(db)
        self.assertEqual(f["status"], "trial")
        self.assertEqual(f["trial_ends_at"], TODAY + datetime.timedelta(days=14))
        self.assertIsNone(provision_tenant.cli_plan_fields(db, "yearly")["trial_ends_at"])


if __name__ == "__main__":
    unittest.main()
