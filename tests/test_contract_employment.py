"""Contract / contractor employment support, phase by phase, through the
real HTTP API on top of the recruitment-workflow harness
(tests/test_recruitment_workflow.py -- same tenant, same always-rolled-back
transaction, uploads to a temp folder, no email sent).

Every phase has two halves:
  * the new contract behaviour, and
  * proof that full-time / part-time / intern behaviour is unchanged
    (identical validation, identical stored values, byte-identical offer
    letter placeholders against a frozen pre-change copy of the renderer).

    cd backend && venv/Scripts/python -m unittest tests.test_contract_employment -v
"""

from __future__ import annotations

import calendar
import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crud, models, offer_letter_html_renderer as letter  # noqa: E402
from app import recruitment_onboarding as onb  # noqa: E402

import tests.test_recruitment_workflow as rwt  # noqa: E402  (module, so its tests aren't re-collected here)
from tests.baselines import offer_letter_renderer_pre_contract as letter_v0  # noqa: E402

NON_CONTRACT_TYPES = ("Full-time", "Part-time", "Intern")


def _add_months(d: datetime.date, months: int) -> datetime.date:
    return onb._add_months(d, months)


class _Harness(rwt.RecruitmentWorkflowTests):
    """The recruitment harness without its own test_* methods."""


for _name in [n for n in dir(rwt.RecruitmentWorkflowTests) if n.startswith("test_")]:
    setattr(_Harness, _name, None)


class _ContractBase(_Harness):
    def opening_of_type(self, employment_type: str, title: str = "Integration Consultant", stages=None) -> dict:
        body = {
            "title": title, "department_id": str(self.dept.id), "branch_id": str(self.branch.id), "vacancies": 1,
            "description": "Client integration work.", "employment_type": employment_type, "work_mode": "Remote",
            "reporting_manager_id": str(self.owner.employee_id), "hiring_team": [str(self.hr.employee_id)],
        }
        if stages is not None:
            body["interview_stages"] = stages
        o = self.call("POST", "/job-openings", body, expect=201)
        return self.call("POST", f"/job-openings/{o['id']}/publish")

    def selected_application(self, employment_type: str) -> str:
        opening = self.opening_of_type(employment_type)
        app_id, _cand = self.make_application(opening["id"], name="Kiran Rao")
        self.to_selected(app_id)
        return app_id


