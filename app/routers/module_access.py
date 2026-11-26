"""Administration > Roles & Permissions > Employee Module Access.

  * GET /api/module-access/catalog -- every HRMS module, derived at request
    time (no hand-kept list): this company's enabled modules
    (public.modules + core_company_modules), each module's catalog actions
    (public.permission_actions), the RBAC matrix columns that gate it
    (rbac_columns), and its functional areas taken from the application's
    real API routes (grouped by the router that serves them). Custom
    modules are listed too (they're assigned in their own section). Routes
    that belong to no grantable module are reported under
    "core" so nothing is silently left out.
  * Grants: extra access to one built-in module for selected employees, on
    top of their role (additive -- roles, defaults and workflows are
    unchanged). Stored in core_employee_access_grants; enforced through
    crud.user_has_action / crud.effective_user_matrix, i.e. by the same
    checks every API and /api/auth/me (navigation) already use.
"""

import datetime
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import crud, models, role_tiers
from ..database import get_db
from ..deps import require_permission
from ..rbac_columns import RBAC_COLUMNS

router = APIRouter(prefix="/api/module-access", tags=["module-access"])
_ADMIN = require_permission("system_settings_rbac")

# Which module a router's endpoints belong to. Router modules not listed
# here are reported under "core" (self-service / platform plumbing).
_ROUTER_MODULE = {
    "attendance": "attendance", "leave": "leave", "payroll": "payroll", "recruitment": "recruitment",
    "performance": "performance", "learning": "learning", "benefits": "benefits", "assets": "assets",
    "documents": "documents", "projects": "projects", "work": "work", "travel": "travel",
    "reports": "reports", "organization": "organization", "designations": "organization",
    "bands": "organization", "employees": "people", "approval_inbox": "approvals",
    "approvals_config": "admin", "settings": "admin", "config": "admin", "modules": "admin",
    "roles": "admin", "permissions": "admin", "user_roles": "admin", "audit": "admin",
    "custom_modules": "admin", "module_access": "admin",
}
_NOT_TENANT = {"super_admin"}  # platform console, not part of a tenant's HRMS


def _area_of(path: str) -> str:
    parts = [p for p in path.split("/") if p and not p.startswith("{")]
    if parts and parts[0] == "api":
        parts = parts[1:]
    if not parts:
        return "general"
    head = parts[0]
    # /api/employees/<area>, /api/config/<area>, /api/payroll/<area>, ... -> the sub-area
    if head in ("employees", "config", "payroll", "attendance", "organization", "settings", "approvals",
                "me", "reports", "dashboard", "roles", "performance") and len(parts) > 1:
        return f"{head}/{parts[1]}"
    return head


def _humanize(area: str) -> str:
    parts = area.split("/")
    label = re.sub(r"[-_]+", " ", parts[-1]).strip().title() or "General"
    if len(parts) > 1 and parts[0] == "employees":
        return f"Employee › {label}"  # an employee-profile tab / people sub-area
    return label


def _route_areas(request: Request) -> tuple[dict[str, dict[str, set]], dict[str, set]]:
    """{module_key: {area: {"GET /path", ...}}} from the running app's routes."""
    by_module: dict[str, dict[str, set]] = {}
    core: dict[str, set] = {}
    for route in request.app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        router_name = (route.endpoint.__module__ or "").rsplit(".", 1)[-1]
        if router_name in _NOT_TENANT:
            continue
        area = _area_of(route.path)
        entries = {f"{m} {route.path}" for m in route.methods or [] if m not in ("HEAD", "OPTIONS")}
        module_key = "dashboard" if area.startswith("dashboard") else _ROUTER_MODULE.get(router_name)
        if module_key is None:
            core.setdefault(area, set()).update(entries)
        else:
            by_module.setdefault(module_key, {}).setdefault(area, set()).update(entries)
    return by_module, core


def _table_present(db: Session) -> bool:
    return db.execute(text("SELECT to_regclass('core_employee_access_grants')")).scalar() is not None


def _catalog_actions(db: Session) -> dict[str, dict]:
    """{resource: {"module": key, "actions": [..], "labels": {action: label}}}"""
    out: dict[str, dict] = {}
    for pa in crud.list_permission_actions(db):
        entry = out.setdefault(pa.resource, {"module": pa.module.key if pa.module else None, "actions": [], "labels": {}})
        entry["actions"].append(pa.action)
        entry["labels"][pa.action] = pa.label
    return out


