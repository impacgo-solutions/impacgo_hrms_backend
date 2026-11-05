"""Documents > Templates > Designs: ready-made, print-aligned styles for the
letter-type templates (Offer Letter, Intern Offer Letter, NDA, Freelancer
Agreement), shown in the template editor's "Designs" gallery.

Every letter uses one shared, semantic layout (LAYOUT CLASSES below), so a
style is just CSS: picking a style swaps the CSS and keeps the text, and
"Use this design" also resets the HTML to the standard layout for that
document type. HR can then edit both freely -- nothing here is enforced.

Layout classes
    .doc (+ {{logo_state}} = with-logo / no-logo)   page body
    header.lh > .lh-logo img{{company_logo}} + .lh-co > .lh-name, .lh-line*
    .lh-rule              rule under the letterhead
    .doc-title            document title
    .doc-meta             date (left) / reference (right)
    .doc-to (.k labels)   recipient block
    .doc-body p, h2       body text, clause headings
    table.terms td.k      key / value terms
    table.parties         agreement parties (two cards)
    .doc-sign (.space)    signature block
    .doc-accept (.row)    acceptance / countersignature
    footer.doc-foot       footer line

Print: every style sets its own A4 page margins (BASE_PRINT_CSS uses 0),
so long letters keep a margin on page 2+, and hides the logo slot when the
company has no logo (logo_state = no-logo) -- the letterhead then aligns
without an empty gap. Empty letterhead lines (no CIN, no contact) collapse.
"""

from __future__ import annotations

import datetime

from sqlalchemy.orm import Session

from . import template_rendering as tr

DOC_TYPES = ("offer", "intern_offer", "nda", "agreement")

_COMMON = """
.doc { color: #1f2937; }
.lh { display: flex; align-items: center; gap: 16px; }
.lh-logo img { display: block; max-height: 64px; max-width: 150px; width: auto; height: auto; object-fit: contain; }
.no-logo .lh-logo { display: none; }
.lh-co { flex: 1; min-width: 0; }
.lh-line:empty, .doc-meta span:empty { display: none; }
.doc-meta { display: flex; justify-content: space-between; gap: 12px; }
.doc-to p { margin: 0 0 2px; }
.doc-to { margin-bottom: 14px; }
.doc-to .k { font-weight: bold; }
.doc-body p { margin: 0 0 10px; text-align: justify; }
table.terms { width: 100%; border-collapse: collapse; margin: 6px 0 14px; }
table.compensation-terms { margin: 2px 0 12px; }
table.terms td { padding: 7px 10px; vertical-align: top; }
table.terms td.k { width: 34%; font-weight: bold; }
table.parties { width: 100%; border-collapse: separate; border-spacing: 10px 0; margin: 4px -10px 12px; }
table.parties td { width: 50%; vertical-align: top; padding: 10px 14px; }
table.parties .role { font-size: 10.5px; text-transform: uppercase; letter-spacing: .7px; margin-bottom: 2px; }
.doc-sign { margin-top: 24px; page-break-inside: avoid; }
.doc-sign .space { height: 42px; }
.doc-sign p { margin: 0; }
.doc-accept { margin-top: 26px; padding-top: 12px; page-break-inside: avoid; }
.doc-accept .row { display: flex; justify-content: space-between; gap: 24px; margin-top: 28px; }
.muted { color: #6b7280; }
"""

