"""Historical/reference only -- superseded by the metadata-driven
public.permission_actions catalog (see backend/db/
add_permission_actions_catalog.sql, models.PermissionAction,
crud.list_permission_actions). No runtime code imports
GRANULAR_PERMISSIONS/GRANULAR_RESOURCES/GRANULAR_ACTION_SET from this file
anymore -- crud.apply_actions_update/build_role_actions/the /api/
permission-actions endpoint all read the live DB catalog instead, so a new
module's actions only need a DB insert, never a code change. Kept here
only as a record of the original (7-resource, pre-real-module-catalog)
seed values this module's DB replacement was seeded from.

Granular, module-grouped (resource, action) permission catalog -- additive
to (not a replacement for) rbac_columns.py's existing 16-column v/s/e/a
matrix, which stays running exactly as-is. Where the old matrix answers
"how much access does this role have to this area" with one of 4 ordinal
levels, this catalog answers "can this role do this specific thing" with an
independent yes/no per (resource, action) pair -- multiple actions on the
same resource can be granted at once (e.g. a role can have employee.create
and employee.transfer without employee.delete), which the old single-level-
per-column model can't express.

Stored as ordinary core.permissions rows (code = "{resource}.{action}"),
granted via ordinary core.role_permissions rows -- same two tables the old
matrix already uses. The two catalogs never collide: every action string
here is a full word (e.g. "create", "transfer"), while the old matrix only
ever uses the single characters "v"/"s"/"e"/"a".
"""

GRANULAR_PERMISSIONS: list[tuple[str, str, str, str]] = [
    # (resource, action, label, module)
    ("employee", "view", "View Employee", "employee"),
    ("employee", "create", "Create Employee", "employee"),
    ("employee", "edit", "Edit Employee", "employee"),
    ("employee", "delete", "Delete Employee", "employee"),
    ("employee", "transfer", "Transfer Employee", "employee"),
    ("employee", "terminate", "Terminate Employee", "employee"),
    ("leave", "view", "View Leave", "leave"),
    ("leave", "approve", "Approve Leave", "leave"),
    ("leave", "reject", "Reject Leave", "leave"),
    ("payroll", "view", "View Payroll", "payroll"),
    ("payroll", "generate", "Generate Payroll", "payroll"),
    ("payroll", "export", "Export Payroll", "payroll"),
    ("projects", "view", "View Projects", "projects"),
    ("projects", "assign", "Assign Projects", "projects"),
    ("projects", "edit", "Edit Projects", "projects"),
    ("finance", "view", "View Finance", "finance"),
    ("finance", "approve", "Approve Finance", "finance"),
    ("finance", "reject", "Reject Finance", "finance"),
    ("reports", "view", "View Reports", "reports"),
    ("reports", "export", "Export Reports", "reports"),
    ("reports", "import", "Import Reports", "reports"),
    ("config", "view", "View Configuration", "config"),
    ("config", "edit", "Edit Configuration", "config"),
]

GRANULAR_RESOURCES: list[str] = sorted({resource for resource, _, _, _ in GRANULAR_PERMISSIONS})
GRANULAR_ACTION_SET: set[str] = {action for _, action, _, _ in GRANULAR_PERMISSIONS}
