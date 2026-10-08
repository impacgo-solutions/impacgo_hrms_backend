"""QA payroll fixes: H-11..H-14, H-16, M-28..M-36, L-21, L-22, L-33, N-06.

Runs against the real dev DB (tenant acme) inside a transaction that is
always rolled back. Uses far-future periods (2031) so no real run collides.

    cd backend && venv/Scripts/python -m unittest tests.test_qa_payroll -v
"""

from __future__ import annotations

import datetime
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import event, func, select, text  # noqa: E402

from app import config, crud, models, payroll_period, tax_engine  # noqa: E402
from tests.test_people_visibility import PeopleTestBase  # noqa: E402

D = datetime.date


class PayrollQABase(PeopleTestBase):
    def setUp(self):
        super().setUp()
        mock.patch.object(config.settings, "rate_limit_enabled", False).start()
        self.owner, self.payroll_user = self.users["owner"], self.users["payroll"]
        if self.payroll_user.company_id != self.owner.company_id:
            # The checker must be in the maker's company: any other active
            # user there with Edit/Admin on Payroll.
            pick = None
            for u in self.db.scalars(select(models.User).where(
                    models.User.status == "active", models.User.company_id == self.owner.company_id,
                    models.User.id != self.owner.id, models.User.employee_id.is_not(None))).all():
                if crud.effective_user_matrix(self.db, u).get("payroll_process") in ("e", "a"):
                    pick = u
                    break
            if pick is None:
                self.skipTest("no second payroll user in the owner's company")
            self.users["payroll"] = self.payroll_user = pick
        self.cid = self.owner.company_id
        # No other assignment may leak into these runs.
        assigned = set(self.db.scalars(select(models.SalaryStructureAssignment.employee_id)).all())
        self.emps = [e for e in self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.cid, models.Employee.is_active.is_(True)
        ).order_by(models.Employee.employee_code)).all() if e.id not in assigned][:6]
        if len(self.emps) < 4:
            self.skipTest("need 4 active employees")
        for e in self.emps:
            e.date_of_joining = D(2020, 1, 1)
            e.employment_type = "full_time"
            e.tax_regime = "new"
        crud.upsert_company_settings(self.db, self.cid, {"auto_tds_estimate_enabled": False,
                                                          "lop_deduction_enabled": False,
                                                          "asset_deduction_enabled": False})
        self.pf_ceiling = crud.get_company_pf_wage_ceiling(self.db, self.cid)
        c = self._comp
        self.basic, self.spl = c("QA Basic", "QABASIC", "earning", "percent"), c("QA Special", "QASPL", "earning", "flat")
        self.hra = self.db.scalar(select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == self.cid, models.SalaryComponent.code == "HRA")) \
            or c("House Rent Allowance", "HRA", "earning", "percent")
        self.pf = c("QA Provident Fund", "QAPF", "deduction", "stat_pf", taxable=False)
        self.structure = crud.create_salary_structure(self.db, self.cid, "QA Std", D(2020, 1, 1), [
            {"component_id": self.basic.id, "percent_of": "ctc", "percent": 50},
            {"component_id": self.hra.id, "percent_of": "basic", "percent": 40},
            {"component_id": self.spl.id, "percent_of": "balance"},
            {"component_id": self.pf.id},
        ])
        self.db.flush()

    # ── helpers ──
    def _comp(self, name, code, ctype, calc, taxable=True):
        return crud.create_salary_component(self.db, self.cid, name, code, ctype, calc, taxable)

    def _assign(self, emp, ctc=600_000, from_date=D(2030, 1, 1), structure=None):
        return crud.create_salary_structure_assignment(self.db, emp.id, (structure or self.structure).id,
                                                       from_date, ctc)

    def _run(self, month, year=2031):
        return crud.create_payroll_run(self.db, self.cid, month, year)

    def _generate(self, run, user=None):
        user = user or self.owner
        slips = payroll_period.generate_run(self.db, self.cid, run, user.id)
        payroll_period.record_generated(self.db, run, user.id, len(slips))
        self.db.flush()
        return slips

    def _close(self, run):
        payroll_period.approve_run(self.db, run, self.payroll_user)
        payroll_period.lock_run(self.db, run, self.payroll_user)

    def _slip(self, run, emp):
        return self.db.scalar(select(models.SalarySlip).where(
            models.SalarySlip.payroll_run_id == run.id, models.SalarySlip.employee_id == emp.id))

    def _lines(self, slip):
        return {line.component.code: float(line.amount) for line in self.db.scalars(
            select(models.SalarySlipLine).where(models.SalarySlipLine.slip_id == slip.id)).all()}


