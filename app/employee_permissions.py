"""Employee Permissions -- per-employee access levels set by an admin in
Administration > Roles & Permissions > Employee Permissions.

One row per (employee, module) in core_employee_permissions
(backend/db/add_employee_permissions.sql). When a row exists it REPLACES
what the employee's role gives for that module -- raising or lowering it,
independent of role, designation, department or title. No row = the role
(and any Employee Module Access grants) apply exactly as before.

Levels (cumulative except Approve):
  none     No Access -- the module is hidden and every API refuses
  self     Self Only -- only the employee's own records
  view     View      -- view the records the module shows (all employees)
  create   Create    -- view + create
  edit     Edit      -- view + create + edit / assign
  delete   Delete    -- view + create + edit + delete
  approve  Approve   -- view + approve / reject requests in the module
  admin    Admin     -- everything the module offers

Enforced where authorization is already decided (crud.effective_user_matrix,
crud.user_has_action, crud.can_access_people_module, the record-visibility
helpers, deps.get_payroll_scope, approval decisions, /media), so the UI,
routes, APIs and data scoping all follow it."""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

LEVELS = ("none", "self", "view", "create", "edit", "delete", "approve", "admin")
LEVEL_LABELS = {
    "none": "No Access", "self": "Self Only", "view": "View", "create": "Create",
    "edit": "Edit", "delete": "Delete", "approve": "Approve", "admin": "Admin",
}

# module key (= sidebar / nav id) -> (label, RBAC matrix columns, catalog resources)
MODULES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "people": ("Employee Management (People)", (), ("employee",)),
    "attendance": ("Attendance", ("team_attendance",), ("attendance",)),
    "leave": ("Leave", ("leave_approval",), ("leave",)),
    "work": ("Work & Timesheet", ("timesheet_approval",), ("work",)),
    "payroll": ("Payroll", ("payroll_view_own", "payroll_process"), ("payroll",)),
    "travel": ("Travel & Expenses", ("travel_expense_approval",), ("travel",)),
    "documents": ("Documents", (), ("documents",)),
    "approvals": ("Approvals", (), ("approvals",)),
    "reports": ("Reports & Analytics", ("reports_analytics",), ("reports",)),
    "recruitment": ("Recruitment", ("recruitment",), ("recruitment",)),
    "performance": ("Performance", ("performance_reviews",), ("performance",)),
    "learning": ("Learning", (), ("learning",)),
    "benefits": ("Benefits & Perks", ("benefits_admin",), ("benefits",)),
    "projects": ("Projects", ("projects_access",), ("projects",)),
    "assets": ("Assets", ("asset_management",), ("assets",)),
    "organization": ("Organization", ("org_structure_config",), ("organization",)),
    "admin": ("Administration", ("system_settings_rbac", "audit_logs"), ("admin", "config")),
}

# matrix character each level maps to (n / s / v / e / a)
_MATRIX_LEVEL = {"none": "n", "self": "s", "view": "v", "create": "e", "edit": "e",
                 "delete": "e", "approve": "e", "admin": "a"}
# catalog actions each level allows (intersected with the module's real actions)
_LEVEL_ACTIONS = {
    "none": set(),
    "self": set(),
    "view": {"view", "export"},
    "create": {"view", "export", "create"},
    "edit": {"view", "export", "create", "edit", "assign", "generate", "import"},
    "delete": {"view", "export", "create", "edit", "assign", "generate", "import", "delete"},
    "approve": {"view", "export", "approve", "reject"},
}

# request doctype -> module (record visibility of request lists)
DOCTYPE_MODULE = {
    "leave_request": "leave", "attendance_regularization": "attendance", "overtime_request": "attendance",
    "travel_request": "travel", "expense_claim": "travel", "timesheet": "work",
    "hiring_requisition": "recruitment", "salary_revision_request": "payroll", "asset_request": "assets",
}

COLUMN_MODULE = {col: key for key, (_l, cols, _r) in MODULES.items() for col in cols}
RESOURCE_MODULE = {res: key for key, (_l, _c, resources) in MODULES.items() for res in resources}


def matrix_level(level: str) -> str:
    return _MATRIX_LEVEL.get(level, "n")


def actions_for(level: str, catalog_actions: set[str]) -> set[str]:
    """Catalog actions `level` allows for a resource offering `catalog_actions`."""
    if level == "admin":
        return set(catalog_actions)
    return _LEVEL_ACTIONS.get(level, set()) & set(catalog_actions)


def table_present(db: Session) -> bool:
    from . import crud

    memo = crud._rbac_memo(db)
    if "employee_permissions_table" not in memo:
        memo["employee_permissions_table"] = db.execute(
            text("SELECT to_regclass('core_employee_permissions')")
        ).scalar() is not None
    return memo["employee_permissions_table"]


def for_employee(db: Session, employee_id: uuid.UUID | None) -> dict[str, str]:
    """{module: level} set for this employee (memoized per request)."""
    if employee_id is None or not table_present(db):
        return {}
    from . import crud, models

    memo = crud._rbac_memo(db)
    key = ("employee_permissions", employee_id)
    if key not in memo:
        memo[key] = {
            m: lvl for m, lvl in db.execute(
                select(models.EmployeePermission.module, models.EmployeePermission.level)
                .where(models.EmployeePermission.employee_id == employee_id)
            ).all()
            if m in MODULES and lvl in LEVELS
        }
    return memo[key]


def level_for(db: Session, employee_id: uuid.UUID | None, module: str | None) -> str | None:
    if module is None:
        return None
    return for_employee(db, employee_id).get(module)


def apply_to_matrix(matrix: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    """Replace every column of an overridden module with the override level."""
    for module, level in overrides.items():
        for column in MODULES[module][1]:
            matrix[column] = matrix_level(level)
    return matrix


def resource_allows(db: Session, employee_id: uuid.UUID | None, resource: str, action: str) -> bool | None:
    """True / False when an override decides (resource, action); None = no
    override for that resource's module (role and grants decide)."""
    module = RESOURCE_MODULE.get(resource)
    level = level_for(db, employee_id, module)
    if level is None:
        return None
    from . import crud

    catalog = {pa.action for pa in crud.list_permission_actions(db) if pa.resource == resource}
    return action in actions_for(level, catalog)


def granted_action_strings(db: Session, employee_id: uuid.UUID | None) -> tuple[set[str], set[str]]:
    """(resource.action strings the overrides allow, resources they decide)."""
    overrides = for_employee(db, employee_id)
    if not overrides:
        return set(), set()
    from . import crud

    by_resource: dict[str, set[str]] = {}
    for pa in crud.list_permission_actions(db):
        by_resource.setdefault(pa.resource, set()).add(pa.action)
    allowed: set[str] = set()
    decided: set[str] = set()
    for module, level in overrides.items():
        for resource in MODULES[module][2]:
            decided.add(resource)
            allowed.update(f"{resource}.{a}" for a in actions_for(level, by_resource.get(resource, set())))
    return allowed, decided


def sees_all_records(level: str | None) -> bool | None:
    """Record scope an override implies: True = every employee's records,
    False = own records only, None = no override (role decides)."""
    if level is None:
        return None
    return level not in ("none", "self")
