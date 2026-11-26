import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import auth_state, crud, models, role_tiers, schemas, security
from ..database import get_db, get_session_tenant_slug
from ..deps import DEFAULT_LIST_LIMIT, require_permission

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[schemas.UserOut])
def list_users(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    return [
        schemas.UserOut(
            id=u.id,
            email=u.email,
            employee_id=u.employee_id,
            employee_name=(
                f"{u.employee.first_name} {u.employee.last_name or ''}".strip()
                if u.employee
                else None
            ),
            status=u.status,
        )
        for u in crud.list_users(db, current_user.company_id, limit=limit, offset=offset)
    ]


@router.get("/password-reset-required")
def list_password_reset_required(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
) -> list[dict]:
    """Accounts in this organization that must get an admin password reset:
    their stored hash came from a >72-byte password that bcrypt truncated
    (flagged at their first sign-in attempt since the fix -- see
    db/add_password_reset_required.sql). `user_id` is the id
    POST /api/users/{user_id}/reset-password takes; a reset clears the flag.
    Empty until that migration has run."""
    if not auth_state.has_reset_required_column(db, get_session_tenant_slug(db)):
        return []
    rows = db.execute(
        text(
            "SELECT password_reset_required_at AS flagged_at, id AS user_id, email, employee_id, full_name "
            "FROM core_users WHERE password_reset_required AND company_id = :cid "
            "ORDER BY password_reset_required_at"
        ),
        {"cid": current_user.company_id},
    ).all()
    return [
        {"user_id": str(r.user_id), "email": r.email, "employee_id": str(r.employee_id) if r.employee_id else None,
         "name": r.full_name, "flagged_at": r.flagged_at.isoformat() if r.flagged_at else None}
        for r in rows
    ]


@router.post("/{user_id}/reset-password", status_code=204)
def reset_user_password(
    user_id: uuid.UUID,
    payload: schemas.PasswordResetRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    """Administration > User Roles > "Reset Password" -- the only way to
    fix a login broken outside the normal password lifecycle (e.g. an
    account created with a password other than what was communicated),
    previously only fixable with a direct database edit."""
    user = db.get(models.User, user_id)
    if user is None or user.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="User not found")
    # SEC-01: Owner-tier reservation -- only the Owner may reset the Owner's
    # (or any higher-tier admin's) password.
    error = role_tiers.user_management_error(db, current_user, user)
    if error is not None:
        raise HTTPException(status_code=403, detail=error)
    new_hash = security.hash_password(payload.new_password)
    crud.reset_user_password(db, user_id, new_hash)
    # AUTH-A2: every session issued before the reset ends (the "pwf" claim no
    # longer matches; token_version bumped too where that column exists).
    auth_state.bump_token_version(db, auth_state.public_user_id_for_core_user(db, user))
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "reset_password", "user", user_id,
    )
    db.commit()


@router.get("/{user_id}/roles", response_model=list[schemas.UserRoleOut])
def list_user_roles(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    user = db.get(models.User, user_id)
    if user is None or user.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="User not found")
    return [
        schemas.UserRoleOut(role_id=role.id, role_name=role.name, branch_id=None)
        for role in crud.get_user_roles(db, user_id)
    ]


@router.post("/{user_id}/roles", response_model=list[schemas.UserRoleOut], status_code=201)
def assign_user_role(
    user_id: uuid.UUID,
    payload: schemas.UserRoleAssignRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    """Assigns an additional role to a user -- core.user_roles' composite PK
    already supported more than one row per user; this is the first endpoint
    that actually adds a second one, matching "multiple roles must be
    assignable to one employee." Existing single-role users/behavior is
    unaffected -- crud.get_user_primary_role still returns the first-ever-
    assigned role for every endpoint that hasn't moved to require_action."""
    user = db.get(models.User, user_id)
    # C14/SEC-09: the target user must belong to the caller's company, same
    # as every sibling endpoint in this router.
    if user is None or user.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="User not found")
    role = db.get(models.Role, payload.role_id)
    if role is None or role.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Role not found")
    # SEC-01: only the Owner may grant the Owner role; nobody may grant a
    # role with more access than their own, or re-role a higher-tier user.
    error = role_tiers.self_role_change_error(db, current_user, user) or (
        role_tiers.role_assignment_error(db, current_user, role)
    ) or role_tiers.user_management_error(db, current_user, user)
    if error is not None:
        raise HTTPException(status_code=403, detail=error)
    crud.assign_user_role(
        db, user_id, payload.role_id, payload.branch_id, actor_id=current_user.id
    )
    auth_state.bump_token_version(db, auth_state.public_user_id_for_core_user(db, user))
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "user_roles", user_id,
        changes={"assigned_role_id": str(payload.role_id)},
    )
    db.commit()
    return [
        schemas.UserRoleOut(role_id=r.id, role_name=r.name, branch_id=None)
        for r in crud.get_user_roles(db, user_id)
    ]


@router.delete("/{user_id}/roles/{role_id}", response_model=list[schemas.UserRoleOut])
def unassign_user_role(
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    user = db.get(models.User, user_id)
    if user is None or user.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="User not found")
    role = db.get(models.Role, role_id)
    if role is not None and role.company_id == current_user.company_id:
        error = role_tiers.self_role_change_error(db, current_user, user) or (
            role_tiers.user_management_error(db, current_user, user)
        ) or (
            role_tiers.role_assignment_error(db, current_user, role)
        )
        if error is not None:
            raise HTTPException(status_code=403, detail=error)
    crud.remove_user_role(db, user_id, role_id)
    auth_state.bump_token_version(db, auth_state.public_user_id_for_core_user(db, user))
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "user_roles", user_id,
        changes={"removed_role_id": str(role_id)},
    )
    db.commit()
    return [
        schemas.UserRoleOut(role_id=r.id, role_name=r.name, branch_id=None)
        for r in crud.get_user_roles(db, user_id)
    ]