class LifecycleTests(PayrollQABase):
    def test_h11_two_person_rule_lock_and_paid_over_http(self):
        self._assign(self.emps[0])
        run = self._run(1)
        self.as_("owner")
        r = self.client.post(f"/api/payroll/runs/{run.id}/generate")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["run"]["status"], "processed")
        self.assertEqual(r.json()["run"]["generated_by"], str(self.owner.id))
        # The maker can't be the checker -- not even the Owner.
        r = self.client.post(f"/api/payroll/runs/{run.id}/approve")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("Two-person", r.json()["detail"])
        # Lock before approval is refused.
        self.assertEqual(self.client.post(f"/api/payroll/runs/{run.id}/lock").status_code, 409)
        self.as_("payroll")
        r = self.client.post(f"/api/payroll/runs/{run.id}/approve")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["approved_by"], str(self.payroll_user.id))
        self.assertEqual(self.client.post(f"/api/payroll/runs/{run.id}/mark-paid").status_code, 409)
        r = self.client.post(f"/api/payroll/runs/{run.id}/lock")
        self.assertEqual(r.json()["status"], "locked", r.text)
        # Locked: no regenerate, no resync, no variable-pay change.
        self.as_("owner")
        r = self.client.post(f"/api/payroll/runs/{run.id}/generate")
        self.assertEqual(r.status_code, 409, r.text)
        slip = self._slip(run, self.emps[0])
        before = (slip.id, float(slip.gross_pay))
        self._assign(self.emps[0], ctc=900_000, from_date=D(2031, 1, 1))
        crud.resync_draft_payroll_runs(self.db, self.cid, self.emps[0].id, D(2031, 1, 1))
        self.db.refresh(slip)
        self.assertEqual((slip.id, float(slip.gross_pay)), before)
        r = self.client.post(f"/api/payroll/runs/{run.id}/mark-paid")
        self.assertEqual(r.json()["status"], "paid", r.text)
        self.db.refresh(slip)
        self.assertEqual(slip.status, "paid")
        actions = set(self.db.scalars(select(models.AuditLog.action).where(
            models.AuditLog.doctype == "payroll_run", models.AuditLog.document_id == run.id)).all())
        self.assertTrue({"generate", "approve", "lock", "mark_paid"} <= actions, actions)

    def test_h11_regenerate_voids_approval(self):
        self._assign(self.emps[0])
        run = self._run(1)
        self._generate(run)
        payroll_period.approve_run(self.db, run, self.payroll_user)
        self._generate(run, self.payroll_user)  # the checker regenerates -> they are now the maker
        self.assertEqual(run.status, "processed")
        self.assertIsNone(run.approved_by)
        with self.assertRaises(payroll_period.PayrollStateError):
            payroll_period.approve_run(self.db, run, self.payroll_user)
        payroll_period.approve_run(self.db, run, self.owner)

    def test_h12_regenerate_keeps_slip_ids(self):
        self._assign(self.emps[0])
        run = self._run(1)
        first = {s.employee_id: s.id for s in self._generate(run)}
        second = {s.employee_id: s.id for s in self._generate(run)}
        self.assertEqual(first, second)
        self.assertEqual(self.db.scalar(select(func.count(models.SalarySlip.id)).where(
            models.SalarySlip.payroll_run_id == run.id)), len(first))

    def test_h12_backdated_change_after_lock_becomes_arrears(self):
        emp = self.emps[0]
        self._assign(emp, 600_000)
        jan = self._run(1)
        self._generate(jan)
        self._close(jan)
        jan_slip = self._slip(jan, emp)
        jan_gross = float(jan_slip.gross_pay)
        self.assertAlmostEqual(jan_gross, 50_000, places=0)
        # Back-dated raise effective 1 Jan, entered after the lock.
        self._assign(emp, 720_000, from_date=D(2031, 1, 1))
        crud.resync_draft_payroll_runs(self.db, self.cid, emp.id, D(2031, 1, 1))
        feb = self._run(2)
        self._generate(feb)
        self.db.refresh(jan_slip)
        self.assertAlmostEqual(float(jan_slip.gross_pay), jan_gross, places=2)
        feb_lines = self._lines(self._slip(feb, emp))
        self.assertAlmostEqual(feb_lines.get("ARREARS", 0), 10_000, places=0)
        self.assertAlmostEqual(float(self._slip(feb, emp).gross_pay), 70_000, places=0)
        # Regenerating Feb doesn't double the arrears.
        self._generate(feb)
        self.assertAlmostEqual(self._lines(self._slip(feb, emp)).get("ARREARS", 0), 10_000, places=0)

    def test_h13_resync_never_creates_slips_in_ungenerated_run(self):
        draft = self._run(3)
        self.as_("owner")
        r = self.client.post("/api/payroll/salary-structure-assignments", json={
            "employee_id": str(self.emps[1].id), "structure_id": str(self.structure.id),
            "from_date": "2030-01-01", "annual_ctc": 600000})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.db.scalar(select(func.count(models.SalarySlip.id)).where(
            models.SalarySlip.payroll_run_id == draft.id)), 0)