class ContractOfferTests(_ContractBase):
    """Phase 1 -- the offer stage."""

    # ── contract offers ───────────────────────────────────────────────────
    def test_contract_offer_is_made_on_a_rate_not_a_ctc(self):
        app_id = self.selected_application("Contract")
        joining = datetime.date.today() + datetime.timedelta(days=10)
        base = {"application_id": app_id, "joining_date": joining.isoformat()}

        # No CTC needed -- but the contract terms are.
        r = self.call("POST", "/offers", base, expect=422)
        self.assertIn("contract rate", r["detail"].lower() if isinstance(r["detail"], str) else str(r["detail"]).lower())
        self.call("POST", "/offers", {**base, "rate_amount": 1500, "rate_unit": "daily"}, expect=422)  # no end date
        self.call("POST", "/offers", {**base, "rate_amount": 1500, "rate_unit": "weekly",
                                      "contract_duration_months": 6}, expect=422)  # bad unit
        self.call("POST", "/offers", {**base, "rate_amount": 0, "rate_unit": "daily",
                                      "contract_duration_months": 6}, expect=422)  # non-positive rate
        self.call("POST", "/offers", {**base, "rate_amount": 1500, "rate_unit": "daily",
                                      "contract_end_date": joining.isoformat()}, expect=422)  # end not after joining

        # Duration only -> end date derived (joining + 6 months - 1 day).
        o = self.call("POST", "/offers", {**base, "rate_amount": 1500, "rate_unit": "daily",
                                          "contract_duration_months": 6}, expect=201)
        self.assertEqual(o["employment_type"], "contract")
        self.assertEqual(o["compensation_type"], "rate")
        self.assertEqual((o["rate_amount"], o["rate_unit"], o["contract_duration_months"]), (1500, "daily", 6))
        self.assertEqual(o["contract_end_date"], (_add_months(joining, 6) - datetime.timedelta(days=1)).isoformat())
        self.assertIsNone(o["probation_months"])
        self.assertIsNone(o["notice_period_days"])

        # The full approval path works with no CTC / probation / notice.
        o = self.call("POST", f"/offers/{o['id']}/submit", {})
        self.assertEqual(o["status"], "approval_pending")
        o = self.call("POST", f"/offers/{o['id']}/decision", {"decision": "approve"}, as_=self.owner)
        o = self.call("POST", f"/offers/{o['id']}/send", {"email": True})
        self.assertEqual(o["status"], "sent")

        row = self.db.get(models.Offer, uuid.UUID(o["id"]))
        self.assertEqual(row.compensation_type, "rate")
        self.assertEqual(float(row.offered_ctc), 0.0)

    def test_contract_offer_letter_renders_contract_terms(self):
        app_id = self.selected_application("Contract")
        joining = datetime.date.today() + datetime.timedelta(days=7)
        end = joining + datetime.timedelta(days=180)
        o = self.call("POST", "/offers", {"application_id": app_id, "joining_date": joining.isoformat(),
                                          "rate_amount": 85000, "rate_unit": "monthly", "contract_duration_months": 6,
                                          "contract_end_date": end.isoformat()}, expect=201)
        company = self.db.get(models.Company, self.company_id)
        ph = letter.build_offer_letter_placeholders(crud.get_offer_detail(self.db, uuid.UUID(o["id"]), company.id), company)
        self.assertEqual(ph["compensation_type"], "Contract rate")
        self.assertEqual((ph["contract_rate"], ph["contract_rate_unit"]), ("85,000.00", "per month"))
        self.assertEqual((ph["contract_duration"], ph["contract_end_date"]), ("6 months", end.isoformat()))
        html = letter.render_offer_letter_html("<div>{{compensation_terms}}</div>", "", ph)
        self.assertIn("Contract rate", html)
        self.assertIn("Contract end date", html)
        self.assertNotIn("Annual CTC", html)
        self.assertNotIn("Probation period", html)
        self.assertNotIn("&lt;table", html)  # block inserted as markup, not escaped

    def default_template(self):
        tpl = crud.get_offer_letter_html_template(self.db, self.company_id)
        if tpl is None:
            self.skipTest("tenant has no offer letter template")
        return tpl

    def test_contract_offer_on_the_default_ctc_template(self):
        tpl = self.default_template()
        app_id = self.selected_application("Contract")
        joining = datetime.date.today() + datetime.timedelta(days=7)
        end = joining + datetime.timedelta(days=90)
        o = self.call("POST", "/offers", {"application_id": app_id, "joining_date": joining.isoformat(),
                                          "rate_amount": 1800, "rate_unit": "daily", "contract_duration_months": 3,
                                          "contract_end_date": end.isoformat()}, expect=201)
        company = self.db.get(models.Company, self.company_id)
        ph = letter.build_offer_letter_placeholders(crud.get_offer_detail(self.db, uuid.UUID(o["id"]), company.id), company)
        html = letter.render_offer_letter_html(tpl.html_body, tpl.css_styles, ph)
        self.assertIn("Contract Rate", html)
        self.assertIn("₹1,800.00 per day", html)
        self.assertIn(end.isoformat(), html)
        self.assertNotIn("Annual CTC", html)
        self.assertNotIn("0.00 (", html)  # no zero CTC anywhere

    def test_non_contract_offer_on_the_default_template_is_byte_identical(self):
        tpl = self.default_template()
        app_id = self.selected_application("Full-time")
        o = self.call("POST", "/offers", {"application_id": app_id, "offered_ctc": 1500000,
                                          "joining_date": (datetime.date.today() + datetime.timedelta(days=9)).isoformat()},
                      expect=201)
        company = self.db.get(models.Company, self.company_id)
        detail = crud.get_offer_detail(self.db, uuid.UUID(o["id"]), company.id)
        new = letter.render_offer_letter_html(tpl.html_body, tpl.css_styles,
                                              letter.build_offer_letter_placeholders(detail, company))
        old = letter_v0.render_offer_letter_html(tpl.html_body, tpl.css_styles,
                                                 letter_v0.build_offer_letter_placeholders(detail, company))
        self.assertEqual(new, old)
        self.assertIn("Annual CTC", new)

    # ── non-contract offers: provably unchanged ───────────────────────────
    def test_non_contract_offers_are_unchanged(self):
        for et in NON_CONTRACT_TYPES:
            with self.subTest(employment_type=et):
                app_id = self.selected_application(et)
                joining = datetime.date.today() + datetime.timedelta(days=20)
                # Same rule as before: CTC is required, nothing else is.
                r = self.call("POST", "/offers", {"application_id": app_id, "joining_date": joining.isoformat()},
                              expect=422)
                self.assertEqual(r["detail"], "Offered CTC is required.")
                o = self.call("POST", "/offers", {"application_id": app_id, "offered_ctc": 1200000,
                                                  "joining_date": joining.isoformat(), "probation_months": 6,
                                                  "notice_period_days": 60}, expect=201)
                self.assertEqual(o["compensation_type"], "ctc")
                for k in ("rate_amount", "rate_unit", "contract_duration_months", "contract_end_date"):
                    self.assertIsNone(o[k], k)
                o = self.call("POST", f"/offers/{o['id']}/submit", {})
                self.assertEqual(o["status"], "approval_pending")
                row = self.db.get(models.Offer, uuid.UUID(o["id"]))
                self.assertEqual((row.compensation_type, row.rate_amount, row.rate_unit, row.contract_end_date),
                                 ("ctc", None, None, None))

                # Offer letter: every placeholder that existed before has the
                # exact same value as the frozen pre-change renderer.
                company = self.db.get(models.Company, self.company_id)
                detail = crud.get_offer_detail(self.db, row.id, company.id)
                new, old = (letter.build_offer_letter_placeholders(detail, company),
                            letter_v0.build_offer_letter_placeholders(detail, company))
                self.assertEqual({k: new[k] for k in old}, old)
                tpl = "".join(f"<p>{{{{{k}}}}}</p>" for k in old)
                self.assertEqual(letter.render_offer_letter_html(tpl, "p{}", new),
                                 letter_v0.render_offer_letter_html(tpl, "p{}", old))
                self.assertIn("Annual CTC", new["compensation_terms"])
                self.assertEqual(new["contract_rate"], "—")


