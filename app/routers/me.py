from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import auth_state, crud, models, schemas, security
from ..database import get_db, get_session_tenant_slug
from ..deps import get_current_user

router = APIRouter(prefix="/api/me", tags=["me"])


@router.get("/people-access", response_model=schemas.PeopleAccessOut)
def get_people_access(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The calling user's own effective People-module access -- backs the
    frontend's people_access_provider (no WorkforceStore involved). See
    crud.effective_people_access / crud.can_access_people_module."""
    configured, actions = crud.effective_people_access(db, current_user)
    return schemas.PeopleAccessOut(configured=configured, actions=actions)


@router.get("/actions/{resource}", response_model=schemas.MyActionsOut)
def get_my_actions(
    resource: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The calling user's own granted granular actions for `resource` --
    backs frontend action-gating for modules with no rbac_columns entry
    (e.g. Learning), mirroring what require_action(resource, ...) enforces
    server-side. See crud.get_user_granted_actions."""
    return schemas.MyActionsOut(
        resource=resource,
        actions=crud.get_user_granted_actions(db, current_user.id, resource),
    )


@router.post("/password")
def change_my_password(
    payload: schemas.ChangeMyPasswordRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
) -> dict:
    """Edit Profile > change own password. Self-service only -- there is no
    employee_id in this path, it always targets the signed-in user's own
    account, and the New/Confirm match check happens here too (not just
    client-side) since this endpoint can be called directly.

    Same steps as Administration > User Roles > Reset Password
    (routers/user_roles.py's reset_user_password): new bcrypt hash written
    to both core_users and public.users, audit logged, and every OTHER
    session/device on this account ends (AUTH-A2 token_version bump) --
    the same "log out everywhere else" behaviour as an admin-triggered
    reset.

    Unlike that admin reset, the CALLER's own session is kept alive: a
    changed password hash would otherwise kill even the token making this
    very request (its "pwf" fingerprint claim, checked on every request in
    deps.get_current_user, stops matching the instant the hash changes --
    same mechanism as the token_version bump, just hash-keyed instead of a
    counter). So a fresh token -- scoped to the NEW hash and the bumped
    token_version -- is minted here and returned; the client swaps its
    stored token for this one and keeps working on the same screen instead
    of being bounced to the login screen."""
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=422, detail="New Password and Confirm New Password do not match.")
    new_hash = security.hash_password(payload.new_password)
    crud.reset_user_password(db, current_user.id, new_hash)
    public_user_id = auth_state.public_user_id_for_core_user(db, current_user)
    auth_state.bump_token_version(db, public_user_id)
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "change_password", "user", current_user.id,
    )
    db.commit()

    token_version = 0
    if public_user_id is not None and auth_state.has_token_version(db):
        token_version = int(
            db.execute(
                text("SELECT token_version FROM public.users WHERE id = :id"), {"id": public_user_id}
            ).scalar() or 0
        )
    roles = crud.get_user_roles(db, current_user.id)
    new_token = security.create_access_token(
        user_id=current_user.id,
        tenant_slug=get_session_tenant_slug(db),
        company_id=current_user.company_id,
        employee_id=current_user.employee_id,
        roles=[r.name for r in roles],
        public_user_id=public_user_id,
        token_version=token_version,
        password_hash=new_hash,
        # Same session: keeps "keep me signed in" and the original sign-in time.
        remember=bool(getattr(db.info.get("token_data"), "remember", False)),
        auth_time=getattr(db.info.get("token_data"), "auth_time", None),
    )
    return {"access_token": new_token}