THEMES: list[dict] = [
    {
        "key": "classic",
        "name": "Classic Letterhead",
        "description": "Logo left, company details centred, double rule -- traditional and formal.",
        "css": _COMMON + """
@page { size: A4; margin: 14mm 0 16mm; }
.doc { font-family: Georgia, 'Times New Roman', serif; font-size: 13.5px; line-height: 1.6; padding: 4mm 18mm 0; }
.lh { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 3fr) minmax(0, 1fr); }
.lh-logo { grid-column: 1; justify-self: start; }
.lh-logo img { max-width: 100%; max-height: 60px; }
.lh-co { grid-column: 2; text-align: center; }
.no-logo .lh-co { grid-column: 1 / span 3; }
.lh-name { font-size: 18px; font-weight: bold; letter-spacing: .8px; text-transform: uppercase; color: #1e3a8a; }
.lh-line { font-size: 11.5px; color: #374151; }
.lh-rule { border-top: 3px double #1e3a8a; margin: 10px 0 18px; }
.doc-title { text-align: center; font-size: 17px; letter-spacing: 1.2px; text-transform: uppercase; text-decoration: underline; margin: 4px 0 16px; }
.doc-meta { font-weight: bold; margin-bottom: 12px; }
.doc-body h2 { font-size: 14px; margin: 14px 0 4px; color: #1e3a8a; }
table.terms td { border: 1px solid #d1d5db; }
table.terms td.k { background: #f3f4f6; }
table.parties td { border: 1px solid #d1d5db; border-radius: 6px; background: #f9fafb; }
.doc-accept { border-top: 1px dashed #9ca3af; }
footer.doc-foot { margin-top: 22px; text-align: center; font-size: 10.5px; color: #6b7280; }
""",
    },
    {
        "key": "modern",
        "name": "Modern Accent",
        "description": "Accent bar, logo left with details right-aligned, clean sans-serif.",
        "css": _COMMON + """
@page { size: A4; margin: 14mm 0 16mm; }
@page :first { margin-top: 0; }
.doc { font-family: 'Segoe UI', Arial, Helvetica, sans-serif; font-size: 13px; line-height: 1.6;
  border-top: 8px solid #2563eb; padding: 12mm 18mm 0; }
.lh { justify-content: space-between; }
.lh-co { text-align: right; }
.lh-name { font-size: 17px; font-weight: 700; color: #111827; }
.lh-line { font-size: 11px; color: #6b7280; }
.lh-rule { height: 1px; background: #e5e7eb; margin: 12px 0 20px; }
.doc-title { font-size: 20px; font-weight: 700; color: #2563eb; margin: 0 0 4px; }
.doc-meta { color: #6b7280; font-size: 12px; margin-bottom: 16px; }
.doc-to { border-left: 3px solid #2563eb; padding: 4px 0 4px 12px; margin-bottom: 14px; }
.doc-body h2 { font-size: 13.5px; margin: 14px 0 4px; color: #2563eb; }
table.terms tr:nth-child(odd) td { background: #f5f8ff; }
table.terms td { border-bottom: 1px solid #e5e7eb; }
table.terms td.k { color: #1e40af; }
table.parties td { background: #f5f8ff; border-radius: 8px; }
table.parties .role { color: #2563eb; }
.doc-accept { border-top: 1px solid #e5e7eb; }
footer.doc-foot { margin-top: 22px; padding-top: 8px; border-top: 1px solid #e5e7eb; font-size: 10.5px; color: #9ca3af; }
""",
    },
    {
        "key": "corporate",
        "name": "Corporate Band",
        "description": "Full-width coloured header band with the logo on a white tile -- bold and branded.",
        "css": _COMMON + """
@page { size: A4; margin: 14mm 0 16mm; }
@page :first { margin-top: 0; }
.doc { font-family: Calibri, 'Segoe UI', Arial, sans-serif; font-size: 13px; line-height: 1.6; }
.lh { background: #0f2a4a; color: #ffffff; padding: 14px 18mm; }
.lh-logo { background: #ffffff; border-radius: 8px; padding: 6px; }
.lh-logo img { max-height: 52px; }
.lh-name { font-size: 18px; font-weight: 700; letter-spacing: .5px; }
.lh-line { font-size: 11px; color: #cbd5e1; }
.lh-rule { height: 4px; background: #f59e0b; margin: 0 0 18px; }
.doc-title, .doc-meta, .doc-to, .doc-body, .doc-sign, .doc-accept, footer.doc-foot { margin-left: 18mm; margin-right: 18mm; }
.doc-title { text-align: center; font-size: 17px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: #0f2a4a; margin-top: 0; margin-bottom: 14px; }
.doc-meta { font-weight: 600; margin-bottom: 12px; }
.doc-body h2 { font-size: 13.5px; margin: 14px 0 4px; color: #0f2a4a; text-transform: uppercase; letter-spacing: .4px; }
table.terms td { border: 1px solid #cbd5e1; }
table.terms td.k { background: #0f2a4a; color: #ffffff; }
table.parties td { border: 1px solid #cbd5e1; border-top: 3px solid #f59e0b; }
.doc-accept { border-top: 1px dashed #94a3b8; }
footer.doc-foot { margin-top: 22px; padding: 8px 0; text-align: center; font-size: 10.5px; color: #475569; border-top: 2px solid #0f2a4a; }
""",
    },
    {
        "key": "minimal",
        "name": "Minimal",
        "description": "Small logo beside the name, hairline rules and generous white space.",
        "css": _COMMON + """
@page { size: A4; margin: 18mm 0 18mm; }
.doc { font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 12.5px; line-height: 1.7; color: #374151; padding: 0 22mm; }
.lh { gap: 12px; }
.lh-logo img { max-height: 40px; }
.lh-name { font-size: 15px; font-weight: 600; color: #111827; letter-spacing: .3px; }
.lh-line { font-size: 10.5px; color: #9ca3af; }
.lh-rule { height: 1px; background: #e5e7eb; margin: 14px 0 26px; }
.doc-title { font-size: 12px; font-weight: 600; letter-spacing: 3px; text-transform: uppercase; color: #6b7280; margin: 0 0 18px; }
.doc-meta { font-size: 11.5px; color: #9ca3af; margin-bottom: 18px; }
.doc-body h2 { font-size: 12.5px; font-weight: 600; margin: 16px 0 4px; color: #111827; }
table.terms td { border-bottom: 1px solid #f0f0f0; }
table.terms td.k { font-weight: 600; color: #6b7280; }
table.parties td { border: 1px solid #f0f0f0; }
.doc-accept { border-top: 1px solid #f0f0f0; }
footer.doc-foot { margin-top: 26px; font-size: 10px; color: #9ca3af; }
""",
    },
    {
        "key": "elegant",
        "name": "Elegant Centred",
        "description": "Logo centred above the company name, serif headings with a gold accent.",
        "css": _COMMON + """
@page { size: A4; margin: 14mm 0 16mm; }
.doc { font-family: 'Palatino Linotype', 'Book Antiqua', Georgia, serif; font-size: 13.5px; line-height: 1.65; padding: 2mm 20mm 0; }
.lh { flex-direction: column; text-align: center; gap: 6px; }
.lh-logo img { max-height: 70px; margin: 0 auto; }
.lh-name { font-size: 19px; font-weight: bold; letter-spacing: 2px; text-transform: uppercase; color: #1f2937; }
.lh-line { font-size: 11px; color: #6b7280; letter-spacing: .3px; }
.lh-rule { width: 120px; height: 2px; background: #b08d3c; margin: 12px auto 20px; }
.doc-title { text-align: center; font-size: 18px; font-weight: normal; letter-spacing: 3px; text-transform: uppercase; color: #7a5c1e; margin: 0 0 18px; }
.doc-meta { font-style: italic; color: #4b5563; margin-bottom: 14px; }
.doc-body h2 { font-size: 14px; margin: 14px 0 4px; color: #7a5c1e; }
table.terms td { border-bottom: 1px solid #e7dcc4; }
table.terms td.k { color: #7a5c1e; }
table.parties td { border: 1px solid #e7dcc4; background: #fdfaf3; }
table.parties .role { color: #7a5c1e; }
.doc-accept { border-top: 1px solid #e7dcc4; }
footer.doc-foot { margin-top: 24px; text-align: center; font-size: 10.5px; color: #9ca3af; letter-spacing: .5px; }
""",
    },
]

