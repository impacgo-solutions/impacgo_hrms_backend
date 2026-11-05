"""Administration > Roles & Permissions > Custom Modules.

Admin-defined modules (each a simple records workspace) whose access is
granted per EMPLOYEE, independently of roles. Tables: backend/db/
add_custom_modules.sql (tenant schema). Rules:

  * Configuration (create / edit / assign / remove) needs System Settings /
    RBAC at Edit or Admin -- the same gate as creating a custom role.
  * Using a module needs an explicit assignment. An unassigned caller gets
    404 (the module's existence is not revealed), whatever their role --
    admins and the Owner included (they can assign themselves). An assigned
    caller gets 403 for an action they were not granted.
  * Every action implies 'view'; assignments always carry it.
  * A tenant schema without the tables simply has no custom modules.
"""

import csv
import datetime
import io
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crud, models
from ..database import get_db
from ..deps import get_current_user, require_permission

router = APIRouter(prefix="/api", tags=["custom-modules"])

ACTIONS = ("view", "create", "edit", "delete", "approve", "export")
_ADMIN = require_permission("system_settings_rbac")


# ── Schemas ───────────────────────────────────────────────────────────────

def _clean_actions(actions: list[str]) -> list[str]:
    cleaned = {a.strip().lower() for a in actions if a and a.strip()}
    unknown = cleaned - set(ACTIONS)
    if unknown:
        raise ValueError(f"Unknown action(s): {', '.join(sorted(unknown))}. Allowed: {', '.join(ACTIONS)}")
    cleaned.add("view")
    return [a for a in ACTIONS if a in cleaned]


class AssignmentIn(BaseModel):
    employee_id: uuid.UUID
    actions: list[str] = Field(default_factory=lambda: ["view"])

    _v = field_validator("actions")(classmethod(lambda cls, v: _clean_actions(v)))


class CustomModuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=40)
    actions: list[str] = Field(default_factory=lambda: ["view"])
    assignments: list[AssignmentIn] = Field(default_factory=list)

    _v = field_validator("actions")(classmethod(lambda cls, v: _clean_actions(v)))


class CustomModuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=40)
    actions: list[str] | None = None
    is_active: bool | None = None

    @field_validator("actions")
    @classmethod
    def _actions(cls, v):
        return None if v is None else _clean_actions(v)


class AssignmentsReplace(BaseModel):
    assignments: list[AssignmentIn]


class RecordIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    details: str | None = Field(default=None, max_length=10000)


class RecordUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    details: str | None = Field(default=None, max_length=10000)


class RecordDecision(BaseModel):
    status: str = Field(pattern="^(approved|rejected|pending)$")


# ── Helpers ───────────────────────────────────────────────────────────────

def _require_tables(db: Session) -> None:
    if db.execute(text("SELECT to_regclass('core_custom_modules')")).scalar() is None:
        raise HTTPException(status_code=404, detail="Custom modules are not available for this organization yet.")


def _tables_present(db: Session) -> bool:
    return db.execute(text("SELECT to_regclass('core_custom_modules')")).scalar() is not None


def _get_module(db: Session, company_id: uuid.UUID, module_id: uuid.UUID) -> models.CustomModule:
    module = db.get(models.CustomModule, module_id)
    if module is None or module.company_id != company_id:
        raise HTTPException(status_code=404, detail="Module not found")
    return module


def _assignment(db: Session, module_id: uuid.UUID, employee_id: uuid.UUID | None):
    if employee_id is None:
        return None
    return db.scalar(
        select(models.CustomModuleAssignment).where(
            models.CustomModuleAssignment.module_id == module_id,
            models.CustomModuleAssignment.employee_id == employee_id,
        )
    )


def _require_action(db: Session, user: models.User, module_id: uuid.UUID, action: str) -> tuple:
    """(module, my_actions) for an assigned caller holding `action`."""
    _require_tables(db)
    module = db.get(models.CustomModule, module_id)
    grant = _assignment(db, module_id, user.employee_id) if module else None
    if module is None or module.company_id != user.company_id or not module.is_active or grant is None:
        raise HTTPException(status_code=404, detail="Module not found")
    held = set(grant.actions or []) & set(module.actions or [])
    if action not in held or "view" not in held:
        raise HTTPException(status_code=403, detail=f"You don't have '{action}' access to this module")
    return module, [a for a in ACTIONS if a in held]


