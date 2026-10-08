"""Administration > Roles & Permissions > Employee Permissions.

Per-employee access levels that replace the employee's role for a module
(see app/employee_permissions.py for what each level means and where it is
enforced).

  GET    /api/employee-permissions/catalog          modules + levels
  GET    /api/employee-permissions                  employees with custom levels
  GET    /api/employee-permissions/{employee_id}    one employee: role vs set vs effective
  PUT    /api/employee-permissions/{employee_id}    {"permissions": {module: level | null}}
  DELETE /api/employee-permissions/{employee_id}    back to the role for every module

Managed by the Owner or anyone with Edit/Admin on System Settings / RBAC,
within their own company. A non-Owner can grant a level only up to their
own level on that module. Every change is written to the audit log."""

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
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
    unknown = [m for m in payload.permissions if m not in ep.MODULES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown module(s): {', '.join(unknown)}")
    bad = [f"{m}={v}" for m, v in payload.permissions.items() if v is not None and v not in ep.LEVELS]
    if bad:
        raise HTTPException(status_code=422, detail=f"Unknown level(s): {', '.join(bad)}")
    for module, level in payload.permissions.items():
        if level is not None:
            error = _grant_ceiling_error(db, current_user, module, level)
            if error:
                raise HTTPException(status_code=403, detail=error)
    crud._advisory_lock(db, "employee_permissions", str(emp.id))
    rows = {p.module: p for p in db.scalars(select(models.EmployeePermission)
                                             .where(models.EmployeePermission.employee_id == emp.id))}
    before = {m: r.level for m, r in rows.items()}
    now = datetime.datetime.now(datetime.timezone.utc)
    for module, level in payload.permissions.items():
        row = rows.get(module)
        if level is None:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(models.EmployeePermission(
                id=uuid.uuid4(), company_id=emp.company_id, employee_id=emp.id, module=module, level=level,
                created_by=current_user.id, created_at=now, updated_by=current_user.id, updated_at=now))
        elif row.level != level:
            row.level, row.updated_by, row.updated_at = level, current_user.id, now
    db.flush()
    after = {p.module: p.level for p in db.scalars(select(models.EmployeePermission)
                                                   .where(models.EmployeePermission.employee_id == emp.id))}
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "employee_permissions", emp.id,
                          changes={"employee": crud._full_name(emp), "before": before, "after": after})
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