THEMES_BY_KEY = {t["key"]: t for t in THEMES}

# Earlier versions of the built-in styles: a template still carrying one of
# these exactly (never edited) is moved to the current version.
PREVIOUS_THEME_CSS: set[str] = {
    (_COMMON + """
@page { size: A4; margin: 14mm 0 16mm; }
.doc { font-family: Georgia, 'Times New Roman', serif; font-size: 13.5px; line-height: 1.6; padding: 4mm 18mm 0; }
.lh-logo { width: 96px; }
.lh-co { text-align: center; }
.with-logo .lh-co { padding-right: 96px; }
.lh-name { font-size: 18px; font-weight: bold; letter-spacing: .8px; text-transform: uppercase; color: #1e3a8a; }
.lh-line { font-size: 11.5px; color: #374151; }
.lh-rule { border-top: 3px double #1e3a8a; margin: 10px 0 18px; }
.doc-title { text-align: center; font-size: 17px; letter-spacing: 1.2px; text-transform: uppercase; text-decoration: underline; margin: 4px 0 16px; }
.doc-meta { font-weight: bold; margin-bottom: 12px; }
.doc-body h2 { font-size: 14px; margin: 14px 0 4px; color: #1e3a8a; }
table.terms td { border: 1px solid #d1d5db; }
table.terms td.k { background: #f3f4f6; }
table.parties td { border: 1px solid #d1d5db; border-radius: 6px; background: #f9fafb; }
.doc-accept { border-top: 1px dashed #9ca3af; }
footer.doc-foot { margin-top: 22px; text-align: center; font-size: 10.5px; color: #6b7280; }
""").strip(),
}


