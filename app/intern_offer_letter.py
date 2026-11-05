"""Documents > Templates > Intern Offer Letter.

Offers whose employment type is Intern are generated from the company's
active Intern Offer Letter template (hcm_intern_offer_letter_html_templates);
every other offer -- and an intern offer when no intern template is active
-- keeps using the standard Offer Letter Template exactly as before.

* Placeholders: everything the standard Offer Letter offers
  (offer_letter_html_renderer.build_offer_letter_placeholders) plus the
  internship-specific ones in INTERN_PLACEHOLDERS (start / end date,
  duration, mode, stipend, college, letterhead, signatory). All come from
  real data (offer, candidate, company, the company's Organization Owner as
  signatory); a missing value is "—" or, for the letterhead contact line,
  simply left out.
* The predefined "Standard Internship Offer Letter" (DEFAULT_HTML /
  DEFAULT_CSS) is added -- active -- the first time a company lists its
  intern templates (Documents > Templates); a company that already has any
  intern template is never touched.
"""

from __future__ import annotations

import calendar
import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import crud, models, template_designs
from . import offer_letter_html_renderer as olr
from . import template_rendering as tr

MODEL = models.InternOfferLetterHtmlTemplate
DEFAULT_NAME = "Standard Internship Offer Letter"
_OWNER_ROLE = "Organization Owner / CEO"

# Internship-specific {{ placeholders }} (on top of the Offer Letter ones).
INTERN_PLACEHOLDERS: dict[str, str] = {
    "intern_name": "Intern's full name",
    "intern_email": "Intern's email",
    "intern_phone": "Intern's mobile number",
    "college": "College / qualification (from the candidate profile)",
    "internship_title": "Internship role, e.g. Full Stack Development Intern",
    "internship_start_date": "Start date (the offer's joining date), e.g. 1 June 2026",
    "internship_end_date": "End date, e.g. 30 June 2026",
    "internship_duration": "Duration, e.g. one-month / 3-month / 45-day",
    "internship_mode": "Mode, e.g. Online (Remote) / Office",
    "stipend": "Monthly stipend, or 'Unpaid'",
    "letter_date": "Letter date, e.g. 11th May 2026",
    "company_legal_name": "Company legal name (upper-case on the letterhead)",
    "company_cin": "Company CIN line, e.g. CIN: U72900AP2020PTC114393 (blank when not set)",
    "logo_state": "with-logo / no-logo -- on the root element's class, hides an empty logo slot",
    "letterhead_contact": "Contact No. / Email Id / Website line (missing parts left out)",
    "signatory_name": "Authorised signatory (the Organization Owner / CEO)",
    "signatory_designation": "Signatory's designation",
}

# The first predefined design (kept so a company still using it unchanged is
# upgraded to the current one -- see ensure_default_template).
_LEGACY_CSS = """
.letter { font-family: 'Times New Roman', Georgia, serif; color: #111827; font-size: 13.5px; line-height: 1.6;
  max-width: 760px; margin: 0 auto; padding: 26px 40px 34px; }
.lh { display: table; width: 100%; border-bottom: 2px solid #1f3a8a; padding-bottom: 10px; margin-bottom: 18px; }
.lh .logo-cell { display: table-cell; width: 90px; vertical-align: middle; }
.lh .logo-cell img { max-width: 80px; max-height: 70px; }
.lh .co { display: table-cell; vertical-align: middle; text-align: center; }
.lh .co-name { font-size: 17px; font-weight: bold; letter-spacing: .6px; color: #1f3a8a; text-transform: uppercase; }
.lh .co-line { font-size: 11.5px; color: #374151; }
h1.title { text-align: center; font-size: 17px; letter-spacing: 1px; text-decoration: underline; margin: 6px 0 16px; }
.date { font-weight: bold; margin-bottom: 12px; }
.to p { margin: 0; }
.to .k { font-weight: bold; }
.welcome { text-align: center; font-weight: bold; font-size: 15px; color: #1f3a8a; margin: 18px 0 12px; }
p { margin: 0 0 11px; text-align: justify; }
table.terms { width: 100%; border-collapse: collapse; margin: 6px 0 16px; }
table.terms td { border: 1px solid #d1d5db; padding: 6px 10px; font-size: 13px; }
table.terms td.k { width: 34%; background: #f3f4f6; font-weight: bold; }
.sign { margin-top: 26px; }
.sign .space { height: 46px; }
.accept { margin-top: 30px; padding-top: 12px; border-top: 1px dashed #9ca3af; font-size: 12.5px; }
.accept .row { display: table; width: 100%; margin-top: 30px; }
.accept .row div { display: table-cell; width: 50%; }
""".strip()

