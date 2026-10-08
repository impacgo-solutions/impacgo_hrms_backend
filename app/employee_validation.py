"""Input validation for employee / organisation data (QA M-01, M-02, L-08,
H-21).

Pydantic-level normalisers (used as Annotated types in schemas.py) and the
DB-level reference checks the routers / crud.create_employee call.

Choices (documented for HR):
  * Status / employment type / work mode are a closed set. Input is
    normalised case- and separator-insensitively ("full_time", "Full-Time",
    "full time" -> "Full-time"), so every spelling already stored in tenant
    data is still accepted. The stored value is the display label the app's
    own forms send (Active, Probation, Full-time, Office, ...).
  * The exit statuses access_lifecycle.EXITED_STATUSES uses (inactive,
    terminated, exited, resigned, relieved, absconded, separated,
    deactivated) are all accepted, so status-driven login revocation keeps
    working.
  * Minimum age at joining: 14 years -- the legal floor for any employment
    in India (Child and Adolescent Labour Act, 1986 as amended 2016);
    apprentices / interns aged 14-17 remain possible. Maximum age 100.
  * Joining date: not before 1950-01-01 and at most 365 days in the future.
  * Annual CTC: 0 .. 1,000,000,000 (Rs 100 crore).
  * Band: 1 .. 99, and must be one of the company's bands when it has any.
"""

from __future__ import annotations

import datetime
import re
import uuid
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator

MIN_AGE_YEARS = 14
MAX_AGE_YEARS = 100
MAX_JOINING_DAYS_AHEAD = 365
EARLIEST_JOINING = datetime.date(1950, 1, 1)
MAX_ANNUAL_CTC = 1_000_000_000
MAX_BAND = 99


class EmployeeInputError(ValueError):
    """Bad input detected below the schema layer -> HTTP 422 (a plain
    ValueError from crud.create_employee stays a 409 conflict)."""


def _key(value: str) -> str:
    return re.sub(r"[\s_\-]+", "_", value.strip().lower())


EMPLOYEE_STATUSES: dict[str, str] = {
    "active": "Active",
    "probation": "Probation", "on_probation": "Probation",
    "notice_period": "Notice Period", "on_notice": "Notice Period", "serving_notice": "Notice Period",
    "on_leave": "On Leave",
    "inactive": "Inactive", "terminated": "Terminated", "exited": "Exited", "resigned": "Resigned",
    "relieved": "Relieved", "absconded": "Absconded", "separated": "Separated", "deactivated": "Deactivated",
}
EMPLOYMENT_TYPES: dict[str, str] = {
    "full_time": "Full-time", "fulltime": "Full-time", "permanent": "Full-time",
    "part_time": "Part-time", "parttime": "Part-time",
    "contract": "Contract", "contractor": "Contract", "fixed_term": "Contract",
    "intern": "Intern", "internship": "Intern", "trainee": "Intern",
    "consultant": "Consultant",
}
WORK_MODES: dict[str, str] = {
    "office": "Office", "on_site": "Office", "onsite": "Office", "wfo": "Office",
    "remote": "Remote", "wfh": "Remote",
    "hybrid": "Hybrid",
}