def _validate_assignments(db: Session, company_id: uuid.UUID, module_actions: list[str], items: list[AssignmentIn]):
    ids = [a.employee_id for a in items]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="An employee can only be listed once per module")
    if ids:
        found = set(db.scalars(
            select(models.Employee.id).where(models.Employee.company_id == company_id, models.Employee.id.in_(ids))
        ))
        missing = set(ids) - found
        if missing:
            raise HTTPException(status_code=422, detail="One or more selected employees don't belong to this organization")
    for a in items:
        extra = set(a.actions) - set(module_actions)
        if extra:
            raise HTTPException(
                status_code=422,
                detail=f"Action(s) {', '.join(sorted(extra))} are not enabled on this module",
            )


def _replace_assignments(db: Session, module: models.CustomModule, items: list[AssignmentIn], actor_id: uuid.UUID) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    existing = {
        a.employee_id: a for a in db.scalars(
            select(models.CustomModuleAssignment).where(models.CustomModuleAssignment.module_id == module.id)
        )
    }
    wanted = {a.employee_id: a.actions for a in items}
    for emp_id, row in existing.items():
        if emp_id not in wanted:
            db.delete(row)
    for emp_id, actions in wanted.items():
        row = existing.get(emp_id)
        if row is None:
            db.add(models.CustomModuleAssignment(
                id=uuid.uuid4(), module_id=module.id, employee_id=emp_id, actions=actions,
                granted_by=actor_id, granted_at=now,
            ))
        elif list(row.actions or []) != actions:
            row.actions = actions
            row.updated_at = now
    db.flush()


def _module_out(db: Session, module: models.CustomModule) -> dict:
    rows = db.execute(
        select(models.CustomModuleAssignment.employee_id, models.CustomModuleAssignment.actions)
        .where(models.CustomModuleAssignment.module_id == module.id)
    ).all()
    names = crud.employee_display_names_bulk(db, [r[0] for r in rows]) if rows else {}
    records = db.scalar(
        select(func.count(models.CustomModuleRecord.id)).where(models.CustomModuleRecord.module_id == module.id)
    ) or 0
    return {
        "id": str(module.id),
        "name": module.name,
        "description": module.description,
        "icon": module.icon,
        "actions": [a for a in ACTIONS if a in (module.actions or [])],
        "is_active": module.is_active,
        "record_count": records,
        "created_at": module.created_at,
        "updated_at": module.updated_at,
        "assignments": sorted(
            [{"employee_id": str(e), "employee_name": names.get(e, "—"),
              "actions": [a for a in ACTIONS if a in (acts or [])]} for e, acts in rows],
            key=lambda x: x["employee_name"].lower(),
        ),
    }


def _record_out(db: Session, r: models.CustomModuleRecord, names: dict | None = None) -> dict:
    def name(eid):
        if eid is None:
            return None
        return (names or {}).get(eid) or crud.employee_display_name(db, eid)

    return {
        "id": str(r.id), "title": r.title, "details": r.details, "status": r.status,
        "created_by": name(r.created_by_employee), "created_at": r.created_at,
        "updated_by": name(r.updated_by_employee), "updated_at": r.updated_at,
        "decided_by": name(r.decided_by_employee), "decided_at": r.decided_at,
    }


def _audit(db, user, action, doctype, doc_id, changes=None):
    try:
        crud.create_audit_log(db, user.company_id, user.id, action, doctype, doc_id, changes=changes)
    except Exception:  # noqa: BLE001 -- never the reason a request fails
        pass


# ── Administration: configure modules and access ──────────────────────────

@router.get("/custom-modules")
def list_custom_modules(db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)):
    if not _tables_present(db):
        return []
    modules = db.scalars(
        select(models.CustomModule)
        .where(models.CustomModule.company_id == current_user.company_id)
        .order_by(func.lower(models.CustomModule.name))
    ).all()
    return [_module_out(db, m) for m in modules]


