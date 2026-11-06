"""Administration > Form Configuration: configurable dropdown options and
admin-defined custom fields.

Tables: backend/db/add_form_configuration.sql (tenant schema). Everything is
scoped by company_id. A schema that has not run the migration yet falls back
to the built-in option lists (read-only) and ignores custom fields, so an
un-migrated database never turns into a 500.

Modules use this in three ways:

  * validate a dropdown value on save:
        form_config.require_option(db, company_id, "travel.type", payload.travel_type)
  * list the options a form offers (the API also serves them to the UI):
        form_config.option_values(db, company_id, "travel.type")
  * persist / read custom field values next to the record they belong to:
        cleaned = form_config.validate_custom_values(db, company_id, "asset", payload.custom_fields)
        form_config.save_custom_values(db, company_id, "asset", asset.id, cleaned)
        form_config.load_custom_values(db, company_id, "asset", [a.id for a in assets])

A built-in option (is_system) can be renamed, hidden and re-ordered but never
hard-deleted -- records keep the stored value, and a hidden option simply
stops being offered. Options the admin added can be deleted outright.
"""

from __future__ import annotations

import datetime
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from . import models


class FieldDef(NamedTuple):
    key: str
    form: str
    label: str
    # (stored value, label shown) -- the seed for every company
    defaults: tuple[tuple[str, str], ...]
    # False: the module's logic depends on these exact values, so admins may
    # rename / hide / re-order them but not add new ones.
    allow_custom: bool = True
    # How a NEW option's stored value is derived from its label: 'label'
    # stores the label itself, 'key' stores a snake_case slug.
    value_style: str = "label"


class FormDef(NamedTuple):
    key: str
    label: str
    module: str


def _same(*labels: str) -> tuple[tuple[str, str], ...]:
    return tuple((x, x) for x in labels)


FORMS: dict[str, FormDef] = {f.key: f for f in (
    FormDef("work_entry", "Work Entry", "Work & Timesheet"),
    FormDef("task", "Task", "Work & Timesheet / Projects"),
    FormDef("employee", "Employee", "People"),
    FormDef("salary_component", "Salary Component", "Payroll"),
    FormDef("travel_request", "Travel Request", "Travel & Expenses"),
    FormDef("expense_claim", "Expense Claim", "Travel & Expenses"),
    FormDef("goal", "Goal", "Performance"),
    FormDef("review_cycle", "Review Cycle", "Performance"),
    FormDef("candidate", "Candidate", "Recruitment"),
    FormDef("course", "Course", "Learning"),
    FormDef("learning_document", "Learning Document", "Learning"),
    FormDef("employee_document", "Employee Document", "People"),
    FormDef("asset", "Asset", "Assets"),
    FormDef("final_settlement", "Final Settlement", "Offboarding"),
)}