def _closed_set(mapping: dict[str, str], label: str, allowed: str):
    def check(value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text")
        if not value.strip():
            return None
        out = mapping.get(_key(value))
        if out is None:
            raise ValueError(f"Invalid {label} '{value}'. Allowed: {allowed}")
        return out
    return check


normalize_status = _closed_set(
    EMPLOYEE_STATUSES, "status",
    "Active, Probation, Notice Period, On Leave, Inactive, Terminated, Exited, Resigned, Relieved, "
    "Absconded, Separated, Deactivated",
)
normalize_employment_type = _closed_set(
    EMPLOYMENT_TYPES, "employment type", "Full-time, Part-time, Contract, Intern, Consultant"
)
normalize_work_mode = _closed_set(WORK_MODES, "work mode", "Office, Remote, Hybrid")


def _required(fn):
    def check(value):
        out = fn(value)
        if out is None:
            raise ValueError("This field is required")
        return out
    return check


# ── formats (L-08) ─────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
ACCOUNT_RE = re.compile(r"^[0-9]{9,18}$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _format(regex: re.Pattern, label: str, *, upper: bool = False, strip_spaces: bool = False):
    def check(value):
        if value is None:
            return None
        value = value.strip()
        if strip_spaces:
            value = value.replace(" ", "")
        if upper:
            value = value.upper()
        if value == "":
            return None
        if not regex.match(value):
            raise ValueError(f"Invalid {label}")
        return value
    return check


validate_email = _format(EMAIL_RE, "email address (expected name@domain.tld)")
validate_ifsc = _format(IFSC_RE, "IFSC code (expected 11 characters, e.g. HDFC0001234)", upper=True)
validate_pan = _format(PAN_RE, "PAN (expected e.g. ABCDE1234F)", upper=True)
validate_account_no = _format(ACCOUNT_RE, "bank account number (expected 9-18 digits)", strip_spaces=True)
validate_gstin = _format(GSTIN_RE, "GSTIN (expected 15 characters, e.g. 27ABCDE1234F1Z5)", upper=True)


def _blank_placeholder(value):
    """The app shows '—' for absent values and some forms echo it back."""
    if isinstance(value, str) and value.strip() in ("", "—", "-"):
        return None
    return value


# ── dates (M-02) ───────────────────────────────────────────────────────────
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y")


def parse_date_text(text: str) -> datetime.date | None:
    """Same formats crud.parse_lenient_date accepts (plus an ISO datetime),
    None when nothing matches."""
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def normalize_date_text(value):
    """Non-empty text must be a real date (422 otherwise -- never silently
    stored as None). Returns ISO 'YYYY-MM-DD'; None / '' pass through
    unchanged (the routes treat '' as "clear")."""
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError("Date must be text in YYYY-MM-DD format")
    if not value.strip():
        return ""
    parsed = parse_date_text(value)
    if parsed is None:
        raise ValueError(f"'{value}' is not a valid date (use YYYY-MM-DD)")
    if not (1900 <= parsed.year <= 2200):
        raise ValueError(f"'{value}' is outside the supported date range")
    return parsed.isoformat()


def as_date(value: str | None) -> datetime.date | None:
    return parse_date_text(value) if value else None


def joining_date_error(doj: datetime.date | None, today: datetime.date | None = None) -> str | None:
    if doj is None:
        return None
    today = today or datetime.date.today()
    if doj < EARLIEST_JOINING:
        return f"Date of joining can't be before {EARLIEST_JOINING.isoformat()}."
    if doj > today + datetime.timedelta(days=MAX_JOINING_DAYS_AHEAD):
        return f"Date of joining can't be more than {MAX_JOINING_DAYS_AHEAD} days in the future."
    return None


def _years_between(earlier: datetime.date, later: datetime.date) -> int:
    return later.year - earlier.year - ((later.month, later.day) < (earlier.month, earlier.day))


def dob_error(dob: datetime.date | None, doj: datetime.date | None,
              today: datetime.date | None = None) -> str | None:
    """DOB must be in the past, before joining, and the person at least
    MIN_AGE_YEARS old on the joining date (MAX_AGE_YEARS sanity cap)."""
    if dob is None:
        return None
    today = today or datetime.date.today()
    if dob >= today:
        return "Date of birth must be in the past."
    if _years_between(dob, today) > MAX_AGE_YEARS:
        return f"Date of birth gives an age over {MAX_AGE_YEARS}."
    if doj is not None:
        if dob >= doj:
            return "Date of birth must be before the date of joining."
        if _years_between(dob, doj) < MIN_AGE_YEARS:
            return f"Employee must be at least {MIN_AGE_YEARS} years old on the date of joining."
    return None


# ── Annotated types for schemas.py ─────────────────────────────────────────
StatusIn = Annotated[str, AfterValidator(_required(normalize_status))]
OptStatusIn = Annotated[str | None, AfterValidator(normalize_status)]
EmploymentTypeIn = Annotated[str, AfterValidator(_required(normalize_employment_type))]
OptEmploymentTypeIn = Annotated[str | None, AfterValidator(normalize_employment_type)]
OptWorkModeIn = Annotated[str | None, AfterValidator(normalize_work_mode)]
EmailIn = Annotated[str, AfterValidator(_required(validate_email))]
OptEmailIn = Annotated[str | None, BeforeValidator(_blank_placeholder), AfterValidator(validate_email)]
OptIfscIn = Annotated[str | None, BeforeValidator(_blank_placeholder), AfterValidator(validate_ifsc)]
OptPanIn = Annotated[str | None, BeforeValidator(_blank_placeholder), AfterValidator(validate_pan)]
OptAccountNoIn = Annotated[str | None, BeforeValidator(_blank_placeholder), AfterValidator(validate_account_no)]
OptGstinIn = Annotated[str | None, BeforeValidator(_blank_placeholder), AfterValidator(validate_gstin)]
OptDateText = Annotated[str | None, BeforeValidator(normalize_date_text)]


# ── DB reference checks (H-21) ─────────────────────────────────────────────

def manager_error(db, company_id: uuid.UUID, manager_id: uuid.UUID | None, label: str,
                  *, employee_id: uuid.UUID | None = None) -> str | None:
    """A manager / leader reference must be an ACTIVE employee of the same
    company (never another tenant's id, a random uuid, or an exited
    employee), and never the employee themself."""
    if manager_id is None:
        return None
    from . import access_lifecycle, models

    if employee_id is not None and manager_id == employee_id:
        return f"An employee cannot be their own {label}."
    manager = db.get(models.Employee, manager_id)
    if manager is None or manager.company_id != company_id:
        return f"{label} does not refer to an employee in your company."
    if access_lifecycle.employee_access_blocked(manager):
        name = f"{manager.first_name} {manager.last_name or ''}".strip()
        return f"{label} must be an active employee ({name} is {manager.status or 'inactive'})."
    return None


def require_manager(db, company_id, manager_id, label, *, employee_id=None) -> None:
    error = manager_error(db, company_id, manager_id, label, employee_id=employee_id)
    if error:
        raise EmployeeInputError(error)


def band_error(db, company_id: uuid.UUID, band: int | None) -> str | None:
    """A band number must be one of the company's configured bands (when
    it has any)."""
    if band is None:
        return None
    from sqlalchemy import select

    from . import models

    numbers = set(db.scalars(select(models.Band.band_number).where(models.Band.company_id == company_id)))
    if numbers and band not in numbers:
        return f"Band {band} doesn't exist. Choose one of: {', '.join(str(n) for n in sorted(numbers))}."
    return None