class _HireMixin:
    """Offer -> accept -> preboarding -> verified -> joining -> employee."""

    def hire(self, employment_type: str, offer_terms: dict, name: str = "Kiran Rao") -> tuple[dict, dict]:
        opening = self.opening_of_type(employment_type, title=f"{employment_type} role {uuid.uuid4().hex[:4]}", stages=[])
        app_id, _ = self.make_application(opening["id"], name=name)
        self.act(app_id, "start_screening")
        self.act(app_id, "shortlist")
        self.act(app_id, "select")  # opening has no interview stages configured -> straight to selected
        today = datetime.date.today()
        o = self.call("POST", "/offers", {"application_id": app_id, "joining_date": today.isoformat(),
                                          **offer_terms}, expect=201)
        self.call("POST", f"/offers/{o['id']}/submit", {})
        self.call("POST", f"/offers/{o['id']}/decision", {"decision": "approve"}, as_=self.owner)
        self.call("POST", f"/offers/{o['id']}/send", {"email": True})
        self.call("POST", f"/offers/{o['id']}/accept", {"source": "email"})
        pb_id = self.call("GET", f"/applications/{app_id}")["preboarding"]["id"]
        self.call("POST", f"/preboarding/{pb_id}/send-tasks", {"email": False})
        first, last = name.split(" ", 1)
        self.call("PUT", f"/preboarding/{pb_id}/details", {
            "personal": {"first_name": first, "last_name": last, "gender": "Male", "date_of_birth": "1994-03-02"},
            "contact": {"personal_phone": "9876500021", "current_address": "Hyderabad"},
            "emergency": {"name": "Sita Rao", "relation": "Mother", "phone": "9876500022"},
            "bank": {"bank_name": "HDFC", "account_no": "123456789012", "ifsc": "HDFC0001234", "account_holder": name}})
        pb = self.call("GET", f"/preboarding/{pb_id}")
        for t in [t for t in pb["tasks"] if t["task_type"] == "document" and t["required"]]:
            pb = self.call("POST", f"/preboarding/tasks/{t['id']}/documents",
                           files={"file": (f"{t['name']}.pdf", rwt.PDF, "application/pdf")}, expect=201)
            doc = next(x for x in pb["tasks"] if x["id"] == t["id"])["documents"][0]
            self.call("POST", f"/preboarding/documents/{doc['id']}/review", {"decision": "approve"})
        for t in self.call("GET", f"/preboarding/{pb_id}")["tasks"]:
            if t["task_type"] == "acknowledgement" and t["status"] != "completed":
                self.call("PATCH", f"/preboarding/tasks/{t['id']}", {"status": "completed"})
            if t["task_type"] == "verification" and t["required"]:
                self.call("PATCH", f"/preboarding/tasks/{t['id']}", {"status": "verified"})
        self.call("POST", f"/preboarding/{pb_id}/confirm-joining", {"joining_date": today.isoformat()})
        prefill = self.call("GET", f"/preboarding/{pb_id}/employee-prefill")
        done = self.call("POST", f"/preboarding/{pb_id}/create-employee", {
            "work_email": f"{first.lower()}.{uuid.uuid4().hex[:6]}@impacgo.com", "role_name": self.role_name,
            "password": "Joiner#Strong42"})
        return prefill, done["result"]