@router.get("/catalog")
def module_catalog(request: Request, db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)):
    enabled = set(crud.get_enabled_module_keys(db, current_user.company_id))
    modules = db.scalars(select(models.Module).order_by(models.Module.sort_order)).all()
    catalog = _catalog_actions(db)
    areas, core = _route_areas(request)
    columns_by_module: dict[str, list[dict]] = {}
    for key, label, module_key in RBAC_COLUMNS:
        columns_by_module.setdefault(module_key, []).append({"key": key, "label": label})
    out = []
    for m in modules:
        resources = [
            {"resource": res, "actions": [{"action": a, "label": info["labels"].get(a)} for a in info["actions"]]}
            for res, info in catalog.items() if info["module"] == m.key
        ]
        route_areas = areas.get(m.key, {})
        if not resources and not route_areas:
            continue  # another product line's module with nothing in this app (e.g. retail)
        out.append({
            "key": m.key,
            "name": m.name,
            "enabled": m.key in enabled,
            "in_navigation": True,  # public.modules keys are the sidebar ids 1:1
            "resources": resources,
            "grantable": bool(resources),
            "rbac_columns": columns_by_module.get(m.key, []),
            "areas": sorted(
                [{"key": a, "name": _humanize(a), "endpoints": len(e)} for a, e in route_areas.items()],
                key=lambda x: x["name"],
            ),
        })
    known = {m["key"] for m in out}
    for module_key, route_areas in areas.items():  # a module the routes use but public.modules lacks
        if module_key not in known:
            out.append({"key": module_key, "name": module_key.title(), "enabled": True, "in_navigation": False,
                        "resources": [], "grantable": False, "rbac_columns": columns_by_module.get(module_key, []),
                        "areas": sorted([{"key": a, "name": _humanize(a), "endpoints": len(e)} for a, e in route_areas.items()],
                                        key=lambda x: x["name"])})
    custom = []
    if db.execute(text("SELECT to_regclass('core_custom_modules')")).scalar() is not None:
        custom = [{"id": str(c.id), "name": c.name, "actions": list(c.actions or []), "is_active": c.is_active}
                  for c in db.scalars(select(models.CustomModule).where(
                      models.CustomModule.company_id == current_user.company_id).order_by(models.CustomModule.name))]
    return {
        "modules": out,
        "custom_modules": custom,
        "core": sorted([{"key": a, "name": _humanize(a), "endpoints": len(e)} for a, e in core.items()],
                       key=lambda x: x["name"]),
        "grants_available": _table_present(db),
    }


class GrantIn(BaseModel):
    employee_id: uuid.UUID
    actions: list[str] = Field(default_factory=lambda: ["view"])


class GrantsReplace(BaseModel):
    assignments: list[GrantIn]


def _grants_out(db: Session, company_id: uuid.UUID, resource: str | None = None) -> list[dict]:
    q = (select(models.EmployeeAccessGrant, models.Employee)
         .join(models.Employee, models.Employee.id == models.EmployeeAccessGrant.employee_id)
         .where(models.Employee.company_id == company_id))
    if resource:
        q = q.where(models.EmployeeAccessGrant.resource == resource)
    catalog = _catalog_actions(db)
    rows = db.execute(q.order_by(models.EmployeeAccessGrant.resource, models.Employee.first_name)).all()
    return [{
        "resource": g.resource,
        "module": (catalog.get(g.resource) or {}).get("module"),
        "employee_id": str(e.id),
        "employee_name": f"{e.first_name} {e.last_name or ''}".strip(),
        "actions": [a for a in (catalog.get(g.resource) or {}).get("actions", []) if a in (g.actions or [])],
        "granted_at": g.granted_at,
        "updated_at": g.updated_at,
    } for g, e in rows]


@router.get("/grants")
def list_grants(db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)):
    if not _table_present(db):
        return []
    return _grants_out(db, current_user.company_id)


