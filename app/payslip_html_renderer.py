"""Payslip-specific placeholder mapping on top of the shared
template_rendering engine (see that module's docstring for the full
rendering/security design -- validate_template_html, render_payslip_html,
and render_html_to_pdf here are thin, name-stable wrappers around it so
every existing caller/import in routers/payroll.py keeps working
unchanged).
"""

import calendar
import re

from . import template_rendering as tr

validate_template_html = tr.validate_template_html
render_html_to_pdf = tr.render_html_to_pdf


def build_payslip_placeholders(slip_detail: dict, company) -> dict[str, str]:
    """Every placeholder key available to a Payslip Template, resolved from
    real backend data only (slip_detail from crud.get_salary_slip_detail,
    company from core_companies) -- never mock/sample values. Raw (unescaped)
    strings/numbers; escaping happens once, uniformly, in render_payslip_html."""
    slip = slip_detail["slip"]
    run = slip_detail["run"]
    employee = slip_detail["employee"]
    lines = slip_detail["lines"]

    employee_name = f"{employee.first_name} {employee.last_name or ''}".strip() if employee else "—"
    period_label = f"{calendar.month_name[run.period_month]} {run.period_year}" if run else "—"

    # get_salary_slip_detail's own line order isn't a meaningful ordering
    # (no ORDER BY -- it reflects incidental DB row order, not a designed
    # display sequence), so it can silently drift whenever a structure's
    # lines are recreated/edited. Basic always leads the Earnings table --
    # matching the reference payslip's own layout and standard payslip
    # convention -- with every other earning line kept in whatever order
    # they already had relative to each other (sort is stable).
    def _basic_first(rows: list[dict]) -> list[dict]:
        return sorted(rows, key=lambda l: 0 if "basic" in l["name"].lower() else 1)

    earning_lines = _basic_first(
        [l for l in lines if l["component_type"] == "earning"]
    )
    deduction_lines = [l for l in lines if l["component_type"] in ("deduction", "tax")]
    tax_lines = [l for l in lines if l["component_type"] == "tax"]
    non_tax_deduction_lines = [l for l in lines if l["component_type"] == "deduction"]

    def find_line(rows: list[dict], *keywords: str) -> dict | None:
        return next(
            (l for l in rows if any(k in l["name"].lower() for k in keywords)), None
        )

    basic_line = find_line(earning_lines, "basic")
    variable_line = find_line(earning_lines, "variable")
    pf_line = find_line(non_tax_deduction_lines, "pf", "provident")

    basic_salary = basic_line["amount"] if basic_line else 0.0
    variable_pay = variable_line["amount"] if variable_line else 0.0
    allowances = sum(
        l["amount"] for l in earning_lines if l is not basic_line and l is not variable_line
    )
    deductions_total = sum(l["amount"] for l in deduction_lines)
    income_tax = sum(l["amount"] for l in tax_lines)
    other_deductions = sum(l["amount"] for l in non_tax_deduction_lines)
    pf_deduction = pf_line["amount"] if pf_line else 0.0
    paid_days = float(slip.working_days) - float(slip.lop_days)
    has_bank_details = bool(employee and employee.bank_name and employee.bank_account_no)

    # Year-To-Date -- each line's own real ytd_amount from crud.
    # get_salary_slip_detail (a genuine sum across this fiscal year's real
    # payroll runs up to and including this one, never fabricated/copied
    # from the current month's figure alone).
    basic_salary_ytd = basic_line["ytd_amount"] if basic_line else 0.0
    variable_pay_ytd = variable_line["ytd_amount"] if variable_line else 0.0
    gross_earnings_ytd = sum(l["ytd_amount"] for l in earning_lines)
    income_tax_ytd = sum(l["ytd_amount"] for l in tax_lines)
    total_deductions_ytd = sum(l["ytd_amount"] for l in deduction_lines)

    def rows_html(rows: list[dict]) -> str:
        if not rows:
            return "<tr><td colspan=\"2\">—</td></tr>"
        return "".join(
            f"<tr><td>{tr.html.escape(r['name'])}</td>"
            f"<td style=\"text-align:right\">{r['amount']:,.2f}</td></tr>"
            for r in rows
        )

    def rows_html_with_ytd(rows: list[dict]) -> str:
        """Same as rows_html, plus a third <td> for each line's own real
        YTD total -- a separate, additive verbatim key (see
        earnings_rows_ytd/deductions_rows_ytd below) so any existing
        template still using the original 2-column earnings_rows/
        deductions_rows keeps rendering exactly as before."""
        if not rows:
            return "<tr><td colspan=\"3\">—</td></tr>"
        return "".join(
            f"<tr><td>{tr.html.escape(r['name'])}</td>"
            f"<td style=\"text-align:right\">{r['amount']:,.2f}</td>"
            f"<td style=\"text-align:right\">{r['ytd_amount']:,.2f}</td></tr>"
            for r in rows
        )

    # Approved Travel/Expense reimbursements paid with this slip (Payroll >
    # Salary Structure > Reimbursement Components) -- label, source request/
    # report number, amount. Separate from earnings: never part of gross.
    reimbursements = slip_detail.get("reimbursements") or []
    reimbursements_total = sum(r["amount"] for r in reimbursements)
    total_payable = float(slip.net_pay) + reimbursements_total

    def reimbursement_rows_html(rows: list[dict]) -> str:
        if not rows:
            return "<tr><td colspan=\"3\">—</td></tr>"
        return "".join(
            f"<tr><td>{tr.html.escape(r['label'])}</td>"
            f"<td>{tr.html.escape(r.get('reference') or '—')}</td>"
            f"<td style=\"text-align:right\">{r['amount']:,.2f}</td></tr>"
            for r in rows
        )

    def inr(value: float) -> str:
        return f"\u20b9{value:,.2f}"

    cell = "padding:6px 8px;border-bottom:1px solid #eeeeee;"
    reimbursement_section = (
        "<div class=\"payslip-reimbursements no-break\" style=\"margin:16px 0 4px;font-size:12px\">"
        "<table style=\"width:100%;border-collapse:collapse\">"
        "<tr>"
        "<th style=\"background:#f4f5f6;text-align:left;font-weight:700;padding:7px 8px;border-bottom:1px solid #dddddd\">Reimbursements</th>"
        "<th style=\"background:#f4f5f6;text-align:left;font-weight:700;padding:7px 8px;border-bottom:1px solid #dddddd\">Reference</th>"
        "<th style=\"background:#f4f5f6;text-align:right;font-weight:700;padding:7px 8px;border-bottom:1px solid #dddddd\">Amount</th>"
        "</tr>"
        + "".join(
            f"<tr><td style=\"{cell}\">{tr.html.escape(r['label'])}</td>"
            f"<td style=\"{cell}color:#555555\">{tr.html.escape(r.get('reference') or '—')}</td>"
            f"<td style=\"{cell}text-align:right\">{inr(r['amount'])}</td></tr>"
            for r in reimbursements
        )
        + "<tr>"
        f"<td colspan=\"2\" style=\"padding:8px 8px 6px;font-weight:700;border-top:1px solid #b9b9b9\">Total Reimbursements</td>"
        f"<td style=\"padding:8px 8px 6px;font-weight:700;border-top:1px solid #b9b9b9;text-align:right\">{inr(reimbursements_total)}</td>"
        "</tr></table>"
        "<table style=\"width:100%;border-collapse:collapse;margin-top:10px\">"
        f"<tr><td style=\"padding:3px 8px;color:#555555\">Net Salary (Gross Earnings \u2212 Total Deductions)</td>"
        f"<td style=\"padding:3px 8px;text-align:right;color:#555555\">{inr(float(slip.net_pay))}</td></tr>"
        f"<tr><td style=\"padding:3px 8px;color:#555555\">Add: Reimbursements</td>"
        f"<td style=\"padding:3px 8px;text-align:right;color:#555555\">{inr(reimbursements_total)}</td></tr>"
        "</table></div>"
    ) if reimbursements else ""

    address_parts = [
        getattr(company, "address_line1", None),
        getattr(company, "address_line2", None),
        getattr(company, "city", None),
        getattr(company, "state", None),
        getattr(company, "pincode", None),
        getattr(company, "country", None),
    ]
    company_address = ", ".join(p for p in address_parts if p)

    return {
        "employee_name": employee_name,
        "employee_id": employee.employee_code if employee else "—",
        "designation": employee.designation.name if employee and employee.designation else "—",
        "department": employee.department.name if employee and employee.department else "—",
        "joining_date": employee.date_of_joining.isoformat() if employee and employee.date_of_joining else "—",
        # Alias of joining_date -- kept as a separate key (not just documented
        # as "same as joining_date") so a template author who naturally reaches
        # for either name gets real data either way, never a blank/literal token.
        "date_of_joining": employee.date_of_joining.isoformat() if employee and employee.date_of_joining else "—",
        # DD/MM/YYYY variants of the same two real dates above -- additive,
        # so any existing template using the plain ISO joining_date/pay_date
        # keeps rendering exactly as before.
        "joining_date_ddmmyyyy": employee.date_of_joining.strftime("%d/%m/%Y") if employee and employee.date_of_joining else "—",
        "pay_period": period_label,
        # The payroll period's own end date -- the closest real, non-
        # fabricated stand-in for "date paid" this app tracks (there is no
        # separate disbursement-date field on a salary slip).
        "pay_date": run.to_date.isoformat() if run and run.to_date else "—",
        "pay_date_ddmmyyyy": run.to_date.strftime("%d/%m/%Y") if run and run.to_date else "—",
        # Derived from whether real bank details are on file for this
        # employee -- never a hardcoded "Bank Transfer" for everyone.
        "payment_mode": "Bank Transfer" if has_bank_details else "—",
        "basic_salary": f"{basic_salary:,.2f}",
        # Year-To-Date: this fiscal year's real cumulative total for the
        # same line, up to and including this slip's own period.
        "basic_salary_ytd": f"{basic_salary_ytd:,.2f}",
        # The real earning-type line whose name identifies it as variable
        # pay/bonus, if this employee's structure has one -- 0.00 (not
        # fabricated) when it doesn't.
        "variable_pay": f"{variable_pay:,.2f}",
        "variable_pay_ytd": f"{variable_pay_ytd:,.2f}",
        "allowances": f"{allowances:,.2f}",
        # Alias of `allowances` -- every earning line other than Basic and
        # Variable Pay, combined.
        "other_allowances": f"{allowances:,.2f}",
        "deductions": f"{deductions_total:,.2f}",
        # Alias of `deductions` (total_deductions is the more explicit name
        # many payslip designs expect for the same figure).
        "total_deductions": f"{deductions_total:,.2f}",
        # Real, computed splits of the same total_deductions figure -- the
        # tax-type component lines (e.g. the auto-estimated TDS line) vs
        # every other deduction-type line (e.g. Professional Tax, PF).
        "income_tax": f"{income_tax:,.2f}",
        "income_tax_ytd": f"{income_tax_ytd:,.2f}",
        "total_deductions_ytd": f"{total_deductions_ytd:,.2f}",
        "other_deductions": f"{other_deductions:,.2f}",
        # The real deduction-type line identifiable as PF/Provident Fund,
        # if this employee's structure has one -- 0.00 when it doesn't.
        "pf_deduction": f"{pf_deduction:,.2f}",
        "gross_salary": f"{float(slip.gross_pay):,.2f}",
        # Alias of gross_salary.
        "gross_earnings": f"{float(slip.gross_pay):,.2f}",
        "gross_earnings_ytd": f"{gross_earnings_ytd:,.2f}",
        # Alias of gross_earnings_ytd.
        "gross_salary_ytd": f"{gross_earnings_ytd:,.2f}",
        "net_salary": f"{float(slip.net_pay):,.2f}",
        # Alias of net_salary.
        "net_payable": f"{float(slip.net_pay):,.2f}",
        # Real net pay amount, spelled out -- computed from the same
        # slip.net_pay every other net-pay placeholder uses, never a
        # separately-fabricated figure.
        "net_payable_in_words": tr.amount_in_words(float(slip.net_pay)),
        "bank_name": (employee.bank_name if employee else None) or "—",
        "bank_account_no": (employee.bank_account_no if employee else None) or "—",
        # Alias of bank_account_no.
        "account_number": (employee.bank_account_no if employee else None) or "—",
        "bank_ifsc": (employee.bank_ifsc if employee else None) or "—",
        # ':g' trims a whole-number Decimal's trailing ".0" (e.g. "21" and
        # "0", not "21.0" and "0.0") while still showing a real half-day
        # value (e.g. "20.5") in full -- same convention paid_days below
        # already used, just not applied to these two yet.
        "working_days": f"{float(slip.working_days):g}",
        "lop_days": f"{float(slip.lop_days):g}",
        # Real computed value (working_days - lop_days), not a duplicate of
        # working_days -- the days actually paid for.
        "paid_days": f"{paid_days:g}",
        "company_name": company.name if company else "—",
        "company_address": company_address or "—",
        # Kept for backward compatibility with any template already using
        # the raw stored URL directly.
        "company_logo_url": (getattr(company, "logo_url", None) or ""),
        # The one to actually use in <img src="{{company_logo}}"> -- always
        # a valid, renderable value (a real logo as a data URL, a real
        # external URL, or a transparent placeholder), never a broken image.
        "company_logo": tr.resolve_company_logo(company),
        # Pre-rendered itemized line-item tables (already-escaped <tr> markup)
        # -- inserted verbatim wherever the admin places these two tokens, so
        # a template can show a real per-component breakdown without needing
        # any loop/logic syntax in the placeholder mini-language.
        "earnings_rows": rows_html(earning_lines),
        "deductions_rows": rows_html(deduction_lines),
        # 3-column (name, amount, YTD) versions of the two rows above --
        # additive, so any existing template still using the plain
        # earnings_rows/deductions_rows keeps its original 2-column output.
        "earnings_rows_ytd": rows_html_with_ytd(earning_lines),
        "deductions_rows_ytd": rows_html_with_ytd(deduction_lines),
        # Reimbursements: 3-column (component, source reference, amount)
        # rows, their total, and net salary + reimbursements.
        "reimbursement_rows": reimbursement_rows_html(reimbursements),
        "reimbursements_total": f"{reimbursements_total:,.2f}",
        "total_payable": f"{total_payable:,.2f}",
        "total_payable_in_words": tr.amount_in_words(total_payable),
        # The complete, styled Reimbursements block (rows, Total
        # Reimbursements, Net Salary + Reimbursements) -- "" without any.
        "reimbursement_section": reimbursement_section,
        # What the employee actually receives: net salary + reimbursements
        # (aliases of total_payable, named the way payslip designs say it).
        "take_home_pay": f"{total_payable:,.2f}",
        "take_home_pay_in_words": tr.amount_in_words(total_payable),
    }


