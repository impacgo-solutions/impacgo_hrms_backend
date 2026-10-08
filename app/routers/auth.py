import logging
import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import access_lifecycle, auth_state, crud, models, schemas, security
from ..database import ensure_tenant_search_path, get_db, set_session_tenant_slug, set_tenant_context
from ..deps import get_current_user
from ..rbac_columns import BUILTIN_ROLES, COLUMN_KEYS

router = APIRouter(prefix="/api/auth", tags=["auth"])

_INVALID_CREDENTIALS = "Invalid username or password"
PASSWORD_RESET_REQUIRED = (
    "Your password must be reset by an administrator before you can sign in. "
    "Please contact your HR or system administrator."
)
logger = logging.getLogger(__name__)


def _locked_response(retry_after: int) -> HTTPException:
    """AUTH-A3: 429 + Retry-After, the status the Flutter login already maps
    to its "too many attempts, try again later" message
    (LoginRateLimitedException in lib/data/auth/auth_api_repository.dart)."""
    minutes = max(1, math.ceil(retry_after / 60))
    return HTTPException(
        status_code=429,
        detail=(
            "Too many failed sign-in attempts. This account is temporarily locked -- "
            f"try again in {minutes} minute{'s' if minutes != 1 else ''}."
        ),
        headers={"Retry-After": str(retry_after)},
    )


@router.post("/login", response_model=schemas.LoginResponse)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    email_key = payload.email.strip().lower()
    # Step 1: public.users — auth + tenant routing
    public_user = crud.find_public_user_by_email(db, payload.email)

    # AUTH-A3: an account locked by repeated failures is refused even with the
    # right password, until the lock expires.
    locked_for = auth_state.lockout_remaining_seconds(db, email_key, public_user)
    if locked_for > 0:
        raise _locked_response(locked_for)

    # Unknown email and wrong password take the same path: one bcrypt check
    # (a dummy hash when there's no user), the same lockout statements, the
    # same generic 401 -- neither the response nor its timing reveals which
    # emails exist.
    password_ok = security.verify_password_or_dummy(
        payload.password, public_user.password_hash if public_user is not None else None
    )
    # bcrypt 72-byte limit: a long password that only matches the OLD
    # truncated hash is refused (no fallback) and the account is flagged for
    # an admin reset. Unknown emails run the same check against the dummy
    # hash, so timing stays the same.
    if not password_ok and security.matches_legacy_truncated_hash(
        payload.password, public_user.password_hash if public_user is not None else None
    ) and public_user is not None:
        auth_state.flag_password_reset_required(db, public_user)
        db.commit()
        logger.warning("Login refused: account %s needs an admin password reset (72-byte bcrypt hash)",
                       public_user.id)
        raise HTTPException(status_code=403, detail=PASSWORD_RESET_REQUIRED)
    if public_user is None or not password_ok:
        lock_seconds = auth_state.record_failed_login(db, email_key, public_user)
        db.commit()
        if lock_seconds:
            raise _locked_response(lock_seconds)
        raise HTTPException(status_code=401, detail=_INVALID_CREDENTIALS)
    # A flagged account (72-byte bcrypt hash) stays refused until an admin
    # resets it -- even for the 72-byte prefix, which its old hash matches.
    if auth_state.is_password_reset_required(db, public_user):
        raise HTTPException(status_code=403, detail=PASSWORD_RESET_REQUIRED)
    # A deactivated login (inactive / terminated / exited employee) is refused
    # even with the right password. Said only after the password check, so
    # guessers learn nothing; not counted as a failed attempt.
    if not public_user.is_active:
        raise HTTPException(status_code=403, detail=access_lifecycle.ACCOUNT_INACTIVE_DETAIL)

    tenant_slug = public_user.tenant_slug
    company_id = public_user.company_id
    # employee_id is stored separately; fall back to id for rows pre-dating the column
    employee_id = public_user.employee_id or public_user.id

    # AUTH-A4 / C6: a blocked tenant or an ended trial can't sign in. Checked
    # only after the password so tenant status isn't revealed to guessers.
    # public.users is shared with other applications: only logins of HRMS
    # ('hcm') tenants may open a session here (also after the password).
    if not tenant_slug or not auth_state.is_hrms_tenant(db, tenant_slug):
        raise HTTPException(status_code=403, detail=auth_state.NOT_HRMS_TENANT_DETAIL)
    block_reason = auth_state.tenant_block_reason_for_slug(db, tenant_slug)
    if block_reason is not None:
        raise HTTPException(status_code=403, detail=block_reason)

    # Step 2: Route to tenant schema — set ContextVar AND explicit SET
    # (after_begin already fired for this session, so we must SET directly)
    set_tenant_context(tenant_slug)
    set_session_tenant_slug(db, tenant_slug)
    ensure_tenant_search_path(db)  # SET LOCAL once per transaction (PERF-05, SEC-08)

    # Step 3: Load employee from tenant schema
    employee = db.get(models.Employee, employee_id)

    # Step 3b: the employee's CURRENT status decides, whatever the login rows
    # say -- a status changed without syncing the login (direct SQL, an
    # older code path) is caught here and the login rows are corrected.
    if access_lifecycle.employee_access_blocked(employee):
        access_lifecycle.sync_login_access(db, employee)
        db.commit()
        raise HTTPException(status_code=403, detail=access_lifecycle.ACCOUNT_INACTIVE_DETAIL)

    # Step 4: Load core_users by employee_id to get the tenant-space user_id
    core_user = crud.get_core_user_by_employee_id(db, employee_id)
    if core_user is None:
        raise HTTPException(status_code=401, detail="Account is not active")
    if core_user.status != "active":
        raise HTTPException(status_code=403, detail=access_lifecycle.ACCOUNT_INACTIVE_DETAIL)

    # Step 5: Load role from core_user_roles → core_roles -- the same
    # "primary role" every require_permission check uses.
    roles = crud.get_user_roles(db, core_user.id)
    if not roles:
        raise HTTPException(status_code=403, detail="This account has no role assigned")
    primary_role = crud.get_user_primary_role(db, core_user.id) or roles[0]

    # Step 6: Load permissions from core_role_permissions → core_permissions
    # (still returned in the response body; no longer embedded in the JWT -- P12).
    permissions = crud.get_role_permission_codes(db, primary_role.id)

    # Successful login clears the failed-attempt counter; stamp last_login_at.
    auth_state.reset_failed_logins(db, email_key, public_user)
    public_user.last_login_at = datetime.now(timezone.utc)
    token_version = 0
    if auth_state.has_token_version(db):
        token_version = int(
            db.execute(
                text("SELECT token_version FROM public.users WHERE id = :id"), {"id": public_user.id}
            ).scalar()
            or 0
        )
    db.commit()

    token = security.create_access_token(
        user_id=core_user.id,
        tenant_slug=tenant_slug,
        company_id=company_id,
        employee_id=employee_id,
        roles=[r.name for r in roles],
        public_user_id=public_user.id,
        token_version=token_version,
        password_hash=public_user.password_hash,
    )

    return schemas.LoginResponse(
        access_token=token,
        role=primary_role.name,
        employee=employee,
        tenant_slug=tenant_slug,
        company_id=company_id,
        permissions=permissions,
    )