FIELDS: dict[str, FieldDef] = {f.key: f for f in (
    FieldDef("work_entry.category", "work_entry", "Category",
             _same("Development", "Design", "QA", "Meeting", "Documentation")),
    FieldDef("task.column", "task", "Column",
             _same("Backlog", "In Progress", "In Review", "Done"), allow_custom=False),
    FieldDef("employee.employment_type", "employee", "Employment Type",
             # Consultant is no longer offered (db/remove_consultant_employment_type.sql
             # hides it for existing companies); a record already stored as
             # Consultant still shows and saves unchanged (require_option current=).
             # allow_custom=False: Contract / Freelancer drive contract dates,
             # rates and payroll logic by their exact stored value.
             _same("Full-time", "Part-time", "Contract", "Intern", "Freelancer"),
             allow_custom=False),
    # work_mode: allow_custom stays True -- every reader (People tables, profile,
    # reports, Add Employee) shows the stored text; no logic keys off the value.
    # Stored in a String(20) column, so the employee routers reject longer ones.
    FieldDef("employee.work_mode", "employee", "Work Mode", _same("Office", "Remote", "Hybrid")),
    # Personal-info lists: nothing keys off the stored text (reports only group by
    # it), so admins and users may add their own. Columns are widened to 120 by
    # db/add_form_config_employee_personal.sql.
    FieldDef("employee.gender", "employee", "Gender", _same("Male", "Female", "Other")),
    FieldDef("employee.marital_status", "employee", "Marital Status",
             _same("Single", "Married", "Divorced", "Widowed")),
    FieldDef("employee.nationality", "employee", "Nationality",
             _same("Indian", "American", "British", "Canadian", "Other")),
    # Previous-employer industry (hcm prior experience, String(150)).
    FieldDef("employee.experience_domain", "employee", "Domain / Industry",
             _same("Information Technology", "Software / SaaS", "ERP / Consulting", "Banking & Finance",
                   "Insurance", "Healthcare & Pharma", "Manufacturing", "Retail & E-commerce",
                   "Telecom", "Education", "Logistics & Transportation", "Construction & Real Estate",
                   "Energy & Utilities", "Agriculture", "Media & Entertainment",
                   "Government / Public Sector", "Hospitality & Travel", "Other")),
    FieldDef("salary_component.type", "salary_component", "Type",
             (("earning", "Earning"), ("deduction", "Deduction"),
              ("benefit", "Benefit (employer cost)"), ("tax", "Tax")),
             allow_custom=False, value_style="key"),
    FieldDef("salary_component.calculation", "salary_component", "Calculation",
             (("flat", "Flat amount"), ("percent", "Percent of CTC/Basic"),
              ("stat_pf", "Statutory PF (auto-computed)"), ("stat_esi", "Statutory ESI (auto-computed)"),
              ("var_annual", "Variable pay (annual)")),
             allow_custom=False, value_style="key"),
    FieldDef("travel.type", "travel_request", "Travel Type", _same("Domestic", "International")),
    FieldDef("travel.funding", "travel_request", "Travel Funding",
             _same("Self", "Company", "Sponsored by Third Party", "Partially Sponsored")),
    FieldDef("travel.id_document_type", "travel_request", "Identification Document Type",
             _same("Passport", "Aadhaar Card", "PAN Card", "Driving License", "Voter ID")),
    FieldDef("travel.mode", "travel_request", "Mode of Travel",
             _same("Flight", "Train", "Bus", "Car", "Cab", "Other")),
    FieldDef("goal.level", "goal", "Level", _same("Company", "Department", "Individual"), allow_custom=False),
    FieldDef("review_cycle.status", "review_cycle", "Status",
             (("scheduled", "Scheduled"), ("in_progress", "In Progress"), ("completed", "Completed")),
             allow_custom=False, value_style="key"),
    FieldDef("candidate.source", "candidate", "Source",
             _same("Career Site", "Employee Referral", "Recruiter", "Job Board", "Agency", "Direct Application",
                   "Walk-in", "Import", "Other")),
    FieldDef("course.type", "course", "Type", _same("Internal", "External", "Mandatory")),
    # Learning Documents: stored as text (hcm_learning_documents.category, widened
    # to 120 by add_form_config_work_documents.sql); only a filter / label.
    FieldDef("learning_document.category", "learning_document", "Category",
             _same("Learning", "Training", "Development", "Certification Prep", "Policy & Process", "Other")),
    # People > Employee Documents > Upload Document (hcm_document_records.document_type,
    # widened to 120 by add_form_config_work_documents.sql). The section types
    # the Employee profile sends ("Education Documents" / "Professional
    # Documentation" / "Other Documents") are system values and bypass this list.
    FieldDef("employee_document.type", "employee_document", "Document Type",
             _same("Offer Letter", "Appointment Letter", "PAN Card", "Aadhaar Card", "Educational Certificates",
                   "Latest Payslip", "Experience Letter", "Relieving Letter", "Experience & Relieving Letter")),
    FieldDef("asset.type", "asset", "Type",
             # + the types the seeded inventory uses (Mobile Device, SIM Card, ID Card)
             _same("Laptop", "Desktop", "Mobile", "Mobile Device", "Monitor", "Tablet", "Accessory",
                   "SIM Card", "ID Card", "Software License")),
    FieldDef("asset.status", "asset", "Status",
             (("available", "Available"), ("assigned", "Assigned"), ("in_repair", "In Repair")),
             allow_custom=False, value_style="key"),
    # Stored as text on the assignment (hcm_asset_assignments.return_condition,
    # widened to 120 by add_form_config_assets_fnf.sql); nothing keys off it.
    FieldDef("asset.return_condition", "asset", "Return Condition",
             _same("Good", "Fair", "Damaged", "Lost")),
    # Stored as text on the settlement (hcm_final_settlements.payment_mode,
    # widened to 120 by add_form_config_assets_fnf.sql); display-only.
    FieldDef("fnf.payment_mode", "final_settlement", "Payment Mode",
             _same("Bank transfer", "Cheque", "UPI", "Cash", "Adjusted against dues")),
)}