_LEGACY_HTML = (
    '<div class="letter">'
    '<div class="lh"><div class="logo-cell"><img src="{{company_logo}}" alt="" /></div>'
    '<div class="co"><div class="co-name">{{company_legal_name}}</div>'
    '<div class="co-line">{{company_cin}}</div>'
    '<div class="co-line">{{company_address}}</div>'
    '<div class="co-line">{{letterhead_contact}}</div></div></div>'
    '<h1 class="title">INTERNSHIP OFFER LETTER</h1>'
    '<div class="date">{{letter_date}}</div>'
    '<div class="to">'
    '<p><span class="k">Name:</span> {{intern_name}}</p>'
    '<p><span class="k">College:</span> {{college}}</p>'
    '<p><span class="k">Email:</span> {{intern_email}}</p>'
    '<p><span class="k">Mobile:</span> {{intern_phone}}</p>'
    '</div>'
    '<div class="welcome">Welcome to {{company_name}}</div>'
    '<p><strong>Congratulations!</strong> We are delighted to extend this offer to you as a '
    '<strong>{{internship_title}}</strong>. This offer letter is to confirm your selection for this internship '
    'at {{company_name}}, effective from <strong>{{internship_start_date}}</strong> '
    'till <strong>{{internship_end_date}}</strong>. This is a {{internship_duration}} internship program. '
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
    '<div class="sign"><p>Yours sincerely,<br />For <strong>{{company_legal_name}}</strong></p>'
    '<div class="space"></div>'
    '<p><strong>{{signatory_name}}</strong><br />{{signatory_designation}}</p></div>'
    '<div class="accept"><strong>Acceptance</strong><br />I, {{intern_name}}, accept this internship offer on the '
    'terms stated above.<div class="row"><div>Signature: ____________________</div>'
    '<div>Date: ____________________</div></div></div>'
    '</div>'
)


# Current predefined design: the shared letter layout (logo letterhead, terms
# table, signature, acceptance) in the "Classic Letterhead" style -- other
# styles are one click away in the editor's Designs gallery.
DEFAULT_HTML = template_designs.INTERN_OFFER_LAYOUT
DEFAULT_CSS = template_designs.theme_css("classic")

# -- CRUD (same generic helpers as the standard Offer Letter) ----------------

def list_templates(db: Session, company_id: uuid.UUID) -> list:
    return crud._list_html_templates(db, MODEL, company_id)


def get_by_id(db: Session, company_id: uuid.UUID, template_id: uuid.UUID):
    return crud._get_html_template_by_id(db, MODEL, company_id, template_id)


def get_active(db: Session, company_id: uuid.UUID):
    return db.scalar(select(MODEL).where(MODEL.company_id == company_id, MODEL.is_active.is_(True)))


def create(db: Session, company_id: uuid.UUID, *, name: str, html_body: str, css_styles: str,
           is_active: bool, created_by: uuid.UUID | None):
    now = datetime.datetime.now(datetime.timezone.utc)
    row = MODEL(id=uuid.uuid4(), company_id=company_id, name=name, html_body=html_body, css_styles=css_styles,
                is_active=False, version=1, updated_by=created_by, created_at=now, updated_at=now)
    db.add(row)
    db.flush()
    if is_active:
        crud._activate_html_template(db, MODEL, company_id, row.id)
    return row


def update(db: Session, company_id: uuid.UUID, template_id: uuid.UUID, *, name: str, html_body: str,
           css_styles: str, is_active: bool, updated_by: uuid.UUID | None):
    row = get_by_id(db, company_id, template_id)
    if row is None:
        raise ValueError("Template not found.")
    row.name, row.html_body, row.css_styles = name, html_body, css_styles
    row.version += 1
    row.updated_by = updated_by
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()
    if is_active and not row.is_active:
        crud._activate_html_template(db, MODEL, company_id, row.id)
    elif not is_active:
        row.is_active = False
        db.flush()
    return row


def delete(db: Session, company_id: uuid.UUID, template_id: uuid.UUID) -> None:
    crud._delete_html_template(db, MODEL, company_id, template_id)


def activate(db: Session, company_id: uuid.UUID, template_id: uuid.UUID):
    return crud._activate_html_template(db, MODEL, company_id, template_id)


def ensure_default_template(db: Session, company_id: uuid.UUID) -> None:
    """Adds the predefined Standard Internship Offer Letter (active) for a
    company that has no intern template at all. Flushes; caller commits."""
    rows = list(db.scalars(select(MODEL).where(MODEL.company_id == company_id)))
    for row in rows:
        # Still exactly the first predefined design (never edited): move it
        # to the current one (logo letterhead, print margins).
        if row.html_body == _LEGACY_HTML and (row.css_styles or "") == _LEGACY_CSS:
            row.html_body, row.css_styles = DEFAULT_HTML, DEFAULT_CSS
        elif row.html_body == DEFAULT_HTML and (row.css_styles or "") in template_designs.PREVIOUS_THEME_CSS:
            row.css_styles = DEFAULT_CSS
    if rows:
        db.flush()
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        with db.begin_nested():  # a concurrent first request may win the race
            db.add(MODEL(id=uuid.uuid4(), company_id=company_id, name=DEFAULT_NAME, html_body=DEFAULT_HTML,
                         css_styles=DEFAULT_CSS, is_active=True, version=1, created_at=now, updated_at=now))
    except IntegrityError:
        pass


# -- rendering -----------------------------------------------------------------