@router.post("/logout", status_code=204)
def logout(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """AUTH-A2: ends the session server-side.
      * The presented token is revoked immediately (its jti, in-process).
      * Where public.users.token_version exists, it is bumped too -- that ends
        EVERY session of this user (all devices/tabs), durably. This is the
        intended "log out everywhere" behavior.
    Idempotent from the client's view: calling it with an already-revoked
    token returns 401, which the app already treats as "logged out"."""
    token_data: security.TokenData | None = db.info.get("token_data")
    if token_data is not None:
        security.revoke_token(token_data.jti, token_data.expires_at)
        public_user_id = token_data.public_user_id or auth_state.public_user_id_for_core_user(
            db, current_user
        )
        auth_state.bump_token_version(db, public_user_id)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "logout", "user", current_user.id)
    db.commit()
    return Response(status_code=204)


@router.get("/me")
def me(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
) -> dict:
    """AUTH-A5 / S1: the server's view of the current session, so the app
    never has to trust a locally stored role. Contract (exact keys):
      user_id, email, employee_id (str|null), company_id, tenant_slug,
      role (primary role name|null), roles [str], is_owner (bool),
      matrix {column_key: level} -- the EFFECTIVE levels authorization uses
        (self-service roles capped at template; Owner = 'a' everywhere,
        mirroring the backend's Owner bypass),
      actions ["resource.action", ...] -- granular grants across all roles
        (Owner = the whole catalog)."""
    token_data: security.TokenData | None = db.info.get("token_data")
    roles = crud.get_user_roles(db, current_user.id)
    primary = crud.get_user_primary_role(db, current_user.id)
    is_owner = any(r.name == BUILTIN_ROLES[0] for r in roles)
    if is_owner:
        matrix = {key: "a" for key in COLUMN_KEYS}
        actions = sorted({f"{pa.resource}.{pa.action}" for pa in crud.list_permission_actions(db)})
    else:
        # Role matrix + actions, raised by the employee's individual grants
        # (Employee Module Access) -- the same view authorization uses.
        effective = crud.effective_user_matrix(db, current_user)
        matrix = {key: effective.get(key, "n") for key in COLUMN_KEYS}
        granted: set[str] = set()
        for role in roles:
            for resource, acts in crud.build_role_actions(role).items():
                granted.update(f"{resource}.{a}" for a in acts)
        for resource, acts in crud.employee_access_grants(db, current_user.employee_id).items():
            granted.update(f"{resource}.{a}" for a in acts)
        # Employee Permissions replace the role's actions for their modules.
        from .. import employee_permissions

        allowed, decided = employee_permissions.granted_action_strings(db, current_user.employee_id)
        granted = {g for g in granted if g.split(".", 1)[0] not in decided} | allowed
        actions = sorted(granted)
    try:
        people_actions = sorted(crud.effective_people_access(db, current_user)[1])
    except Exception:  # noqa: BLE001 -- informational; never fails /me
        people_actions = []
    return {
        "user_id": str(current_user.id),
        "email": current_user.email,
        "employee_id": str(current_user.employee_id) if current_user.employee_id else None,
        "company_id": str(current_user.company_id),
        "tenant_slug": (token_data.tenant_slug if token_data else None) or "",
        "role": primary.name if primary is not None else None,
        "roles": [r.name for r in roles],
        "is_owner": is_owner,
        "matrix": matrix,
        "actions": actions,
        # People-module actions the caller holds (same source as
        # GET /api/me/people-access) -- lets the app pick the right roster
        # endpoint at login without a doomed /employees/full call.
        "people_actions": people_actions,
        # {module: level} set for this employee in Administration > Roles &
        # Permissions > Employee Permissions -- decides module visibility.
        "employee_permissions": ({} if is_owner else _employee_permissions(db, current_user)),
    }


def _employee_permissions(db: Session, user: models.User) -> dict[str, str]:
    from .. import employee_permissions

    try:
        return employee_permissions.for_employee(db, user.employee_id)
    except Exception:  # noqa: BLE001 -- informational; never fails /me
        return {}