# Travel / expense claim types are managed on their own screen
# (hcm_expense_claim_types, Travel & Expenses > Claim Types); the registry
# lists them so the admin screen can point there.
EXTERNALLY_MANAGED = {
    "travel.expense_type": "Travel & Expenses > Expense Categories",
    "expense_claim.claim_type": "Travel & Expenses > Expense Categories",
}

CUSTOM_FIELD_TYPES = ("text", "textarea", "number", "date", "dropdown", "checkbox")
_MAX_TEXT = {"text": 500, "textarea": 5000}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")[:60]


# ── storage access (optional tables, per-request cache) ───────────────────

_CACHE = "form_config_options"


def _tables_ready(db: Session) -> bool:
    ready = db.info.get("form_config_ready")
    if ready is None:
        ready = bool(db.execute(
            text("select to_regclass('hcm_form_field_options') is not null")
        ).scalar())
        db.info["form_config_ready"] = ready
    return ready


def invalidate(db: Session) -> None:
    db.info.pop(_CACHE, None)


def _rows(db: Session, company_id: uuid.UUID) -> dict[str, list[models.FormFieldOption]]:
    """Every option row of the company grouped by field key, seeding the
    built-in defaults for a field that has no rows at all yet."""
    cache = db.info.setdefault(_CACHE, {})
    if company_id in cache:
        return cache[company_id]
    grouped: dict[str, list[models.FormFieldOption]] = {}
    if _tables_ready(db):
        existing = db.scalars(select(models.FormFieldOption).where(
            models.FormFieldOption.company_id == company_id)).all()
        for row in existing:
            grouped.setdefault(row.field_key, []).append(row)
        seeded = False
        for key, fdef in FIELDS.items():
            if key in grouped:
                continue
            for i, (value, label) in enumerate(fdef.defaults):
                row = models.FormFieldOption(
                    id=uuid.uuid4(), company_id=company_id, field_key=key, value=value, label=label,
                    sort_order=i, is_active=True, is_system=True)
                db.add(row)
                grouped.setdefault(key, []).append(row)
                seeded = True
        if seeded:
            db.flush()
    for rows in grouped.values():
        rows.sort(key=lambda r: (r.sort_order, r.label.lower()))
    cache[company_id] = grouped
    return grouped


def options(db: Session, company_id: uuid.UUID, field_key: str, *,
            include_inactive: bool = False) -> list[tuple[str, str]]:
    """[(value, label)] of a field, in the admin's order. Falls back to the
    built-in defaults when the tables don't exist yet."""
    if field_key not in FIELDS and not field_key.startswith("custom."):
        raise KeyError(f"unknown form field {field_key!r}")
    if not _tables_ready(db):
        return list(FIELDS[field_key].defaults) if field_key in FIELDS else []
    rows = _rows(db, company_id).get(field_key, [])
    return [(r.value, r.label) for r in rows if include_inactive or r.is_active]