_BLANK = {"", "nan", "none", "null", "n/a", "-", "—"}


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in _BLANK else s


def is_intern_offer(detail: dict) -> bool:
    offer, opening = detail["offer"], detail.get("opening")
    et = (getattr(offer, "employment_type", None) or getattr(opening, "employment_type", None) or "")
    return et.strip().lower().replace("-", "_").replace(" ", "_") in ("intern", "internship")


def _long(d: datetime.date | None) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else "—"


def _ordinal(d: datetime.date | None) -> str:
    if d is None:
        return "—"
    n = d.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix} {d.strftime('%B %Y')}"


def _add_months(d: datetime.date, months: int) -> datetime.date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight",
          9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}


def internship_period(offer) -> tuple[datetime.date | None, datetime.date | None, str]:
    """(start, end, duration text). End = the offer's end date, else
    start + duration months - 1 day. Duration = the months given, else
    worked out from the dates ("one-month", "45-day")."""
    start = getattr(offer, "proposed_joining_date", None)
    end = getattr(offer, "contract_end_date", None)
    months = getattr(offer, "contract_duration_months", None)
    if end is None and start and months:
        end = _add_months(start, months) - datetime.timedelta(days=1)
    if not months and start and end and end >= start:
        nxt = end + datetime.timedelta(days=1)
        whole = (nxt.year - start.year) * 12 + (nxt.month - start.month)
        if whole >= 1 and nxt.day == start.day:
            months = whole
    if months:
        duration = f"{_WORDS.get(months, str(months))}-month"
    elif start and end and end >= start:
        duration = f"{(end - start).days + 1}-day"
    else:
        duration = "—"
    return start, end, duration


def _signatory(db: Session, company_id: uuid.UUID) -> tuple[str, str, str | None, str | None]:
    """(name, designation, work email, phone) of the company's Organization
    Owner / CEO -- the letter's authorised signatory."""
    row = db.execute(
        select(models.Employee)
        .join(models.User, models.User.employee_id == models.Employee.id)
        .join(models.UserRole, models.UserRole.user_id == models.User.id)
        .join(models.Role, models.Role.id == models.UserRole.role_id)
        .where(models.Role.name == _OWNER_ROLE, models.Employee.company_id == company_id,
               models.Employee.is_active.is_(True))
        .order_by(models.Employee.date_of_joining)
        .limit(1)
    ).scalar()
    if row is None:
        return "Authorised Signatory", "—", None, None
    name = " ".join(p for p in (_clean(row.first_name), _clean(row.last_name)) if p) or "Authorised Signatory"
    designation = None
    if getattr(row, "designation_id", None):
        des = db.get(models.Designation, row.designation_id)
        designation = _clean(getattr(des, "name", None))
    return name, designation or "Authorised Signatory", _clean(row.work_email), _clean(row.personal_phone)


def build_placeholders(db: Session, detail: dict, company) -> dict[str, str]:
    values = olr.build_offer_letter_placeholders(detail, company)
    offer, candidate = detail["offer"], detail.get("candidate")
    start, end, duration = internship_period(offer)
    sign_name, sign_designation, sign_email, sign_phone = _signatory(db, company.id)
    domain = _clean(getattr(company, "email_domain", None))
    contact = " | ".join(p for p in (
        f"Contact No.: {sign_phone}" if sign_phone else None,
        f"Email Id: {sign_email}" if sign_email else None,
        f"Website: www.{domain.lstrip('@')}" if domain else None,
    ) if p)
    annual = float(offer.offered_ctc) if offer.offered_ctc is not None else 0.0
    mode = _clean(getattr(offer, "work_mode", None)) or _clean(values.get("work_mode"))
    if mode and mode.lower() in ("remote", "wfh", "work from home", "online"):
        mode = "Online (Remote)"
    values.update({
        "intern_name": _clean(candidate.name if candidate else None) or "—",
        "intern_email": _clean(candidate.email if candidate else None) or "—",
        "intern_phone": _clean(candidate.phone if candidate else None) or "—",
        "college": _clean(getattr(candidate, "qualification", None)) or "—",
        "internship_title": _clean(getattr(offer, "designation", None)) or values.get("position") or "—",
        "internship_start_date": _long(start),
        "internship_end_date": _long(end),
        "internship_duration": duration,
        "internship_mode": mode or "—",
        "stipend": f"₹{annual / 12:,.2f} per month" if annual > 0 else "Unpaid",
        "letter_date": _ordinal(getattr(offer, "offer_date", None) or datetime.date.today()),
        "company_legal_name": _clean(getattr(company, "legal_name", None)) or _clean(company.name) or "—",
        "company_cin": template_designs.company_values(company)["company_cin"],
        "logo_state": template_designs.company_values(company)["logo_state"],
        "letterhead_contact": contact,
        "signatory_name": sign_name,
        "signatory_designation": sign_designation,
    })
    return values


def render(html_body: str, css_styles: str, placeholders: dict[str, str]) -> str:
    return tr.render_template_html(html_body, css_styles, placeholders, verbatim_keys=olr.VERBATIM_KEYS)