class ContractEmployeeRecordTests(_HireMixin, _ContractBase):
    """Phase 2 -- the employee record."""

    def test_contract_hire_carries_the_contract_end_date(self):
        end = datetime.date.today() + datetime.timedelta(days=200)
        prefill, result = self.hire("Contract", {"rate_amount": 2000, "rate_unit": "daily",
                                                 "contract_end_date": end.isoformat()})
        self.assertEqual(prefill["contract_end_date"], end.isoformat())
        self.assertEqual(prefill["employment_type"], "Contract")
        emp = self.db.get(models.Employee, uuid.UUID(result["employee_id"]))
        self.assertEqual(emp.contract_end_date, end)
        self.assertTrue(crud.is_contract_employee(emp))

    def test_non_contract_hire_record_is_unchanged(self):
        for et in NON_CONTRACT_TYPES:
            with self.subTest(employment_type=et):
                prefill, result = self.hire(et, {"offered_ctc": 900000, "probation_months": 6})
                self.assertNotIn("contract_end_date", prefill)  # prefill identical to before
                emp = self.db.get(models.Employee, uuid.UUID(result["employee_id"]))
                self.assertIsNone(emp.contract_end_date)
                self.assertFalse(crud.is_contract_employee(emp))

    def test_contract_test_matches_the_gratuity_rule(self):
        # Same answer as the existing exit / F&F gratuity rule for every
        # spelling stored today.
        for et in ("Contract", "contract", "Fixed-Term", "fixed term", "Full-time", "Full Time", "full_time",
                   "Part-time", "part_time", "Intern", "", None):
            gratuity_rule = (et or "").lower().replace("-", " ") in ("fixed term", "contract")
            self.assertEqual(crud.is_contract_employee(type("E", (), {"employment_type": et})()), gratuity_rule, et)


# ═══════════════════════════════════════════════════════════════════════════
# Phases 3-5: payroll, leave, contract lifecycle -- tenant with structured
# salaries + PF (Infyq by default, CONTRACT_TEST_TENANT to override), in a
# transaction that is ALWAYS rolled back.
# ═══════════════════════════════════════════════════════════════════════════

import inspect  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import database, reminders  # noqa: E402
from app.database import get_db  # noqa: E402
from app.deps import _OWNER_ROLE_NAME, get_current_user  # noqa: E402
from app.main import app  # noqa: E402

PAYROLL_TENANT = os.environ.get("CONTRACT_TEST_TENANT", "Infyq")


def _without_contract_rules(func, *, payroll: bool = False, leave: bool = False):
    """`func` as it would behave with no contract support at all: the contract
    gates it consults (crud.is_contract_employee for payroll,
    crud.leave_type_applies for leave eligibility) are switched off for the
    call. For a NON-contract employee the result must be identical -- i.e.
    contract support changes nothing for anyone else. (Replaces the old
    source-text surgery, which broke whenever that code was refactored, e.g.
    into app/payroll_period.py and app/leave_policy.py.)"""
    from unittest import mock

    def wrapper(*args, **kwargs):
        patches = []
        if payroll:
            patches.append(mock.patch.object(crud, "is_contract_employee", lambda *_a, **_k: False))
        if leave:
            patches.append(mock.patch.object(crud, "leave_type_applies", lambda *_a, **_k: True))
        for p in patches:
            p.start()
        try:
            return func(*args, **kwargs)
        finally:
            for p in patches:
                p.stop()

    return wrapper


