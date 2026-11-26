"""Freelancer onboarding: per-company Freelancer defaults (tenant-isolated),
rate card currency + bank holder / branch on Add Employee, NDA / Agreement
templates (predefined ones seeded per company) and first-login acceptance.
Runs inside a transaction that is always rolled back.

    cd backend && venv/Scripts/python -m unittest tests.test_freelancer_onboarding -v
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app import agreements, crud, models, schemas  # noqa: E402
from app.routers import employees as emp_api  # noqa: E402
from app.routers import freelancer_onboarding as fo  # noqa: E402
from tests.test_employee_access_lifecycle import _Base  # noqa: E402
from tests.test_people_visibility import PeopleTestBase  # noqa: E402


class _Req:
    """Minimal stand-in for fastapi.Request (client host + headers)."""

    class client:
        host = "127.0.0.1"

    headers = {"user-agent": "unittest"}


class FreelancerOnboardingTests(PeopleTestBase):
    def setUp(self):
        super().setUp()
        self.owner = self.users["owner"]
        self.cid = self.owner.company_id
        # A clean slate for this company's templates / defaults (rolled back).
        self.db.execute(delete(models.AgreementAcceptance).where(models.AgreementAcceptance.company_id == self.cid))
        self.db.execute(delete(models.AgreementTemplate).where(models.AgreementTemplate.company_id == self.cid))
        self.db.execute(delete(models.FreelancerDefaults).where(models.FreelancerDefaults.company_id == self.cid))
        self.db.flush()

    def code(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code

    def _role_name(self):
        return self.db.scalar(select(models.Role.name).where(
            models.Role.company_id == self.cid, models.Role.name != "Organization Owner / CEO").limit(1))

    def _create_freelancer(self, **kw):
        n = uuid.uuid4().hex[:8]
        payload = dict(
            first_name="Fl", last_name=f"Test{n}", work_email=f"fl.{n}@example.com",
            role_name=self._role_name(), password=f"Fl-{uuid.uuid4().hex}!9aZ", employment_type="Freelancer",
            rate_amount=40, rate_unit="hourly", rate_currency="usd",
            bank_name="HDFC Bank", bank_account_no="012345678901", bank_ifsc="HDFC0001234",
            bank_account_holder="Fl Test", bank_branch="Koramangala",
        )
        payload.update(kw)
        out = emp_api.create_employee(schemas.EmployeeCreate(**payload), db=self.db, current_user=self.owner)
        return self.db.get(models.Employee, out.id if hasattr(out, "id") else out["id"])

    # -- defaults --
    def test_defaults_are_per_company_and_validated(self):
        out = fo.get_freelancer_defaults(db=self.db, current_user=self.owner)
        self.assertFalse(out.configured)
        self.assertEqual(out.employment_status, "Active")

        role = self._role_name()
        band = self.db.scalar(select(models.Band.band_number).where(
            models.Band.company_id == self.cid, models.Band.is_active.is_(True)).limit(1))
        saved = fo.put_freelancer_defaults(
            fo.FreelancerDefaultsIn(band_number=band, role_name=role, reporting_manager_id=self.owner.employee_id,
                                    employment_status="active", rate_currency="eur", rate_unit="daily"),
            db=self.db, current_user=self.owner)
        self.assertTrue(saved.configured)
        self.assertEqual((saved.role_name, saved.rate_currency, saved.employment_status), (role, "EUR", "Active"))
        self.assertEqual(saved.reporting_manager_id, self.owner.employee_id)
        self.assertIsNotNone(saved.reporting_manager_name)

        bad = [
            fo.FreelancerDefaultsIn(role_name="No Such Role xyz"),
            fo.FreelancerDefaultsIn(rate_currency="ZZZ"),
            fo.FreelancerDefaultsIn(branch_name="No Such Branch xyz"),
            fo.FreelancerDefaultsIn(employment_status="Terminated"),
            fo.FreelancerDefaultsIn(reporting_manager_id=uuid.uuid4()),
        ]
        for payload in bad:
            self.assertEqual(self.code(fo.put_freelancer_defaults, payload, db=self.db, current_user=self.owner), 422)

    # -- create --
    def test_create_freelancer_saves_rate_currency_and_bank(self):
        e = self._create_freelancer()
        self.assertEqual((float(e.contract_rate_amount), e.contract_rate_unit, e.contract_rate_currency),
                         (40.0, "hourly", "USD"))
        self.assertEqual((e.bank_account_holder, e.bank_branch), ("Fl Test", "Koramangala"))
        rate = crud.employee_rate_out(self.db, e, True)
        self.assertEqual(rate["currency_code"], "USD")
        self.assertEqual(rate["history"][0]["currency_code"], "USD")
        payroll = crud.get_employee_payroll(self.db, e.id)
        self.assertEqual((payroll["accountHolder"], payroll["bankBranch"]), ("Fl Test", "Koramangala"))

        # Default currency when none is given; unknown currency rejected.
        e2 = self._create_freelancer(rate_currency=None)
        self.assertEqual(crud.employee_rate_currency(self.db, e2), crud.company_currency(self.db, self.cid))
        self.assertEqual(self.code(self._create_freelancer, rate_currency="XYZ"), 422)

    # -- templates --
    def test_builtin_templates_seeded_once_with_one_active_per_type(self):
        rows = fo.list_agreement_templates(doc_type=None, db=self.db, current_user=self.owner)
        by_type = {}
        for r in rows:
            by_type.setdefault(r.doc_type, []).append(r)
        self.assertEqual({k: len(v) for k, v in by_type.items()}, {"nda": 4, "agreement": 4})
        for v in by_type.values():
            self.assertEqual(sum(1 for r in v if r.is_active), 1)
            self.assertTrue(all(r.is_builtin for r in v))
        # Not re-seeded.
        self.assertEqual(len(fo.list_agreement_templates(doc_type="nda", db=self.db, current_user=self.owner)), 4)
        # Preview renders sample values; scripts are refused.
        nda = by_type["nda"][0]
        html = fo.preview_agreement_template(nda.id, db=self.db, current_user=self.owner).rendered_html
        self.assertIn("Priya Sharma", html)
        self.assertNotIn("{{", html)
        self.assertEqual(self.code(fo.preview_agreement_html, fo.PreviewIn(html_body="<script>x</script>"),
                                   db=self.db, current_user=self.owner), 422)
        # The active template can't be deleted.
        active = next(r for r in by_type["nda"] if r.is_active)
        self.assertEqual(self.code(fo.delete_agreement_template, active.id, db=self.db, current_user=self.owner), 409)

    # -- first-login acceptance --
    def test_freelancer_must_accept_active_nda_and_agreement(self):
        e = self._create_freelancer()
        user = self.db.scalar(select(models.User).where(models.User.employee_id == e.id))
        mine = fo.my_agreements(db=self.db, current_user=user)
        self.assertTrue(mine.required)
        self.assertEqual([p.doc_type for p in mine.pending], ["nda", "agreement"])
        self.assertIn(e.first_name, mine.pending[0].rendered_html)
        self.assertIn("USD 40.00 per hour", mine.pending[1].rendered_html)

        nda = mine.pending[0]
        self.assertEqual(self.code(fo.accept_my_agreement, nda.template_id, fo.AcceptIn(version=nda.version + 1),
                                   _Req(), db=self.db, current_user=user), 409)
        mine = fo.accept_my_agreement(nda.template_id, fo.AcceptIn(version=nda.version), _Req(),
                                      db=self.db, current_user=user)
        self.assertEqual([p.doc_type for p in mine.pending], ["agreement"])
        agr = mine.pending[0]
        mine = fo.accept_my_agreement(agr.template_id, fo.AcceptIn(version=agr.version), _Req(),
                                      db=self.db, current_user=user)
        self.assertEqual(mine.pending, [])
        self.assertEqual(len(mine.accepted), 2)
        stored = self.db.scalar(select(models.AgreementAcceptance).where(
            models.AgreementAcceptance.employee_id == e.id, models.AgreementAcceptance.doc_type == "nda"))
        self.assertEqual((stored.template_version, stored.ip_address), (nda.version, "127.0.0.1"))
        self.assertIn(e.first_name, stored.rendered_html)

        # HR sees the history; an accepted template is kept.
        hist = fo.employee_agreements(e.id, db=self.db, current_user=self.owner)
        self.assertEqual(len(hist), 2)
        # Editing the content -> new version -> must be accepted again.
        fo.update_agreement_template(nda.template_id, fo.AgreementTemplateUpdate(html_body="<p>v2 {{ employee_name }}</p>"),
                                     db=self.db, current_user=self.owner)
        mine = fo.my_agreements(db=self.db, current_user=user)
        self.assertEqual([(p.doc_type, p.version) for p in mine.pending], [("nda", nda.version + 1)])
        # A rename alone doesn't.
        fo.update_agreement_template(agr.template_id, fo.AgreementTemplateUpdate(name="Renamed"),
                                     db=self.db, current_user=self.owner)
        self.assertEqual(len(fo.my_agreements(db=self.db, current_user=user).pending), 1)

    def test_non_freelancers_have_nothing_to_accept(self):
        for key in ("owner", "hr", "employee"):
            out = fo.my_agreements(db=self.db, current_user=self.users[key])
            self.assertFalse(out.required)
            self.assertEqual(out.pending, [])
        e = self.db.get(models.Employee, self.users["employee"].employee_id)
        tpl = agreements.list_templates(self.db, self.cid)
        if tpl:
            self.assertEqual(self.code(fo.accept_my_agreement, tpl[0].id, fo.AcceptIn(version=1), _Req(),
                                       db=self.db, current_user=self.users["employee"]), 409)
        self.assertFalse(agreements.requires_acceptance(e))


class ImpacgoDefaultsIsolationTests(_Base):
    """The Impacgo Solutions defaults (db/impacgo_freelancer_defaults.sql)
    belong to that tenant only; other tenants never see them."""

    tenant = "impacgo-solutions"

    def _any_user(self):
        user = self.db.scalar(select(models.User).where(
            models.User.status == "active", models.User.employee_id.is_not(None)).limit(1))
        if user is None:
            self.skipTest(f"no active user in {self.tenant}")
        return user

    def test_impacgo_defaults_resolved_by_its_own_data(self):
        user = self._any_user()
        out = fo.get_freelancer_defaults(db=self.db, current_user=user)
        if not out.configured:
            self.skipTest("impacgo_freelancer_defaults.sql not applied to this database")
        self.assertEqual((out.band_number, out.role_name, out.employment_status), (10, "Freelancer", "Active"))
        self.assertIn("Srikar", out.reporting_manager_name or "")
        mgr = self.db.get(models.Employee, out.reporting_manager_id)
        self.assertEqual(mgr.company_id, user.company_id)


class OtherTenantGetsNoImpacgoDefaultsTests(ImpacgoDefaultsIsolationTests):
    tenant = "acme"

    def test_impacgo_defaults_resolved_by_its_own_data(self):  # not applicable here
        pass

    def test_no_impacgo_values_leak(self):
        user = self._any_user()
        out = fo.get_freelancer_defaults(db=self.db, current_user=user)
        row = self.db.get(models.FreelancerDefaults, user.company_id)
        if row is None:
            self.assertFalse(out.configured)
            self.assertIsNone(out.role_name)
            self.assertIsNone(out.reporting_manager_id)
        self.assertNotIn("Srikar", out.reporting_manager_name or "")


if __name__ == "__main__":
    unittest.main()