def theme_css(key: str) -> str:
    return THEMES_BY_KEY[key]["css"].strip()


# -- shared letterhead ---------------------------------------------------------

def letterhead(*lines: str) -> str:
    """Letterhead markup: logo + legal name + the given {{placeholder}} lines."""
    return (
        '<header class="lh"><div class="lh-logo"><img src="{{company_logo}}" alt="" /></div>'
        '<div class="lh-co"><div class="lh-name">{{company_legal_name}}</div>'
        + "".join(f'<div class="lh-line">{line}</div>' for line in lines)
        + '</div></header><div class="lh-rule"></div>'
    )


OFFER_LAYOUT = (
    '<div class="doc {{logo_state}}">'
    + letterhead("{{company_cin}}", "{{company_address}}")
    + '<h1 class="doc-title">Offer of Employment</h1>'
    '<div class="doc-meta"><span>{{letter_date}}</span><span>Valid until {{offer_expiry_date_long}}</span></div>'
    '<div class="doc-to"><p><span class="k">{{candidate_name}}</span></p>'
    '<p>{{candidate_email}}</p><p>{{candidate_phone}}</p></div>'
    '<div class="doc-body">'
    '<p>Dear {{candidate_name}},</p>'
    '<p>We are pleased to offer you the position of <strong>{{designation}}</strong> in the '
    '<strong>{{department}}</strong> department at {{company_name}}, based at our {{branch}} location. '
    'We were impressed by your background and believe you will be a valuable addition to our team.</p>'
    '<table class="terms">'
    '<tr><td class="k">Designation</td><td>{{designation}}</td></tr>'
    '<tr><td class="k">Department</td><td>{{department}}</td></tr>'
    '<tr><td class="k">Employment Type</td><td>{{employment_type}}</td></tr>'
    '<tr><td class="k">Work Mode</td><td>{{work_mode}}</td></tr>'
    '<tr><td class="k">Date of Joining</td><td>{{joining_date_long}}</td></tr>'
    '<tr><td class="k">Reporting Manager</td><td>{{reporting_manager}}</td></tr>'
    '<tr><td class="k">Working Hours</td><td>{{working_hours}}</td></tr>'
    '</table>'
    '<p><strong>Compensation &amp; terms</strong></p>'
    '{{compensation_terms}}'
    '<p><strong>Benefits:</strong> {{benefits}}</p>'
    '<p><strong>Other terms:</strong> {{other_terms}}</p>'
    '<p>This offer is subject to the successful verification of your documents and references. Please confirm '
    'your acceptance by {{offer_expiry_date_long}}. For any questions, please contact {{hr_contact}}.</p>'
    '<p>We look forward to welcoming you to {{company_name}}.</p>'
    '</div>'
    '<div class="doc-sign"><p>Yours sincerely,</p><p>For <strong>{{company_legal_name}}</strong></p>'
    '<div class="space"></div><p><strong>{{hr_contact}}</strong></p><p class="muted">Authorised Signatory</p></div>'
    '<div class="doc-accept"><strong>Acceptance</strong><br />I, {{candidate_name}}, accept this offer on the terms '
    'stated above.<div class="row"><div>Signature: ____________________</div><div>Date: ____________________</div>'
    '</div></div>'
    '<footer class="doc-foot">{{company_legal_name}} &middot; {{company_address}}</footer>'
    '</div>'
)