class _TenantTx(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{PAYROLL_TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, PAYROLL_TENANT)
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None),
                                                          models.User.status == "active")).all()
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) and r.name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("tenant needs an Owner")
        self.company_id = self.owner.company_id
        self.user = self.owner
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def api(self, method, path, body=None, expect=200):
        r = self.client.request(method, "/api" + path, json=body)
        self.assertEqual(r.status_code, expect, f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

    def employees_with_structure(self, minimum=25):
        """Active employees with a salary structure. N-10: a tenant with fewer
        (a new tenant has none) gets a structure assigned to more of its
        employees inside the rolled-back transaction."""
        def query():
            return self.db.scalars(
                select(models.Employee).where(
                    models.Employee.company_id == self.company_id, models.Employee.is_active.is_(True),
                    models.Employee.id.in_(select(models.SalaryStructureAssignment.employee_id)),
                ).order_by(models.Employee.employee_code)
            ).all()
        rows = query()
        if len(rows) < minimum:
            self._seed_structures(minimum - len(rows) + 5)
            rows = query()
        return rows

    def _seed_structures(self, count):
        run_start = datetime.date(2026, 10, 1)
        candidates = [
            e for e in self.db.scalars(
                select(models.Employee).where(
                    models.Employee.company_id == self.company_id, models.Employee.is_active.is_(True),
                    models.Employee.id.not_in(select(models.SalaryStructureAssignment.employee_id)),
                ).order_by(models.Employee.employee_code)
            ).all()
            if not crud.is_contract_employee(e) and (e.date_of_joining is None or e.date_of_joining <= run_start)
        ][:count]
        if not candidates:
            return
        tag = uuid.uuid4().hex[:4].upper()
        basic = crud.create_salary_component(self.db, self.company_id, "Basic Fixture", f"BAS{tag}", "earning", "fixed", True)
        hra = crud.create_salary_component(self.db, self.company_id, "HRA Fixture", f"HRA{tag}", "earning", "fixed", True)
        structure = models.SalaryStructure(id=uuid.uuid4(), company_id=self.company_id, name=f"Fixture {tag}",
                                           effective_from=datetime.date(2025, 4, 1), is_active=True)
        self.db.add(structure)
        self.db.flush()
        for comp, amount in ((basic, 30000), (hra, 12000)):
            self.db.add(models.SalaryStructureLine(id=uuid.uuid4(), structure_id=structure.id, component_id=comp.id,
                                                   amount=amount, percent_of=None))
        for e in candidates:
            self.db.add(models.SalaryStructureAssignment(id=uuid.uuid4(), employee_id=e.id, structure_id=structure.id,
                                                         from_date=datetime.date(2025, 4, 1), base_amount=42000,
                                                         annual_ctc=504000,
                                                         created_at=datetime.datetime.now(datetime.timezone.utc)))
        self.db.flush()

    def make_run(self, month=10, year=2026):
        run = models.PayrollRun(id=uuid.uuid4(), company_id=self.company_id, run_no=f"T-{uuid.uuid4().hex[:6]}",
                                period_month=month, period_year=year,
                                from_date=datetime.date(year, month, 1),
                                to_date=datetime.date(year, month, calendar.monthrange(year, month)[1]), status="draft")
        self.db.add(run)
        self.db.flush()
        return run

    def slip_snapshot(self, fn, run, employee):
        """Run a slip computation in a savepoint, return what it produced,
        then roll it back."""
        sp = self.db.begin_nested()
        try:
            slip = fn(self.db, self.company_id, run, employee, crud.get_company_pf_wage_ceiling(self.db, self.company_id),
                      crud.get_company_auto_tds_estimate_enabled(self.db, self.company_id))
            if slip is None:
                return None
            self.db.flush()
            lines = sorted((l.component.code, float(l.amount)) for l in self.db.scalars(
                select(models.SalarySlipLine).where(models.SalarySlipLine.slip_id == slip.id)).all())
            return (float(slip.gross_pay), float(slip.total_deductions), float(slip.net_pay), tuple(lines))
        finally:
            sp.rollback()


class ContractPayrollTests(_TenantTx):
    """Phase 3 -- payroll."""

    def test_contract_employees_get_no_pf_or_esi(self):
        emp = next((e for e in self.employees_with_structure() if not crud.is_contract_employee(e)), None)
        if emp is None:
            self.skipTest("no employee with a salary structure")
        run = self.make_run()
        # Make sure the structure has a statutory PF and an ESI line.
        assignment = self.db.scalar(select(models.SalaryStructureAssignment).where(
            models.SalaryStructureAssignment.employee_id == emp.id).order_by(models.SalaryStructureAssignment.from_date.desc()))
        pf = crud.create_salary_component(self.db, self.company_id, "PF Test", "PF-T", "deduction", "stat_pf", False)
        # A flat ESI line of our own: tenants may already hold a seeded ESI
        # (N-06) whose wage-ceiling rule can drop the line for this salary.
        esi = crud.create_salary_component(self.db, self.company_id, "ESI Test", "ESI-T", "deduction", "flat", False)
        for c in (pf, esi):
            self.db.add(models.SalaryStructureLine(id=uuid.uuid4(), structure_id=assignment.structure_id,
                                                   component_id=c.id, amount=750 if c is esi else None,
                                                   percent_of=None))
        self.db.flush()
        self.db.expire_all()
        full_time = self.slip_snapshot(crud._compute_and_write_slip, run, emp)
        codes = [c for c, _ in full_time[3]]
        self.assertIn("ESI-T", codes)
        emp = self.db.get(models.Employee, emp.id)
        emp.employment_type = "Contract"
        self.db.flush()
        contract = self.slip_snapshot(crud._compute_and_write_slip, run, emp)
        by_code = {c.code: c for c in self.db.scalars(select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == self.company_id)).all()}
        statutory = {code for code, _ in full_time[3] if code in by_code and crud._is_statutory_pf_esi(by_code[code])}
        self.assertIn("ESI-T", statutory)
        self.assertFalse(statutory & {c for c, _ in contract[3]}, contract[3])
        # Everything that isn't PF / ESI is identical (earnings unchanged).
        self.assertEqual([l for l in full_time[3] if l[0] not in statutory], list(contract[3]))
        self.assertEqual(full_time[0], contract[0])  # same gross

    def test_hourly_and_daily_contractors_are_paid_from_approved_timesheets(self):
        emp = next((e for e in self.employees_with_structure() if not crud.is_contract_employee(e)), None)
        project = self.db.scalar(select(models.Project).where(models.Project.company_id == self.company_id).limit(1))
        if emp is None or project is None:
            self.skipTest("needs an employee and a project")
        emp.employment_type = "Contract"
        emp.contract_rate_amount, emp.contract_rate_unit = 500, "hourly"
        run = self.make_run()
        week = datetime.date(2026, 10, 5)  # a Monday inside the run

        def timesheet(status, entries):
            ts = models.Timesheet(id=uuid.uuid4(), company_id=self.company_id, employee_id=emp.id,
                                  week_start=week if status == "approved" else week + datetime.timedelta(days=7),
                                  status=status, total_hours=0, billable_hours=0)
            self.db.add(ts)
            self.db.flush()
            for d, h, st in entries:
                self.db.add(models.WorkEntry(id=uuid.uuid4(), timesheet_id=ts.id, project_id=project.id, entry_date=d,
                                             hours=h, is_billable=True, status=st))
        timesheet("approved", [(week, 8, "approved"), (week + datetime.timedelta(days=1), 4, "approved"),
                               (week + datetime.timedelta(days=2), 3, "rejected")])  # rejected entry excluded
        timesheet("draft", [(week + datetime.timedelta(days=7), 8, "pending")])     # not approved -> excluded
        self.db.flush()

        slip = self.slip_snapshot(crud._compute_and_write_slip, run, emp)
        self.assertEqual(slip[0], 12 * 500.0)               # 12 approved hours x 500
        self.assertEqual(slip[3], (("CONTRACT-FEE", 6000.0),))  # replaces the CTC/12 structure, no PF/ESI
        emp.contract_rate_amount, emp.contract_rate_unit = 3000, "daily"
        slip = self.slip_snapshot(crud._compute_and_write_slip, run, emp)
        self.assertEqual(slip[0], 2 * 3000.0)               # 2 days with approved hours
        emp.contract_rate_unit = "monthly"                    # monthly -> back to the structure (minus PF/ESI)
        slip = self.slip_snapshot(crud._compute_and_write_slip, run, emp)
        self.assertNotIn("CONTRACT-FEE", [c for c, _ in slip[3]])

    def test_payroll_for_non_contract_employees_is_byte_identical(self):
        before = _without_contract_rules(crud._compute_and_write_slip, payroll=True)
        run = self.make_run()
        produced = 0
        for emp in [e for e in self.employees_with_structure() if not crud.is_contract_employee(e)][:30]:
            with self.subTest(employee=emp.employee_code, employment_type=emp.employment_type):
                now, then = self.slip_snapshot(crud._compute_and_write_slip, run, emp), self.slip_snapshot(before, run, emp)
                self.assertEqual(now, then)
                produced += now is not None
        self.assertGreater(produced, 20)  # real payslips compared, not "nothing == nothing"