def option_values(db: Session, company_id: uuid.UUID, field_key: str) -> list[str]:
    return [v for v, _ in options(db, company_id, field_key)]


def option_label(db: Session, company_id: uuid.UUID, field_key: str, value: str | None) -> str | None:
    """Display label of a stored value (the raw value if it's not listed any
    more, e.g. an option that was since removed)."""
    if value is None:
        return None
    for v, label in options(db, company_id, field_key, include_inactive=True):
        if v.lower() == value.lower():
            return label
    return value


def require_option(db: Session, company_id: uuid.UUID, field_key: str, value: str | None, *,
                   current: str | None = None, required: bool = False) -> str | None:
    """Validates [value] against the field's active options and returns the
    canonical stored value. Blank -> None (or 422 when [required]). A value
    equal to [current] (the record's existing, possibly since-hidden value)
    is accepted unchanged, so editing other fields of an old record works."""
    label = FIELDS[field_key].label if field_key in FIELDS else field_key
    if value is None or not str(value).strip():
        if required:
            raise HTTPException(status_code=422, detail=f"{label} is required.")
        return None
    text = str(value).strip()
    for v, _ in options(db, company_id, field_key):
        if v.lower() == text.lower():
            return v
    if current is not None and text.lower() == current.strip().lower():
        return current
    raise HTTPException(
        status_code=422,
        detail=f"'{text}' is not an available {label}. Pick one from the list, "
               "or ask an administrator to add it in Form Configuration.",
    )


# ── custom fields ─────────────────────────────────────────────────────────

def custom_field_defs(db: Session, company_id: uuid.UUID, form_key: str, *,
                      include_inactive: bool = False) -> list[models.CustomFieldDef]:
    if not _tables_ready(db):
        return []
    q = select(models.CustomFieldDef).where(
        models.CustomFieldDef.company_id == company_id, models.CustomFieldDef.form_key == form_key)
    if not include_inactive:
        q = q.where(models.CustomFieldDef.is_active.is_(True))
    return list(db.scalars(q.order_by(models.CustomFieldDef.sort_order, models.CustomFieldDef.label)))


def _clean_one(db: Session, company_id: uuid.UUID, d: models.CustomFieldDef, raw) -> str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if d.is_required and d.field_type != "checkbox":
            raise HTTPException(status_code=422, detail=f"{d.label} is required.")
        return None
    t = d.field_type
    if t == "checkbox":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        s = str(raw).strip().lower()
        if s not in ("true", "false"):
            raise HTTPException(status_code=422, detail=f"{d.label} must be Yes or No.")
        return s
    s = str(raw).strip()
    if t in ("text", "textarea"):
        if len(s) > _MAX_TEXT[t]:
            raise HTTPException(status_code=422, detail=f"{d.label} must be {_MAX_TEXT[t]} characters or fewer.")
        return s
    if t == "number":
        try:
            n = Decimal(s)
        except InvalidOperation:
            raise HTTPException(status_code=422, detail=f"{d.label} must be a number.") from None
        if not n.is_finite() or abs(n) >= Decimal("1e15"):
            raise HTTPException(status_code=422, detail=f"{d.label} must be a valid number.")
        return format(n.normalize(), "f")
    if t == "date":
        try:
            return datetime.date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{d.label} must be a date (YYYY-MM-DD).") from None
    if t == "dropdown":
        for v, _ in options(db, company_id, f"custom.{d.id}"):
            if v.lower() == s.lower():
                return v
        raise HTTPException(status_code=422, detail=f"'{s}' is not an available choice for {d.label}.")
    raise HTTPException(status_code=422, detail=f"{d.label} has an unsupported type.")


