"""Owner-tier reservation and role-tier validation (SEC-01 code part,
SEC-05, RBAC-02).

Rules
─────
1. Only a user holding "Organization Owner / CEO" may assign that role (via
   POST /api/users/{id}/roles or POST /api/employees role_name), edit its
   matrix/actions/settings, delete it, or reset the password of / change the
   roles of a user who holds it.
2. A non-Owner may only assign a role, or edit the matrix of a role, whose
   levels do not exceed the caller's own effective matrix on any RBAC
   column ("no granting more than you have").
3. A non-Owner may only reset the password of, or change roles of, a user
   whose effective access does not exceed their own on any column (so e.g. a
   System-Settings editor can't take over a higher-tier admin account).

Every check uses crud.effective_role_matrix, so a self-service role's
corrupted grants never count as "tier".
"""

import uuid

from sqlalchemy.orm import Session

from . import crud, models
from .rbac_columns import BUILTIN_ROLES, COLUMN_KEYS, COLUMN_LABELS, LEVEL_RANK

OWNER_ROLE_NAME = BUILTIN_ROLES[0]


def is_owner(db: Session, user_id: uuid.UUID) -> bool:
    return any(r.name == OWNER_ROLE_NAME for r in crud.get_user_roles(db, user_id))


def _combined_matrix(roles: list[models.Role]) -> dict[str, str]:
    """Column-wise max over every role a user holds."""
    combined = {key: "n" for key in COLUMN_KEYS}
    for role in roles:
        matrix = crud.effective_role_matrix(role)
        for key in COLUMN_KEYS:
            if LEVEL_RANK.get(matrix.get(key, "n"), 0) > LEVEL_RANK.get(combined[key], 0):
                combined[key] = matrix.get(key, "n")
    return combined


def _first_exceeding_column(candidate: dict[str, str], ceiling: dict[str, str]) -> str | None:
    """First RBAC column on which `candidate` grants more than `ceiling`.

    A self-only ('s') level never counts as exceeding: it confers access to
    the holder's OWN records only, which every employee has anyway, so it is
    not "more access" than the caller's for tier purposes (H-01: HR, who sits
    at 'n' on Timesheet Approval / Payroll (View Own) / Assets, must still be
    able to onboard IC and Intern staff whose roles are 's' there). Every
    level above 's' (v/e/a) is still compared strictly."""
    for key in COLUMN_KEYS:
        level = candidate.get(key, "n")
        if level == "s":
            continue
        if LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(ceiling.get(key, "n"), 0):
            return key
    return None


def role_assignment_error(db: Session, actor: models.User, role: models.Role) -> str | None:
    """Why `actor` may not assign `role` to someone, or None if allowed."""
    if is_owner(db, actor.id):
        return None
    if role.name == OWNER_ROLE_NAME:
        return "Only the Organization Owner can assign the Organization Owner / CEO role."
    column = _first_exceeding_column(
        crud.effective_role_matrix(role), _combined_matrix(crud.get_user_roles(db, actor.id))
    )
    if column is not None:
        return (
            f"You can't assign the role '{role.name}': it has more access than your own "
            f"on '{COLUMN_LABELS.get(column, column)}'."
        )
    return None


def role_edit_error(
    db: Session, actor: models.User, role: models.Role, new_matrix: dict[str, str] | None = None
) -> str | None:
    """Why `actor` may not edit/delete `role` (or set it to `new_matrix`)."""
    if is_owner(db, actor.id):
        return None
    if role.name == OWNER_ROLE_NAME:
        return "Only the Organization Owner can change the Organization Owner / CEO role."
    if new_matrix:
        ceiling = _combined_matrix(crud.get_user_roles(db, actor.id))
        current = crud.build_role_matrix(role)
        for key, level in new_matrix.items():
            if level == current.get(key, "n"):
                continue  # unchanged columns never block a save
            if key in ceiling and LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(ceiling[key], 0):
                return (
                    f"You can't grant '{COLUMN_LABELS.get(key, key)}' at a higher level "
                    "than you hold yourself."
                )
    return None


def user_management_error(db: Session, actor: models.User, target: models.User) -> str | None:
    """Why `actor` may not reset the password of / change roles of `target`."""
    if actor.id == target.id or is_owner(db, actor.id):
        return None
    target_roles = crud.get_user_roles(db, target.id)
    if any(r.name == OWNER_ROLE_NAME for r in target_roles):
        return "Only the Organization Owner can manage the Organization Owner's account."
    column = _first_exceeding_column(
        _combined_matrix(target_roles), _combined_matrix(crud.get_user_roles(db, actor.id))
    )
    if column is not None:
        return (
            "You can't manage this account: it has more access than your own "
            f"on '{COLUMN_LABELS.get(column, column)}'."
        )
    return None


# ── Individual grants / role actions ceiling ──────────────────────────────
# Same rule as Employee Permissions (routers/employee_permissions.
# _grant_ceiling_error): a non-Owner may hand out access to a module only up
# to their own level on it. Used by Employee Module Access grants
# (PUT/DELETE /api/module-access/grants/...) and role action edits
# (PUT /api/roles/{id}/actions).

_ADMIN_ACTIONS = {"manage", "configure"}


def implied_level(actions: set[str]) -> str:
    """Matrix level a set of catalog actions confers on the module's columns
    -- the same mapping crud.grant_matrix_levels applies to a grant."""
    if actions & _ADMIN_ACTIONS:
        return "a"
    return "e" if actions - {"view"} else "v"


def grant_ceiling_error(db: Session, actor: models.User, resource: str, actions: set[str]) -> str | None:
    """Why `actor` may not grant (or revoke) `actions` on `resource`, or None.
      * Module with RBAC columns (e.g. payroll): the level those actions
        confer may not exceed the actor's own effective level on the module
        (highest of its columns, as in Employee Permissions).
      * Module without columns: the actor must hold every action
        themselves (crud.user_has_action)."""
    if not actions or is_owner(db, actor.id):
        return None
    from .rbac_columns import COLUMN_MODULES

    module_key = next((pa.module.key for pa in crud.list_permission_actions(db)
                       if pa.resource == resource and pa.module), None)
    columns = [c for c, m in COLUMN_MODULES.items() if m == module_key] if module_key else []
    if columns:
        mine = crud.effective_user_matrix(db, actor)
        own = max((mine.get(c, "n") for c in columns), key=lambda x: LEVEL_RANK.get(x, 0))
        wanted = implied_level(actions)
        if LEVEL_RANK.get(wanted, 0) > LEVEL_RANK.get(own, 0):
            return (f"You can't grant {', '.join(sorted(actions))} on '{resource}': that gives "
                    f"'{_LEVEL_LABELS[wanted]}' access, above your own level "
                    f"('{_LEVEL_LABELS.get(own, 'No Access')}').")
        return None
    missing = sorted(a for a in actions if not crud.user_has_action(db, actor.id, resource, a))
    if missing:
        return (f"You can't grant {', '.join(missing)} on '{resource}': you don't hold "
                "that access yourself.")
    return None


_LEVEL_LABELS = {"n": "No Access", "s": "Self Only", "v": "View", "e": "Edit", "a": "Admin"}

SELF_GRANT_ERROR = "You can't change your own access. Ask the Organization Owner or another administrator."


def self_role_change_error(db: Session, actor: models.User, target: models.User | None) -> str | None:
    """A non-Owner may not add, change or remove roles on their OWN account
    (self-escalation -- e.g. an IT / System Admin giving themselves a
    payroll role). Call only when the target's roles would actually change."""
    if target is None or actor.id != target.id or is_owner(db, actor.id):
        return None
    return SELF_GRANT_ERROR
