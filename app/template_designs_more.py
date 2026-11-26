"""Documents > Templates > Designs for the template types that use their own
layouts: Payslip, Experience & Relieving Letter, F&F Settlement Statement and
every Email kind. Same contract as template_designs.designs_for (themes =
CSS, layout_html = the type's standard layout, sample values for the gallery
previews), served by routers/template_designs.py.

* Payslip / exit letters: each theme is the type's standard stylesheet plus
  overrides, so the existing placeholders and markup keep working unchanged.
* Emails: the layout is built from the kind's own insertable tokens, so every
  kind gets a design that uses exactly the values it really has.
Picking a style swaps the CSS only; "Use this design" also resets the HTML to
the standard layout -- nothing is enforced, HR can edit both afterwards."""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from . import template_design_layouts as L
from . import template_rendering as tr

MORE_DOC_TYPES = ("payslip", "experience_relieving", "fnf_statement")


# --------------------------------------------------------------------------- payslip

def _payslip_theme(key: str, name: str, description: str, extra: str) -> dict:
    return {"key": key, "name": name, "description": description, "css": (L.PAYSLIP_CSS + "\n" + extra).strip()}


PAYSLIP_THEMES = [
    _payslip_theme("standard", "Standard", "The default payslip: clean grey rules, green net-pay figure.", ""),
    _payslip_theme("corporate", "Corporate Blue", "Solid blue header band, tinted table headers, blue net pay.", """
.payslip { font-family: Arial, Helvetica, sans-serif; }
.header { background: #1e3a8a; color: #ffffff; padding: 16px 20px; border-bottom: none; border-radius: 4px; }
.header-left h1 { color: #ffffff; }
.header-left .address { color: #dbeafe; }
.title { color: #1e3a8a; border-bottom: 2px solid #1e3a8a; padding-bottom: 6px; }
.section-title { color: #1e3a8a; text-transform: uppercase; letter-spacing: 0.6px; font-size: 11.5px; }
.net-pay { background: #eff6ff; border: 1px solid #93c5fd; }
.net-pay-value { color: #1e3a8a; }
.component-table .table-head th { background: #1e3a8a; color: #ffffff; border-bottom: none; }
.component-table .table-foot td { background: #eff6ff; border-top: 1px solid #93c5fd; }
.net-payable-band { background: #eff6ff; border-left: 4px solid #1e3a8a; }
"""),
    _payslip_theme("minimal", "Minimal Mono", "Black and white, hairline rules, no boxes - prints well and saves ink.", """
.payslip { font-family: Helvetica, Arial, sans-serif; color: #111111; }
.header { border-bottom: 2px solid #111111; }
.header-left h1 { font-size: 19px; text-transform: uppercase; letter-spacing: 1.2px; }
.title { font-weight: 600; letter-spacing: 0.4px; }
.section-title { font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.8px; color: #555555; }
.net-pay { border: none; border-top: 2px solid #111111; border-bottom: 2px solid #111111; border-radius: 0; }
.net-pay-value { color: #111111; }
.component-table .table-head th { background: #ffffff; border-bottom: 2px solid #111111; text-transform: uppercase; font-size: 10.5px; letter-spacing: 0.6px; }
.component-table td { border-bottom: 1px solid #dddddd; }
.component-table .table-foot td { border-top: 2px solid #111111; }
.net-payable-band { background: #ffffff; border-left: none; border-top: 1px solid #111111; border-bottom: 1px solid #111111; padding: 10px 0; }
"""),
    _payslip_theme("emerald", "Emerald Accent", "Soft green accents with a highlighted net-pay card.", """
.payslip { font-family: "Segoe UI", Arial, Helvetica, sans-serif; }
.header { border-bottom: 3px solid #047857; }
.header-left h1 { color: #047857; }
.title { color: #065f46; }
.section-title { color: #047857; }
.net-pay { background: #ecfdf5; border: 1px solid #6ee7b7; border-radius: 8px; }
.net-pay-value { color: #047857; }
.component-table .table-head th { background: #d1fae5; color: #064e3b; border-bottom: 1px solid #6ee7b7; }
.component-table .table-foot td { border-top: 1px solid #34d399; }
.net-payable-band { background: #ecfdf5; border-left: 4px solid #047857; }
"""),
]