def validate_custom_values(db: Session, company_id: uuid.UUID, form_key: str, values: dict | None, *,
                           partial: bool = False) -> dict[str, str | None]:
    """Cleans the {field_key: value} map a form submitted. Unknown keys are a
    422 (a stale form after an admin removed the field). On create
    ([partial] False) every required field must be present; on edit only the
    keys sent are checked."""
    values = values or {}
    defs = {d.field_key: d for d in custom_field_defs(db, company_id, form_key)}
    unknown = [k for k in values if k not in defs]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown custom field(s): {', '.join(sorted(unknown))}. Reload the form and try again.")
    out: dict[str, str | None] = {}
    for key, d in defs.items():
        if key in values:
            out[key] = _clean_one(db, company_id, d, values[key])
        elif d.is_required and not partial:
            raise HTTPException(status_code=422, detail=f"{d.label} is required.")
    return out


def save_custom_values(db: Session, company_id: uuid.UUID, form_key: str, entity_id: uuid.UUID,
                       cleaned: dict[str, str | None]) -> None:
    """Upserts the cleaned values for one record (a None value clears it)."""
    if not cleaned or not _tables_ready(db):
        return
    defs = {d.field_key: d for d in custom_field_defs(db, company_id, form_key, include_inactive=True)}
    existing = {
        v.custom_field_id: v for v in db.scalars(select(models.CustomFieldValue).where(
            models.CustomFieldValue.company_id == company_id,
            models.CustomFieldValue.entity_id == entity_id,
            models.CustomFieldValue.custom_field_id.in_([d.id for d in defs.values()] or [uuid.uuid4()])))
    }
    for key, value in cleaned.items():
        d = defs.get(key)
        if d is None:
            continue
        row = existing.get(d.id)
        if value is None:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(models.CustomFieldValue(
                id=uuid.uuid4(), company_id=company_id, custom_field_id=d.id, entity_id=entity_id, value=value))
        else:
            row.value = value
            row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()


def load_custom_values(db: Session, company_id: uuid.UUID, form_key: str,
                       entity_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, str]]:
    """{entity_id: {field_key: value}} for the records, active fields only."""
    if not entity_ids or not _tables_ready(db):
        return {}
    defs = {d.id: d for d in custom_field_defs(db, company_id, form_key)}
    if not defs:
        return {}
    out: dict[uuid.UUID, dict[str, str]] = {}
    rows = db.scalars(select(models.CustomFieldValue).where(
        models.CustomFieldValue.company_id == company_id,
        models.CustomFieldValue.custom_field_id.in_(list(defs)),
        models.CustomFieldValue.entity_id.in_(entity_ids)))
    for r in rows:
        if r.value is not None:
            out.setdefault(r.entity_id, {})[defs[r.custom_field_id].field_key] = r.value
    return out


def delete_custom_values_for(db: Session, company_id: uuid.UUID, entity_id: uuid.UUID) -> None:
    """Call when a record is hard-deleted."""
    if _tables_ready(db):
        db.execute(delete(models.CustomFieldValue).where(
            models.CustomFieldValue.company_id == company_id, models.CustomFieldValue.entity_id == entity_id))


# ── "Add custom..." from a form (any signed-in user) ──────────────────────

QUICK_ADD_PER_USER_PER_HOUR = 20


def _settings_ready(db: Session) -> bool:
    ready = db.info.get("form_field_settings_ready")
    if ready is None:
        ready = bool(db.execute(text("select to_regclass('hcm_form_field_settings') is not null")).scalar())
        db.info["form_field_settings_ready"] = ready
    return ready


def user_add_settings(db: Session, company_id: uuid.UUID) -> dict[str, bool]:
    """{field_key: allow_user_add} for the keys an admin has switched
    (absent = the default, allowed)."""
    if not _settings_ready(db):
        return {}
    return {r.field_key: r.allow_user_add for r in db.scalars(select(models.FormFieldSetting).where(
        models.FormFieldSetting.company_id == company_id))}


