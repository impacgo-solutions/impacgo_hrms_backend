import uuid

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import access_lifecycle, auth_state, crud, database, models, security
from .database import get_db
from .rbac_columns import BUILTIN_ROLES, SELF_SERVICE_ROLES

_bearer_scheme = HTTPBearer(auto_error=False)

# The Organization Owner / CEO role bypasses every permission check, mirroring
# the frontend's RbacEngine.canAct: `if (role == kRoles[0]) return true;`.
_OWNER_ROLE_NAME = BUILTIN_ROLES[0]

# API-05: page size a list endpoint returns when the caller sends no `limit`
# (previously unbounded -- the whole table). Callers wanting everything page
# through with explicit limit/offset (the Flutter client does this in
# AuthAwareHttpClient); `le=500` stays the hard per-request cap.
DEFAULT_LIST_LIMIT = 100


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    """Validates the bearer token every mutating endpoint now requires."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return authenticate_token(credentials.credentials, db)


def authenticate_token(raw_token: str, db: Session) -> models.User:
    """Shared by get_current_user, the notifications WebSocket and /media.
    Beyond signature/expiry, rejects (AUTH-A2/A4, C6):
      * platform (super-admin) tokens -- no tenant;
      * tokens revoked by POST /api/auth/logout (jti) or whose token_version
        is behind public.users.token_version ("tv"; tokens issued before this
        claim existed count as version 0);
      * tokens issued before the password last changed ("pwf" fingerprint;
        tokens without the claim are accepted until they expire);
      * a deactivated public.users login (is_active re-checked per request),
        an inactive core_users row, or an employee whose status ended access
        (access_lifecycle.employee_access_blocked);
      * a blocked tenant or an ended trial (401 carrying the reason, so the
        app drops the session and the login screen shows why).
    Account/tenant state is cached for settings.auth_state_cache_seconds."""
    token_data = security.decode_access_token(raw_token)
    if token_data is None or not token_data.tenant_slug:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    state = auth_state.load_auth_state(db, token_data)
    if state is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not state.is_active:
        raise HTTPException(status_code=401, detail=access_lifecycle.ACCOUNT_INACTIVE_DETAIL)
    if state.tenant_block_reason is not None:
        raise HTTPException(status_code=401, detail=state.tenant_block_reason)
    if (
        token_data.password_fingerprint is not None
        and token_data.password_fingerprint != state.password_fingerprint
    ) or token_data.token_version != state.token_version:
        raise HTTPException(status_code=401, detail="Your session has ended. Please log in again.")
    # Set tenant search_path — both ContextVar (for after_begin) and explicit SET
    # (in case the transaction is already open from connection pool reuse)
    if token_data.tenant_slug:
        database.set_tenant_context(token_data.tenant_slug)
        database.set_session_tenant_slug(db, token_data.tenant_slug)
        database.ensure_tenant_search_path(db)  # SET LOCAL once per transaction (PERF-05, SEC-08)
    user = crud.get_user_by_id(db, token_data.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # core_users.status and the employee's own status are read fresh every
    # request (not cached), so a deactivation takes effect immediately on
    # every worker -- not only once the cached auth state expires.
    if user.status != "active" or access_lifecycle.employee_access_blocked(
        db.get(models.Employee, user.employee_id) if user.employee_id else None
    ):
        raise HTTPException(status_code=401, detail=access_lifecycle.ACCOUNT_INACTIVE_DETAIL)
    if token_data.company_id is not None and user.company_id != token_data.company_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    database.set_session_current_user(db, user.id)
    db.info["token_data"] = token_data
    return user


def is_owner(db: Session, user: models.User) -> bool:
    return any(role.name == _OWNER_ROLE_NAME for role in crud.get_user_roles(db, user.id))


def require_permission(column_key: str):
    """Backend twin of the frontend's RbacEngine.canAct(column, role) — the
    same RBAC matrix column that hides/shows a button client-side must also
    hold 'e' (Edit) or 'a' (Admin) for the caller's role here, or the request
    is rejected regardless of what the UI would have allowed."""

    def checker(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        role = crud.get_user_primary_role(db, user.id)
        if role is None:
            raise HTTPException(status_code=403, detail="This account has no role assigned")
        if role.name == _OWNER_ROLE_NAME:
            return user
        # effective_role_matrix: a built-in self-service role (IC/Associate)
        # is capped at its template, whatever its stored grants say (SEC-01);
        # effective_user_matrix then adds the person's individual grants
        # (Employee Module Access -- additive, explicitly assigned by an admin).
        matrix = crud.effective_user_matrix(db, user)
        if matrix.get(column_key) not in ("e", "a"):
            raise HTTPException(status_code=403, detail="You don't have permission to do this")
        return user

    return checker


def require_view_permission(column_key: str):
    """Read-side twin of require_permission: View, Edit or Admin on the
    column (Self-only and No Access are refused). For list endpoints whose
    rows belong to other people -- e.g. candidate offers/compensation."""

    def checker(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        role = crud.get_user_primary_role(db, user.id)
        if role is None:
            raise HTTPException(status_code=403, detail="This account has no role assigned")
        if role.name == _OWNER_ROLE_NAME:
            return user
        if crud.effective_user_matrix(db, user).get(column_key) not in ("v", "e", "a"):
            raise HTTPException(status_code=403, detail="You don't have permission to view this")
        return user

    return checker


def require_permission_or_action(column_key: str, resource: str, action: str):
    """Widened sibling of require_permission: passes under EITHER the old
    matrix rule (Edit/Admin on `column_key`, same as require_permission)
    OR the new granular catalog's (resource, action) grant on any role the
    caller holds (crud.user_has_action) -- see crud.permission_satisfied.
    Purely additive: every caller who could already pass require_permission
    still can; a caller with only the new granular grant now also can.
    Same Owner bypass as require_permission/require_action."""

    def checker(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        role = crud.get_user_primary_role(db, user.id)
        if role is not None and role.name == _OWNER_ROLE_NAME:
            return user
        if not crud.permission_satisfied(db, user, column_key, resource, action):
            raise HTTPException(status_code=403, detail="You don't have permission to do this")
        return user

    return checker


def require_action(resource: str, action: str):
    """Sibling of require_permission for the new granular (resource, action)
    permission catalog (see permission_actions.py) -- checks the UNION of
    every role assigned to the caller (crud.user_has_action), not just the
    single "primary" role require_permission still uses, since a user can
    now hold more than one role (core.user_roles always supported this;
    only the permission-check code never looked past the first row)."""

    def checker(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        roles = crud.get_user_roles(db, user.id)
        if any(role.name == _OWNER_ROLE_NAME for role in roles):
            return user
        if not crud.user_has_action(db, user.id, resource, action):
            raise HTTPException(status_code=403, detail="You don't have permission to do this")
        return user

    return checker


def require_people_access(action: str):
    """Gate a People-module endpoint behind the tenant-configurable
    People access policy (see crud.can_access_people_module) -- open to
    everyone until a company grants at least one Role an 'employee'
    action, at which point only Roles granted `action` (minus any
    Designation-level denial) may pass. Zero-risk for any company that
    never touches this feature."""

    def checker(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        if not crud.can_access_people_module(db, user, action):
            raise HTTPException(status_code=403, detail="You don't have permission to do this")
        return user

    return checker


def require_employee_visible(db: Session, user: models.User, employee_id: uuid.UUID) -> None:
    """403 unless `employee_id` is within the caller's People visibility
    (crud.get_people_directory_visible_ids -- the rule GET /employees and
    /full use): org-wide scopes (Owner / HR / payroll processors /
    document-admin tiers / Employee Permissions 'all records') reach anyone
    in their company; everyone else only their reporting subtree + self.
    Another company's or tenant's id is never in a scoped set."""
    visible = crud.get_people_directory_visible_ids(db, user)
    if visible is not None and employee_id not in visible:
        raise HTTPException(status_code=403, detail="You don't have permission to do this")


def require_people_access_to_employee(action: str):
    """People-module `action` on ONE employee (path `employee_id`): the
    module gate plus require_employee_visible. For HR actions on someone's
    record (transfer, org / reporting / payroll edits, documents, contract
    changes) -- no self exemption beyond what the caller's own access gives."""

    def checker(
        employee_id: uuid.UUID,
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        if not crud.can_access_people_module(db, user, action):
            raise HTTPException(status_code=403, detail="You don't have permission to do this")
        require_employee_visible(db, user, employee_id)
        return user

    return checker


def require_people_access_or_self(action: str):
    """Same gate as require_people_access, EXCEPT a caller viewing/editing
    their own employee record always passes regardless of the company's
    People access policy state.

    This matters because can_access_people_module is a company-WIDE switch:
    the instant any Role in a company is granted a single 'employee.*'
    action (e.g. via the RBAC default template's HR / Recruitment Staff
    grant), every OTHER Role with no explicit 'employee.*' grant is cut off
    from the whole module -- including from viewing their own profile,
    since the underlying endpoints (GET /api/employees/{id}/overview
    /personal/professional/education) never had a "is this me?" exemption.
    Own-profile access is governed separately by the legacy matrix's
    own_profile column (always non-'n' for every built-in role -- see
    rbac_columns.DEFAULT_MATRIX), so this exemption doesn't widen anything
    that wasn't already supposed to be available -- it just stops a
    company-wide policy switch from accidentally locking someone out of
    their own record purely because of a name coincidence between two
    unrelated permission dimensions.

    Relies on FastAPI resolving the `employee_id` path parameter into this
    dependency by name, exactly as it does for the route handler itself."""

    def checker(
        employee_id: uuid.UUID,
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        if user.employee_id == employee_id:
            return user
        if not crud.can_access_people_module(db, user, action):
            raise HTTPException(status_code=403, detail="You don't have permission to do this")
        require_employee_visible(db, user, employee_id)
        return user

    return checker


def require_people_access_or_self_or_manager(action: str):
    """Same as require_people_access_or_self, plus: a caller who is the
    viewed employee's Reporting Manager or Dotted-Line Manager -- anywhere
    in their upward reporting chain, per crud.is_manager_of_employee, the
    same definition already used to authorize deciding that employee's
    pending approval requests -- may also pass.

    Deliberately opt-in per endpoint (not a blanket widening of
    require_people_access_or_self): only apply this to endpoints that edit
    non-sensitive, non-org-structure fields (e.g. Employee Profile personal
    info, Education & Experience). Payroll/bank/compensation, reporting-
    hierarchy, and other org-structure endpoints must keep using
    require_people_access_or_self / require_permission so a manager can
    never touch a report's sensitive or structural fields through this
    path."""

    def checker(
        employee_id: uuid.UUID,
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        if user.employee_id == employee_id:
            return user
        if user.employee_id is not None and crud.is_manager_of_employee(
            db, user.employee_id, employee_id
        ):
            return user
        if crud.can_access_people_module(db, user, action):
            require_employee_visible(db, user, employee_id)  # not any employee by id
            return user
        raise HTTPException(status_code=403, detail="You don't have permission to do this")

    return checker


def require_module_enabled(module_key: str):
    """Gate an entire router behind a company's module toggle (see
    crud.get_enabled_module_keys / core.company_modules). Returns 404, not
    403 -- a disabled module should look like it doesn't exist, matching
    the sidebar/dashboard tile disappearing for the same reason. Only
    applied to routers whose whole domain maps 1:1 to one toggleable
    module (payroll, recruitment, performance, learning, benefits, assets,
    projects, travel) -- core modules (auth, employees, organization,
    roles, permissions, audit, config, settings) never gate on this."""

    def checker(
        user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        if not crud.is_module_enabled(db, user.company_id, module_key):
            raise HTTPException(status_code=404, detail="This module is not enabled for your company")
        return user

    return checker


def get_payroll_scope(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> str:
    """Backend twin of the frontend's RbacEngine.payrollScope: "org" if the
    caller's role can process payroll (Edit/Admin on payroll_process,
    mirroring require_permission's own e/a-only rule), else "self" -- the
    same fallback the UI already uses to decide whether Payroll shows
    company-wide data or just the logged-in employee's own payslip.

    Fails closed (SEC-02): any doubt -- no role, a built-in self-service role
    (IC / Associate, whatever its stored grants say), no explicit Edit/Admin
    on payroll_process, or any error while resolving the role -- yields
    "self". Only the Owner or a role explicitly holding payroll_process at
    e/a sees org-wide payroll data."""
    try:
        role = crud.get_user_primary_role(db, user.id)
        if role is None:
            return "self"
        if role.name == _OWNER_ROLE_NAME:
            return "org"
        # An admin-set Payroll level for this employee (Employee
        # Permissions) decides; otherwise the role rules below.
        from . import employee_permissions

        payroll_level = employee_permissions.level_for(db, user.employee_id, "payroll")
        if payroll_level is not None:
            return "org" if employee_permissions.matrix_level(payroll_level) in ("e", "a") else "self"
        if role.name in SELF_SERVICE_ROLES:
            return "self"
        return "org" if crud.effective_user_matrix(db, user).get("payroll_process") in ("e", "a") else "self"
    except Exception:  # noqa: BLE001 -- fail closed, never open
        return "self"


# Roles whose *dashboard_scope* bucket (see roles.py's _DASHBOARD_SCOPE,
# frontend's rbac_seed.dart kDashboardScope) is "team" rather than "org" --
# kept as a small standalone set here (rather than importing routers/roles.py
# into deps.py). Must stay in sync with _DASHBOARD_SCOPE if either changes.
# C9: "team" now always means the caller's reporting subtree + self
# (crud.get_team_scope_employee_ids) -- previously it was resolved through
# crud.get_visible_employee_ids_for_docs, whose document tiers treat General
# Manager / Sr. Manager as unrestricted, so GMs silently got org-wide data.
_REPORTS_TEAM_SCOPE_ROLES = {"General Manager / Sr. Manager", "Manager", "Team Lead"}


def get_reports_scope(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> str:
    """"org" (full company) or "team" (the caller's own direct-report subtree
    only, via crud.get_team_scope_employee_ids -- C9). Reports endpoints previously returned company-wide data to
    anyone who merely held Edit/Admin on reports_analytics, with no further
    restriction -- this closes that gap for the 2 roles whose own
    dashboard_scope was already documented as "team", not "org"."""
    role = crud.get_user_primary_role(db, user.id)
    if role is not None and role.name == _OWNER_ROLE_NAME:
        return "org"
    if role is not None and role.name in _REPORTS_TEAM_SCOPE_ROLES:
        return "team"
    return "org"


def require_super_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> models.AdminUser:
    """Gate for every HRMS Super Admin platform endpoint (routers/
    super_admin.py). Deliberately independent of get_current_user: a
    platform token has no tenant_slug/company_id at all (see
    security.create_access_token's all-optional fields), so this never
    touches database.set_tenant_context/set_session_tenant_slug -- the
    session's search_path stays whatever the pooled connection already
    has, which is safe here because every public-schema model this token
    is allowed to touch (Tenant, AdminUser, PlatformSettings,
    PlatformNotification) declares __table_args__ = {"schema": "public"}
    and so is always schema-qualified regardless of search_path."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token_data = security.decode_access_token(credentials.credentials)
    if token_data is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # AUTH-A1: a valid TENANT token is authenticated but not authorized here
    # -> 403, not 401 (the Flutter client treats any 401 as "log out").
    if token_data.tenant_slug is not None:
        raise HTTPException(status_code=403, detail="Super admin access required")
    if "super_admin" not in token_data.roles:
        raise HTTPException(status_code=403, detail="Super admin access required")
    admin = db.get(models.AdminUser, token_data.user_id)
    if not crud.is_platform_super_admin(db, admin):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # SA-03: a password change ends every session issued before it.
    if token_data.password_fingerprint != security.password_fingerprint(admin.password_hash):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    db.info["sa_token_data"] = token_data
    return admin


WS_BEARER_SUBPROTOCOL = "bearer"


def websocket_token(scope_subprotocols: list[str], query_token: str | None) -> tuple[str | None, str | None]:
    """AUTH-A6: (jwt, subprotocol_to_accept) for a WebSocket handshake.
    Preferred: the client offers the subprotocols ["bearer", "<jwt>"] (the
    only header a browser WebSocket can set), so the token never appears in
    the URL / access logs; the server then accepts subprotocol "bearer".
    Legacy fallback: `?token=<jwt>` (older app builds) -- still accepted, and
    redacted from uvicorn logs by main._RedactTokensFilter."""
    protocols = [p.strip() for p in scope_subprotocols or []]
    if WS_BEARER_SUBPROTOCOL in protocols:
        idx = protocols.index(WS_BEARER_SUBPROTOCOL)
        if idx + 1 < len(protocols) and protocols[idx + 1]:
            return protocols[idx + 1], WS_BEARER_SUBPROTOCOL
        return None, WS_BEARER_SUBPROTOCOL
    return query_token, None


def authenticate_websocket_user(raw_token: str | None) -> uuid.UUID | None:
    """Same checks as get_current_user (revocation, is_active, tenant status),
    on a short-lived session of its own. Returns the core user id or None."""
    if not raw_token:
        return None
    db = database.SessionLocal()
    try:
        return authenticate_token(raw_token, db).id
    except HTTPException:
        return None
    finally:
        db.close()


# HR documents (letters, HR-document emails): the Owner, or Edit/Admin on
# any of these matrix columns -- never a self-service role.
HR_DOCUMENT_COLUMNS = ("recruitment", "payroll_process", "system_settings_rbac")


def has_column_access(db: Session, user: models.User, *columns: str) -> bool:
    """Owner, or Edit/Admin on any of [columns] (same effective matrix as
    require_permission)."""
    role = crud.get_user_primary_role(db, user.id)
    if role is None:
        return False
    if role.name == _OWNER_ROLE_NAME:
        return True
    matrix = crud.effective_user_matrix(db, user)
    return any(matrix.get(c) in ("e", "a") for c in columns)


def require_hr_documents(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> models.User:
    if not has_column_access(db, user, *HR_DOCUMENT_COLUMNS):
        raise HTTPException(status_code=403, detail="Only HR / admin users can manage HR documents.")
    return user
