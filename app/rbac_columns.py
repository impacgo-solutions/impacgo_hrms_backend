# Ported 1:1 from the Flutter frontend's kRbacCols (lib/data/seed/rbac_seed.dart)
# and RbacLevel enum (lib/models/enums.dart). Order matters — it's the exact
# column order the Administration > Roles & Permissions matrix renders.
#
# key: slug stored as permissions.resource in the DB
# label: exact display string from kRbacCols
# module: which kModuleRbacCols group this column belongs to (metadata only)

RBAC_COLUMNS: list[tuple[str, str, str]] = [
    ("own_profile", "Own Profile", "profile"),
    ("team_attendance", "Team Attendance", "attendance"),
    ("leave_approval", "Leave Approval", "leave"),
    ("timesheet_approval", "Timesheet Approval", "work"),
    ("payroll_view_own", "Payroll (View Own)", "payroll"),
    ("payroll_process", "Payroll (Process)", "payroll"),
    ("benefits_admin", "Benefits Admin", "benefits"),
    ("recruitment", "Recruitment", "recruitment"),
    ("performance_reviews", "Performance Reviews", "performance"),
    ("org_structure_config", "Org Structure Config", "organization"),
    ("reports_analytics", "Reports & Analytics", "reports"),
    ("system_settings_rbac", "System Settings / RBAC", "admin"),
    ("audit_logs", "Audit Logs", "admin"),
    ("travel_expense_approval", "Travel & Expense Approval", "travel"),
    # Added so Projects/Assets stop being visible to every role regardless
    # of permission -- see backend/db/add_projects_assets_rbac_columns.sql
    # for how every EXISTING role is backfilled to avoid regressing today's
    # unrestricted access; DEFAULT_MATRIX below only governs *new* roles
    # created after this change.
    ("projects_access", "Projects", "projects"),
    ("asset_management", "Asset Management", "assets"),
]

COLUMN_KEYS: list[str] = [key for key, _label, _module in RBAC_COLUMNS]
COLUMN_LABELS: dict[str, str] = {key: label for key, label, _module in RBAC_COLUMNS}
COLUMN_MODULES: dict[str, str] = {key: module for key, _label, module in RBAC_COLUMNS}

# RbacLevel enum: n=No Access, v=View, s=Self, e=Edit, a=Admin. 'n' is never
# stored as a permission row — absence of a role_permissions link for a
# column IS "no access", matching the frontend's RbacLevel.n default.
LEVELS: list[str] = ["v", "s", "e", "a"]
LEVEL_LABELS: dict[str, str] = {
    "n": "No Access",
    "v": "View",
    "s": "Self",
    "e": "Edit",
    "a": "Admin",
}

# Ported 1:1 from kRbacMatrix — one comma-separated row per built-in role,
# values aligned to RBAC_COLUMNS order above. Lowercased to match LEVELS.
DEFAULT_MATRIX: dict[str, str] = {
    # Trailing pair on every row below is (projects_access, asset_management)
    # -- new columns as of add_projects_assets_rbac_columns.sql. Levels
    # chosen to match each role's existing ROLE_BLURBS description (e.g. IT
    # / System Admin's blurb already says "...and asset management", which
    # is exactly why it gets 'a' there and 'n' on projects).
    "Organization Owner / CEO": "s,v,v,v,s,v,a,v,v,a,a,a,a,a,a,a",
    "C-Level Executive": "s,v,v,v,s,v,e,v,v,e,a,e,v,e,e,e",
    "VP / Director": "s,v,v,v,s,n,v,v,e,e,a,n,v,n,v,v",
    "General Manager / Sr. Manager": "s,e,e,e,s,n,v,v,e,v,a,n,n,e,e,e",
    "Manager": "s,e,e,e,s,n,n,v,e,n,e,n,n,e,e,e",
    "Team Lead": "s,e,v,e,s,n,n,n,v,n,v,n,n,n,e,v",
    # N-01: Travel & Expense Approval 'v' (read-only, company-wide) so HR can
    # view expense reports; HR's other documented gaps (org-wide leave
    # balances, document upload, exit letters) were the HR-representative
    # narrowing fixed in crud.get_visible_employee_ids_for_docs (M-10).
    "HR / Recruitment Staff": "s,v,v,n,n,e,e,a,e,n,e,n,n,v,e,n",
    "Finance / Payroll Staff": "s,n,n,v,n,a,e,n,n,n,e,n,n,a,n,n",
    "IT / System Admin": "s,n,n,n,n,n,n,n,n,n,v,a,a,n,n,a",
    # Branch Manager: view-only access scoped to their branch.
    # Columns: own_profile, team_attendance, leave_approval, timesheet_approval,
    #          payroll_view_own, payroll_process, benefits_admin, recruitment,
    #          performance_reviews, org_structure_config, reports_analytics,
    #          system_settings_rbac, audit_logs, travel_expense_approval,
    #          projects_access, asset_management
    "Branch Manager": "s,v,v,n,s,n,n,n,v,n,v,n,n,n,v,v",
    # Branch Head: oversees every Branch Manager company-wide -- cross-branch
    # visibility/approvals into org structure and reporting, but no platform
    # administration (that stays with IT / System Admin and Organization
    # Owner / CEO).
    "Branch Head": "s,v,v,e,s,n,n,n,v,v,a,n,n,e,v,v",
    # Project Manager: approve requests for their project team; view-only on
    # everything else. Separate from Reporting Manager — if the same person
    # holds both, permissions combine without changing existing RM logic.
    # Full Projects admin matches the role's whole purpose; assets view-only.
    "Project Manager": "s,v,e,e,s,n,n,n,v,n,v,n,n,e,a,v",
    "Professional / IC Employee": "s,s,n,s,s,n,n,n,s,n,n,n,n,n,s,s",
    "Associate / Intern": "s,s,n,s,s,n,n,n,s,n,n,n,n,n,s,s",
}