def _payslip_sample(base: dict) -> dict:
    def rows(items):
        return "".join(
            f"<tr><td>{n}</td><td style=\"text-align:right\">{a}</td><td style=\"text-align:right\">{y}</td></tr>"
            for n, a, y in items)
    today = datetime.date.today()
    return {**base,
            "employee_name": "Priya Sharma", "employee_id": "EMP-0042", "designation": "Software Engineer",
            "department": "Engineering", "joining_date_ddmmyyyy": "03/12/2024",
            "pay_period": today.strftime("%B %Y"), "pay_date_ddmmyyyy": today.strftime("%d/%m/%Y"),
            "bank_name": "HDFC Bank", "bank_account_no": "XXXXXX4321", "payment_mode": "Bank Transfer",
            "net_salary": "46,500.00", "paid_days": "30", "lop_days": "0",
            "gross_earnings": "50,000.00", "gross_earnings_ytd": "1,50,000.00",
            "total_deductions": "3,500.00", "net_payable_in_words": "Forty Six Thousand Five Hundred Rupees Only",
            "earnings_rows_ytd": rows([("Basic", "25,000.00", "75,000.00"), ("HRA", "12,500.00", "37,500.00"),
                                       ("Special Allowance", "12,500.00", "37,500.00")]),
            "deductions_rows_ytd": rows([("Provident Fund", "3,000.00", "9,000.00"),
                                         ("Professional Tax", "500.00", "1,500.00")])}


# --------------------------------------------------------------------------- exit letters

def _exit_themes(base_css: str) -> list[dict]:
    def theme(key, name, description, extra):
        return {"key": key, "name": name, "description": description, "css": (base_css + "\n" + extra).strip()}
    return [
        theme("standard", "Standard", "The default layout: blue letterhead rule, sans-serif body.", ""),
        theme("classic", "Classic Serif", "Traditional serif letter with a double rule under the letterhead.", """
body { font-family: Georgia, "Times New Roman", serif; }
.letterhead { border-bottom: 3px double #1e3a8a; }
h1 { letter-spacing: 2px; text-decoration: underline; font-weight: 700; }
.net { border: 1px double #1e3a8a; }
.lines th { background: #eef2ff; }
"""),
        theme("modern", "Modern Teal", "Teal accents, left-aligned title with an accent bar.", """
.letterhead { border-bottom: 4px solid #0f766e; }
.company-name { color: #0f766e; }
h1 { text-align: left; border-left: 5px solid #0f766e; padding-left: 10px; letter-spacing: 0.8px; }
.net { border: 1px solid #0f766e; background: #f0fdfa; }
.lines th { background: #ccfbf1; color: #134e4a; }
.lines tfoot td { border-top: 1px solid #0f766e; }
.facts th { color: #0f766e; }
"""),
        theme("formal", "Formal Monochrome", "Black and grey, ruled sections - formal and print-friendly.", """
.letterhead { border-bottom: 1px solid #111111; }
.company-name { color: #111111; text-transform: uppercase; letter-spacing: 1px; }
h1 { color: #111111; border-top: 1px solid #111111; border-bottom: 1px solid #111111; padding: 6px 0; }
.net { border: 1px solid #111111; background: #f9fafb; }
.lines { border: 1px solid #111111; }
.lines th { background: #e5e7eb; color: #111111; }
.lines tfoot td { border-top: 1px solid #111111; }
footer { border-top: 1px solid #111111; color: #4b5563; }
"""),
    ]


