"""Reimbursements on the payslip PDF: shown inside the payslip (below the
Earnings / Deductions tables, above the net-pay summary), with Total
Reimbursements and a net-pay figure that includes them -- for templates
written before reimbursements existed as well as ones that place the
placeholders themselves. Payslips without reimbursements are unchanged.

    cd backend && venv/Scripts/python -m unittest tests.test_payslip_reimbursements -v

Read-only against the configured database (a real payslip with
reimbursements in PAYSLIP_TEST_TENANT, default impacgo-solutions).
"""

from __future__ import annotations

import datetime
import os
from html import unescape
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

from app import crud, models, payslip_html_renderer as r  # noqa: E402

TENANT = os.environ.get("PAYSLIP_TEST_TENANT", "impacgo-solutions")

LEGACY = """<div class="payslip">
  <div class="net-pay">Employee Net Pay &#8377;{{net_salary}}</div>
  <table class="salary-table"><tr>
    <td><table class="component-table">
      <tr><th>Earnings</th><th>Amount</th><th>YTD</th></tr>{{earnings_rows_ytd}}
      <tr><td>Gross Earnings</td><td>{{gross_earnings}}</td><td>{{gross_earnings_ytd}}</td></tr>
    </table></td>
    <td><table class="component-table">
      <tr><th>Deductions</th><th>Amount</th><th>YTD</th></tr>{{deductions_rows_ytd}}
      <tr><td>Total Deductions</td><td>{{total_deductions}}</td><td></td></tr>
    </table></td>
  </tr></table>
  <div class="net-payable-band">Total Net Payable &#8377; {{net_salary}} ({{net_payable_in_words}})</div>
  <p class="note">**Total Net Payable = Gross Earnings - Total Deductions</p>
</div>"""


class PayslipReimbursementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = crud.open_tenant_session(TENANT)
        rows = cls.db.execute(
            select(models.SalarySlip).where(models.SalarySlip.reimbursements_total > 0)
        ).scalars().all()
        cls.detail = None
        for slip in rows:
            detail = crud.get_salary_slip_detail(cls.db, slip.id)
            if detail and detail.get("reimbursements"):
                cls.detail = detail
                break
        if cls.detail is None:
            # N-10: a new tenant has no paid-out reimbursement yet -- build the
            # slip detail the renderer consumes from an existing employee and
            # unsaved run/slip objects (nothing is written).
            cls.detail = cls._synthetic_detail()
        cls.company = cls.db.get(models.Company, cls.detail["employee"].company_id)
        cls.ph = r.build_payslip_placeholders(cls.detail, cls.company)
        slip = cls.detail["slip"]
        cls.net = float(slip.net_pay)
        cls.reimb = sum(x["amount"] for x in cls.detail["reimbursements"])

    @classmethod
    def _synthetic_detail(cls):
        employee = cls.db.scalars(
            select(models.Employee).where(models.Employee.is_active.is_(True)).order_by(models.Employee.employee_code)
        ).first()
        if employee is None:
            cls.db.close()
            raise unittest.SkipTest("tenant has no employee")
        today = datetime.date.today()
        start = today.replace(day=1)
        run = models.PayrollRun(
            id=uuid.uuid4(), company_id=employee.company_id, run_no="PR-TEST", period_month=start.month,
            period_year=start.year, from_date=start, to_date=start + datetime.timedelta(days=27), status="draft",
        )
        slip = models.SalarySlip(
            id=uuid.uuid4(), payroll_run_id=run.id, employee_id=employee.id, working_days=30, lop_days=0,
            gross_pay=50000, total_deductions=4500, net_pay=45500, reimbursements_total=3750, status="draft",
        )
        return {
            "slip": slip, "run": run, "employee": employee,
            "lines": [
                {"component_id": uuid.uuid4(), "name": "Basic", "component_type": "earning",
                 "amount": 50000.0, "ytd_amount": 50000.0},
                {"component_id": uuid.uuid4(), "name": "Professional Tax", "component_type": "deduction",
                 "amount": 4500.0, "ytd_amount": 4500.0},
            ],
            "reimbursements": [
                {"label": "Travel Reimbursement", "amount": 2500.0, "reference": "TR-TEST-0001",
                 "source_type": "travel_request", "inclusion_id": str(uuid.uuid4())},
                {"label": "Expense Reimbursement", "amount": 1250.0, "reference": "EXP-TEST-0001",
                 "source_type": "expense_claim", "inclusion_id": str(uuid.uuid4())},
            ],
        }

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_totals(self):
        self.assertEqual(self.ph["reimbursements_total"], f"{self.reimb:,.2f}")
        self.assertEqual(self.ph["total_payable"], f"{self.net + self.reimb:,.2f}")
        self.assertEqual(self.ph["take_home_pay"], self.ph["total_payable"])
        self.assertTrue(self.ph["take_home_pay_in_words"].startswith("Rupees"))

    def test_legacy_template_gets_section_in_place_and_full_net(self):
        html = r.render_payslip_html(LEGACY, "", self.ph)
        section = html.index("payslip-reimbursements")
        self.assertGreater(section, html.index("Total Deductions"))  # below the salary tables
        self.assertLess(section, html.index("net-payable-band"))     # above the net-pay summary
        self.assertEqual(html.count("payslip-reimbursements"), 1)
        for item in self.detail["reimbursements"]:
            self.assertIn(item["label"], html)
            self.assertIn(item.get("reference") or "—", html)
        total = f"{self.net + self.reimb:,.2f}"
        # The allowlist sanitiser (M-34) may emit &#8377; as the literal ₹.
        self.assertIn(f"Employee Net Pay ₹{total}", unescape(html))
        self.assertIn(f"Total Net Payable ₹ {total}", unescape(html))
        self.assertIn(self.ph["total_payable_in_words"], html)
        self.assertIn("Total Reimbursements", html)
        self.assertIn(f"₹{self.net:,.2f}", html)  # Net Salary line
        self.assertIn("Gross Earnings - Total Deductions + Reimbursements", html)
        self.assertNotIn("{{", html)

    def test_template_placing_reimbursements_itself_is_left_alone(self):
        own = LEGACY.replace("</div>\n  <p", "</div>{{reimbursement_rows}} {{total_payable}}\n  <p")
        html = r.render_payslip_html(own, "", self.ph)
        self.assertNotIn("payslip-reimbursements", html)  # no auto section
        self.assertIn(f"Employee Net Pay ₹{self.net:,.2f}", unescape(html))  # net_salary keeps its meaning
        self.assertIn("Gross Earnings - Total Deductions</p>", html)

    def test_no_reimbursements_renders_unchanged(self):
        plain = r.build_payslip_placeholders({**self.detail, "reimbursements": []}, self.company)
        self.assertEqual(plain["reimbursement_section"], "")
        html = r.render_payslip_html(LEGACY, "", plain)
        self.assertNotIn("payslip-reimbursements", html)
        self.assertIn(f"Employee Net Pay ₹{self.net:,.2f}", unescape(html))
        self.assertIn("Gross Earnings - Total Deductions</p>", html)

    def test_template_without_salary_table_still_gets_section_inside_layout(self):
        html = r.render_payslip_html('<div class="p"><p>{{employee_name}}</p></div>', "", self.ph)
        body = html[html.index("<body>"):]
        self.assertLess(body.index("payslip-reimbursements"), body.rindex("</div></body>"))

    def test_real_saved_template_renders_pdf(self):
        tpl = self.db.scalars(select(models.PayslipHtmlTemplate).where(
            models.PayslipHtmlTemplate.company_id == self.company.id)).first()
        if tpl is None:
            self.skipTest("company has no saved payslip template")
        html = r.render_payslip_html(tpl.html_body, tpl.css_styles, self.ph)
        self.assertEqual(html.count("payslip-reimbursements"), 1)
        self.assertNotIn("{{", html)
        self.assertTrue(r.render_html_to_pdf(html).startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