# Ported 1:1 from kRoleBlurbs — used as each seeded role's description.
ROLE_BLURBS: dict[str, str] = {
    "Organization Owner / CEO": "Full visibility and control across every module, branch and approval chain.",
    "C-Level Executive": "Company-wide visibility with edit rights over org structure, benefits and platform settings.",
    "VP / Director": "Company-wide visibility across your function, with edit rights on performance and org structure.",
    "General Manager / Sr. Manager": "Your reporting line only — attendance, leave and timesheet approvals for your team.",
    "Manager": "Your direct reports only — day-to-day approvals, no org-wide administration.",
    "Team Lead": "Your squad only — first-line attendance & timesheet review, view-only on leave and org data.",
    "HR / Recruitment Staff": "Full People, Recruitment and Benefits administration; no org-structure or platform settings access.",
    "Finance / Payroll Staff": "Full payroll processing and benefits administration; no attendance, performance or recruitment access.",
    "IT / System Admin": "Full platform administration and asset management; no HR, payroll or performance access.",
    "Branch Manager": "Branch-scoped view of people, attendance and performance; no payroll, recruitment or platform access.",
    "Branch Head": "Oversees every Branch Manager company-wide — cross-branch visibility and approvals, no platform administration.",
    "Project Manager": "Approve leave, timesheets and travel for your project team; view-only on broader org and payroll data.",
    "Professional / IC Employee": "Self-service only — your profile, attendance, leave, payslip and assigned work.",
    "Associate / Intern": "Self-service only — your profile, attendance, leave, payslip and assigned work.",
}

# Ported 1:1 from kRoles — the 11 built-in roles, in display order.
BUILTIN_ROLES: list[str] = list(DEFAULT_MATRIX.keys())

# Built-in self-service roles (SEC-01/02/03 defense in depth). Whatever their
# stored matrix says, authorization caps these roles at their template levels
# (crud.effective_role_matrix), so a corrupted/over-granted self-service role
# can never act as a tenant admin. backend/app/check_role_matrix_drift.py
# reports any tenant whose stored data exceeds this.
SELF_SERVICE_ROLES: frozenset[str] = frozenset({"Professional / IC Employee", "Associate / Intern"})

# Level ordering for "is A broader than B": Self (own records only) is
# narrower than View (everyone's records).
LEVEL_RANK: dict[str, int] = {"n": 0, "s": 1, "v": 2, "e": 3, "a": 4}

# Columns every self-service template leaves at 'n' -- administrative
# capabilities a self-service role must never hold at e/a.
ADMIN_COLUMNS: frozenset[str] = frozenset(
    key
    for i, key in enumerate(COLUMN_KEYS)
    if all(DEFAULT_MATRIX[r].split(",")[i] == "n" for r in SELF_SERVICE_ROLES)
)


def template_levels(role_name: str) -> dict[str, str] | None:
    row = DEFAULT_MATRIX.get(role_name)
    if row is None:
        return None
    return dict(zip(COLUMN_KEYS, row.split(",")))