class CalculationTests(PayrollQABase):
    def test_h14_mid_month_joiner_is_prorated(self):
        emp = self.emps[0]
        emp.date_of_joining = D(2031, 1, 15)
        self._assign(emp, 600_000, from_date=D(2031, 1, 15))
        run = self._run(1)
        self._generate(run)
        slip = self._slip(run, emp)
        self.assertIsNotNone(slip, "a mid-month joiner must get a slip")
        ratio = float(slip.payable_days) / float(slip.working_days)
        self.assertLess(ratio, 1)
        self.assertAlmostEqual(float(slip.gross_pay), 50_000 * ratio, delta=1)

    def test_h14_exit_last_working_day_is_prorated_even_if_inactive(self):
        emp = self.emps[0]
        self._assign(emp)
        self.db.add(models.ExitRequestModel(id=uuid.uuid4(), employee_id=emp.id, resignation_date=D(2030, 12, 20),
                                            last_working_day=D(2031, 1, 10), status="approved"))
        emp.is_active = False
        run = self._run(1)
        self._generate(run)
        slip = self._slip(run, emp)
        self.assertIsNotNone(slip)
        self.assertLess(float(slip.payable_days), float(slip.working_days))
        self.assertAlmostEqual(float(slip.gross_pay),
                               50_000 * float(slip.payable_days) / float(slip.working_days), delta=1)
        feb = self._run(2)
        self._generate(feb)
        self.assertIsNone(self._slip(feb, emp), "no slip after the last working day")

    def test_h16_net_pay_never_negative_and_carried_forward(self):
        emp = self.emps[0]
        big = self._comp("QA Recovery", "QAREC", "deduction", "flat", taxable=False)
        st = crud.create_salary_structure(self.db, self.cid, "QA Heavy", D(2020, 1, 1), [
            {"component_id": self.basic.id, "percent_of": "ctc", "percent": 50},
            {"component_id": self.spl.id, "percent_of": "balance"},
            {"component_id": self.pf.id},
            {"component_id": big.id, "amount": 80_000},
        ])
        self._assign(emp, 600_000, structure=st)
        jan = self._run(1)
        self._generate(jan)
        slip = self._slip(jan, emp)
        self.assertEqual(float(slip.net_pay), 0)
        self.assertAlmostEqual(float(slip.total_deductions), float(slip.gross_pay), places=2)
        self.assertIn("deductions_capped", slip.flags)
        pf = min(25_000, self.pf_ceiling) * 0.12
        self.assertAlmostEqual(self._lines(slip)["QAPF"], pf, places=2)  # statutory taken first
        self.assertAlmostEqual(float(slip.deduction_carry_forward), 80_000 + pf - 50_000, places=2)
        self.assertGreaterEqual(jan.flagged_slips, 1)
        feb = self._run(2)
        self._generate(feb)
        self.assertIn("DED-CF", self._lines(self._slip(feb, emp)))

    def test_m29_m30_statutory_tds_new_and_old_regime(self):
        crud.upsert_company_settings(self.db, self.cid, {"auto_tds_estimate_enabled": True})
        new_emp, old_emp = self.emps[0], self.emps[1]
        old_emp.tax_regime = "old"
        for e in (new_emp, old_emp):
            self._assign(e, 2_400_000)
        self.db.add(models.TaxDeclaration(id=uuid.uuid4(), employee_id=old_emp.id, fiscal_year="2031-32",
                                          tax_regime="old", hra_claimed=300_000, section_80c=150_000,
                                          section_80d=0, section_80ccd_1b=0, home_loan_interest=0, status="draft"))
        apr = self._run(4)  # first month of FY 2031-32 -> no YTD, 11 months left
        self._generate(apr)
        new_tds = self._lines(self._slip(apr, new_emp))["TDS-EST"]
        expected_new = tax_engine.compute_annual_tax(tax_engine.TaxInputs("new", 2_400_000)).total_tax
        self.assertAlmostEqual(new_tds, round(expected_new / 12), places=0)
        old_tds = self._lines(self._slip(apr, old_emp))["TDS-EST"]
        pf = min(100_000, self.pf_ceiling) * 0.12 * 12
        # M-29: HRA (matched on code HRA) is exempted up to the claim.
        expected_old = tax_engine.compute_annual_tax(tax_engine.TaxInputs(
            "old", 2_400_000, hra_exemption=300_000, section_80c=150_000 + pf)).total_tax
        without_hra = tax_engine.compute_annual_tax(tax_engine.TaxInputs(
            "old", 2_400_000, section_80c=150_000 + pf)).total_tax
        self.assertAlmostEqual(old_tds, round(expected_old / 12), places=0)
        self.assertLess(old_tds, round(without_hra / 12))

    def test_n06_esi_computed_within_wage_ceiling(self):
        esi = self._comp("QA ESI", "QAESI", "deduction", "stat_esi", taxable=False)
        st = crud.create_salary_structure(self.db, self.cid, "QA ESI", D(2020, 1, 1), [
            {"component_id": self.basic.id, "percent_of": "ctc", "percent": 50},
            {"component_id": self.spl.id, "percent_of": "balance"},
            {"component_id": esi.id},
        ])
        low, high = self.emps[0], self.emps[1]
        self._assign(low, 180_000, structure=st)  # 15,000 / month
        self._assign(high, 600_000, structure=st)
        run = self._run(1)
        self._generate(run)
        self.assertEqual(self._lines(self._slip(run, low))["QAESI"], 113.0)  # 112.5 rounded up
        self.assertNotIn("QAESI", self._lines(self._slip(run, high)))

    def test_m31_loan_emi_recovered_and_applied_on_lock(self):
        emp = self.emps[0]
        self._assign(emp)
        for other in self.db.scalars(select(models.Loan).where(models.Loan.employee_id == emp.id)).all():
            other.status = "closed"  # seeded loans would add their own EMIs
        loan = crud.create_loan(self.db, emp.id, "Personal", 12_000, 5_000, 12_000, company_id=self.cid)
        jan = self._run(1)
        self._generate(jan)
        self.assertEqual(self._lines(self._slip(jan, emp))["LOAN-EMI"], 5_000)
        self._generate(jan)  # regenerate does not double-count
        self.assertEqual(float(loan.outstanding_balance), 12_000)
        self._close(jan)
        self.db.refresh(loan)
        self.assertEqual(float(loan.outstanding_balance), 7_000)
        feb, mar = self._run(2), self._run(3)
        self._generate(feb)
        self._generate(mar)  # Feb's 5,000 is earmarked -> only 2,000 left for Mar
        self.assertEqual(self._lines(self._slip(mar, emp))["LOAN-EMI"], 2_000)

    def test_m31_loan_validation_over_http(self):
        self.as_("owner")
        base = {"employee_id": str(self.emps[0].id), "loan_type": "x", "principal_amount": 1000,
                "outstanding_balance": 1000}
        self.assertEqual(self.client.post("/api/loans", json=base | {"emi_amount": -5}).status_code, 422)
        self.assertEqual(self.client.post("/api/loans", json=base | {"outstanding_balance": 5000}).status_code, 422)
        other = self.db.scalar(select(models.Employee.id).where(models.Employee.company_id != self.cid).limit(1))
        if other is not None:
            r = self.client.post("/api/loans", json=base | {"employee_id": str(other), "emi_amount": 100})
            self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.client.post("/api/loans", json=base | {"emi_amount": 100}).status_code, 201)

    def test_m32_deleting_assignment_resyncs_open_run(self):
        a = self._assign(self.emps[0])
        run = self._run(1)
        self._generate(run)
        self.assertIsNotNone(self._slip(run, self.emps[0]))
        self.as_("owner")
        r = self.client.delete(f"/api/payroll/salary-structure-assignments/{a.id}")
        self.assertEqual(r.status_code, 204, r.text)
        self.assertIsNone(self._slip(run, self.emps[0]))

    def test_m33_revision_uses_server_ctc_and_approval_creates_assignment(self):
        emp = self.emps[0]
        self._assign(emp, 600_000)
        self.assertEqual(payroll_period.current_ctc(self.db, emp), 600_000)
        req = crud.create_salary_revision_request(self.db, emp.id, 600_000, 750_000, "raise")
        before = self.db.scalar(select(func.count(models.SalaryStructureAssignment.id)).where(
            models.SalaryStructureAssignment.employee_id == emp.id))
        new = payroll_period.apply_approved_revision(self.db, req, self.owner.id)
        self.assertEqual(float(new.annual_ctc), 750_000)
        self.assertEqual(new.structure_id, self.structure.id)
        self.assertEqual(self.db.scalar(select(func.count(models.SalaryStructureAssignment.id)).where(
            models.SalaryStructureAssignment.employee_id == emp.id)), before + 1)
        if emp.reporting_manager_id is not None:
            self.as_("owner")
            r = self.client.post("/api/salary-revision-requests", json={
                "employee_id": str(emp.id), "current_ctc": 1, "proposed_ctc": 800000, "reason": "x"})
            if r.status_code == 201:
                self.assertNotEqual(r.json()["current_ctc"], 1)

    def test_m35_percent_of_ctc_earnings_capped_at_100(self):
        self.as_("owner")
        r = self.client.post("/api/payroll/salary-structures", json={
            "name": "QA Over", "effective_from": "2031-01-01", "lines": [
                {"component_id": str(self.basic.id), "percent_of": "ctc", "percent": 70},
                {"component_id": str(self.spl.id), "percent_of": "ctc", "percent": 50}]})
        self.assertEqual(r.status_code, 409, r.text)

    def test_m36_query_count_does_not_grow_with_head_count(self):
        counts = [0]

        def counter(*_a, **_k):
            counts[-1] += 1

        event.listen(self.conn, "before_cursor_execute", counter)
        self.addCleanup(event.remove, self.conn, "before_cursor_execute", counter)
        for e in self.emps[:2]:
            self._assign(e)
        warm = self._run(1)
        self._generate(warm)  # creates engine components once
        feb = self._run(2)
        counts.append(0)
        self._generate(feb)
        for e in self.emps[2:4]:
            self._assign(e)
        mar = self._run(3)
        counts.append(0)
        self._generate(mar)
        self.assertLessEqual(counts[2] - counts[1], 2, counts)