_REIMBURSEMENT_KEYS = ("reimbursement_rows", "reimbursement_section", "reimbursements_total",
                       "total_payable", "take_home_pay")
_TABLE_TAG = re.compile(r"<table\b|</table\s*>", re.I)
_FORMULA = re.compile(r"(Gross\s+Earnings\s*[-\u2013\u2212]\s*Total\s+Deductions)(?!\s*\+)", re.I)


def _insert_after_salary_tables(html_body: str, fragment: str) -> str:
    """[fragment] right after the (outermost) table holding the earnings /
    deductions rows -- i.e. below Earnings | Deductions, above the net-pay
    summary; before the template's closing tag when there is no such table."""
    idx = -1
    for token in ("{{earnings_rows", "{{deductions_rows"):
        found = html_body.find(token)
        if found != -1 and (idx == -1 or found < idx):
            idx = found
    if idx != -1:
        before = html_body[:idx]
        depth = len(re.findall(r"<table\b", before, re.I)) - len(re.findall(r"</table\s*>", before, re.I))
        if depth > 0:
            for m in _TABLE_TAG.finditer(html_body, idx):
                depth += 1 if m.group(0).lower().startswith("<table") else -1
                if depth == 0:
                    return html_body[:m.end()] + fragment + html_body[m.end():]
    stripped = html_body.rstrip()
    if stripped.lower().endswith("</div>"):
        cut = stripped.lower().rfind("</div>")
        return stripped[:cut] + fragment + stripped[cut:]
    return html_body + fragment


