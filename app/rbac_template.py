"""Deterministic derivation of default granular (resource, action) grants
for each of the 14 built-in roles (see rbac_columns.BUILTIN_ROLES), from
that role's existing rbac_columns.DEFAULT_MATRIX levels.

Why this exists: no tenant in this system -- Vertexa included -- has ever
populated a single granular (view/create/edit/delete/approve/export/...)
grant; every tenant's RBAC state today is 100% the legacy 16-column
View/Self/Edit/Admin matrix. When the RBAC default template is baked into
the `_template` schema (see backend/db/seed_rbac_template.sql), it should
ship with the newer granular system pre-populated too, not left empty --
but since no real tenant has ever configured it, there is nothing to mine
from Vertexa here. This module is that synthesized default policy, written
out explicitly and reviewably (not hand-typed per role/resource) so it can
be checked once as a whole rather than trusted cell-by-cell.

Nothing in this module touches a database -- it's a pure function of the
constants in rbac_columns.py. backend/app/generate_rbac_template_actions_sql.py
imports it to print the SQL that ends up in seed_rbac_template.sql.
"""

from __future__ import annotations

# Mirrors public.permission_actions exactly as seeded by backend/db/
# add_permission_actions_catalog.sql + add_employee_permission_actions.sql.
# Kept as a static snapshot (not queried live) so this module has no DB
# dependency and its output is reproducible offline; the generator script
# re-validates every emitted (resource, action) pair against this table
# before printing SQL, so a stale snapshot fails loudly instead of silently
# producing an orphaned permission.
CATALOG_ACTIONS: dict[str, list[str]] = {
    "employee": ["view", "create", "edit", "delete", "transfer", "terminate", "export", "import"],
    "leave": ["view", "approve", "reject"],
    "payroll": ["view", "generate", "edit", "delete", "export"],
    "projects": ["view", "create", "edit", "delete", "assign"],
    "reports": ["view", "export", "import"],
    "config": ["view", "edit"],
    "dashboard": ["view"],
    "organization": ["view", "edit", "configure"],
    "attendance": ["view", "create", "edit", "delete", "approve", "reject"],
    "performance": ["view", "create", "edit", "delete", "approve"],
    "recruitment": ["view", "create", "edit", "delete", "manage"],
    "benefits": ["view", "create", "edit", "delete", "manage"],
    "learning": ["view", "create", "edit", "delete", "assign"],
    "work": ["view", "create", "edit", "approve", "reject"],
    "travel": ["view", "create", "edit", "approve", "reject"],
    "assets": ["view", "create", "edit", "delete", "assign", "manage"],
    "documents": ["view", "create", "delete", "manage"],
    "approvals": ["view"],
    "admin": ["view", "configure", "manage"],
}

# Legacy 16-column matrix key -> granular resource(s) it drives. Absent on
# purpose: own_profile / payroll_view_own (self-scoped -- the granular
# vocabulary has no "own records only" concept, see the 's'-handling note
# below) and audit_logs (no "audit" resource exists in the granular catalog
# at all -- there is nowhere for it to map to).
COLUMN_TO_RESOURCES: dict[str, list[str]] = {
    "team_attendance": ["attendance"],
    "leave_approval": ["leave"],
    "timesheet_approval": ["work"],
    "payroll_process": ["payroll"],
    "benefits_admin": ["benefits"],
    "recruitment": ["recruitment"],
    "performance_reviews": ["performance"],
    "org_structure_config": ["organization", "config"],
    "reports_analytics": ["reports"],
    "system_settings_rbac": ["admin", "config"],
    "travel_expense_approval": ["travel"],
    "projects_access": ["projects"],
    "asset_management": ["assets"],
}

# Per-resource level -> action-set tiers, using only actions that exist in
# CATALOG_ACTIONS for that resource. 'v' (View) and 's' (Self) are treated
# as equivalent here -- deliberately. The granular vocabulary has no
# per-row "own records only" scope, so a matrix level of 's' can't be
# expressed any more narrowly than 'v' once translated into this system.
# This is a documented simplification, not a bug: e.g. a role with
# team_attendance='s' gets a flat attendance.view grant, not one scoped to
# "their own attendance only".
LEVEL_TIERS: dict[str, dict[str, list[str]]] = {
    "attendance": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "create", "edit", "approve", "reject"],
        "a": ["view", "create", "edit", "delete", "approve", "reject"],
    },
    "leave": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "approve", "reject"],
        "a": ["view", "approve", "reject"],
    },
    "work": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "create", "edit", "approve", "reject"],
        "a": ["view", "create", "edit", "approve", "reject"],
    },
    "travel": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "create", "edit", "approve", "reject"],
        "a": ["view", "create", "edit", "approve", "reject"],
    },
    "payroll": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "generate", "edit"],
        "a": ["view", "generate", "edit", "delete", "export"],
    },
    "benefits": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "create", "edit"],
        "a": ["view", "create", "edit", "delete", "manage"],
    },
    "recruitment": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "create", "edit"],
        "a": ["view", "create", "edit", "delete", "manage"],
    },
    "performance": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "create", "edit", "approve"],
        "a": ["view", "create", "edit", "delete", "approve"],
    },
    "organization": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "edit"],
        "a": ["view", "edit", "configure"],
    },
    "reports": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "export"],
        "a": ["view", "export", "import"],
    },
    "projects": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "create", "edit"],
        "a": ["view", "create", "edit", "delete", "assign"],
    },
    "assets": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "create", "edit"],
        "a": ["view", "create", "edit", "delete", "assign", "manage"],
    },
    "admin": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "configure"],
        "a": ["view", "configure", "manage"],
    },
    "config": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "edit"],
        "a": ["view", "edit"],
    },
    "learning": {
        "v": ["view"], "s": ["view"],
        "e": ["view", "create", "edit"],
        "a": ["view", "create", "edit", "delete", "assign"],
    },
}