class ValidationAndAccessTests(PayrollQABase):
    def test_m28_tax_declaration_bounds(self):
        self.as_("owner")
        base = {"employee_id": str(self.emps[0].id), "fiscal_year": "2031-32", "tax_regime": "old"}
        self.assertEqual(self.client.post("/api/tax-declarations", json=base | {"hra_claimed": -1}).status_code, 422)
        self.assertEqual(self.client.post("/api/tax-declarations", json=base | {"section_80c": 200000}).status_code, 422)
        r = self.client.post("/api/tax-declarations", json=base | {"section_80c": 150000, "section_80d": 25000})
        self.assertEqual(r.status_code, 201, r.text)

    def test_l21_override_upper_bound(self):
        a = self._assign(self.emps[0])
        self.as_("owner")
        url = f"/api/payroll/salary-structure-assignments/{a.id}/overrides"
        for body in ({"override_type": "percent", "value": 1e9}, {"override_type": "percent", "value": 150},
                     {"override_type": "amount", "value": 1e9}):
            r = self.client.post(url, json={"component_id": str(self.hra.id)} | body)
            self.assertEqual(r.status_code, 422, (body, r.text))
        r = self.client.post(url, json={"component_id": str(self.hra.id), "override_type": "percent", "value": 50})
        self.assertEqual(r.status_code, 201, r.text)

    def test_l22_structure_templates_need_payroll_permission(self):
        self.as_("employee")
        self.assertEqual(self.client.get("/api/payroll/salary-structures").status_code, 403)
        self.assertEqual(self.client.get("/api/payroll/salary-components").status_code, 403)
        self.as_("payroll")
        self.assertEqual(self.client.get("/api/payroll/salary-structures").status_code, 200)

    def test_l33_no_structure_is_200_null(self):
        self.as_("owner")
        r = self.client.get(f"/api/employees/{self.emps[3].id}/salary-structure")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json())
        self.assertEqual(self.client.get(f"/api/employees/{uuid.uuid4()}/salary-structure").status_code, 404)

    def test_n06_template_and_tenants_have_pf_and_esi(self):
        rows = self.db.execute(text("""
            SELECT s FROM (SELECT '_template'::text s UNION SELECT tenant_slug FROM public.tenant_modules
                           WHERE module_code = 'hcm' AND is_enabled) x
            WHERE to_regclass(format('%I.hcm_salary_components', s)) IS NOT NULL""")).scalars().all()
        for schema in rows:
            missing = self.db.execute(text(f"""
                SELECT count(*) FROM "{schema}".core_companies c WHERE NOT EXISTS (
                  SELECT 1 FROM "{schema}".hcm_salary_components x WHERE x.company_id = c.id AND upper(x.code) = 'PF')
                OR NOT EXISTS (SELECT 1 FROM "{schema}".hcm_salary_components x
                  WHERE x.company_id = c.id AND upper(x.code) IN ('ESI', 'ESIC'))""")).scalar()
            self.assertEqual(missing, 0, schema)
        calc = dict(self.db.execute(text(
            "SELECT code, calc_type FROM _template.hcm_salary_components WHERE code IN ('PF','ESI')")).all())
        self.assertEqual(calc, {"PF": "stat_pf", "ESI": "stat_esi"})


if __name__ == "__main__":
    unittest.main()