def render_payslip_html(html_body: str, css_styles: str, placeholders: dict[str, str]) -> str:
    """A payslip with reimbursements always shows them properly. A template
    that places none of the reimbursement placeholders itself (every
    template authored before they existed) gets:
      * the styled Reimbursements section right below the Earnings /
        Deductions tables (above the net-pay summary),
      * its net-pay figures (net_salary / net_payable / net_payable_in_words
        -- the "Employee Net Pay" box and "Total Net Payable" band) showing
        what the employee is actually paid: net salary + reimbursements,
      * a "Gross Earnings - Total Deductions" formula note completed with
        "+ Reimbursements".
    Payslips without reimbursements render exactly as the template says."""
    section = placeholders.get("reimbursement_section") or ""
    if section and not any(k in html_body for k in _REIMBURSEMENT_KEYS):
        html_body = _insert_after_salary_tables(html_body, "{{reimbursement_section}}")
        html_body = _FORMULA.sub(r"\1 + Reimbursements", html_body)
        placeholders = {
            **placeholders,
            "net_salary": placeholders["total_payable"],
            "net_payable": placeholders["total_payable"],
            "net_payable_in_words": placeholders["total_payable_in_words"],
        }
    return tr.render_template_html(
        html_body, css_styles, placeholders,
        verbatim_keys=(
            "earnings_rows", "deductions_rows", "earnings_rows_ytd", "deductions_rows_ytd",
            "reimbursement_rows", "reimbursement_section",
        ),
    )