# performance_reviews doubles as the signal for Learning & Development
# access -- this app has no separate matrix column for L&D, and the two are
# adjacent HR functions in every built-in role's real-world remit.
LEARNING_SOURCE_COLUMN = "performance_reviews"

# Any role with edit/admin on at least one of these three approval columns
# is assumed to also want the unified Approvals inbox.
APPROVAL_TRIGGER_COLUMNS = ["leave_approval", "timesheet_approval", "travel_expense_approval"]

# Fallback grants for the two resources with no legacy-matrix equivalent at
# all. This is genuinely new policy -- review/tune per role before it ships
# in _template. Roughly: Owner + HR get full employee lifecycle control;
# company-wide senior roles (C-level, VP/Director) get view+create+export
# for reporting; line-management roles get read-only visibility into their
# team's records; individual contributors get no company-wide grant (their
# own record is already covered by own_profile, an unrestricted mechanism
# today -- see crud.can_access_people_module).
EMPLOYEE_ACTIONS_BY_ROLE: dict[str, list[str]] = {
    "Organization Owner / CEO": ["view", "create", "edit", "delete", "transfer", "terminate", "export", "import"],
    "HR / Recruitment Staff": ["view", "create", "edit", "delete", "transfer", "terminate", "export", "import"],
    "C-Level Executive": ["view", "create", "edit", "export"],
    "VP / Director": ["view", "create", "edit", "export"],
    "General Manager / Sr. Manager": ["view", "edit"],
    "Manager": ["view"],
    "Team Lead": ["view"],
    "Branch Manager": ["view"],
    "Branch Head": ["view"],
    "Project Manager": ["view"],
    "Finance / Payroll Staff": ["view", "export"],
    "IT / System Admin": ["view", "export"],
    # Professional / IC Employee, Associate / Intern: intentionally absent
    # -- no company-wide employee-records grant.
}

DOCUMENTS_ACTIONS_BY_ROLE: dict[str, list[str]] = {
    "Organization Owner / CEO": ["view", "create", "delete", "manage"],
    "HR / Recruitment Staff": ["view", "create", "delete", "manage"],
    "IT / System Admin": ["view", "create", "delete", "manage"],
    "C-Level Executive": ["view", "create"],
    "VP / Director": ["view", "create"],
    "General Manager / Sr. Manager": ["view", "create"],
    "Manager": ["view", "create"],
    "Team Lead": ["view", "create"],
    "Branch Manager": ["view", "create"],
    "Branch Head": ["view", "create"],
    "Project Manager": ["view", "create"],
    "Finance / Payroll Staff": ["view", "create"],
    "Professional / IC Employee": ["view"],
    "Associate / Intern": ["view"],
}


def derive_default_actions(role_name: str, matrix: dict[str, str]) -> dict[str, list[str]]:
    """matrix is {column_key: level} for one role, e.g.
    dict(zip(rbac_columns.COLUMN_KEYS, rbac_columns.DEFAULT_MATRIX[role_name].split(","))).

    Returns {resource: [action, ...]}, sorted and de-duplicated, with every
    pair validated against CATALOG_ACTIONS -- an unknown (resource, action)
    raises immediately instead of silently producing an orphaned
    permission that GET /api/permission-actions would never advertise."""
    result: dict[str, set[str]] = {}

    for column_key, resources in COLUMN_TO_RESOURCES.items():
        level = matrix.get(column_key, "n")
        if level == "n":
            continue
        for resource in resources:
            tiers = LEVEL_TIERS[resource]
            result.setdefault(resource, set()).update(tiers.get(level, tiers["v"]))

    learning_level = matrix.get(LEARNING_SOURCE_COLUMN, "n")
    if learning_level != "n":
        tiers = LEVEL_TIERS["learning"]
        result.setdefault("learning", set()).update(tiers.get(learning_level, tiers["v"]))

    # Every role gets the dashboard -- there is no scenario in this app
    # where a logged-in user shouldn't see a dashboard at all.
    result.setdefault("dashboard", set()).add("view")

    if any(matrix.get(col, "n") in ("e", "a") for col in APPROVAL_TRIGGER_COLUMNS):
        result.setdefault("approvals", set()).add("view")

    if role_name in EMPLOYEE_ACTIONS_BY_ROLE:
        result.setdefault("employee", set()).update(EMPLOYEE_ACTIONS_BY_ROLE[role_name])
    if role_name in DOCUMENTS_ACTIONS_BY_ROLE:
        result.setdefault("documents", set()).update(DOCUMENTS_ACTIONS_BY_ROLE[role_name])

    for resource, actions in result.items():
        valid = set(CATALOG_ACTIONS.get(resource, []))
        invalid = actions - valid
        if invalid:
            raise ValueError(
                f"derive_default_actions({role_name!r}): {resource!r} has actions "
                f"{sorted(invalid)} not present in CATALOG_ACTIONS[{resource!r}] "
                f"({sorted(valid)}) -- update the CATALOG_ACTIONS snapshot to match "
                f"the live public.permission_actions catalog."
            )

    return {resource: sorted(actions) for resource, actions in result.items() if actions}