@router.post("/custom-modules", status_code=201)
def create_custom_module(
    payload: CustomModuleCreate, db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)
):
    _require_tables(db)
    _validate_assignments(db, current_user.company_id, payload.actions, payload.assignments)
    module = models.CustomModule(
        id=uuid.uuid4(), company_id=current_user.company_id, name=payload.name.strip(),
        description=(payload.description or "").strip() or None, icon=payload.icon,
        actions=payload.actions, is_active=True, created_by=current_user.id,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(module)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A custom module with this name already exists") from exc
    _replace_assignments(db, module, payload.assignments, current_user.id)
    _audit(db, current_user, "create", "custom_module", module.id,
           {"name": module.name, "actions": module.actions,
            "assignments": [{"employee_id": str(a.employee_id), "actions": a.actions} for a in payload.assignments]})
    db.commit()
    return _module_out(db, module)


@router.patch("/custom-modules/{module_id}")
def update_custom_module(
    module_id: uuid.UUID, payload: CustomModuleUpdate,
    db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN),
):
    _require_tables(db)
    module = _get_module(db, current_user.company_id, module_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is not None:
        module.name = changes["name"].strip()
    if "description" in changes:
        module.description = (changes["description"] or "").strip() or None
    if "icon" in changes:
        module.icon = changes["icon"]
    if changes.get("is_active") is not None:
        module.is_active = changes["is_active"]
    if changes.get("actions") is not None:
        module.actions = changes["actions"]
        # an action the module no longer supports is withdrawn from everyone
        for row in db.scalars(select(models.CustomModuleAssignment).where(
                models.CustomModuleAssignment.module_id == module.id)):
            kept = [a for a in (row.actions or []) if a in module.actions]
            if kept != list(row.actions or []):
                row.actions = kept or ["view"]
                row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    module.updated_by = current_user.id
    module.updated_at = datetime.datetime.now(datetime.timezone.utc)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A custom module with this name already exists") from exc
    _audit(db, current_user, "update", "custom_module", module.id, {k: v for k, v in changes.items()})
    db.commit()
    return _module_out(db, module)


@router.put("/custom-modules/{module_id}/assignments")
def replace_custom_module_assignments(
    module_id: uuid.UUID, payload: AssignmentsReplace,
    db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN),
):
    _require_tables(db)
    module = _get_module(db, current_user.company_id, module_id)
    _validate_assignments(db, current_user.company_id, module.actions or ["view"], payload.assignments)
    _replace_assignments(db, module, payload.assignments, current_user.id)
    module.updated_by = current_user.id
    module.updated_at = datetime.datetime.now(datetime.timezone.utc)
    _audit(db, current_user, "assign", "custom_module", module.id,
           {"assignments": [{"employee_id": str(a.employee_id), "actions": a.actions} for a in payload.assignments]})
    db.commit()
    return _module_out(db, module)


@router.delete("/custom-modules/{module_id}", status_code=204)
def delete_custom_module(
    module_id: uuid.UUID, db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)
):
    _require_tables(db)
    module = _get_module(db, current_user.company_id, module_id)
    name = module.name
    db.execute(delete(models.CustomModuleRecord).where(models.CustomModuleRecord.module_id == module.id))
    db.execute(delete(models.CustomModuleAssignment).where(models.CustomModuleAssignment.module_id == module.id))
    db.delete(module)
    _audit(db, current_user, "delete", "custom_module", module_id, {"name": name})
    db.commit()
    return Response(status_code=204)


# ── Using a module (assigned employees only) ──────────────────────────────

@router.get("/custom-modules/mine")
def my_custom_modules(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """The modules the caller was explicitly assigned, with their actions --
    drives the sidebar. Empty for everyone else."""
    if current_user.employee_id is None or not _tables_present(db):
        return []
    rows = db.execute(
        select(models.CustomModule, models.CustomModuleAssignment.actions)
        .join(models.CustomModuleAssignment, models.CustomModuleAssignment.module_id == models.CustomModule.id)
        .where(
            models.CustomModule.company_id == current_user.company_id,
            models.CustomModule.is_active.is_(True),
            models.CustomModuleAssignment.employee_id == current_user.employee_id,
        )
        .order_by(func.lower(models.CustomModule.name))
    ).all()
    out = []
    for module, actions in rows:
        held = [a for a in ACTIONS if a in set(actions or []) & set(module.actions or [])]
        if "view" in held:
            out.append({"id": str(module.id), "name": module.name, "description": module.description,
                        "icon": module.icon, "actions": held})
    return out


@router.get("/custom-modules/{module_id}/records")
def list_module_records(
    module_id: uuid.UUID, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)
):
    module, held = _require_action(db, current_user, module_id, "view")
    records = db.scalars(
        select(models.CustomModuleRecord)
        .where(models.CustomModuleRecord.module_id == module.id)
        .order_by(models.CustomModuleRecord.created_at.desc())
    ).all()
    ids = {e for r in records for e in (r.created_by_employee, r.updated_by_employee, r.decided_by_employee) if e}
    names = crud.employee_display_names_bulk(db, list(ids)) if ids else {}
    return {
        "module": {"id": str(module.id), "name": module.name, "description": module.description, "actions": held},
        "records": [_record_out(db, r, names) for r in records],
    }