def _check_grant_changes(
    db: Session, actor: models.User, resource: str,
    existing: dict[uuid.UUID, "models.EmployeeAccessGrant"], wanted: dict[uuid.UUID, list[str]],
) -> None:
    """403 unless every CHANGED grant is within the actor's own access
    (role_tiers.grant_ceiling_error -- the Employee Permissions ceiling) and
    none of the changes add to the actor's own access. Rows left exactly as
    they are never block a save, so editing others' grants on a module with
    unrelated existing rows still works."""
    is_owner = role_tiers.is_owner(db, actor.id)
    changed: set[str] = set()
    for emp_id in set(existing) | set(wanted):
        before = set(existing[emp_id].actions or []) if emp_id in existing else set()
        after = set(wanted.get(emp_id, []))
        if before == after:
            continue
        if not is_owner and emp_id == actor.employee_id and after - before:
            raise HTTPException(status_code=403, detail=role_tiers.SELF_GRANT_ERROR)
        changed |= before ^ after
    error = role_tiers.grant_ceiling_error(db, actor, resource, changed)
    if error:
        raise HTTPException(status_code=403, detail=error)


@router.put("/grants/{resource}")
def replace_grants(
    resource: str, payload: GrantsReplace,
    db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN),
):
    """Full replace of who holds individual access to `resource`."""
    if not _table_present(db):
        raise HTTPException(status_code=404, detail="Employee module access is not available for this organization yet.")
    catalog = _catalog_actions(db)
    info = catalog.get(resource)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Unknown module resource '{resource}'")
    ids = [a.employee_id for a in payload.assignments]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="An employee can only be listed once")
    if ids:
        found = set(db.scalars(select(models.Employee.id).where(
            models.Employee.company_id == current_user.company_id, models.Employee.id.in_(ids))))
        if set(ids) - found:
            raise HTTPException(status_code=422, detail="One or more selected employees don't belong to this organization")
    wanted: dict[uuid.UUID, list[str]] = {}
    for a in payload.assignments:
        acts = {x.strip().lower() for x in a.actions if x and x.strip()}
        unknown = acts - set(info["actions"])
        if unknown:
            raise HTTPException(status_code=422, detail=f"Action(s) {', '.join(sorted(unknown))} don't exist for {resource}")
        if "view" in info["actions"]:
            acts.add("view")
        if not acts:
            raise HTTPException(status_code=422, detail="Select at least one action")
        wanted[a.employee_id] = [x for x in info["actions"] if x in acts]
    now = datetime.datetime.now(datetime.timezone.utc)
    existing = {g.employee_id: g for g in db.scalars(
        select(models.EmployeeAccessGrant)
        .join(models.Employee, models.Employee.id == models.EmployeeAccessGrant.employee_id)
        .where(models.EmployeeAccessGrant.resource == resource, models.Employee.company_id == current_user.company_id))}
    _check_grant_changes(db, current_user, resource, existing, wanted)
    for emp_id, row in existing.items():
        if emp_id not in wanted:
            db.delete(row)
    for emp_id, acts in wanted.items():
        row = existing.get(emp_id)
        if row is None:
            db.add(models.EmployeeAccessGrant(id=uuid.uuid4(), employee_id=emp_id, resource=resource,
                                              actions=acts, granted_by=current_user.id, granted_at=now))
        elif list(row.actions or []) != acts:
            row.actions = acts
            row.updated_at = now
    try:
        crud.create_audit_log(db, current_user.company_id, current_user.id, "assign", "employee_module_access", None,
                              changes={"resource": resource,
                                       "assignments": [{"employee_id": str(k), "actions": v} for k, v in wanted.items()]})
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    crud.clear_rbac_memo(db)
    return _grants_out(db, current_user.company_id, resource)


@router.delete("/grants/{resource}/{employee_id}", status_code=204)
def remove_grant(
    resource: str, employee_id: uuid.UUID,
    db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN),
):
    if not _table_present(db):
        raise HTTPException(status_code=404, detail="Not found")
    row = db.scalar(
        select(models.EmployeeAccessGrant)
        .join(models.Employee, models.Employee.id == models.EmployeeAccessGrant.employee_id)
        .where(models.EmployeeAccessGrant.resource == resource,
               models.EmployeeAccessGrant.employee_id == employee_id,
               models.Employee.company_id == current_user.company_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Grant not found")
    _check_grant_changes(db, current_user, resource, {row.employee_id: row}, {})
    db.delete(row)
    try:
        crud.create_audit_log(db, current_user.company_id, current_user.id, "revoke", "employee_module_access", None,
                              changes={"resource": resource, "employee_id": str(employee_id)})
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return Response(status_code=204)
