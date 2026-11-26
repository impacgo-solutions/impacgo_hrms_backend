"""Administration > Roles & Permissions > Employee Permissions.

Per-employee access levels that replace the employee's role for a module
(see app/employee_permissions.py for what each level means and where it is
enforced).

  GET    /api/employee-permissions/catalog          modules + levels
  GET    /api/employee-permissions                  employees with custom levels
  GET    /api/employee-permissions/{employee_id}    one employee: role vs set vs effective
  PUT    /api/employee-permissions/{employee_id}    {"permissions": {module: level | null}}
  PUT    /api/employee-permissions/bulk             {"employee_ids": [...], "permissions": {...}} -> {updated, failed}
  DELETE /api/employee-permissions/{employee_id}    back to the role for every module

Managed by the Owner or anyone with Edit/Admin on System Settings / RBAC,
within their own company. A non-Owner can grant a level only up to their
own level on that module. Every change is written to the audit log."""

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import crud, employee_permissions as ep, models, role_tiers
from ..database import get_db
from ..deps import require_permission
from ..rbac_columns import BUILTIN_ROLES, LEVEL_RANK

router = APIRouter(prefix="/api/employee-permissions", tags=["employee-permissions"])
_ADMIN = require_permission("system_settings_rbac")
_LEVEL_FROM_CHAR = {"n": "none", "s": "self", "v": "view", "e": "edit", "a": "admin"}


def _require_table(db: Session) -> None:
    if not ep.table_present(db):
        raise HTTPException(status_code=404, detail="Employee Permissions isn't available for this organization yet.")


def _employee(db: Session, company_id: uuid.UUID, employee_id: uuid.UUID) -> models.Employee:
    emp = db.get(models.Employee, employee_id)
    if emp is None or emp.company_id != company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp


def _user_for(db: Session, employee_id: uuid.UUID) -> models.User | None:
    return db.scalar(select(models.User).where(models.User.employee_id == employee_id))


def _is_owner(db: Session, user: models.User | None) -> bool:
    return user is not None and any(r.name == BUILTIN_ROLES[0] for r in crud.get_user_roles(db, user.id))


def _role_level(db: Session, user: models.User | None, module: str) -> str | None:
    """What the employee's role (+ Employee Module Access grants) gives for
    the module, before Employee Permissions -- None where the module has no
    matrix column (its access is decided by other role rules)."""
    if user is None:
        return "none"
    if _is_owner(db, user):
        return "admin"
    columns = ep.MODULES[module][1]
    if not columns:
        return None
    role = crud.get_user_primary_role(db, user.id)
    matrix = dict(crud.effective_role_matrix(role)) if role is not None else {}
    for column, level in crud.grant_matrix_levels(db, user.employee_id).items():
        if LEVEL_RANK[level] > LEVEL_RANK.get(matrix.get(column, "n"), 0):
            matrix[column] = level
    best = max((matrix.get(c, "n") for c in columns), key=lambda x: LEVEL_RANK.get(x, 0))
    return _LEVEL_FROM_CHAR.get(best, "none")


def _detail(db: Session, emp: models.Employee) -> dict:
    user = _user_for(db, emp.id)
    role = crud.get_user_primary_role(db, user.id) if user is not None else None
    rows = {p.module: p for p in db.scalars(select(models.EmployeePermission)
                                             .where(models.EmployeePermission.employee_id == emp.id))}
    modules = []
    enabled = set(crud.get_enabled_module_keys(db, emp.company_id))
    for key, (label, _cols, _res) in ep.MODULES.items():
        row = rows.get(key)
        role_level = _role_level(db, user, key)
        modules.append({
            "key": key, "label": label, "enabled": key in enabled or key in ("people", "approvals", "admin", "organization"),
            "role_level": role_level,
            "level": row.level if row else None,
            "effective": row.level if row else role_level,
            "updated_at": row.updated_at or row.created_at if row else None,
        })
    return {
        "employee_id": str(emp.id), "employee_name": crud._full_name(emp), "employee_code": emp.employee_code,
        "role": role.name if role else None, "has_login": user is not None, "is_owner": _is_owner(db, user),
        "modules": modules,
    }


@router.get("/catalog")
def catalog(db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)):
    return {
        "levels": [{"key": k, "label": ep.LEVEL_LABELS[k]} for k in ep.LEVELS],
        "modules": [{"key": k, "label": label} for k, (label, _c, _r) in ep.MODULES.items()],
        "available": ep.table_present(db),
    }