@router.post("/custom-modules/{module_id}/records", status_code=201)
def create_module_record(
    module_id: uuid.UUID, payload: RecordIn,
    db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    module, _held = _require_action(db, current_user, module_id, "create")
    now = datetime.datetime.now(datetime.timezone.utc)
    record = models.CustomModuleRecord(
        id=uuid.uuid4(), module_id=module.id, company_id=current_user.company_id,
        title=payload.title.strip(), details=(payload.details or "").strip() or None, status="pending",
        created_by_employee=current_user.employee_id, created_at=now,
    )
    db.add(record)
    _audit(db, current_user, "create", "custom_module_record", record.id, {"module": module.name, "title": record.title})
    db.commit()
    return _record_out(db, record)


def _get_record(db: Session, module: models.CustomModule, record_id: uuid.UUID) -> models.CustomModuleRecord:
    record = db.get(models.CustomModuleRecord, record_id)
    if record is None or record.module_id != module.id:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


@router.patch("/custom-modules/{module_id}/records/{record_id}")
def update_module_record(
    module_id: uuid.UUID, record_id: uuid.UUID, payload: RecordUpdate,
    db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    module, _held = _require_action(db, current_user, module_id, "edit")
    record = _get_record(db, module, record_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("title") is not None:
        record.title = changes["title"].strip()
    if "details" in changes:
        record.details = (changes["details"] or "").strip() or None
    record.updated_by_employee = current_user.employee_id
    record.updated_at = datetime.datetime.now(datetime.timezone.utc)
    _audit(db, current_user, "update", "custom_module_record", record.id, changes)
    db.commit()
    return _record_out(db, record)


@router.post("/custom-modules/{module_id}/records/{record_id}/decision")
def decide_module_record(
    module_id: uuid.UUID, record_id: uuid.UUID, payload: RecordDecision,
    db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    module, _held = _require_action(db, current_user, module_id, "approve")
    record = _get_record(db, module, record_id)
    before = record.status
    record.status = payload.status
    record.decided_by_employee = current_user.employee_id if payload.status != "pending" else None
    record.decided_at = datetime.datetime.now(datetime.timezone.utc) if payload.status != "pending" else None
    _audit(db, current_user, payload.status, "custom_module_record", record.id, {"before": before, "after": payload.status})
    db.commit()
    return _record_out(db, record)


@router.delete("/custom-modules/{module_id}/records/{record_id}", status_code=204)
def delete_module_record(
    module_id: uuid.UUID, record_id: uuid.UUID,
    db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    module, _held = _require_action(db, current_user, module_id, "delete")
    record = _get_record(db, module, record_id)
    db.delete(record)
    _audit(db, current_user, "delete", "custom_module_record", record_id, {"title": record.title})
    db.commit()
    return Response(status_code=204)


@router.get("/custom-modules/{module_id}/records/export")
def export_module_records(
    module_id: uuid.UUID, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)
):
    module, _held = _require_action(db, current_user, module_id, "export")
    records = db.scalars(
        select(models.CustomModuleRecord)
        .where(models.CustomModuleRecord.module_id == module.id)
        .order_by(models.CustomModuleRecord.created_at.desc())
    ).all()
    def when(value) -> str:
        return value.strftime("%Y-%m-%d %H:%M") if value else ""

    def cell(value) -> str:
        # Spreadsheet formula-injection guard: = + - @ at the start of a
        # cell would run as a formula in Excel, so it is shown as text.
        text = "" if value is None else str(value)
        if text[:1] in ("=", "+", "-", "@") and not re.fullmatch(r"[+-]?[\d\s().,-]+", text):
            return f"'{text}"
        return text

    # BOM so Excel opens it as UTF-8 (names, ₹); CRLF rows (csv default).
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf)
    writer.writerow(["Title", "Details", "Status", "Created by", "Created at", "Decided by", "Decided at"])
    for r in records:
        o = _record_out(db, r)
        writer.writerow([cell(o["title"]), cell(o["details"]), cell((o["status"] or "").replace("_", " ").title()),
                         cell(o["created_by"]), when(o["created_at"]),
                         cell(o["decided_by"]), when(o["decided_at"])])
    _audit(db, current_user, "export", "custom_module", module.id, {"rows": len(records)})
    db.commit()
    safe = "".join(ch if ch.isalnum() else "_" for ch in module.name)[:40] or "module"
    return Response(content=buf.getvalue().encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'})