def _exit_sample(base: dict, fnf: bool) -> dict:
    today = datetime.date.today()
    long = f"{today.day} {today.strftime('%B %Y')}"
    out = {**base,
           "letter_number": f"{'FNF' if fnf else 'EXR'}/EMP-0042/{today.year}/1", "issue_date_long": long,
           "employee_name": "Priya Sharma", "employee_salutation": "Ms.", "employee_id": "EMP-0042",
           "pronoun_subject": "she", "pronoun_object": "her", "pronoun_possessive": "her",
           "designation": "Software Engineer", "date_of_joining_long": "3 December 2023",
           "last_working_day_long": long, "resignation_date_long": "1 October 2026",
           "relieving_date_long": long, "tenure": "2 years, 11 months",
           "full_and_final_statement": "Her full and final settlement will be processed as per company policy.",
           "company_registration": "CIN: U00000KA2020PTC000000",
           "authorized_signatory": "Anita Rao", "authorized_signatory_designation": "Head - Human Resources",
           "authorized_signature": tr.TRANSPARENT_PIXEL_DATA_URL, "company_seal": tr.TRANSPARENT_PIXEL_DATA_URL}
    if fnf:
        def rows(items):
            return "".join(f'<tr><td>{n}</td><td class="amount">{a}</td></tr>' for n, a in items)
        out.update({
            "earnings_rows": rows([("Salary for notice period", "₹50,000.00"), ("Leave encashment", "₹12,000.00")]),
            "deductions_rows": rows([("Notice shortfall recovery", "₹5,000.00")]),
            "total_earnings": "₹62,000.00", "total_deductions": "₹5,000.00", "net_amount": "₹57,000.00",
            "net_amount_label": "Net amount payable",
            "net_amount_words": "Fifty Seven Thousand Rupees Only", "settlement_status": "Approved",
            "settlement_approved_by": "Anita Rao", "settlement_approved_date": long,
            "payment_date": long, "payment_mode": "Bank Transfer", "payment_reference": "UTR0000000001",
            "payment_details": "", "settlement_notes": ""})
    return out


# --------------------------------------------------------------------------- emails

_SPECIAL = {"heading", "intro_line", "hrms_url", "cta_label", "company_name"}


def _label(token: str) -> str:
    return token.replace("_", " ").strip().title()


def email_layout(kind: str, tokens: list[str]) -> str:
    """A complete design for [kind] made only of tokens this kind really has."""
    title = "{{heading}}" if "heading" in tokens else _label(kind)
    parts = ['<div class="em"><div class="em-band"></div>', f'<h1 class="em-title">{title}</h1>']
    if "intro_line" in tokens:
        parts.append('<p class="em-text">{{intro_line}}</p>')
    plain = [t for t in tokens if t not in _SPECIAL and not t.endswith("_html")]
    if plain:
        parts.append('<table class="em-table">' + "".join(
            f'<tr><td class="k">{_label(t)}</td><td>{{{{{t}}}}}</td></tr>' for t in plain) + "</table>")
    for t in tokens:
        if t.endswith("_html") and t != "action_buttons_html":
            parts.append(f'<div class="em-block">{{{{{t}}}}}</div>')
    if "action_buttons_html" in tokens:
        parts.append('<div class="em-block">{{action_buttons_html}}</div>')
    if "hrms_url" in tokens:
        label = "{{cta_label}}" if "cta_label" in tokens else "Open in HRMS"
        parts.append(f'<p class="em-cta-wrap"><a class="em-cta" href="{{{{hrms_url}}}}">{label}</a></p>')
    parts.append('<p class="em-sign">Regards,<br />{{company_name}}</p></div>')
    return "".join(parts)


def _email_css(accent: str, tint: str, text: str, radius: str, title_align: str, font: str) -> str:
    return f"""
.em {{ font-family: {font}; color: {text}; font-size: 14px; line-height: 1.6; }}
.em-band {{ height: 4px; background: {accent}; border-radius: {radius}; margin: 0 0 18px; }}
.em-title {{ margin: 0 0 12px; font-size: 20px; font-weight: 800; color: {accent}; text-align: {title_align}; }}
.em-text {{ margin: 0 0 14px; }}
.em-table {{ width: 100%; border-collapse: collapse; background: {tint}; border-radius: {radius}; margin: 0 0 16px; }}
.em-table td {{ padding: 8px 14px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
.em-table td.k {{ width: 36%; color: #64748b; font-size: 12.5px; }}
.em-block {{ margin: 0 0 16px; }}
.em-cta-wrap {{ margin: 18px 0; text-align: {title_align}; }}
.em-cta {{ display: inline-block; background: {accent}; color: #ffffff; text-decoration: none; padding: 10px 22px; border-radius: {radius}; font-weight: 700; font-size: 13.5px; }}
.em-sign {{ margin: 22px 0 0; color: #64748b; font-size: 13px; }}
""".strip()