INTERN_OFFER_LAYOUT = (
    '<div class="doc {{logo_state}}">'
    + letterhead("{{company_cin}}", "{{company_address}}", "{{letterhead_contact}}")
    + '<h1 class="doc-title">Internship Offer Letter</h1>'
    '<div class="doc-meta"><span>{{letter_date}}</span><span></span></div>'
    '<div class="doc-to">'
    '<p><span class="k">Name:</span> {{intern_name}}</p>'
    '<p><span class="k">College:</span> {{college}}</p>'
    '<p><span class="k">Email:</span> {{intern_email}}</p>'
    '<p><span class="k">Mobile:</span> {{intern_phone}}</p>'
    '</div>'
    '<div class="doc-body">'
    '<p style="text-align:center"><strong>Welcome to {{company_name}}</strong></p>'
    '<p><strong>Congratulations!</strong> We are delighted to extend this offer to you as a '
    '<strong>{{internship_title}}</strong>. This offer letter is to confirm your selection for this internship '
    'at {{company_name}}, effective from <strong>{{internship_start_date}}</strong> till '
    '<strong>{{internship_end_date}}</strong>. This is a {{internship_duration}} internship program. '
    'We hope you will make the most of this experience.</p>'
    '<p>This internship is recognised by {{company_name}} as a valuable learning opportunity for you. Given your '
    'academic background and the skills you demonstrated during our selection process, we are confident you will '
    'make a meaningful contribution during your tenure.</p>'
    '<p>Your internship will include an orientation and will place strong emphasis on learning new skills and '
    'deepening your understanding of the concepts through hands-on application. You will work on real-world '
    'projects that build on your existing skills while broadening your practical knowledge as an intern.</p>'
    '<table class="terms">'
    '<tr><td class="k">Internship Role</td><td>{{internship_title}}</td></tr>'
    '<tr><td class="k">Department</td><td>{{department}}</td></tr>'
    '<tr><td class="k">Duration</td><td>{{internship_start_date}} to {{internship_end_date}} ({{internship_duration}})</td></tr>'
    '<tr><td class="k">Mode</td><td>{{internship_mode}}</td></tr>'
    '<tr><td class="k">Working Hours</td><td>{{working_hours}}</td></tr>'
    '<tr><td class="k">Stipend</td><td>{{stipend}}</td></tr>'
    '<tr><td class="k">Reporting To</td><td>{{reporting_manager}}</td></tr>'
    '</table>'
    '<p>You will acknowledge your obligation to perform all work allocated to you to the best of your ability within '
    'lawful and reasonable direction given to you, and to keep all company and client information confidential '
    'during and after the internship.</p>'
    '<p>We look forward to a worthwhile and fruitful association that will equip you with the skills and exposure '
    'needed for your future career.</p>'
    '<p>Wishing you the most enjoyable and truly meaningful internship experience.</p>'
    '</div>'
    '<div class="doc-sign"><p>Yours sincerely,</p><p>For <strong>{{company_legal_name}}</strong></p>'
    '<div class="space"></div><p><strong>{{signatory_name}}</strong></p><p>{{signatory_designation}}</p></div>'
    '<div class="doc-accept"><strong>Acceptance</strong><br />I, {{intern_name}}, accept this internship offer on '
    'the terms stated above.<div class="row"><div>Signature: ____________________</div>'
    '<div>Date: ____________________</div></div></div>'
    '</div>'
)