class ContractLeaveTests(_TenantTx):
    """Phase 4 -- leave eligibility."""

    def leave_types(self):
        return self.db.scalars(select(models.LeaveType).where(models.LeaveType.company_id == self.company_id)).all()

    def test_contract_employees_are_excluded_until_hr_opts_a_type_in(self):
        contractor = self.db.scalar(select(models.Employee).where(
            models.Employee.company_id == self.company_id, models.Employee.is_active.is_(True),
            func_lower(models.Employee.employment_type) == "contract"))
        if contractor is None:
            contractor = next(e for e in self.employees_with_structure())
            contractor.employment_type = "Contract"
            self.db.flush()
        lop = crud.get_lop_leave_type(self.db, self.company_id)
        gated = [lt for lt in self.leave_types() if lop is None or lt.id != lop.id]
        for lt in gated:
            self.assertEqual(crud.get_remaining_leave_balance(self.db, contractor.id, lt), 0.0, lt.name)
            with self.assertRaises(ValueError):
                crud.resolve_leave_request_split(self.db, self.company_id, contractor.id, lt.name, 1)
        if lop is not None:  # Loss of Pay is always available
            self.assertEqual(crud.resolve_leave_request_split(self.db, self.company_id, contractor.id, lop.name, 1)[1], 1)
        # HR opts one type back in for contractors.
        target = gated[0]
        out = self.api("PATCH", f"/leave-types/{target.id}",
                       {"applicable_employment_types": ["full_time", "part_time", "intern", "contract"]})
        self.assertIn("contract", out["applicable_employment_types"])
        self.db.expire_all()
        self.assertNotEqual(crud.get_remaining_leave_balance(self.db, contractor.id, self.db.get(models.LeaveType, target.id)), 0.0)
        self.api("PATCH", f"/leave-types/{target.id}", {"applicable_employment_types": ["weekly"]}, expect=422)
        listed = {t["id"]: t for t in self.api("GET", "/leave-types")}
        self.assertEqual(listed[str(gated[-1].id)]["applicable_employment_types"], ["full_time", "part_time", "intern"])

    def test_leave_balances_api_hides_ineligible_allocations_only(self):
        out = self.api("GET", "/leave-balances?limit=500")
        rows = crud.list_leave_allocations(self.db, self.company_id,
                                           employee_ids=crud.get_visible_employee_ids_for_docs(self.db, self.user),
                                           limit=500, offset=0)
        emp = {e.id: e for e in self.db.scalars(select(models.Employee).where(models.Employee.company_id == self.company_id))}
        expected = {str(la.id) for la, _lt in rows if not crud.is_contract_employee(emp[la.employee_id])}
        hidden = {str(la.id) for la, _lt in rows if crud.is_contract_employee(emp[la.employee_id])}
        self.assertEqual({r["id"] for r in out}, expected)   # every non-contract row, unchanged
        self.assertFalse(hidden & {r["id"] for r in out})    # contractors' allocations hidden

    def test_leave_balances_for_non_contract_employees_are_byte_identical(self):
        before = _without_contract_rules(crud.get_remaining_leave_balance, leave=True)
        employees = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.company_id, models.Employee.is_active.is_(True))
            .order_by(models.Employee.employee_code)).all()
        sample = [e for e in employees if not crud.is_contract_employee(e)]
        sample = sample[:25] + [e for e in sample if crud.employment_type_code(e.employment_type) in ("part_time", "intern")]
        for emp in sample:
            for lt in self.leave_types():
                with self.subTest(employee=emp.employee_code, leave_type=lt.name):
                    self.assertEqual(crud.get_remaining_leave_balance(self.db, emp.id, lt), before(self.db, emp.id, lt))