@router.get("")
def list_customised(db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)):
    if not ep.table_present(db):
        return []
    rows = db.execute(
        select(models.EmployeePermission, models.Employee)
        .join(models.Employee, models.Employee.id == models.EmployeePermission.employee_id)
        .where(models.Employee.company_id == current_user.company_id)
        .order_by(models.Employee.first_name, models.EmployeePermission.module)
    ).all()
    out: dict[uuid.UUID, dict] = {}
    for perm, emp in rows:
        entry = out.setdefault(emp.id, {"employee_id": str(emp.id), "employee_name": crud._full_name(emp),
                                        "employee_code": emp.employee_code, "permissions": {}, "updated_at": None})
        entry["permissions"][perm.module] = perm.level
        stamp = perm.updated_at or perm.created_at
        if stamp and (entry["updated_at"] is None or stamp > entry["updated_at"]):
            entry["updated_at"] = stamp
    return list(out.values())


@router.get("/{employee_id}")
def get_employee(employee_id: uuid.UUID, db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)):
    _require_table(db)
    return _detail(db, _employee(db, current_user.company_id, employee_id))


class PermissionsUpdate(BaseModel):
    # module -> level, or null to go back to the role for that module
    permissions: dict[str, str | None] = Field(default_factory=dict)


def _grant_ceiling_error(db: Session, actor: models.User, module: str, level: str) -> str | None:
    """A non-Owner may grant up to their own level on the module."""
    if _is_owner(db, actor):
        return None
    mine = crud.effective_user_matrix(db, actor)
    columns = ep.MODULES[module][1]
    wanted = ep.matrix_level(level)
    if columns:
        own = max((mine.get(c, "n") for c in columns), key=lambda x: LEVEL_RANK.get(x, 0))
        if LEVEL_RANK.get(wanted, 0) > LEVEL_RANK.get(own, 0):
            return (f"You can grant {ep.MODULES[module][0]} up to your own level "
                    f"({ep.LEVEL_LABELS[_LEVEL_FROM_CHAR.get(own, 'none')]}), not {ep.LEVEL_LABELS[level]}.")
        return None
    # Modules without a matrix column: Admin needs System Settings / RBAC Admin.
    if level == "admin" and mine.get("system_settings_rbac") != "a":
        return f"Only the Owner or a System Settings / RBAC Admin can give Admin on {ep.MODULES[module][0]}."
    return None


def _forbid_self(db: Session, actor: models.User, emp: models.Employee) -> None:
    """A non-Owner can't edit or reset their OWN Employee Permissions -- a
    reset/clear would lift a restriction the Owner placed on them, and a
    level change would re-derive their granular actions."""
    if emp.id == actor.employee_id and not _is_owner(db, actor):
        raise HTTPException(status_code=403, detail=role_tiers.SELF_GRANT_ERROR)