def agreement_layout(title: str, subtitle: str, clauses: list[tuple[str, str]]) -> str:
    """NDA / Agreement HTML in the shared layout (used by app/agreements.py)."""
    body = "".join(f"<h2>{i}. {h}</h2><p>{p}</p>" for i, (h, p) in enumerate(clauses, start=1))
    return (
        '<div class="doc {{logo_state}}">'
        + letterhead("{{company_cin}}", "{{company_address}}")
        + f'<h1 class="doc-title">{title}</h1>'
        f'<div class="doc-meta"><span>{subtitle}</span><span></span></div>'
        '<div class="doc-body">'
        "<p>This document is entered into on <strong>{{effective_date_long}}</strong> between the parties below.</p>"
        '<table class="parties"><tr>'
        '<td><div class="role">The Company</div><strong>{{company_legal_name}}</strong>'
        '<div class="muted">{{company_address}}</div></td>'
        '<td><div class="role">The Freelancer</div><strong>{{employee_name}}</strong>'
        '<div class="muted">Employee ID: {{employee_id}}</div>'
        '<div class="muted">Email: {{employee_email}}</div>'
        '<div class="muted">Address: {{employee_address}}</div></td>'
        "</tr></table>"
        f"{body}"
        "</div>"
        '<div class="doc-accept"><div class="row">'
        '<div><strong>For {{company_name}}</strong><br /><span class="muted">Authorised signatory</span></div>'
        '<div><strong>{{employee_name}}</strong><br />'
        '<span class="muted">Accepted electronically on {{effective_date_long}}</span></div></div></div>'
        "</div>"
    )


# -- shared placeholder helpers -------------------------------------------------

def logo_values(company) -> dict[str, str]:
    """company_logo + logo_state ('with-logo' / 'no-logo') for any letter."""
    has_logo = bool((getattr(company, "logo_url", None) or "").strip()) if company is not None else False
    return {
        "company_logo": tr.resolve_company_logo(company) if company is not None else tr.TRANSPARENT_PIXEL_DATA_URL,
        "logo_state": "with-logo" if has_logo else "no-logo",
    }


def ordinal_date(d: datetime.date | None) -> str:
    if d is None:
        return "—"
    n = d.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix} {d.strftime('%B %Y')}"


def company_values(company) -> dict[str, str]:
    """Letterhead values shared by every letter type."""
    name = (getattr(company, "name", None) or "").strip() or "—"
    legal = (getattr(company, "legal_name", None) or "").strip()
    cin = (getattr(company, "cin", None) or "").strip()
    return {
        "company_legal_name": legal if legal and legal.lower() not in ("nan", "none") else name,
        "company_cin": f"CIN: {cin}" if cin and cin.lower() not in ("nan", "none") else "",
        **logo_values(company),
    }


# -- gallery samples (client-side preview only) ---------------------------------