def func_lower(col):
    from sqlalchemy import func
    return func.lower(col)


class ContractLifecycleTests(_TenantTx):
    """Phase 5 -- reminders, renew, convert."""

    def contractor(self, end_in_days=20):
        # Never the Owner: reminders rightly skip the contractor themselves,
        # and the test asserts the Owner is among the recipients.
        emp = next(e for e in self.employees_with_structure()
                   if not crud.is_contract_employee(e) and e.id != self.owner.employee_id)
        emp.employment_type = "Contract"
        emp.contract_end_date = datetime.date.today() + datetime.timedelta(days=end_in_days)
        emp.contract_rate_amount, emp.contract_rate_unit = 90000, "monthly"
        # commit = release this test's savepoint (the outer transaction is
        # still rolled back in tearDown), so a router's rollback on a
        # refused request doesn't undo the setup.
        self.db.commit()
        return emp

    def reminder_rows(self, emp):
        return self.db.scalars(select(models.Notification).where(
            models.Notification.entity_type == "contract_expiry", models.Notification.entity_id == emp.id)).all()

    def test_reminders_at_30_15_7_days_to_hr_owner_and_manager(self):
        emp = self.contractor(end_in_days=20)
        today = datetime.date.today()
        self.assertEqual(reminders.contract_end_reminders(self.db, self.company_id, today), 1)
        rows = self.reminder_rows(emp)
        recipients = {r.user_id for r in rows}
        self.assertIn(self.owner.id, recipients)
        if emp.reporting_manager_id:
            mgr = self.db.scalar(select(models.User.id).where(models.User.employee_id == emp.reporting_manager_id,
                                                               models.User.status == "active"))
            if mgr:
                self.assertIn(mgr, recipients)
        self.assertTrue(all("[30d·" in r.body for r in rows))
        self.assertEqual(reminders.contract_end_reminders(self.db, self.company_id, today), 0)  # once
        self.assertEqual(reminders.contract_end_reminders(self.db, self.company_id, today + datetime.timedelta(days=6)), 1)  # 14 left -> 15d
        self.assertEqual(reminders.contract_end_reminders(self.db, self.company_id, today + datetime.timedelta(days=13)), 1)  # 7 left -> 7d
        self.assertEqual(reminders.contract_end_reminders(self.db, self.company_id, today + datetime.timedelta(days=14)), 0)
        self.assertTrue(crud.is_contract_employee(self.db.get(models.Employee, emp.id)))  # nothing automatic

    def test_non_contract_employees_never_get_contract_reminders(self):
        emp = next(e for e in self.employees_with_structure() if not crud.is_contract_employee(e))
        emp.contract_end_date = datetime.date.today() + datetime.timedelta(days=5)  # stray date on a full-timer
        self.db.flush()
        reminders.contract_end_reminders(self.db, self.company_id, datetime.date.today())
        self.assertEqual(self.reminder_rows(emp), [])

    def test_renew_contract(self):
        emp = self.contractor(end_in_days=20)
        old_end = emp.contract_end_date
        full_timer = next(e for e in self.employees_with_structure() if not crud.is_contract_employee(e))
        self.api("POST", f"/employees/{full_timer.id}/contract/renew",
                 {"new_end_date": (old_end + datetime.timedelta(days=90)).isoformat()}, expect=409)
        self.api("POST", f"/employees/{emp.id}/contract/renew", {"new_end_date": old_end.isoformat()}, expect=422)
        self.api("POST", f"/employees/{emp.id}/contract/renew",
                 {"new_end_date": (old_end + datetime.timedelta(days=90)).isoformat(), "new_rate_unit": "weekly"}, expect=422)
        new_end = old_end + datetime.timedelta(days=180)
        out = self.api("POST", f"/employees/{emp.id}/contract/renew",
                       {"new_end_date": new_end.isoformat(), "new_rate_amount": 95000, "notes": "Phase 2 extension"})
        self.assertEqual((out["contract_end_date"], out["contract_rate_amount"], out["contract_rate_unit"]),
                         (new_end.isoformat(), 95000.0, "monthly"))
        ev = self.db.get(models.EmployeeLifecycleEvent, uuid.UUID(out["event_id"]))
        self.assertEqual((ev.event_type, ev.from_contract_end_date, ev.to_contract_end_date, float(ev.from_contract_rate),
                          float(ev.to_contract_rate), ev.notes),
                         ("contract_renewal", old_end, new_end, 90000.0, 95000.0, "Phase 2 extension"))
        listed = [e for e in self.api("GET", "/employees/lifecycle-events?limit=500") if e["id"] == out["event_id"]]
        self.assertEqual((listed[0]["type"], listed[0]["from_value"], listed[0]["to_value"]),
                         ("Contract Renewal", f"Contract to {old_end.isoformat()}", f"Contract to {new_end.isoformat()}"))
        audit = self.db.scalar(text("SELECT changes FROM core_audit_logs WHERE action = 'contract_renew' "
                                    "AND document_id = :i ORDER BY created_at DESC LIMIT 1"), {"i": emp.id})
        self.assertEqual(audit["after"]["contract_end_date"], new_end.isoformat())

    def test_profile_overview_shows_contract_terms_only_for_contractors(self):
        full_timer = next(e for e in self.employees_with_structure() if not crud.is_contract_employee(e))
        self.assertNotIn("contract", self.api("GET", f"/employees/{full_timer.id}/overview"))
        emp = self.contractor(end_in_days=12)
        c = self.api("GET", f"/employees/{emp.id}/overview")["contract"]
        self.assertEqual((c["endDate"], c["daysLeft"], c["rateAmount"], c["rateUnit"]),
                         (emp.contract_end_date.isoformat(), 12, 90000.0, "monthly"))

    def test_convert_to_permanent(self):
        emp = self.contractor(end_in_days=10)
        out = self.api("POST", f"/employees/{emp.id}/contract/convert-to-permanent", {"notes": "Great fit"})
        self.assertEqual((out["employment_type"], out["contract_end_date"], out["contract_rate_amount"]),
                         ("full_time", None, None))
        self.assertFalse(out["needs_salary_structure"])  # this employee has a structure
        emp = self.db.get(models.Employee, emp.id)
        self.assertFalse(crud.is_contract_employee(emp))
        ev = self.db.get(models.EmployeeLifecycleEvent, uuid.UUID(out["event_id"]))
        self.assertEqual(ev.event_type, "contract_conversion")
        self.api("POST", f"/employees/{emp.id}/contract/convert-to-permanent", {}, expect=409)  # already permanent
        # From now on: leave applies again and reminders stop.
        before = _without_contract_rules(crud.get_remaining_leave_balance, leave=True)
        for lt in self.db.scalars(select(models.LeaveType).where(models.LeaveType.company_id == self.company_id)).all():
            self.assertEqual(crud.get_remaining_leave_balance(self.db, emp.id, lt), before(self.db, emp.id, lt))


if __name__ == "__main__":
    unittest.main()
