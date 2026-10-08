"""WorkTrack (WorkTrackIQ) API -- the WorkTrack app runs on the HRMS backend.

Same logins and the same tenant data as HRMS: WorkTrack's employees are HRMS
employees, its projects are HRMS projects (+ allocations), its work entries
are HRMS work entries (pm_time_entries, so they also show under HRMS Work &
Timesheet) and its check-ins drive HRMS attendance (clock in / out).

Only what HRMS has no place for lives in WorkTrack's own tables (pm_wt_*,
created in every tenant schema and in _template, so new tenants get them):
entry location / remarks / Draft-Submitted-Approved status, project contact
details, the IN/OUT check-in log, the activity feed, freelancer invoices and
per-user profile extras. Nothing existing in HRMS is altered or deleted --
"deleting" an employee or project from WorkTrack only marks it inactive.

The app keeps its table-style data access: POST /api/worktrack/query takes
{table, op, filters, ...} for a fixed set of WorkTrack tables and maps each
to the HRMS data above, scoped to the caller's company and permissions.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import string
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import access_lifecycle, auth_state, crud, models, role_tiers, schemas, security, work_rules
from .. import employee_validation as ev
from ..database import get_db
from ..deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/worktrack", tags=["worktrack"])

# HRMS roles that get WorkTrack's Admin view.
ADMIN_ROLES = {"Organization Owner / CEO", "IT / System Admin", "HR / Recruitment Staff"}
FREELANCE_TYPES = {"freelancer", "freelance", "contract", "contractor", "consultant"}
WT_ADMIN_LABEL = "System Administrator"

# ── WorkTrack tables (additive; CREATE ... IF NOT EXISTS) ──────────────────

WT_DDL = [
    """CREATE TABLE IF NOT EXISTS pm_wt_entry_meta (
        entry_id uuid PRIMARY KEY,
        location varchar(60),
        remarks text,
        wt_status varchar(20),
        submitted_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS pm_wt_project_meta (
        project_id uuid PRIMARY KEY,
        company_name varchar(200),
        contact varchar(100),
        email varchar(200),
        attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
        progress numeric(4,2),
        wt_status varchar(30),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS pm_wt_checkin_logs (
        id uuid PRIMARY KEY,
        company_id uuid NOT NULL,
        employee_id uuid NOT NULL,
        employee_code varchar(40),
        employee_name varchar(200),
        employee_email varchar(200),
        log_type varchar(5) NOT NULL,
        log_time varchar(20),
        log_date varchar(12),
        location_device_id varchar(200),
        mode_of_work varchar(30),
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS ix_pm_wt_checkin_logs_emp ON pm_wt_checkin_logs (employee_id, created_at)",
    """CREATE TABLE IF NOT EXISTS pm_wt_activities (
        id uuid PRIMARY KEY,
        company_id uuid NOT NULL,
        employee_code varchar(40),
        employee_name varchar(200),
        employee_email varchar(200),
        action varchar(80),
        project varchar(200),
        activity_time varchar(20),
        status varchar(30),
        avatar_url text,
        details jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS ix_pm_wt_activities_created ON pm_wt_activities (company_id, created_at)",
    """CREATE TABLE IF NOT EXISTS pm_wt_freelancer_invoices (
        id uuid PRIMARY KEY,
        company_id uuid NOT NULL,
        employee_id uuid NOT NULL,
        employee_name varchar(200),
        month_year varchar(30),
        total_hours numeric(10,2),
        hourly_rate numeric(12,2),
        currency varchar(5),
        total_amount numeric(14,2),
        projects jsonb NOT NULL DEFAULT '[]'::jsonb,
        status varchar(20),
        submitted_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS pm_wt_user_meta (
        user_id uuid PRIMARY KEY,
        data jsonb NOT NULL DEFAULT '{}'::jsonb,
        updated_at timestamptz NOT NULL DEFAULT now())""",
]

_ensured: set[str] = set()


def ensure_tables(db: Session) -> None:
    """Creates WorkTrack's own tables in the current tenant schema once per
    process (normally already there from the migration)."""
    schema = db.execute(text("select current_schema()")).scalar() or ""
    if schema in _ensured:
        return
    for ddl in WT_DDL:
        db.execute(text(ddl))
    db.commit()
    _ensured.add(schema)


# ── request model ──────────────────────────────────────────────────────────

class QueryIn(BaseModel):
    table: str
    op: str = Field(pattern="^(select|insert|update|upsert|delete)$")
    columns: str = "*"
    filters: list[list[Any]] = []        # [[col, op, value], ...]  (AND)
    or_groups: list[list[list[Any]]] = []  # [[[col, op, value], ...], ...] each group OR-ed
    order: list[list[Any]] = []          # [[col, ascending], ...]
    limit: int | None = None
    offset: int | None = None
    values: Any = None                   # dict | list[dict]
    on_conflict: str | None = None


class PasswordIn(BaseModel):
    new_password: schemas.NewPassword  # same password policy as HRMS


class ResetPasswordIn(BaseModel):
    target_email: str
    new_password: schemas.NewPassword  # same password policy as HRMS


class MetadataIn(BaseModel):
    data: dict[str, Any]


class WtError(Exception):
    def __init__(self, message: str, code: str = "P0001", status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def _fail(exc: WtError):
    raise HTTPException(status_code=exc.status, detail={"message": exc.message, "code": exc.code})


# ── context ────────────────────────────────────────────────────────────────

class Ctx:
    def __init__(self, db: Session, user: models.User):
        self.db = db
        self.user = user
        self.company_id = user.company_id
        roles = {r.name for r in crud.get_user_roles(db, user.id)}
        self.is_admin = bool(roles & ADMIN_ROLES)
        self.employee_id = user.employee_id
        self._emp_cache: dict[uuid.UUID, models.Employee] | None = None

    def employees(self) -> dict[uuid.UUID, models.Employee]:
        if self._emp_cache is None:
            rows = self.db.scalars(select(models.Employee).where(models.Employee.company_id == self.company_id)).all()
            self._emp_cache = {e.id: e for e in rows}
        return self._emp_cache

    def require_admin(self):
        if not self.is_admin:
            raise WtError("Only an admin can do this.", "42501", 403)


def _uuid(v) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (ValueError, TypeError, AttributeError):
        return None


def _name(e: models.Employee) -> str:
    return f"{e.first_name} {e.last_name}".strip() if e.last_name else (e.first_name or "")


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return (v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)).isoformat()
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _num(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def _date(v) -> dt.date | None:
    if v in (None, ""):
        return None
    if isinstance(v, dt.date):
        return v
    s = str(v)[:10]
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        try:
            return dt.datetime.strptime(s, "%d-%m-%Y").date()
        except ValueError:
            return None


def _time(v) -> dt.time | None:
    if not v:
        return None
    try:
        return dt.time.fromisoformat(str(v)[:8])
    except ValueError:
        return None


# ── employees / freelancers ────────────────────────────────────────────────

def _user_roles_by_employee(ctx: Ctx) -> dict[uuid.UUID, set[str]]:
    rows = ctx.db.execute(
        select(models.User.employee_id, models.Role.name)
        .join(models.UserRole, models.UserRole.user_id == models.User.id)
        .join(models.Role, models.Role.id == models.UserRole.role_id)
        .where(models.User.company_id == ctx.company_id)
    ).all()
    out: dict[uuid.UUID, set[str]] = {}
    for emp_id, role in rows:
        if emp_id:
            out.setdefault(emp_id, set()).add(role)
    return out


def _login_emails(ctx: Ctx) -> dict[uuid.UUID, str]:
    return {e: m for e, m in ctx.db.execute(
        select(models.User.employee_id, models.User.email).where(models.User.company_id == ctx.company_id)) if e}


def _is_freelance(e: models.Employee) -> bool:
    return (e.employment_type or "").strip().lower().replace("-", " ").replace("_", " ") in FREELANCE_TYPES


def _employee_rows(ctx: Ctx, freelancers: bool) -> list[dict]:
    db = ctx.db
    emps = [e for e in ctx.employees().values() if _is_freelance(e) == freelancers]
    roles = _user_roles_by_employee(ctx)
    emails = _login_emails(ctx)
    depts = {d.id: d.name for d in db.scalars(select(models.Department).where(models.Department.company_id == ctx.company_id))}
    desigs = {d.id: d.name for d in db.scalars(select(models.Designation).where(models.Designation.company_id == ctx.company_id))}
    branches = {b.id: (b.city or b.name) for b in db.scalars(select(models.Branch).where(models.Branch.company_id == ctx.company_id))}
    out = []
    for e in emps:
        designation = desigs.get(e.designation_id) or ""
        if roles.get(e.id, set()) & ADMIN_ROLES:
            role = WT_ADMIN_LABEL
        elif freelancers:
            role = "Freelancer"
        else:
            role = designation or "Employee"
        # Same rule as HRMS login access (inactive / terminated / exited ...).
        active = not access_lifecycle.employee_access_blocked(e)
        row = {
            "id": str(e.id),
            "name": _name(e),
            "email": emails.get(e.id) or e.work_email,
            "role": role,
            "department": depts.get(e.department_id) or "General",
            "designation": designation,
            "location": branches.get(e.branch_id),
            "employment_type": e.employment_type,
            "joined_date": _iso(e.date_of_joining),
            "status": "Active" if active else "Inactive",
            "phone": getattr(e, "personal_phone", None),
            "avatar_url": e.photo_url,
            "created_at": _iso(getattr(e, "created_at", None)),
        }
        if freelancers:
            row["freelancer_id"] = e.employee_code
            row["expertise"] = row["department"]
        else:
            row["employee_id"] = e.employee_code
        out.append(row)
    return out


def _first_branch(ctx: Ctx) -> models.Branch:
    b = ctx.db.scalars(select(models.Branch).where(models.Branch.company_id == ctx.company_id)
                       .order_by(models.Branch.name)).first()
    if b is None:
        raise WtError("This company has no branch yet. Create one in HRMS Organization first.")
    return b


def _department(ctx: Ctx, name: str | None) -> models.Department:
    name = (name or "").strip() or "General"
    d = crud.get_department_by_name(ctx.db, ctx.company_id, name)
    if d is None:
        d = crud.create_department(ctx.db, ctx.company_id, schemas.DepartmentCreate(name=name))
    return d


def _temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Wt@" + "".join(secrets.choice(alphabet) for _ in range(9))


def _create_employee(ctx: Ctx, v: dict, freelancer: bool) -> dict:
    """WorkTrack Admin > Add Employee / Add Admin -> a real HRMS employee + login."""
    from . import employees as employees_router

    ctx.require_admin()
    name = (v.get("name") or "").strip()
    email = (v.get("email") or "").strip().lower()
    if not name:
        raise WtError("Name is required.")
    if not email or "@" not in email:
        raise WtError("An email address is required -- it is the employee's login for WorkTrack and HRMS.")
    parts = name.split(None, 1)
    is_admin_account = (v.get("role") or "") == WT_ADMIN_LABEL
    designation = "Freelancer" if freelancer else (
        "System Administrator" if is_admin_account else ((v.get("role") or "").strip() or "Employee"))
    password = v.get("_password") or _temp_password()
    dept = _department(ctx, v.get("expertise") if freelancer else v.get("department"))
    try:
        # Same validation as HRMS Add Employee (email, password policy, ...).
        payload = schemas.EmployeeCreate(
            employee_code=(v.get("freelancer_id") if freelancer else v.get("employee_id")) or None,
            first_name=parts[0], last_name=parts[1] if len(parts) > 1 else None,
            work_email=email, role_name="IT / System Admin" if is_admin_account else "Professional / IC Employee",
            password=password, date_of_joining=_date(v.get("joined_date")) or crud.company_today(ctx.db, ctx.company_id),
            employment_type="contract" if freelancer else "full_time", status="active",
            branch_name=_first_branch(ctx).name, department_name=dept.name, designation_name=designation,
            personal_phone=v.get("phone") or None,
            # The role is picked from WorkTrack's list or typed via "Add Custom"
            # -- a deliberate choice, like HRMS's "+ Custom Designation".
            create_designation=True,
        )
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", ()))
        message = str(first.get("msg", "Invalid employee details")).removeprefix("Value error, ")
        raise WtError(f"{message}" if field in ("", "password") else f"{field}: {message}") from exc
    try:
        out = employees_router.create_employee(payload, ctx.db, ctx.user)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        code = "23505" if exc.status_code == 409 and "code" in detail.lower() else "P0001"
        raise WtError(detail.replace("Employee code", "Employee ID"), code, exc.status_code) from exc
    ctx._emp_cache = None
    row = next(r for r in _employee_rows(ctx, freelancer) if r["id"] == str(out.id))
    if not v.get("_password"):
        row["_temp_password"] = password
    return row


def _update_employee(ctx: Ctx, e: models.Employee, v: dict, freelancer: bool) -> None:
    if not ctx.is_admin and e.id != ctx.employee_id:
        raise WtError("You can only update your own profile.", "42501", 403)
    if "name" in v and (v["name"] or "").strip():
        parts = v["name"].strip().split(None, 1)
        e.first_name, e.last_name = parts[0], (parts[1] if len(parts) > 1 else None)
    if "phone" in v:
        e.personal_phone = v["phone"] or None
    code = v.get("freelancer_id" if freelancer else "employee_id")
    if code and code != e.employee_code:
        clash = ctx.db.scalar(select(models.Employee.id).where(
            models.Employee.company_id == ctx.company_id, models.Employee.employee_code == code,
            models.Employee.id != e.id))
        if clash:
            raise WtError(f'duplicate key value violates unique constraint "employees_employee_id_key"', "23505", 409)
        e.employee_code = code
    dept_name = v.get("expertise") if freelancer else v.get("department")
    if dept_name:
        e.department_id = _department(ctx, dept_name).id
    title = v.get("designation") or (None if freelancer else v.get("role"))
    if title and title not in (WT_ADMIN_LABEL, "Employee", "Freelancer"):
        e.designation_id = crud.get_or_create_designation_by_name(ctx.db, ctx.company_id, title).id
    if v.get("employment_type"):
        try:
            e.employment_type = ev.normalize_employment_type(v["employment_type"]) or e.employment_type
        except ValueError as exc:
            raise WtError(str(exc)) from exc
    if v.get("joined_date") and _date(v["joined_date"]):
        e.date_of_joining = _date(v["joined_date"])
    if "status" in v and ctx.is_admin and v["status"]:
        _set_active(ctx, e, str(v["status"]).lower() != "inactive")
    url = v.get("avatar_url")
    if isinstance(url, str) and url.startswith("/media/"):
        e.photo_url = url.split("?", 1)[0]


def _set_active(ctx: Ctx, e: models.Employee, active: bool) -> None:
    """Active / Inactive exactly as HRMS People does it: the employee's
    status, then access_lifecycle brings the login in line (an inactive
    login is refused and every open session ends)."""
    if active == (not access_lifecycle.employee_access_blocked(e)):
        return
    e.is_active = active
    e.status = "Active" if active else "Inactive"
    ctx.db.flush()
    change = access_lifecycle.sync_login_access(ctx.db, e)
    if change is not None:
        crud.create_audit_log(ctx.db, ctx.company_id, ctx.user.id,
                              "access_revoked" if change == "deactivated" else "access_restored",
                              "employee", e.id, changes={"status": e.status})


# ── projects ───────────────────────────────────────────────────────────────

_TO_HRMS_STATUS = {"in progress": "active", "pending": "planning", "completed": "completed", "on hold": "on_hold"}
_FROM_HRMS_STATUS = {"active": "In Progress", "planning": "Pending", "on_hold": "Pending", "completed": "Completed",
                     "cancelled": "Completed"}


def _meta(ctx: Ctx, table: str, key: str, ids: list) -> dict:
    if not ids:
        return {}
    rows = ctx.db.execute(text(f"select * from {table} where {key} = any(cast(:ids as uuid[]))"), {"ids": [str(i) for i in ids]}).mappings()
    return {str(r[key]): dict(r) for r in rows}


def _project_rows(ctx: Ctx) -> list[dict]:
    db = ctx.db
    projects = db.scalars(select(models.Project).where(models.Project.company_id == ctx.company_id,
                                                       models.Project.is_active.is_(True))).all()
    ids = [p.id for p in projects]
    allocs: dict[uuid.UUID, list[str]] = {}
    if ids:
        for a in db.scalars(select(models.ProjectAllocation).where(models.ProjectAllocation.project_id.in_(ids),
                                                                     models.ProjectAllocation.is_active.is_(True))):
            allocs.setdefault(a.project_id, []).append(str(a.employee_id))
    customers = {c.id: c.name for c in db.scalars(select(models.Customer).where(models.Customer.company_id == ctx.company_id))}
    meta = _meta(ctx, "pm_wt_project_meta", "project_id", ids)
    out = []
    for p in projects:
        m = meta.get(str(p.id), {})
        status = m.get("wt_status") or _FROM_HRMS_STATUS.get((p.status or "").lower(), "In Progress")
        progress = m.get("progress")
        out.append({
            "id": str(p.id),
            "name": p.name,
            "client": customers.get(p.customer_id) or "",
            "company_name": m.get("company_name") or "",
            "contact": m.get("contact") or "",
            "email": m.get("email") or "",
            "attachments": m.get("attachments") or [],
            "status": status,
            "progress": float(progress) if progress is not None else {"Completed": 1.0, "Pending": 0.0}.get(status, 0.1),
            "start_date": _iso(p.planned_start_date),
            "end_date": _iso(p.planned_end_date),
            "due_date": _iso(p.planned_end_date),
            "assigned_employee_ids": allocs.get(p.id, []),
            "created_at": _iso(p.created_at),
        })
    return out


def _resolve_employee_ids(ctx: Ctx, refs) -> list[uuid.UUID]:
    """WorkTrack stores assignees as employee ids, codes or emails."""
    emps = ctx.employees()
    emails = {m.lower(): e for e, m in _login_emails(ctx).items()}
    by_code = {(e.employee_code or "").lower(): e.id for e in emps.values()}
    out = []
    for r in refs or []:
        s = str(r).strip()
        u = _uuid(s)
        if u and u in emps:
            out.append(u)
        elif s.lower() in by_code:
            out.append(by_code[s.lower()])
        elif s.lower() in emails:
            out.append(emails[s.lower()])
    return list(dict.fromkeys(out))


def _unique_project_code(ctx: Ctx, name: str) -> str:
    """Project code from the name (as HRMS does), made unique per company:
    "Internal Tools" -> INTERNALTO, then INTERNALTO-2, -3, ..."""
    import re
    base = re.sub(r"[^A-Z0-9]", "", name.upper())[:10] or "PRJ"
    taken = set(ctx.db.scalars(select(models.Project.code).where(
        models.Project.company_id == ctx.company_id, models.Project.code.like(f"{base}%"))))
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _save_project(ctx: Ctx, v: dict, project: models.Project | None) -> models.Project:
    db = ctx.db
    status_label = v.get("status")
    if project is None:
        name = (v.get("name") or "").strip()
        if not name:
            raise WtError("Project name is required.")
        existing = crud.get_project_by_name(db, ctx.company_id, name)
        if existing is not None:
            if not existing.is_active:
                existing.is_active = True
            project = existing
        else:
            customer = crud.get_or_create_customer(db, ctx.company_id, v.get("client") or "")
            project = crud.create_project(
                db, ctx.company_id, name, code=_unique_project_code(ctx, name),
                status=_TO_HRMS_STATUS.get((status_label or "In Progress").lower(), "active"),
                planned_start_date=_date(v.get("start_date")) or crud.company_today(db, ctx.company_id),
                planned_end_date=_date(v.get("due_date") or v.get("end_date")),
                customer_id=customer.id if customer else None,
            )
    else:
        if v.get("name"):
            project.name = v["name"].strip()
        if "client" in v:
            customer = crud.get_or_create_customer(db, ctx.company_id, v.get("client") or "")
            project.customer_id = customer.id if customer else None
        if status_label:
            project.status = _TO_HRMS_STATUS.get(status_label.lower(), project.status)
        if v.get("due_date") or v.get("end_date"):
            project.planned_end_date = _date(v.get("due_date") or v.get("end_date"))
        if v.get("start_date"):
            project.planned_start_date = _date(v["start_date"])
    db.flush()
    meta_fields = {k: v[k] for k in ("company_name", "contact", "email", "attachments", "progress") if k in v}
    if status_label:
        meta_fields["wt_status"] = status_label
    if meta_fields:
        import json
        cols = ", ".join(meta_fields)
        params = {k: (json.dumps(val) if k == "attachments" else val) for k, val in meta_fields.items()}
        vals = ", ".join(f"cast(:{k} as jsonb)" if k == "attachments" else f":{k}" for k in meta_fields)
        sets = ", ".join(f"{k} = excluded.{k}" for k in meta_fields)
        db.execute(text(f"insert into pm_wt_project_meta (project_id, {cols}) values (:pid, {vals}) "
                        f"on conflict (project_id) do update set {sets}, updated_at = now()"),
                   {"pid": str(project.id), **params})
    if "assigned_employee_ids" in v:
        wanted = set(_resolve_employee_ids(ctx, v.get("assigned_employee_ids")))
        current = {a.employee_id: a for a in db.scalars(select(models.ProjectAllocation).where(
            models.ProjectAllocation.project_id == project.id, models.ProjectAllocation.is_active.is_(True)))}
        today = crud.company_today(db, ctx.company_id)
        start = project.planned_start_date if project.planned_start_date and project.planned_start_date <= today else today
        for emp_id in wanted - set(current):
            # WorkTrack has no allocation %: give the employee's remaining
            # capacity (HRMS caps the total across projects at 100%).
            pct = int(max(0.0, min(100.0, 100.0 - work_rules.allocated_pct(db, emp_id))))
            try:
                crud.create_project_allocation(db, project.id, emp_id, pct, start_date=start)
            except ValueError:
                pass
        for emp_id in set(current) - wanted:
            current[emp_id].is_active = False
            current[emp_id].end_date = crud.company_today(db, ctx.company_id)
    return project


# ── work entries ───────────────────────────────────────────────────────────

def _entry_rows(ctx: Ctx, employee_ids: list[uuid.UUID] | None, date_from=None, date_to=None, entry_id=None) -> list[dict]:
    db = ctx.db
    q = (select(models.WorkEntry, models.Timesheet.employee_id, models.Project.name)
         .join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
         .outerjoin(models.Project, models.Project.id == models.WorkEntry.project_id)
         .where(models.Timesheet.company_id == ctx.company_id))
    if employee_ids is not None:
        q = q.where(models.Timesheet.employee_id.in_(employee_ids))
    if date_from:
        q = q.where(models.WorkEntry.entry_date >= date_from)
    if date_to:
        q = q.where(models.WorkEntry.entry_date <= date_to)
    if entry_id:
        q = q.where(models.WorkEntry.id == entry_id)
    rows = db.execute(q).all()
    meta = _meta(ctx, "pm_wt_entry_meta", "entry_id", [r[0].id for r in rows])
    out = []
    for e, emp_id, project_name in rows:
        m = meta.get(str(e.id), {})
        status = m.get("wt_status")
        if status is None and (e.status or "") == "rejected":
            status = "Rejected"
        out.append({
            "id": str(e.id),
            "user_id": str(emp_id),
            "entry_date": _iso(e.entry_date),
            "project_id": str(e.project_id) if e.project_id else None,
            "project_name": project_name or e.task_name,
            "description": e.description or "",
            "remarks": m.get("remarks"),
            "notes": m.get("remarks"),
            "start_time": e.start_time.strftime("%H:%M:%S") if e.start_time else None,
            "end_time": e.end_time.strftime("%H:%M:%S") if e.end_time else None,
            "hours_worked": _num(e.hours),
            "location": m.get("location") or "",
            "billable": bool(e.is_billable),
            "status": status,
            "submitted_at": _iso(m.get("submitted_at")),
            "created_at": _iso(e.created_at),
        })
    return out


def _timesheet_for(ctx: Ctx, employee_id: uuid.UUID, day: dt.date) -> models.Timesheet:
    week_start = day - dt.timedelta(days=day.weekday())
    ts = ctx.db.scalar(select(models.Timesheet).where(models.Timesheet.employee_id == employee_id,
                                                      models.Timesheet.week_start == week_start))
    if ts is None:
        ts = crud.create_timesheet(ctx.db, ctx.company_id, employee_id, week_start, 0.0, 0.0, status="draft")
    return ts


def _project_for_entry(ctx: Ctx, v: dict, employee_id: uuid.UUID, day: dt.date | None = None) -> models.Project:
    pid = _uuid(v.get("project_id"))
    project = ctx.db.get(models.Project, pid) if pid else None
    if project is not None and project.company_id != ctx.company_id:
        project = None
    if project is None and (v.get("project_name") or "").strip():
        project = crud.get_project_by_name(ctx.db, ctx.company_id, v["project_name"].strip())
        if project is None:
            # WorkTrack's "Other (add new project)" -- same as before, the
            # employee gets the new project assigned to them, from the
            # entry's date (HRMS only accepts time on allocated projects).
            start = min(day, crud.company_today(ctx.db, ctx.company_id)) if day else None
            project = _save_project(ctx, {"name": v["project_name"].strip(),
                                          "start_date": start.isoformat() if start else None,
                                          "assigned_employee_ids": [str(employee_id)]}, None)
    if project is None:
        raise WtError("A valid project is required for work entries.")
    return project


def _upsert_entry_meta(ctx: Ctx, entry_id: uuid.UUID, v: dict) -> None:
    fields = {}
    if "location" in v:
        fields["location"] = v["location"]
    if "remarks" in v or "notes" in v:
        fields["remarks"] = v.get("remarks", v.get("notes"))
    if "status" in v:
        fields["wt_status"] = v["status"]
    if "submitted_at" in v:
        fields["submitted_at"] = v["submitted_at"]
    if not fields:
        return
    cols = ", ".join(fields)
    vals = ", ".join(f"cast(:{k} as timestamptz)" if k == "submitted_at" else f":{k}" for k in fields)
    sets = ", ".join(f"{k} = excluded.{k}" for k in fields)
    ctx.db.execute(text(f"insert into pm_wt_entry_meta (entry_id, {cols}) values (:eid, {vals}) "
                        f"on conflict (entry_id) do update set {sets}, updated_at = now()"),
                   {"eid": str(entry_id), **fields})


def _apply_entry_status(ctx: Ctx, e: models.WorkEntry, status: str | None) -> None:
    """Keeps the HRMS entry status in step: Rejected in WorkTrack = rejected
    in HRMS; anything else counts as a valid (approved) entry, which is how
    HRMS saves work entries."""
    if status is None:
        return
    s = status.lower()
    e.status = "rejected" if s == "rejected" else "approved"
    if s in ("approved", "rejected"):
        e.approver_id = ctx.employee_id
        e.decided_at = dt.datetime.now(dt.timezone.utc)


def _check_entry_rules(ctx: Ctx, employee_id: uuid.UUID, project: models.Project, day: dt.date, hours: float,
                       task_name: str | None, exclude_id: uuid.UUID | None = None,
                       check_allocation: bool = True) -> None:
    """The HRMS work-entry rules (routers/work.py create / edit): allocated
    to the project that day, the week isn't locked, at most 24 h a day, no
    identical entry."""
    if check_allocation and (err := work_rules.allocation_error(ctx.db, employee_id, project, day)):
        raise WtError(err, "42501", 403)
    if err := work_rules.locked_week_error(ctx.db, employee_id, day):
        raise WtError(err, "P0001", 409)
    crud._advisory_lock(ctx.db, "work_entry_day", str(employee_id), day.isoformat())
    if err := work_rules.entry_error(ctx.db, employee_id, day, project_id=project.id, task_id=None,
                                     task_name=task_name, start_time=None, end_time=None, hours=hours,
                                     exclude_id=exclude_id):
        raise WtError(err)


def _insert_entry(ctx: Ctx, v: dict) -> uuid.UUID:
    employee_id = _uuid(v.get("user_id")) or ctx.employee_id
    if employee_id != ctx.employee_id and not ctx.is_admin:
        raise WtError("You can only add your own work entries.", "42501", 403)
    if employee_id not in ctx.employees():
        raise WtError("Employee not found.", "P0002", 404)
    day = _date(v.get("entry_date"))
    if day is None:
        raise WtError("A valid date is required.")
    try:
        crud.validate_work_entry_date(ctx.db, ctx.company_id, day)
    except ValueError as exc:
        raise WtError(str(exc)) from exc
    project = _project_for_entry(ctx, v, employee_id, day)
    hours = float(v.get("hours_worked") or 0)
    if hours <= 0 or hours > 24:
        raise WtError("Hours worked must be more than 0 and at most 24.")
    task_name = ((v.get("description") or project.name) or "")[:200]
    _check_entry_rules(ctx, employee_id, project, day, hours, task_name)
    ts = _timesheet_for(ctx, employee_id, day)
    # WorkTrack always sends a fixed 09:00-18:00 placeholder (it records
    # hours, not a time window), so no start / end time is stored -- that
    # also keeps several entries on one day from counting as "overlapping".
    entry = crud.create_work_entry(
        ctx.db, ts.id, project.id, day, category="General",
        start_time=None, end_time=None, hours=hours,
        description=v.get("description") or None, is_billable=bool(v.get("billable", True)),
        task_name=task_name,
    )
    _upsert_entry_meta(ctx, entry.id, v)
    _apply_entry_status(ctx, entry, v.get("status"))
    crud.create_audit_log(ctx.db, ctx.company_id, ctx.user.id, "create", "work_entry", entry.id)
    crud.resync_timesheet_hours(ctx.db, employee_id, ts.week_start)
    return entry.id


def _update_entry(ctx: Ctx, entry_id: uuid.UUID, v: dict) -> None:
    db = ctx.db
    e = db.get(models.WorkEntry, entry_id)
    ts = db.get(models.Timesheet, e.timesheet_id) if e else None
    if e is None or ts is None or ts.company_id != ctx.company_id:
        return
    only_status = set(v) <= {"status", "submitted_at"}
    if ts.employee_id != ctx.employee_id and not ctx.is_admin:
        raise WtError("You can only change your own work entries.", "42501", 403)
    if v.get("status") in ("Approved", "Rejected") and not ctx.is_admin:
        raise WtError("Only an admin can approve or reject entries.", "42501", 403)
    old_week = ts.week_start
    if not only_status:
        # HRMS: an approved / rejected week is locked for edits.
        if err := work_rules.locked_week_error(db, ts.employee_id, e.entry_date):
            raise WtError(err, "P0001", 409)
        day = e.entry_date
        if "entry_date" in v and _date(v["entry_date"]):
            day = _date(v["entry_date"])
            try:
                crud.validate_work_entry_date(db, ctx.company_id, day)
            except ValueError as exc:
                raise WtError(str(exc)) from exc
        project = db.get(models.Project, e.project_id)
        if "project_id" in v or "project_name" in v:
            project = _project_for_entry(ctx, v, ts.employee_id, day)
        hours = float(e.hours)
        if "hours_worked" in v and v["hours_worked"] is not None:
            hours = float(v["hours_worked"])
            if hours <= 0 or hours > 24:
                raise WtError("Hours worked must be more than 0 and at most 24.")
        task_name = ((v["description"] or e.task_name or "")[:200]) if "description" in v else e.task_name
        _check_entry_rules(ctx, ts.employee_id, project, day, hours, task_name, exclude_id=e.id,
                           check_allocation=(project.id != e.project_id or day != e.entry_date))
        if day != e.entry_date:
            e.entry_date = day
            e.timesheet_id = _timesheet_for(ctx, ts.employee_id, day).id
        e.project_id = project.id
        e.hours = hours
        e.task_name = task_name
        if "description" in v:
            e.description = v["description"] or None
        if "billable" in v:
            e.is_billable = bool(v["billable"])
        # start / end: WorkTrack's fixed placeholder is not stored (see
        # _insert_entry); times set in HRMS are left as they are.
    _upsert_entry_meta(ctx, e.id, v)
    _apply_entry_status(ctx, e, v.get("status"))
    db.flush()
    crud.resync_timesheet_hours(db, ts.employee_id, old_week)
    new_week = e.entry_date - dt.timedelta(days=e.entry_date.weekday())
    if new_week != old_week:
        crud.resync_timesheet_hours(db, ts.employee_id, new_week)


def _delete_entry(ctx: Ctx, entry_id: uuid.UUID) -> None:
    db = ctx.db
    e = db.get(models.WorkEntry, entry_id)
    ts = db.get(models.Timesheet, e.timesheet_id) if e else None
    if e is None or ts is None or ts.company_id != ctx.company_id:
        return
    if ts.employee_id != ctx.employee_id and not ctx.is_admin:
        raise WtError("You can only delete your own work entries.", "42501", 403)
    if err := work_rules.locked_week_error(db, ts.employee_id, e.entry_date):
        raise WtError(err, "P0001", 409)
    db.execute(text("delete from pm_wt_entry_meta where entry_id = :id"), {"id": str(entry_id)})
    db.delete(e)
    db.flush()
    crud.resync_timesheet_hours(db, ts.employee_id, ts.week_start)


# ── simple WorkTrack-only tables ───────────────────────────────────────────

ATTENDANCE_LOG_DAYS = 60


def _checkin_rows(ctx: Ctx, employee_ids: list[uuid.UUID] | None) -> list[dict]:
    """WorkTrack's IN/OUT log, plus check-ins made in HRMS Attendance (last
    ATTENDANCE_LOG_DAYS days) so both apps show the same day."""
    sql = "select * from pm_wt_checkin_logs where company_id = :c"
    params: dict = {"c": str(ctx.company_id)}
    if employee_ids is not None:
        sql += " and employee_id = any(cast(:e as uuid[]))"
        params["e"] = [str(i) for i in employee_ids]
    rows = [{
        "id": str(r["id"]), "user_id": str(r["employee_id"]), "employee_id": r["employee_code"],
        "employee_name": r["employee_name"], "employee_email": r["employee_email"], "log_type": r["log_type"],
        "time": r["log_time"], "date": r["log_date"], "location_device_id": r["location_device_id"],
        "mode_of_work": r["mode_of_work"], "created_at": _iso(r["created_at"]),
    } for r in ctx.db.execute(text(sql), params).mappings()]
    logged = {(r["user_id"], r["date"], r["log_type"]) for r in rows}

    tz = crud.company_tzinfo(ctx.db, ctx.company_id)
    since = crud.company_today(ctx.db, ctx.company_id) - dt.timedelta(days=ATTENDANCE_LOG_DAYS)
    q = select(models.AttendanceRecord).where(models.AttendanceRecord.company_id == ctx.company_id,
                                              models.AttendanceRecord.attendance_date >= since,
                                              models.AttendanceRecord.check_in.is_not(None))
    if employee_ids is not None:
        q = q.where(models.AttendanceRecord.employee_id.in_(employee_ids))
    emps = ctx.employees()
    emails = _login_emails(ctx)
    for rec in ctx.db.scalars(q):
        e = emps.get(rec.employee_id)
        if e is None:
            continue
        for log_type, at in (("IN", rec.check_in), ("OUT", rec.check_out)):
            if at is None:
                continue
            local = (at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)).astimezone(tz)
            day = local.strftime("%d-%m-%Y")
            if (str(e.id), day, log_type) in logged:
                continue  # already in WorkTrack's own log
            rows.append({
                "id": f"att-{rec.id}-{log_type}", "user_id": str(e.id), "employee_id": e.employee_code,
                "employee_name": _name(e), "employee_email": emails.get(e.id) or e.work_email, "log_type": log_type,
                "time": local.strftime("%I:%M:%S %p").lstrip("0"), "date": day, "location_device_id": "HRMS Attendance",
                "mode_of_work": rec.work_mode, "created_at": _iso(at),
            })
    return rows


def _insert_checkin(ctx: Ctx, v: dict) -> uuid.UUID:
    employee_id = _uuid(v.get("user_id")) or ctx.employee_id
    if employee_id != ctx.employee_id and not ctx.is_admin:
        raise WtError("You can only check in for yourself.", "42501", 403)
    emp = ctx.employees().get(employee_id)
    if emp is None:
        raise WtError("Employee not found.", "P0002", 404)
    log_type = (v.get("log_type") or "").upper()
    if log_type not in ("IN", "OUT"):
        raise WtError("Log type must be IN or OUT.")
    # HRMS attendance clock -- same rules as HRMS Attendance (shift, leave).
    mode = v.get("mode_of_work") if v.get("mode_of_work") in schemas.ATTENDANCE_WORK_MODES else None
    try:
        with ctx.db.begin_nested():
            crud.clock_in_out(ctx.db, ctx.company_id, employee_id, crud.company_today(ctx.db, ctx.company_id),
                              dt.datetime.now(dt.timezone.utc), "check_in" if log_type == "IN" else "check_out",
                              work_mode=mode)
    except ValueError as exc:
        raise WtError(str(exc)) from exc
    new_id = uuid.uuid4()
    ctx.db.execute(text(
        "insert into pm_wt_checkin_logs (id, company_id, employee_id, employee_code, employee_name, employee_email,"
        " log_type, log_time, log_date, location_device_id, mode_of_work) values (:id, :c, :e, :code, :name, :email,"
        " :lt, :t, :d, :loc, :mode)"),
        {"id": str(new_id), "c": str(ctx.company_id), "e": str(employee_id), "code": v.get("employee_id") or emp.employee_code,
         "name": v.get("employee_name") or _name(emp), "email": v.get("employee_email"), "lt": log_type,
         "t": v.get("time"), "d": v.get("date"), "loc": v.get("location_device_id"), "mode": v.get("mode_of_work")})
    crud.create_audit_log(ctx.db, ctx.company_id, ctx.user.id, "update", "attendance_record", employee_id)
    return new_id


def _activity_rows(ctx: Ctx) -> list[dict]:
    out = []
    for r in ctx.db.execute(text("select * from pm_wt_activities where company_id = :c"), {"c": str(ctx.company_id)}).mappings():
        row = {
            "id": str(r["id"]), "employee_id": r["employee_code"], "employee_name": r["employee_name"],
            "employee_email": r["employee_email"], "action": r["action"], "project": r["project"],
            "time": r["activity_time"], "status": r["status"], "avatar_url": r["avatar_url"],
            "created_at": _iso(r["created_at"]),
        }
        row.update({k: val for k, val in (r["details"] or {}).items() if k not in row})
        out.append(row)
    return out


_ACTIVITY_COLS = {"employee_id": "employee_code", "employee_name": "employee_name", "employee_email": "employee_email",
                  "action": "action", "project": "project", "time": "activity_time", "status": "status",
                  "avatar_url": "avatar_url"}


def _insert_activity(ctx: Ctx, v: dict) -> uuid.UUID:
    import json
    new_id = uuid.uuid4()
    details = {k: val for k, val in v.items() if k not in _ACTIVITY_COLS and k not in ("id", "created_at")}
    ctx.db.execute(text(
        "insert into pm_wt_activities (id, company_id, employee_code, employee_name, employee_email, action, project,"
        " activity_time, status, avatar_url, details, created_at) values (:id, :c, :code, :name, :email, :action,"
        " :project, :t, :status, :avatar, cast(:details as jsonb), coalesce(cast(:created as timestamptz), now()))"),
        {"id": str(new_id), "c": str(ctx.company_id), "code": v.get("employee_id"), "name": v.get("employee_name"),
         "email": v.get("employee_email"), "action": v.get("action"), "project": v.get("project"),
         "t": v.get("time"), "status": v.get("status"), "avatar": v.get("avatar_url"),
         "details": json.dumps(details, default=str), "created": v.get("created_at")})
    return new_id


def _invoice_rows(ctx: Ctx, employee_ids: list[uuid.UUID] | None) -> list[dict]:
    sql = "select * from pm_wt_freelancer_invoices where company_id = :c"
    params: dict = {"c": str(ctx.company_id)}
    if employee_ids is not None:
        sql += " and employee_id = any(cast(:e as uuid[]))"
        params["e"] = [str(i) for i in employee_ids]
    return [{
        "id": str(r["id"]), "user_id": str(r["employee_id"]), "employee_name": r["employee_name"],
        "month_year": r["month_year"], "total_hours": _num(r["total_hours"]), "hourly_rate": _num(r["hourly_rate"]),
        "currency": r["currency"], "total_amount": _num(r["total_amount"]), "projects": r["projects"],
        "status": r["status"], "submitted_at": _iso(r["submitted_at"]), "created_at": _iso(r["created_at"]),
    } for r in ctx.db.execute(text(sql), params).mappings()]


def _insert_invoice(ctx: Ctx, v: dict) -> uuid.UUID:
    import json
    employee_id = _uuid(v.get("user_id")) or ctx.employee_id
    if employee_id != ctx.employee_id and not ctx.is_admin:
        raise WtError("You can only submit your own invoices.", "42501", 403)
    new_id = uuid.uuid4()
    ctx.db.execute(text(
        "insert into pm_wt_freelancer_invoices (id, company_id, employee_id, employee_name, month_year, total_hours,"
        " hourly_rate, currency, total_amount, projects, status, submitted_at) values (:id, :c, :e, :name, :m, :h,"
        " :r, :cur, :amt, cast(:p as jsonb), :s, cast(:sub as timestamptz))"),
        {"id": str(new_id), "c": str(ctx.company_id), "e": str(employee_id), "name": v.get("employee_name"),
         "m": v.get("month_year"), "h": v.get("total_hours"), "r": v.get("hourly_rate"), "cur": v.get("currency"),
         "amt": v.get("total_amount"), "p": json.dumps(v.get("projects") or []), "s": v.get("status") or "Submitted",
         "sub": v.get("submitted_at")})
    return new_id


def _task_rows(ctx: Ctx, employee_ids: list[uuid.UUID] | None) -> list[dict]:
    q = select(models.TaskBoardCard).where(models.TaskBoardCard.company_id == ctx.company_id,
                                           models.TaskBoardCard.is_active.is_(True))
    if employee_ids is not None:
        q = q.where(models.TaskBoardCard.assignee_employee_id.in_(employee_ids))
    return [{
        "id": str(t.id), "title": t.title, "description": t.description, "assigned_to":
            str(t.assignee_employee_id) if t.assignee_employee_id else None,
        "project_id": str(t.project_id), "due_date": _iso(t.due_date), "status": t.status, "priority": t.priority,
        "created_at": _iso(t.created_at),
    } for t in ctx.db.scalars(q)]


# ── generic filtering (same semantics as the app's query builder) ──────────

def _cmp(a, b) -> int:
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    try:
        fa, fb = float(a), float(b)
        return (fa > fb) - (fa < fb)
    except (TypeError, ValueError):
        sa, sb = str(a), str(b)
        return (sa > sb) - (sa < sb)


def _match(row: dict, col: str, op: str, val) -> bool:
    v = row.get(col)
    if op == "eq":
        return str(v) == str(val) if v is not None else val is None
    if op == "neq":
        return str(v) != str(val)
    if op == "in":
        return str(v) in {str(x) for x in (val or [])}
    if op == "is":
        return v is None if val is None else v == val
    if op == "ilike":
        import re
        pattern = "^" + re.escape(str(val)).replace("%", ".*").replace("_", ".") + "$"
        return re.match(pattern, str(v or ""), re.IGNORECASE) is not None
    if v is None:
        return False
    c = _cmp(v, val)
    return {"gt": c > 0, "gte": c >= 0, "lt": c < 0, "lte": c <= 0}.get(op, False)


def _apply(rows: list[dict], q: QueryIn) -> list[dict]:
    rows = [r for r in rows if all(_match(r, f[0], f[1], f[2]) for f in q.filters)
            and all(any(_match(r, g[0], g[1], g[2]) for g in group) for group in q.or_groups)]
    for col, asc in reversed(q.order):
        import functools
        rows.sort(key=functools.cmp_to_key(lambda a, b, c=col: _cmp(a.get(c), b.get(c))), reverse=not asc)
    if q.offset:
        rows = rows[q.offset:]
    if q.limit is not None:
        rows = rows[: q.limit]
    return rows


def _filter_value(q: QueryIn, col: str, ops=("eq",)):
    return [f[2] for f in q.filters if f[0] == col and f[1] in ops]


def _scoped_employee_ids(ctx: Ctx, q: QueryIn, col: str) -> list[uuid.UUID] | None:
    """Employees whose rows may be returned: admins see the company (or the
    ids they filter on), everyone else only themselves."""
    if not ctx.is_admin:
        return [ctx.employee_id] if ctx.employee_id else []
    ids = [_uuid(x) for x in _filter_value(q, col)]
    ids += [_uuid(x) for f in q.filters if f[0] == col and f[1] == "in" for x in (f[2] or [])]
    for group in q.or_groups:
        if group and all(g[0] == col and g[1] == "eq" for g in group):
            ids += [_uuid(g[2]) for g in group]
    ids = [i for i in ids if i]
    return ids or None


def _rows_for(ctx: Ctx, q: QueryIn) -> list[dict]:
    t = q.table
    if t in ("employees", "freelancers"):
        rows = _employee_rows(ctx, freelancers=(t == "freelancers"))
        if not ctx.is_admin:
            rows = [r for r in rows if r["id"] == str(ctx.employee_id)]
        return rows
    if t == "projects":
        return _project_rows(ctx)
    if t == "work_entries":
        scope = _scoped_employee_ids(ctx, q, "user_id")
        gte = [_date(x) for x in _filter_value(q, "entry_date", ("gte", "gt"))]
        lte = [_date(x) for x in _filter_value(q, "entry_date", ("lte", "lt"))]
        entry_id = next((_uuid(x) for x in _filter_value(q, "id")), None)
        return _entry_rows(ctx, scope, max([d for d in gte if d], default=None),
                           min([d for d in lte if d], default=None), entry_id)
    if t == "employee_checkins":
        return _checkin_rows(ctx, _scoped_employee_ids(ctx, q, "user_id"))
    if t == "activities":
        return _activity_rows(ctx)
    if t == "freelancer_invoices":
        return _invoice_rows(ctx, _scoped_employee_ids(ctx, q, "user_id"))
    if t == "tasks":
        scope = _scoped_employee_ids(ctx, q, "assigned_to")
        return _task_rows(ctx, scope)
    if t == "users":
        return []
    raise WtError(f"Could not find the table 'public.{t}' in the schema cache", "PGRST205", 404)


def _embed(ctx: Ctx, q: QueryIn, rows: list[dict]) -> list[dict]:
    """`*, projects:project_id (name)` style embeds (used by My Tasks)."""
    import re
    for alias, fk, cols in re.findall(r"(\w+)\s*:\s*(\w+)\s*\(([^)]*)\)", q.columns):
        if alias != "projects":
            continue
        projects = {p["id"]: p for p in _project_rows(ctx)}
        wanted = [c.strip() for c in cols.split(",") if c.strip()]
        for r in rows:
            p = projects.get(str(r.get(fk)))
            r[alias] = None if p is None else ({c: p.get(c) for c in wanted} if wanted and "*" not in wanted else p)
    return rows


# ── write dispatch ─────────────────────────────────────────────────────────

def _write(ctx: Ctx, q: QueryIn) -> list[dict]:
    t, op = q.table, q.op
    values = q.values if isinstance(q.values, list) else ([q.values] if q.values is not None else [])
    touched: list[str] = []

    if t in ("employees", "freelancers"):
        freelancer = t == "freelancers"
        emps = ctx.employees()
        if op in ("insert", "upsert"):
            for v in values:
                eid = _uuid(v.get("id"))
                e = emps.get(eid) if eid else None
                if e is None and op == "upsert" and v.get("email"):
                    # Sign-up style upserts for an existing login map onto that employee.
                    match = [i for i, m in _login_emails(ctx).items() if m.lower() == str(v["email"]).lower()]
                    e = emps.get(match[0]) if match else None
                if e is None:
                    row = _create_employee(ctx, v, freelancer)
                    touched.append(row["id"])
                    if row.get("_temp_password"):
                        ctx._last_temp_password = row["_temp_password"]
                else:
                    _update_employee(ctx, e, v, freelancer)
                    touched.append(str(e.id))
        elif op == "update":
            for r in _apply(_rows_for(ctx, q), q):
                _update_employee(ctx, emps[uuid.UUID(r["id"])], q.values or {}, freelancer)
                touched.append(r["id"])
        elif op == "delete":
            ctx.require_admin()
            for r in _apply(_rows_for(ctx, q), q):
                e = emps[uuid.UUID(r["id"])]
                if e.id == ctx.employee_id:
                    raise WtError("You cannot deactivate your own account.")
                for u in ctx.db.scalars(select(models.User).where(models.User.employee_id == e.id)):
                    error = role_tiers.user_management_error(ctx.db, ctx.user, u)
                    if error is not None:
                        raise WtError(error, "42501", 403)
                _set_active(ctx, e, False)  # never deleted -- HRMS data stays
                touched.append(r["id"])
        ctx.db.flush()
        ctx._emp_cache = None
        return [r for r in _rows_for(ctx, QueryIn(table=t, op="select")) if r["id"] in touched] if op != "delete" else []

    if t == "projects":
        if op in ("insert", "upsert"):
            for v in values:
                pid = _uuid(v.get("id"))
                project = ctx.db.get(models.Project, pid) if pid else None
                if project is not None and project.company_id != ctx.company_id:
                    project = None
                if project is not None and not ctx.is_admin:
                    raise WtError("Only an admin can edit projects.", "42501", 403)
                project = _save_project(ctx, v, project)
                touched.append(str(project.id))
        elif op == "update":
            ctx.require_admin()
            for r in _apply(_rows_for(ctx, q), q):
                _save_project(ctx, q.values or {}, ctx.db.get(models.Project, uuid.UUID(r["id"])))
                touched.append(r["id"])
        elif op == "delete":
            ctx.require_admin()
            for r in _apply(_rows_for(ctx, q), q):
                ctx.db.get(models.Project, uuid.UUID(r["id"])).is_active = False  # soft delete
        ctx.db.flush()
        return [r for r in _project_rows(ctx) if r["id"] in touched]

    if t == "work_entries":
        if op in ("insert", "upsert"):
            for v in values:
                eid = _uuid(v.get("id"))
                if eid and ctx.db.get(models.WorkEntry, eid) is not None:
                    _update_entry(ctx, eid, v)
                    touched.append(str(eid))
                else:
                    touched.append(str(_insert_entry(ctx, v)))
        elif op == "update":
            for r in _apply(_rows_for(ctx, q), q):
                _update_entry(ctx, uuid.UUID(r["id"]), q.values or {})
                touched.append(r["id"])
        elif op == "delete":
            for r in _apply(_rows_for(ctx, q), q):
                _delete_entry(ctx, uuid.UUID(r["id"]))
            return []
        ctx.db.flush()
        ids = [uuid.UUID(i) for i in touched]
        return [r for r in _entry_rows(ctx, None) if r["id"] in touched] if len(ids) > 20 else \
            [row for i in ids for row in _entry_rows(ctx, None, entry_id=i)]

    if t == "employee_checkins":
        if op != "insert":
            raise WtError("Check-in logs can only be added.")
        ids = [str(_insert_checkin(ctx, v)) for v in values]
        return [r for r in _checkin_rows(ctx, None) if r["id"] in ids]

    if t == "activities":
        if op == "insert":
            ids = [str(_insert_activity(ctx, v)) for v in values]
            return [r for r in _activity_rows(ctx) if r["id"] in ids]
        ctx.require_admin()
        matched = _apply(_activity_rows(ctx), q)
        ids = [r["id"] for r in matched]
        if not ids:
            return []
        if op == "delete":
            ctx.db.execute(text("delete from pm_wt_activities where id = any(cast(:ids as uuid[]))"), {"ids": ids})
            return []
        if op == "update" and q.values:
            sets = {_ACTIVITY_COLS[k]: val for k, val in q.values.items() if k in _ACTIVITY_COLS}
            if sets:
                ctx.db.execute(text("update pm_wt_activities set " + ", ".join(f"{c} = :{c}" for c in sets)
                                    + " where id = any(cast(:ids as uuid[]))"), {**sets, "ids": ids})
            return [r for r in _activity_rows(ctx) if r["id"] in ids]
        return []

    if t == "freelancer_invoices":
        if op != "insert":
            raise WtError("Invoices can only be submitted.")
        ids = [str(_insert_invoice(ctx, v)) for v in values]
        return [r for r in _invoice_rows(ctx, None) if r["id"] in ids]

    raise WtError(f"Writing to '{t}' is not supported.", "42501", 403)


# ── endpoints ──────────────────────────────────────────────────────────────

@router.post("/query")
def query(payload: QueryIn, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    ensure_tables(db)
    ctx = Ctx(db, current_user)
    ctx._last_temp_password = None
    try:
        if payload.op == "select":
            rows = _embed(ctx, payload, _apply(_rows_for(ctx, payload), payload))
            return {"rows": rows}
        rows = _write(ctx, payload)
        db.commit()
        extra = {"temp_password": ctx._last_temp_password} if ctx._last_temp_password else {}
        return {"rows": rows, **extra}
    except WtError as exc:
        db.rollback()
        _fail(exc)
    except IntegrityError as exc:
        db.rollback()
        logger.warning("worktrack write failed: %s", exc)
        _fail(WtError("duplicate key value violates unique constraint", "23505", 409))
    except ValueError as exc:
        db.rollback()
        _fail(WtError(str(exc)))


def _user_metadata(ctx: Ctx) -> dict:
    e = ctx.db.get(models.Employee, ctx.employee_id) if ctx.employee_id else None
    meta = ctx.db.execute(text("select data from pm_wt_user_meta where user_id = :u"),
                          {"u": str(ctx.user.id)}).scalar() or {}
    name = _name(e) if e else (ctx.user.full_name or ctx.user.email.split("@")[0])
    dept = ctx.db.get(models.Department, e.department_id).name if e and e.department_id else None
    base = {
        "name": name, "full_name": name,
        "avatar_url": e.photo_url if e else None,
        "role": WT_ADMIN_LABEL if ctx.is_admin else "Employee",
        "department": dept, "employee_id": e.employee_code if e else None,
        "phone": getattr(e, "personal_phone", None) if e else None,
        "date_of_birth": _iso(e.date_of_birth) if e and getattr(e, "date_of_birth", None) else None,
    }
    return {**base, **{k: v for k, v in meta.items() if k not in ("name", "full_name", "avatar_url")}}


@router.get("/session")
def session(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """The signed-in user, shaped like WorkTrack's auth user."""
    ensure_tables(db)
    ctx = Ctx(db, current_user)
    return {
        "id": str(current_user.employee_id or current_user.id),
        "email": current_user.email,
        "is_admin": ctx.is_admin,
        "user_metadata": _user_metadata(ctx),
        "created_at": _iso(getattr(current_user, "created_at", None)),
    }


@router.put("/session/metadata")
def update_metadata(payload: MetadataIn, db: Session = Depends(get_db),
                    current_user: models.User = Depends(get_current_user)):
    """WorkTrack's own-profile extras (admin profile: manager, work schedule,
    ...). Name / phone / date of birth also update the HRMS employee."""
    import json
    ensure_tables(db)
    ctx = Ctx(db, current_user)
    data = dict(payload.data)
    e = db.get(models.Employee, current_user.employee_id) if current_user.employee_id else None
    if e is not None:
        if (data.get("name") or "").strip():
            parts = data["name"].strip().split(None, 1)
            e.first_name, e.last_name = parts[0], (parts[1] if len(parts) > 1 else None)
        if "phone" in data:
            e.personal_phone = data.get("phone") or None
        if data.get("date_of_birth") and _date(data["date_of_birth"]) and hasattr(e, "date_of_birth"):
            e.date_of_birth = _date(data["date_of_birth"])
        url = data.get("avatar_url")
        if isinstance(url, str) and url.startswith("/media/"):
            e.photo_url = url.split("?", 1)[0]
    keep = {k: v for k, v in data.items() if k not in ("avatar_url",)}
    db.execute(text("insert into pm_wt_user_meta (user_id, data) values (:u, cast(:d as jsonb)) "
                    "on conflict (user_id) do update set data = pm_wt_user_meta.data || excluded.data, updated_at = now()"),
               {"u": str(current_user.id), "d": json.dumps(keep, default=str)})
    db.commit()
    return {"user_metadata": _user_metadata(ctx)}


@router.post("/me/password", status_code=204)
def change_my_password(payload: PasswordIn, db: Session = Depends(get_db),
                       current_user: models.User = Depends(get_current_user)):
    """Signed-in user changes their own password (shared with HRMS). Existing
    sessions end -- the app signs in again with the new password."""
    _set_password(db, current_user, current_user, payload.new_password)


def _set_password(db: Session, actor: models.User, target: models.User, new_password: str) -> None:
    """Same steps as HRMS Administration > reset password: new hash, every
    older session ends, audit log -- and the login cache is refreshed after
    commit so signing in with the new password works straight away."""
    crud.reset_user_password(db, target.id, security.hash_password(new_password))
    public_id = auth_state.public_user_id_for_core_user(db, target)
    auth_state.bump_token_version(db, public_id)
    crud.create_audit_log(db, actor.company_id, actor.id, "reset_password", "user", target.id)
    db.commit()
    auth_state.invalidate(public_id)


@router.post("/rpc/admin_reset_password")
def admin_reset_password(payload: ResetPasswordIn, db: Session = Depends(get_db),
                         current_user: models.User = Depends(get_current_user)):
    """Admin resets someone's password (or anyone their own) -- needs a login,
    unlike WorkTrack's old version."""
    ctx = Ctx(db, current_user)
    target = db.scalar(select(models.User).where(models.User.company_id == current_user.company_id,
                                                 func.lower(models.User.email) == payload.target_email.strip().lower()))
    if target is None:
        raise HTTPException(status_code=404, detail={"message": "No account with this email", "code": "P0002"})
    if target.id != current_user.id:
        if not ctx.is_admin:
            raise HTTPException(status_code=403, detail={"message": "Only an admin can reset another user's password.",
                                                         "code": "42501"})
        # Same rule as HRMS: only the Owner may reset the Owner's (or a
        # higher-tier admin's) password.
        error = role_tiers.user_management_error(db, current_user, target)
        if error is not None:
            raise HTTPException(status_code=403, detail={"message": error, "code": "42501"})
    _set_password(db, current_user, target, payload.new_password)
    return True