def _sample_values(db: Session, company, doc_type: str) -> dict[str, str]:
    """Realistic sample values for the gallery previews -- the company's own
    name / address / logo, made-up person details."""
    today = datetime.date.today()
    long = f"{today.day} {today.strftime('%B %Y')}"
    address = ", ".join(str(p) for p in (
        getattr(company, "address_line1", None), getattr(company, "city", None),
        getattr(company, "state", None), getattr(company, "pincode", None)) if p) or "Company address"
    base = {
        "company_name": getattr(company, "name", None) or "Your Company",
        "company_address": address,
        "current_date": today.isoformat(),
        "letter_date": ordinal_date(today),
        **company_values(company),
    }
    terms = ('<table class="terms"><tr><td class="k">Annual CTC</td><td>₹6,00,000.00</td></tr>'
             '<tr><td class="k">Probation period</td><td>6 months</td></tr>'
             '<tr><td class="k">Notice period</td><td>30 days</td></tr></table>')
    person = {
        "offer": {"candidate_name": "Priya Sharma", "candidate_email": "priya.sharma@example.com",
                  "candidate_phone": "+91 98765 43210", "designation": "Software Engineer",
                  "position": "Software Engineer", "role": "Software Engineer", "department": "Engineering",
                  "branch": "Head Office", "employment_type": "Full Time", "work_mode": "Office",
                  "joining_date": (today + datetime.timedelta(days=30)).isoformat(),
                  "joining_date_long": "3 December 2026", "offer_expiry_date_long": "10 November 2026",
                  "reporting_manager": "Rahul Verma", "hr_contact": "Anita Rao", "working_hours": "9:30 AM – 6:30 PM",
                  "offer_expiry_date": (today + datetime.timedelta(days=7)).isoformat(),
                  "benefits": "Health insurance, paid leave", "other_terms": "As per company policy",
                  "compensation_terms": terms, "offered_ctc": "6,00,000.00",
                  "offered_ctc_in_words": "Six Lakh Rupees Only"},
        "intern_offer": {"intern_name": "Surakathula Momina", "college": "B.Tech, GITAM University",
                         "intern_email": "intern@example.com", "intern_phone": "+91 89518 72393",
                         "internship_title": "Full Stack Development Intern", "department": "Engineering",
                         "internship_start_date": "1 June 2026", "internship_end_date": "30 June 2026",
                         "internship_duration": "one-month", "internship_mode": "Online (Remote)",
                         "working_hours": "10:00 AM – 5:00 PM", "stipend": "Unpaid",
                         "reporting_manager": "Rahul Verma", "signatory_name": "Authorised Signatory",
                         "signatory_designation": "Founder & CEO",
                         "letterhead_contact": "Contact No.: +91 98663 23418 | Email Id: hr@example.com"},
    }.get(doc_type, {"employee_name": "Priya Sharma", "employee_id": "FL-0001",
                     "employee_email": "priya.sharma@example.com", "employee_address": "Bengaluru, India",
                     "effective_date_long": long, "designation": "UI/UX Designer",
                     "reporting_manager": "Rahul Verma", "date_of_joining_long": long,
                     "rate": "INR 500.00 per hour"})
    return {**base, **person}


def standard_layout(doc_type: str) -> str:
    if doc_type == "offer":
        return OFFER_LAYOUT
    if doc_type == "intern_offer":
        return INTERN_OFFER_LAYOUT
    from . import agreements  # the predefined NDA / Agreement's text
    first = next(b for b in agreements.BUILTIN_TEMPLATES if b["doc_type"] == doc_type)
    return first["html"]


def designs_for(db: Session, company, doc_type: str) -> dict:
    """GET /api/template-designs/{doc_type}: styles + this type's standard
    layout + sample values (with the company's own logo / name) for the
    gallery previews."""
    return {
        "doc_type": doc_type,
        "layout_html": standard_layout(doc_type),
        "themes": [{"key": t["key"], "name": t["name"], "description": t["description"], "css": theme_css(t["key"])}
                   for t in THEMES],
        "sample": _sample_values(db, company, doc_type),
        "verbatim_keys": ["compensation_terms"],
    }