EMAIL_THEMES = [
    {"key": "clean-blue", "name": "Clean Blue", "description": "Blue accent bar, rounded detail card, friendly sans-serif.",
     "css": _email_css("#2564cf", "#f8fafc", "#334155", "8px", "left", "'Segoe UI', Roboto, Helvetica, Arial, sans-serif")},
    {"key": "forest", "name": "Forest Green", "description": "Calm green accents - good for approvals and celebrations.",
     "css": _email_css("#15803d", "#f0fdf4", "#1f2937", "10px", "left", "'Segoe UI', Roboto, Helvetica, Arial, sans-serif")},
    {"key": "sunset", "name": "Warm Sunset", "description": "Orange accents with a centered title and button.",
     "css": _email_css("#ea580c", "#fff7ed", "#44403c", "12px", "center", "'Trebuchet MS', 'Segoe UI', Arial, sans-serif")},
    {"key": "mono", "name": "Minimal Mono", "description": "Black and white, square corners - understated and formal.",
     "css": _email_css("#111827", "#f9fafb", "#1f2937", "0", "left", "Georgia, 'Times New Roman', serif")},
]

_TOKEN_SAMPLES = {
    "employee_name": "Priya Sharma", "designation": "Software Engineer", "department": "Engineering",
    "years_of_service": "3", "holiday_name": "Diwali", "holiday_date": "12 November 2026",
    "holiday_type": "Public holiday", "branch_name": "Head Office", "new_designation": "Senior Software Engineer",
    "previous_designation": "Software Engineer", "approved_by": "Rahul Verma", "rejected_by": "Rahul Verma",
    "rejection_reason": "Team capacity during that week.", "reason": "Family function", "status": "Approved",
    "candidate_name": "Arjun Mehta", "job_title": "Backend Engineer", "role": "Backend Engineer",
    "interview_date": "18 November 2026", "interview_time": "11:00 AM", "company_name": "Your Company",
}


def email_sample(base: dict, kind: str, tokens: list[str]) -> dict:
    out = dict(base)
    for t in tokens:
        if t in out and t != "company_name":
            continue
        if t == "heading":
            out[t] = _label(kind)
        elif t == "intro_line":
            out[t] = "Here are the details you need to know."
        elif t == "hrms_url":
            out[t] = "#"
        elif t == "cta_label":
            out[t] = "Open in HRMS"
        elif t == "details_html":
            out[t] = ('<table style="width:100%"><tr><td>From</td><td><strong>3 Dec 2026</strong></td></tr>'
                      '<tr><td>To</td><td><strong>5 Dec 2026</strong></td></tr></table>')
        elif t == "action_buttons_html":
            out[t] = '<p><a href="#">Approve</a> &nbsp; <a href="#">Reject</a></p>'
        elif t.endswith("_html"):
            out[t] = "<p>Sample message content goes here.</p>"
        else:
            out[t] = _TOKEN_SAMPLES.get(t) or f"Sample {_label(t).lower()}"
    out["company_name"] = base.get("company_name") or out.get("company_name", "Your Company")
    return out


# --------------------------------------------------------------------------- entry points

def designs_for_more(db: Session, company, doc_type: str) -> dict:
    from . import template_designs as td
    base = {"company_name": getattr(company, "name", None) or "Your Company",
            "company_address": td._sample_values(db, company, "offer")["company_address"],
            **td.company_values(company)}
    if doc_type == "payslip":
        return {"doc_type": doc_type, "layout_html": L.PAYSLIP_LAYOUT, "themes": PAYSLIP_THEMES,
                "sample": _payslip_sample({**base, "company_logo": td.logo_values(company).get("company_logo", "")}),
                "verbatim_keys": ["earnings_rows_ytd", "deductions_rows_ytd", "earnings_rows", "deductions_rows"]}
    fnf = doc_type == "fnf_statement"
    return {"doc_type": doc_type, "layout_html": L.FNF_LAYOUT if fnf else L.EXPERIENCE_LAYOUT,
            "themes": _exit_themes(L.FNF_CSS if fnf else L.EXPERIENCE_CSS),
            "sample": _exit_sample(base, fnf),
            "verbatim_keys": ["earnings_rows", "deductions_rows"]}


def designs_for_email(db: Session, company, kind: str, tokens: list[str]) -> dict:
    from . import template_designs as td
    base = {"company_name": getattr(company, "name", None) or "Your Company"}
    return {"doc_type": f"email:{kind}", "layout_html": email_layout(kind, tokens), "themes": EMAIL_THEMES,
            "sample": email_sample(base, kind, tokens),
            "verbatim_keys": [t for t in tokens if t.endswith("_html")]}