def user_may_add(db: Session, company_id: uuid.UUID, field_key: str, *,
                 settings_map: dict[str, bool] | None = None) -> bool:
    """May an ordinary user add an option to [field_key] from a form? Needs a
    list that accepts new options (not one built-in logic depends on), the
    tables, and the admin not having switched it off."""
    if not _tables_ready(db):
        return False
    if field_key in FIELDS:
        if not FIELDS[field_key].allow_custom:
            return False
    elif field_key.startswith("custom."):
        try:
            fid = uuid.UUID(field_key.split(".", 1)[1])
        except ValueError:
            return False
        d = db.get(models.CustomFieldDef, fid)
        if d is None or d.company_id != company_id or d.field_type != "dropdown" or not d.is_active:
            return False
    else:
        return False
    allowed = (settings_map if settings_map is not None else user_add_settings(db, company_id)).get(field_key, True)
    return bool(allowed)


def quick_add_keys(db: Session, company_id: uuid.UUID) -> list[str]:
    """Every field key a user may add to right now (built-in + custom dropdowns)."""
    if not _tables_ready(db):
        return []
    sm = user_add_settings(db, company_id)
    keys = [k for k, f in FIELDS.items() if f.allow_custom and sm.get(k, True)]
    for d in db.scalars(select(models.CustomFieldDef).where(
            models.CustomFieldDef.company_id == company_id, models.CustomFieldDef.field_type == "dropdown",
            models.CustomFieldDef.is_active.is_(True))):
        if sm.get(f"custom.{d.id}", True):
            keys.append(f"custom.{d.id}")
    return keys


def clean_option_label(raw: str | None) -> str:
    """Trim, collapse inner whitespace, refuse control characters / empty."""
    text_in = raw or ""
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text_in):
        raise HTTPException(status_code=422, detail="The name can't contain control characters.")
    label = re.sub(r"\s+", " ", text_in.strip())
    if not label or not any(ch.isalnum() for ch in label):
        raise HTTPException(status_code=422, detail="Enter a name with letters or numbers.")
    if len(label) > 120:
        raise HTTPException(status_code=422, detail="The name must be 120 characters or fewer.")
    return label


def quick_add_option(db: Session, company_id: uuid.UUID, user_id: uuid.UUID | None, field_key: str,
                     label: str) -> tuple[models.FormFieldOption, bool]:
    """Adds [label] to the list (or returns the existing active option).
    Returns (row, created). Rules: the list must allow user additions; a
    name already in the list is reused; a name an admin hid is refused (it
    was removed on purpose); at most QUICK_ADD_PER_USER_PER_HOUR per user."""
    if not _tables_ready(db):
        raise HTTPException(status_code=409, detail="Form Configuration isn't set up for this organization yet.")
    if not user_may_add(db, company_id, field_key):
        raise HTTPException(status_code=403, detail="Adding your own option isn't allowed for this list.")
    label = clean_option_label(label)
    rows = _rows(db, company_id).get(field_key, [])
    for r in rows:
        if r.label.lower() == label.lower() or r.value.lower() == label.lower():
            if r.is_active:
                return r, False
            raise HTTPException(
                status_code=409,
                detail=f"'{r.label}' is hidden for this list. Ask an administrator to show it again in Form Configuration.")
    if len(rows) >= 200:
        raise HTTPException(status_code=409, detail="This list is full (200 options). Ask an administrator to tidy it up.")
    if user_id is not None:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        recent = sum(1 for rs in _rows(db, company_id).values() for r in rs
                     if r.created_by == user_id and not r.is_system and r.created_at and r.created_at >= since)
        if recent >= QUICK_ADD_PER_USER_PER_HOUR:
            raise HTTPException(status_code=429, detail="You've added a lot of options in the last hour. Please try again later.")
    style = FIELDS[field_key].value_style if field_key in FIELDS else "label"
    value = (slugify(label) if style == "key" else label)[:120]
    row = models.FormFieldOption(
        id=uuid.uuid4(), company_id=company_id, field_key=field_key, value=value, label=label,
        sort_order=(max((r.sort_order for r in rows), default=-1) + 1), is_active=True, is_system=False,
        created_by=user_id)
    db.add(row)
    db.flush()
    db.refresh(row)  # server-side created_at
    rows.append(row)
    return row, True