def _validate_permissions(db: Session, actor: models.User, permissions: dict[str, str | None]) -> None:
    """Request-level checks shared by the single and bulk updates."""
    unknown = [m for m in permissions if m not in ep.MODULES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown module(s): {', '.join(unknown)}")
    bad = [f"{m}={v}" for m, v in permissions.items() if v is not None and v not in ep.LEVELS]
    if bad:
        raise HTTPException(status_code=422, detail=f"Unknown level(s): {', '.join(bad)}")
    for module, level in permissions.items():
        if level is not None:
            error = _grant_ceiling_error(db, actor, module, level)
            if error:
                raise HTTPException(status_code=403, detail=error)


def _apply_permissions(db: Session, actor: models.User, emp: models.Employee,
                       permissions: dict[str, str | None], now: datetime.datetime,
                       rows: dict[str, models.EmployeePermission] | None = None,
                       to_delete: list[uuid.UUID] | None = None) -> None:
    """Write one employee's changes and its audit row. Without `rows` (single
    update) it locks, loads and flushes for this employee alone; the bulk path
    passes the employee's pre-loaded rows (already locked) and flushes once."""
    bulk = rows is not None
    if not bulk:
        crud._advisory_lock(db, "employee_permissions", str(emp.id))
        rows = {p.module: p for p in db.scalars(select(models.EmployeePermission)
                                                 .where(models.EmployeePermission.employee_id == emp.id))}
    before = {m: r.level for m, r in rows.items()}
    after = dict(before)
    for module, level in permissions.items():
        row = rows.get(module)
        if level is None:
            if row is not None:
                if to_delete is not None:  # bulk: removed in one statement by the caller
                    to_delete.append(row.id)
                else:
                    db.delete(row)
                after.pop(module, None)
        elif row is None:
            db.add(models.EmployeePermission(
                id=uuid.uuid4(), company_id=emp.company_id, employee_id=emp.id, module=module, level=level,
                created_by=actor.id, created_at=now, updated_by=actor.id, updated_at=now))
            after[module] = level
        elif row.level != level:
            row.level, row.updated_by, row.updated_at = level, actor.id, now
            after[module] = level
    if not bulk:
        db.flush()
        after = {p.module: p.level for p in db.scalars(select(models.EmployeePermission)
                                                       .where(models.EmployeePermission.employee_id == emp.id))}
    crud.create_audit_log(db, actor.company_id, actor.id, "update", "employee_permissions", emp.id,
                          changes={"employee": crud._full_name(emp), "before": before, "after": after},
                          flush=not bulk)


class BulkPermissionsUpdate(BaseModel):
    employee_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    # module -> level, or null to go back to the role for that module
    permissions: dict[str, str | None] = Field(default_factory=dict)


# Declared before "/{employee_id}" so "bulk" is never read as an employee id.
@router.put("/bulk")
def update_employees_bulk(
    payload: BulkPermissionsUpdate,
    db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN),
):
    """Apply the same changes to many employees in ONE request / transaction.
    Employees that can't be changed (not found, the Owner, yourself) are
    skipped and reported in `failed`; everyone else is updated together."""
    _require_table(db)
    _validate_permissions(db, current_user, payload.permissions)
    now = datetime.datetime.now(datetime.timezone.utc)
    # Stable order -> consistent advisory-lock order across concurrent requests.
    ids = sorted(set(payload.employee_ids), key=str)
    # A handful of set-based queries instead of several per employee.
    employees = {e.id: e for e in db.scalars(select(models.Employee).where(
        models.Employee.id.in_(ids), models.Employee.company_id == current_user.company_id))}
    owner_employee_ids = set(db.scalars(
        select(models.User.employee_id)
        .join(models.UserRole, models.UserRole.user_id == models.User.id)
        .join(models.Role, models.Role.id == models.UserRole.role_id)
        .where(models.User.employee_id.in_(list(employees)), models.Role.name == BUILTIN_ROLES[0])))
    actor_is_owner = _is_owner(db, current_user)
    found = [i for i in ids if i in employees]
    if found:  # one statement takes every per-employee lock, in order
        db.execute(select(*[func.pg_advisory_xact_lock(func.hashtext(f"employee_permissions:{i}")) for i in found]))
    existing: dict[uuid.UUID, dict[str, models.EmployeePermission]] = {i: {} for i in found}
    for row in db.scalars(select(models.EmployeePermission).where(models.EmployeePermission.employee_id.in_(found))):
        existing[row.employee_id][row.module] = row
    failed: list[dict] = []
    to_delete: list[uuid.UUID] = []
    updated = 0
    for employee_id in ids:
        emp = employees.get(employee_id)
        if emp is None:
            failed.append({"employee_id": str(employee_id), "employee_name": None, "detail": "Employee not found"})
        elif emp.id == current_user.employee_id and not actor_is_owner:
            failed.append({"employee_id": str(emp.id), "employee_name": crud._full_name(emp),
                           "detail": role_tiers.SELF_GRANT_ERROR})
        elif emp.id in owner_employee_ids:
            failed.append({"employee_id": str(emp.id), "employee_name": crud._full_name(emp),
                           "detail": "The Organization Owner always has full access and can't be restricted."})
        else:
            _apply_permissions(db, current_user, emp, payload.permissions, now, rows=existing[emp.id], to_delete=to_delete)
            updated += 1
    if to_delete:
        db.execute(delete(models.EmployeePermission).where(models.EmployeePermission.id.in_(to_delete)),
                   execution_options={"synchronize_session": False})
    db.commit()
    crud.clear_rbac_memo(db)
    return {"updated": updated, "failed": failed}


@router.put("/{employee_id}")
def update_employee(
    employee_id: uuid.UUID, payload: PermissionsUpdate,
    db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN),
):
    _require_table(db)
    emp = _employee(db, current_user.company_id, employee_id)
    target_user = _user_for(db, emp.id)
    _forbid_self(db, current_user, emp)
    if _is_owner(db, target_user):
        raise HTTPException(status_code=422, detail="The Organization Owner always has full access and can't be restricted.")
    _validate_permissions(db, current_user, payload.permissions)
    _apply_permissions(db, current_user, emp, payload.permissions, datetime.datetime.now(datetime.timezone.utc))
    db.commit()
    crud.clear_rbac_memo(db)
    return _detail(db, emp)


@router.delete("/{employee_id}", status_code=204)
def reset_employee(employee_id: uuid.UUID, db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)):
    _require_table(db)
    emp = _employee(db, current_user.company_id, employee_id)
    _forbid_self(db, current_user, emp)
    rows = list(db.scalars(select(models.EmployeePermission).where(models.EmployeePermission.employee_id == emp.id)))
    before = {r.module: r.level for r in rows}
    for r in rows:
        db.delete(r)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "delete", "employee_permissions", emp.id,
                          changes={"employee": crud._full_name(emp), "before": before, "after": {}})
    db.commit()
    crud.clear_rbac_memo(db)
    return Response(status_code=204)
