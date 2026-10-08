import base64
import calendar
import datetime
import json
import logging
import re
import uuid
from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import and_, create_engine, delete, event, func, literal, or_, select, update
from sqlalchemy import column as sa_column, table as sa_table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, joinedload, load_only, selectinload, sessionmaker

from . import database, email_service, models, schemas, security, ws_manager
from .config import settings
from .designation_seed_data import BAND_NUMBERS, DESIGNATION_BANDS, SEED_TITLE_ORDER
from .rbac_columns import BUILTIN_ROLES, COLUMN_LABELS, COLUMN_MODULES

logger = logging.getLogger(__name__)


# (tenant schema, table name) -> (exists, checked_at monotonic). Positive
# results are cached for the process lifetime (tables are never dropped by
# the app); negative results are re-checked after _TABLE_MISSING_TTL so a
# migration that later adds the table is picked up without a restart.
_TENANT_TABLE_EXISTS: dict[tuple[str | None, str], tuple[bool, float]] = {}
_TABLE_MISSING_TTL = 300.0


def tenant_table_exists(db: Session, table: str) -> bool:
    """True if `table` exists in the current request's tenant schema (or,
    with no tenant stamped on the session, anywhere on the search_path).
    One `to_regclass` round trip per (process, schema, table) instead of a
    SAVEPOINT/RELEASE pair per row (PERF-01)."""
    import time

    from sqlalchemy import text as _text

    slug = database.get_session_tenant_slug(db) or database.get_tenant_slug()
    key = (slug, table)
    hit = _TENANT_TABLE_EXISTS.get(key)
    now = time.monotonic()
    if hit is not None and (hit[0] or now - hit[1] < _TABLE_MISSING_TTL):
        return hit[0]
    qualified = f'"{slug}".{table}' if slug else table
    exists = bool(db.scalar(_text("SELECT to_regclass(:t) IS NOT NULL"), {"t": qualified}))
    _TENANT_TABLE_EXISTS[key] = (exists, now)
    return exists


def _optional_tenant_table(label: str, db: Session, fn, default=None, table: str | None = None):
    """Runs [fn] (a zero-arg callable making one or more queries against a
    table this app maps but that isn't guaranteed to exist in every
    tenant's schema) inside a savepoint. Some tenant schemas predate
    certain pm_*/fin_* tables -- confirmed live: the "Infyq" tenant is
    missing fin_fiscal_years, and older tenants can be missing
    pm_project_categories/pm_project_roles/pm_project_status_history/
    pm_project_budgets entirely. Returns [default] and logs a warning
    instead of letting a missing table 500 the whole request and poison
    the caller's outer transaction (without the savepoint, one failed
    statement aborts every change already made in this request, including
    ones that had nothing to do with the missing table).

    When `table` is given, existence is checked once per (process, tenant
    schema) via tenant_table_exists: a missing table returns [default]
    with no query at all, an existing one runs [fn] directly with no
    SAVEPOINT/RELEASE round trips (PERF-01)."""
    if table is not None:
        if not tenant_table_exists(db, table):
            return default
        return fn()
    try:
        with db.begin_nested():
            return fn()
    except ProgrammingError:
        logger.warning(
            "Tenant schema is missing a table needed for %s -- skipping", label, exc_info=True,
        )
        return default


def derive_company_email_domain(company: models.Company) -> str:
    """The domain half of an auto-generated work email. Prefers the
    company's own configured core.companies.email_domain; falls back to a
    name-derived domain (lowercase, alphanumeric-only, + ".com") for any
    company that never set one explicitly -- same formula the backfill in
    backend/db/add_company_email_domain.sql used."""
    if company.email_domain:
        return company.email_domain
    return re.sub(r"[^a-z0-9]", "", company.name.lower()) + ".com"


def get_default_company(db: Session) -> models.Company:
    """The frontend has no multi-company concept, so every role/permission
    lookup is scoped to this single seeded company row."""
    company = db.scalar(
        select(models.Company).where(models.Company.name == settings.default_company_name)
    )
    if company is None:
        raise RuntimeError(
            "Default company not found — run the seed script first "
            "(python -m app.seed)."
        )
    return company


def update_company_profile(db: Session, company: models.Company, updates: dict) -> models.Company:
    """Partial update — `updates` only has the keys the request body actually
    set (Pydantic's exclude_unset), so fields the admin didn't touch keep
    their current value rather than getting wiped to null."""
    for field, value in updates.items():
        setattr(company, field, value)
    db.flush()
    return company


def update_employee_profile(db: Session, employee: models.Employee, updates: dict) -> models.Employee:
    """Partial update -- same reasoning as update_company_profile."""
    for field, value in updates.items():
        setattr(employee, field, value)
    db.flush()
    return employee


def get_emergency_contact(db: Session, employee_id: uuid.UUID) -> models.Contact | None:
    return db.scalar(
        select(models.Contact).where(
            models.Contact.entity_type == "employee",
            models.Contact.entity_id == employee_id,
            models.Contact.is_emergency.is_(True),
        )
    )


def upsert_emergency_contact(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    name: str | None,
    relation: str | None,
    phone: str | None,
) -> models.Contact | None:
    """Employee Profile > Edit Profile's Emergency Contact fields -- a
    separate core.contacts row (entity_type='employee', is_emergency=true),
    same table Add Employee writes to. No-ops if all three fields are
    unset (nothing to save)."""
    if name is None and relation is None and phone is None:
        return get_emergency_contact(db, employee_id)

    existing = get_emergency_contact(db, employee_id)
    if existing is not None:
        if name is not None:
            existing.name = name
        if relation is not None:
            existing.designation = relation
        if phone is not None:
            existing.phone = phone
        db.flush()
        return existing

    contact = models.Contact(
        id=uuid.uuid4(),
        company_id=company_id,
        entity_type="employee",
        entity_id=employee_id,
        name=name or "",
        designation=relation,
        phone=phone,
        is_emergency=True,
    )
    db.add(contact)
    db.flush()
    return contact


def get_or_create_permission(db: Session, column_key: str, level: str) -> models.Permission:
    code = f"{column_key}.{level}"
    perm = db.scalar(select(models.Permission).where(models.Permission.code == code))
    if perm is not None:
        return perm
    perm = models.Permission(
        id=uuid.uuid4(),
        code=code,
        module=COLUMN_MODULES.get(column_key, "general")[:20],
        resource=column_key,
        action=level,
    )
    db.add(perm)
    db.flush()
    return perm


def build_role_matrix(role: models.Role) -> dict[str, str]:
    """Every RBAC column defaults to 'n' (no access); a role only has a row
    in role_permissions for the columns it's been granted above 'n'."""
    matrix = {key: "n" for key in COLUMN_LABELS}
    for rp in role.role_permissions:
        # role_permissions also holds granular catalog grants whose resource
        # can share a column's name (e.g. ("recruitment", "view")); only the
        # single-letter level rows belong in the matrix, otherwise whichever
        # row loaded last overwrote the real level.
        if rp.permission.resource in matrix and rp.permission.action in ("n", "v", "s", "e", "a"):
            matrix[rp.permission.resource] = rp.permission.action
    return matrix


def effective_role_matrix(role: models.Role) -> dict[str, str]:
    """The matrix AUTHORIZATION should use: build_role_matrix, except a
    built-in self-service role (rbac_columns.SELF_SERVICE_ROLES -- IC,
    Associate/Intern) is capped at its template level per column, so
    over-granted/corrupted stored data (SEC-01: the impacgo-solutions IC role
    was found holding HR's '.a' grants) can never make a self-service
    employee a tenant admin. The RBAC matrix editor (GET
    /api/roles/{id}/matrix) shows these effective levels too, plus the caps,
    and PUT refuses levels above them (routers/roles.py)."""
    from .rbac_columns import LEVEL_RANK, SELF_SERVICE_ROLES, template_levels

    matrix = build_role_matrix(role)
    if role.name not in SELF_SERVICE_ROLES:
        return matrix
    ceiling = template_levels(role.name) or {}
    for key, cap in ceiling.items():
        level = matrix.get(key, "n")
        if LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(cap, 0):
            matrix[key] = cap
    return matrix


def apply_matrix_update(
    db: Session, role: models.Role, updates: dict[str, str], user_id: uuid.UUID | None = None
) -> None:
    for column_key, level in updates.items():
        if column_key not in COLUMN_LABELS:
            raise ValueError(f"Unknown RBAC column: {column_key}")
        if level not in ("n", "v", "s", "e", "a"):
            raise ValueError(f"Invalid level '{level}' for column {column_key}")

        # Drop any existing LEGACY grant for this column (only one level per
        # column can be active at a time). cascade="all, delete-orphan" on
        # Role.role_permissions deletes the row once it leaves the collection.
        # Scoped to single-char n/v/s/e/a actions only (see build_role_actions'
        # identical length-based distinction) -- some column slugs collide
        # with a granular permission_actions resource of the same name (e.g.
        # "recruitment" is both a legacy matrix column AND a granular
        # catalog resource with real create/edit/delete/manage actions).
        # Without this filter, saving the legacy matrix for such a column
        # silently deleted that role's granular grants for the same
        # resource -- apply_actions_update's own deletion loop already
        # filters the opposite way (`action in all_actions`), this mirrors
        # it for symmetry.
        for rp in list(role.role_permissions):
            if rp.permission.resource == column_key and len(rp.permission.action) == 1:
                role.role_permissions.remove(rp)

        if level == "n":
            continue  # no access -> no role_permissions row

        permission = get_or_create_permission(db, column_key, level)
        role.role_permissions.append(
            models.RolePermission(permission=permission, created_by=user_id, updated_by=user_id)
        )

    db.flush()


def get_organization_dashboard_summary(db: Session, company_id: uuid.UUID) -> dict:
    """Pure aggregation over tables that already exist -- no new storage.
    Backs GET /api/organization/dashboard-summary's summary cards."""

    def _count(model, *extra_where):
        return db.scalar(
            select(func.count(model.id)).where(model.company_id == company_id, *extra_where)
        ) or 0

    sub_departments = db.scalar(
        select(func.count(models.SubDepartment.id))
        .join(models.Department, models.Department.id == models.SubDepartment.department_id)
        .where(models.Department.company_id == company_id)
    ) or 0

    pending_approvals = db.scalar(
        select(func.count(models.ApprovalRequest.id)).where(
            models.ApprovalRequest.company_id == company_id,
            models.ApprovalRequest.status == "pending",
        )
    ) or 0

    employee_transfers = db.scalar(
        select(func.count(models.EmployeeLifecycleEvent.id))
        .join(models.Employee, models.Employee.id == models.EmployeeLifecycleEvent.employee_id)
        .where(
            models.Employee.company_id == company_id,
            models.EmployeeLifecycleEvent.event_type == "transfer",
        )
    ) or 0

    return {
        "employees": _count(models.Employee),
        "branches": _count(models.Branch),
        "business_units": _count(models.BusinessUnit),
        "departments": _count(models.Department),
        "sub_departments": sub_departments,
        "designations": _count(models.Designation),
        "roles": _count(models.Role),
        "pending_approvals": pending_approvals,
        "employee_transfers": employee_transfers,
    }


def list_modules(db: Session) -> list[models.Module]:
    return db.scalars(select(models.Module).order_by(models.Module.sort_order)).all()


def get_company_module_states(db: Session, company_id: uuid.UUID) -> dict[str, bool]:
    """{module_key: is_enabled} for every module in the global catalog --
    modules with no core.company_modules row for this company default to
    enabled (True), so an unconfigured company is unaffected by this
    feature's existence."""
    rows = db.scalars(
        select(models.CompanyModule)
        .options(selectinload(models.CompanyModule.module))
        .where(models.CompanyModule.company_id == company_id)
    ).all()
    overrides = {row.module.key: row.is_enabled for row in rows}
    return {module.key: overrides.get(module.key, True) for module in list_modules(db)}


def get_enabled_module_keys(db: Session, company_id: uuid.UUID) -> set[str]:
    return {key for key, enabled in get_company_module_states(db, company_id).items() if enabled}


def is_module_enabled(db: Session, company_id: uuid.UUID, module_key: str) -> bool:
    """True (fail-open) for a module key that isn't in the catalog at all --
    a router gated on a not-yet-cataloged key should never 404 by accident."""
    row = db.scalar(
        select(models.CompanyModule)
        .join(models.Module)
        .where(
            models.CompanyModule.company_id == company_id,
            models.Module.key == module_key,
        )
    )
    return True if row is None else row.is_enabled


def set_company_module_enabled(
    db: Session,
    company_id: uuid.UUID,
    module_key: str,
    is_enabled: bool,
    actor_id: uuid.UUID | None,
) -> None:
    module = db.scalar(select(models.Module).where(models.Module.key == module_key))
    if module is None:
        raise ValueError(f"Unknown module: {module_key}")
    if module.is_core and not is_enabled:
        raise ValueError(f"'{module.name}' is a core module and cannot be disabled")
    row = db.scalar(
        select(models.CompanyModule).where(
            models.CompanyModule.company_id == company_id,
            models.CompanyModule.module_id == module.id,
        )
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    if row is None:
        row = models.CompanyModule(
            id=uuid.uuid4(), company_id=company_id, module_id=module.id,
            is_enabled=is_enabled, updated_by=actor_id, updated_at=now,
        )
        db.add(row)
    else:
        row.is_enabled = is_enabled
        row.updated_by = actor_id
        row.updated_at = now
    db.flush()


def list_approval_workflows(db: Session, company_id: uuid.UUID) -> list[models.ApprovalWorkflow]:
    return db.scalars(
        select(models.ApprovalWorkflow)
        .options(selectinload(models.ApprovalWorkflow.steps))
        .where(models.ApprovalWorkflow.company_id == company_id)
        .order_by(models.ApprovalWorkflow.doctype)
    ).all()


def upsert_approval_workflow(
    db: Session,
    company_id: uuid.UUID,
    doctype: str,
    name: str,
    steps: list[dict],
    user_id: uuid.UUID | None = None,
) -> models.ApprovalWorkflow:
    """Replaces the active workflow for (company_id, doctype) with the given
    name/steps -- deactivates any prior workflow for this doctype rather
    than deleting it, so in-flight ApprovalRequests still pointing at the
    old workflow_id keep resolving against the steps they started with."""
    old = db.scalar(
        select(models.ApprovalWorkflow).where(
            models.ApprovalWorkflow.company_id == company_id,
            models.ApprovalWorkflow.doctype == doctype,
            models.ApprovalWorkflow.is_active.is_(True),
        )
    )
    if old is not None:
        old.is_active = False
        old.updated_by = user_id

    workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(), company_id=company_id, doctype=doctype, name=name, is_active=True,
        created_by=user_id, updated_by=user_id,
    )
    db.add(workflow)
    db.flush()
    for step in sorted(steps, key=lambda s: s["step_order"]):
        db.add(
            models.ApprovalWorkflowStep(
                id=uuid.uuid4(), workflow_id=workflow.id,
                step_order=step["step_order"], approver_type=step["approver_type"],
                role_id=step.get("role_id"), user_id=step.get("user_id"),
                min_amount=step.get("min_amount"), max_amount=step.get("max_amount"),
                created_by=user_id, updated_by=user_id,
            )
        )
    db.flush()
    db.refresh(workflow)
    return workflow


def get_hierarchy_role_bindings(
    db: Session, company_id: uuid.UUID
) -> list[models.HierarchyRoleBinding]:
    return db.scalars(
        select(models.HierarchyRoleBinding)
        .options(selectinload(models.HierarchyRoleBinding.role))
        .where(models.HierarchyRoleBinding.company_id == company_id)
    ).all()


def upsert_hierarchy_role_bindings(
    db: Session,
    company_id: uuid.UUID,
    bindings: dict[str, "uuid.UUID | None"],
    user_id: uuid.UUID | None = None,
) -> None:
    from .org_hierarchy import _RUNG_DEFAULTS

    for rung_key, role_id in bindings.items():
        if rung_key not in _RUNG_DEFAULTS:
            raise ValueError(f"Unknown hierarchy rung: {rung_key}")
        existing = db.scalar(
            select(models.HierarchyRoleBinding).where(
                models.HierarchyRoleBinding.company_id == company_id,
                models.HierarchyRoleBinding.rung_key == rung_key,
            )
        )
        if role_id is None:
            if existing is not None:
                db.delete(existing)
            continue
        if existing is not None:
            existing.role_id = role_id
            existing.updated_by = user_id
        else:
            db.add(
                models.HierarchyRoleBinding(
                    id=uuid.uuid4(), company_id=company_id, rung_key=rung_key, role_id=role_id,
                    created_by=user_id, updated_by=user_id,
                )
            )
    db.flush()


def list_users(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.User]:
    """Backs Administration > User Roles' user picker."""
    q = (
        select(models.User)
        .options(selectinload(models.User.employee))
        .where(models.User.company_id == company_id)
        .order_by(models.User.email, models.User.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def reset_user_password(db: Session, user_id: uuid.UUID, new_password_hash: str) -> bool:
    """Admin-triggered password reset (Administration > User Roles). Writes
    to both core_users (this tenant schema) and public.users -- the actual
    login check only ever reads public.users.password_hash (see
    routers/auth.py's login), but core_users carries its own copy too, so
    both are kept in sync rather than leaving one stale. Returns False if
    no core_users row exists for this id (caller 404s)."""
    core_user = db.get(models.User, user_id)
    if core_user is None:
        return False
    core_user.password_hash = new_password_hash
    # public.users.id usually, but not always, equals core_users.id -- resolve
    # the login row the same way auth does, or a reset could miss the row
    # login actually reads.
    from . import auth_state

    public_user_id = auth_state.public_user_id_for_core_user(db, core_user)
    public_user = db.get(models.PublicUser, public_user_id) if public_user_id is not None else None
    if public_user is not None:
        public_user.password_hash = new_password_hash
        # A new hash (always made by security.hash_password) ends any
        # "needs admin reset" state from the 72-byte bcrypt fix.
        auth_state.clear_password_reset_required(db, public_user.tenant_slug, core_user.id)
    db.flush()
    return True


def get_user_roles(db: Session, user_id: uuid.UUID) -> list[models.Role]:
    """Every role assigned to this user (core.user_roles supports more than
    one row per user_id -- see the composite PK -- but until now only
    get_user_primary_role's first-row lookup was ever used). Used by the new
    granular-action engine so a user with multiple roles gets the union of
    every role's grants, per the "permissions combine" requirement.

    Eager-loads role_permissions.permission -- build_role_matrix/
    build_role_actions iterate every RolePermission row and touch
    `.permission` on each one; without this, a role with dozens of grants
    (any admin-tier role) triggered one lazy-load round trip per row, every
    single time (never cached across calls since a fresh Role is fetched
    each time) -- multiple seconds added to every permission check against
    a real, network-latent database.

    Ordered by USER_ROLE_ORDER (C8) -- the same deterministic rule
    get_user_primary_role and auth.py's login (`roles[0]`) rely on -- and
    memoized per request (PERF-05)."""
    return [ur.role for ur in _user_role_rows(db, user_id)]


# ---------------------------------------------------------------------------
# Per-request RBAC memo (PERF-05 / P4). require_permission*, get_*_scope,
# can_access_people_module etc. each used to re-run the same
# user_roles -> roles -> role_permissions -> permissions lookup, 2-4x per
# request. Results live in Session.info (one Session per request, shared by
# every dependency) and are dropped when:
#   * the top-level transaction ends (commit/rollback), or
#   * a flush / bulk UPDATE/DELETE/INSERT touches an RBAC table in this
#     session (role assignment, matrix/actions update, workflow edits),
# so a request that mutates roles/permissions always re-reads them.
# ---------------------------------------------------------------------------
_RBAC_MEMO_KEY = "_rbac_memo"

# C8: ONE deterministic "primary role" rule for every call site --
# earliest assignment first, role_id as the tie-break (NULL created_at last).
USER_ROLE_ORDER = (models.UserRole.created_at.asc().nullslast(), models.UserRole.role_id)


def _rbac_memo(db: Session) -> dict:
    memo = db.info.get(_RBAC_MEMO_KEY)
    if memo is None:
        memo = db.info[_RBAC_MEMO_KEY] = {}
    return memo


def clear_rbac_memo(db: Session) -> None:
    db.info.pop(_RBAC_MEMO_KEY, None)


def _rbac_memo_classes() -> tuple:
    return (
        models.UserRole, models.Role, models.RolePermission, models.Permission,
        models.DesignationPermission, models.PermissionAction,
        models.ApprovalWorkflow, models.ApprovalWorkflowStep,
        models.ShiftAssignment, models.Shift,
    )


@event.listens_for(Session, "after_flush")
def _rbac_memo_after_flush(session, flush_context):
    if _RBAC_MEMO_KEY not in session.info:
        return
    classes = _rbac_memo_classes()
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, classes):
            clear_rbac_memo(session)
            return


@event.listens_for(Session, "after_transaction_end")
def _rbac_memo_after_txn(session, transaction):
    if transaction.parent is None and not transaction.nested:
        clear_rbac_memo(session)


@event.listens_for(Session, "do_orm_execute")
def _rbac_memo_bulk_dml(orm_execute_state):
    if _RBAC_MEMO_KEY not in orm_execute_state.session.info:
        return
    if not (orm_execute_state.is_update or orm_execute_state.is_delete or orm_execute_state.is_insert):
        return
    classes = _rbac_memo_classes()
    for mapper in orm_execute_state.all_mappers:
        if issubclass(mapper.class_, classes):
            clear_rbac_memo(orm_execute_state.session)
            return


def _user_role_rows(db: Session, user_id: uuid.UUID) -> list[models.UserRole]:
    memo = _rbac_memo(db)
    key = ("user_roles", user_id)
    if key not in memo:
        memo[key] = db.scalars(
            select(models.UserRole)
            .options(
                selectinload(models.UserRole.role)
                .selectinload(models.Role.role_permissions)
                .selectinload(models.RolePermission.permission)
            )
            .where(models.UserRole.user_id == user_id)
            .order_by(*USER_ROLE_ORDER)
        ).all()
    return memo[key]


def assign_user_role(
    db: Session,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
) -> models.UserRole:
    existing = db.get(models.UserRole, {"user_id": user_id, "role_id": role_id})
    if existing is not None:
        return existing
    user_role = models.UserRole(
        user_id=user_id, role_id=role_id, branch_id=branch_id,
        created_by=actor_id, updated_by=actor_id,
    )
    db.add(user_role)
    db.flush()
    return user_role


def remove_user_role(db: Session, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    user_role = db.get(models.UserRole, {"user_id": user_id, "role_id": role_id})
    if user_role is not None:
        db.delete(user_role)
        db.flush()


def get_or_create_action_permission(db: Session, resource: str, action: str) -> models.Permission:
    code = f"{resource}.{action}"
    perm = db.scalar(select(models.Permission).where(models.Permission.code == code))
    if perm is not None:
        return perm
    perm = models.Permission(
        id=uuid.uuid4(), code=code, module=resource[:20], resource=resource, action=action,
    )
    db.add(perm)
    db.flush()
    return perm


def list_permission_actions(db: Session) -> list[models.PermissionAction]:
    """The metadata-driven granular (resource, action) catalog -- public.
    permission_actions, added via add_permission_actions_catalog.sql. The
    single source of truth for both what GET /api/permission-actions
    advertises and what apply_actions_update/build_role_actions accept --
    a plain INSERT into that table (no code change) is enough for a new
    module's actions to take effect everywhere.

    Memoized per request (PERF-05) -- see _rbac_memo."""
    memo = _rbac_memo(db)
    if "permission_actions" not in memo:
        memo["permission_actions"] = db.scalars(
            select(models.PermissionAction)
            .options(selectinload(models.PermissionAction.module))
            .join(models.Module)
            .order_by(models.Module.sort_order, models.PermissionAction.sort_order)
        ).all()
    return memo["permission_actions"]


def build_role_actions(role: models.Role) -> dict[str, list[str]]:
    """{resource: [action, ...]} of every granular action this role has been
    granted -- disjoint from build_role_matrix's v/s/e/a columns because the
    two catalogs never share an action string (single-char vs. full word).
    Reads the raw stored rows directly rather than re-validating against
    the current catalog -- a role keeps whatever it was granted even if a
    catalog row is later removed."""
    granted: dict[str, list[str]] = {}
    for rp in role.role_permissions:
        # Old matrix actions are always the single characters n/v/s/e/a
        # (get_or_create_permission); every granular action here is a full
        # word (view/create/edit/...) -- length alone reliably tells them
        # apart, same disjoint-vocabulary invariant as before, without
        # needing an extra catalog query on every role-actions read.
        if len(rp.permission.action) > 1:
            granted.setdefault(rp.permission.resource, []).append(rp.permission.action)
    return granted


def apply_actions_update(
    db: Session,
    role: models.Role,
    granted: dict[str, list[str]],
    user_id: uuid.UUID | None = None,
) -> None:
    """Full-replace semantics per resource included in `granted` (mirrors
    apply_matrix_update's per-column replace) -- a resource key present in
    the payload gets exactly the actions listed; a resource key absent from
    the payload is left untouched. Valid (resource, action) pairs come from
    the live public.permission_actions catalog, not a hardcoded list."""
    catalog = list_permission_actions(db)
    valid_actions_by_resource: dict[str, set[str]] = {}
    for pa in catalog:
        valid_actions_by_resource.setdefault(pa.resource, set()).add(pa.action)
    all_actions = {pa.action for pa in catalog}

    for resource, actions in granted.items():
        if resource not in valid_actions_by_resource:
            raise ValueError(f"Unknown permission resource: {resource}")
        for action in actions:
            if action not in valid_actions_by_resource[resource]:
                raise ValueError(f"Unknown action '{action}' for resource {resource}")

        for rp in list(role.role_permissions):
            if rp.permission.resource == resource and rp.permission.action in all_actions:
                role.role_permissions.remove(rp)

        for action in actions:
            permission = get_or_create_action_permission(db, resource, action)
            role.role_permissions.append(
                models.RolePermission(permission=permission, created_by=user_id, updated_by=user_id)
            )

    db.flush()


def user_has_action(db: Session, user_id: uuid.UUID, resource: str, action: str) -> bool:
    """True if ANY role assigned to this user grants (resource, action) --
    union across roles, since a user can hold more than one role -- or the
    employee was granted it individually (Employee Module Access). An
    Employee Permissions level on the resource's module decides instead."""
    from . import employee_permissions

    memo = _rbac_memo(db)
    key = ("user_employee_id", user_id)
    if key not in memo:
        memo[key] = db.scalar(select(models.User.employee_id).where(models.User.id == user_id))
    decided = employee_permissions.resource_allows(db, memo[key], resource, action)
    if decided is not None:
        return decided
    for role in get_user_roles(db, user_id):
        if action in build_role_actions(role).get(resource, []):
            return True
    return action in employee_access_grants_for_user(db, user_id).get(resource, set())


# ── Employee Module Access (per-employee grants, additive to roles) ───────
# core_employee_access_grants (backend/db/add_employee_access_grants.sql).
# A grant never removes access; it only adds catalog actions for one
# employee and, for modules that have RBAC matrix columns, raises those
# columns (view -> 'v'; manage/configure -> 'a'; any other action -> 'e').

_GRANT_ADMIN_ACTIONS = {"manage", "configure"}


def _employee_grants_table_present(db: Session) -> bool:
    memo = _rbac_memo(db)
    if "grants_table" not in memo:
        from sqlalchemy import text as _sql_text

        memo["grants_table"] = db.execute(
            _sql_text("SELECT to_regclass('core_employee_access_grants')")
        ).scalar() is not None
    return memo["grants_table"]


def employee_access_grants(db: Session, employee_id: uuid.UUID | None) -> dict[str, set[str]]:
    """{resource: {action, ...}} individually granted to this employee."""
    if employee_id is None or not _employee_grants_table_present(db):
        return {}
    memo = _rbac_memo(db)
    key = ("employee_grants", employee_id)
    if key not in memo:
        grants: dict[str, set[str]] = {}
        for resource, actions in db.execute(
            select(models.EmployeeAccessGrant.resource, models.EmployeeAccessGrant.actions)
            .where(models.EmployeeAccessGrant.employee_id == employee_id)
        ).all():
            grants.setdefault(resource, set()).update(actions or [])
        memo[key] = grants
    return memo[key]


def employee_access_grants_for_user(db: Session, user_id: uuid.UUID) -> dict[str, set[str]]:
    memo = _rbac_memo(db)
    key = ("user_employee_id", user_id)
    if key not in memo:
        memo[key] = db.scalar(select(models.User.employee_id).where(models.User.id == user_id))
    return employee_access_grants(db, memo[key])


def grant_matrix_levels(db: Session, employee_id: uuid.UUID | None) -> dict[str, str]:
    """RBAC column levels implied by the employee's individual grants."""
    grants = employee_access_grants(db, employee_id)
    if not grants:
        return {}
    module_of_resource = {pa.resource: pa.module.key for pa in list_permission_actions(db) if pa.module}
    from .rbac_columns import COLUMN_MODULES, LEVEL_RANK

    levels: dict[str, str] = {}
    for resource, actions in grants.items():
        module_key = module_of_resource.get(resource)
        if module_key is None or not actions:
            continue
        level = "a" if actions & _GRANT_ADMIN_ACTIONS else ("e" if actions - {"view"} else "v")
        for column, column_module in COLUMN_MODULES.items():
            if column_module == module_key and LEVEL_RANK[level] > LEVEL_RANK.get(levels.get(column, "n"), 0):
                levels[column] = level
    return levels


def effective_user_matrix(db: Session, user: "models.User") -> dict[str, str]:
    """What authorization uses for a person: their primary role's effective
    matrix, raised (never lowered) by their individual grants."""
    from .rbac_columns import LEVEL_RANK

    role = get_user_primary_role(db, user.id)
    matrix = dict(effective_role_matrix(role)) if role is not None else {}
    for column, level in grant_matrix_levels(db, user.employee_id).items():
        if LEVEL_RANK[level] > LEVEL_RANK.get(matrix.get(column, "n"), 0):
            matrix[column] = level
    # Employee Permissions (admin-set, per employee): a module's level
    # replaces whatever the role / grants gave for its columns.
    from . import employee_permissions

    return employee_permissions.apply_to_matrix(matrix, employee_permissions.for_employee(db, user.employee_id))


def get_user_granted_actions(db: Session, user_id: uuid.UUID, resource: str) -> list[str]:
    """The current user's own granted actions for `resource`, unioned across
    every role they hold -- backs GET /api/me/actions/{resource}, for
    frontend action-gating on modules like Learning that have no
    rbac_columns entry at all, only the granular permission_actions
    catalog (see require_action)."""
    roles = get_user_roles(db, user_id)
    if any(role.name == BUILTIN_ROLES[0] for role in roles):
        return sorted({pa.action for pa in list_permission_actions(db) if pa.resource == resource})
    granted: set[str] = set()
    for role in roles:
        granted |= set(build_role_actions(role).get(resource, []))
    granted |= employee_access_grants_for_user(db, user_id).get(resource, set())
    return sorted(granted)


def build_designation_denials(db: Session, designation_id: uuid.UUID) -> dict[str, list[str]]:
    """{resource: [action, ...]} explicitly DENIED for this Designation --
    see models.DesignationPermission. Named "denials", never "granted", so
    this can't be confused with build_role_actions' opposite semantics at a
    call site. No ORM relationship on Designation (same no-FK convention as
    role_id/band_id) -- queried directly by designation_id.

    Memoized per request (PERF-05); callers get a fresh copy so they can
    never mutate the cached value."""
    memo = _rbac_memo(db)
    key = ("designation_denials", designation_id)
    if key not in memo:
        rows = db.scalars(
            select(models.DesignationPermission).where(
                models.DesignationPermission.designation_id == designation_id
            )
        ).all()
        denied: dict[str, list[str]] = {}
        for row in rows:
            permission = db.get(models.Permission, row.permission_id)
            if permission is not None:
                denied.setdefault(permission.resource, []).append(permission.action)
        memo[key] = denied
    return {k: list(v) for k, v in memo[key].items()}


def apply_designation_denials_update(
    db: Session,
    designation: models.Designation,
    denied: dict[str, list[str]],
    user_id: uuid.UUID | None = None,
) -> None:
    """Full-replace per resource key present in `denied` (mirrors
    apply_actions_update's per-resource replace) -- a resource key present
    in the payload gets exactly the denials listed; a resource key absent
    from the payload is left untouched. Valid (resource, action) pairs come
    from the live public.permission_actions catalog, same as the Role side."""
    catalog = list_permission_actions(db)
    valid_actions_by_resource: dict[str, set[str]] = {}
    for pa in catalog:
        valid_actions_by_resource.setdefault(pa.resource, set()).add(pa.action)

    existing = db.scalars(
        select(models.DesignationPermission).where(
            models.DesignationPermission.designation_id == designation.id
        )
    ).all()
    existing_by_resource: dict[str, list[models.DesignationPermission]] = {}
    for row in existing:
        permission = db.get(models.Permission, row.permission_id)
        if permission is not None:
            existing_by_resource.setdefault(permission.resource, []).append(row)

    for resource, actions in denied.items():
        if resource not in valid_actions_by_resource:
            raise ValueError(f"Unknown permission resource: {resource}")
        for action in actions:
            if action not in valid_actions_by_resource[resource]:
                raise ValueError(f"Unknown action '{action}' for resource {resource}")

        for row in existing_by_resource.get(resource, []):
            db.delete(row)

        for action in actions:
            permission = get_or_create_action_permission(db, resource, action)
            db.add(
                models.DesignationPermission(
                    id=uuid.uuid4(), designation_id=designation.id,
                    permission_id=permission.id, created_by=user_id,
                )
            )

    db.flush()


def get_user_designation(db: Session, user_id: uuid.UUID) -> models.Designation | None:
    """The Designation of the Employee this login is linked to, or None if
    there's no linked employee or that employee has no designation set --
    used only by can_access_people_module's deny-list check."""
    user = db.get(models.User, user_id)
    if user is None or user.employee_id is None:
        return None
    employee = db.get(models.Employee, user.employee_id)
    if employee is None or employee.designation_id is None:
        return None
    return db.get(models.Designation, employee.designation_id)


def designation_denies_action(
    db: Session, designation_id: uuid.UUID, resource: str, action: str
) -> bool:
    return action in build_designation_denials(db, designation_id).get(resource, [])


def _has_custom_people_access_policy(db: Session, company_id: uuid.UUID) -> bool:
    """True only if this company has granted at least one Role the
    'employee' resource's granular actions -- Role is the only independent
    grant path (Designation can only narrow an existing Role grant, never
    act alone), so only Role grants count as "this company has started
    configuring People access". Same role _has_custom_approval_workflow
    plays for the Approval Workflow engine: until this is True, People
    access stays exactly as open as it's always been.

    Memoized per request (PERF-05) -- see _rbac_memo."""
    memo = _rbac_memo(db)
    key = ("people_policy", company_id)
    if key not in memo:
        memo[key] = (
            db.scalar(
                select(models.RolePermission.role_id)
                .join(models.Permission, models.RolePermission.permission_id == models.Permission.id)
                .join(models.Role, models.RolePermission.role_id == models.Role.id)
                .where(models.Role.company_id == company_id, models.Permission.resource == "employee")
                .limit(1)
            )
            is not None
        )
    return memo[key]


# SEC-05/C1: People access when a company has NOT configured any
# 'employee.*' grant yet. Previously that meant "open to everyone"; it now
# fails closed to the built-in role blurbs (rbac_columns.ROLE_BLURBS): the
# Owner and HR / Recruitment Staff ("Full People ... administration") get
# every action, other non-self-service roles may only view, and self-service
# roles (IC, Associate/Intern) or users with no role get nothing -- their own
# profile stays reachable through deps.require_people_access_or_self.
_PEOPLE_ADMIN_ROLES = frozenset({"HR / Recruitment Staff"})


def _default_people_actions(role_names: set[str], all_actions: set[str]) -> set[str]:
    from .rbac_columns import SELF_SERVICE_ROLES

    if BUILTIN_ROLES[0] in role_names or role_names & _PEOPLE_ADMIN_ROLES:
        return set(all_actions)
    if not role_names or role_names <= SELF_SERVICE_ROLES:
        return set()
    return {"view"} & set(all_actions) if all_actions else {"view"}


def can_access_people_module(db: Session, user: "models.User", action: str) -> bool:
    """The single check every People-module endpoint uses. Fails closed:
    until a company grants a Role at least one 'employee' action, only the
    built-in defaults in _default_people_actions apply (was: everyone
    allowed). Once configured: Owner always passes; everyone else needs
    their Role to grant `action` AND their own Designation (if any) to not
    explicitly deny it."""
    # Employee Permissions: an admin-set People level decides for this
    # employee, whatever the company-wide policy or their role says (the
    # Owner is never restricted).
    from . import employee_permissions

    people_level = employee_permissions.level_for(db, user.employee_id, "people")
    if people_level is not None:
        role = get_user_primary_role(db, user.id)
        if role is not None and role.name == BUILTIN_ROLES[0]:
            return True
        catalog = {pa.action for pa in list_permission_actions(db) if pa.resource == "employee"} or {action}
        return action in employee_permissions.actions_for(people_level, catalog)
    if not _has_custom_people_access_policy(db, user.company_id):
        role_names = {r.name for r in get_user_roles(db, user.id)}
        return action in _default_people_actions(role_names, {action}) or action in (
            employee_access_grants(db, user.employee_id).get("employee", set())
        )
    role = get_user_primary_role(db, user.id)
    if role is not None and role.name == BUILTIN_ROLES[0]:
        return True
    if not user_has_action(db, user.id, "employee", action):
        return False
    designation = get_user_designation(db, user.id)
    if designation is not None and designation_denies_action(db, designation.id, "employee", action):
        return False
    return True


def effective_people_access(db: Session, user: "models.User") -> tuple[bool, list[str]]:
    """(configured, actions) for GET /api/me/people-access -- `actions` is
    every 'employee' action in the live catalog the calling user currently
    has, computed via the exact same rule can_access_people_module enforces
    per-action elsewhere (Owner bypass, else granted-by-any-role minus
    denied-by-designation), so this can never drift out of sync with the
    real gate.

    Deliberately NOT implemented as `can_access_people_module(db, user, a)
    for a in all_actions` (which is what this used to do): that calls
    get_user_roles/get_user_designation/build_role_actions fresh for EVERY
    action being checked (6+ in the current catalog), none of which vary
    per-action, so it repeated the same handful of DB round trips 6+ times
    for no reason -- against a real (non-local) database this endpoint,
    which every login/session-start calls once, took 20-30+ seconds,
    comfortably past the frontend's request timeout. Computing each
    per-user (not per-action) piece once and checking catalog membership
    in-memory is mathematically identical, just fast."""
    configured = _has_custom_people_access_policy(db, user.company_id)
    all_actions = {pa.action for pa in list_permission_actions(db) if pa.resource == "employee"}
    # Employee Permissions: same rule as can_access_people_module.
    from . import employee_permissions

    people_level = employee_permissions.level_for(db, user.employee_id, "people")
    if people_level is not None:
        role = get_user_primary_role(db, user.id)
        if role is not None and role.name == BUILTIN_ROLES[0]:
            return configured, sorted(all_actions)
        return configured, sorted(employee_permissions.actions_for(people_level, all_actions))
    individually_granted = employee_access_grants(db, user.employee_id).get("employee", set()) & all_actions
    if not configured:
        role_names = {r.name for r in get_user_roles(db, user.id)}
        return False, sorted(_default_people_actions(role_names, all_actions) | individually_granted)

    role = get_user_primary_role(db, user.id)
    if role is not None and role.name == BUILTIN_ROLES[0]:
        return True, sorted(all_actions)

    granted: set[str] = set()
    for r in get_user_roles(db, user.id):
        granted |= set(build_role_actions(r).get("employee", []))

    denied: set[str] = set()
    designation = get_user_designation(db, user.id)
    if designation is not None:
        denied = set(build_designation_denials(db, designation.id).get("employee", []))

    return True, sorted((all_actions & (granted | individually_granted)) - denied)


def permission_satisfied(
    db: Session, user: "models.User", matrix_column: str, resource: str, action: str
) -> bool:
    """True if `user`'s primary role already satisfies the old matrix
    column (Edit/Admin, same rule require_permission uses), OR if any role
    they hold has the granular (resource, action) grant -- purely additive:
    every caller who could already pass the old check still can, and a
    caller with only the new granular grant now also can. Used at the
    handful of endpoints where a require_permission gate is being widened
    to also accept the new catalog, without removing the original check."""
    if effective_user_matrix(db, user).get(matrix_column) in ("e", "a"):
        return True
    return user_has_action(db, user.id, resource, action)


def can_submit_self_service_request(
    db: Session,
    user: "models.User",
    employee_id: uuid.UUID,
    matrix_column: str,
    resource: str,
    action: str,
) -> bool:
    """Guards a "self-service create" endpoint (leave, clock-in, work entry,
    reimbursement, ...) where payload.employee_id normally equals the
    caller's own. Acting ON BEHALF of another employee (SEC-03 / E1) is
    allowed only when ALL of these hold:
      1. the target employee exists in the caller's own company (C14);
      2. the caller is not a built-in self-service role (IC, Associate);
      3. one of:
         a. the caller is the Organization Owner / CEO;
         b. the caller's effective matrix holds `matrix_column` at 'a'
            (Admin = company-wide authority for that domain, e.g. HR on
            leave_approval), or
         c. the caller holds `matrix_column` at 'e' or the granular
            (resource, action) grant AND the target is in the caller's
            reporting subtree (Reporting / Dotted-Line manager anywhere up
            the chain, crud.is_manager_of_employee) -- a manager acting for
            their own team, never for an unrelated employee."""
    if employee_id == user.employee_id:
        return True
    from .rbac_columns import SELF_SERVICE_ROLES

    target = db.get(models.Employee, employee_id)
    if target is None or target.company_id != user.company_id:
        return False
    role = get_user_primary_role(db, user.id)
    if role is None:
        return False
    if role.name == BUILTIN_ROLES[0]:
        return True
    from . import employee_permissions

    has_override = employee_permissions.level_for(
        db, user.employee_id, employee_permissions.COLUMN_MODULE.get(matrix_column)) is not None
    # Self-service roles act only for themselves -- unless an admin set this
    # employee's own level for the module (Employee Permissions).
    if role.name in SELF_SERVICE_ROLES and not has_override:
        return False
    level = effective_user_matrix(db, user).get(matrix_column, "n")
    if level == "a":
        return True
    if level == "e" or user_has_action(db, user.id, resource, action):
        return user.employee_id is not None and is_manager_of_employee(
            db, user.employee_id, employee_id
        )
    return False


PLATFORM_SUPER_ADMIN_ROLE = "super_admin"


def is_reserved_platform_role_name(name: str | None) -> bool:
    """SA-01: "super_admin" (any case/spacing/separator) is the platform
    console's role and must never be usable as a tenant role name."""
    return re.sub(r"[\s_\-]+", "_", (name or "").strip().lower()) == PLATFORM_SUPER_ADMIN_ROLE


def is_platform_super_admin(db: Session, admin: models.AdminUser | None) -> bool:
    """SA-01: a real platform operator is an admin_users row with role
    super_admin AND no tenant login (public.users). Every tenant user also
    has an admin_users row (create_admin_user_login), so the role string
    alone must never grant platform access."""
    if admin is None or not admin.is_active or admin.role != PLATFORM_SUPER_ADMIN_ROLE:
        return False
    return db.scalar(select(models.PublicUser.id).where(models.PublicUser.id == admin.id)) is None


def find_admin_user_by_email(db: Session, email: str) -> models.AdminUser | None:
    """Looks up the global login record in public.admin_users by email."""
    normalized = email.strip().lower()
    return db.scalar(
        select(models.AdminUser).where(
            func.lower(models.AdminUser.email) == normalized
        )
    )


def find_public_user_by_email(db: Session, email: str) -> models.PublicUser | None:
    """Looks up the tenant routing record in public.users by email."""
    normalized = email.strip().lower()
    return db.scalar(
        select(models.PublicUser).where(
            func.lower(models.PublicUser.email) == normalized
        )
    )


def get_core_user_by_employee_id(db: Session, employee_id: uuid.UUID) -> models.User | None:
    """Looks up the tenant-schema application user by employee_id (core_users.employee_id)."""
    return db.scalar(select(models.User).where(models.User.employee_id == employee_id))


def get_role_permission_codes(db: Session, role_id: uuid.UUID) -> list[str]:
    """All permission codes granted to a role via core_role_permissions + core_permissions."""
    return list(
        db.scalars(
            select(models.Permission.code)
            .join(models.RolePermission, models.RolePermission.permission_id == models.Permission.id)
            .where(models.RolePermission.role_id == role_id)
        ).all()
    )


def get_or_create_tenant(db: Session, slug: str, name: str) -> models.Tenant:
    """Ensures a public.tenants row exists for the given slug."""
    tenant = db.scalar(select(models.Tenant).where(models.Tenant.slug == slug))
    if tenant is not None:
        return tenant
    tenant = models.Tenant(id=uuid.uuid4(), slug=slug, name=name)
    db.add(tenant)
    db.flush()
    return tenant


def create_admin_user_login(
    db: Session,
    user_id: uuid.UUID,
    email: str,
    password_hash: str,
    full_name: str | None,
    role_name: str,
    tenant_slug: str,
    company_id: uuid.UUID,
    employee_id: uuid.UUID | None = None,
) -> None:
    """Writes login credentials to public.admin_users (global auth) and
    public.users (tenant routing + auth). employee_id links the auth account
    to the tenant's core_employees row (separate from the user account id)."""
    existing_admin = db.scalar(select(models.AdminUser).where(models.AdminUser.id == user_id))
    if existing_admin is None:
        db.add(models.AdminUser(
            id=user_id,
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            # SA-01: never let a tenant role name become the platform role.
            role="tenant_user" if is_reserved_platform_role_name(role_name) else role_name[:30],
        ))

    existing_public = db.scalar(select(models.PublicUser).where(models.PublicUser.id == user_id))
    if existing_public is None:
        db.add(models.PublicUser(
            id=user_id,
            email=email,
            password_hash=password_hash,
            tenant_slug=tenant_slug,
            company_id=company_id,
            role=role_name,
            full_name=full_name,
            employee_id=employee_id,
        ))
    db.flush()


def find_user_by_email(db: Session, email: str) -> models.User | None:
    """Resolves a login/lookup email to its user, treating a linked
    employee's live core.employees.work_email as authoritative over the
    core.users.email copy taken at account-creation time. That copy is
    never updated after creation, so without this join a work_email edit
    (however it happens -- future edit UI, admin, direct DB update) would
    silently stop matching. Falls back to core.users.email for the rare
    account with no linked employee row."""
    normalized = email.strip().lower()
    return db.scalar(
        select(models.User)
        .outerjoin(models.Employee, models.User.employee_id == models.Employee.id)
        .where(
            func.lower(func.coalesce(models.Employee.work_email, models.User.email))
            == normalized
        )
    )


def get_user_primary_role(db: Session, user_id: uuid.UUID) -> models.Role | None:
    """A user can hold more than one role (e.g. one per branch via
    core.user_roles), but the frontend's session model is "acting as a
    single role" -- mirrors the existing RoleSwitcher behavior, so we just
    take the first one assigned.

    Eager-loads role_permissions.permission -- this is what require_permission
    calls on nearly every protected endpoint, so build_role_matrix's per-row
    `.permission` lazy-load (previously one DB round trip per grant, every
    single request) was adding multi-second overhead to any request made by
    a user whose role has more than a handful of grants.

    C8: "first" is now deterministic -- USER_ROLE_ORDER (earliest
    assignment, then role_id), the same order get_user_roles returns and
    auth.py's login takes roles[0] from, so the acting role can no longer
    change between requests for a multi-role user. Shares
    get_user_roles' per-request memo (PERF-05)."""
    rows = _user_role_rows(db, user_id)
    return rows[0].role if rows else None


def get_user_by_id(db: Session, user_id: uuid.UUID) -> models.User | None:
    return db.get(models.User, user_id)


_TRACKED_TRANSFER_FIELDS = [
    "branch_id", "business_unit_id", "department_id", "sub_department_id",
    "reporting_manager_id", "dotted_line_manager_id",
]
_ORG_UNIT_TRANSFER_FIELDS = {"branch_id", "business_unit_id", "department_id", "sub_department_id"}


def record_employee_change(
    db: Session,
    employee: models.Employee,
    old_values: dict,
    new_values: dict,
) -> models.EmployeeLifecycleEvent | None:
    """Writes one hcm.employee_lifecycle_events row for a branch/business
    unit/department/sub-department transfer and/or a reporting/dotted-line
    manager change -- the two are recorded as one row when both happen
    together (e.g. a transfer that also reassigns the manager), tagged
    'transfer' if any org-unit field changed, else 'reporting_change'.
    Returns None (writes nothing) if none of the tracked fields in
    `new_values` actually differ from `old_values` -- callers pass only the
    fields they're actually changing in `new_values`; a field absent there
    is treated as untouched, not cleared. History rows are append-only: this
    function only ever INSERTs, matching "transfer history must never be
    overwritten or deleted"."""
    touched = [f for f in _TRACKED_TRANSFER_FIELDS if f in new_values]
    changed = [f for f in touched if old_values.get(f) != new_values.get(f)]
    if not changed:
        return None

    event_type = "transfer" if any(f in _ORG_UNIT_TRANSFER_FIELDS for f in changed) else "reporting_change"
    event = models.EmployeeLifecycleEvent(
        id=uuid.uuid4(),
        employee_id=employee.id,
        event_type=event_type,
        event_date=company_today(db, employee.company_id),
        from_branch_id=old_values.get("branch_id"),
        to_branch_id=new_values.get("branch_id", old_values.get("branch_id")),
        from_business_unit_id=old_values.get("business_unit_id"),
        to_business_unit_id=new_values.get("business_unit_id", old_values.get("business_unit_id")),
        from_department_id=old_values.get("department_id"),
        to_department_id=new_values.get("department_id", old_values.get("department_id")),
        from_sub_department_id=old_values.get("sub_department_id"),
        to_sub_department_id=new_values.get("sub_department_id", old_values.get("sub_department_id")),
        from_reporting_manager_id=old_values.get("reporting_manager_id"),
        to_reporting_manager_id=new_values.get("reporting_manager_id", old_values.get("reporting_manager_id")),
        from_dotted_line_manager_id=old_values.get("dotted_line_manager_id"),
        to_dotted_line_manager_id=new_values.get(
            "dotted_line_manager_id", old_values.get("dotted_line_manager_id")
        ),
    )
    db.add(event)
    db.flush()
    return event


def can_decide_request(
    db: Session, current_user: models.User, requester_employee_id: uuid.UUID
) -> bool:
    """Reporting Manager Approval Workflow's core authorization rule: only
    someone in the requester's actual upward reporting chain (resolved live
    here, never a client-supplied approver id) may Approve/Reject/Send Back
    their request -- not just anyone holding Edit/Admin on the relevant RBAC
    column, which was the previous (too broad) behavior.

    Walks the full chain (via _is_in_reporting_chain), not just the direct
    manager -- the Approvals inbox shows a request to every manager in the
    requester's subtree (BFS, see approvals_screen.dart's _reportingSubtree),
    so a direct-only check here meant a GM or Branch Manager saw requests
    from employees several rungs below them but got a 403 clicking Approve.
    can_act_on_travel_request already used the full-chain check for travel
    requests specifically; this makes every other doctype gated by
    can_decide_request (leave, attendance regularization, work entry,
    timesheet, overtime, reimbursement, salary revision, asset request)
    consistent with it instead of silently narrower.

    Organization Owner / CEO and System Settings / RBAC admins keep their
    existing platform-wide override, mirroring require_permission's own
    Owner bypass and the same admin-override convention already used
    elsewhere in this app (e.g. Edit Project's
    canAct('System Settings / RBAC') check) -- EXCEPT self-approval, which
    is never permitted, not even for those roles: checked first, before any
    override, so an Owner/System Admin who submits their own leave/
    regularization/travel/etc. request can never be routed to themselves as
    the decider (see also approval_engine.can_act's identical guard for the
    configurable multi-step engine)."""
    if current_user.employee_id is not None and current_user.employee_id == requester_employee_id:
        return False
    # SEC-09 / C14: never across companies -- a schema can hold several
    # companies, and the Owner / RBAC-admin override below is company-wide,
    # not schema-wide.
    requester = db.get(models.Employee, requester_employee_id)
    if requester is None or requester.company_id != current_user.company_id:
        return False
    if is_fallback_approver(db, current_user):
        return True
    if current_user.employee_id is None:
        return False
    return _is_in_reporting_chain(db, current_user.employee_id, requester_employee_id)


def is_fallback_approver(db: Session, user: models.User) -> bool:
    """WF-03: the same company-wide override can_decide_request grants --
    Organization Owner / CEO or a System Settings / RBAC editor/admin. These
    are the approvers a request falls back to when its requester has no
    Reporting Manager (e.g. the CEO). Callers still apply the self-approval
    guard separately. Also anyone an admin gave Approve / Admin on the
    Approvals module (Employee Permissions)."""
    from . import employee_permissions

    role = get_user_primary_role(db, user.id)
    if role is not None and role.name == BUILTIN_ROLES[0]:
        return True
    if employee_permissions.level_for(db, user.employee_id, "approvals") in ("approve", "admin"):
        return True
    if role is None:
        return False
    return effective_user_matrix(db, user).get("system_settings_rbac") in ("e", "a")


def get_visible_employee_ids_for_requests(
    db: Session, current_user: models.User, doctype: str | None = None
) -> list[uuid.UUID] | None:
    """Request-list (leave/regularization/overtime/travel/expense) scope:
    everyone whose requests the caller may decide, so a request is never
    decidable but missing from the caller's list. The union of:
      - get_visible_employee_ids_for_docs (None = whole company);
      - the caller's reporting subtree (Reporting and Dotted-Line, any
        depth) -- can_decide_request honours it for every role, but the
        docs scope gives HR staff only their HR departments;
      - for `doctype`'s custom approval workflow, every employee a step
        names the caller for (approval_engine: dotted-line manager, branch
        manager, department / BU head, or the caller's role / user);
      - for a fallback approver (is_fallback_approver), every employee with
        no reporting or dotted-line manager, whose requests are routed to
        them (WF-03). Without this an HR admin was notified of the CEO's
        leave but couldn't see it to decide it.
    An Employee Permissions level on `doctype`'s module decides the scope
    instead: Self Only / No Access = own requests, anything above = all."""
    from . import employee_permissions

    scope = employee_permissions.sees_all_records(employee_permissions.level_for(
        db, current_user.employee_id, employee_permissions.DOCTYPE_MODULE.get(doctype or "")))
    if scope is True:
        return None
    if scope is False:
        return [current_user.employee_id] if current_user.employee_id else []
    # M-27: Travel & Expense Approval at Admin (e.g. Finance / Payroll) or
    # View (e.g. HR, read-only) is a company-wide level for travel / expense
    # lists; Edit (Managers) keeps the team scope below.
    if doctype in ("travel_request", "expense_claim", "expense_report") and effective_user_matrix(
        db, current_user
    ).get("travel_expense_approval") in ("a", "v"):
        return None
    visible = get_visible_employee_ids_for_docs(db, current_user)
    if visible is None or current_user.employee_id is None:
        return visible
    ids = {*visible, *_reporting_subtree_ids(db, current_user.employee_id, current_user.company_id)}
    if doctype is not None:
        workflow_ids = _workflow_step_employee_ids(db, current_user, doctype)
        if workflow_ids is None:
            return None
        ids |= workflow_ids
    if is_fallback_approver(db, current_user):
        ids.update(db.scalars(
            select(models.Employee.id).where(
                models.Employee.company_id == current_user.company_id,
                models.Employee.reporting_manager_id.is_(None),
                models.Employee.dotted_line_manager_id.is_(None),
            )
        ).all())
    return list(ids)


def _workflow_step_employee_ids(
    db: Session, current_user: models.User, doctype: str
) -> set[uuid.UUID] | None:
    """Employees whose `doctype` requests name `current_user` at some step
    of the company's custom approval workflow -- the set-valued twin of
    approval_engine._step_matches, over every step (not only the current
    one) so a later-stage approver sees the request before their turn.
    None = every employee (a 'role' or 'user' step matching the caller).
    Empty when the company uses the default reporting-manager rule, which
    the reporting subtree already covers."""
    from . import approval_engine

    workflow = approval_engine.get_active_workflow(db, current_user.company_id, doctype)
    if not _is_custom_workflow(workflow):
        return set()
    me = current_user.employee_id
    company_id = current_user.company_id
    E = models.Employee
    ids: set[uuid.UUID] = set()
    role = None
    for step in workflow.steps:
        kind = step.approver_type
        if kind == "user":
            if step.user_id == current_user.id:
                return None
        elif kind == "role":
            if role is None:
                role = get_user_primary_role(db, current_user.id)
            if role is not None and step.role_id == role.id:
                return None
        elif kind in ("reporting_manager", "dotted_line_manager"):
            col = E.reporting_manager_id if kind == "reporting_manager" else E.dotted_line_manager_id
            ids.update(db.scalars(select(E.id).where(E.company_id == company_id, col == me)).all())
        elif kind == "branch_manager":
            ids.update(db.scalars(
                select(E.id).join(models.Branch, models.Branch.id == E.branch_id)
                .where(E.company_id == company_id, models.Branch.branch_manager_id == me)
            ).all())
        elif kind == "department_head":
            ids.update(db.scalars(
                select(E.id).join(models.Department, models.Department.id == E.department_id)
                .where(E.company_id == company_id, models.Department.head_employee_id == me)
            ).all())
        elif kind == "business_unit_head":
            ids.update(db.scalars(
                select(E.id)
                .join(models.Department, models.Department.id == E.department_id)
                .join(models.BusinessUnit, models.BusinessUnit.id == models.Department.business_unit_id)
                .where(E.company_id == company_id, models.BusinessUnit.head_employee_id == me)
            ).all())
    return ids


def list_fallback_approver_employee_ids(
    db: Session, company_id: uuid.UUID, exclude_employee_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """WF-03: employees of `company_id` who can decide a request that has no
    Reporting Manager to route to (is_fallback_approver), minus the
    requester -- used to notify someone instead of rejecting the request."""
    users = db.scalars(
        select(models.User).where(
            models.User.company_id == company_id,
            models.User.employee_id.is_not(None),
            models.User.status == "active",
        )
    ).all()
    return sorted(
        {
            u.employee_id
            for u in users
            if u.employee_id != exclude_employee_id and is_fallback_approver(db, u)
        },
        key=str,
    )


def _has_custom_approval_workflow(db: Session, company_id: uuid.UUID, doctype: str) -> bool:
    """True only if this company has defined a REAL custom workflow for
    doctype -- more than one step, or a single step whose approver_type
    isn't the trivial 'reporting_manager' default. Distinguishes an
    admin-configured chain from approval_engine.get_or_create_default_workflow's
    auto-provisioned single step, so callers can cheaply skip the
    stateful ApprovalRequest path entirely when nothing has been
    configured (today's exact behavior, zero extra DB writes)."""
    from . import approval_engine

    memo = _rbac_memo(db)
    key = ("custom_workflow", company_id, doctype)
    if key in memo:
        return memo[key]
    memo[key] = _has_custom_approval_workflow_uncached(approval_engine, db, company_id, doctype)
    return memo[key]


def prime_custom_workflow_memo(db: Session, company_id: uuid.UUID, doctypes: Iterable[str]) -> None:
    """Fills _has_custom_approval_workflow's per-request memo for several
    doctypes with ONE query (workflows + steps) -- for the Approvals inbox,
    which checks up to ten doctypes in a single request."""
    memo = _rbac_memo(db)
    wanted = [d for d in dict.fromkeys(doctypes) if ("custom_workflow", company_id, d) not in memo]
    if not wanted:
        return
    workflows = db.scalars(
        select(models.ApprovalWorkflow)
        .options(selectinload(models.ApprovalWorkflow.steps))
        .where(
            models.ApprovalWorkflow.company_id == company_id,
            models.ApprovalWorkflow.doctype.in_(wanted),
            models.ApprovalWorkflow.is_active.is_(True),
        )
    ).all()
    by_doctype = {w.doctype: w for w in workflows}
    for doctype in wanted:
        memo[("custom_workflow", company_id, doctype)] = _is_custom_workflow(by_doctype.get(doctype))
        memo[("active_workflow", company_id, doctype)] = by_doctype.get(doctype)


def prime_approval_requests(db: Session, company_id: uuid.UUID, pairs: Iterable[tuple[str, uuid.UUID]]) -> None:
    """One query for the engine's existing ApprovalRequest rows of many
    (doctype, document_id) pairs -- used by can_decide_request_readonly."""
    memo = _rbac_memo(db)
    pairs = [p for p in dict.fromkeys(pairs) if ("approval_request",) + p not in memo]
    if not pairs:
        return
    found = db.scalars(
        select(models.ApprovalRequest).where(
            models.ApprovalRequest.company_id == company_id,
            models.ApprovalRequest.document_id.in_([d for _t, d in pairs]),
        )
    ).all()
    by_key = {(r.doctype, r.document_id): r for r in found}
    for pair in pairs:
        memo[("approval_request",) + pair] = by_key.get(pair)


def _has_custom_approval_workflow_uncached(approval_engine, db: Session, company_id: uuid.UUID, doctype: str) -> bool:
    return _is_custom_workflow(approval_engine.get_active_workflow(db, company_id, doctype))


def _is_custom_workflow(workflow: "models.ApprovalWorkflow | None") -> bool:
    if workflow is None:
        return False
    steps = sorted(workflow.steps, key=lambda s: s.step_order)
    # A workflow row with zero steps is a broken/incomplete configuration,
    # not an intentional custom chain -- treating it as "custom" (as a bare
    # `len(steps) != 1` would) routes every decision into approval_engine
    # with no step to ever satisfy, permanently 403'ing every reporting
    # manager (including Owner/admin) for this doctype. Fall back to the
    # default reporting-manager rule instead, matching this function's own
    # documented intent ("more than one step, or a single non-default
    # step") which zero steps satisfies neither of.
    if not steps:
        return False
    return len(steps) != 1 or steps[0].approver_type != "reporting_manager"


def can_decide_request_configurable(
    db: Session,
    current_user: models.User,
    requester_employee_id: uuid.UUID,
    doctype: str,
    document_id: uuid.UUID,
) -> bool:
    """Configurable-workflow-aware sibling of can_decide_request: if this
    company hasn't defined a custom approval_workflows row for `doctype`,
    delegates to can_decide_request unchanged (byte-for-byte today's
    behavior, no new rows written). Only once an admin has configured a
    real multi-step/non-default chain for this doctype does this switch to
    the stateful approval_engine (lazily creating the ApprovalRequest for
    `document_id` the first time it's checked)."""
    company_id = current_user.company_id
    if not _has_custom_approval_workflow(db, company_id, doctype):
        return can_decide_request(db, current_user, requester_employee_id)

    from . import approval_engine

    requester = db.get(models.Employee, requester_employee_id)
    if requester is None or current_user.employee_id is None:
        return False
    request = db.scalar(
        select(models.ApprovalRequest).where(
            models.ApprovalRequest.company_id == company_id,
            models.ApprovalRequest.doctype == doctype,
            models.ApprovalRequest.document_id == document_id,
        )
    )
    if request is None:
        request = approval_engine.start_request(
            db, company_id, doctype, document_id, requester_employee_id
        )
    return approval_engine.can_act(db, request, current_user, requester)


def can_decide_request_readonly(
    db: Session,
    current_user: models.User,
    requester_employee_id: uuid.UUID,
    doctype: str,
    document_id: uuid.UUID,
) -> bool:
    """can_decide_request_configurable without side effects, for listing
    (the Approvals inbox's per-row `can_decide`): where the decide path
    would lazily create the engine's ApprovalRequest, an unsaved in-memory
    equivalent is evaluated instead -- nothing is written."""
    if current_user.employee_id is not None and current_user.employee_id == requester_employee_id:
        return False
    company_id = current_user.company_id
    if not _has_custom_approval_workflow(db, company_id, doctype):
        return can_decide_request(db, current_user, requester_employee_id)

    from . import approval_engine

    requester = db.get(models.Employee, requester_employee_id)
    if requester is None or current_user.employee_id is None:
        return False
    memo = _rbac_memo(db)
    key = ("approval_request", doctype, document_id)
    if key in memo:
        request = memo[key]
    else:
        request = db.scalar(
            select(models.ApprovalRequest).where(
                models.ApprovalRequest.company_id == company_id,
                models.ApprovalRequest.doctype == doctype,
                models.ApprovalRequest.document_id == document_id,
            )
        )
    if request is None:
        # What start_request would create -- built in memory only, never
        # added to the session.
        workflow_key = ("active_workflow", company_id, doctype)
        workflow = memo[workflow_key] if workflow_key in memo else approval_engine.get_active_workflow(
            db, company_id, doctype
        )
        if workflow is None:
            return False
        request = models.ApprovalRequest(
            company_id=company_id, workflow_id=workflow.id, doctype=doctype,
            document_id=document_id, status="pending", current_step=1,
            requested_by=requester_employee_id,
        )
    return approval_engine.can_act(db, request, current_user, requester)


def decide_configurable_request(
    db: Session,
    current_user: models.User,
    requester_employee_id: uuid.UUID,
    doctype: str,
    document_id: uuid.UUID,
    decision: str,
    comments: str | None = None,
) -> str:
    """Call after can_decide_request_configurable has authorized the actor
    for the current step. Returns the effective status the calling
    module's own row should be set to.

    When this company hasn't defined a real custom workflow for `doctype`,
    returns `decision` unchanged -- zero new writes, byte-for-byte today's
    single-step behavior. Once a real multi-step chain is configured,
    advances the engine's ApprovalRequest one step (approval_engine.
    record_action) and returns its resulting status: 'pending' while more
    steps remain, or the terminal 'approved'/'rejected'. Callers should only
    treat the terminal values as final (e.g. only fire "decision" side
    effects like leave-balance adjustment or the requester notification
    once the returned status is no longer 'pending')."""
    company_id = current_user.company_id
    if not _has_custom_approval_workflow(db, company_id, doctype):
        return decision

    from . import approval_engine

    request = db.scalar(
        select(models.ApprovalRequest).where(
            models.ApprovalRequest.company_id == company_id,
            models.ApprovalRequest.doctype == doctype,
            models.ApprovalRequest.document_id == document_id,
        )
    )
    if request is None:
        # Defensive only -- can_decide_request_configurable's own check
        # (which every caller runs first) already lazily creates this row.
        request = approval_engine.start_request(
            db, company_id, doctype, document_id, requester_employee_id
        )
    step_before = request.current_step
    status_before = request.status
    try:
        updated = approval_engine.record_action(db, request, current_user, decision, comments)
    except approval_engine.InvalidDecision as exc:
        # WF-02: unknown / illegal decisions are a client error, never an
        # accidental approval (or a 500).
        from fastapi import HTTPException

        code = 409 if "already" in str(exc) else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    # Sequential chain: this approval moved the request to the next step --
    # now (and only now) tell that step's approver(s) it is their turn.
    # M-09: a sent-back request decided again restarts at step 1 and may
    # land on the step it was sent back from -- still a new turn.
    if (updated.status == "pending"
            and (updated.current_step != step_before or status_before == "sent_back")
            and doctype not in _SELF_NOTIFYING_DOCTYPES):
        requester = db.get(models.Employee, requester_employee_id)
        if requester is not None:
            entity_type, label = DOCTYPE_NOTICE.get(doctype, (doctype, doctype.replace("_", " ").title()))
            try:
                notify_new_request(db, company_id, requester, label, entity_type, document_id)
            except Exception:  # a notification must never undo a recorded approval
                import logging

                logging.getLogger(__name__).exception("next-step approval notification failed")
    return updated.status


def _get_or_start_approval_request(
    db: Session, company_id: uuid.UUID, doctype: str, document_id: uuid.UUID,
    requester_employee_id: uuid.UUID,
) -> "models.ApprovalRequest":
    from . import approval_engine

    request = db.scalar(
        select(models.ApprovalRequest).where(
            models.ApprovalRequest.company_id == company_id,
            models.ApprovalRequest.doctype == doctype,
            models.ApprovalRequest.document_id == document_id,
        )
    )
    if request is None:
        request = approval_engine.start_request(db, company_id, doctype, document_id, requester_employee_id)
    return request


def _is_in_reporting_chain(
    db: Session,
    manager_id: uuid.UUID,
    employee_id: uuid.UUID,
    max_depth: int = 10,
) -> bool:
    """Returns True if manager_id is the direct reporting_manager_id OR
    dotted_line_manager_id of employee_id, OR any ancestor reached by
    following reporting_manager_id upward through the chain (up to
    max_depth hops).

    Two Reporting Managers: every employee may have a primary
    reporting_manager_id and a secondary dotted_line_manager_id, both
    treated as equally authorized deciders for that employee's own
    requests (checked at every level of the upward walk, so an ancestor's
    dotted-line manager also qualifies, same as their primary chain
    already did). The upward walk itself still follows reporting_manager_id
    only -- dotted-line is a direct co-manager relationship, not a second
    chain to climb.

    Used by can_act_on_travel_request so that branch-level managers can
    approve travel for employees whose *direct* reporting_manager_id is a
    team lead below them — same visibility logic the frontend's
    reportingSubtreeIds BFS uses, applied on the backend."""
    current_id: uuid.UUID | None = employee_id
    for _ in range(max_depth):
        emp = db.get(models.Employee, current_id)
        if emp is None:
            return False
        if emp.reporting_manager_id == manager_id or emp.dotted_line_manager_id == manager_id:
            return True
        if emp.reporting_manager_id is None:
            return False
        current_id = emp.reporting_manager_id
    return False


def is_manager_of_employee(
    db: Session, manager_employee_id: uuid.UUID, employee_id: uuid.UUID
) -> bool:
    """Public name for _is_in_reporting_chain, for authorization call sites
    outside the approval-decision flow (e.g. deps.py's
    require_people_access_or_self_or_manager) -- same Reporting Manager /
    Dotted-Line Manager / upward-chain definition, just used to gate direct
    field edits instead of request decisions."""
    return _is_in_reporting_chain(db, manager_employee_id, employee_id)


def decided_by_manager_type(
    requester: models.Employee | None, approver_id: uuid.UUID | None
) -> str | None:
    """Two Reporting Managers cross-visibility: classifies who actually
    decided a request relative to the REQUESTER'S OWN two direct managers
    (reporting_manager_id and dotted_line_manager_id), so each of those two
    managers can immediately tell -- from the decision itself, not just the
    status -- whether it was the OTHER one of them (rather than a
    higher-level manager reached via _is_in_reporting_chain's upward walk,
    or an Owner/RBAC-admin override) who acted.

    None until decided (approver_id is None). "Other Approver" covers every
    decider who isn't literally one of the requester's own two direct
    managers -- still a fully legitimate decision (can_decide_request
    already authorized it), just not the specific "other manager" this
    feature is about surfacing."""
    if approver_id is None or requester is None:
        return None
    if approver_id == requester.reporting_manager_id:
        return "Reporting Manager"
    if approver_id == requester.dotted_line_manager_id:
        return "Dotted-Line Manager"
    return "Other Approver"


def can_act_on_travel_request(
    db: Session, current_user: models.User, requester_employee_id: uuid.UUID
) -> bool:
    """Kept as a distinctly-named alias so travel.py's call site stays
    self-documenting -- can_decide_request now performs the exact same
    full-chain check for every other doctype, so this simply delegates."""
    return can_decide_request(db, current_user, requester_employee_id)


# Role sets for document-visibility scoping — kept in sync with BUILTIN_ROLES
# in rbac_columns.py so adding a new admin-tier role only needs one edit.
_DOC_ADMIN_ROLES = frozenset({
    # Fixed: was 'System Admin / Owner', which matches no real role name in
    # BUILTIN_ROLES (rbac_columns.py) -- silently excluded both of these
    # top-tier roles from full document visibility since this dict was
    # introduced. The real names are 'Organization Owner / CEO' and
    # 'IT / System Admin'.
    'Organization Owner / CEO', 'IT / System Admin', 'Branch Head', 'Branch Manager',
    'General Manager / Sr. Manager',
})
_DOC_HR_ROLES = frozenset({'HR / Recruitment Staff'})


def _reporting_subtree_ids(
    db: Session, manager_id: uuid.UUID, company_id: uuid.UUID, max_depth: int = 10
) -> set[uuid.UUID]:
    """Every employee reachable from manager_id by following
    reporting_manager_id OR dotted_line_manager_id downward, at any depth
    (capped at max_depth hops as a guard against a corrupted cyclic chain --
    same cap _is_in_reporting_chain uses for the upward walk). Two
    Reporting Managers: mirrors _is_in_reporting_chain's upward walk (which
    checks both fields at every level), so a manager's subtree here always
    matches exactly who is authorized to decide their requests. A single
    recursive CTE rather than N queries.

    DB-02: the manager edges are expressed as a UNION ALL of the two
    manager columns (child -> parent) instead of joining on
    `reporting_manager_id = x OR dotted_line_manager_id = x`, which forced
    a sequential scan at every recursion level. Each branch of the edge
    union can use its own index (ix_emp_reporting_manager /
    ix_emp_dotted_line_manager, backend/db/fix_2026_09_24_db02_indexes.sql).
    Same result set: a child reachable through either column, company-
    scoped on the child row, depth-capped at max_depth."""
    E = models.Employee.__table__
    edges = (
        select(E.c.id.label("id"), E.c.reporting_manager_id.label("parent_id"))
        .where(E.c.company_id == company_id, E.c.reporting_manager_id.isnot(None))
        .union_all(
            select(E.c.id.label("id"), E.c.dotted_line_manager_id.label("parent_id"))
            .where(E.c.company_id == company_id, E.c.dotted_line_manager_id.isnot(None))
        )
        .cte(name="manager_edges")
    )
    base = select(edges.c.id, literal(1).label("depth")).where(edges.c.parent_id == manager_id)
    subtree = base.cte(name="subtree", recursive=True)
    recursive = (
        select(edges.c.id, (subtree.c.depth + 1).label("depth"))
        .select_from(edges.join(subtree, edges.c.parent_id == subtree.c.id))
        .where(subtree.c.depth < max_depth)
    )
    subtree = subtree.union(recursive)
    return set(db.scalars(select(subtree.c.id)).all())


def get_visible_employee_ids_for_docs(
    db: Session, current_user: models.User
) -> list[uuid.UUID] | None:
    """Compute which employees' documents the current user may see or upload.

    Returns None  → unrestricted (all company employees).
    Returns []    → service account with no employee row (no access).
    Returns [ids] → only those employees' documents are accessible.

    Tiers:
      Admin-level (System Admin, Branch Head, Branch Manager, GM/Sr. Manager)
        → None (see everything).
      HR / Recruitment Staff
        → employees in departments they are the HR representative for, plus self.
      Everyone else (Team Lead, Project Manager, Reporting Manager, IC, Associate)
        → their full downward reporting subtree (via reporting_manager_id,
        any depth) plus self -- matches can_decide_request's already-full-
        chain decision authorization and the frontend Approvals inbox's
        own subtree BFS, instead of the direct-reports-only scope this
        function used before.
        IC employees with no reports get [self] only (empty subtree).
    """
    if current_user.employee_id is None:
        return []

    # Employee Permissions: an admin-set Documents level decides first.
    from . import employee_permissions

    scope = employee_permissions.sees_all_records(
        employee_permissions.level_for(db, current_user.employee_id, "documents"))
    if scope is not None:
        return None if scope else [current_user.employee_id]

    role = get_employee_role_name(db, current_user.employee_id)

    if role in _DOC_ADMIN_ROLES:
        return None

    # Employee Module Access: an individual Documents grant (Administration >
    # Roles & Permissions) opens every employee's documents to this person,
    # on top of their role -- the grant had no effect before.
    if employee_access_grants(db, current_user.employee_id).get("documents"):
        return None

    if role in _DOC_HR_ROLES:
        # M-10: HR Representative assignments narrow HR to their departments
        # only where they exist. HR who represents no department falls back
        # to their People scope (an Employee Permissions People level, else
        # org-wide like get_people_directory_visible_ids); HR who does also
        # covers every employee whose department has no HR representative.
        rep_dept_ids = set(db.scalars(
            select(models.Department.id).where(
                models.Department.company_id == current_user.company_id,
                models.Department.hr_representative_id == current_user.employee_id,
            )
        ))
        if not rep_dept_ids:
            people_scope = employee_permissions.sees_all_records(
                employee_permissions.level_for(db, current_user.employee_id, "people"))
            return [current_user.employee_id] if people_scope is False else None
        unrepresented = select(models.Department.id).where(
            models.Department.company_id == current_user.company_id,
            models.Department.hr_representative_id.is_(None),
        )
        hr_emp_ids = list(db.scalars(
            select(models.Employee.id).where(
                models.Employee.company_id == current_user.company_id,
                or_(
                    models.Employee.department_id.in_(rep_dept_ids),
                    models.Employee.department_id.in_(unrepresented),
                    models.Employee.department_id.is_(None),
                ),
            )
        ).all())
        visible: set[uuid.UUID] = {*hr_emp_ids, current_user.employee_id}
        return list(visible)

    # All other roles: full downward subtree (via reporting_manager_id,
    # any depth) + self -- matches can_decide_request's already-full-chain
    # authorization and the frontend Approvals inbox's own subtree BFS.
    # IC/Associate employees with no reports get {self} only (empty subtree).
    subtree_ids = _reporting_subtree_ids(db, current_user.employee_id, current_user.company_id)
    return list({*subtree_ids, current_user.employee_id})


def _is_people_or_payroll_admin(db: Session, user: models.User) -> bool:
    """Owner, HR / Recruitment Staff (People administrators), or a
    non-self-service role explicitly holding payroll_process at e/a."""
    from . import employee_permissions
    from .rbac_columns import SELF_SERVICE_ROLES

    names = {r.name for r in get_user_roles(db, user.id)}
    if BUILTIN_ROLES[0] in names:
        return True
    # An admin-set Payroll level for this employee decides (create and
    # above = payroll processing); otherwise the role rules below apply.
    payroll_level = employee_permissions.level_for(db, user.employee_id, "payroll")
    if payroll_level is not None:
        return employee_permissions.matrix_level(payroll_level) in ("e", "a")
    if names & _PEOPLE_ADMIN_ROLES:
        return True
    role = get_user_primary_role(db, user.id)
    return (
        role is not None
        and role.name not in SELF_SERVICE_ROLES
        and effective_role_matrix(role).get("payroll_process") in ("e", "a")
    )


def get_people_directory_visible_ids(db: Session, user: models.User) -> set[uuid.UUID] | None:
    """C2: which employees' full records (GET /api/employees/full -- CTC,
    PAN, bank details) the caller may receive, on top of the People-module
    gate. None = whole company: Owner / HR / payroll processors
    (_is_people_or_payroll_admin) or a document-visibility admin tier.
    Everyone else: their reporting subtree + self. An Employee Permissions
    People level decides first (Self Only / No Access = own record)."""
    from . import employee_permissions

    scope = employee_permissions.sees_all_records(employee_permissions.level_for(db, user.employee_id, "people"))
    if scope is not None:
        return None if scope else ({user.employee_id} if user.employee_id else set())
    if _is_people_or_payroll_admin(db, user):
        return None
    visible = get_visible_employee_ids_for_docs(db, user)
    return None if visible is None else set(visible)


def get_payroll_visible_ids(db: Session, user: models.User) -> set[uuid.UUID] | None:
    """Bulk twin of can_view_employee_payroll (same rule, one evaluation for
    a whole list): whose CTC / salary / PAN / bank details the caller may
    see. None = everyone in the company (Owner / HR / payroll processors).
    Otherwise self, plus -- only with People 'edit' -- their reporting
    subtree / document visibility. List endpoints blank these fields for
    every other row."""
    if _is_people_or_payroll_admin(db, user):
        return None
    own = {user.employee_id} if user.employee_id else set()
    if not can_access_people_module(db, user, "edit"):
        return own
    visible = get_visible_employee_ids_for_docs(db, user)
    return None if visible is None else set(visible) | own


def _management_chain_ids(db: Session, employee_id: uuid.UUID | None, company_id: uuid.UUID) -> set[uuid.UUID]:
    """Everyone above `employee_id`: reporting and dotted-line managers, all
    the way up (cycle-safe)."""
    chain: set[uuid.UUID] = set()
    frontier = [employee_id] if employee_id else []
    while frontier and len(chain) < 500:
        rows = db.execute(
            select(models.Employee.reporting_manager_id, models.Employee.dotted_line_manager_id)
            .where(models.Employee.id.in_(frontier), models.Employee.company_id == company_id)
        ).all()
        frontier = []
        for rm, dl in rows:
            for m in (rm, dl):
                if m is not None and m not in chain and m != employee_id:
                    chain.add(m)
                    frontier.append(m)
    return chain


def get_directory_visible_ids(db: Session, user: models.User) -> set[uuid.UUID] | None:
    """GET /api/employees/directory (names / department / designation /
    branch / managers only): the People visibility of
    get_people_directory_visible_ids, plus the caller's own management chain
    -- so anyone can still see who their managers are, but no longer the
    whole company. None = whole company."""
    visible = get_people_directory_visible_ids(db, user)
    if visible is None:
        return None
    own = {user.employee_id} if user.employee_id else set()
    return set(visible) | own | _management_chain_ids(db, user.employee_id, user.company_id)


def can_view_employee_payroll(db: Session, user: models.User, employee_id: uuid.UUID) -> bool:
    """C2: GET /api/employees/{id}/payroll (CTC, PAN, bank account). Deny by
    default: self; Owner / HR / payroll processors for anyone in the
    company; otherwise only with People 'edit' access AND the employee in
    the caller's reporting subtree / document visibility."""
    if user.employee_id == employee_id:
        return True
    if _is_people_or_payroll_admin(db, user):
        return True
    if not can_access_people_module(db, user, "edit"):
        return False
    visible = get_visible_employee_ids_for_docs(db, user)
    return visible is None or employee_id in visible


def get_team_scope_employee_ids(db: Session, current_user: models.User) -> list[uuid.UUID]:
    """C9: the "team" data scope for Reports (deps.get_reports_scope) -- the
    caller's full downward reporting subtree plus self, for EVERY role that
    is team-scoped. Deliberately not get_visible_employee_ids_for_docs,
    whose document-visibility tiers treat General Manager / Sr. Manager as
    admin-level (unrestricted) and so leaked company-wide report data to a
    role whose dashboard_scope / blurb is "your reporting line only"."""
    if current_user.employee_id is None:
        return []
    subtree_ids = _reporting_subtree_ids(db, current_user.employee_id, current_user.company_id)
    return list({*subtree_ids, current_user.employee_id})


def get_primary_project_allocation(
    db: Session, employee_id: uuid.UUID
) -> models.ProjectAllocation | None:
    """An employee's "primary" project allocation when they have more than
    one -- a deterministic first row (ordered by id), same convention
    org_hierarchy.resolve_employee_hierarchy uses for the Team
    Lead/Project Manager rungs."""
    return db.scalar(
        select(models.ProjectAllocation)
        .where(models.ProjectAllocation.employee_id == employee_id)
        .order_by(models.ProjectAllocation.id)
        .limit(1)
    )


def get_employee_role_name(db: Session, employee_id: uuid.UUID) -> str | None:
    """Single-employee counterpart to list_employees_full's bulk
    role_by_employee join -- same USER_ROLE_ORDER tie-break (C8: the one
    deterministic "primary role" rule get_user_primary_role uses) for an
    employee somehow holding more than one role."""
    row = db.execute(
        select(models.Role.name)
        .join(models.UserRole, models.UserRole.role_id == models.Role.id)
        .join(models.User, models.User.id == models.UserRole.user_id)
        .where(models.User.employee_id == employee_id)
        .order_by(*USER_ROLE_ORDER)
    ).first()
    return row[0] if row else None


def get_role_by_name(db: Session, company_id: uuid.UUID, name: str) -> models.Role | None:
    return db.scalar(
        select(models.Role).where(models.Role.company_id == company_id, models.Role.name == name)
    )


def company_has_band_role_mapping(db: Session, company_id: uuid.UUID) -> bool:
    """True once this company has assigned Role.band_id on at least one
    role -- i.e. it has started configuring the Band -> Role hierarchy.
    Used to decide whether list_roles' band_id filter should actually
    filter, or fall back to the pre-hierarchy "show every role" behavior."""
    return (
        db.scalar(
            select(func.count()).select_from(models.Role).where(
                models.Role.company_id == company_id,
                models.Role.band_id.isnot(None),
            )
        )
        or 0
    ) > 0


def company_has_role_designation_mapping(db: Session, company_id: uuid.UUID) -> bool:
    """Same idea as company_has_band_role_mapping, one level down: true once
    at least one designation has been mapped to a Role (role_id) *or*
    straight to a Band with no Role in between (band_id) -- either counts
    as "this company has started configuring designation mappings"."""
    return (
        db.scalar(
            select(func.count()).select_from(models.Designation).where(
                models.Designation.company_id == company_id,
                or_(
                    models.Designation.role_id.isnot(None),
                    models.Designation.band_id.isnot(None),
                ),
            )
        )
        or 0
    ) > 0


def list_roles(
    db: Session, company_id: uuid.UUID, band_id: uuid.UUID | None = None
) -> list[models.Role]:
    """Backs GET /api/roles. When band_id is given AND this company has
    started configuring the Band -> Role hierarchy (company_has_band_role_
    mapping), filters to roles mapped to that band -- otherwise every role
    is returned unfiltered, exactly as before this feature existed, so Add
    Employee's Role dropdown keeps working for companies that haven't
    configured Band -> Role mappings yet."""
    query = select(models.Role).where(models.Role.company_id == company_id)
    if band_id is not None and company_has_band_role_mapping(db, company_id):
        query = query.where(models.Role.band_id == band_id)
    return db.scalars(
        query.order_by(models.Role.is_system.desc(), models.Role.name)
    ).all()


def list_designations_for_role(
    db: Session,
    company_id: uuid.UUID,
    role_id: uuid.UUID | None = None,
    band_id: uuid.UUID | None = None,
    legacy_band_name: str | None = None,
) -> list[models.Designation]:
    """Backs the Employee Creation Band/Role -> Designation cascade (GET
    /api/designations/by-role). A designation matches if it's mapped to
    role_id *or* mapped straight to band_id (no Role in between) -- a
    company can configure either path independently, or both. Filters this
    way once this company has mapped at least one designation to a role or
    band (company_has_role_designation_mapping). Until then, this company
    hasn't configured the hierarchy, so rather than returning every
    designation unfiltered (a real UX regression for the Add Employee
    dialog -- dozens of titles instead of the ~9 for the currently selected
    band), it falls back to legacy_band_name: the exact same free-text
    Designation.band filter the dialog used before this feature existed."""
    query = select(models.Designation).where(
        models.Designation.company_id == company_id,
        models.Designation.is_active.is_(True),
    )
    if (role_id is not None or band_id is not None) and company_has_role_designation_mapping(
        db, company_id
    ):
        conditions = []
        if role_id is not None:
            conditions.append(models.Designation.role_id == role_id)
        if band_id is not None:
            conditions.append(models.Designation.band_id == band_id)
        query = query.where(or_(*conditions))
    elif legacy_band_name is not None:
        query = query.where(models.Designation.band == legacy_band_name)
    return db.scalars(query.order_by(models.Designation.name)).all()


def company_has_direct_designation_band_mapping(db: Session, company_id: uuid.UUID) -> bool:
    """True once this company has mapped at least one designation straight
    to a Band (band_id) with no Role in between."""
    return (
        db.scalar(
            select(func.count()).select_from(models.Designation).where(
                models.Designation.company_id == company_id,
                models.Designation.band_id.isnot(None),
            )
        )
        or 0
    ) > 0


def band_role_designation_hierarchy(db: Session, company_id: uuid.UUID) -> dict:
    """Backs GET /api/organization/band-hierarchy -- the real, tenant-
    configured Band -> Role -> Designation tree, plus any designations
    mapped straight to a Band with no Role (direct_designations), shared by
    the Organization > Designations tab and the Org Chart tab so both
    render identical structure. Returns {"configured": False, "bands": []}
    until this company has mapped at least one role to a band *or* at least
    one designation straight to a band -- callers should fall back to their
    pre-hierarchy legacy view in that case rather than rendering an empty
    tree."""
    if not company_has_band_role_mapping(
        db, company_id
    ) and not company_has_direct_designation_band_mapping(db, company_id):
        return {"configured": False, "bands": []}

    bands = list_bands(db, company_id, active_only=False)
    roles = db.scalars(
        select(models.Role).where(models.Role.company_id == company_id)
    ).all()
    designations = db.scalars(
        select(models.Designation).where(
            models.Designation.company_id == company_id,
            models.Designation.is_active.is_(True),
        )
    ).all()
    counts = dict(
        db.execute(
            select(models.Designation.id, func.count(models.Employee.id))
            .select_from(models.Designation)
            .outerjoin(models.Employee, models.Employee.designation_id == models.Designation.id)
            .where(models.Designation.company_id == company_id)
            .group_by(models.Designation.id)
        ).all()
    )

    roles_by_id: dict[uuid.UUID, models.Role] = {r.id: r for r in roles}

    # A Role is "in" a Band two independent, never-synced ways: Role.band_id
    # itself (set via Add/Edit Custom Role's Band picker), or a Designation
    # that carries both role_id and band_id (set via the Designations tab's
    # "Edit Mapping" dialog -- which only ever touches the Designation row,
    # never Role.band_id). A real company can easily have only the second
    # kind: every Designation properly mapped to a Role and a Band, while
    # every Role.band_id stays null forever. Keying strictly off
    # Role.band_id (the old behavior) made every one of those roles --
    # and therefore every designation under them -- vanish from this tree
    # entirely, even though company_has_direct_designation_band_mapping
    # (checking Designation.band_id alone) had already set configured=True.
    # That combination is exactly what made the Designations tab flip from
    # its correct legacy view to an all-"nothing mapped" hierarchy view a
    # moment after loading.
    role_ids_by_band: dict[uuid.UUID, set[uuid.UUID]] = {}
    for r in roles:
        if r.band_id is not None:
            role_ids_by_band.setdefault(r.band_id, set()).add(r.id)

    designations_by_role: dict[uuid.UUID, list[models.Designation]] = {}
    # Designations mapped straight to a Band (band_id) with no Role.
    designations_by_band: dict[uuid.UUID, list[models.Designation]] = {}
    for d in designations:
        if d.role_id is not None:
            designations_by_role.setdefault(d.role_id, []).append(d)
            if d.band_id is not None:
                role_ids_by_band.setdefault(d.band_id, set()).add(d.role_id)
        elif d.band_id is not None:
            designations_by_band.setdefault(d.band_id, []).append(d)

    band_nodes = []
    for band in bands:
        role_nodes = []
        band_total = 0
        band_role_ids = sorted(
            role_ids_by_band.get(band.id, set()),
            key=lambda rid: roles_by_id[rid].name,
        )
        for role_id in band_role_ids:
            role = roles_by_id[role_id]
            designation_nodes = []
            role_total = 0
            # Scoped to *this* band -- a Role can be linked to more than one
            # Band this way (its own Role.band_id, plus wherever any of its
            # Designations independently point via their own band_id), so
            # its designation list must be filtered per band here rather
            # than dumping every designation the Role has anywhere under
            # each Band it happens to touch. A Designation with no band_id
            # of its own inherits its Role's Band.
            relevant = [
                d for d in designations_by_role.get(role.id, [])
                if d.band_id == band.id or (d.band_id is None and role.band_id == band.id)
            ]
            for d in sorted(relevant, key=lambda d: d.name):
                count = counts.get(d.id, 0)
                role_total += count
                designation_nodes.append(
                    {"id": d.id, "name": d.name, "is_active": d.is_active, "employee_count": count}
                )
            band_total += role_total
            role_nodes.append(
                {
                    "id": role.id,
                    "name": role.name,
                    "employee_count": role_total,
                    "designations": designation_nodes,
                }
            )
        direct_designation_nodes = []
        for d in sorted(designations_by_band.get(band.id, []), key=lambda d: d.name):
            count = counts.get(d.id, 0)
            band_total += count
            direct_designation_nodes.append(
                {"id": d.id, "name": d.name, "is_active": d.is_active, "employee_count": count}
            )
        band_nodes.append(
            {
                "id": band.id,
                "name": band.name,
                "band_number": band.band_number,
                "employee_count": band_total,
                "roles": role_nodes,
                "direct_designations": direct_designation_nodes,
            }
        )
    return {"configured": True, "bands": band_nodes}


def reassign_employee_role(
    db: Session, employee_id: uuid.UUID, role_name: str, company_id: uuid.UUID
) -> models.Role:
    """Changes which role an existing employee's account holds. Deletes any
    prior core.user_roles row(s) first -- get_user_primary_role only ever
    reads the first one assigned, so leaving an old row behind would make
    the reassignment silently not take effect."""
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != company_id:
        raise ValueError("Employee not found")
    user = db.scalar(select(models.User).where(models.User.employee_id == employee_id))
    if user is None:
        raise ValueError("This employee has no login account to assign a role to")
    role = get_role_by_name(db, company_id, role_name)
    if role is None:
        raise ValueError(f"Role '{role_name}' not found")
    db.execute(delete(models.UserRole).where(models.UserRole.user_id == user.id))
    db.add(models.UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return role


def org_hierarchy_summary(db: Session, company_id: uuid.UUID) -> list[dict]:
    """Organization > Org Chart tiers -- one per real core.designations.band
    value, ordered fewest-employees-first (a pyramid-shaped org chart puts
    the smallest, most senior band on top; for this data that empirically
    matches seniority -- Exec band has 5 people, not 125). Each tier's
    subtitle samples up to 4 of its band's actual designation titles,
    ranked by headcount, so a band code like "L6" reads as "Manager,
    Finance Manager, HR Manager, ..." instead of a bare code."""
    rows = db.execute(
        select(models.Designation.band, models.Designation.name, func.count(models.Employee.id))
        .join(models.Employee, models.Employee.designation_id == models.Designation.id)
        .where(models.Designation.company_id == company_id, models.Designation.band.isnot(None))
        .group_by(models.Designation.band, models.Designation.name)
    ).all()

    bands: dict[str, list[tuple[str, int]]] = {}
    for band, name, count in rows:
        bands.setdefault(band, []).append((name, count))

    tiers = []
    for band, designations in bands.items():
        designations.sort(key=lambda d: d[1], reverse=True)
        total = sum(c for _n, c in designations)
        sample = ", ".join(n for n, _c in designations[:4])
        tiers.append({"band": band, "count": total, "sample_designations": sample})
    tiers.sort(key=lambda t: t["count"])
    return tiers


def list_designation_bands(db: Session, company_id: uuid.UUID) -> list[models.Designation]:
    return db.scalars(
        select(models.Designation).where(
            models.Designation.company_id == company_id,
            models.Designation.is_active.is_(True),
        )
    ).all()


def band_sort_key(band_name: str | None) -> tuple[int, str]:
    """Orders bands the way the frontend's kDesignationBands does (Ownership
    -> ... -> Associate/Entry Level). Anything not in that fixed list (e.g. a
    band added later through the API) sorts after the known ones, alphabetically."""
    if band_name in BAND_NUMBERS:
        return (BAND_NUMBERS[band_name], "")
    return (len(BAND_NUMBERS) + 1, band_name or "")


def designation_sort_key(band_name: str | None, designation_name: str) -> tuple[int, object]:
    """Within a band, seeded titles keep the frontend's original display
    order; anything added later sorts after them, alphabetically."""
    order = SEED_TITLE_ORDER.get(band_name or "", {})
    if designation_name in order:
        return (0, order[designation_name])
    return (1, designation_name)


def _advisory_lock(db: Session, *parts: str) -> None:
    """Postgres advisory lock scoped to the given key parts, held for the
    rest of the current transaction. Used everywhere this module does a
    check-then-insert against a table with no unique constraint to fall back
    on — serializes concurrent "does this already exist?" races so two
    simultaneous requests can't both pass the check and create duplicates."""
    key = ":".join(parts)
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))


def create_designation(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    band: str | None = None,
    role_id: uuid.UUID | None = None,
    band_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> models.Designation:
    """Concurrency-safe insert.

    core.designations has no unique constraint on (company_id, name), so two
    admins submitting the same designation at the same moment could both pass
    a plain "does it exist?" check and end up with two rows. A Postgres
    advisory lock scoped to (company_id, name) serializes exactly that pair
    of requests for the rest of this transaction: the second caller blocks
    until the first commits, then re-checks and gets a clean 409 instead of a
    duplicate row.

    role_id optionally maps this designation into the tenant-configurable
    Role -> Designation hierarchy; band_id maps it straight to a Band with
    no Role in between (see backend/db/add_designation_band_id.sql) -- both
    independent of `band`, the older free-text label. When band_id is
    given, the legacy `band` string is *derived* from that Band's own name
    instead of requiring the caller to also supply a matching string --
    this is what stops new designations from defaulting to the hardcoded
    'Unassigned' label once a real Band is known. `band` is still honored
    as a plain string when band_id isn't given (e.g. the legacy Add
    Designation flow), and 'Unassigned' remains the last-resort fallback
    when neither is available.
    """
    _advisory_lock(db, "designation", str(company_id), name)

    existing = db.scalar(
        select(models.Designation).where(
            models.Designation.company_id == company_id,
            models.Designation.name == name,
        )
    )
    if existing is not None:
        raise ValueError(f"Designation '{name}' already exists")

    band_name = band
    if band_id is not None:
        band_row = db.scalar(
            select(models.Band).where(
                models.Band.id == band_id, models.Band.company_id == company_id
            )
        )
        if band_row is not None:
            band_name = band_row.name

    designation = models.Designation(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        band=band_name or "Unassigned",
        is_active=True,
        role_id=role_id,
        band_id=band_id,
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(designation)
    db.flush()
    return designation


def get_designation_for_update(
    db: Session, designation_id: uuid.UUID, company_id: uuid.UUID
) -> models.Designation | None:
    """Row-locked read (SELECT ... FOR UPDATE): serializes concurrent edits to
    the same designation so a second admin's update can't silently overwrite
    the first admin's change (classic lost-update race)."""
    return db.scalar(
        select(models.Designation)
        .where(
            models.Designation.id == designation_id,
            models.Designation.company_id == company_id,
        )
        .with_for_update()
    )


def get_or_create_designation_by_name(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    role_id: uuid.UUID | None = None,
    band_id: uuid.UUID | None = None,
) -> models.Designation:
    """Add Employee's Designation field is free text, not a dropdown tied to
    the Designations catalog, so a new hire's title might not exist yet.
    Rather than reject the request, this grows the catalog on the fly —
    same concurrency-safe insert as create_designation, just tolerant of the
    "already exists" case instead of raising.

    role_id (the role the employee is being hired under) and band_id (the
    Band selected on the same Add Employee form) are stamped onto a
    brand-new designation so it's already mapped into the hierarchy instead
    of landing on the old hardcoded band='Unassigned'. An *existing*
    designation that predates the hierarchy (role_id/band_id/band still
    unset or 'Unassigned') is backfilled with them too -- a company typing
    designations through Add Employee under a consistent Band/Role
    naturally finishes configuring its hierarchy over time instead of never
    getting mapped at all."""
    existing = db.scalar(
        select(models.Designation).where(
            models.Designation.company_id == company_id,
            func.lower(models.Designation.name) == name.strip().lower(),
        )
    )
    if existing is not None:
        if role_id is not None and existing.role_id is None:
            existing.role_id = role_id
        if band_id is not None and existing.band_id is None:
            existing.band_id = band_id
            if existing.band is None or existing.band == "Unassigned":
                band_row = db.scalar(
                    select(models.Band).where(
                        models.Band.id == band_id, models.Band.company_id == company_id
                    )
                )
                if band_row is not None:
                    existing.band = band_row.name
        return existing
    try:
        return create_designation(
            db, company_id, name.strip(), role_id=role_id, band_id=band_id
        )
    except ValueError:
        # Lost the race to another concurrent request creating the same
        # designation between our lookup and the insert -- fetch what it made.
        return db.scalar(
            select(models.Designation).where(
                models.Designation.company_id == company_id,
                func.lower(models.Designation.name) == name.strip().lower(),
            )
        )


def get_or_seed_company_bands(db: Session, company_id: uuid.UUID) -> list[models.Band]:
    """Auto-provisions a company's default Band rows the first time they're
    needed -- same lazy-provisioning shape as
    get_or_create_employee_number_series. Covers any company not already
    backfilled by the one-time migration (backend/db/
    add_core_bands_table.sql), e.g. a company created after that migration
    ran, so its Add Employee "Grade / Band" dropdown still opens with the
    same 10 bands the app has always offered (designation_seed_data.
    DESIGNATION_BANDS) instead of an empty list."""
    existing = db.scalars(
        select(models.Band).where(models.Band.company_id == company_id)
    ).all()
    if existing:
        return list(existing)

    _advisory_lock(db, "band_seed", str(company_id))
    # Re-check under the lock -- a concurrent first request may have already
    # seeded these while this one waited.
    existing = db.scalars(
        select(models.Band).where(models.Band.company_id == company_id)
    ).all()
    if existing:
        return list(existing)

    seeded: list[models.Band] = []
    for i, (band_name, _titles) in enumerate(DESIGNATION_BANDS):
        band = models.Band(
            id=uuid.uuid4(),
            company_id=company_id,
            name=band_name,
            code=f"BAND-{i + 1:02d}",
            band_number=i + 1,
            is_active=True,
        )
        db.add(band)
        seeded.append(band)
    db.flush()
    return seeded


def list_bands(
    db: Session, company_id: uuid.UUID, active_only: bool = False
) -> list[models.Band]:
    """Backs GET /api/bands. Auto-seeds defaults on first call for a company
    that has none yet (see get_or_seed_company_bands)."""
    bands = get_or_seed_company_bands(db, company_id)
    if active_only:
        bands = [b for b in bands if b.is_active]
    return sorted(bands, key=lambda b: b.band_number)


def get_band_for_update(
    db: Session, band_id: uuid.UUID, company_id: uuid.UUID
) -> models.Band | None:
    """Row-locked read (SELECT ... FOR UPDATE), same pattern as
    get_designation_for_update -- serializes concurrent edits to the same
    band so a second admin's update can't silently overwrite the first."""
    return db.scalar(
        select(models.Band)
        .where(
            models.Band.id == band_id,
            models.Band.company_id == company_id,
        )
        .with_for_update()
    )


def _find_conflicting_band(
    db: Session, company_id: uuid.UUID, name: str, code: str, exclude_id: uuid.UUID | None = None
) -> models.Band | None:
    q = select(models.Band).where(
        models.Band.company_id == company_id,
        or_(
            func.lower(models.Band.name) == name.lower(),
            func.lower(models.Band.code) == code.lower(),
        ),
    )
    if exclude_id is not None:
        q = q.where(models.Band.id != exclude_id)
    return db.scalar(q)


def create_band(
    db: Session, company_id: uuid.UUID, payload, user_id: uuid.UUID | None = None
) -> models.Band:
    """Concurrency-safe insert, same advisory-lock pattern as
    create_designation -- core.bands has no unique constraint on
    (company_id, name)/(company_id, code) (this schema uses app-level
    advisory locks instead of DB constraints throughout, e.g. core_
    designations, employee_code), so two admins submitting the same band
    name/code at the same moment could both pass a plain "does it exist?"
    check and end up with duplicates.

    band_number is assigned automatically (max existing + 1 for this
    company) -- it's the stable per-company integer the Add Employee
    "Grade / Band" dropdown displays and the one that ends up copied
    verbatim into core_employees.band, so it's not something the caller
    picks."""
    name = payload.name.strip()
    code = payload.code.strip()
    _advisory_lock(db, "band", str(company_id), name.lower(), code.lower())

    # get_or_seed_company_bands first so a brand-new company's advisory-
    # locked seed (band_number 1-10) has already happened before we compute
    # "max + 1" below -- otherwise a company's very first manually-created
    # band could collide with band_number 1 once the lazy seed runs later.
    get_or_seed_company_bands(db, company_id)

    conflict = _find_conflicting_band(db, company_id, name, code)
    if conflict is not None:
        if conflict.name.lower() == name.lower():
            raise ValueError(f"Band '{name}' already exists")
        raise ValueError(f"Band code '{code}' already exists")

    next_number = (
        db.scalar(
            select(func.coalesce(func.max(models.Band.band_number), 0)).where(
                models.Band.company_id == company_id
            )
        )
        or 0
    )

    band = models.Band(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        code=code,
        band_number=next_number + 1,
        description=payload.description,
        is_active=True,
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(band)
    db.flush()
    return band


def update_band(
    db: Session, band: models.Band, updates: dict, user_id: uuid.UUID | None = None
) -> models.Band:
    """Partial update (name/code/description/is_active) -- re-validates
    name/code uniqueness within the company if either is changing, same
    advisory-lock + conflict-check pattern as create_band. band_number is
    never changed by an update -- it's assigned once at creation."""
    new_name = updates.get("name", band.name)
    new_code = updates.get("code", band.code)
    if "name" in updates or "code" in updates:
        _advisory_lock(db, "band", str(band.company_id), new_name.lower(), new_code.lower())
        conflict = _find_conflicting_band(
            db, band.company_id, new_name, new_code, exclude_id=band.id
        )
        if conflict is not None:
            if conflict.name.lower() == new_name.lower():
                raise ValueError(f"Band '{new_name}' already exists")
            raise ValueError(f"Band code '{new_code}' already exists")

    for field, value in updates.items():
        if field in ("name", "code") and isinstance(value, str):
            value = value.strip()
        setattr(band, field, value)
    band.updated_by = user_id
    db.flush()
    return band


def delete_band(db: Session, band: models.Band) -> None:
    """Hard delete -- unlike Designation (which core.employees.designation_id
    actually references, so it's only ever soft-deleted/deactivated),
    core.employees.band is a bare integer with no reference to core.bands,
    so nothing is left dangling at the DB level. Still guarded at the
    application level: a band currently assigned to at least one employee
    can't be removed out from under them -- raises ValueError so the router
    can turn it into a clean 409 telling the caller to deactivate instead."""
    in_use = db.scalar(
        select(func.count())
        .select_from(models.Employee)
        .where(
            models.Employee.company_id == band.company_id,
            models.Employee.band == band.band_number,
        )
    )
    if in_use:
        raise ValueError(
            f"Cannot delete band '{band.name}' -- {in_use} employee(s) are "
            "currently assigned to it. Deactivate it instead."
        )
    db.delete(band)
    db.flush()


def delete_role(db: Session, role: models.Role) -> None:
    """Hard delete -- guarded the same way delete_band is: raises ValueError
    (turned into a clean 409 by the router) instead of a raw FK-violation
    500 whenever anything still points at this role. RolePermission rows
    are not checked -- Role.role_permissions has cascade="all,
    delete-orphan", so those are removed automatically as part of this same
    delete."""
    if role.is_system:
        raise ValueError(f"Cannot delete built-in role '{role.name}'.")

    user_role_count = db.scalar(
        select(func.count()).select_from(models.UserRole).where(models.UserRole.role_id == role.id)
    )
    if user_role_count:
        raise ValueError(
            f"Cannot delete role '{role.name}' -- {user_role_count} user(s) are "
            "currently assigned to it."
        )

    designation_count = db.scalar(
        select(func.count())
        .select_from(models.Designation)
        .where(models.Designation.role_id == role.id)
    )
    if designation_count:
        raise ValueError(
            f"Cannot delete role '{role.name}' -- {designation_count} designation(s) "
            "are mapped to it in the Band -> Role -> Designation hierarchy."
        )

    binding_count = db.scalar(
        select(func.count())
        .select_from(models.HierarchyRoleBinding)
        .where(models.HierarchyRoleBinding.role_id == role.id)
    )
    if binding_count:
        raise ValueError(
            f"Cannot delete role '{role.name}' -- it is bound to a reporting-hierarchy "
            "rung in Organization Hierarchy."
        )

    step_count = db.scalar(
        select(func.count())
        .select_from(models.ApprovalWorkflowStep)
        .where(models.ApprovalWorkflowStep.role_id == role.id)
    )
    if step_count:
        raise ValueError(
            f"Cannot delete role '{role.name}' -- it is used as an approver in one or "
            "more Approval Workflow steps."
        )

    field_rule_count = db.scalar(
        select(func.count()).select_from(models.FieldRule).where(models.FieldRule.role_id == role.id)
    )
    if field_rule_count:
        raise ValueError(
            f"Cannot delete role '{role.name}' -- it has role-specific Field Rule overrides."
        )

    db.delete(role)
    db.flush()


def employee_display_name(db: Session, employee_id: uuid.UUID | None) -> str:
    if employee_id is None:
        return "—"
    employee = db.get(models.Employee, employee_id)
    if employee is None:
        return "—"
    return f"{employee.first_name} {employee.last_name}".strip() if employee.last_name else employee.first_name


def employee_display_names_bulk(
    db: Session, employee_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    """Bulk version of employee_display_name -- ONE query for many ids
    instead of one query per id.

    Every list endpoint that serializes N rows and previously called
    employee_display_name(db, id) once to three times per row (employee
    name, approver name, reporting-manager name) turned into an N+1 (up to
    3N+1) query pattern -- fine at the handful of seed employees this app
    shipped with, expensive now that companies carry hundreds of
    employees/requests. Callers should collect every id they need across
    the whole result set first, resolve them ALL in one call here, then
    look up per row via `.get(id, "—")` -- same "—" fallback
    employee_display_name already used for a missing/None id, just moved
    to the call site since a dict has no id to be missing "for"."""
    ids = {i for i in employee_ids if i is not None}
    if not ids:
        return {}
    rows = db.execute(
        select(models.Employee.id, models.Employee.first_name, models.Employee.last_name)
        .where(models.Employee.id.in_(ids))
    ).all()
    return {
        row.id: (f"{row.first_name} {row.last_name}".strip() if row.last_name else row.first_name)
        for row in rows
    }


def employee_codes_bulk(
    db: Session, employee_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    """Bulk version of `db.get(models.Employee, id).employee_code` -- ONE
    query for many ids instead of one `db.get` per row (see
    employee_display_names_bulk's docstring for the same reasoning)."""
    ids = {i for i in employee_ids if i is not None}
    if not ids:
        return {}
    rows = db.execute(
        select(models.Employee.id, models.Employee.employee_code).where(models.Employee.id.in_(ids))
    ).all()
    return {row.id: row.employee_code for row in rows}


def branch_display_name(db: Session, branch_id: uuid.UUID | None) -> str:
    if branch_id is None:
        return "—"
    branch = db.get(models.Branch, branch_id)
    return branch.name if branch is not None else "—"


def department_display_name(db: Session, department_id: uuid.UUID | None) -> str:
    if department_id is None:
        return "—"
    department = db.get(models.Department, department_id)
    return department.name if department is not None else "—"


def format_inr_budget(amount: float | None) -> str:
    if amount is None:
        return "—"
    if amount >= 1_00_00_000:
        return f"₹{amount / 1_00_00_000:.1f}Cr"
    if amount >= 1_00_000:
        return f"₹{amount / 1_00_000:.1f}L"
    return f"₹{amount:,.0f}"


def parse_inr_abbreviated(text: str) -> float | None:
    """Inverse of format_inr_budget -- Add Project's Budget field (and this
    format's own seed data) uses the same '₹1.2Cr' / '₹86L' style. None for
    anything with no digits (e.g. '—')."""
    match = re.search(r"([\d.]+)\s*(cr|l)?", text, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    if suffix == "cr":
        return value * 1_00_00_000
    if suffix == "l":
        return value * 1_00_000
    return value


def list_branches(db: Session, company_id: uuid.UUID) -> list[models.Branch]:
    return db.scalars(
        select(models.Branch)
        .where(models.Branch.company_id == company_id, models.Branch.is_active.is_(True))
        .order_by(models.Branch.name)
    ).all()


def count_employees_in_branch(db: Session, branch_id: uuid.UUID) -> int:
    return db.scalar(
        select(func.count(models.Employee.id)).where(
            models.Employee.branch_id == branch_id, models.Employee.is_active.is_(True)
        )
    )


def assign_org_code(db: Session, model, company_id: uuid.UUID, name: str, prefix: str) -> str:
    """F25: a unique per-company code for a new branch/department whose
    create form has no Code field -- assigned here, inside the create
    transaction, instead of being made up in the browser. Readable and
    name-derived ("HYDERABAD", then "HYDERABAD-2", ...); the caller holds a
    company-level advisory lock so two concurrent creates can't pick the
    same one."""
    base = re.sub(r"[^A-Z0-9]", "", (name or "").upper())[:12] or prefix
    taken = set(
        db.scalars(
            select(model.code).where(model.company_id == company_id, model.code.like(f"{base}%"))
        )
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def create_branch(db: Session, company_id: uuid.UUID, payload) -> models.Branch:
    if not payload.code:
        _advisory_lock(db, "branch_code", str(company_id))
        payload.code = assign_org_code(db, models.Branch, company_id, payload.name, "BR")
    _advisory_lock(db, "branch", str(company_id), payload.code)
    existing = db.scalar(
        select(models.Branch).where(
            models.Branch.company_id == company_id, models.Branch.code == payload.code
        )
    )
    if existing is not None:
        raise ValueError(f"Branch code '{payload.code}' already exists")

    branch = models.Branch(
        id=uuid.uuid4(),
        company_id=company_id,
        code=payload.code,
        name=payload.name,
        gstin=payload.tax,
        city=payload.city,
        state=payload.state,
        country=payload.country,
        tz=payload.tz or "IST (UTC+5:30)",
        is_active=True,
        address_line1=payload.address_line1,
        pincode=payload.pincode,
        branch_manager_id=payload.branch_manager_id,
    )
    db.add(branch)
    db.flush()
    return branch


def update_branch(db: Session, branch: models.Branch, updates: dict) -> models.Branch:
    """Partial update -- same reasoning as update_company_profile."""
    for field, value in updates.items():
        setattr(branch, field, value)
    db.flush()
    return branch


def list_business_units(db: Session, company_id: uuid.UUID) -> list[models.BusinessUnit]:
    return db.scalars(
        select(models.BusinessUnit)
        .where(
            models.BusinessUnit.company_id == company_id,
            models.BusinessUnit.is_active.is_(True),
        )
        .order_by(models.BusinessUnit.name)
    ).all()


def get_business_unit_by_name(
    db: Session, company_id: uuid.UUID, name: str
) -> models.BusinessUnit | None:
    return db.scalar(
        select(models.BusinessUnit).where(
            models.BusinessUnit.company_id == company_id, models.BusinessUnit.name == name
        )
    )


def create_business_unit(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    cost_center: str | None,
    head_employee_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
) -> models.BusinessUnit:
    _advisory_lock(db, "business_unit", str(company_id), name)
    existing = get_business_unit_by_name(db, company_id, name)
    if existing is not None:
        raise ValueError(f"Business unit '{name}' already exists")

    unit = models.BusinessUnit(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        cost_center=cost_center,
        head_employee_id=head_employee_id,
        branch_id=branch_id,
        is_active=True,
    )
    db.add(unit)
    db.flush()
    return unit


def update_business_unit(
    db: Session, unit: models.BusinessUnit, updates: dict
) -> models.BusinessUnit:
    """Partial update -- same reasoning as update_company_profile. A rename
    to a name already in use surfaces as an IntegrityError (company_id,
    name) for the router to turn into a 409, same as create_business_unit."""
    for field, value in updates.items():
        setattr(unit, field, value)
    db.flush()
    return unit


def assign_departments_to_business_unit(
    db: Session, business_unit_id: uuid.UUID, department_ids: list[uuid.UUID]
) -> list[str]:
    departments = db.scalars(
        select(models.Department).where(models.Department.id.in_(department_ids))
    ).all()
    for department in departments:
        department.business_unit_id = business_unit_id
    db.flush()
    return [d.name for d in departments]


def list_departments(db: Session, company_id: uuid.UUID) -> list[models.Department]:
    return db.scalars(
        select(models.Department)
        .where(
            models.Department.company_id == company_id, models.Department.is_active.is_(True)
        )
        .order_by(models.Department.name)
    ).all()


def get_department_by_name(
    db: Session, company_id: uuid.UUID, name: str
) -> models.Department | None:
    return db.scalar(
        select(models.Department).where(
            models.Department.company_id == company_id,
            func.lower(models.Department.name) == name.strip().lower(),
        )
    )


def get_department_by_code(
    db: Session, company_id: uuid.UUID, code: str
) -> models.Department | None:
    return db.scalar(
        select(models.Department).where(
            models.Department.company_id == company_id, models.Department.code == code
        )
    )


def count_employees_in_department(db: Session, department_id: uuid.UUID) -> int:
    return db.scalar(
        select(func.count(models.Employee.id)).where(
            models.Employee.department_id == department_id,
            models.Employee.is_active.is_(True),
        )
    )


def count_active_employees_by(db: Session, column, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Bulk count_employees_in_department/count_employees_in_branch: one
    GROUP BY for many org units (PERF-08). `column` is
    models.Employee.department_id or models.Employee.branch_id; units with
    no active employees are absent (callers use .get(id, 0))."""
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return dict(
        db.execute(
            select(column, func.count(models.Employee.id))
            .where(column.in_(ids), models.Employee.is_active.is_(True))
            .group_by(column)
        ).all()
    )


def list_sub_departments_bulk(
    db: Session, department_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[models.SubDepartment]]:
    """Bulk list_sub_departments (same active filter and name ordering),
    one query for every department (PERF-08)."""
    ids = {i for i in department_ids if i is not None}
    if not ids:
        return {}
    out: dict[uuid.UUID, list[models.SubDepartment]] = {}
    for sub in db.scalars(
        select(models.SubDepartment)
        .where(
            models.SubDepartment.department_id.in_(ids),
            models.SubDepartment.is_active.is_(True),
        )
        .order_by(models.SubDepartment.name)
    ).all():
        out.setdefault(sub.department_id, []).append(sub)
    return out


def names_by_id_bulk(db: Session, model, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    """{id: name} for any model with id/name columns (Branch,
    BusinessUnit, Department) -- bulk counterpart of branch_display_name/
    department_display_name, callers keep the "—" fallback."""
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return dict(db.execute(select(model.id, model.name).where(model.id.in_(ids))).all())


def list_sub_departments(db: Session, department_id: uuid.UUID) -> list[models.SubDepartment]:
    return db.scalars(
        select(models.SubDepartment)
        .where(
            models.SubDepartment.department_id == department_id,
            models.SubDepartment.is_active.is_(True),
        )
        .order_by(models.SubDepartment.name)
    ).all()


def create_department(
    db: Session, company_id: uuid.UUID, payload
) -> models.Department:
    if not payload.code:
        _advisory_lock(db, "department_code", str(company_id))
        payload.code = assign_org_code(db, models.Department, company_id, payload.name, "DEPT")
    _advisory_lock(db, "department", str(company_id), payload.code)
    existing = db.scalar(
        select(models.Department).where(
            models.Department.company_id == company_id, models.Department.code == payload.code
        )
    )
    if existing is not None:
        raise ValueError(f"Department code '{payload.code}' already exists")

    business_unit_id = None
    if payload.business_unit_name:
        unit = get_business_unit_by_name(db, company_id, payload.business_unit_name)
        if unit is None:
            unit = create_business_unit(db, company_id, payload.business_unit_name, None)
        business_unit_id = unit.id

    department = models.Department(
        id=uuid.uuid4(),
        company_id=company_id,
        branch_id=payload.branch_id,
        code=payload.code,
        name=payload.name,
        business_unit_id=business_unit_id,
        annual_budget=payload.annual_budget,
        head_employee_id=payload.head_employee_id,
        hr_representative_id=payload.hr_representative_id,
        senior_manager_id=payload.senior_manager_id,
        project_manager_id=payload.project_manager_id,
        is_active=True,
    )
    db.add(department)
    db.flush()
    return department


def update_department(
    db: Session, department: models.Department, updates: dict, company_id: uuid.UUID
) -> models.Department:
    """Partial update -- same reasoning as update_company_profile.
    business_unit_name isn't a real column, so it's resolved to
    business_unit_id here (find-or-create the unit, same as
    create_department) before the generic setattr loop; an explicit empty
    value clears the department's business unit."""
    if "business_unit_name" in updates:
        name = updates.pop("business_unit_name")
        if name:
            unit = get_business_unit_by_name(db, company_id, name)
            if unit is None:
                unit = create_business_unit(db, company_id, name, None)
            updates["business_unit_id"] = unit.id
        else:
            updates["business_unit_id"] = None
    for field, value in updates.items():
        setattr(department, field, value)
    db.flush()
    return department


def create_sub_department(
    db: Session, department_id: uuid.UUID, name: str
) -> models.SubDepartment:
    _advisory_lock(db, "sub_department", str(department_id), name)
    existing = db.scalar(
        select(models.SubDepartment).where(
            models.SubDepartment.department_id == department_id,
            models.SubDepartment.name == name,
        )
    )
    if existing is not None:
        return existing

    sub_department = models.SubDepartment(
        id=uuid.uuid4(), department_id=department_id, name=name, is_active=True
    )
    db.add(sub_department)
    db.flush()
    return sub_department


def update_sub_department(
    db: Session, sub_department: models.SubDepartment, name: str
) -> models.SubDepartment:
    sub_department.name = name
    db.flush()
    return sub_department


def get_or_create_employee_number_series(db: Session, company_id: uuid.UUID) -> models.NumberSeries:
    """Fetches this company's Employee Code number series (core.
    number_series, doctype='employee'), auto-provisioning a generic default
    (EMP-0001, EMP-0002, ...) the first time a company ever hires through
    this endpoint. Companies that need a different prefix/suffix/padding/
    starting number just get their row edited directly in the database --
    that's the whole point of keeping this config-driven instead of
    hardcoded (see generate_employee_code).

    Caller must hold the "employee_number_series" advisory lock for
    company_id first (see generate_employee_code) -- without it, two
    concurrent first-ever-hire requests for the same company could both see
    no row and both insert one, leaving two 'employee' series rows for one
    company with no way to tell which is authoritative."""
    series = db.scalar(
        select(models.NumberSeries).where(
            models.NumberSeries.company_id == company_id,
            models.NumberSeries.doctype == "employee",
        )
    )
    if series is not None:
        return series
    series = models.NumberSeries(
        id=uuid.uuid4(),
        company_id=company_id,
        doctype="employee",
        prefix="EMP-",
        suffix="",
        fiscal_infix=False,
        padding=4,
        current_no=0,
    )
    db.add(series)
    db.flush()
    return series


def generate_employee_code(db: Session, company_id: uuid.UUID) -> str:
    """Formats {prefix}{zero-padded sequence}{suffix} (e.g. EMP-0001,
    ACME-0001-HR) from this company's core.number_series row, atomically
    incrementing current_no so the value is never handed out twice.

    Concurrency-safe the same way create_designation/create_branch are: an
    advisory lock scoped to (company_id, "employee") serializes every
    concurrent hire for this company for the rest of the transaction, so
    two simultaneous POST /api/employees requests can't both read the same
    current_no and generate the same code. The lock is acquired before the
    series row is even fetched/created, so the auto-provisioning path above
    is covered too.

    The existence check below is a second, independent safety net (not the
    primary uniqueness mechanism -- the lock already is): it only matters if
    current_no was ever set/reset by hand to a value behind what's actually
    in use (e.g. after restoring older data), in which case it just skips
    forward past whatever's already taken instead of failing the hire."""
    _advisory_lock(db, "employee_number_series", str(company_id), "employee")
    series = get_or_create_employee_number_series(db, company_id)
    max_attempts = 1000
    for _ in range(max_attempts):
        series.current_no += 1
        code = f"{series.prefix}{str(series.current_no).zfill(series.padding)}{series.suffix}"
        collision = db.scalar(
            select(models.Employee.id).where(
                models.Employee.company_id == company_id,
                models.Employee.employee_code == code,
            )
        )
        if collision is None:
            db.flush()
            return code
    raise RuntimeError(
        f"Could not generate a unique employee code for company {company_id} "
        f"after {max_attempts} attempts -- check core.number_series"
    )


def get_employee_id_format(db: Session, company_id: uuid.UUID) -> models.NumberSeries:
    """Administration > General Settings' Employee ID Format card, and the
    Add Employee form's suggested-next-ID field -- both read this directly.
    No lock needed for a plain read."""
    return get_or_create_employee_number_series(db, company_id)


def preview_next_employee_code(series: models.NumberSeries) -> str:
    """Same format generate_employee_code uses, one number ahead of
    current_no -- a preview only, doesn't touch current_no (see
    EmployeeIdFormatOut's docstring for why: the number this previews might
    never actually get used, if the form is submitted with a different
    manually-entered code, or not submitted at all)."""
    return f"{series.prefix}{str(series.current_no + 1).zfill(series.padding)}{series.suffix}"


def update_employee_id_format(
    db: Session,
    company_id: uuid.UUID,
    prefix: str | None = None,
    suffix: str | None = None,
    padding: int | None = None,
    next_number: int | None = None,
) -> models.NumberSeries:
    """Organization Owner (or System Settings/RBAC admin) reconfiguring the
    Employee ID format. Locked the same way generate_employee_code is, so
    this can't race a concurrent hire that's mid-way through reading/
    incrementing current_no."""
    _advisory_lock(db, "employee_number_series", str(company_id), "employee")
    series = get_or_create_employee_number_series(db, company_id)
    if prefix is not None:
        series.prefix = prefix
    if suffix is not None:
        series.suffix = suffix
    if padding is not None:
        series.padding = padding
    if next_number is not None:
        series.current_no = next_number
    db.flush()
    return series


def create_employee(
    db: Session, company_id: uuid.UUID, payload
) -> models.Employee:
    """Concurrency-safe insert -- see generate_employee_code for how the
    Employee Code itself is generated and protected against concurrent
    duplicates.

    schemas.EmployeeCreate.employee_code is optional: if the caller (the
    Add Employee form, or the internal seed scripts' duck-typed payloads)
    supplies one, it's used as-is -- never silently overwritten -- after
    the uniqueness check below (ValueError on a collision, same
    company-scoped advisory lock pattern as create_department/
    create_branch). Left blank, this falls back to auto-generating from
    this company's configured number series."""
    company = db.get(models.Company, company_id)
    requested_code = (getattr(payload, "employee_code", None) or "").strip()
    if requested_code:
        _advisory_lock(db, "employee_code", str(company_id), requested_code)
        existing = db.scalar(
            select(models.Employee).where(
                models.Employee.company_id == company_id,
                models.Employee.employee_code == requested_code,
            )
        )
        if existing is not None:
            raise ValueError(f"Employee code '{requested_code}' already exists")
        employee_code = requested_code
    else:
        employee_code = generate_employee_code(db, company_id)
    # Work email is manually entered, not auto-generated: used exactly as
    # given (the caller may still pre-fill a suggestion client-side using
    # derive_company_email_domain, but nothing here silently rewrites
    # whatever domain the caller actually submitted). Global uniqueness
    # (across every tenant, via public.users/admin_users) is still
    # enforced below by find_user_by_email -- same protection a
    # forced-domain rewrite gave, without discarding what was typed.
    work_email = payload.work_email.strip()
    if "@" not in work_email or work_email.startswith("@") or work_email.endswith("@"):
        raise ValueError("A valid work email address is required")

    branch = db.scalar(
        select(models.Branch).where(
            models.Branch.company_id == company_id,
            func.lower(models.Branch.name) == payload.branch_name.strip().lower(),
        )
    )
    if branch is None:
        raise ValueError(f"Branch '{payload.branch_name}' not found")

    department = get_department_by_name(db, company_id, payload.department_name)
    if department is None:
        raise ValueError(f"Department '{payload.department_name}' not found")

    sub_department_id = None
    sub_dept_name = getattr(payload, 'sub_department_name', None)
    if sub_dept_name:
        sub_dept = db.scalar(
            select(models.SubDepartment).where(
                models.SubDepartment.department_id == department.id,
                func.lower(models.SubDepartment.name) == sub_dept_name.strip().lower(),
            )
        )
        if sub_dept is None:
            from .employee_validation import EmployeeInputError
            raise EmployeeInputError(
                f"Sub-department '{sub_dept_name}' not found in department '{department.name}'"
            )
        sub_department_id = sub_dept.id

    role = get_role_by_name(db, company_id, payload.role_name)
    if role is None:
        raise ValueError(f"Role '{payload.role_name}' not found")

    # H-21 / M-01: manager references must be active employees of THIS
    # company; the band one of the company's bands (422 via
    # employee_validation.EmployeeInputError).
    from . import employee_validation as _ev
    _ev.require_manager(db, company_id, payload.reporting_manager_id, "Reporting Manager")
    _ev.require_manager(db, company_id, getattr(payload, "dotted_line_manager_id", None),
                        "Second Reporting Manager")
    if (payload.reporting_manager_id is not None
            and payload.reporting_manager_id == getattr(payload, "dotted_line_manager_id", None)):
        raise _ev.EmployeeInputError("Second Reporting Manager must be different from the Reporting Manager.")
    band_problem = _ev.band_error(db, company_id, payload.band)
    if band_problem:
        raise _ev.EmployeeInputError(band_problem)

    matched_band_id = None
    if payload.band:
        matched_band = next(
            (b for b in list_bands(db, company_id, active_only=False) if b.band_number == payload.band),
            None,
        )
        if matched_band is not None:
            matched_band_id = matched_band.id

    # role.id/matched_band_id let a brand-new (or previously-unmapped)
    # designation get slotted into the hierarchy automatically instead of
    # landing on the old hardcoded band='Unassigned' -- see
    # get_or_create_designation_by_name. L-10: a designation that isn't in
    # the catalog is only created when the caller explicitly asks
    # (create_designation=True, the form's "+ Custom Designation"); a typo
    # is a 422 instead of a silent new catalog entry.
    if not getattr(payload, "create_designation", True) and db.scalar(
        select(models.Designation.id).where(
            models.Designation.company_id == company_id,
            func.lower(models.Designation.name) == payload.designation_name.strip().lower(),
        )
    ) is None:
        raise _ev.EmployeeInputError(
            f"Designation '{payload.designation_name}' doesn't exist. Pick one from the list, "
            "or choose '+ Custom Designation' to add it."
        )
    designation = get_or_create_designation_by_name(
        db, company_id, payload.designation_name, role_id=role.id, band_id=matched_band_id
    )

    # Same concurrency-safe pattern as generate_employee_code's lock above --
    # core.users/public.users/public.admin_users all key on this email with
    # no DB-level unique constraint scoping to *this* check, so without the
    # lock two simultaneous hires resolving to the same email (e.g. two
    # "Tarun"s joining the same day) could both pass find_user_by_email and
    # then collide on public.users_email_unique / admin_users_email_unique,
    # surfacing as an opaque IntegrityError instead of a clean 409.
    _advisory_lock(db, "employee_email", work_email)
    existing_user = find_user_by_email(db, work_email)
    if existing_user is not None:
        raise ValueError(f"An account with email '{work_email}' already exists")

    employee = models.Employee(
        id=uuid.uuid4(),
        company_id=company_id,
        branch_id=branch.id,
        department_id=department.id,
        designation_id=designation.id,
        sub_department_id=sub_department_id,
        employee_code=employee_code,
        first_name=payload.first_name,
        last_name=payload.last_name,
        work_email=work_email,
        gender=payload.gender,
        date_of_joining=payload.date_of_joining,
        employment_type=payload.employment_type,
        status=payload.status,
        pan=payload.pan,
        bank_name=payload.bank_name,
        bank_account_no=payload.bank_account_no,
        bank_ifsc=payload.bank_ifsc,
        uan=getattr(payload, "pf", None),
        esi_number=getattr(payload, "esi", None),
        tax_regime=getattr(payload, "tax_regime", None),
        work_mode=payload.work_mode,
        band=payload.band,
        annual_ctc=payload.annual_ctc,
        date_of_birth=parse_lenient_date(payload.date_of_birth) if payload.date_of_birth else None,
        is_active=True,
        blood_group=payload.blood_group,
        nationality=payload.nationality,
        marital_status=payload.marital_status,
        personal_email=payload.personal_email,
        personal_phone=payload.personal_phone,
        current_address=payload.current_address,
        permanent_address=payload.permanent_address,
        reporting_manager_id=payload.reporting_manager_id,
        dotted_line_manager_id=getattr(payload, "dotted_line_manager_id", None),
    )
    db.add(employee)
    db.flush()
    # N-04: current-fiscal-year allocation of every eligible capped leave type.
    from . import leave_policy

    leave_policy.allocate_for_new_employee(db, company_id, employee)

    full_name = f"{payload.first_name} {payload.last_name or ''}".strip()
    user = models.User(
        id=uuid.uuid4(),
        company_id=company_id,
        email=work_email,
        # core_users.phone has no source field on EmployeeCreate -- the Add
        # Employee form never collected a phone number at all (confirmed
        # against the live form), and the one column that used to double as
        # its source, core_employees.phone, is gone from the DB. Left null,
        # same as it always was in practice.
        password_hash=security.hash_password(payload.password),
        employee_id=employee.id,
        status="active",
        full_name=full_name,
    )
    db.add(user)
    db.flush()
    db.add(models.UserRole(user_id=user.id, role_id=role.id))

    # NOT get_tenant_slug() -- that ContextVar is set by deps.get_current_user
    # in its own threadpool-copied context (FastAPI runs sync dependencies
    # via run_in_threadpool/anyio.to_thread.run_sync), which never propagates
    # to this call. get_session_tenant_slug reads it off the `db` Session
    # object itself instead, which IS the same instance threaded through
    # every dependency in this request (see database.set_session_tenant_slug).
    from .database import get_session_tenant_slug as _get_session_tenant_slug
    live_tenant_slug = _get_session_tenant_slug(db) or settings.default_tenant_slug
    create_admin_user_login(
        db,
        user_id=user.id,
        email=work_email,
        password_hash=user.password_hash,
        full_name=full_name,
        role_name=role.name,
        tenant_slug=live_tenant_slug,
        company_id=company_id,
        employee_id=employee.id,
    )

    if payload.qualification or payload.institute or payload.specialization or payload.year_of_passing:
        db.add(
            models.EmployeeEducation(
                id=uuid.uuid4(),
                employee_id=employee.id,
                qualification=payload.qualification,
                institute=payload.institute,
                specialization=payload.specialization,
                year_of_passing=payload.year_of_passing,
            )
        )

    if payload.previous_employer:
        db.add(
            models.EmployeePriorExperience(
                id=uuid.uuid4(),
                employee_id=employee.id,
                employer_name=payload.previous_employer,
                years_experience=parse_leading_number(payload.experience_years or ""),
                domain=payload.domain,
            )
        )

    # hcm.employee_skills has UNIQUE(employee_id, skill) -- dedupe
    # defensively even though the Flutter client already does this, since
    # this endpoint can be called directly.
    seen_skills: set[str] = set()
    for skill in payload.skills:
        skill = skill.strip()
        if skill and skill not in seen_skills:
            seen_skills.add(skill)
            db.add(models.EmployeeSkill(id=uuid.uuid4(), employee_id=employee.id, skill=skill))

    for cert in payload.certifications:
        cert = cert.strip()
        if cert:
            db.add(
                models.Certification(id=uuid.uuid4(), employee_id=employee.id, name=cert)
            )

    if payload.emergency_contact_name:
        db.add(
            models.Contact(
                id=uuid.uuid4(),
                company_id=company_id,
                entity_type="employee",
                entity_id=employee.id,
                name=payload.emergency_contact_name,
                designation=payload.emergency_contact_relation,
                phone=payload.emergency_contact_phone,
                is_emergency=True,
            )
        )

    db.flush()

    # If one or more projects were specified at creation time, allocate the
    # employee to each -- an employee can work on multiple projects at
    # once. This is additive — existing allocations are unaffected, and an
    # empty list leaves the employee on bench (no allocation rows).
    project_names = getattr(payload, "project_names", None) or []
    seen_project_ids: set[uuid.UUID] = set()
    for raw_name in project_names:
        name = (raw_name or "").strip()
        if not name:
            continue
        project = db.scalar(
            select(models.Project).where(
                models.Project.company_id == company_id,
                func.lower(models.Project.name) == name.lower(),
            )
        )
        # Same project listed twice in the request -- skip the repeat
        # rather than create two allocation rows for it.
        if project is not None and project.id not in seen_project_ids:
            seen_project_ids.add(project.id)
            db.add(
                models.ProjectAllocation(
                    id=uuid.uuid4(),
                    project_id=project.id,
                    employee_id=employee.id,
                    allocation_pct=100,
                    start_date=company_today(db, company_id),
                )
            )
    if seen_project_ids:
        db.flush()

    return employee


def list_employees(db: Session, company_id: uuid.UUID) -> list[models.Employee]:
    return db.scalars(
        select(models.Employee)
        .where(models.Employee.company_id == company_id, models.Employee.is_active.is_(True))
        .order_by(models.Employee.first_name)
    ).all()


def _iso(d) -> str:
    return d.isoformat() if d is not None else "—"


def _num(x) -> float:
    return float(x) if x is not None else 0.0


def _dash(x: str | None) -> str:
    return x if x else "—"


def _employee_lifecycle_counts(events: "list[models.EmployeeLifecycleEvent]") -> tuple[int, int]:
    """(transfers, promotions) from a list of one employee's
    hcm_employee_lifecycle_events rows -- same classification
    routers/employees.py's list_lifecycle_events uses for company-wide rows:
    a "promotion" is a row whose designation actually changed; everything
    else counts as a transfer."""
    promotions = sum(
        1 for e in events
        if e.to_designation_id is not None and e.to_designation_id != e.from_designation_id
    )
    return len(events) - promotions, promotions


def _full_name(e: "models.Employee | None") -> str:
    if e is None:
        return "—"
    return f"{e.first_name} {e.last_name}".strip() if e.last_name else e.first_name


# Leave type codes this app's LeaveInfo model tracks -- ported 1:1 from the
# Dart Employee.leaveInfo shape (cl/sl/el/compOff/lop).
_LEAVE_CODE_MAP = {"CL": "cl", "SL": "sl", "EL": "el", "CO": "compOff", "LOP": "lop"}


def list_employees_directory(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0,
    visible_ids: set[uuid.UUID] | None = None,
) -> list[dict]:
    """Non-sensitive identity/org fields only (no salary, bank, PAN,
    personal contact info) for every active employee in the company --
    safe for any authenticated user, unlike list_employees_full below
    (gated behind require_people_access, since it includes real
    compensation/personal data). Exists because the app's shared employee
    roster (used everywhere for basic id -> name/department resolution --
    Leave, Attendance, org-chart displays, etc.) was previously sourced
    exclusively from the sensitive endpoint, so a role without full
    People-module access got an empty roster and couldn't even resolve
    their OWN manager's name. Built from one bulk query, same
    never-N-plus-1 approach as list_employees_full.

    limit/offset default to None/0 -- unbounded, i.e. the complete roster
    -- since this is consumed as a shared reference dataset (resolving
    "who is my manager" client-side, etc.) rather than a paginated screen;
    only an explicit caller opts into paging a slice of it."""
    q = (
        select(models.Employee)
        .options(
            selectinload(models.Employee.branch),
            selectinload(models.Employee.department),
            selectinload(models.Employee.designation),
        )
        .where(models.Employee.company_id == company_id, models.Employee.is_active.is_(True))
        .order_by(models.Employee.id)
    )
    if visible_ids is not None:
        q = q.where(models.Employee.id.in_(visible_ids or [uuid.UUID(int=0)]))
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    rows = list(db.scalars(q).all())
    return [
        {
            "id": str(e.id),
            "name": f"{e.first_name} {e.last_name or ''}".strip(),
            "department": e.department.name if e.department else None,
            "designation": e.designation.name if e.designation else None,
            "branch": e.branch.name if e.branch else None,
            "reporting_manager_id": str(e.reporting_manager_id) if e.reporting_manager_id else None,
            "dotted_line_manager_id": str(e.dotted_line_manager_id) if e.dotted_line_manager_id else None,
        }
        for e in rows
    ]


def list_employees_full(
    db: Session,
    company_id: uuid.UUID,
    limit: int | None = None,
    cursor: str | None = None,
    visible_ids: set[uuid.UUID] | None = None,
) -> list[dict] | dict:
    """Every column of core.employees plus every satellite table the
    Employee Directory / Employee Profile screens read. Built from bulk
    queries (never one query per employee) so it stays fast for any
    roster size.

    Without limit/cursor (default): returns list[dict] of all employees.
    With limit set: applies keyset cursor pagination on (first_name, id)
    and returns {"items": list[dict], "next_cursor": str|None, "has_more": bool}."""
    # PERF-07: many-to-one lookups joined into the main query (one round
    # trip) instead of 6 separate selectin queries.
    q = (
        select(models.Employee)
        .options(
            joinedload(models.Employee.branch),
            joinedload(models.Employee.department),
            joinedload(models.Employee.sub_department),
            joinedload(models.Employee.designation),
            joinedload(models.Employee.reporting_manager),
            joinedload(models.Employee.dotted_line_manager),
        )
        .where(models.Employee.company_id == company_id, models.Employee.is_active.is_(True))
    )
    if visible_ids is not None:  # in the query, so pages stay full and correct
        q = q.where(models.Employee.id.in_(visible_ids or [uuid.UUID(int=0)]))
    if cursor:
        # API-04/C18: opaque cursor (legacy "<first_name>:<uuid>" still
        # accepted); an undecodable one raises InvalidCursor -> 400 instead
        # of silently restarting at page 1.
        last_fname, last_id = decode_cursor(cursor)
        q = q.where(
            (models.Employee.first_name > last_fname)
            | (
                (models.Employee.first_name == last_fname)
                & (models.Employee.id > last_id)
            )
        )
    q = q.order_by(models.Employee.first_name, models.Employee.id)
    if limit is not None:
        q = q.limit(limit + 1)
    rows = list(db.scalars(q).all())
    has_more = limit is not None and len(rows) > limit
    employees = rows[:limit] if limit is not None else rows
    next_cursor = (
        encode_cursor(employees[-1].first_name, employees[-1].id)
        if has_more and employees
        else None
    )
    ids = [e.id for e in employees]
    if not ids:
        if limit is not None:
            return {"items": [], "next_cursor": None, "has_more": False}
        return []

    business_unit_ids = {
        e.department.business_unit_id for e in employees if e.department and e.department.business_unit_id
    }
    business_units_by_id = {
        bu.id: bu.name
        for bu in (
            db.scalars(select(models.BusinessUnit).where(models.BusinessUnit.id.in_(business_unit_ids)))
            if business_unit_ids
            else []
        )
    }

    education = {
        row.employee_id: row
        for row in db.scalars(select(models.EmployeeEducation).where(models.EmployeeEducation.employee_id.in_(ids)))
    }
    experience = {
        row.employee_id: row
        for row in db.scalars(
            select(models.EmployeePriorExperience).where(models.EmployeePriorExperience.employee_id.in_(ids))
        )
    }
    skills: dict[uuid.UUID, list[str]] = {}
    for row in db.scalars(select(models.EmployeeSkill).where(models.EmployeeSkill.employee_id.in_(ids))):
        skills.setdefault(row.employee_id, []).append(row.skill)

    certifications: dict[uuid.UUID, list[str]] = {}
    for row in db.scalars(select(models.Certification).where(models.Certification.employee_id.in_(ids))):
        certifications.setdefault(row.employee_id, []).append(row.name)

    emergency_contacts = {
        row.entity_id: row
        for row in db.scalars(
            select(models.Contact).where(
                models.Contact.company_id == company_id,
                models.Contact.entity_type == "employee",
                models.Contact.entity_id.in_(ids),
                models.Contact.is_emergency.is_(True),
            )
        )
    }

    benefits = {
        row.employee_id: row
        for row in db.scalars(select(models.EmployeeBenefits).where(models.EmployeeBenefits.employee_id.in_(ids)))
    }

    assets: dict[uuid.UUID, list[dict]] = {}
    for aa, inv in db.execute(
        select(models.AssetAssignment, models.AssetInventoryItem)
        .join(models.AssetInventoryItem, models.AssetInventoryItem.id == models.AssetAssignment.asset_id)
        .where(models.AssetAssignment.employee_id.in_(ids))
    ).all():
        assets.setdefault(aa.employee_id, []).append(
            {"type": inv.asset_type, "tag": inv.asset_tag, "assigned": _iso(aa.assigned_on)}
        )

    documents: dict[uuid.UUID, list[dict]] = {}
    for row in db.scalars(select(models.DocumentRecord).where(models.DocumentRecord.employee_id.in_(ids))):
        documents.setdefault(row.employee_id, []).append(
            {"type": row.document_type, "status": row.status, "date": _iso(row.uploaded_on)}
        )

    projects: dict[uuid.UUID, list[dict]] = {}
    # PERF-07: team leads (by "Team Lead" project role), their names, and
    # department/branch names are resolved for ALL allocations at once
    # below, instead of 3-5 queries per project/allocation row.
    project_alloc_rows = db.execute(
        select(models.ProjectAllocation, models.Project)
        .join(models.Project, models.Project.id == models.ProjectAllocation.project_id)
        .options(
            selectinload(models.ProjectAllocation.project_role),
            # PERF-06: only the project columns this roster uses -- the full
            # pm_projects row (descriptions etc.) for every allocation was
            # the slowest statement of this endpoint.
            load_only(
                models.Project.id, models.Project.name, models.Project.is_billable,
                models.Project.project_manager_id, models.Project.company_id,
            ),
            joinedload(models.Project.project_manager),
        )
        .where(
            models.ProjectAllocation.employee_id.in_(ids),
            # pm_resource_allocations has no company_id column of its own --
            # without this, an allocation whose project belongs to a
            # DIFFERENT company (tenants sharing this schema store every
            # company's projects in the same table) would still surface
            # that other company's project name/PM/team-lead here, since
            # `ids` alone only guarantees the *employee* is in this company.
            models.Project.company_id == company_id,
        )
    ).all()
    project_team_leads = resolve_project_team_lead_ids_bulk(
        db, [proj.id for _pa, proj in project_alloc_rows]
    )
    team_lead_names = employee_display_names_bulk(db, project_team_leads.values())
    alloc_dept_ids = set()
    alloc_branch_ids = set()
    for pa, _proj in project_alloc_rows:
        department_id, branch_id = allocation_department_branch(pa)
        alloc_dept_ids.add(department_id)
        alloc_branch_ids.add(branch_id)
    alloc_dept_ids.discard(None)
    alloc_branch_ids.discard(None)
    alloc_dept_names = (
        dict(db.execute(
            select(models.Department.id, models.Department.name).where(models.Department.id.in_(alloc_dept_ids))
        ).all())
        if alloc_dept_ids else {}
    )
    alloc_branch_names = (
        dict(db.execute(
            select(models.Branch.id, models.Branch.name).where(models.Branch.id.in_(alloc_branch_ids))
        ).all())
        if alloc_branch_ids else {}
    )
    for pa, proj in project_alloc_rows:
        department_id, branch_id = allocation_department_branch(pa)
        team_lead_id = project_team_leads.get(proj.id)
        projects.setdefault(pa.employee_id, []).append(
            {
                "name": proj.name,
                "role": pa.project_role.name if pa.project_role else "—",
                "allocation": pa.allocation_pct,
                "billable": proj.is_billable,
                "teamLead": team_lead_names.get(team_lead_id, "—") if team_lead_id else "—",
                "department": alloc_dept_names.get(department_id, "—") if department_id else "—",
                "branch": alloc_branch_names.get(branch_id, "—") if branch_id else "—",
                "projectManager": (
                    _full_name(proj.project_manager)
                    if getattr(proj, "project_manager_id", None)
                    else "—"
                ),
            }
        )

    latest_appraisal: dict[uuid.UUID, tuple] = {}
    for ap, cycle in db.execute(
        select(models.Appraisal, models.AppraisalCycle)
        .join(models.AppraisalCycle, models.AppraisalCycle.id == models.Appraisal.cycle_id)
        .where(models.Appraisal.employee_id.in_(ids))
        .order_by(models.AppraisalCycle.to_date.desc())
    ).all():
        latest_appraisal.setdefault(ap.employee_id, (ap, cycle))  # first = latest cycle

    goal_progress: dict[uuid.UUID, list[float]] = {}
    for row in db.scalars(select(models.Goal).where(models.Goal.employee_id.in_(ids))):
        goal_progress.setdefault(row.employee_id, []).append(_num(row.progress_pct))

    current_shift: dict[uuid.UUID, str] = {}
    for sa, shift in db.execute(
        select(models.ShiftAssignment, models.Shift)
        .join(models.Shift, models.Shift.id == models.ShiftAssignment.shift_id)
        .where(
            models.ShiftAssignment.employee_id.in_(ids),
            _shift_assignment_active_on(company_today(db)),
        )
        .order_by(models.ShiftAssignment.from_date.desc())
    ).all():
        current_shift.setdefault(sa.employee_id, shift.name)  # the active assignment

    leave_alloc: dict[uuid.UUID, dict[str, dict]] = {}
    # N-05: bucketed by leave_policy.leave_slot (code OR name -- a seeded
    # 'CASUALLEAV' is still Casual Leave); oldest fiscal year first so the
    # latest year's allocation wins.
    from . import leave_policy as _leave_policy

    for la, lt in db.execute(
        select(models.LeaveAllocation, models.LeaveType)
        .join(models.LeaveType, models.LeaveType.id == models.LeaveAllocation.leave_type_id)
        .outerjoin(models.FiscalYear, models.FiscalYear.id == models.LeaveAllocation.fiscal_year_id)
        .where(models.LeaveAllocation.employee_id.in_(ids))
        .order_by(models.FiscalYear.start_date.asc().nulls_first())
    ).all():
        leave_alloc.setdefault(la.employee_id, {})[_leave_policy.leave_slot(lt) or lt.code] = {
            "allocated": _num(la.allocated_days) + _num(la.carried_forward_days),
            "used": _num(la.used_days),
        }

    tax_decl: dict[uuid.UUID, models.TaxDeclaration] = {}
    for row in db.scalars(
        select(models.TaxDeclaration)
        .where(models.TaxDeclaration.employee_id.in_(ids))
        .order_by(models.TaxDeclaration.fiscal_year.desc())
    ):
        tax_decl.setdefault(row.employee_id, row)  # first = latest fiscal year

    latest_slip: dict[uuid.UUID, tuple] = {}
    for slip, run in db.execute(
        select(models.SalarySlip, models.PayrollRun)
        .join(models.PayrollRun, models.PayrollRun.id == models.SalarySlip.payroll_run_id)
        .where(models.SalarySlip.employee_id.in_(ids))
        .order_by(models.PayrollRun.period_year.desc(), models.PayrollRun.period_month.desc())
    ).all():
        latest_slip.setdefault(slip.employee_id, (slip, run))  # first = most recent run

    slip_ids = [slip.id for slip, _run in latest_slip.values()]
    slip_lines: dict[uuid.UUID, dict[str, float]] = {}
    # Same lines keyed by component code: names are company-editable
    # ("Basic" vs "Basic Salary"), codes are stable (BASIC everywhere).
    slip_codes: dict[uuid.UUID, dict[str, float]] = {}
    if slip_ids:
        for line in db.scalars(
            select(models.SalarySlipLine)
            .options(joinedload(models.SalarySlipLine.component))
            .where(models.SalarySlipLine.slip_id.in_(slip_ids))
        ):
            slip_lines.setdefault(line.slip_id, {})[line.component.name] = _num(line.amount)
            codes = slip_codes.setdefault(line.slip_id, {})
            code = (line.component.code or "").upper()
            codes[code] = codes.get(code, 0.0) + _num(line.amount)

    role_by_employee: dict[uuid.UUID, str] = {}
    for user_row, role_name in db.execute(
        select(models.User.employee_id, models.Role.name)
        .join(models.UserRole, models.UserRole.user_id == models.User.id)
        .join(models.Role, models.Role.id == models.UserRole.role_id)
        .where(models.User.employee_id.in_(ids))
        .order_by(*USER_ROLE_ORDER)
    ).all():
        role_by_employee.setdefault(user_row, role_name)  # first = primary role (C8, USER_ROLE_ORDER)

    today = company_today(db, company_id)  # DATA-03: company-timezone "today"
    attendance_by_employee: dict[uuid.UUID, list] = {}
    for row in db.scalars(
        select(models.AttendanceRecord).where(models.AttendanceRecord.employee_id.in_(ids))
    ):
        attendance_by_employee.setdefault(row.employee_id, []).append(row)

    awards_by_employee: dict[uuid.UUID, list[str]] = {}
    for row in db.scalars(
        select(models.Recognition)
        .where(models.Recognition.employee_id.in_(ids))
        .order_by(models.Recognition.given_on.desc())
    ):
        awards_by_employee.setdefault(row.employee_id, []).append(row.badge)

    lifecycle_events_by_employee: dict[uuid.UUID, list] = {}
    for row in db.scalars(
        select(models.EmployeeLifecycleEvent).where(models.EmployeeLifecycleEvent.employee_id.in_(ids))
    ):
        lifecycle_events_by_employee.setdefault(row.employee_id, []).append(row)

    latest_exit_by_employee: dict[uuid.UUID, models.ExitRequestModel] = {}
    for row in db.scalars(
        select(models.ExitRequestModel)
        .where(models.ExitRequestModel.employee_id.in_(ids))
        .order_by(models.ExitRequestModel.created_at.desc())
    ):
        latest_exit_by_employee.setdefault(row.employee_id, row)  # first = most recent

    result: list[dict] = []
    for e in employees:
        eid = e.id
        edu = education.get(eid)
        exp = experience.get(eid)
        emc = emergency_contacts.get(eid)
        ben = benefits.get(eid)
        alloc = leave_alloc.get(eid, {})
        tax = tax_decl.get(eid)
        slip_pair = latest_slip.get(eid)
        slip, run = slip_pair if slip_pair else (None, None)
        lines = slip_lines.get(slip.id, {}) if slip else {}
        codes = slip_codes.get(slip.id, {}) if slip else {}
        appraisal_pair = latest_appraisal.get(eid)
        appraisal, cycle = appraisal_pair if appraisal_pair else (None, None)
        goals = goal_progress.get(eid, [])
        goals_completed_pct = round(sum(goals) / len(goals)) if goals else 0

        # Scoped to the current calendar month, matching get_employee_attendance
        # -- previously present/absent/overtime silently summed every record
        # ever created for this employee, and late was hardcoded to 0 despite
        # AttendanceRecord.status genuinely being set to "late" elsewhere.
        this_month_records = [
            r for r in attendance_by_employee.get(eid, [])
            if r.attendance_date.month == today.month and r.attendance_date.year == today.year
        ]
        # "regularized" counts as present: an approved Attendance
        # Regularization writes real check_in/check_out onto this exact
        # record (see apply_regularization_to_attendance_record) -- the
        # employee did attend, the correction just replaced a missing or
        # wrong punch, so it shouldn't silently drop out of the monthly
        # present/absent/late tally.
        present = sum(
            1 for r in this_month_records
            if r.status in ("present", "regularized", "early_in", "off_shift")
        )
        absent = sum(1 for r in this_month_records if r.status == "absent")
        late = sum(1 for r in this_month_records if r.status == "late")
        this_month_hrs = sum(_num(r.work_hours) for r in this_month_records)
        overtime_hrs = sum(_num(getattr(r, "overtime_hours", None)) for r in this_month_records)
        transfers, promotions = _employee_lifecycle_counts(lifecycle_events_by_employee.get(eid, []))

        result.append(
            {
                "id": str(eid),
                "code": e.employee_code,
                "name": _full_name(e),
                "gender": _dash(e.gender),
                "designation": e.designation.name if e.designation else "—",
                "band": e.band or 0,
                "department": e.department.name if e.department else "—",
                "subDept": e.sub_department.name if e.sub_department else "—",
                "branch": e.branch.name if e.branch else "—",
                "manager": _full_name(e.reporting_manager) if e.reporting_manager else "—",
                "reportingManagerId": e.reporting_manager_id,
                "dottedLineManagerId": e.dotted_line_manager_id,
                "type": e.employment_type,
                "mode": _dash(e.work_mode),
                "doj": _iso(e.date_of_joining),
                "status": e.status,
                "ctc": e.annual_ctc or 0,
                "role": role_by_employee.get(eid, "Employee (ESS)"),
                "city": e.branch.city if e.branch and e.branch.city else "—",
                "email": _dash(e.work_email),
                "photoUrl": e.photo_url,
                "personal": {
                    "dob": _iso(e.date_of_birth),
                    "gender": _dash(e.gender),
                    "blood": _dash(e.blood_group),
                    "nationality": _dash(e.nationality),
                    "marital": _dash(e.marital_status),
                    "currentAddr": _dash(e.current_address),
                    "permAddr": _dash(e.permanent_address),
                    "personalEmail": _dash(e.personal_email),
                    "personalPhone": _dash(e.personal_phone),
                    "emergencyName": emc.name if emc else "—",
                    "emergencyRel": _dash(emc.designation) if emc else "—",
                    "emergencyPhone": _dash(emc.phone) if emc else "—",
                },
                "professional": {
                    "businessUnit": (
                        business_units_by_id.get(e.department.business_unit_id, "—")
                        if e.department and e.department.business_unit_id
                        else "—"
                    ),
                    "division": "—",
                    "department": e.department.name if e.department else "—",
                    "designation": e.designation.name if e.designation else "—",
                    "band": str(e.band) if e.band else "—",
                    "reportingManager": _full_name(e.reporting_manager) if e.reporting_manager else "—",
                    "dottedLine": _full_name(e.dotted_line_manager) if e.dotted_line_manager else "—",
                    "employmentType": e.employment_type,
                    "workMode": _dash(e.work_mode),
                    "shift": current_shift.get(eid, "—"),
                    "doj": _iso(e.date_of_joining),
                    "confirmationDate": _iso(e.confirmation_date),
                    "probationEnd": _iso(e.probation_end_date),
                    "status": e.status,
                    "documents": [],
                },
                "education": {
                    "qualification": _dash(edu.qualification) if edu else "—",
                    "institute": _dash(edu.institute) if edu else "—",
                    "specialization": _dash(edu.specialization) if edu else "—",
                    "yearOfPassing": edu.year_of_passing if edu and edu.year_of_passing else 0,
                    "certifications": ", ".join(certifications.get(eid, [])) or "—",
                    "previousEmployers": exp.employer_name if exp else "—",
                    "experienceYears": str(exp.years_experience) if exp and exp.years_experience else "—",
                    "skills": ", ".join(skills.get(eid, [])) or "—",
                    "domain": _dash(exp.domain) if exp else "—",
                    "documents": [],
                },
                "payrollInfo": {
                    "ctc": e.annual_ctc or 0,
                    "basic": int(codes.get("BASIC", lines.get("Basic Salary", 0))),
                    "gross": int(_num(slip.gross_pay)) if slip else 0,
                    "net": int(_num(slip.net_pay)) if slip else 0,
                    "variable": int(codes.get("VARIABLE", codes.get("VARPAY", lines.get("Variable Pay / Bonus", 0)))),
                    "pf": _dash(e.uan),
                    "esi": _dash(e.esi_number),
                    "pan": _dash(e.pan),
                    "taxRegime": e.tax_regime or (tax.tax_regime if tax else "—"),
                    "bank": _dash(e.bank_name),
                    "account": _dash(e.bank_account_no),
                    "ifsc": _dash(e.bank_ifsc),
                    "periodMonth": run.period_month if run else None,
                    "periodYear": run.period_year if run else None,
                },
                "benefitsInfo": {
                    "insurance": _dash(ben.insurance_plan) if ben else "—",
                    "dependents": ben.dependents_covered if ben else 0,
                    "esop": str(ben.esop_units) if ben else "0",
                    "cab": "Yes" if ben and ben.cab_facility else "No",
                    "meal": "Yes" if ben and ben.meal_card else "No",
                    "internet": "Yes" if ben and ben.internet_reimbursement else "No",
                    "wellness": "Yes" if ben and ben.wellness_program else "No",
                    "learningBudget": f"{_num(ben.learning_budget_used)}/{_num(ben.learning_budget_total)}"
                    if ben
                    else "—",
                },
                "attendanceInfo": {
                    "present": present,
                    "absent": absent,
                    "late": late,
                    "wfh": 0,
                    "overtimeHrs": int(overtime_hrs),
                    "thisMonthHrs": int(this_month_hrs),
                },
                "leaveInfo": {
                    "cl": int(alloc.get("CL", {}).get("allocated", 0) - alloc.get("CL", {}).get("used", 0)),
                    "sl": int(alloc.get("SL", {}).get("allocated", 0) - alloc.get("SL", {}).get("used", 0)),
                    "el": int(alloc.get("EL", {}).get("allocated", 0) - alloc.get("EL", {}).get("used", 0)),
                    "compOff": int(alloc.get("CO", {}).get("allocated", 0) - alloc.get("CO", {}).get("used", 0)),
                    "lop": int(alloc.get("LOP", {}).get("used", 0)),
                },
                "projectsInfo": projects.get(eid, []),
                "performanceInfo": {
                    "rating": appraisal.final_rating if appraisal and appraisal.final_rating else "—",
                    "goalsCompleted": f"{goals_completed_pct}%",
                    "lastReview": _iso(cycle.to_date) if cycle else "—",
                    "nextReview": "—",
                    "awards": awards_by_employee.get(eid, []),
                },
                "assetsInfo": assets.get(eid, []),
                "documentsInfo": documents.get(eid, []),
                "lifecycleInfo": {
                    # onboarding: no ORM mapping exists anywhere for
                    # hcm_onboardings/hcm_onboarding_tasks -- "—" is honest,
                    # unlike the previous hardcoded "Completed" literal.
                    "onboarding": "—",
                    "transfers": transfers,
                    "promotions": promotions,
                    "exitStatus": (
                        latest_exit_by_employee[eid].status.replace("_", " ").title()
                        if eid in latest_exit_by_employee
                        else ("Active" if e.status == "active" else e.status)
                    ),
                    "history": [],
                },
            }
        )
    if limit is not None:
        return {"items": result, "next_cursor": next_cursor, "has_more": has_more}
    return result


# ---------------------------------------------------------------------------
# Shifts / Holidays / Leave Types -- reference data
# ---------------------------------------------------------------------------


def list_shifts(
    db: Session, company_id: uuid.UUID, active_only: bool = True
) -> list[models.Shift]:
    """active_only=True backs the "Assign Employee" picker (never offer a
    deactivated shift); active_only=False backs the Shift Management admin
    table itself, matching list_bands' active_only shape, so a deactivated
    shift stays visible with a Reactivate action instead of disappearing."""
    query = select(models.Shift).where(models.Shift.company_id == company_id)
    if active_only:
        query = query.where(models.Shift.is_active.is_(True))
    return db.scalars(query.order_by(models.Shift.name)).all()


def get_shift_for_update(
    db: Session, shift_id: uuid.UUID, company_id: uuid.UUID
) -> models.Shift | None:
    """Row-locked, company-scoped read -- same pattern as
    get_band_for_update. Scoping the fetch by company_id here (rather than
    trusting a bare id lookup) is what makes edit/deactivate/delete
    tenant-safe in the shared acme schema."""
    return db.scalar(
        select(models.Shift)
        .where(models.Shift.id == shift_id, models.Shift.company_id == company_id)
        .with_for_update()
    )


def _find_conflicting_shift_name(
    db: Session, company_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
) -> models.Shift | None:
    # L-11: trimmed + case-insensitive ("Morning" == " morning ").
    query = select(models.Shift).where(
        models.Shift.company_id == company_id,
        func.lower(func.btrim(models.Shift.name)) == " ".join((name or "").split()).lower(),
    )
    if exclude_id is not None:
        query = query.where(models.Shift.id != exclude_id)
    return db.scalars(query.limit(1)).first()


def _check_shift_times(start_time: datetime.time, end_time: datetime.time, is_night: bool) -> None:
    """M-15: a day shift must end after it starts; only a night shift may
    cross midnight (end < start)."""
    if start_time == end_time:
        raise ValueError("Start Time and End Time cannot be the same")
    if end_time < start_time and not is_night:
        raise ValueError("End Time must be after Start Time for a day shift (mark it as a Night shift to cross midnight)")


def create_shift(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    start_time: datetime.time,
    end_time: datetime.time,
    is_night: bool,
    break_minutes: int = 60,
    max_breaks: int = 1,
) -> models.Shift:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Shift name is required")
    _check_shift_times(start_time, end_time, is_night)
    if _find_conflicting_shift_name(db, company_id, name) is not None:
        raise ValueError(f"Shift '{name}' already exists")
    shift = models.Shift(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        is_night=is_night,
        is_active=True,
        break_minutes=break_minutes,
        max_breaks=max_breaks,
    )
    db.add(shift)
    db.flush()
    return shift


def update_shift(db: Session, shift: models.Shift, updates: dict) -> models.Shift:
    """Partial update (name/start_time/end_time/is_night/is_active), same
    shape as update_band. hcm_shifts has no updated_by/updated_at columns at
    all (verified against db/full_db.sql) -- there is nothing to stamp; the
    router's create_audit_log call is this table's only change record."""
    if "name" in updates:
        updates["name"] = " ".join((updates["name"] or "").split())
        if not updates["name"]:
            raise ValueError("Shift name is required")
    new_name = updates.get("name", shift.name)
    new_start = updates.get("start_time", shift.start_time)
    new_end = updates.get("end_time", shift.end_time)
    if "name" in updates:
        if _find_conflicting_shift_name(db, shift.company_id, new_name, exclude_id=shift.id) is not None:
            raise ValueError(f"Shift '{new_name}' already exists")
    if {"start_time", "end_time", "is_night"} & updates.keys():
        _check_shift_times(new_start, new_end, bool(updates.get("is_night", shift.is_night)))
    elif new_start == new_end:
        raise ValueError("Start Time and End Time cannot be the same")
    for field, value in updates.items():
        setattr(shift, field, value)
    db.flush()
    return shift


def delete_shift(db: Session, shift: models.Shift) -> None:
    """Hard delete -- guarded at the application level (hcm_shift_assignments
    has no DB foreign key on shift_id, so nothing would stop an orphaned
    reference otherwise): a shift with any assignment history, past or
    present, can't be removed out from under it. Same pattern as
    delete_band -- raises ValueError so the router turns it into a clean 409
    telling the caller to deactivate instead."""
    in_use = db.scalar(
        select(func.count())
        .select_from(models.ShiftAssignment)
        .where(models.ShiftAssignment.shift_id == shift.id)
    )
    if in_use:
        raise ValueError(
            f"Cannot delete shift '{shift.name}' -- {in_use} employee assignment(s) "
            "reference it. Deactivate it instead."
        )
    db.delete(shift)
    db.flush()


def _shift_assignment_active_on(as_of_date: datetime.date):
    """The ONE definition of an "active" shift assignment on a date: it has
    started (from_date <= as_of_date) and has not ended (to_date IS NULL or
    to_date >= as_of_date). create_shift_assignment / end_shift_assignment
    keep an employee's rows non-overlapping, so at most one row matches."""
    return and_(
        models.ShiftAssignment.from_date <= as_of_date,
        or_(
            models.ShiftAssignment.to_date.is_(None),
            models.ShiftAssignment.to_date >= as_of_date,
        ),
    )


def count_active_shift_assignments(db: Session, shift_id: uuid.UUID) -> int:
    today = company_today()
    return (
        db.scalar(
            select(func.count(models.ShiftAssignment.id)).where(
                models.ShiftAssignment.shift_id == shift_id,
                _shift_assignment_active_on(today),
            )
        )
        or 0
    )


def create_shift_assignment(
    db: Session, employee_id: uuid.UUID, shift_id: uuid.UUID, from_date: datetime.date
) -> models.ShiftAssignment:
    """Makes shift_id the employee's shift from from_date on -- an employee
    is only ever actively assigned to ONE shift. Every assignment of theirs
    still in effect on/after from_date ends the day before (to_date =
    from_date - 1), so there is no handoff day on which two rows are both
    active. Rows that ended earlier (history) are never touched. A row that
    itself started on/after from_date (a same-day reassignment, or a
    previously scheduled future change) is superseded the same way and so
    is never effective; it stays as history.

    Serialized per employee (advisory lock): two concurrent reassignments of
    the same employee can't both read "no active row" and leave two open.

    previous_shift_id records the shift the employee was on right before
    from_date (history); created_*/updated_* are stamped by
    database._stamp_audit_fields."""
    _advisory_lock(db, "shift_assignment", str(employee_id))
    before = get_active_shift_assignment(db, employee_id, from_date - datetime.timedelta(days=1))
    if before is None:
        before = get_active_shift_assignment(db, employee_id, from_date)
    still_open = db.scalars(
        select(models.ShiftAssignment).where(
            models.ShiftAssignment.employee_id == employee_id,
            or_(
                models.ShiftAssignment.to_date.is_(None),
                models.ShiftAssignment.to_date >= from_date,
            ),
        )
    ).all()
    for row in still_open:
        row.to_date = from_date - datetime.timedelta(days=1)
    assignment = models.ShiftAssignment(
        id=uuid.uuid4(),
        employee_id=employee_id,
        shift_id=shift_id,
        from_date=from_date,
        to_date=None,
        previous_shift_id=before.shift_id if before is not None else None,
    )
    db.add(assignment)
    db.flush()
    return assignment


def get_active_shift_assignment(
    db: Session, employee_id: uuid.UUID, as_of_date: datetime.date
) -> "models.ShiftAssignment | None":
    """The employee's assignment row active on as_of_date (see
    _shift_assignment_active_on), or None."""
    return db.scalar(
        select(models.ShiftAssignment)
        .where(
            models.ShiftAssignment.employee_id == employee_id,
            _shift_assignment_active_on(as_of_date),
        )
        .order_by(models.ShiftAssignment.from_date.desc())
        .limit(1)
    )


def current_shift_assignments(
    db: Session, company_id: uuid.UUID, as_of_date: datetime.date
) -> list[tuple[models.ShiftAssignment, models.Shift]]:
    """Every employee's active assignment (with its shift) in the company on
    as_of_date -- lets the Shift Management checklists show which shift each
    employee is currently on, so a reassignment is visible before saving."""
    return list(
        db.execute(
            select(models.ShiftAssignment, models.Shift)
            .join(models.Shift, models.Shift.id == models.ShiftAssignment.shift_id)
            .where(
                models.Shift.company_id == company_id,
                _shift_assignment_active_on(as_of_date),
            )
        ).all()
    )


SHIFT_CHANGED_EVENT = "shift_changed"


def queue_shift_change_push(
    db: Session,
    company_id: uuid.UUID,
    shift_id: uuid.UUID | None,
    employee_ids: "Iterable[uuid.UUID] | None" = None,
) -> None:
    """Real-time sync for shift changes: after the current transaction
    COMMITS (ws_manager.queue_push), every signed-in user of the company gets
    a small {"event": "shift_changed"} message over the notifications
    WebSocket. Open screens then re-read the shift from the database:
    Attendance (Check-In/Out and break gating), Apply Leave (half-day time),
    Employee Profile (Shift) and Shift Management (lists / counts).
    It carries no shift values itself -- the database stays the only source
    of truth. employee_ids = whose active shift changed (None = possibly
    everyone on shift_id, e.g. its times were edited). Not a Notification
    row: nothing is added to the bell feed. Best-effort, like
    create_notification -- never the reason a request fails."""
    try:
        payload = {
            "event": SHIFT_CHANGED_EVENT,
            "shift_id": str(shift_id) if shift_id else None,
            "employee_ids": None if employee_ids is None else sorted({str(e) for e in employee_ids}),
        }
        user_ids = db.scalars(
            select(models.User.id).where(
                models.User.company_id == company_id, models.User.status == "active"
            )
        ).all()
        for uid in user_ids:
            ws_manager.queue_push(db, uid, payload)
    except Exception:  # pragma: no cover - push is best-effort
        logger.warning("could not queue shift_changed push", exc_info=True)


def employee_ids_outside_company(
    db: Session, employee_ids: list[uuid.UUID], company_id: uuid.UUID
) -> list[uuid.UUID]:
    """Which of employee_ids do NOT belong to company_id -- empty means
    every id is valid. Guards a bulk shift assignment against referencing
    another company's employee inside the shared acme schema."""
    found = set(
        db.scalars(
            select(models.Employee.id).where(
                models.Employee.id.in_(employee_ids),
                models.Employee.company_id == company_id,
            )
        ).all()
    )
    return [eid for eid in employee_ids if eid not in found]


def bulk_create_shift_assignments(
    db: Session,
    shift_id: uuid.UUID,
    employee_ids: list[uuid.UUID],
    from_date: datetime.date,
) -> dict:
    """Assigns every employee in employee_ids to shift_id in one operation.
    Duplicate prevention: an employee already actively assigned to this
    EXACT shift as of from_date is skipped rather than routed through
    create_shift_assignment's close-and-reopen, which would otherwise leave
    a redundant zero-length history row behind for no state change at all.
    Also de-duplicates the input list itself, so selecting the same
    checkbox row twice behaves like selecting it once."""
    assigned: list[tuple[uuid.UUID, uuid.UUID]] = []
    skipped_duplicate: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for employee_id in employee_ids:
        if employee_id in seen:
            continue
        seen.add(employee_id)
        current = get_active_shift_assignment(db, employee_id, from_date)
        if current is not None and current.shift_id == shift_id:
            skipped_duplicate.append(employee_id)
            continue
        assignment = create_shift_assignment(db, employee_id, shift_id, from_date)
        assigned.append((employee_id, assignment.id))
    return {"assigned": assigned, "skipped_duplicate": skipped_duplicate}


def list_active_shift_assignments(
    db: Session, shift_id: uuid.UUID, as_of_date: "datetime.date | None" = None
) -> list[models.ShiftAssignment]:
    """Every ShiftAssignment row currently active for this one shift -- the
    per-shift counterpart of get_active_shift_assignment (which is
    per-employee). Backs the Shift Management > Manage Employees
    checklist's pre-checked state; count_active_shift_assignments already
    computed a count from this exact same filter, just without
    materializing the rows themselves."""
    as_of_date = as_of_date or company_today()
    return list(
        db.scalars(
            select(models.ShiftAssignment).where(
                models.ShiftAssignment.shift_id == shift_id,
                _shift_assignment_active_on(as_of_date),
            )
        ).all()
    )


def end_shift_assignment(
    db: Session, shift_id: uuid.UUID, employee_id: uuid.UUID, as_of_date: datetime.date
) -> bool:
    """Closes out employee_id's active assignment to shift_id specifically
    -- the Manage Employees checklist's "uncheck to unassign" action.
    Returns False as a no-op if they have no active assignment to THIS
    shift (already unassigned, or actively assigned to a different one),
    rather than closing an unrelated assignment. Sets to_date rather than
    deleting the row, matching create_shift_assignment's own close-out
    convention and delete_shift's comment that assignment rows are kept
    around as history.

    to_date is set to the day BEFORE as_of_date, not as_of_date itself --
    unlike create_shift_assignment's close-out (where to_date == the new
    assignment's from_date is correct, since "active" is to_date >=
    as_of_date and the two rows are meant to hand off on that exact day),
    an explicit unassign has no replacement row to hand off to. Setting
    to_date = as_of_date would leave the employee still counted as
    actively assigned for the rest of as_of_date, so unchecking them in
    the Manage Employees checklist and immediately reopening it the same
    day would show them still checked -- this makes removal effective
    immediately instead."""
    _advisory_lock(db, "shift_assignment", str(employee_id))
    # Their open assignment(s) to THIS shift -- the active one and any
    # scheduled for later; never a row of another shift, never history.
    rows = db.scalars(
        select(models.ShiftAssignment).where(
            models.ShiftAssignment.employee_id == employee_id,
            models.ShiftAssignment.shift_id == shift_id,
            or_(
                models.ShiftAssignment.to_date.is_(None),
                models.ShiftAssignment.to_date >= as_of_date,
            ),
        )
    ).all()
    if not rows:
        return False
    today = company_today(db)
    for row in sorted(rows, key=lambda r: r.from_date):
        if row.from_date >= as_of_date:
            # Would only start on/after the removal date: it never takes
            # effect -- cancelled (kept as history). If it was a scheduled
            # future move, the employee stays on the shift they were moving
            # off; otherwise cancelling it would leave them with no shift.
            row.to_date = row.from_date - datetime.timedelta(days=1)
            db.flush()
            if row.from_date > today:
                _continue_previous_shift(db, row)
        else:
            row.to_date = as_of_date - datetime.timedelta(days=1)
    db.flush()
    return True


def _continue_previous_shift(db: Session, cancelled: models.ShiftAssignment) -> None:
    """After a scheduled assignment is cancelled, its previous shift carries
    on from the date the cancelled one would have started, up to the next
    assignment still scheduled after it (if any). A new history row -- the
    original rows stay as they are."""
    if cancelled.previous_shift_id is None:
        return
    start = cancelled.from_date
    if get_active_shift_assignment(db, cancelled.employee_id, start) is not None:
        return  # something else already covers that date
    next_start = db.scalar(
        select(func.min(models.ShiftAssignment.from_date)).where(
            models.ShiftAssignment.employee_id == cancelled.employee_id,
            models.ShiftAssignment.from_date > start,
            or_(
                models.ShiftAssignment.to_date.is_(None),
                models.ShiftAssignment.to_date >= models.ShiftAssignment.from_date,
            ),
        )
    )
    db.add(models.ShiftAssignment(
        id=uuid.uuid4(),
        employee_id=cancelled.employee_id,
        shift_id=cancelled.previous_shift_id,
        from_date=start,
        to_date=(next_start - datetime.timedelta(days=1)) if next_start else None,
        previous_shift_id=cancelled.shift_id,
    ))
    db.flush()


def upcoming_shift_assignments(
    db: Session, company_id: uuid.UUID, after_date: datetime.date,
    employee_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, tuple[models.ShiftAssignment, models.Shift]]:
    """Each employee's NEXT scheduled assignment that starts after
    after_date (and isn't cancelled) -- the "moves to <shift> from <date>"
    shown in Shift Management and on the employee's Attendance screen."""
    q = (
        select(models.ShiftAssignment, models.Shift)
        .join(models.Shift, models.Shift.id == models.ShiftAssignment.shift_id)
        .where(
            models.Shift.company_id == company_id,
            models.ShiftAssignment.from_date > after_date,
            or_(
                models.ShiftAssignment.to_date.is_(None),
                models.ShiftAssignment.to_date >= models.ShiftAssignment.from_date,
            ),
        )
        .order_by(models.ShiftAssignment.from_date)
    )
    if employee_id is not None:
        q = q.where(models.ShiftAssignment.employee_id == employee_id)
    out: dict = {}
    for a, s in db.execute(q).all():
        out.setdefault(a.employee_id, (a, s))
    return out


def shift_assignment_history(
    db: Session, company_id: uuid.UUID, employee_id: uuid.UUID | None = None,
    shift_id: uuid.UUID | None = None, limit: int = 500,
) -> list[models.ShiftAssignment]:
    """Every assignment row (newest effective date first) for the company,
    optionally for one employee / one shift -- the complete history: what
    was assigned, effective from / to, the previous shift, when and by whom
    it was made and last changed."""
    q = (
        select(models.ShiftAssignment)
        .join(models.Shift, models.Shift.id == models.ShiftAssignment.shift_id)
        .where(models.Shift.company_id == company_id)
    )
    if employee_id is not None:
        q = q.where(models.ShiftAssignment.employee_id == employee_id)
    if shift_id is not None:
        q = q.where(models.ShiftAssignment.shift_id == shift_id)
    q = q.order_by(
        models.ShiftAssignment.from_date.desc(),
        models.ShiftAssignment.created_at.desc().nulls_last(),
    ).limit(limit)
    return list(db.scalars(q).all())


def bulk_end_shift_assignments(
    db: Session,
    shift_id: uuid.UUID,
    employee_ids: list[uuid.UUID],
    as_of_date: datetime.date,
) -> dict:
    """Bulk counterpart of bulk_create_shift_assignments, for unassigning
    -- the "remove selected employees" half of the Manage Employees
    checklist's diff-and-save."""
    unassigned: list[uuid.UUID] = []
    skipped_not_assigned: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for employee_id in employee_ids:
        if employee_id in seen:
            continue
        seen.add(employee_id)
        if end_shift_assignment(db, shift_id, employee_id, as_of_date):
            unassigned.append(employee_id)
        else:
            skipped_not_assigned.append(employee_id)
    return {"unassigned": unassigned, "skipped_not_assigned": skipped_not_assigned}


def list_holidays(db: Session, company_id: uuid.UUID) -> list[models.Holiday]:
    return db.scalars(
        select(models.Holiday)
        .where(models.Holiday.company_id == company_id)
        .order_by(models.Holiday.holiday_date)
    ).all()


def find_duplicate_holiday(
    db: Session,
    company_id: uuid.UUID,
    holiday_date: datetime.date,
    name: str,
    branch_id: uuid.UUID | None,
) -> "models.Holiday | None":
    """The same holiday already on the calendar: same date, same name
    (case / spacing ignored) and the same applicability (branch, or all
    branches). The same holiday for a different branch is not a duplicate."""
    return db.scalar(
        select(models.Holiday).where(
            models.Holiday.company_id == company_id,
            models.Holiday.holiday_date == holiday_date,
            func.lower(func.regexp_replace(func.trim(models.Holiday.name), r"\s+", " ", "g"))
            == " ".join(name.split()).lower(),
            models.Holiday.branch_id.is_(None) if branch_id is None else models.Holiday.branch_id == branch_id,
        ).limit(1)
    )


def create_holiday(
    db: Session,
    company_id: uuid.UUID,
    holiday_date: datetime.date,
    name: str,
    branch_id: uuid.UUID | None = None,
    is_optional: bool = False,
    source_import_id: uuid.UUID | None = None,
) -> models.Holiday:
    name = " ".join(name.split())
    if find_duplicate_holiday(db, company_id, holiday_date, name, branch_id) is not None:
        raise ValueError(f"Holiday '{name}' on {holiday_date} already exists for this branch")
    holiday = models.Holiday(
        id=uuid.uuid4(),
        company_id=company_id,
        holiday_date=holiday_date,
        name=name,
        branch_id=branch_id,
        is_optional=is_optional,
        source_import_id=source_import_id,
    )
    db.add(holiday)
    db.flush()
    return holiday


def list_leave_types(db: Session, company_id: uuid.UUID) -> list[models.LeaveType]:
    return db.scalars(
        select(models.LeaveType).where(models.LeaveType.company_id == company_id)
    ).all()


def create_leave_type(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    code: str,
    is_paid: bool = True,
    max_days_per_year: float | None = None,
    carry_forward: bool = False,
    is_encashable: bool = False,
    applicable_employment_types: list[str] | None = None,
) -> models.LeaveType:
    existing = db.scalar(
        select(models.LeaveType).where(
            models.LeaveType.company_id == company_id, models.LeaveType.code == code
        )
    )
    if existing is not None:
        raise ValueError(f"Leave type code '{code}' already exists")
    if db.scalar(select(models.LeaveType.id).where(
            models.LeaveType.company_id == company_id,
            func.lower(func.trim(models.LeaveType.name)) == " ".join(name.split()).lower()).limit(1)) is not None:
        raise ValueError(f"Leave type '{name}' already exists")
    leave_type = models.LeaveType(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        code=code,
        is_paid=is_paid,
        max_days_per_year=max_days_per_year,
        carry_forward=carry_forward,
        is_encashable=is_encashable,
        **({"applicable_employment_types": applicable_employment_types}
           if applicable_employment_types is not None else {}),
    )
    db.add(leave_type)
    db.flush()
    return leave_type


def get_or_create_leave_type_by_name(
    db: Session, company_id: uuid.UUID, name: str
) -> models.LeaveType:
    existing = db.scalar(
        select(models.LeaveType).where(
            models.LeaveType.company_id == company_id,
            func.lower(models.LeaveType.name) == name.strip().lower(),
        )
    )
    if existing is not None:
        return existing
    # Not one of the seeded types -- seed/import scripts only (leave
    # requests and allocations require an existing type, H-09). The code is
    # made unique so names sharing their first 10 characters never collide.
    from . import leave_policy

    return create_leave_type(db, company_id, name.strip(), leave_policy.unique_code(db, company_id, name))


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


_PRESENT_STATUSES = ("present", "half_day", "regularized", "early_in", "off_shift", "late")


def dashboard_attendance_summary(
    db: Session, company_id: uuid.UUID, employee_ids: list[uuid.UUID] | None = None
) -> dict:
    """Dashboard / Attendance headline stats (DATA-01).

    - "today" is the real calendar date in the company's timezone
      (company_today), not the latest date that happens to have data.
    - present_today counts present + late (+ half_day/regularized/early_in/
      off_shift, all of which mean the employee attended).
    - attendance_pct = present_today / active headcount.
    - daily_present: 7 ints, Mon..Sun of the CURRENT company-timezone week
      (oldest -> newest), each the % (0-100) of active headcount present
      that day; future days are 0. daily_present_dates holds the matching 7
      ISO dates, daily_present_counts the raw counts, summary_date today.
    - MTD figures cover the 1st of today's month through today.
    - employee_ids (RPT-9): the caller's visible employees (None = whole
      company) -- an employee sees their own figures, a manager their team.
      Only active employees count, matching the headcount denominator."""
    today = company_today(db, company_id)
    week_start = today - datetime.timedelta(days=today.weekday())
    week_days = [week_start + datetime.timedelta(days=i) for i in range(7)]
    month_start = today.replace(day=1)
    range_start = min(week_start, month_start)

    scope = [models.Employee.company_id == company_id, models.Employee.is_active.is_(True)]
    if employee_ids is not None:
        scope.append(models.Employee.id.in_(employee_ids))
    headcount = db.scalar(select(func.count(models.Employee.id)).where(*scope)) or 0

    records = db.scalars(
        select(models.AttendanceRecord)
        .join(models.Employee, models.Employee.id == models.AttendanceRecord.employee_id)
        .where(
            *scope,
            models.AttendanceRecord.attendance_date >= range_start,
            models.AttendanceRecord.attendance_date <= today,
        )
    ).all()

    counts: dict[str, int] = {}
    present_by_day: dict[datetime.date, int] = {}
    overtime_hours = 0.0
    late_month = absent_month = 0
    overtime_hours_month = 0.0
    for r in records:
        d = r.attendance_date
        if d == today:
            counts[r.status] = counts.get(r.status, 0) + 1
        if week_start <= d <= today:
            overtime_hours += _num(getattr(r, "overtime_hours", None))
            if r.status in _PRESENT_STATUSES:
                present_by_day[d] = present_by_day.get(d, 0) + 1
        if d >= month_start:
            late_month += r.status == "late"
            absent_month += r.status == "absent"
            overtime_hours_month += _num(getattr(r, "overtime_hours", None))

    present = sum(counts.get(s, 0) for s in _PRESENT_STATUSES)
    daily_counts = [present_by_day.get(d, 0) if d <= today else 0 for d in week_days]
    return {
        "present_today": present,
        "absent_today": counts.get("absent", 0),
        "on_leave_today": counts.get("on_leave", 0),
        "attendance_pct": min(100, round(present / headcount * 100)) if headcount else 0,
        "overtime_hours_week": round(overtime_hours),
        "daily_present": [
            min(100, round(c / headcount * 100)) if headcount else 0 for c in daily_counts
        ],
        "daily_present_dates": [d.isoformat() for d in week_days],
        "daily_present_counts": daily_counts,
        "summary_date": today.isoformat(),
        "active_headcount": headcount,
        "late_month": late_month,
        "absent_month": absent_month,
        "overtime_hours_month": round(overtime_hours_month),
    }


# Shift times are stored in IST (Indian Standard Time, UTC+5:30).
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# DATA-03/C11: core_company_settings.default_timezone is free text such as
# "IST (UTC+5:30)" (or an IANA name like "Asia/Kolkata").
_TZ_OFFSET_RE = re.compile(r"UTC\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", re.IGNORECASE)


def _parse_timezone(value: str | None) -> datetime.tzinfo | None:
    if not value:
        return None
    m = _TZ_OFFSET_RE.search(value)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        delta = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return datetime.timezone(sign * delta)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(value.strip())
    except Exception:
        return None


def company_tzinfo(db: Session | None = None, company_id: uuid.UUID | None = None) -> datetime.tzinfo:
    """The company's configured timezone (core_company_settings.
    default_timezone), falling back to IST -- the single source of the
    attendance/leave "today". Cached per session/company in db.info."""
    if db is None or company_id is None:
        return _IST
    cache = db.info.setdefault("_company_tz", {})
    if company_id not in cache:
        tz = None
        try:
            # SAVEPOINT: a failing read (e.g. a tenant whose settings table
            # lags _template) must not abort the caller's transaction.
            with db.begin_nested():
                company_settings = db.get(models.CompanySettings, company_id)
                tz = _parse_timezone(company_settings.default_timezone if company_settings else None)
        except Exception:  # pragma: no cover - defensive, never fail a request on this
            logger.warning("company_tzinfo: could not read settings for %s", company_id)
        cache[company_id] = tz or _IST
    return cache[company_id]


def company_now(db: Session | None = None, company_id: uuid.UUID | None = None) -> datetime.datetime:
    return datetime.datetime.now(company_tzinfo(db, company_id))


def employee_company_today(db: Session, employee_id: uuid.UUID | None) -> datetime.date:
    """company_today for the company this employee belongs to (C11)."""
    company_id = db.scalar(select(models.Employee.company_id).where(models.Employee.id == employee_id)) if employee_id else None
    return company_today(db, company_id)


def company_today(db: Session | None = None, company_id: uuid.UUID | None = None) -> datetime.date:
    """Calendar date in the company's timezone -- use instead of the
    server-local date.today() for attendance/leave "today" (a UTC server
    otherwise files 00:00-05:30 IST check-ins under the previous day)."""
    return company_now(db, company_id).date()


def _get_active_shift(
    db: Session, employee_id: uuid.UUID, as_of_date: datetime.date | None = None
) -> "models.Shift | None":
    """Return the employee's Shift row active as of as_of_date (defaults to
    today), or None if unassigned. Delegates to get_active_shift_assignment
    for the canonical "active" definition (_shift_assignment_active_on:
    started by as_of_date and not yet ended) -- previously used a narrower to_date-IS-NULL-only
    check, which incorrectly reported "no shift" for an employee whose
    current assignment carries a future to_date (e.g. a shift change
    scheduled ahead via a future from_date on the replacement assignment,
    see create_shift_assignment), wrongly skipping late/early detection for
    them until that future date arrived."""
    as_of = as_of_date or company_today()
    prefetched = _rbac_memo(db).get("active_shifts")
    if prefetched is not None and (employee_id, as_of) in prefetched:
        return prefetched[(employee_id, as_of)]
    assignment = get_active_shift_assignment(db, employee_id, as_of)
    if assignment is None:
        return None
    return db.get(models.Shift, assignment.shift_id)


def prefetch_active_shifts(
    db: Session, keys: "Iterable[tuple[uuid.UUID, datetime.date]]"
) -> None:
    """PERF-09 / P10: resolves _get_active_shift for many (employee_id,
    as_of_date) keys with 2 queries total (all candidate assignments, then
    their shifts) and parks the answers in the per-request memo, so later
    _get_active_shift calls for those keys (e.g. the Leave list's half-day
    time range, 2 queries per row before) cost nothing. Same rule as
    get_active_shift_assignment: to_date IS NULL or >= as_of_date, latest
    from_date wins. Dropped with the rest of the memo when the transaction
    ends or a ShiftAssignment/Shift is flushed in this session."""
    wanted = {k for k in keys if k[0] is not None and k[1] is not None}
    if not wanted:
        return
    assignments = db.scalars(
        select(models.ShiftAssignment)
        .where(models.ShiftAssignment.employee_id.in_({k[0] for k in wanted}))
        .order_by(models.ShiftAssignment.from_date.desc())
    ).all()
    by_emp: dict = {}
    for a in assignments:
        by_emp.setdefault(a.employee_id, []).append(a)
    chosen: dict = {}
    for emp_id, as_of in wanted:
        chosen[(emp_id, as_of)] = next(
            (a.shift_id for a in by_emp.get(emp_id, [])
             if a.from_date <= as_of and (a.to_date is None or a.to_date >= as_of)),
            None,
        )
    shift_ids = {sid for sid in chosen.values() if sid is not None}
    shifts = (
        {sh.id: sh for sh in db.scalars(select(models.Shift).where(models.Shift.id.in_(shift_ids))).all()}
        if shift_ids else {}
    )
    memo = _rbac_memo(db)
    cache = memo.setdefault("active_shifts", {})
    for key, sid in chosen.items():
        cache[key] = shifts.get(sid) if sid is not None else None


def preload_employees(db: Session, employee_ids: "Iterable[uuid.UUID | None]") -> None:
    """PERF-09: loads many Employee rows in ONE query and pins them for the
    rest of the request (per-request memo), so the per-row
    db.get(models.Employee, id) calls in list serializers are served from
    the identity map instead of one round trip each."""
    ids = {i for i in employee_ids if i is not None}
    if not ids:
        return
    rows = db.scalars(select(models.Employee).where(models.Employee.id.in_(ids))).all()
    _rbac_memo(db).setdefault("pinned_employees", []).extend(rows)


def _time_from_minutes(minutes: int) -> datetime.time:
    return datetime.time(hour=(minutes // 60) % 24, minute=minutes % 60)


def get_blocking_leave_window(
    db: Session,
    employee_id: uuid.UUID,
    on_date: datetime.date,
    shift: "models.Shift | None" = None,
) -> dict | None:
    """None if nothing about this employee's leave should block Check-In/
    Check-Out on `on_date`. Otherwise a dict describing exactly what to
    refuse:
      - {"full_day": True, "start": None, "end": None, "reason": ...} --
        block Check-In AND Check-Out outright, and prevent an attendance
        record from being created at all for this date.
      - {"full_day": False, "start": <time>, "end": <time>, "reason": ...}
        -- block whichever of Check-In/Check-Out would happen while the
        current time falls inside [start, end], derived from the SAME
        shift-midpoint split routers.leave._half_day_time_range already
        uses to describe a half-day request's morning/afternoon range, so
        both displays and this gate always agree. A Morning half-day thus
        blocks a Check-In attempt in [shift start, midpoint] while still
        allowing one afterward for the afternoon working portion; an
        Afternoon half-day blocks whichever action (Check-In or Check-Out)
        would occur in [midpoint, shift end].

    Only a FINAL "approved" leave request blocks anything -- 'pending' and
    the interim 'l1_approved' (still awaiting a second-level decision, see
    routers/leave.py's two-level approval flow) are deliberately NOT
    treated as blocking: nothing else in this system currently treats a
    not-yet-fully-approved leave request as binding (leave balances,
    dashboard/report aggregates etc. only ever count "approved"), so
    Attendance doesn't invent a stricter rule here either. 'rejected' and
    'sent_back' obviously never block anything.
    """
    leave = db.scalars(
        select(models.LeaveRequest).where(
            models.LeaveRequest.employee_id == employee_id,
            models.LeaveRequest.status == "approved",
            models.LeaveRequest.from_date <= on_date,
            models.LeaveRequest.to_date >= on_date,
        )
    ).first()
    if leave is None:
        return None
    reason = "Leave applied for this date – Attendance unavailable."
    if not leave.is_half_day:
        return {"full_day": True, "start": None, "end": None, "reason": reason}
    if leave.half_day_period is None:
        # Data inconsistency (half-day with no period) -- fail open rather
        # than guess which half to block, same guard _half_day_time_range
        # itself uses for its own display value.
        return None
    if shift is None:
        shift = _get_active_shift(db, employee_id, on_date)
    if shift is None or shift.is_night:
        return None
    start_minutes = shift.start_time.hour * 60 + shift.start_time.minute
    end_minutes = shift.end_time.hour * 60 + shift.end_time.minute
    if end_minutes <= start_minutes:
        return None
    midpoint = (start_minutes + end_minutes) // 2
    if leave.half_day_period == "morning":
        window_start, window_end = shift.start_time, _time_from_minutes(midpoint)
    else:
        window_start, window_end = _time_from_minutes(midpoint), shift.end_time
    return {"full_day": False, "start": window_start, "end": window_end, "reason": reason}


def clock_in_out(
    db: Session, company_id: uuid.UUID, employee_id: uuid.UUID, today: datetime.date, now: datetime.datetime,
    action: str, work_mode: str | None = None,
) -> models.AttendanceRecord:
    """Applies exactly the ONE action the caller explicitly asked for --
    'check_in' or 'check_out' -- rather than blindly toggling off whatever
    state the record happens to be in. This is deliberate, not incidental:
    a blind toggle means a duplicate/late-arriving request (e.g. a rapid
    double-click on "Clock In" firing two POSTs before the UI disables the
    button) would see the record its OWN first call just created and
    silently reinterpret the second call as a Check-Out -- immediately
    ending a session the employee never asked to end. With an explicit
    `action`, a duplicate Check-In while already checked in (whether or not
    also already checked out) is always an idempotent no-op that returns
    the existing record unchanged, and likewise for a duplicate Check-Out
    once already checked out; a Check-Out attempted with no prior Check-In
    is rejected outright. See routers/attendance.py's clock_in_out endpoint
    for the request schema this comes from.

    hcm.attendance_records has a real UNIQUE(employee_id, attendance_date)
    constraint, so an advisory lock scoped to that pair serializes concurrent
    clock actions for the same employee/day instead of racing two inserts
    against that constraint -- this is also what makes the no-op paths above
    safe: two concurrent requests for the same employee/day are always
    processed one at a time, never interleaved.

    Shift-aware: an employee's individually ASSIGNED shift (hcm.shifts, via
    hcm.shift_assignments -- see _get_active_shift) is always used ahead of
    the company-wide core_company_settings.working_hours_start/end policy
    (currently only configured for impacgo-solutions) when both exist --
    the shift is the more specific rule and wins. The company-wide policy
    remains the fallback for any employee with no active shift assignment,
    so every tenant/employee that never had a shift assigned keeps exactly
    the previous behavior.

    Whichever one applies (effective_start/effective_end below) drives, in
    one unified scheme:
      - Check-In refused more than 30 minutes before effective_start
        (mirrors the "Enable Check-In 30 min early" UI gate server-side).
      - Checking in before effective_start -> 'early_in' ("Early Check-In").
      - Checking in after effective_start + grace_minutes (shift only --
        the company-wide policy has no grace concept) -> 'late'
        ("Late Check-In").
      - Otherwise -> 'present' ("On Time").
      - Check-Out refused more than 30 minutes before effective_end.
      - Checking out at/after effective_end -> 'off_shift' ("Off Shift"),
        overriding whatever check-in status was set. The actual check_out
        timestamp is always preserved regardless of status; nothing here
        ever auto-checks-out an employee.

    Night shifts (shift.is_night) are deliberately exempted from ALL of the
    above (no window guard, status stays whatever it would otherwise
    default to) rather than risk it: comparing bare time-of-day against a
    start/end range that crosses midnight produces false positives/
    negatives (e.g. a 22:00-06:00 shift, checked out at 23:30 the same
    night, is NOT "before 06:00" once the date is dropped) -- this matches
    day-to-day reality for a night-shift employee unchanged from before
    office-hours/shift-aware statuses existed at all.

    If no shift is assigned and no company-wide policy is configured
    either, effective_start/effective_end are both None and this degrades
    to the original pre-office-hours behavior: status stays 'present',
    no window is enforced.
    """
    _advisory_lock(db, "attendance", str(employee_id), today.isoformat())

    record = db.scalar(
        select(models.AttendanceRecord)
        .where(
            models.AttendanceRecord.employee_id == employee_id,
            models.AttendanceRecord.attendance_date == today,
        )
        .with_for_update()
    )

    shift = _get_active_shift(db, employee_id, today)
    is_night = shift is not None and shift.is_night
    if shift is not None and not is_night:
        effective_start: datetime.time | None = shift.start_time
        effective_end: datetime.time | None = shift.end_time
        grace_minutes = shift.grace_minutes
    elif shift is None:
        settings_row = get_company_settings(db, company_id)
        effective_start = settings_row.working_hours_start if settings_row else None
        effective_end = settings_row.working_hours_end if settings_row else None
        grace_minutes = None
    else:
        # Night shift: opt out of the whole scheme, see docstring.
        effective_start = None
        effective_end = None
        grace_minutes = None
    # Convert the UTC timestamp to IST for comparison against stored shift/
    # company-policy times.
    now_ist = now.astimezone(company_tzinfo(db, company_id)) if now.tzinfo else now
    now_time = now_ist.time()

    if action not in ("check_in", "check_out"):
        raise ValueError(f"Unknown attendance action: {action!r}")

    # H-17: a check-out after midnight with nothing open today closes the
    # previous day's still-open session (night shift, or any session that
    # started at most attendance_rules.MAX_OPEN_SESSION_HOURS ago), judged
    # against that day's shift -- previously it was refused and the record
    # stayed open forever.
    if action == "check_out" and (record is None or record.check_in is None):
        from . import attendance_rules

        prev = attendance_rules.open_previous_record(db, employee_id, today, now)
        if prev is not None:
            if _open_break(db, prev.id) is not None:
                raise ValueError("End your break before checking out.")
            prev_shift = _get_active_shift(db, employee_id, prev.attendance_date)
            prev.check_out = now
            prev.work_hours = round((now - prev.check_in).total_seconds() / 3600, 2)
            if prev_shift is not None and not prev_shift.is_night and prev.status != "late":
                end_at = datetime.datetime.combine(prev.attendance_date, prev_shift.end_time)
                if now_ist.replace(tzinfo=None) >= end_at:
                    prev.status = "off_shift"
            if work_mode is not None:
                prev.work_mode = work_mode
            db.flush()
            return prev

    # Duplicate Check-In: already have a record for today (whether or not
    # also already checked out) -- idempotent no-op, NEVER falls through to
    # the check-out logic below. A still-supplied work_mode is applied as a
    # correction (e.g. the employee forgot to set it on the real first
    # Check-In), same convention as the duplicate-Check-Out no-op further
    # down.
    # A record holding only overtime (made by an Overtime Clock Out, never a
    # regular punch) is not a check-in: the regular check-in still happens
    # on it, and a check-out still needs one first.
    # M-14: an auto-marked 'absent' placeholder is likewise not a check-in.
    overtime_only = record is not None and record.check_in is None and record.check_out is None and (
        record.status == "overtime" or (record.status == "absent" and record.source == "auto_absent"))
    if action == "check_in" and record is not None and not overtime_only:
        if work_mode is not None:
            record.work_mode = work_mode
        db.flush()
        return record

    # Check-Out with no Check-In on record at all -- nothing to complete.
    if action == "check_out" and (record is None or overtime_only):
        raise ValueError("You need to Check-In before you can Check-Out.")

    # Duplicate Check-Out: already checked out today -- idempotent no-op,
    # same reasoning as the duplicate-Check-In case above.
    if action == "check_out" and record.check_out is not None:
        if work_mode is not None:
            record.work_mode = work_mode
        db.flush()
        return record

    # From here on exactly one of the two real transitions applies: a fresh
    # Check-In (action == "check_in" and record is None) or completing an
    # open Check-Out (action == "check_out" and record.check_out is None).
    leave_block = get_blocking_leave_window(db, employee_id, today, shift=shift)
    if leave_block is not None:
        if leave_block["full_day"]:
            raise ValueError(leave_block["reason"])
        if leave_block["start"] <= now_time <= leave_block["end"]:
            raise ValueError(leave_block["reason"])

    if action == "check_in":
        if effective_start is not None:
            # Compared as full date-times: for a shift starting just after
            # midnight the window opens the evening before (comparing bare
            # times wrapped 00:00 - 30 min to 23:30 and refused 00:05).
            checkin_open_at = (
                datetime.datetime.combine(today, effective_start) - datetime.timedelta(minutes=30)
            )
            if now_ist.replace(tzinfo=None) < checkin_open_at:
                raise ValueError(
                    f"Check-in opens at {checkin_open_at.strftime('%I:%M %p').lstrip('0')}"
                )
        status = "present"
        if effective_start is not None:
            if now_time < effective_start:
                status = "early_in"
            elif grace_minutes is not None:
                grace_cutoff = (
                    datetime.datetime.combine(today, effective_start)
                    + datetime.timedelta(minutes=grace_minutes)
                ).time()
                if now_time > grace_cutoff:
                    status = "late"
        if overtime_only:
            # Keep the day's overtime hours; this is the regular check-in.
            record.check_in, record.status, record.work_mode = now, status, work_mode
            record.arrival_status = status
            if record.source == "auto_absent":
                record.source = "web"
        else:
            record = models.AttendanceRecord(
                id=uuid.uuid4(),
                company_id=company_id,
                employee_id=employee_id,
                attendance_date=today,
                check_in=now,
                status=status,
                arrival_status=status,
                work_mode=work_mode,
            )
            db.add(record)
    else:
        # Break Management: an open (not yet ended) break must be closed
        # first -- otherwise its break_end would have to be silently
        # fabricated as the check-out time, and the employee never
        # explicitly confirmed they'd actually stopped their break then.
        if _open_break(db, record.id) is not None:
            raise ValueError("End your break before checking out.")
        if effective_end is not None:
            checkout_open_at = (
                datetime.datetime.combine(today, effective_end) - datetime.timedelta(minutes=30)
            )
            if now_ist.replace(tzinfo=None) < checkout_open_at:
                raise ValueError(
                    f"Check-out opens at {checkout_open_at.strftime('%I:%M %p').lstrip('0')}"
                )
        record.check_out = now
        elapsed_hours = (now - record.check_in).total_seconds() / 3600 if record.check_in else None
        record.work_hours = round(elapsed_hours, 2) if elapsed_hours is not None else None
        # M-13: a late arrival stays 'late' (every late tally reads status);
        # arrival_status keeps the check-in verdict in any case.
        if effective_end is not None and now_time >= effective_end and record.status != "late":
            record.status = "off_shift"
        if work_mode is not None:
            record.work_mode = work_mode

    db.flush()
    return record


# ── Employee Break Management ───────────────────────────────────────────
# Break rules always come from the employee's own ASSIGNED SHIFT
# (Shift.break_minutes = total minutes allowed across every break in one
# session, Shift.max_breaks = how many separate breaks that may be split
# into) -- never a company-wide default. An employee with no active shift
# has no break policy to fall back to, so breaks are simply unavailable to
# them (get_break_policy returns 0/0), exactly like clock_in_out's own
# "no shift, no company policy either -> no window enforced" case, except
# here the honest answer is "nothing to enforce against" rather than
# "unrestricted".

def get_break_policy(shift: "models.Shift | None") -> dict:
    if shift is None:
        return {"allowed_minutes": 0.0, "max_breaks": 0}
    return {"allowed_minutes": float(shift.break_minutes), "max_breaks": int(shift.max_breaks)}


def _open_break(db: Session, attendance_record_id: uuid.UUID) -> models.BreakRecord | None:
    return db.scalar(
        select(models.BreakRecord)
        .where(
            models.BreakRecord.attendance_record_id == attendance_record_id,
            models.BreakRecord.break_end.is_(None),
        )
        .with_for_update()
    )


def _breaks_for_record(db: Session, attendance_record_id: uuid.UUID) -> list[models.BreakRecord]:
    return list(
        db.scalars(
            select(models.BreakRecord)
            .where(models.BreakRecord.attendance_record_id == attendance_record_id)
            .order_by(models.BreakRecord.break_start)
        ).all()
    )


def break_duration_minutes(
    b: "models.BreakRecord | None", now: datetime.datetime | None = None
) -> float:
    if b is None:
        return 0.0
    end = b.break_end or (now or datetime.datetime.now(datetime.timezone.utc))
    return round((end - b.break_start).total_seconds() / 60, 2)


def total_break_minutes(
    breaks: list[models.BreakRecord], now: datetime.datetime | None = None
) -> float:
    return round(sum(break_duration_minutes(b, now) for b in breaks), 2)


def serialize_break(b: models.BreakRecord, now: datetime.datetime | None = None) -> dict:
    return {
        "id": b.id,
        "break_start": b.break_start,
        "break_end": b.break_end,
        "duration_minutes": break_duration_minutes(b, now),
        "in_progress": b.break_end is None,
    }


def start_break(
    db: Session, employee_id: uuid.UUID, today: datetime.date, now: datetime.datetime
) -> models.BreakRecord:
    """Starts a new break against today's attendance record. Raises
    ValueError (turned into a 400 by routers/attendance.py) for every
    invalid case: no Check-In yet, already Checked Out, a break already in
    progress, the shift allows zero breaks or the max has already been
    taken, the full break-time allowance is already used up, taking a
    break outside shift hours, or no shift assigned at all.

    Advisory-locked per employee/day (same pattern as clock_in_out), and
    the attendance-record fetch + the open-break check below both run
    FOR UPDATE, so two concurrent "Take Break" clicks -- or two different
    devices -- can never both succeed and create overlapping breaks.
    """
    _advisory_lock(db, "break", str(employee_id), today.isoformat())
    record = db.scalar(
        select(models.AttendanceRecord)
        .where(
            models.AttendanceRecord.employee_id == employee_id,
            models.AttendanceRecord.attendance_date == today,
        )
        .with_for_update()
    )
    if record is None or record.check_in is None:
        raise ValueError("You need to Check-In before you can take a break.")
    if record.check_out is not None:
        raise ValueError("You've already Checked Out for today — breaks are no longer available.")
    if _open_break(db, record.id) is not None:
        raise ValueError("You already have a break in progress.")

    shift = _get_active_shift(db, employee_id, today)
    policy = get_break_policy(shift)
    if policy["max_breaks"] <= 0 or policy["allowed_minutes"] <= 0:
        raise ValueError("Your assigned shift does not allow any breaks.")

    existing = _breaks_for_record(db, record.id)
    if len(existing) >= policy["max_breaks"]:
        raise ValueError(
            f"You've already taken the maximum of {policy['max_breaks']} "
            f"break{'s' if policy['max_breaks'] != 1 else ''} allowed for your shift."
        )
    if total_break_minutes(existing, now) >= policy["allowed_minutes"]:
        raise ValueError("You've used your full break time allowance for today.")

    # Break window: only enforced for a real (non-night) shift -- comparing
    # bare time-of-day against a start/end range that crosses midnight
    # produces false positives for a night shift, same exemption
    # clock_in_out itself documents and uses.
    if not shift.is_night:
        now_ist = now.astimezone(company_tzinfo(db, record.company_id)) if now.tzinfo else now
        now_time = now_ist.time()
        if now_time < shift.start_time or now_time > shift.end_time:
            raise ValueError("Breaks can only be taken during your shift hours.")

    brk = models.BreakRecord(
        id=uuid.uuid4(),
        company_id=record.company_id,
        employee_id=employee_id,
        attendance_record_id=record.id,
        break_start=now,
    )
    db.add(brk)
    db.flush()
    return brk


def end_break(
    db: Session, employee_id: uuid.UUID, today: datetime.date, now: datetime.datetime
) -> models.BreakRecord:
    _advisory_lock(db, "break", str(employee_id), today.isoformat())
    record = db.scalar(
        select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == employee_id,
            models.AttendanceRecord.attendance_date == today,
        )
    )
    if record is None:
        raise ValueError("No attendance record found for today.")
    brk = _open_break(db, record.id)
    if brk is None:
        raise ValueError("You don't have a break in progress.")
    brk.break_end = now
    db.flush()
    return brk


def get_break_status(db: Session, employee_id: uuid.UUID, today: datetime.date) -> dict:
    """GET /api/attendance/breaks/mine's payload -- the sole source of
    truth the frontend timer hydrates from on load/refresh/relogin/another
    device, never local-only state."""
    now = datetime.datetime.now(datetime.timezone.utc)
    record = db.scalar(
        select(models.AttendanceRecord).where(
            models.AttendanceRecord.employee_id == employee_id,
            models.AttendanceRecord.attendance_date == today,
        )
    )
    shift = _get_active_shift(db, employee_id, today)
    policy = get_break_policy(shift)
    if record is None:
        return {
            "has_attendance_record": False, "checked_out": False, "in_progress": False,
            "current_break_id": None, "break_start": None, "elapsed_minutes": 0.0,
            "breaks_taken": 0, "max_breaks": policy["max_breaks"],
            "total_break_minutes_used": 0.0, "allowed_break_minutes": policy["allowed_minutes"],
            "remaining_break_minutes": policy["allowed_minutes"], "breaks": [],
        }
    breaks = _breaks_for_record(db, record.id)
    open_break = next((b for b in breaks if b.break_end is None), None)
    used = total_break_minutes(breaks, now)
    return {
        "has_attendance_record": True,
        "checked_out": record.check_out is not None,
        "in_progress": open_break is not None,
        "current_break_id": open_break.id if open_break else None,
        "break_start": open_break.break_start if open_break else None,
        "elapsed_minutes": break_duration_minutes(open_break, now),
        "breaks_taken": len(breaks),
        "max_breaks": policy["max_breaks"],
        "total_break_minutes_used": used,
        "allowed_break_minutes": policy["allowed_minutes"],
        "remaining_break_minutes": round(max(policy["allowed_minutes"] - used, 0.0), 2),
        "breaks": [serialize_break(b, now) for b in breaks],
    }


def _effective_attendance_status(record: models.AttendanceRecord) -> str:
    """Read-time-only status override for display -- NEVER written back to
    the DB row (the real fix-up happens only via an approved regularization,
    see apply_regularization_to_attendance_record), so this can't interfere
    with that flow or with the present/absent/late aggregate counts
    elsewhere in this file, which all still tally the raw stored `status`
    column unchanged.

    'regularized' always wins outright -- an approved correction already
    resolved whatever gap existed, so it must never be re-flagged as still
    missing something.

    Otherwise surfaces two states the stored `status` column (set once, at
    check-in/out time) can't express on its own:
      - check_out present but check_in missing -> 'missing_checkin' (e.g. a
        regularization approved only Requested Out for a day the employee
        never punched in at all).
      - check_in present, check_out missing, and the day has already ended
        -> 'missing_checkout'. Still reported as whatever `status` was set
        to at check-in (present/late/early_in) while the day is still in
        progress -- see the attendance_date < today guard -- so an employee
        currently checked in doesn't get flagged as having "missing" their
        checkout mid-session.
    """
    if record.status == "regularized":
        return "regularized"
    if record.check_in is None and record.check_out is not None:
        return "missing_checkin"
    if (
        record.check_in is not None
        and record.check_out is None
        and record.attendance_date < company_today()
    ):
        return "missing_checkout"
    return record.status


def serialize_attendance_record(record: models.AttendanceRecord) -> dict:
    """AttendanceRecordOut payload, denormalized with the employee's name/
    code/branch straight off `record.employee` -- lets the Daily
    Attendance tab render without a separate GET /api/employees/full roster
    call, which is gated by this company's own People-access policy (see
    can_access_people_module) and can legitimately be empty/stale for a
    caller who can still see this exact attendance row.

    work_mode comes straight off the attendance record itself (the
    employee's own manual WFO/WFH/Client Site pick for THIS day -- see
    AttendanceRecord.work_mode), not off the employee's static profile
    field -- those are deliberately different things.

    total_break_minutes/net_work_hours are derived here from the record's
    real BreakRecord rows (record.breaks), never stored -- see the Break
    Management section above for why."""
    e = record.employee
    total_break = total_break_minutes(list(record.breaks))
    net_hours = (
        round(float(record.work_hours) - total_break / 60, 2)
        if record.work_hours is not None
        else None
    )
    return {
        "id": record.id,
        "employee_id": record.employee_id,
        "attendance_date": record.attendance_date,
        "check_in": record.check_in,
        "check_out": record.check_out,
        "work_hours": record.work_hours,
        "overtime_hours": record.overtime_hours,
        "status": _effective_attendance_status(record),
        # The raw stored status the dashboard / report aggregates tally
        # (dashboard_attendance_summary) -- lets a figure's drill-down
        # match exactly the rows it counted, while `status` stays the
        # display override.
        "stored_status": record.status,
        "arrival_status": record.arrival_status,
        "employee_name": _full_name(e),
        "employee_code": e.employee_code if e else None,
        "work_mode": record.work_mode,
        "branch_name": e.branch.name if e and e.branch else None,
        "total_break_minutes": total_break,
        "net_work_hours": net_hours,
    }


def get_attendance_record_for_update(
    db: Session, record_id: uuid.UUID, company_id: uuid.UUID
) -> models.AttendanceRecord | None:
    """Scoped by the caller's own company_id so this can't reach another
    company's attendance record even inside a shared tenant schema --
    same convention as get_shift_for_update/get_regularization_for_update."""
    return db.scalar(
        select(models.AttendanceRecord).where(
            models.AttendanceRecord.id == record_id,
            models.AttendanceRecord.company_id == company_id,
        )
    )


class InvalidCursor(ValueError):
    """A pagination cursor that can't be decoded -- routers answer 400."""


def encode_cursor(key: str, row_id: uuid.UUID) -> str:
    """API-04/C18: opaque keyset cursor -- urlsafe base64 of the JSON pair
    [sort_key, id]. Unlike the old f"{key}:{id}" string it survives sort
    keys containing ':' (e.g. a first name) and is safe in a query string."""
    raw = json.dumps([key, str(row_id)], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, uuid.UUID]:
    """Inverse of encode_cursor; also accepts the legacy "<key>:<uuid>"
    format (split on the LAST ':' -- a uuid never contains one) so a cursor
    handed out before this change still works. Raises InvalidCursor."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        key, id_str = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return str(key), uuid.UUID(str(id_str))
    except Exception:
        pass
    try:
        key, id_str = cursor.rsplit(":", 1)
        return key, uuid.UUID(id_str)
    except Exception as exc:
        raise InvalidCursor("Invalid pagination cursor") from exc


def list_attendance_records(
    db: Session,
    company_id: uuid.UUID,
    limit: int = 50,
    cursor: str | None = None,
    employee_ids: list[uuid.UUID] | None = None,
    from_date: datetime.date | None = None,
    to_date: datetime.date | None = None,
    active_only: bool = False,
) -> dict:
    """Cursor-keyed pagination on (attendance_date DESC, id DESC).
    Cursor: opaque (encode_cursor of the date and id of the last row
    returned in the previous page); invalid -> InvalidCursor. employee_ids, when given (see
    get_visible_employee_ids_for_docs), restricts the page to just those
    employees -- callers pass None for company-wide (admin-tier) visibility.
    from_date/to_date (inclusive) and active_only narrow the rows to the
    exact set dashboard_attendance_summary counts, so a dashboard figure's
    drill-down lists the same records."""
    q = select(models.AttendanceRecord).where(
        models.AttendanceRecord.company_id == company_id
    )
    if employee_ids is not None:
        q = q.where(models.AttendanceRecord.employee_id.in_(employee_ids))
    if from_date is not None:
        q = q.where(models.AttendanceRecord.attendance_date >= from_date)
    if to_date is not None:
        q = q.where(models.AttendanceRecord.attendance_date <= to_date)
    if active_only:
        q = q.join(
            models.Employee, models.Employee.id == models.AttendanceRecord.employee_id
        ).where(models.Employee.is_active.is_(True))
    if cursor:
        date_str, cur_id = decode_cursor(cursor)
        try:
            cur_date = datetime.date.fromisoformat(date_str)
        except ValueError as exc:
            raise InvalidCursor("Invalid pagination cursor") from exc
        q = q.where(
            (models.AttendanceRecord.attendance_date < cur_date)
            | (
                (models.AttendanceRecord.attendance_date == cur_date)
                & (models.AttendanceRecord.id < cur_id)
            )
        )
    q = q.options(
        selectinload(models.AttendanceRecord.employee).selectinload(models.Employee.branch),
        selectinload(models.AttendanceRecord.breaks),
    ).order_by(
        models.AttendanceRecord.attendance_date.desc(),
        models.AttendanceRecord.id.desc(),
    ).limit(limit + 1)
    rows = list(db.scalars(q).all())
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = (
        encode_cursor(page_rows[-1].attendance_date.isoformat(), page_rows[-1].id)
        if has_more and page_rows
        else None
    )
    items = [serialize_attendance_record(r) for r in page_rows]
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def create_regularization(
    db: Session,
    employee_id: uuid.UUID,
    attendance_date: datetime.date,
    reason: str,
    requested_in: datetime.datetime | None,
    requested_out: datetime.datetime | None,
) -> models.AttendanceRegularization:
    regularization = models.AttendanceRegularization(
        id=uuid.uuid4(),
        employee_id=employee_id,
        attendance_date=attendance_date,
        reason=reason,
        requested_in=requested_in,
        requested_out=requested_out,
        status="pending",
    )
    db.add(regularization)
    db.flush()
    return regularization


def list_regularizations(
    db: Session,
    company_id: uuid.UUID,
    status: str | None = None,
    employee_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.AttendanceRegularization]:
    q = (
        select(models.AttendanceRegularization)
        .join(models.Employee, models.Employee.id == models.AttendanceRegularization.employee_id)
        .where(models.Employee.company_id == company_id)
    )
    if status is not None:
        q = q.where(models.AttendanceRegularization.status == status)
    if employee_ids is not None:
        q = q.where(models.AttendanceRegularization.employee_id.in_(employee_ids))
    q = q.order_by(models.AttendanceRegularization.attendance_date.desc(), models.AttendanceRegularization.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_regularization_for_update(
    db: Session, regularization_id: uuid.UUID
) -> models.AttendanceRegularization | None:
    """SELECT ... FOR UPDATE -- serializes two managers actioning the same
    regularization request at once (classic double-approval race)."""
    return db.scalar(
        select(models.AttendanceRegularization)
        .where(models.AttendanceRegularization.id == regularization_id)
        .with_for_update()
    )


def apply_regularization_to_attendance_record(
    db: Session,
    company_id: uuid.UUID,
    regularization: models.AttendanceRegularization,
) -> None:
    """Fires exactly once, when update_regularization (attendance.py) moves a
    request into the terminal 'approved' state -- writes the manager-approved
    requested_in/requested_out onto the employee's actual AttendanceRecord for
    that date (creating the record if the employee never punched in/out at
    all that day), recomputes work_hours from the final check_in/check_out,
    and marks the record 'regularized' so the Daily Attendance view reflects
    the correction instead of the original (or missing) punch. Before this,
    approving a regularization only flipped the request's own status -- the
    underlying attendance data was silently left untouched."""
    record = db.scalar(
        select(models.AttendanceRecord)
        .where(
            models.AttendanceRecord.employee_id == regularization.employee_id,
            models.AttendanceRecord.attendance_date == regularization.attendance_date,
        )
        .with_for_update()
    )
    if record is None:
        record = models.AttendanceRecord(
            id=uuid.uuid4(),
            company_id=company_id,
            employee_id=regularization.employee_id,
            attendance_date=regularization.attendance_date,
            source="correction",
        )
        db.add(record)
    if regularization.requested_in is not None:
        record.check_in = regularization.requested_in
    if regularization.requested_out is not None:
        record.check_out = regularization.requested_out
    if record.check_in is not None and record.check_out is not None:
        elapsed_hours = (record.check_out - record.check_in).total_seconds() / 3600
        record.work_hours = round(elapsed_hours, 2)
    record.status = "regularized"
    db.flush()


def apply_overtime_to_attendance_record(
    db: Session,
    company_id: uuid.UUID,
    request: models.OvertimeRequest,
) -> None:
    """Fires exactly once, when update_overtime_request (routers/work.py)
    moves a request into the terminal 'approved' state -- adds the
    approved hours onto the employee's AttendanceRecord.overtime_hours for
    that date (creating the record if none exists, e.g. overtime worked on
    an otherwise-unpunched day), so the Attendance screen's "Overtime
    Hours (Week)/(MTD)" stat cards -- and every other crud.py aggregate
    that sums AttendanceRecord.overtime_hours -- actually reflect approved
    requests instead of always reading 0. Before this, approving an
    overtime request only flipped the request's own status.

    Unlike apply_regularization_to_attendance_record, this never touches
    check_in/check_out/work_hours/status -- an overtime request doesn't
    correct the day's punches, it just adds extra worked hours on top.
    Hours ACCUMULATE rather than overwrite, so two separately approved
    overtime requests for the same date both count."""
    record = db.scalar(
        select(models.AttendanceRecord)
        .where(
            models.AttendanceRecord.employee_id == request.employee_id,
            models.AttendanceRecord.attendance_date == request.work_date,
        )
        .with_for_update()
    )
    if record is None:
        record = models.AttendanceRecord(
            id=uuid.uuid4(),
            company_id=company_id,
            employee_id=request.employee_id,
            attendance_date=request.work_date,
            status="present",
            source="overtime",
        )
        db.add(record)
    record.overtime_hours = float(record.overtime_hours or 0) + float(request.hours)
    db.flush()


# ---------------------------------------------------------------------------
# Leave
# ---------------------------------------------------------------------------


_EMPLOYMENT_TYPE_CODES = {
    "full_time": "full_time", "fulltime": "full_time", "permanent": "full_time",
    "part_time": "part_time", "parttime": "part_time",
    "intern": "intern", "internship": "intern", "trainee": "intern",
    "contract": "contract", "contractor": "contract", "fixed_term": "contract",
}


def employment_type_code(value: str | None) -> str | None:
    """'Full Time' / 'Full-time' / 'full_time' -> 'full_time' (employment
    type is stored as typed); None for a spelling this app doesn't know."""
    key = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _EMPLOYMENT_TYPE_CODES.get(key)


def leave_type_applies(employee, leave_type: models.LeaveType) -> bool:
    """Leave Types > Applicable employment types. An employee whose type
    isn't recognised is never excluded, so only the listed types are ever
    gated -- by default that means contract employees."""
    code = employment_type_code(getattr(employee, "employment_type", None))
    allowed = getattr(leave_type, "applicable_employment_types", None)
    if code is None or allowed is None:
        return True
    return code in allowed


def get_remaining_leave_balance(
    db: Session, employee_id: uuid.UUID, leave_type: models.LeaveType,
    on_date: "datetime.date | None" = None, exclude_request_ids=(),
) -> float | None:
    """H-07/H-08: see leave_policy.balance -- fiscal-year scoped, pending
    days subtracted, capped types without an allocation capped by approved
    usage, negatives reported as negative."""
    from . import leave_policy

    return leave_policy.remaining(db, employee_id, leave_type, on_date, exclude_request_ids)


def is_earned_only_leave_type(db: Session, leave_type: "models.LeaveType | None") -> bool:
    """True for the company's compensatory-off leave type while Overtime
    Settings compensates overtime as Compensatory Leave -- usable only up to
    what approved overtime has credited (app/overtime.py). With Payable
    Overtime (or overtime off) nothing earns comp-off, so it follows the
    normal leave-type rules (max_days_per_year)."""
    if leave_type is None:
        return False
    from . import overtime
    settings = overtime.get_settings(db, leave_type.company_id)
    if not settings.enabled or settings.compensation_mode != "comp_off":
        return False
    comp_off = overtime.comp_off_leave_type(db, leave_type.company_id, settings)
    return comp_off is not None and comp_off.id == leave_type.id


def get_lop_leave_type(db: Session, company_id: uuid.UUID) -> models.LeaveType | None:
    """The company's Loss-of-Pay leave type, identified by code -- not
    auto-created if missing, since guessing at is_paid/max_days_per_year
    defaults for a company's own unpaid-leave policy isn't this app's call
    to make. Callers turn a None here into a 409 telling the company to
    configure one via Leave Types > Add Leave Type."""
    return db.scalar(
        select(models.LeaveType).where(
            models.LeaveType.company_id == company_id,
            func.upper(models.LeaveType.code) == "LOP",
        )
    )


def list_other_leave_types_with_balance(
    db: Session, company_id: uuid.UUID, employee_id: uuid.UUID, exclude_leave_type_id: uuid.UUID
) -> list[tuple[models.LeaveType, float | None]]:
    """Every configured leave type (excluding the one already being applied
    for, and excluding the LOP type itself -- LOP is never a user-chosen
    fallback, only the automatic last resort) that still has balance left,
    paired with that remaining balance (None = uncapped/always available).
    Used to offer the employee a real choice of which of their OWN other
    company-provided leave balances should absorb a shortfall, instead of
    defaulting straight to unpaid leave."""
    lop_type = get_lop_leave_type(db, company_id)
    types = db.scalars(
        select(models.LeaveType).where(models.LeaveType.company_id == company_id)
    ).all()
    out = []
    for lt in types:
        if lt.id == exclude_leave_type_id:
            continue
        if lop_type is not None and lt.id == lop_type.id:
            continue
        remaining = get_remaining_leave_balance(db, employee_id, lt)
        if remaining is None or remaining > 0:
            out.append((lt, remaining))
    return out


def get_overlapping_leave_request(
    db: Session,
    employee_id: uuid.UUID,
    from_date: datetime.date,
    to_date: datetime.date,
) -> models.LeaveRequest | None:
    """Any existing leave request for this employee (pending, sent_back, or
    approved -- rejected requests never block) whose [from_date, to_date]
    range overlaps the new one. Used by POST /leave-requests to reject a
    duplicate/overlapping application with a 409 instead of silently
    allowing two live requests to cover the same day(s). Cancelled requests
    (WF-04) never block either."""
    return db.scalar(
        select(models.LeaveRequest).where(
            models.LeaveRequest.employee_id == employee_id,
            # M-06: withdrawn leave never blocks re-applying either.
            models.LeaveRequest.status.not_in(("rejected", "cancelled", "withdrawn")),
            models.LeaveRequest.from_date <= to_date,
            models.LeaveRequest.to_date >= from_date,
        ).limit(1)
    )


def resolve_leave_request_split(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    leave_type_name: str,
    requested_days: float,
    overflow_leave_type_name: str | None = None,
    on_date: "datetime.date | None" = None,
    leave_type_id: uuid.UUID | None = None,
    exclude_request_ids=(),
) -> tuple[models.LeaveType, float, list[tuple[models.LeaveType, float]]]:
    """Returns (primary_leave_type, primary_days, overflow_legs) where
    overflow_legs is a list of (leave_type, days) pairs -- empty when no
    split is needed (enough balance, an uncapped type, or the requested
    type already *is* the company's LOP type).

    When a split IS needed:
    - [overflow_leave_type_name], if given, is the OTHER company-provided
      leave type the employee chose (via
      list_other_leave_types_with_balance) to cover the shortfall first.
      If that type's own balance covers the whole shortfall, it's the only
      overflow leg. If it doesn't fully cover it (its own balance runs out
      first), a second leg for the remainder is added against the LOP
      type -- LOP is always the final backstop once every other balance is
      exhausted, never skipped straight to.
    - If no overflow_leave_type_name is given (the employee had no other
      leave type with balance left to choose from), the whole shortfall
      goes to LOP directly, same as before.
    Raises ValueError (-> 409 in the router) when LOP would be needed for
    some or all of the shortfall but the company has no LOP-coded leave
    type configured."""
    from . import leave_policy

    # H-09: only an existing leave type (leave_policy.LeaveTypeNotFound -> 422).
    leave_type = leave_policy.require_leave_type(db, company_id, leave_type_name, leave_type_id)
    lop_type = get_lop_leave_type(db, company_id)
    if lop_type is not None and leave_type.id == lop_type.id:
        return leave_type, requested_days, []
    employee = db.get(models.Employee, employee_id)
    if employee is not None and not leave_type_applies(employee, leave_type):
        raise ValueError(
            f"'{leave_type.name}' isn't available for {employee.employment_type or 'your'} employees. "
            "Apply for Loss of Pay instead, or ask HR to enable this leave type for your employment type."
        )

    remaining = get_remaining_leave_balance(db, employee_id, leave_type, on_date, exclude_request_ids)
    if remaining is None or requested_days <= remaining:
        return leave_type, requested_days, []

    primary_days = max(0.0, remaining)
    shortfall = requested_days - primary_days
    legs: list[tuple[models.LeaveType, float]] = []

    if overflow_leave_type_name is not None:
        chosen_type = leave_policy.require_leave_type(db, company_id, overflow_leave_type_name)
        chosen_remaining = get_remaining_leave_balance(db, employee_id, chosen_type, on_date, exclude_request_ids)
        if chosen_remaining is None:
            legs.append((chosen_type, shortfall))
            shortfall = 0.0
        else:
            covered = min(max(0.0, chosen_remaining), shortfall)
            if covered > 0:
                legs.append((chosen_type, covered))
                shortfall -= covered

    if shortfall > 0:
        if lop_type is None:
            raise ValueError(
                f"'{leave_type.name}' only has {remaining} day(s) remaining, and "
                "this company has no 'LOP' (Loss of Pay) leave type configured "
                "to cover the rest. Add one under Leave Types before submitting "
                "a request that exceeds your available balance."
            )
        legs.append((lop_type, shortfall))

    return leave_type, primary_days, legs


def create_leave_request(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    leave_type_name: str,
    from_date: datetime.date,
    to_date: datetime.date,
    days: float,
    reason: str | None,
    is_half_day: bool,
    half_day_period: str | None = None,
) -> models.LeaveRequest:
    from . import leave_policy

    leave_type = leave_policy.require_leave_type(db, company_id, leave_type_name)
    leave_request = models.LeaveRequest(
        id=uuid.uuid4(),
        company_id=company_id,
        employee_id=employee_id,
        leave_type_id=leave_type.id,
        from_date=from_date,
        to_date=to_date,
        days=days,
        is_half_day=is_half_day,
        half_day_period=half_day_period if is_half_day else None,
        reason=reason,
        status="pending",
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(leave_request)
    db.flush()
    return leave_request


def list_leave_requests(
    db: Session,
    company_id: uuid.UUID,
    status: str | None = None,
    employee_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.LeaveRequest]:
    q = (
        select(models.LeaveRequest)
        .options(
            selectinload(models.LeaveRequest.leave_type),
            selectinload(models.LeaveRequest.employee),
        )
        .where(models.LeaveRequest.company_id == company_id)
    )
    if status is not None:
        q = q.where(models.LeaveRequest.status == status)
    if employee_ids is not None:
        q = q.where(models.LeaveRequest.employee_id.in_(employee_ids))
    q = q.order_by(models.LeaveRequest.from_date.desc(), models.LeaveRequest.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def _session_company_id(db: Session) -> uuid.UUID | None:
    """C3/C14: company of the authenticated caller this Session belongs to
    (stamped by deps.get_current_user via database.set_session_current_user),
    used as the default company filter for the *_for_update lookups below so
    a caller can never lock/decide another company's row that happens to live
    in the same tenant schema (e.g. the multi-company 'acme' schema). None
    outside a request (scripts), where no filter is applied."""
    user_id = database.get_session_current_user(db)
    if user_id is None:
        return None
    user = db.get(models.User, user_id)
    return user.company_id if user is not None else None


def get_leave_request_for_update(
    db: Session, leave_request_id: uuid.UUID, company_id: uuid.UUID | None = None
) -> models.LeaveRequest | None:
    """SELECT ... FOR UPDATE -- same double-approval protection as
    get_regularization_for_update. Company-scoped (C3): explicit
    `company_id`, else the authenticated caller's company."""
    company_id = company_id or _session_company_id(db)
    q = select(models.LeaveRequest).where(models.LeaveRequest.id == leave_request_id)
    if company_id is not None:
        q = q.where(models.LeaveRequest.company_id == company_id)
    return db.scalar(q.with_for_update())


def adjust_leave_allocation_used(
    db: Session,
    employee_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    delta_days: float,
    on_date: datetime.date | None = None,
) -> None:
    """Approving a leave request should count its days against the
    employee's balance; un-approving (e.g. correcting to rejected) should
    give them back. No-ops if no allocation row exists for this employee/
    leave type yet, rather than fabricating one.

    DATA-02/C13: `on_date` (the leave's from_date) selects the allocation
    of the fiscal year covering that date -- without it, once a second
    year's allocation existed, db.scalar picked an arbitrary year. Callers
    that omit it keep the legacy (unscoped) lookup."""
    stmt = select(models.LeaveAllocation).where(
        models.LeaveAllocation.employee_id == employee_id,
        models.LeaveAllocation.leave_type_id == leave_type_id,
    )
    if on_date is not None:
        stmt = stmt.join(
            models.FiscalYear, models.FiscalYear.id == models.LeaveAllocation.fiscal_year_id
        ).where(
            models.FiscalYear.start_date <= on_date,
            models.FiscalYear.end_date >= on_date,
        )
    allocation = db.scalar(stmt.order_by(models.LeaveAllocation.id).limit(1))
    if allocation is None:
        return
    allocation.used_days = max(0.0, _num(allocation.used_days) + delta_days)
    db.flush()


def get_leave_request(db: Session, leave_request_id: uuid.UUID) -> models.LeaveRequest | None:
    """Plain (non-locking) lookup -- used to validate a leave request exists
    before attaching a file to it; unlike get_leave_request_for_update, this
    isn't part of a status-change transaction so there's nothing to lock."""
    return db.scalar(select(models.LeaveRequest).where(models.LeaveRequest.id == leave_request_id))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def create_audit_log(
    db: Session,
    company_id: uuid.UUID,
    user_id: uuid.UUID | None,
    action: str,
    doctype: str,
    document_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    changes: dict | None = None,
) -> models.AuditLog:
    """Best-effort: called from within an endpoint's own db.commit(), never
    the reason a request fails -- callers wrap this in a try/except.
    `changes` is an optional free-form {"before": ..., "after": ...} (or
    similar) diff, stored as-is in the JSONB column of the same name --
    added so configuration-mutation endpoints can leave a real diff behind,
    not just an action/doctype tag. Existing call sites are unaffected
    since it defaults to None.

    API-02: ip_address defaults to the current request's client IP (set by
    middleware/client_ip.py, honouring RATE_LIMIT_TRUST_PROXY like the rate
    limiter), so no call site has to pass it explicitly."""
    if ip_address is None:
        from .middleware.client_ip import get_current_client_ip

        ip_address = get_current_client_ip()
    log = models.AuditLog(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        action=action,
        doctype=doctype,
        document_id=document_id,
        ip_address=ip_address,
        changes=changes,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(log)
    db.flush()
    return log


def list_audit_logs(
    db: Session,
    company_id: uuid.UUID,
    limit: int = 200,
    offset: int = 0,
    from_date: datetime.date | None = None,
    to_date: datetime.date | None = None,
) -> list[models.AuditLog]:
    """Previously capped at the 200 most-recent rows with no way to page
    past that or filter by date -- callers needing older history had no
    way to reach it. Defaults (limit=200, offset=0, no date filter) match
    the exact prior behavior; limit is still hard-capped at 200
    server-side regardless of what's requested, same ceiling as before,
    just now reachable page by page via offset instead of being a dead
    end."""
    q = select(models.AuditLog).where(models.AuditLog.company_id == company_id)
    if from_date is not None:
        q = q.where(models.AuditLog.created_at >= from_date)
    if to_date is not None:
        q = q.where(models.AuditLog.created_at < to_date + datetime.timedelta(days=1))
    return db.scalars(
        q.order_by(models.AuditLog.created_at.desc()).offset(offset).limit(min(limit, 200))
    ).all()


# ---------------------------------------------------------------------------
# Notifications (Reporting Manager Approval Workflow)
# ---------------------------------------------------------------------------


def get_user_id_for_employee(db: Session, employee_id: uuid.UUID | None) -> uuid.UUID | None:
    if employee_id is None:
        return None
    user = db.scalar(select(models.User).where(models.User.employee_id == employee_id))
    return user.id if user is not None else None


def create_notification(
    db: Session,
    company_id: uuid.UUID,
    user_id: uuid.UUID | None,
    title: str,
    body: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> models.Notification | None:
    """Best-effort, like create_audit_log: never the reason a request
    fails. Silently no-ops when the target has no login account (user_id is
    None) -- there's nowhere to deliver it."""
    if user_id is None:
        return None
    note = models.Notification(
        id=uuid.uuid4(),
        company_id=company_id,
        user_id=user_id,
        title=title,
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(note)
    db.flush()
    payload = schemas.NotificationOut.model_validate(note).model_dump(mode="json")
    ws_manager.queue_push(db, user_id, payload)
    return note


# Reporting Manager Approval Workflow -- entity_type -> the real model
# `entity_id` refers to, so _request_entity_details/_request_entity_approver_id
# can fetch the row fresh (reflecting whatever state it's in: pending at
# notify_new_request time, decided at notify_decision time) without every
# router call site having to pass its own field dump.
_ENTITY_TYPE_TO_MODEL: dict[str, type] = {
    "leave_request": models.LeaveRequest,
    "regularization": models.AttendanceRegularization,
    "overtime_request": models.OvertimeRequest,
    "timesheet": models.Timesheet,
    "travel_request": models.TravelRequestModel,
    "expense_report": models.ExpenseClaim,
    "reimbursement": models.ExpenseClaim,
    "asset_request": models.AssetRequestModel,
    "salary_revision_request": models.SalaryRevisionRequest,
    "hiring_requisition": models.HiringRequisition,
    "exit_request": models.ExitRequestModel,
    "leave_withdrawal": models.LeaveWithdrawal,
}

# "Leave Request" (and its auto-split variant "Leave Request (Earned Leave +
# Loss of Pay)") reads as "Leave Application Notification" in the email
# heading, matching the reference Leave Application email's terminology --
# every other type just gets "{request_type_label} Notification" (see
# _request_heading_label).
_REQUEST_HEADING_LABEL = {"Leave Request": "Leave Application"}


def _request_heading_label(request_type_label: str) -> str:
    base = request_type_label.split(" (", 1)[0]
    return _REQUEST_HEADING_LABEL.get(base, base)


def _fmt_req_date(d: "datetime.date | None") -> str:
    return d.strftime("%d %b %Y") if d else "—"


def _fmt_req_datetime(dt: "datetime.datetime | None") -> str:
    return dt.strftime("%d %b %Y, %I:%M %p UTC") if dt else "—"


def _request_entity_details(db: Session, entity_type: str, entity_id: uuid.UUID) -> dict[str, str]:
    """Real, type-specific "Details" fields for the notification email --
    fetched fresh by entity_id, so it shows only the fields relevant to
    that request type (Leave Type/From/To Date for Leave, Asset Type for
    an Asset Request, etc.), never a one-size-fits-all generic dump."""
    model = _ENTITY_TYPE_TO_MODEL.get(entity_type)
    if model is None:
        return {}
    row = db.get(model, entity_id)
    if row is None:
        return {}

    if entity_type == "leave_request":
        leave_type = db.get(models.LeaveType, row.leave_type_id)
        details = {
            "Leave Type": leave_type.name if leave_type else "—",
            "From Date": _fmt_req_date(row.from_date),
            "To Date": _fmt_req_date(row.to_date),
            "Days": str(row.days),
        }
        if row.is_half_day:
            details["Half Day"] = (row.half_day_period or "—").capitalize()
        details["Reason"] = row.reason or "—"
        return details
    if entity_type == "regularization":
        return {
            "Attendance Date": _fmt_req_date(row.attendance_date),
            "Requested Check-In": _fmt_req_datetime(row.requested_in),
            "Requested Check-Out": _fmt_req_datetime(row.requested_out),
            "Reason": row.reason or "—",
        }
    if entity_type == "leave_withdrawal":
        leave = db.get(models.LeaveRequest, row.leave_request_id)
        details = {
            "Leave Type": leave.leave_type.name if leave and leave.leave_type else "—",
            "Leave Dates": (f"{_fmt_req_date(leave.from_date)} – {_fmt_req_date(leave.to_date)}" if leave else "—"),
            "Days": str(leave.days) if leave else "—",
            "Leave Status": (leave.status.replace("_", " ").capitalize() if leave else "—"),
            "Withdrawal Reason": row.reason or "—",
        }
        return details
    if entity_type == "overtime_request":
        details = {"Work Date": _fmt_req_date(row.work_date)}
        if row.start_time is not None:
            details["Start Time"] = row.start_time.strftime("%I:%M %p")
        if row.end_time is not None:
            details["End Time"] = row.end_time.strftime("%I:%M %p")
        details["Hours"] = f"{float(row.hours):g}"
        details["Reason"] = row.reason or "—"
        return details
    if entity_type == "timesheet":
        return {
            "Week Starting": _fmt_req_date(row.week_start),
            "Total Hours": str(row.total_hours),
            "Billable Hours": str(row.billable_hours),
        }
    if entity_type == "travel_request":
        return {
            "Purpose": row.purpose or "—",
            "Destination": row.destination or "—",
            "From Date": _fmt_req_date(row.from_date),
            "To Date": _fmt_req_date(row.to_date),
            "Travel Mode": row.travel_mode or "—",
            "Estimated Cost": format_inr_budget(row.estimated_cost) if row.estimated_cost is not None else "—",
        }
    if entity_type in ("expense_report", "reimbursement"):
        details = {
            "Claim No": row.claim_no,
            "Claim Date": _fmt_req_date(row.claim_date),
            "Purpose": row.purpose or "—",
            "Total Amount": format_inr_budget(row.total_amount),
        }
        lines = db.scalars(
            select(models.ExpenseClaimLine).where(models.ExpenseClaimLine.claim_id == row.id)
        ).all()
        if lines:
            details["Line Items"] = "; ".join(
                f"{line.category or line.description or 'Item'}: {format_inr_budget(line.amount)}"
                for line in lines
            )
        return details
    if entity_type == "asset_request":
        return {
            "Asset Type": row.asset_type,
            "Justification": row.justification or "—",
        }
    if entity_type == "salary_revision_request":
        return {
            "Current CTC": format_inr_budget(row.current_ctc) if row.current_ctc is not None else "—",
            "Proposed CTC": format_inr_budget(row.proposed_ctc) if row.proposed_ctc is not None else "—",
            "Reason": row.reason or "—",
        }
    if entity_type == "hiring_requisition":
        department = db.get(models.Department, row.department_id) if row.department_id else None
        return {
            "Designation": row.designation_title,
            "Department": department.name if department else "—",
            "Positions": str(row.positions_count),
            "Justification": row.justification or "—",
        }
    if entity_type == "exit_request":
        return {
            "Resignation Date": _fmt_req_date(row.resignation_date),
            "Last Working Day": _fmt_req_date(row.last_working_day),
            "Reason": row.reason or "—",
        }
    return {}


_AUTO_APPROVED_ENTITY_TYPES = {"work_entry", "timesheet"}

# Notification entity_type -> approval-workflow doctype, where the two
# names differ (Administration > Approval Workflows is keyed by doctype).
ENTITY_TYPE_TO_DOCTYPE = {
    "regularization": "attendance_regularization",
    "reimbursement": "expense_claim",
    "expense_report": "expense_claim",
}
# Approval-workflow doctype -> (notification entity_type, label) for the
# "it's your turn" notice sent to the next step of a chain.
DOCTYPE_NOTICE = {
    "leave_request": ("leave_request", "Leave Request"),
    "leave_withdrawal": ("leave_withdrawal", "Leave Withdrawal"),
    "attendance_regularization": ("regularization", "Attendance Regularization"),
    "overtime_request": ("overtime_request", "Overtime Request"),
    "travel_request": ("travel_request", "Travel Request"),
    "expense_claim": ("reimbursement", "Reimbursement / Expense Claim"),
    "salary_revision_request": ("salary_revision_request", "Salary Revision Request"),
    "asset_request": ("asset_request", "Asset Request"),
    "exit_request": ("exit_request", "Exit Request"),
}
# Doctypes whose module already notifies its own next approver (recruitment).
_SELF_NOTIFYING_DOCTYPES = {"hiring_requisition", "recruitment_offer"}


def notify_new_request(
    db: Session,
    company_id: uuid.UUID,
    requester: models.Employee,
    request_type_label: str,
    entity_type: str,
    entity_id: uuid.UUID,
) -> None:
    """Reporting Manager Approval Workflow: tells the requester's Reporting
    Manager(s) a new request is waiting on them. [requester] must already
    have a non-null reporting_manager_id (every create endpoint validates
    this before calling here).

    Two Reporting Managers: notifies both reporting_manager_id and, when
    set, dotted_line_manager_id -- either one may act on the request (see
    crud._is_in_reporting_chain), so both need to know it's waiting."""
    manager_ids = {requester.reporting_manager_id, requester.dotted_line_manager_id} - {None}
    # Configured approval workflow (Administration > Approval Workflows):
    # notify exactly the approvers of the request's CURRENT step -- in a
    # Sequential chain that is step 1 only (step 2 hears about it once step 1
    # approves, see decide_configurable_request); in Parallel mode every
    # approver of the shared step. No configured workflow = unchanged.
    doctype = ENTITY_TYPE_TO_DOCTYPE.get(entity_type, entity_type)
    if entity_type not in _AUTO_APPROVED_ENTITY_TYPES and _has_custom_approval_workflow(db, company_id, doctype):
        from . import approval_engine

        engine_request = _get_or_start_approval_request(db, company_id, doctype, entity_id, requester.id)
        manager_ids = approval_engine.current_step_approver_employee_ids(db, engine_request, requester)
    if not manager_ids:
        # WF-03: a requester with no Reporting Manager (e.g. the CEO) is
        # routed to the fallback approvers who can already decide it via
        # can_decide_request / approval_engine.can_act (Owner / System
        # Settings RBAC holders), never to themselves.
        manager_ids = set(
            list_fallback_approver_employee_ids(db, company_id, exclude_employee_id=requester.id)
        )
        if doctype in ("leave_request", "leave_withdrawal"):
            # M-08: leave administrators (HR) are leave's fallback approvers too.
            from . import leave_policy

            manager_ids |= leave_policy.leave_admin_employee_ids(db, company_id, exclude_employee_id=requester.id)
    requester_name = f"{requester.first_name} {requester.last_name or ''}".strip()
    details = _request_entity_details(db, entity_type, entity_id)
    # Work entries / timesheets are approved on submission and never appear
    # in the Approvals inbox, so the manager is informed, not asked to review.
    auto_approved = entity_type in _AUTO_APPROVED_ENTITY_TYPES
    body = (
        f"{requester_name} submitted a {request_type_label.lower()} (approved automatically)."
        if auto_approved
        else f"{requester_name} submitted a {request_type_label.lower()} that needs your review."
    )
    intro_line = (
        f"{requester_name} has submitted a {request_type_label}. It was approved automatically; no action is needed."
        if auto_approved
        else f"{requester_name} has raised a {request_type_label}, awaiting your review as their manager."
    )
    leave = _leave_email_fields(db, entity_type, entity_id)
    for manager_employee_id in manager_ids:
        create_notification(
            db,
            company_id,
            get_user_id_for_employee(db, manager_employee_id),
            title=f"New {request_type_label} from {requester_name}",
            body=body,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        manager = db.get(models.Employee, manager_employee_id)
        if leave is not None:
            # Leave has its own template / subject ("New Leave Application - ...").
            email_service.send_leave_applied_email(
                db,
                to=manager.work_email if manager else None,
                employee_name=requester_name,
                employee_code=requester.employee_code,
                status="Pending Approval",
                leave_request_id=entity_id,
                company_id=company_id,
                from_display_name=requester_name,
                approver_employee_id=manager_employee_id,
                **leave,
            )
            continue
        email_service.send_request_email(
            manager.work_email if manager else None,
            subject=f"{requester_name} has raised a {request_type_label}",
            heading=f"{_request_heading_label(request_type_label)} Notification",
            intro_line=intro_line,
            details={
                "Employee": requester_name,
                "Employee ID": requester.employee_code or "—",
                "Request Type": request_type_label,
                **details,
                "Status": "Approved" if auto_approved else "Pending Approval",
            },
            entity_type=entity_type,
            entity_id=entity_id,
            from_display_name=requester_name,
            cta_label="Open Now",
            db=db,
            company_id=company_id,
            email_type=email_service.REQUEST_SUBMITTED,
        )
    # Leave applications also go to the configured HR inbox(es)
    # (EMAIL_HR_RECIPIENTS), in addition to the reporting manager(s).
    if leave is not None and settings.email_hr_recipients:
        email_service.send_leave_applied_email(
            db,
            to=settings.email_hr_recipients,
            employee_name=requester_name,
            employee_code=requester.employee_code,
            status="Pending Approval",
            leave_request_id=entity_id,
            company_id=company_id,
            from_display_name=requester_name,
            **leave,
        )


def _leave_email_fields(db: Session, entity_type: str, entity_id: uuid.UUID) -> dict | None:
    """The leave-template fields for a leave request, or None for any other type."""
    if entity_type != "leave_request":
        return None
    row = db.get(models.LeaveRequest, entity_id)
    if row is None:
        return None
    leave_type = db.get(models.LeaveType, row.leave_type_id)
    days = float(row.days)
    days_label = f"{days:g}" + (f" ({(row.half_day_period or '').capitalize()} half day)" if row.is_half_day else "")
    return {
        "leave_type": leave_type.name if leave_type else "—",
        "start_date": _fmt_req_date(row.from_date),
        "end_date": _fmt_req_date(row.to_date),
        "number_of_days": days_label,
        "reason": row.reason,
    }


_STATUS_VERB = {"approved": "approved", "rejected": "rejected", "sent_back": "sent back for revision"}


def notify_decision(
    db: Session,
    company_id: uuid.UUID,
    requester_employee_id: uuid.UUID,
    request_type_label: str,
    status: str,
    decision_notes: str | None,
    entity_type: str,
    entity_id: uuid.UUID,
) -> None:
    """Reporting Manager Approval Workflow: tells the requester their
    manager decided on their request. Reads the row's own approver_id
    (already set by the router's decide-request endpoint before this is
    called) rather than requiring every call site to pass the deciding
    manager explicitly."""
    requester_user_id = get_user_id_for_employee(db, requester_employee_id)
    verb = _STATUS_VERB.get(status, status)
    body = f"Your {request_type_label.lower()} was {verb}."
    if decision_notes:
        body += f' Comments: "{decision_notes}"'
    create_notification(
        db,
        company_id,
        requester_user_id,
        title=f"{request_type_label} {verb}",
        body=body,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    requester = db.get(models.Employee, requester_employee_id)
    if requester is None:
        return
    requester_name = f"{requester.first_name} {requester.last_name or ''}".strip()
    details = _request_entity_details(db, entity_type, entity_id)
    model = _ENTITY_TYPE_TO_MODEL.get(entity_type)
    row = db.get(model, entity_id) if model else None
    approver_id = getattr(row, "approver_id", None) if row is not None else None
    approver_name = employee_display_name(db, approver_id)
    decided_at = getattr(row, "decided_at", None) if row is not None else None
    decision_fields = {
        "Employee": requester_name,
        "Employee ID": requester.employee_code or "—",
        "Request Type": request_type_label,
        **details,
        "Status": verb.capitalize(),
        "Approver": approver_name,
        "Decision Date/Time": _fmt_req_datetime(decided_at) if decided_at else _fmt_req_datetime(datetime.datetime.now(datetime.timezone.utc)),
    }
    if decision_notes:
        decision_fields["Decision Notes"] = decision_notes
    leave = _leave_email_fields(db, entity_type, entity_id)
    if leave is not None and status in ("approved", "rejected") and requester.work_email:
        common = dict(
            to=requester.work_email,
            employee_name=requester_name,
            employee_code=requester.employee_code,
            leave_type=leave["leave_type"],
            start_date=leave["start_date"],
            end_date=leave["end_date"],
            number_of_days=leave["number_of_days"],
            leave_request_id=entity_id,
            company_id=company_id,
        )
        if status == "approved":
            email_service.send_leave_approved_email(
                db, approved_by=approver_name, notes=decision_notes, **common,
            )
        else:
            email_service.send_leave_rejected_email(
                db, rejected_by=approver_name, rejection_reason=decision_notes, **common,
            )
        return
    email_service.send_request_email(
        requester.work_email,
        subject=f"Your {request_type_label} has been {verb}",
        heading=f"{_request_heading_label(request_type_label)} Notification",
        intro_line=f"{approver_name} has {verb} your {request_type_label}.",
        details=decision_fields,
        entity_type=entity_type,
        entity_id=entity_id,
        from_display_name=approver_name,
        cta_label="Open Now",
        db=db,
        company_id=company_id,
        email_type=f"{email_service.REQUEST_DECIDED}_{status.upper()}"[:40],
    )


def list_notifications(db: Session, user_id: uuid.UUID, limit: int = 50) -> list[models.Notification]:
    return db.scalars(
        select(models.Notification)
        .where(models.Notification.user_id == user_id)
        .order_by(models.Notification.created_at.desc())
        .limit(limit)
    ).all()


def count_unread_notifications(db: Session, user_id: uuid.UUID) -> int:
    return db.scalar(
        select(func.count())
        .select_from(models.Notification)
        .where(models.Notification.user_id == user_id, models.Notification.read_at.is_(None))
    ) or 0


def mark_notification_read(db: Session, notification_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    note = db.get(models.Notification, notification_id)
    if note is None or note.user_id != user_id:
        return False
    if note.read_at is None:
        note.read_at = datetime.datetime.now(datetime.timezone.utc)
        db.flush()
    return True


def mark_all_notifications_read(db: Session, user_id: uuid.UUID) -> None:
    db.execute(
        update(models.Notification)
        .where(models.Notification.user_id == user_id, models.Notification.read_at.is_(None))
        .values(read_at=datetime.datetime.now(datetime.timezone.utc))
    )


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def create_attachment(
    db: Session,
    company_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    file_name: str,
    file_url: str,
    mime_type: str | None,
    size_bytes: int | None,
) -> models.Attachment:
    attachment = models.Attachment(
        id=uuid.uuid4(),
        company_id=company_id,
        entity_type=entity_type,
        entity_id=entity_id,
        file_name=file_name,
        file_url=file_url,
        mime_type=mime_type,
        size_bytes=size_bytes,
    )
    db.add(attachment)
    db.flush()
    return attachment


def list_attachments_for_entity(
    db: Session, entity_type: str, entity_id: uuid.UUID
) -> list[models.Attachment]:
    return db.scalars(
        select(models.Attachment)
        .where(
            models.Attachment.entity_type == entity_type,
            models.Attachment.entity_id == entity_id,
        )
        .order_by(models.Attachment.id)
    ).all()


def attachment_file_names_bulk(
    db: Session, entity_type: str, entity_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Bulk version of list_attachments_for_entity's file_name projection --
    ONE query for many entity_ids instead of one query per row. Used by
    list endpoints that show attached document names per row (e.g. Leave
    Request certificates); an id with no attachments simply isn't a key in
    the returned dict -- callers already default via `.get(id, [])`."""
    ids = list({i for i in entity_ids if i is not None})
    if not ids:
        return {}
    rows = db.execute(
        select(models.Attachment.entity_id, models.Attachment.file_name)
        .where(models.Attachment.entity_type == entity_type, models.Attachment.entity_id.in_(ids))
        .order_by(models.Attachment.entity_id, models.Attachment.id)
    ).all()
    result: dict[uuid.UUID, list[str]] = {}
    for row in rows:
        result.setdefault(row.entity_id, []).append(row.file_name)
    return result


def get_attachment_for_entity(
    db: Session, entity_type: str, entity_id: uuid.UUID, attachment_id: uuid.UUID
) -> models.Attachment | None:
    return db.scalar(
        select(models.Attachment).where(
            models.Attachment.id == attachment_id,
            models.Attachment.entity_type == entity_type,
            models.Attachment.entity_id == entity_id,
        )
    )


def delete_attachment(db: Session, attachment: models.Attachment) -> None:
    db.delete(attachment)
    db.flush()


# ---------------------------------------------------------------------------
# Payroll
# ---------------------------------------------------------------------------


def list_payslips(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[dict]:
    """Payroll > Payslips -- every real hcm.salary_slips row, newest run
    first, with the payroll_runs period formatted for display."""
    q = (
        select(models.SalarySlip, models.PayrollRun)
        .join(models.PayrollRun, models.PayrollRun.id == models.SalarySlip.payroll_run_id)
        .join(models.Employee, models.Employee.id == models.SalarySlip.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(
            models.PayrollRun.period_year.desc(),
            models.PayrollRun.period_month.desc(),
            models.SalarySlip.id,
        )
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    rows = db.execute(q).all()
    return [
        {
            "id": slip.id,
            "employee_id": slip.employee_id,
            "month": f"{calendar.month_name[run.period_month]} {run.period_year}",
            "gross": round(_num(slip.gross_pay)),
            "deductions": round(_num(slip.total_deductions)),
            "net": round(_num(slip.net_pay)),
            "reimbursements": round(_num(slip.reimbursements_total)),
            "status": slip.status,
        }
        for slip, run in rows
    ]


def payroll_dashboard_summary(db: Session, company_id: uuid.UUID) -> dict:
    """Payroll screen's 4 headline StatCards -- computed from the most
    recent real payroll run rather than the whole 3-run history, matching
    "MTD"/"Employees Processed"-for-this-run semantics."""
    # RPT-8: the latest run up to the current month -- runs already created
    # for future periods must not show up as this month's figures.
    today = company_today(db, company_id)
    period_key = models.PayrollRun.period_year * 100 + models.PayrollRun.period_month
    latest_run = db.scalar(
        select(models.PayrollRun)
        .where(models.PayrollRun.company_id == company_id)
        .where(period_key <= today.year * 100 + today.month)
        .order_by(models.PayrollRun.period_year.desc(), models.PayrollRun.period_month.desc())
    )
    total_employees = db.scalar(
        select(func.count(models.Employee.id)).where(
            models.Employee.company_id == company_id, models.Employee.is_active.is_(True)
        )
    )
    if latest_run is None:
        return {
            "total_payroll_cost": 0,
            "employees_processed": 0,
            "total_employees": total_employees or 0,
            "pf_contribution": 0,
            "tds_deducted": 0,
        }

    slips = db.scalars(
        select(models.SalarySlip).where(models.SalarySlip.payroll_run_id == latest_run.id)
    ).all()
    total_cost = sum(_num(s.gross_pay) for s in slips)

    slip_ids = [s.id for s in slips]
    pf_total = 0.0
    type_totals: dict[str, float] = {}
    if slip_ids:
        for line, component in db.execute(
            select(models.SalarySlipLine, models.SalaryComponent)
            .join(models.SalaryComponent, models.SalaryComponent.id == models.SalarySlipLine.component_id)
            .where(models.SalarySlipLine.slip_id.in_(slip_ids))
        ).all():
            if _is_pf_deduction(component):
                pf_total += _num(line.amount)
            type_totals[component.component_type] = (
                type_totals.get(component.component_type, 0) + _num(line.amount)
            )

    return {
        "total_payroll_cost": round(total_cost),
        "employees_processed": len(slips),
        "total_employees": total_employees or 0,
        "pf_contribution": round(pf_total),
        # By component_type=='tax', not a hardcoded name -- covers both the
        # seeded "Income Tax (TDS)" component and get_or_create_tds_component's
        # auto-appended "TDS (Estimated)" line uniformly.
        "tds_deducted": round(type_totals.get("tax", 0)),
        "period_label": f"{calendar.month_name[latest_run.period_month]} {latest_run.period_year}",
    }


def list_salary_components(db: Session, company_id: uuid.UUID) -> list[models.SalaryComponent]:
    return db.scalars(
        select(models.SalaryComponent).where(models.SalaryComponent.company_id == company_id)
    ).all()


def create_salary_component(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    code: str,
    component_type: str,
    calc_type: str,
    is_taxable: bool,
) -> models.SalaryComponent:
    existing = db.scalar(
        select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == company_id, models.SalaryComponent.code == code
        )
    )
    if existing is not None:
        raise ValueError(f"Salary component code '{code}' already exists")
    component = models.SalaryComponent(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        code=code,
        component_type=component_type,
        calc_type=calc_type,
        is_taxable=is_taxable,
    )
    db.add(component)
    db.flush()
    return component


def update_salary_component(
    db: Session,
    component_id: uuid.UUID,
    name: str | None = None,
    component_type: str | None = None,
    calc_type: str | None = None,
    is_taxable: bool | None = None,
) -> models.SalaryComponent | None:
    component = db.get(models.SalaryComponent, component_id)
    if component is None:
        return None
    if name is not None:
        component.name = name
    if component_type is not None:
        component.component_type = component_type
    if calc_type is not None:
        component.calc_type = calc_type
    if is_taxable is not None:
        component.is_taxable = is_taxable
    db.flush()
    return component


def list_salary_structures(db: Session, company_id: uuid.UUID) -> list[models.SalaryStructure]:
    return db.scalars(
        select(models.SalaryStructure)
        .options(selectinload(models.SalaryStructure.lines).selectinload(models.SalaryStructureLine.component))
        .where(models.SalaryStructure.company_id == company_id)
        .order_by(models.SalaryStructure.name)
    ).all()


def _replace_salary_structure_lines(
    db: Session, structure_id: uuid.UUID, lines: list[dict]
) -> None:
    if sum(1 for line in lines if line.get("percent_of") == "balance") > 1:
        raise ValueError("A structure can have at most one 'balance' line")
    # Overtime pay, reimbursements, LOP, asset recovery and the TDS estimate
    # are added by the payroll engine itself -- never a fixed structure line.
    from .payroll_rules import automatic_reason
    for line in lines:
        component = db.get(models.SalaryComponent, line["component_id"])
        reason = automatic_reason(component.name, component.code) if component is not None else None
        if reason:
            raise ValueError(f'"{component.name}" cannot be part of a salary structure: {reason} '
                             "Remove this line.")
    # M-35: percent-of-CTC earnings can never add up to more than the CTC.
    from .payroll_rules import validate_structure_lines
    validate_structure_lines(db, lines)
    db.execute(
        delete(models.SalaryStructureLine).where(models.SalaryStructureLine.structure_id == structure_id)
    )
    for line in lines:
        db.add(
            models.SalaryStructureLine(
                id=uuid.uuid4(),
                structure_id=structure_id,
                component_id=line["component_id"],
                amount=line.get("amount"),
                percent_of=line.get("percent_of"),
                percent=line.get("percent"),
            )
        )


def create_salary_structure(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    effective_from: datetime.date,
    lines: list[dict],
    department_id: uuid.UUID | None = None,
    designation_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    grade: str | None = None,
) -> models.SalaryStructure:
    structure = models.SalaryStructure(
        id=uuid.uuid4(), company_id=company_id, name=name, effective_from=effective_from, is_active=True,
        department_id=department_id, designation_id=designation_id, branch_id=branch_id, grade=grade,
    )
    db.add(structure)
    db.flush()
    _replace_salary_structure_lines(db, structure.id, lines)
    db.flush()
    return structure


def update_salary_structure(
    db: Session,
    structure_id: uuid.UUID,
    name: str | None = None,
    effective_from: datetime.date | None = None,
    lines: list[dict] | None = None,
    department_id: uuid.UUID | None = None,
    designation_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    grade: str | None = None,
) -> models.SalaryStructure | None:
    structure = db.get(models.SalaryStructure, structure_id)
    if structure is None:
        return None
    if name is not None:
        structure.name = name
    if effective_from is not None:
        structure.effective_from = effective_from
    if lines is not None:
        _replace_salary_structure_lines(db, structure_id, lines)
    if department_id is not None:
        structure.department_id = department_id
    if designation_id is not None:
        structure.designation_id = designation_id
    if branch_id is not None:
        structure.branch_id = branch_id
    if grade is not None:
        structure.grade = grade
    db.flush()
    return structure


def delete_salary_structure(db: Session, structure_id: uuid.UUID) -> bool:
    """Permanently removes a structure -- refuses (ValueError, router turns
    this into a 409) while any employee is still assigned to it, past or
    present, since deleting out from under a live assignment would silently
    break that employee's self-service view and future payroll runs.
    Reassign or deactivate first. Returns False if the structure doesn't
    exist (caller 404s)."""
    structure = db.get(models.SalaryStructure, structure_id)
    if structure is None:
        return False
    in_use = db.scalar(
        select(func.count(models.SalaryStructureAssignment.id)).where(
            models.SalaryStructureAssignment.structure_id == structure_id
        )
    )
    if in_use:
        raise ValueError(
            f"Cannot delete: {in_use} employee assignment(s) still reference this structure"
        )
    db.execute(
        delete(models.SalaryStructureLine).where(models.SalaryStructureLine.structure_id == structure_id)
    )
    db.delete(structure)
    db.flush()
    return True


def delete_salary_structure_assignment(db: Session, assignment_id: uuid.UUID) -> bool:
    """Permanently removes one assignment (+ any of its Phase-1 overrides).
    Safe at any time -- already-generated payslips are frozen snapshots
    (hcm_salary_slips/_lines) with no FK back to the assignment that
    produced them, so deleting an assignment never touches historical
    payroll data. Returns False if it doesn't exist (caller 404s)."""
    assignment = db.get(models.SalaryStructureAssignment, assignment_id)
    if assignment is None:
        return False
    db.execute(
        delete(models.SalaryStructureAssignmentOverride).where(
            models.SalaryStructureAssignmentOverride.assignment_id == assignment_id
        )
    )
    db.delete(assignment)
    db.flush()
    return True


def set_salary_structure_active(
    db: Session, structure_id: uuid.UUID, is_active: bool
) -> models.SalaryStructure | None:
    """Only toggles the flag -- existing assignments to a since-deactivated
    structure are left alone (create_salary_structure_assignment is what
    refuses to hand out an inactive structure to a NEW assignment)."""
    structure = db.get(models.SalaryStructure, structure_id)
    if structure is None:
        return None
    structure.is_active = is_active
    db.flush()
    return structure


def create_salary_structure_assignment(
    db: Session,
    employee_id: uuid.UUID,
    structure_id: uuid.UUID,
    from_date: datetime.date,
    annual_ctc: float,
    base_amount: float | None = None,
) -> models.SalaryStructureAssignment:
    """The "configured and approved" step itself -- only a caller who
    already passed require_permission_or_action("payroll_process", ...)
    can reach this, so the privileged action IS the approval; no separate
    approval workflow is layered on top (hcm_salary_revision_requests
    already covers CTC-change requests on an already-assigned employee --
    a distinct, pre-existing flow, untouched here)."""
    structure = db.get(models.SalaryStructure, structure_id)
    if structure is None:
        raise ValueError("Salary structure not found")
    if not structure.is_active:
        raise ValueError("Cannot assign an inactive salary structure")
    if base_amount is None:
        # Derive it from the structure's own Basic-equivalent line (the
        # first 'ctc'-percent line, same convention
        # _resolve_structure_lines_monthly uses) rather than defaulting to
        # annual_ctc itself, which would be a full year's figure sitting in
        # a "monthly base" column.
        lines = db.scalars(
            select(models.SalaryStructureLine)
            .options(selectinload(models.SalaryStructureLine.component))
            .where(models.SalaryStructureLine.structure_id == structure_id)
        ).all()
        resolved = _resolve_structure_lines_monthly(lines, annual_ctc)
        basic_line = next((r for r in resolved if r["line"].percent_of == "ctc"), None)
        base_amount = basic_line["amount"] if basic_line is not None else annual_ctc / 12
    assignment = models.SalaryStructureAssignment(
        id=uuid.uuid4(),
        employee_id=employee_id,
        structure_id=structure_id,
        from_date=from_date,
        base_amount=base_amount,
        annual_ctc=annual_ctc,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(assignment)
    db.flush()
    return assignment


def get_superseding_assignment(
    db: Session, employee_id: uuid.UUID, from_date: datetime.date, exclude_id: uuid.UUID
) -> models.SalaryStructureAssignment | None:
    """Finds another assignment on this employee with a LATER from_date
    than the one just created -- meaning the assignment just created will
    NOT be treated as current (get_employee_salary_structure/
    generate_payroll_slips both pick whichever assignment has the latest
    from_date). This is the exact confusion an Owner can hit: assigning a
    new CTC effective from an earlier date can look like it silently did
    nothing, because an unrelated later-dated record already exists and
    keeps winning. The resolution logic itself is untouched (an Owner may
    genuinely want to schedule a raise for a real future date); this only
    makes the ambiguity visible immediately, at creation time, instead of
    leaving the Owner to discover it by surprise."""
    return db.scalar(
        select(models.SalaryStructureAssignment)
        .where(
            models.SalaryStructureAssignment.employee_id == employee_id,
            models.SalaryStructureAssignment.id != exclude_id,
            models.SalaryStructureAssignment.from_date > from_date,
        )
        .order_by(models.SalaryStructureAssignment.from_date.desc())
    )


def resync_draft_payroll_runs(
    db: Session, company_id: uuid.UUID, employee_id: uuid.UUID, effective_from: datetime.date
) -> list[models.PayrollRun]:
    """Whenever a salary structure (re)assignment or override takes effect,
    keep "My Payslips" connected to it: any payroll run whose period could
    be affected (to_date >= effective_from) AND that has no slip already
    marked 'paid' has JUST this one employee's slip regenerated immediately
    (via resync_employee_slip_in_run), so they see the correct figures
    right away instead of a stale pre-change snapshot sitting there until
    someone happens to click "Generate" again -- without paying the cost
    of recomputing every other employee in the company, who didn't change.

    Deliberately keyed on per-SLIP status, not the run's own status --
    hcm_payroll_runs.status flips to 'processed' the moment "Generate" is
    first clicked, but its individual hcm_salary_slips rows stay 'draft'
    (not yet actually paid out) until some later, separate disbursement
    step marks them 'paid'. A 'processed'-but-still-'draft' run (e.g. this
    month's just-generated, not-yet-paid run) is exactly the case that
    needs to stay live and connected -- only a run with a genuinely 'paid'
    slip is a real historical record, and that one is left alone,
    untouched, exactly as before."""
    # H-11/H-13: only generated, not-yet-locked runs (never a 'draft' run
    # nobody generated, never a locked/paid one) -- see payroll_period.
    from . import payroll_period
    return payroll_period.resync_runs_from(db, company_id, employee_id, effective_from)


_STATUTORY_PF_RATE = 0.12


def _var_annual_configured_monthly_amount(
    line: models.SalaryStructureLine, annual_ctc: float
) -> float:
    """The CONFIGURED monthly figure for a 'var_annual' component's line --
    used only to derive the annual entitlement ceiling (this * 12), never
    as an auto-paid amount (see _resolve_structure_lines_monthly, which
    always resolves a var_annual line's PAYABLE amount to 0 regardless of
    this). An admin may express this line as a flat ₹ or a %-of-CTC split
    (Payroll > Salary Structures > Edit Structure supports either) -- this
    resolves either shape the same way _resolve_structure_lines_monthly's
    own ctc_lines/flat_lines branches would, so the entitlement never
    silently reads as 0 just because the line happens to be percent-based
    today instead of flat."""
    if line.percent_of == "ctc" and line.percent is not None:
        return (annual_ctc / 12) * float(line.percent) / 100
    return float(line.amount or 0)


def _resolve_structure_lines_monthly(
    lines: list[models.SalaryStructureLine],
    annual_ctc: float,
    overrides: "dict[uuid.UUID, models.SalaryStructureAssignmentOverride] | None" = None,
    pf_wage_ceiling: float = 15000,
) -> list[dict]:
    """Resolves a salary structure's lines into monthly amounts, in
    dependency order: 'ctc'-percent lines first (computed off monthly
    CTC = annual_ctc / 12), then 'basic'-percent lines (computed off the
    FIRST 'ctc'-percent line resolved -- treated as this structure's
    Basic), then flat/statutory lines, then 'balance'-percent_of lines
    last -- at most one per structure (enforced in
    _replace_salary_structure_lines) -- as whatever's left of monthly CTC
    after every other EARNING line (deductions/benefits/tax don't shrink
    the earnings pool a balance line fills).

    A line whose component.calc_type == 'stat_pf' (Phase 2 of the
    redesign) ignores its own amount/percent_of/percent entirely -- the
    engine computes MIN(Basic, pf_wage_ceiling) * 12% itself, so PF is
    never a stale hand-typed rupee figure that's wrong for anyone whose
    Basic sits below the ceiling (or the ceiling itself changes).

    overrides, keyed by component_id, is this one employee's assignment-
    level overrides (Phase 1 of the redesign: deviate one person from a
    shared template without editing the template). When a line's
    component has an override, it replaces the line's own formula
    entirely (including a 'stat_pf' line) -- 'amount' is a literal
    monthly ₹, 'percent' is interpreted the same way the line itself
    would be (% of Basic if the line was a 'basic' or 'stat_pf' line, %
    of CTC/remaining-CTC otherwise).

    Also used by get_employee_salary_structure (the self-service display,
    which multiplies these monthly amounts by 12 to show annual figures)
    so an employee's "My Salary Structure" view and their actual payslip
    always agree on how HRA/PF/etc. were derived -- both call this one
    function rather than each doing their own percent-of resolution.

    Returns a list of {'line': SalaryStructureLine, 'component':
    SalaryComponent, 'amount': float}, in the order computed (ctc-percent,
    basic-percent, flat/statutory, then balance)."""
    monthly_ctc = annual_ctc / 12
    overrides = overrides or {}
    resolved: list[dict] = []
    basic_amount: float | None = None

    ctc_lines = [l for l in lines if l.percent_of == "ctc"]
    basic_lines = [l for l in lines if l.percent_of == "basic"]
    balance_lines = [l for l in lines if l.percent_of == "balance"]
    flat_lines = [l for l in lines if l.percent_of not in ("ctc", "basic", "balance")]

    def resolve_amount(line: models.SalaryStructureLine, natural_amount: float) -> float:
        override = overrides.get(line.component_id)
        if override is None:
            return natural_amount
        if override.override_type == "amount":
            return float(override.value)
        base = basic_amount if line.percent_of == "basic" or line.component.calc_type == "stat_pf" else monthly_ctc
        return (base or 0.0) * float(override.value) / 100

    for line in ctc_lines:
        natural = monthly_ctc * float(line.percent) / 100 if line.percent is not None else 0.0
        amount = resolve_amount(line, natural)
        resolved.append({"line": line, "component": line.component, "amount": amount})
        if basic_amount is None:
            basic_amount = amount

    for line in basic_lines:
        base = basic_amount or 0.0
        natural = base * float(line.percent) / 100 if line.percent is not None else 0.0
        amount = resolve_amount(line, natural)
        resolved.append({"line": line, "component": line.component, "amount": amount})

    for line in flat_lines:
        if line.component.calc_type == "stat_pf":
            natural = min(basic_amount or 0.0, pf_wage_ceiling) * _STATUTORY_PF_RATE
        elif line.component.calc_type == "var_annual":
            # Pure annual entitlement -- never auto-divided into a monthly
            # amount. _compute_and_write_slip injects the real amount (if
            # any) from a confirmed VariablePayPayout for this exact run,
            # after proration is applied to every other line, so this stays
            # 0 here regardless of an override (an override on a var_annual
            # line wouldn't make sense -- there's no "natural" monthly
            # figure to override).
            natural = 0.0
        else:
            natural = float(line.amount) if line.amount is not None else 0.0
        amount = resolve_amount(line, natural)
        resolved.append({"line": line, "component": line.component, "amount": amount})

    if balance_lines:
        earnings_so_far = sum(
            r["amount"] for r in resolved if r["component"].component_type == "earning"
        )
        remainder = max(0.0, monthly_ctc - earnings_so_far)
        for line in balance_lines:
            amount = resolve_amount(line, remainder)
            resolved.append({"line": line, "component": line.component, "amount": amount})

    # N-06: statutory employee ESI -- 0.75% of monthly gross, only while
    # gross is within the ESI wage ceiling (21,000).
    esi_items = [r for r in resolved if r["component"].calc_type == "stat_esi"
                 and r["line"].component_id not in overrides]
    if esi_items:
        gross = sum(r["amount"] for r in resolved if r["component"].component_type == "earning")
        for r in esi_items:
            r["amount"] = round(gross * 0.0075, 2) if 0 < gross <= 21000 else 0.0

    return resolved


def get_assignment_overrides_map(
    db: Session, assignment_id: uuid.UUID
) -> "dict[uuid.UUID, models.SalaryStructureAssignmentOverride]":
    overrides = db.scalars(
        select(models.SalaryStructureAssignmentOverride).where(
            models.SalaryStructureAssignmentOverride.assignment_id == assignment_id
        )
    ).all()
    return {o.component_id: o for o in overrides}


def list_assignment_overrides(
    db: Session, assignment_id: uuid.UUID
) -> list[models.SalaryStructureAssignmentOverride]:
    return list(
        db.scalars(
            select(models.SalaryStructureAssignmentOverride)
            .options(selectinload(models.SalaryStructureAssignmentOverride.component))
            .where(models.SalaryStructureAssignmentOverride.assignment_id == assignment_id)
        ).all()
    )


def set_assignment_override(
    db: Session,
    assignment_id: uuid.UUID,
    component_id: uuid.UUID,
    override_type: str,
    value: float,
    reason: str | None,
    created_by: uuid.UUID | None,
) -> models.SalaryStructureAssignmentOverride:
    """Create or replace (one override per component per assignment --
    UNIQUE (assignment_id, component_id) at the DB level) this employee's
    override for one component on one assignment."""
    existing = db.scalar(
        select(models.SalaryStructureAssignmentOverride).where(
            models.SalaryStructureAssignmentOverride.assignment_id == assignment_id,
            models.SalaryStructureAssignmentOverride.component_id == component_id,
        )
    )
    if existing is not None:
        existing.override_type = override_type
        existing.value = value
        existing.reason = reason
        # When it last changed -- arrears for locked periods key on it (H-12).
        existing.created_at = datetime.datetime.now(datetime.timezone.utc)
        db.flush()
        return existing
    override = models.SalaryStructureAssignmentOverride(
        id=uuid.uuid4(),
        assignment_id=assignment_id,
        component_id=component_id,
        override_type=override_type,
        value=value,
        reason=reason,
        created_by=created_by,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(override)
    db.flush()
    return override


def delete_assignment_override(
    db: Session, assignment_id: uuid.UUID, component_id: uuid.UUID
) -> bool:
    existing = db.scalar(
        select(models.SalaryStructureAssignmentOverride).where(
            models.SalaryStructureAssignmentOverride.assignment_id == assignment_id,
            models.SalaryStructureAssignmentOverride.component_id == component_id,
        )
    )
    if existing is None:
        return False
    db.delete(existing)
    db.flush()
    return True


# Auto-TDS (component code TDS-EST, "Income Tax (TDS)") is computed by
# payroll_period._compute_tds with the statutory engine in tax_engine.py.
def _estimate_annual_tds(taxable_annual: float, regime: str | None = None) -> float:
    """M-30: statutory annual tax (app/tax_engine.py) on an annual gross
    salary -- kept for callers of the old estimator."""
    from . import tax_engine
    return tax_engine.compute_annual_tax(
        tax_engine.TaxInputs(regime=tax_engine.normalise_regime(regime), gross_salary=taxable_annual)
    ).total_tax


def get_or_create_tds_component(db: Session, company_id: uuid.UUID) -> models.SalaryComponent:
    existing = db.scalar(
        select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == company_id,
            models.SalaryComponent.code == "TDS-EST",
        )
    )
    if existing is not None:
        return existing
    return create_salary_component(
        db, company_id, "Income Tax (TDS)", "TDS-EST", "tax", "flat", True,
    )


def _ensure_lop_deduction_component(db: Session, company_id: uuid.UUID) -> models.SalaryComponent:
    """Payroll Settings > Deductions Configuration's LOP deduction line --
    get-or-create, same convention as get_or_create_tds_component, so it's
    a real, stable SalaryComponent every LOP-deduction SalarySlipLine can
    point at (never fabricated per-slip)."""
    existing = db.scalar(
        select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == company_id,
            models.SalaryComponent.code == "LOP-DED",
        )
    )
    if existing is not None:
        return existing
    return create_salary_component(
        db, company_id, "Loss of Pay (LOP)", "LOP-DED", "deduction", "flat", False,
    )


def _ensure_asset_deduction_component(db: Session, company_id: uuid.UUID) -> models.SalaryComponent:
    """Payroll Settings > Deductions Configuration's Asset Recovery
    deduction line -- get-or-create, same convention as
    get_or_create_tds_component."""
    existing = db.scalar(
        select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == company_id,
            models.SalaryComponent.code == "ASSET-DED",
        )
    )
    if existing is not None:
        return existing
    return create_salary_component(
        db, company_id, "Asset Recovery Deduction", "ASSET-DED", "deduction", "flat", False,
    )


def _compute_approved_lop_days(
    db: Session,
    employee_id: uuid.UUID,
    from_date: datetime.date,
    to_date: datetime.date,
) -> float:
    """Payroll Settings > Deductions Configuration's LOP deduction --
    real approved Loss-of-Pay LEAVE REQUEST days overlapping [from_date,
    to_date] (a payroll run's period), joined via LeaveType.code == 'LOP'.
    Deliberately independent of _compute_working_days_and_lop's
    attendance-'absent'-derived lop_days (a different, already-existing
    signal used to prorate every earning line) -- an employee on APPROVED
    LOP leave is not necessarily marked 'absent' in hcm_attendance_records
    at all (they filed a real leave request; attendance wouldn't
    separately flag those same days unexplained-absent), so this is
    additive, not a double-count of the same days.

    A request entirely within the run's range contributes its own `days`
    figure as-is (preserving half-day granularity, e.g. 0.5). A request
    that only partially overlaps the run's range (crosses a month
    boundary) is prorated by the fraction of its calendar span that falls
    inside the run's range -- `days` is one total for the whole request,
    not stored per-day."""
    rows = db.execute(
        select(models.LeaveRequest)
        .join(models.LeaveType, models.LeaveType.id == models.LeaveRequest.leave_type_id)
        .where(
            models.LeaveRequest.employee_id == employee_id,
            models.LeaveRequest.status == "approved",
            models.LeaveType.code == "LOP",
            models.LeaveRequest.from_date <= to_date,
            models.LeaveRequest.to_date >= from_date,
        )
    ).scalars().all()
    total = 0.0
    for req in rows:
        request_span_days = (req.to_date - req.from_date).days + 1
        overlap_start = max(req.from_date, from_date)
        overlap_end = min(req.to_date, to_date)
        overlap_span_days = (overlap_end - overlap_start).days + 1
        if overlap_span_days >= request_span_days:
            total += float(req.days)
        elif request_span_days > 0:
            total += float(req.days) * (overlap_span_days / request_span_days)
    return total


def get_approved_asset_recovery_amount(
    db: Session,
    employee_id: uuid.UUID,
    period_month: int,
    period_year: int,
    payroll_run_id: uuid.UUID,
) -> list[models.AssetRecoveryDeduction]:
    """Payroll Settings > Deductions Configuration's Asset deduction --
    every APPROVED, not-yet-applied-to-a-DIFFERENT-run recovery targeted
    at this employee's [period_month]/[period_year]. Matching
    applied_payroll_run_id == payroll_run_id too (not just IS NULL) is
    what keeps regenerating a still-draft run idempotent: a recovery this
    exact run already consumed on a prior generate stays eligible so it's
    picked up again, while one a *different* (e.g. an earlier month's)
    run already consumed is correctly excluded."""
    return db.scalars(
        select(models.AssetRecoveryDeduction).where(
            models.AssetRecoveryDeduction.employee_id == employee_id,
            models.AssetRecoveryDeduction.status == "approved",
            models.AssetRecoveryDeduction.target_period_month == period_month,
            models.AssetRecoveryDeduction.target_period_year == period_year,
            or_(
                models.AssetRecoveryDeduction.applied_payroll_run_id.is_(None),
                models.AssetRecoveryDeduction.applied_payroll_run_id == payroll_run_id,
            ),
        )
    ).all()


def get_eligible_reimbursement_inclusions(
    db: Session,
    employee_id: uuid.UUID,
    period_month: int,
    period_year: int,
    payroll_run_id: uuid.UUID,
    enabled_source_types: "set[str] | None" = None,
) -> list[models.PayrollReimbursementInclusion]:
    """Payroll > Travel & Expense Reimbursements -- every INCLUDED
    inclusion scheduled for this employee's [period_month]/[period_year]
    that hasn't already been consumed by a DIFFERENT payroll run. Matching
    applied_payroll_run_id == payroll_run_id too (not just IS NULL) is
    what keeps regenerating a still-draft run idempotent -- same pattern
    as get_approved_asset_recovery_amount. HR-excluded rows, and rows whose
    Reimbursement Component is disabled ([enabled_source_types], None =
    no restriction), are never paid."""
    q = select(models.PayrollReimbursementInclusion).where(
        models.PayrollReimbursementInclusion.employee_id == employee_id,
        models.PayrollReimbursementInclusion.target_period_month == period_month,
        models.PayrollReimbursementInclusion.target_period_year == period_year,
        models.PayrollReimbursementInclusion.status == "included",
        or_(
            models.PayrollReimbursementInclusion.applied_payroll_run_id.is_(None),
            models.PayrollReimbursementInclusion.applied_payroll_run_id == payroll_run_id,
        ),
    )
    if enabled_source_types is not None:
        if not enabled_source_types:
            return []
        q = q.where(models.PayrollReimbursementInclusion.source_type.in_(enabled_source_types))
    return db.scalars(q.order_by(models.PayrollReimbursementInclusion.created_at)).all()


# ── Payroll > Salary Structure > Reimbursement Components ────────────────────

REIMBURSEMENT_COMPONENT_DEFAULT_NAMES = {
    "travel_request": "Travel Reimbursement",
    "expense_claim": "Expense Reimbursement",
}


def get_reimbursement_components(db: Session, company_id: uuid.UUID) -> dict[str, dict]:
    """Both Reimbursement Components for this company, keyed by source_type.
    A type HR hasn't configured yet uses the built-in default -- enabled,
    not auto-included -- which is exactly how payroll behaved before these
    settings existed (only records HR schedules by hand are paid)."""
    rows = {
        r.source_type: r
        for r in db.scalars(
            select(models.PayrollReimbursementComponent).where(
                models.PayrollReimbursementComponent.company_id == company_id
            )
        )
    }
    out: dict[str, dict] = {}
    for source_type, default_name in REIMBURSEMENT_COMPONENT_DEFAULT_NAMES.items():
        r = rows.get(source_type)
        out[source_type] = {
            "source_type": source_type,
            "display_name": r.display_name if r else default_name,
            "is_enabled": bool(r.is_enabled) if r else True,
            "auto_include": bool(r.auto_include) if r else False,
            "max_amount_per_request": (
                float(r.max_amount_per_request)
                if r is not None and r.max_amount_per_request is not None
                else None
            ),
            "configured": r is not None,
            "updated_at": r.updated_at if r else None,
        }
    return out


def update_reimbursement_component(
    db: Session,
    company_id: uuid.UUID,
    source_type: str,
    updates: dict,
    actor_id: uuid.UUID | None,
) -> tuple[dict, dict]:
    """Creates (first save) or updates this company's configuration row for
    [source_type]. [updates] holds only the fields the caller sent (an
    explicit None max_amount_per_request clears the cap). Returns
    (before, after) for the audit trail."""
    before = get_reimbursement_components(db, company_id)[source_type]
    now = datetime.datetime.now(datetime.timezone.utc)
    row = db.scalar(
        select(models.PayrollReimbursementComponent).where(
            models.PayrollReimbursementComponent.company_id == company_id,
            models.PayrollReimbursementComponent.source_type == source_type,
        )
    )
    if row is None:
        row = models.PayrollReimbursementComponent(
            id=uuid.uuid4(),
            company_id=company_id,
            source_type=source_type,
            display_name=before["display_name"],
            is_enabled=before["is_enabled"],
            auto_include=before["auto_include"],
            max_amount_per_request=before["max_amount_per_request"],
            created_by=actor_id,
            created_at=now,
        )
        db.add(row)
    for key in ("display_name", "is_enabled", "auto_include", "max_amount_per_request"):
        if key in updates:
            value = updates[key]
            if key == "display_name":
                value = (value or "").strip() or before["display_name"]
            setattr(row, key, value)
    row.updated_by = actor_id
    row.updated_at = now
    db.flush()
    after = get_reimbursement_components(db, company_id)[source_type]
    return before, after


def _reimbursement_source_amount(source_type: str, row) -> float:
    """The real approved amount of a Travel Requisition (its estimated cost,
    the figure its approvers signed off) or Expense Report/Reimbursement."""
    if source_type == "travel_request":
        return float(row.estimated_cost or 0)
    return float(row.total_amount or 0)


def _reimbursement_reference(source_type: str, row) -> str:
    """The request/report number shown on the payroll screens and payslip."""
    if source_type == "travel_request":
        return f"TRV-{row.id.hex[:8].upper()}"
    return row.claim_no or f"EXP-{row.id.hex[:8].upper()}"


def identify_reimbursements_for_period(
    db: Session,
    company_id: uuid.UUID,
    period_month: int,
    period_year: int,
    actor_id: uuid.UUID | None = None,
) -> int:
    """Automatic identification for a payroll period: for every
    Reimbursement Component that is enabled AND auto_include, schedules each
    fully-approved (status='approved') record of that type -- approved on or
    before the period's last day, belonging to an active employee of this
    company, and never scheduled before -- into [period_month]/[period_year]
    (capped at max_amount_per_request when set). Records already scheduled
    (in any period, included or excluded) are left alone -- the UNIQUE
    (source_type, source_id) guard, so nothing is ever paid twice. Returns
    how many were newly scheduled."""
    components = get_reimbursement_components(db, company_id)
    wanted = [c for c in components.values() if c["is_enabled"] and c["auto_include"]]
    if not wanted:
        return 0
    last_day = calendar.monthrange(period_year, period_month)[1]
    period_end = datetime.datetime.combine(
        datetime.date(period_year, period_month, last_day),
        datetime.time(23, 59, 59),
        tzinfo=company_tzinfo(db, company_id),
    )
    already = {
        (st, sid)
        for st, sid in db.execute(
            select(
                models.PayrollReimbursementInclusion.source_type,
                models.PayrollReimbursementInclusion.source_id,
            ).where(models.PayrollReimbursementInclusion.company_id == company_id)
        ).all()
    }
    now = datetime.datetime.now(datetime.timezone.utc)
    created = 0
    for component in wanted:
        source_type = component["source_type"]
        if source_type == "travel_request":
            rows = db.scalars(
                select(models.TravelRequestModel)
                .join(models.Employee, models.Employee.id == models.TravelRequestModel.employee_id)
                .where(
                    models.Employee.company_id == company_id,
                    models.Employee.is_active.is_(True),
                    models.TravelRequestModel.status == "approved",
                )
            ).all()
        else:
            rows = db.scalars(
                select(models.ExpenseClaim)
                .join(models.Employee, models.Employee.id == models.ExpenseClaim.employee_id)
                .where(
                    models.ExpenseClaim.company_id == company_id,
                    models.Employee.company_id == company_id,
                    models.Employee.is_active.is_(True),
                    models.ExpenseClaim.status == "approved",
                )
            ).all()
        cap = component["max_amount_per_request"]
        for row in rows:
            if (source_type, row.id) in already:
                continue
            decided_at = getattr(row, "decided_at", None)
            if decided_at is not None and decided_at > period_end:
                continue  # approved after this period -- belongs to a later run
            approved = _reimbursement_source_amount(source_type, row)
            if approved <= 0:
                continue
            db.add(models.PayrollReimbursementInclusion(
                id=uuid.uuid4(),
                company_id=company_id,
                employee_id=row.employee_id,
                source_type=source_type,
                source_id=row.id,
                amount=min(approved, cap) if cap else approved,
                approved_amount=approved,
                target_period_month=period_month,
                target_period_year=period_year,
                status="included",
                auto_included=True,
                created_by=actor_id,
                created_at=now,
            ))
            already.add((source_type, row.id))
            created += 1
    db.flush()
    return created


def reimbursement_payroll_statuses(
    db: Session, company_id: uuid.UUID, source_type: str, rows
) -> dict[uuid.UUID, str]:
    """Travel & Expense screens: for each APPROVED record in [rows], where
    it stands in payroll -- "Paid · <month>", "In payroll · <month>"
    (on a generated, unpaid payslip), "Scheduled · <month>", "Excluded from
    payroll", or, not scheduled yet, "Awaiting payroll" (its component
    auto-includes it at the next run) / "Not scheduled". One bulk query."""
    approved = [r for r in rows if r.status == "approved"]
    if not approved:
        return {}
    inclusions = {
        i.source_id: i
        for i in db.scalars(
            select(models.PayrollReimbursementInclusion).where(
                models.PayrollReimbursementInclusion.company_id == company_id,
                models.PayrollReimbursementInclusion.source_type == source_type,
                models.PayrollReimbursementInclusion.source_id.in_([r.id for r in approved]),
            )
        )
    }
    paid_pairs = set()
    run_ids = {i.applied_payroll_run_id for i in inclusions.values() if i.applied_payroll_run_id}
    if run_ids:
        paid_pairs = set(
            db.execute(
                select(models.SalarySlip.payroll_run_id, models.SalarySlip.employee_id).where(
                    models.SalarySlip.payroll_run_id.in_(run_ids),
                    models.SalarySlip.status == "paid",
                )
            ).all()
        )
    component = get_reimbursement_components(db, company_id)[source_type]
    out: dict[uuid.UUID, str] = {}
    for r in approved:
        i = inclusions.get(r.id)
        if i is None:
            out[r.id] = (
                "Awaiting payroll"
                if component["is_enabled"] and component["auto_include"]
                else "Not scheduled"
            )
            continue
        period = f"{calendar.month_name[i.target_period_month]} {i.target_period_year}"
        if i.status == "excluded":
            out[r.id] = "Excluded from payroll"
        elif i.applied_payroll_run_id and (i.applied_payroll_run_id, i.employee_id) in paid_pairs:
            out[r.id] = f"Paid · {period}"
        elif i.applied_payroll_run_id:
            out[r.id] = f"In payroll · {period}"
        else:
            out[r.id] = f"Scheduled · {period}"
    return out


def _inclusion_snapshot(inclusion: models.PayrollReimbursementInclusion) -> dict:
    return {
        "amount": f"{float(inclusion.amount):.2f}",
        "status": inclusion.status,
        "target_period": f"{inclusion.target_period_year}-{inclusion.target_period_month:02d}",
        "notes": inclusion.notes or "",
    }


def _inclusion_is_paid(db: Session, inclusion: models.PayrollReimbursementInclusion) -> bool:
    if inclusion.applied_payroll_run_id is None:
        return False
    applied_run = db.get(models.PayrollRun, inclusion.applied_payroll_run_id)
    if applied_run is not None and applied_run.status in ("locked", "paid"):
        return True  # H-11: a locked period's payslip can't change
    return (
        db.scalar(
            select(models.SalarySlip.id).where(
                models.SalarySlip.payroll_run_id == inclusion.applied_payroll_run_id,
                models.SalarySlip.employee_id == inclusion.employee_id,
                models.SalarySlip.status == "paid",
            )
        )
        is not None
    )


def _resync_unpaid_runs_for_employee(
    db: Session, company_id: uuid.UUID, run_ids: set[uuid.UUID], employee_id: uuid.UUID
) -> None:
    """Recomputes [employee_id]'s slip in each of these already-generated
    runs whose slip for them isn't paid, so a reviewed reimbursement shows
    (or disappears) on the draft payslip at once. Runs never generated for
    this employee are left for their own Generate."""
    for run_id in run_ids:
        run = db.get(models.PayrollRun, run_id)
        if run is None or run.company_id != company_id:
            continue
        slip = db.scalar(
            select(models.SalarySlip).where(
                models.SalarySlip.payroll_run_id == run.id,
                models.SalarySlip.employee_id == employee_id,
            )
        )
        if slip is None or slip.status == "paid":
            continue
        from . import payroll_period
        payroll_period.resync_employee(db, company_id, run, employee_id)  # skips locked runs


def update_reimbursement_inclusion(
    db: Session,
    company_id: uuid.UUID,
    inclusion_id: uuid.UUID,
    updates: dict,
    actor_id: uuid.UUID | None,
):
    """Review before payout: edit amount (0 < amount <= approved amount),
    include/exclude, defer (new target period) and/or note. Returns None
    (not found), "locked" (already paid), "amount_exceeds", or
    (inclusion, before, after). A row already on a generated-but-unpaid
    slip is released from it, and every affected unpaid slip (the old run
    and any generated run of the target period) is recomputed."""
    inclusion = db.get(models.PayrollReimbursementInclusion, inclusion_id)
    if inclusion is None or inclusion.company_id != company_id:
        return None
    if _inclusion_is_paid(db, inclusion):
        return "locked"
    approved = float(inclusion.approved_amount if inclusion.approved_amount is not None else inclusion.amount)
    if "amount" in updates and updates["amount"] is not None and float(updates["amount"]) > approved + 0.005:
        return "amount_exceeds"
    before = _inclusion_snapshot(inclusion)
    if updates.get("amount") is not None:
        inclusion.amount = round(float(updates["amount"]), 2)
    if updates.get("status") is not None:
        inclusion.status = updates["status"]
    if updates.get("target_period_month") is not None:
        inclusion.target_period_month = updates["target_period_month"]
        inclusion.target_period_year = updates["target_period_year"]
    if "notes" in updates:
        inclusion.notes = (updates["notes"] or "").strip() or None
    after = _inclusion_snapshot(inclusion)
    inclusion.updated_by = actor_id
    inclusion.updated_at = datetime.datetime.now(datetime.timezone.utc)

    affected_runs: set[uuid.UUID] = set()
    if inclusion.applied_payroll_run_id is not None:
        affected_runs.add(inclusion.applied_payroll_run_id)
        inclusion.applied_payroll_run_id = None
        inclusion.applied_at = None
    affected_runs.update(
        db.scalars(
            select(models.PayrollRun.id).where(
                models.PayrollRun.company_id == company_id,
                models.PayrollRun.period_month == inclusion.target_period_month,
                models.PayrollRun.period_year == inclusion.target_period_year,
            )
        ).all()
    )
    db.flush()
    _resync_unpaid_runs_for_employee(db, company_id, affected_runs, inclusion.employee_id)
    db.flush()
    return inclusion, before, after


def _reimbursement_source_row(
    db: Session, company_id: uuid.UUID, source_type: str, source_id: uuid.UUID
):
    """Fetches the real hcm_travel_requests or hcm_expense_claims row
    [source_id] refers to, tenant-scoped. TravelRequestModel has no
    company_id column of its own -- scoped via its employee's company_id
    instead, same join every other travel-request lookup in this codebase
    already uses."""
    if source_type == "travel_request":
        row = db.get(models.TravelRequestModel, source_id)
        if row is None:
            return None
        employee = db.get(models.Employee, row.employee_id)
        if employee is None or employee.company_id != company_id:
            return None
        return row
    if source_type == "expense_claim":
        row = db.get(models.ExpenseClaim, source_id)
        if row is None or row.company_id != company_id:
            return None
        return row
    return None


def create_payroll_reimbursement_inclusion(
    db: Session,
    company_id: uuid.UUID,
    source_type: str,
    source_id: uuid.UUID,
    target_period_month: int,
    target_period_year: int,
    created_by: uuid.UUID,
) -> "models.PayrollReimbursementInclusion | None | str":
    """Payroll > Travel & Expense Reimbursements -- HR schedules one
    already-fully-approved record into a payroll period. Returns None if
    the source record doesn't exist/isn't in this company; the string
    "not_approved" if it exists but isn't status=='approved' yet; the
    string "already_scheduled" if it's already been scheduled once
    before (the UNIQUE (source_type, source_id) constraint's friendly
    pre-check); otherwise the new row, with [amount] captured from the
    source's own real approved amount at scheduling time."""
    row = _reimbursement_source_row(db, company_id, source_type, source_id)
    if row is None:
        return None
    if row.status != "approved":
        return "not_approved"
    already = db.scalar(
        select(models.PayrollReimbursementInclusion).where(
            models.PayrollReimbursementInclusion.source_type == source_type,
            models.PayrollReimbursementInclusion.source_id == source_id,
        )
    )
    if already is not None:
        return "already_scheduled"
    amount = _reimbursement_source_amount(source_type, row)
    inclusion = models.PayrollReimbursementInclusion(
        id=uuid.uuid4(),
        company_id=company_id,
        employee_id=row.employee_id,
        source_type=source_type,
        source_id=source_id,
        amount=amount,
        approved_amount=amount,
        target_period_month=target_period_month,
        target_period_year=target_period_year,
        status="included",
        auto_included=False,
        created_by=created_by,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(inclusion)
    db.flush()
    return inclusion


def _reimbursement_type_label(source_type: str, claim_no: str | None = None) -> str:
    if source_type == "travel_request":
        return "Travel Reimbursement"
    # Reimbursements and Expense Reports share hcm.expense_claims,
    # distinguished only by claim_no prefix (see create_reimbursement vs
    # create_expense_report) -- both are genuinely eligible reimbursable
    # amounts, so both are listed here; the label just reflects which
    # flow actually created the real row.
    return "Reimbursement" if (claim_no or "").startswith("REIM-") else "Expense Report"


def list_payroll_reimbursement_items(db: Session, company_id: uuid.UUID) -> list[dict]:
    """Payroll > Travel & Expense Reimbursements -- every FULLY approved
    (status='approved') Travel Requisition and Expense Report/
    Reimbursement for this company, whether or not HR has scheduled it
    into a payroll period yet. Real data only: never fabricates a
    schedule/payment state for a record HR hasn't acted on."""
    items: list[dict] = []

    travel_rows = db.execute(
        select(models.TravelRequestModel, models.Employee)
        .join(models.Employee, models.Employee.id == models.TravelRequestModel.employee_id)
        .where(models.Employee.company_id == company_id, models.TravelRequestModel.status == "approved")
    ).all()
    for travel, employee in travel_rows:
        items.append({
            "source_type": "travel_request",
            "source_id": travel.id,
            "employee_id": travel.employee_id,
            "employee_name": _full_name(employee),
            "request_code": f"TRV-{travel.id.hex[:8].upper()}",
            "type_label": _reimbursement_type_label("travel_request"),
            "approved_amount": float(travel.estimated_cost or 0),
            "approval_date": travel.decided_at,
        })

    expense_rows = db.scalars(
        select(models.ExpenseClaim).where(
            models.ExpenseClaim.company_id == company_id, models.ExpenseClaim.status == "approved"
        )
    ).all()
    expense_employee_ids = {e.employee_id for e in expense_rows}
    expense_employees = {
        e.id: e
        for e in (
            db.scalars(select(models.Employee).where(models.Employee.id.in_(expense_employee_ids)))
            if expense_employee_ids
            else []
        )
    }
    for claim in expense_rows:
        items.append({
            "source_type": "expense_claim",
            "source_id": claim.id,
            "employee_id": claim.employee_id,
            "employee_name": _full_name(expense_employees.get(claim.employee_id)),
            "request_code": claim.claim_no,
            "type_label": _reimbursement_type_label("expense_claim", claim.claim_no),
            "approved_amount": float(claim.total_amount or 0),
            "approval_date": claim.decided_at,
        })

    # One bulk lookup of every existing inclusion for these exact
    # (source_type, source_id) pairs, instead of one query per item.
    source_ids = [item["source_id"] for item in items]
    inclusions_by_source: dict[tuple[str, uuid.UUID], models.PayrollReimbursementInclusion] = {}
    if source_ids:
        for inclusion in db.scalars(
            select(models.PayrollReimbursementInclusion).where(
                models.PayrollReimbursementInclusion.company_id == company_id,
                models.PayrollReimbursementInclusion.source_id.in_(source_ids),
            )
        ):
            inclusions_by_source[(inclusion.source_type, inclusion.source_id)] = inclusion

    applied_run_ids = {
        i.applied_payroll_run_id for i in inclusions_by_source.values() if i.applied_payroll_run_id
    }
    paid_run_employee_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    if applied_run_ids:
        for run_id, emp_id in db.execute(
            select(models.SalarySlip.payroll_run_id, models.SalarySlip.employee_id).where(
                models.SalarySlip.payroll_run_id.in_(applied_run_ids),
                models.SalarySlip.status == "paid",
            )
        ).all():
            paid_run_employee_pairs.add((run_id, emp_id))

    components = get_reimbursement_components(db, company_id)
    for item in items:
        inclusion = inclusions_by_source.get((item["source_type"], item["source_id"]))
        if inclusion is None:
            item.update(
                target_period_month=None, target_period_year=None,
                payment_status="Not Scheduled", inclusion_id=None, applied_payroll_run_id=None,
            )
            continue
        review = {
            "payroll_amount": float(inclusion.amount),
            "inclusion_status": inclusion.status,
            "auto_included": bool(inclusion.auto_included),
            "notes": inclusion.notes,
            "editable": True,
        }
        if inclusion.applied_payroll_run_id is None:
            if inclusion.status == "excluded":
                status = "Excluded"
            elif not components[inclusion.source_type]["is_enabled"]:
                status = "On Hold"  # its Reimbursement Component is disabled
            else:
                status = "Scheduled"
            item.update(
                target_period_month=inclusion.target_period_month,
                target_period_year=inclusion.target_period_year,
                payment_status=status, inclusion_id=inclusion.id, applied_payroll_run_id=None,
                **review,
            )
        else:
            is_paid = (inclusion.applied_payroll_run_id, item["employee_id"]) in paid_run_employee_pairs
            review["editable"] = not is_paid
            item.update(
                target_period_month=inclusion.target_period_month,
                target_period_year=inclusion.target_period_year,
                payment_status="Paid" if is_paid else "Included in Payroll",
                inclusion_id=inclusion.id,
                applied_payroll_run_id=inclusion.applied_payroll_run_id,
                **review,
            )
    items.sort(key=lambda i: i["approval_date"] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)
    return items


def _compute_working_days_and_lop(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    from_date: datetime.date,
    to_date: datetime.date,
) -> tuple[float, float]:
    """working_days: calendar working days in [from_date, to_date] per this
    company's configured working_days_per_week, minus company-wide or this
    employee's branch's holidays in range. lop_days: this employee's real
    hcm_attendance_records rows in range with status='absent' -- a
    genuine, not fabricated, LOP source; days simply never punched
    in/out (no attendance row at all) are NOT counted as LOP here, only
    explicit 'absent' status is, matching what 'absent' already means
    elsewhere in this app (see attendance.py)."""
    working_days_per_week = (
        db.scalar(
            select(models.CompanySettings.working_days_per_week).where(
                models.CompanySettings.company_id == company_id
            )
        )
        or 5
    )
    holiday_dates = set(
        db.scalars(
            select(models.Holiday.holiday_date).where(
                models.Holiday.company_id == company_id,
                models.Holiday.holiday_date >= from_date,
                models.Holiday.holiday_date <= to_date,
                or_(models.Holiday.branch_id.is_(None), models.Holiday.branch_id == branch_id),
            )
        ).all()
    )
    working_days = 0
    d = from_date
    one_day = datetime.timedelta(days=1)
    while d <= to_date:
        # 6-day week: only Sunday (weekday 6) is off. Otherwise (5-day
        # week, this company's default): Saturday+Sunday are both off.
        is_weekend = d.weekday() == 6 if working_days_per_week >= 6 else d.weekday() >= 5
        if not is_weekend and d not in holiday_dates:
            working_days += 1
        d += one_day
    lop_days = (
        db.scalar(
            select(func.count(models.AttendanceRecord.id)).where(
                models.AttendanceRecord.employee_id == employee_id,
                models.AttendanceRecord.attendance_date >= from_date,
                models.AttendanceRecord.attendance_date <= to_date,
                models.AttendanceRecord.status == "absent",
            )
        )
        or 0
    )
    return float(working_days), float(lop_days)


def run_has_paid_slip(db: Session, payroll_run_id: uuid.UUID) -> bool:
    """True once at least one of this run's slips has been marked 'paid' --
    the same "genuinely paid, therefore untouchable" test
    resync_draft_payroll_runs already uses (see its own docstring). Callers
    use this to decide whether a run may still be regenerated: a
    'processed'-but-nothing-paid-yet run is safe to re-run (e.g. after a
    salary-structure correction lands, or simply to re-check figures) --
    generate_payroll_slips deletes and rewrites every slip on the run, so a
    real 'paid' slip must never be exposed to that regardless of the run's
    own 'draft'/'processed' status, which flips to 'processed' the moment
    Generate is first clicked and says nothing about whether anyone's
    actually been paid yet."""
    return (
        db.scalar(
            select(models.SalarySlip.id)
            .where(
                models.SalarySlip.payroll_run_id == payroll_run_id,
                models.SalarySlip.status == "paid",
            )
            .limit(1)
        )
        is not None
    )


def generate_payroll_slips(
    db: Session, company_id: uuid.UUID, payroll_run_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> list[models.SalarySlip]:
    """Payroll > Run Payroll's "Process"/"Generate" action: one slip per
    employee with a salary-structure assignment in force during the
    payable window (joining date .. last working day), prorated by working
    days and attendance LOP (H-14). An employee with no assignment is
    skipped, not an error.

    If the structure has no non-zero 'tax' line and auto-TDS is on, the
    statutory TDS for the employee's regime is added (M-30).

    Idempotent and in place: re-running an open run updates each slip
    (its id stays stable, H-12); a locked / paid run is refused (H-11).

    Payroll Settings > Deductions Configuration (both off by default, see
    CompanySettings.lop_deduction_enabled/asset_deduction_enabled): when
    on, _compute_and_write_slip additionally appends a real, separate "Loss
    of Pay (LOP)" deduction line computed from this employee's APPROVED LOP
    leave requests overlapping the run's period (independent of the
    attendance-'absent'-derived lop_days above -- see
    _compute_approved_lop_days), and/or an "Asset Recovery Deduction" line
    for any approved AssetRecoveryDeduction rows targeted at this exact
    period (see get_approved_asset_recovery_amount)."""
    # QA H-11..H-16 / M-30 / M-36: the batch engine in app/payroll_period.py
    # (in-place slips with stable ids, proration, statutory TDS, loan EMIs,
    # arrears, net >= 0). Raises payroll_period.PayrollStateError (a
    # ValueError) for a locked / paid run.
    from . import payroll_period
    run = db.get(models.PayrollRun, payroll_run_id)
    if run is None or run.company_id != company_id:
        raise ValueError("Payroll run not found")
    return payroll_period.generate_run(db, company_id, run, actor_id)


def _compute_and_write_slip(
    db: Session,
    company_id: uuid.UUID,
    run: models.PayrollRun,
    employee: models.Employee,
    pf_wage_ceiling: float,
    auto_tds_estimate_enabled: bool = False,
) -> models.SalarySlip | None:
    """One employee's slip on [run] -- now a thin wrapper over the batch
    engine (app/payroll_period.py), recomputing that employee in place.
    pf_wage_ceiling / auto_tds_estimate_enabled are read from company
    settings by the engine (kept for call compatibility)."""
    from . import payroll_period
    slips = payroll_period.generate_run(db, company_id, run, employee_ids={employee.id})
    return slips[0] if slips else None


_PF_ESI_NAME_RE = re.compile(r"provident\s*fund|\bepf\b|\besic?\b|employees?'?\s*state\s*insurance")


def _is_statutory_pf_esi(component: models.SalaryComponent) -> bool:
    """Statutory PF / ESI deduction (or employer-contribution benefit) line:
    the engine's 'stat_pf' calc type, or a PF / EPF / ESI / ESIC code or
    name. Earnings are never matched."""
    if component.component_type not in ("deduction", "benefit"):
        return False
    if component.calc_type == "stat_pf":
        return True
    code = (component.code or "").upper().replace(" ", "").replace("_", "-")
    if code in ("PF", "EPF", "ESI", "ESIC") or code.startswith(("PF-", "EPF-", "ESI-", "ESIC-")):
        return True
    return bool(_PF_ESI_NAME_RE.search((component.name or "").lower()))


def _is_pf_deduction(component: models.SalaryComponent) -> bool:
    """Employee PF deduction line -- statutory PF/ESI minus ESI, so seeded
    "Provident Fund (Employee)" / EPF codes count, not only a component
    named exactly "Provident Fund"."""
    if component.component_type != "deduction" or not _is_statutory_pf_esi(component):
        return False
    if component.calc_type == "stat_esi":
        return False
    code = (component.code or "").upper()
    name = (component.name or "").lower()
    return not (code.startswith("ESI") or "esi" in name.split() or "state insurance" in name)


def contract_payable_units(
    db: Session, employee_id: uuid.UUID, unit: str, from_date: datetime.date, to_date: datetime.date
) -> float:
    """Hourly / daily contractors: approved Work & Timesheet time in the
    period -- hours on approved timesheets (rejected entries excluded), or
    the number of distinct days with approved hours."""
    q = (
        select(models.WorkEntry.entry_date, func.sum(models.WorkEntry.hours))
        .join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
        .where(
            models.Timesheet.employee_id == employee_id,
            func.lower(models.Timesheet.status) == "approved",
            func.lower(func.coalesce(models.WorkEntry.status, "")) != "rejected",
            models.WorkEntry.entry_date >= from_date,
            models.WorkEntry.entry_date <= to_date,
        )
        .group_by(models.WorkEntry.entry_date)
    )
    per_day = [float(h or 0) for _d, h in db.execute(q).all()]
    if unit == "hourly":
        return round(sum(per_day), 2)
    return float(sum(1 for h in per_day if h > 0))


def _ensure_contract_fee_component(db: Session, company_id: uuid.UUID) -> models.SalaryComponent:
    component = db.scalar(
        select(models.SalaryComponent).where(
            models.SalaryComponent.company_id == company_id, models.SalaryComponent.code == "CONTRACT-FEE"
        )
    )
    if component is None:
        component = create_salary_component(db, company_id, "Contract Fee", "CONTRACT-FEE", "earning", "flat", True)
    return component


def _compute_and_write_contract_rate_slip(
    db: Session, company_id: uuid.UUID, run: models.PayrollRun, employee: models.Employee
) -> models.SalarySlip:
    """Hourly / daily contract employees: pay = approved timesheet hours /
    days in the run's period x their contract rate, replacing the salary
    structure's CTC / 12. No statutory PF / ESI, no attendance LOP and no
    overtime pay (they are paid for the time they log); approved asset
    recoveries and reimbursements still apply, exactly as for everyone."""
    # Computed by the batch engine (payroll_period._contract_part).
    return _compute_and_write_slip(db, company_id, run, employee, 0.0)


def resync_employee_slip_in_run(
    db: Session, company_id: uuid.UUID, run: models.PayrollRun, employee_id: uuid.UUID
) -> models.SalarySlip | None:
    """Regenerates just ONE employee's slip within an already-existing run
    -- the per-employee counterpart to generate_payroll_slips, used by
    resync_draft_payroll_runs so a single employee's salary-structure
    change doesn't pay the cost of recomputing the whole company's payroll
    just to refresh the one person who actually changed."""
    # In place (stable slip id); refuses a draft / locked / paid run (H-13).
    from . import payroll_period
    return payroll_period.resync_employee(db, company_id, run, employee_id)


def _resolve_assignment_as_of(
    db: Session, employee_id: uuid.UUID, as_of_date: datetime.date
) -> models.SalaryStructureAssignment | None:
    """Same 'current assignment as of a date' resolution
    _compute_and_write_slip uses inline -- factored out here (not applied
    back to _compute_and_write_slip, to avoid touching stable payroll-run
    code) so Variable Pay entitlement/payout logic always agrees with
    whichever assignment a given payroll run would actually use."""
    return db.scalar(
        select(models.SalaryStructureAssignment)
        .where(
            models.SalaryStructureAssignment.employee_id == employee_id,
            models.SalaryStructureAssignment.from_date <= as_of_date,
        )
        .order_by(
            models.SalaryStructureAssignment.from_date.desc(),
            models.SalaryStructureAssignment.created_at.desc(),
        )
    )


def get_variable_pay_summary(
    db: Session, employee_id: uuid.UUID, as_of_date: datetime.date | None = None
) -> list[dict]:
    """One entry per 'var_annual' component in the employee's salary
    structure assignment current as of [as_of_date] (today, if omitted):
    the annual entitlement (structure line's amount * 12 -- never
    auto-divided into a monthly payment), how much of it has already been
    confirmed-paid this fiscal year, the remainder still payable, and the
    real payout history (each with its payroll run/period). Empty list if
    the employee has no current assignment or their structure has no
    var_annual component -- never fabricated."""
    as_of_date = as_of_date or employee_company_today(db, employee_id)
    assignment = _resolve_assignment_as_of(db, employee_id, as_of_date)
    if assignment is None:
        # No assignment effective as of the reference date -- most likely
        # a future-dated assignment that hasn't started yet (e.g. assigned
        # today, effective next month). Fall back to the most recent
        # assignment overall so HR can still see/manage entitlement ahead
        # of its effective date, instead of this looking like "no
        # structure assigned at all".
        assignment = db.scalar(
            select(models.SalaryStructureAssignment)
            .where(models.SalaryStructureAssignment.employee_id == employee_id)
            .order_by(
                models.SalaryStructureAssignment.from_date.desc(),
                models.SalaryStructureAssignment.created_at.desc(),
            )
        )
        if assignment is None:
            return []
    company = db.get(models.Company, db.get(models.Employee, employee_id).company_id)
    fy_start, fy_end = _fiscal_year_window(company, as_of_date)
    lines = db.scalars(
        select(models.SalaryStructureLine)
        .options(selectinload(models.SalaryStructureLine.component))
        .where(
            models.SalaryStructureLine.structure_id == assignment.structure_id,
            models.SalaryStructureLine.component.has(models.SalaryComponent.calc_type == "var_annual"),
        )
    ).all()
    summaries: list[dict] = []
    for line in lines:
        entitlement = _var_annual_configured_monthly_amount(line, float(assignment.annual_ctc or 0)) * 12
        payouts = db.scalars(
            select(models.VariablePayPayout)
            .options(selectinload(models.VariablePayPayout.payroll_run))
            .where(
                models.VariablePayPayout.employee_id == employee_id,
                models.VariablePayPayout.component_id == line.component_id,
                models.VariablePayPayout.fiscal_year_start == fy_start,
            )
            .order_by(models.VariablePayPayout.created_at.desc())
        ).all()
        paid = sum(float(p.amount) for p in payouts)
        summaries.append({
            "component_id": line.component_id,
            "component_name": line.component.name,
            "structure_assignment_id": assignment.id,
            "fiscal_year_start": fy_start,
            "fiscal_year_end": fy_end,
            "annual_entitlement": entitlement,
            "amount_paid": paid,
            "amount_remaining": max(0.0, entitlement - paid),
            "payouts": [
                {
                    "id": p.id,
                    "payroll_run_id": p.payroll_run_id,
                    "period_month": p.payroll_run.period_month,
                    "period_year": p.payroll_run.period_year,
                    "amount": float(p.amount),
                    "created_at": p.created_at,
                    "notes": p.notes,
                }
                for p in payouts
            ],
        })
    return summaries


def create_variable_pay_payout(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    component_id: uuid.UUID,
    payroll_run_id: uuid.UUID,
    amount: float,
    created_by: uuid.UUID | None,
    notes: str | None = None,
) -> models.SalarySlip | None:
    """HR/Owner/Payroll-Admin confirms a real Variable Pay payment for one
    employee in one specific payroll run. Validates: the run belongs to
    this company, the employee has a var_annual line for this component as
    of the run's period, the amount is positive and doesn't push this
    fiscal year's total past the configured annual entitlement, and no
    payout already exists for this exact (employee, component, run) --
    raises ValueError with a caller-facing message on any violation (the DB
    unique constraint is the last-resort backstop for the same check).

    Immediately resyncs just this one employee's slip in this run (if the
    run's slips already exist and aren't 'paid' yet) so Preview/PDF reflect
    the payout right away -- same safety rule resync_draft_payroll_runs
    already uses elsewhere (never touches a 'paid' slip)."""
    run = db.get(models.PayrollRun, payroll_run_id)
    if run is None or run.company_id != company_id:
        raise ValueError("Payroll run not found")
    if run.status in ("locked", "paid") or run_has_paid_slip(db, run.id):
        raise ValueError("This payroll run is locked -- confirm the payout in an open payroll run.")
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != company_id:
        raise ValueError("Employee not found")
    assignment = _resolve_assignment_as_of(db, employee_id, run.from_date)
    if assignment is None:
        raise ValueError("This employee has no salary structure assigned for this payroll period")
    line = db.scalar(
        select(models.SalaryStructureLine)
        .options(selectinload(models.SalaryStructureLine.component))
        .where(
            models.SalaryStructureLine.structure_id == assignment.structure_id,
            models.SalaryStructureLine.component_id == component_id,
        )
    )
    if line is None or line.component.calc_type != "var_annual":
        raise ValueError("This component is not configured as an annual Variable Pay component for this employee")
    if amount is None or amount <= 0:
        raise ValueError("Amount must be greater than zero")

    company = db.get(models.Company, company_id)
    fy_start, _fy_end = _fiscal_year_window(company, run.to_date)
    entitlement = _var_annual_configured_monthly_amount(line, float(assignment.annual_ctc or 0)) * 12
    already_paid = float(
        db.scalar(
            select(func.coalesce(func.sum(models.VariablePayPayout.amount), 0)).where(
                models.VariablePayPayout.employee_id == employee_id,
                models.VariablePayPayout.component_id == component_id,
                models.VariablePayPayout.fiscal_year_start == fy_start,
            )
        )
        or 0
    )
    remaining = entitlement - already_paid
    if amount > remaining + 0.01:
        raise ValueError(
            f"Amount exceeds the remaining eligible Variable Pay for this fiscal year "
            f"(₹{remaining:,.2f} remaining of ₹{entitlement:,.2f} entitlement)"
        )
    existing = db.scalar(
        select(models.VariablePayPayout).where(
            models.VariablePayPayout.employee_id == employee_id,
            models.VariablePayPayout.component_id == component_id,
            models.VariablePayPayout.payroll_run_id == payroll_run_id,
        )
    )
    if existing is not None:
        raise ValueError("A Variable Pay payment has already been confirmed for this employee in this payroll run")

    db.add(models.VariablePayPayout(
        id=uuid.uuid4(),
        company_id=company_id,
        employee_id=employee_id,
        structure_assignment_id=assignment.id,
        component_id=component_id,
        payroll_run_id=payroll_run_id,
        fiscal_year_start=fy_start,
        amount=amount,
        created_by=created_by,
        created_at=datetime.datetime.now(datetime.timezone.utc),
        notes=notes,
    ))
    db.flush()

    existing_slip = db.scalar(
        select(models.SalarySlip).where(
            models.SalarySlip.payroll_run_id == payroll_run_id,
            models.SalarySlip.employee_id == employee_id,
        )
    )
    if existing_slip is None or existing_slip.status != "paid":
        # Only a generated, open run is refreshed (H-13).
        return resync_employee_slip_in_run(db, company_id, run, employee_id) or existing_slip
    return existing_slip


_DEFAULT_PAYSLIP_TEMPLATE = {
    "show_company_logo": False,
    "header_fields": ["company_name", "pay_period", "employee_name", "employee_code", "designation"],
    "section_labels": {
        "earning": "Earnings",
        "deduction": "Deductions",
        "benefit": "Benefits (Employer Cost)",
        "tax": "Tax",
    },
    "footer_text": "This is a system-generated payslip.",
}


def get_payslip_template(db: Session, company_id: uuid.UUID) -> dict:
    """The one common company-wide payslip layout/format. Returns the
    parsed JSON from core_company_settings.payslip_template, or a
    sensible default shape (never persisted implicitly) when this company
    hasn't configured one yet -- same hydrate-with-fallback convention as
    get_employee_salary_structure's own docstring."""
    raw = db.scalar(
        select(models.CompanySettings.payslip_template).where(
            models.CompanySettings.company_id == company_id
        )
    )
    if not raw:
        return dict(_DEFAULT_PAYSLIP_TEMPLATE)
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return dict(_DEFAULT_PAYSLIP_TEMPLATE)


def set_payslip_template(db: Session, company_id: uuid.UUID, template: dict) -> None:
    settings_row = db.get(models.CompanySettings, company_id)
    if settings_row is None:
        settings_row = models.CompanySettings(company_id=company_id)
        db.add(settings_row)
    settings_row.payslip_template = json.dumps(template)
    db.flush()


def _list_html_templates(db: Session, model, company_id: uuid.UUID, *, extra=None) -> list:
    q = select(model).where(model.company_id == company_id)
    if extra is not None:
        col, val = extra
        q = q.where(col == val)
    return list(db.scalars(q.order_by(model.created_at.desc())).all())


def _get_html_template_by_id(db: Session, model, company_id: uuid.UUID, template_id: uuid.UUID, *, extra=None):
    q = select(model).where(model.company_id == company_id, model.id == template_id)
    if extra is not None:
        col, val = extra
        q = q.where(col == val)
    return db.scalar(q)


def _deactivate_sibling_templates(db: Session, model, company_id: uuid.UUID, keep_id: uuid.UUID, *, extra=None) -> None:
    q = select(model).where(model.company_id == company_id, model.is_active == True, model.id != keep_id)  # noqa: E712
    if extra is not None:
        col, val = extra
        q = q.where(col == val)
    for row in db.scalars(q).all():
        row.is_active = False


def _activate_html_template(db: Session, model, company_id: uuid.UUID, template_id: uuid.UUID, *, extra=None):
    target = _get_html_template_by_id(db, model, company_id, template_id, extra=extra)
    if target is None:
        raise ValueError("Template not found.")
    _deactivate_sibling_templates(db, model, company_id, target.id, extra=extra)
    # Flushed separately from the activation below: same-table UPDATEs in one
    # flush go out together via executemany with no guaranteed row order, so
    # the partial unique index (one active row per company) can see the new
    # active row before the old one's deactivation if both were batched.
    db.flush()
    target.is_active = True
    db.flush()
    return target


def _delete_html_template(db: Session, model, company_id: uuid.UUID, template_id: uuid.UUID, *, extra=None) -> None:
    target = _get_html_template_by_id(db, model, company_id, template_id, extra=extra)
    if target is None:
        raise ValueError("Template not found.")
    if target.is_active:
        raise ValueError("Make another template active before deleting this one.")
    db.delete(target)
    db.flush()


def get_payslip_html_template(
    db: Session, company_id: uuid.UUID
) -> models.PayslipHtmlTemplate | None:
    """Documents > Templates > Payslip Template -- the company's own
    HTML+CSS payslip design, one ACTIVE row per company (it may now have
    several saved templates -- see list_payslip_html_templates -- but at
    most one is active at a time). None means no active template (either
    none authored yet, or all deactivated); payslip generation then falls
    back to the legacy JSON-template/reportlab renderer, see
    get_payslip_template)."""
    return db.scalar(
        select(models.PayslipHtmlTemplate).where(
            models.PayslipHtmlTemplate.company_id == company_id,
            models.PayslipHtmlTemplate.is_active == True,  # noqa: E712
        )
    )


def list_payslip_html_templates(db: Session, company_id: uuid.UUID) -> list[models.PayslipHtmlTemplate]:
    return _list_html_templates(db, models.PayslipHtmlTemplate, company_id)


def get_payslip_html_template_by_id(db: Session, company_id: uuid.UUID, template_id: uuid.UUID):
    return _get_html_template_by_id(db, models.PayslipHtmlTemplate, company_id, template_id)


def create_payslip_html_template(
    db: Session, company_id: uuid.UUID, *, name: str, html_body: str, css_styles: str,
    is_active: bool, created_by: uuid.UUID | None,
) -> models.PayslipHtmlTemplate:
    # Always inserted inactive first -- the partial unique index only
    # covers is_active rows, so this insert never conflicts with an
    # existing active template. Flipping it active (if requested) happens
    # only after that sibling is deactivated, in a second flush.
    now = datetime.datetime.now(datetime.timezone.utc)
    row = models.PayslipHtmlTemplate(
        id=uuid.uuid4(), company_id=company_id, name=name, html_body=html_body, css_styles=css_styles,
        is_active=False, version=1, updated_by=created_by, created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    if is_active:
        _deactivate_sibling_templates(db, models.PayslipHtmlTemplate, company_id, row.id)
        db.flush()
        row.is_active = True
        db.flush()
    return row


def update_payslip_html_template_by_id(
    db: Session, company_id: uuid.UUID, template_id: uuid.UUID, *, name: str, html_body: str,
    css_styles: str, is_active: bool, updated_by: uuid.UUID | None,
) -> models.PayslipHtmlTemplate:
    row = get_payslip_html_template_by_id(db, company_id, template_id)
    if row is None:
        raise ValueError("Template not found.")
    row.name, row.html_body, row.css_styles = name, html_body, css_styles
    row.version += 1
    row.updated_by = updated_by
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    if is_active:
        _deactivate_sibling_templates(db, models.PayslipHtmlTemplate, company_id, row.id)
        db.flush()
    row.is_active = is_active
    db.flush()
    return row


def delete_payslip_html_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID) -> None:
    _delete_html_template(db, models.PayslipHtmlTemplate, company_id, template_id)


def activate_payslip_html_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID) -> models.PayslipHtmlTemplate:
    return _activate_html_template(db, models.PayslipHtmlTemplate, company_id, template_id)


def upsert_payslip_html_template(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    html_body: str,
    css_styles: str,
    is_active: bool,
    updated_by: uuid.UUID | None,
) -> models.PayslipHtmlTemplate:
    """Creates the company's one Payslip Template row, or updates it in
    place and bumps `version` -- there is no history of past designs, same
    single-row-per-company granularity as the legacy JSON template it
    supersedes."""
    now = datetime.datetime.now(datetime.timezone.utc)
    row = get_payslip_html_template(db, company_id)
    if row is None:
        row = models.PayslipHtmlTemplate(
            id=uuid.uuid4(),
            company_id=company_id,
            name=name,
            html_body=html_body,
            css_styles=css_styles,
            is_active=is_active,
            version=1,
            updated_by=updated_by,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.name = name
        row.html_body = html_body
        row.css_styles = css_styles
        row.is_active = is_active
        row.version += 1
        row.updated_by = updated_by
        row.updated_at = now
    db.flush()
    return row


def get_latest_salary_slip_id_for_employee(
    db: Session, employee_id: uuid.UUID
) -> uuid.UUID | None:
    """Most recent (by payroll period) real hcm.salary_slips row for one
    employee -- used by the Payslip Template Live Preview so an admin can
    pick any real employee and see their actual latest payslip data
    substituted into the template, never fabricated sample values."""
    return db.scalar(
        select(models.SalarySlip.id)
        .join(models.PayrollRun, models.PayrollRun.id == models.SalarySlip.payroll_run_id)
        .where(models.SalarySlip.employee_id == employee_id)
        .order_by(
            models.PayrollRun.period_year.desc(),
            models.PayrollRun.period_month.desc(),
        )
        .limit(1)
    )


def _fiscal_year_window(company: "models.Company | None", on_date: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Pure date-math version of get_or_create_fiscal_year's window (no DB
    write) -- safe to call from a plain read/preview/PDF path that must
    never have a side effect of inserting a fin.fiscal_years row."""
    start_month = company.fiscal_year_start_month if company else 4
    start_year = on_date.year if on_date.month >= start_month else on_date.year - 1
    start_date = datetime.date(start_year, start_month, 1)
    end_date = datetime.date(start_year + 1, start_month, 1) - datetime.timedelta(days=1)
    return start_date, end_date


def get_salary_slip_detail(db: Session, slip_id: uuid.UUID) -> dict | None:
    """Everything the payslip PDF (or an in-app detail view) needs for one
    slip: the slip itself, its lines with resolved component name/type,
    the employee, the payroll run's period, and each line's Year-To-Date
    total (see ytd_by_component) -- the real sum of that same component
    across every payroll run in this employee's current fiscal year up to
    and including this slip's own run, computed fresh every time from
    hcm_salary_slip_lines (never stored/cached), so it's always accurate
    even if a past run gets regenerated. Returns None if the slip doesn't
    exist."""
    slip = db.get(models.SalarySlip, slip_id)
    if slip is None:
        return None
    run = db.get(models.PayrollRun, slip.payroll_run_id)
    employee = db.get(models.Employee, slip.employee_id)
    lines = db.scalars(
        select(models.SalarySlipLine)
        .options(selectinload(models.SalarySlipLine.component))
        .where(models.SalarySlipLine.slip_id == slip.id)
    ).all()

    ytd_by_component: dict[uuid.UUID, float] = {}
    if run is not None:
        company = db.get(models.Company, run.company_id)
        fy_start, _fy_end = _fiscal_year_window(company, run.to_date)
        ytd_rows = db.execute(
            select(
                models.SalarySlipLine.component_id,
                func.sum(models.SalarySlipLine.amount),
            )
            .join(models.SalarySlip, models.SalarySlip.id == models.SalarySlipLine.slip_id)
            .join(models.PayrollRun, models.PayrollRun.id == models.SalarySlip.payroll_run_id)
            .where(
                models.SalarySlip.employee_id == slip.employee_id,
                models.PayrollRun.from_date >= fy_start,
                models.PayrollRun.to_date <= run.to_date,
            )
            .group_by(models.SalarySlipLine.component_id)
        ).all()
        ytd_by_component = {row.component_id: float(row[1]) for row in ytd_rows}

    # Payroll > Travel & Expense Reimbursements -- deliberately a SEPARATE
    # list from "lines" above (never a fake SalarySlipLine/SalaryComponent
    # entry), so a reimbursement is never miscategorized as an earning/
    # deduction/tax/benefit in this same detail view.
    reimbursement_lines = db.scalars(
        select(models.SalarySlipReimbursementLine).where(
            models.SalarySlipReimbursementLine.slip_id == slip.id
        )
    ).all()

    return {
        "slip": slip,
        "run": run,
        "employee": employee,
        "lines": [
            {
                "component_id": line.component_id,
                "name": line.component.name,
                "component_type": line.component.component_type,
                "amount": float(line.amount),
                "ytd_amount": ytd_by_component.get(line.component_id, float(line.amount)),
            }
            for line in lines
        ],
        "reimbursements": _slip_reimbursement_rows(db, reimbursement_lines),
    }


def _slip_reimbursement_rows(db: Session, reimbursement_lines) -> list[dict]:
    """Payslip reimbursement lines with their source Travel Requisition /
    Expense Report reference, resolved through each line's inclusion."""
    inclusions = {
        i.id: i
        for i in (
            db.scalars(
                select(models.PayrollReimbursementInclusion).where(
                    models.PayrollReimbursementInclusion.id.in_(
                        [l.inclusion_id for l in reimbursement_lines]
                    )
                )
            )
            if reimbursement_lines
            else []
        )
    }
    rows = []
    for line in reimbursement_lines:
        inclusion = inclusions.get(line.inclusion_id)
        reference = None
        source_type = inclusion.source_type if inclusion else None
        if inclusion is not None:
            model = (
                models.TravelRequestModel
                if inclusion.source_type == "travel_request"
                else models.ExpenseClaim
            )
            source = db.get(model, inclusion.source_id)
            if source is not None:
                reference = _reimbursement_reference(inclusion.source_type, source)
        rows.append({
            "label": line.label,
            "amount": float(line.amount),
            "reference": reference or "—",
            "source_type": source_type,
            "inclusion_id": str(line.inclusion_id),
        })
    return rows


def get_employee_salary_structure(db: Session, employee_id: uuid.UUID) -> dict | None:
    """Resolves an employee's current salary structure assignment into a
    flat list of (component, amount) pairs the frontend can render directly.
    Returns None when nothing is assigned yet -- hcm.salary_structure_
    assignments has no seed data (see SalaryStructure's docstring), so this
    is None for every employee until real rows exist; the caller falls back
    to the existing illustrative display in that case, same as every other
    hydrate-with-fallback in this app.

    Prefers the assignment whose from_date has actually arrived (matching
    generate_payroll_slips' own from_date <= reference-date filter), same
    as before. But when NONE has arrived yet -- e.g. an assignment created
    today to take effect next month, which is the employee's real, only
    configured structure -- falls back to the most recently effective one
    overall instead of returning None, so "My Salary Structure" isn't
    blank for an employee who genuinely has a structure, just not one that
    has started yet. Only a truly unassigned employee (no assignment rows
    at all) still gets None here."""
    assignment = db.scalar(
        select(models.SalaryStructureAssignment)
        .where(
            models.SalaryStructureAssignment.employee_id == employee_id,
            models.SalaryStructureAssignment.from_date <= employee_company_today(db, employee_id),
        )
        .order_by(models.SalaryStructureAssignment.from_date.desc(), models.SalaryStructureAssignment.created_at.desc())
    )
    if assignment is None:
        assignment = db.scalar(
            select(models.SalaryStructureAssignment)
            .where(models.SalaryStructureAssignment.employee_id == employee_id)
            .order_by(
                models.SalaryStructureAssignment.from_date.desc(),
                models.SalaryStructureAssignment.created_at.desc(),
            )
        )
    if assignment is None:
        return None

    structure = db.get(models.SalaryStructure, assignment.structure_id)
    if structure is None:
        return None

    lines = db.scalars(
        select(models.SalaryStructureLine)
        .options(selectinload(models.SalaryStructureLine.component))
        .where(models.SalaryStructureLine.structure_id == structure.id)
    ).all()

    # Same chained resolution generate_payroll_slips uses (ctc-percent
    # lines first, e.g. Basic, then basic-percent lines, e.g. HRA/PF,
    # computed off THAT resolved Basic, then flat lines as-is) -- this
    # used to compute every percent line directly off annual CTC with no
    # chaining, which silently showed a different HRA/PF figure here than
    # an employee's actual payslip used for the exact same assignment.
    # Displayed at annual granularity (this endpoint's existing, expected
    # unit) by multiplying the monthly-resolved amount by 12; the payslip
    # itself shows the same underlying monthly figures unmultiplied -- the
    # two now differ only by that documented x12, never by a different
    # calculation.
    annual_ctc = float(assignment.annual_ctc if assignment.annual_ctc is not None else assignment.base_amount)
    overrides = get_assignment_overrides_map(db, assignment.id)
    employee = db.get(models.Employee, employee_id)
    pf_wage_ceiling = get_company_pf_wage_ceiling(db, employee.company_id) if employee else 15000.0
    resolved = _resolve_structure_lines_monthly(lines, annual_ctc, overrides, pf_wage_ceiling)

    components = [
        {
            "name": r["component"].name,
            "type": r["component"].component_type.replace("_", " ").title(),
            # var_annual lines always resolve to 0 here (they're never an
            # auto-monthly amount -- see _resolve_structure_lines_monthly),
            # but "My Salary Structure" is about what's CONFIGURED, not
            # what's been paid out this specific month, so show the real
            # configured annual entitlement (line.amount * 12) instead of
            # 0 for these -- otherwise this view would misleadingly claim
            # the employee has no Variable Pay entitlement at all.
            "amount": (
                _var_annual_configured_monthly_amount(r["line"], annual_ctc) * 12
                if r["component"].calc_type == "var_annual"
                else r["amount"] * 12
            ),
            "taxable": "Yes" if r["component"].is_taxable else "No",
        }
        for r in resolved
    ]

    return {"structure_name": structure.name, "components": components}


def list_payroll_runs(db: Session, company_id: uuid.UUID) -> list[models.PayrollRun]:
    """Every payroll run this company has ever created, newest period
    first -- backs the payroll-run picker in the Variable Pay confirm-
    payment flow (and any other admin UI that needs "which runs exist")."""
    return db.scalars(
        select(models.PayrollRun)
        .where(models.PayrollRun.company_id == company_id)
        .order_by(models.PayrollRun.period_year.desc(), models.PayrollRun.period_month.desc())
    ).all()


def create_payroll_run(
    db: Session, company_id: uuid.UUID, period_month: int, period_year: int
) -> models.PayrollRun:
    """Backs the Payroll screen's "Run Payroll" button. Concurrency-safe:
    two clicks (or two admins) for the same period must not create two
    runs -- hcm.payroll_runs has a real UNIQUE(company_id, run_no)
    constraint, and run_no is derived deterministically from the period, so
    the advisory lock plus that constraint together make this safe."""
    run_no = f"PR-{period_year:04d}-{period_month:02d}"
    _advisory_lock(db, "payroll_run", str(company_id), run_no)

    existing = db.scalar(
        select(models.PayrollRun).where(
            models.PayrollRun.company_id == company_id, models.PayrollRun.run_no == run_no
        )
    )
    if existing is not None:
        raise ValueError(f"Payroll run '{run_no}' already exists")

    first_day = datetime.date(period_year, period_month, 1)
    next_month_first = (
        datetime.date(period_year + 1, 1, 1)
        if period_month == 12
        else datetime.date(period_year, period_month + 1, 1)
    )
    last_day = next_month_first - datetime.timedelta(days=1)

    run = models.PayrollRun(
        id=uuid.uuid4(),
        company_id=company_id,
        run_no=run_no,
        period_month=period_month,
        period_year=period_year,
        from_date=first_day,
        to_date=last_day,
        status="draft",
    )
    db.add(run)
    db.flush()
    return run


def create_reimbursement(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    category: str,
    amount: float,
    expense_date: datetime.date,
) -> models.ExpenseClaim:
    # M-21: unique claim_no per claim + double-submit replay (expense_claims.py).
    from . import expense_claims

    expense_claims.lock_employee(db, "reimbursement", company_id, employee_id)
    existing = expense_claims.recent_duplicate(
        db, company_id, employee_id, "REIM", expense_date, category, amount
    )
    if existing is not None:
        existing.idempotent_replay = True
        return existing
    claim_no = expense_claims.generate_claim_no(db, company_id, "REIM", expense_date)

    claim = models.ExpenseClaim(
        id=uuid.uuid4(),
        company_id=company_id,
        created_at=expense_claims.now(),
        claim_no=claim_no,
        employee_id=employee_id,
        claim_date=expense_date,
        purpose=category,
        total_amount=amount,
        status="pending",
    )
    db.add(claim)
    db.flush()

    db.add(
        models.ExpenseClaimLine(
            id=uuid.uuid4(),
            claim_id=claim.id,
            expense_date=expense_date,
            description=category,
            amount=amount,
        )
    )
    db.flush()
    return claim


def list_reimbursements(
    db: Session,
    company_id: uuid.UUID,
    employee_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.ExpenseClaim]:
    """Reimbursements and Expense Reports share hcm.expense_claims --
    distinguished only by claim_no prefix (see create_reimbursement vs
    create_expense_report), since a reimbursement has no real doctype
    column of its own to filter on."""
    q = (
        select(models.ExpenseClaim)
        .where(
            models.ExpenseClaim.company_id == company_id,
            models.ExpenseClaim.claim_no.like("REIM-%"),
        )
        .order_by(models.ExpenseClaim.claim_date.desc(), models.ExpenseClaim.id)
    )
    if employee_ids is not None:
        q = q.where(models.ExpenseClaim.employee_id.in_(employee_ids))
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_expense_claim_for_update(db: Session, claim_id: uuid.UUID) -> models.ExpenseClaim | None:
    """SELECT ... FOR UPDATE -- same double-approval protection as
    get_regularization_for_update/get_leave_request_for_update."""
    return db.scalar(
        select(models.ExpenseClaim).where(models.ExpenseClaim.id == claim_id).with_for_update()
    )


def list_loans(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.Loan]:
    q = (
        select(models.Loan)
        .join(models.Employee, models.Employee.id == models.Loan.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.Loan.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def create_loan(
    db: Session,
    employee_id: uuid.UUID,
    loan_type: str,
    principal_amount: float,
    emi_amount: float | None,
    outstanding_balance: float,
    company_id: uuid.UUID | None = None,
) -> models.Loan:
    """M-31: amounts are validated (schemas.LoanCreate + here) and the
    employee must belong to [company_id]. The EMI is recovered by payroll
    (payroll_period) and reduces outstanding_balance when the run locks."""
    if company_id is not None:
        employee = db.get(models.Employee, employee_id)
        if employee is None or employee.company_id != company_id:
            raise LookupError("Employee not found")
    if principal_amount is None or principal_amount <= 0:
        raise ValueError("Principal amount must be greater than zero.")
    if outstanding_balance is None or outstanding_balance < 0 or outstanding_balance > principal_amount:
        raise ValueError("Outstanding balance must be between 0 and the principal amount.")
    if emi_amount is not None and (emi_amount <= 0 or emi_amount > principal_amount):
        raise ValueError("EMI must be greater than zero and not more than the principal amount.")
    loan = models.Loan(
        id=uuid.uuid4(),
        employee_id=employee_id,
        loan_type=loan_type,
        principal_amount=principal_amount,
        emi_amount=emi_amount,
        outstanding_balance=outstanding_balance,
        status="active",
    )
    db.add(loan)
    db.flush()
    return loan


def list_tax_declarations(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.TaxDeclaration]:
    q = (
        select(models.TaxDeclaration)
        .join(models.Employee, models.Employee.id == models.TaxDeclaration.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.TaxDeclaration.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def create_tax_declaration(
    db: Session,
    employee_id: uuid.UUID,
    fiscal_year: str,
    tax_regime: str,
    hra_claimed: float,
    section_80c: float,
    section_80d: float = 0,
    section_80ccd_1b: float = 0,
    home_loan_interest: float = 0,
) -> models.TaxDeclaration:
    existing = db.scalar(
        select(models.TaxDeclaration).where(
            models.TaxDeclaration.employee_id == employee_id,
            models.TaxDeclaration.fiscal_year == fiscal_year,
        )
    )
    if existing is not None:
        raise ValueError(f"Tax declaration for {fiscal_year} already exists for this employee")
    declaration = models.TaxDeclaration(
        id=uuid.uuid4(),
        employee_id=employee_id,
        fiscal_year=fiscal_year,
        tax_regime=tax_regime,
        hra_claimed=hra_claimed,
        section_80c=section_80c,
        section_80d=section_80d,
        section_80ccd_1b=section_80ccd_1b,
        home_loan_interest=home_loan_interest,
        status="draft",
    )
    db.add(declaration)
    db.flush()
    return declaration


# ---------------------------------------------------------------------------
# Recruitment
# ---------------------------------------------------------------------------

_STAGE_DISPLAY = {
    "applied": "Applied",
    "screened": "Screened",
    "interview": "Interview",
    "offer": "Offer",
    "hired": "Hired",
}


def list_job_openings(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.JobOpening]:
    q = (
        select(models.JobOpening)
        .where(models.JobOpening.company_id == company_id)
        .order_by(models.JobOpening.posted_date.desc(), models.JobOpening.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def list_lifecycle_events(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0,
    visible_ids: "set[uuid.UUID] | None" = None,
) -> list[dict]:
    """People > Transfers & Promotions. Empty for every company today
    (hcm.employee_lifecycle_events has zero rows) -- the caller shows an
    empty state until this table is populated, same as Offboarding did
    before hcm.exit_requests existed. Returns {"event": ..., "employee_name":
    ..., "employee_code": ...} dicts -- EmployeeLifecycleEvent has no ORM
    `employee` relationship (employee_id is a bare FK column, same no-
    relationship convention as ExitRequestModel), so the denormalized
    identity fields TransferEventOut needs are pulled via this query's own
    join rather than a lazy-load."""
    q = (
        select(models.EmployeeLifecycleEvent, models.Employee)
        .join(models.Employee, models.Employee.id == models.EmployeeLifecycleEvent.employee_id)
        .options(
            selectinload(models.EmployeeLifecycleEvent.from_designation),
            selectinload(models.EmployeeLifecycleEvent.to_designation),
            selectinload(models.EmployeeLifecycleEvent.from_department),
            selectinload(models.EmployeeLifecycleEvent.to_department),
        )
        .where(models.Employee.company_id == company_id)
        .order_by(models.EmployeeLifecycleEvent.event_date.desc(), models.EmployeeLifecycleEvent.id)
    )
    if visible_ids is not None:  # H-03: the caller's People visibility
        q = q.where(models.Employee.id.in_(list(visible_ids)))
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return [
        {"event": event, "employee_name": _full_name(emp), "employee_code": emp.employee_code}
        for event, emp in db.execute(q).all()
    ]


_LIFECYCLE_EVENT_REF_FIELDS = [
    "designation", "department", "branch", "business_unit", "sub_department",
]


def _named_ref(obj) -> "dict | None":
    if obj is None:
        return None
    return {"id": obj.id, "name": obj.name}


def get_lifecycle_event(
    db: Session, company_id: uuid.UUID, event_id: uuid.UUID,
    visible_ids: "set[uuid.UUID] | None" = None,
) -> "dict | None":
    """People > Transfers & Promotions > (click a row). Tenant-isolated via
    the Employee join, same as list_lifecycle_events -- returns None (the
    router turns this into a 404) for an id from another company, so a
    cross-tenant guess is indistinguishable from a nonexistent id."""
    row = db.execute(
        select(models.EmployeeLifecycleEvent, models.Employee)
        .join(models.Employee, models.Employee.id == models.EmployeeLifecycleEvent.employee_id)
        .options(
            *[
                opt
                for field in _LIFECYCLE_EVENT_REF_FIELDS
                for opt in (
                    selectinload(getattr(models.EmployeeLifecycleEvent, f"from_{field}")),
                    selectinload(getattr(models.EmployeeLifecycleEvent, f"to_{field}")),
                )
            ]
        )
        .where(
            models.EmployeeLifecycleEvent.id == event_id,
            models.Employee.company_id == company_id,
            *([models.Employee.id.in_(list(visible_ids))] if visible_ids is not None else []),
        )
    ).first()
    if row is None:
        return None
    e, emp = row
    is_promotion = e.to_designation_id is not None and e.to_designation_id != e.from_designation_id
    if is_promotion:
        event_type_label = "Promotion"
    elif e.to_department_id is not None and e.to_department_id != e.from_department_id:
        event_type_label = "Inter-department Transfer"
    else:
        event_type_label = e.event_type.replace("_", " ").title()

    def manager_ref(manager_id):
        if manager_id is None:
            return None
        manager = db.get(models.Employee, manager_id)
        return {"id": manager.id, "name": _full_name(manager)} if manager else None

    return {
        "id": e.id,
        "employee_id": emp.id,
        "employee_name": _full_name(emp),
        "employee_code": emp.employee_code,
        "type": event_type_label,
        "event_date": e.event_date,
        "from_designation": _named_ref(e.from_designation),
        "to_designation": _named_ref(e.to_designation),
        "from_department": _named_ref(e.from_department),
        "to_department": _named_ref(e.to_department),
        "from_branch": _named_ref(e.from_branch),
        "to_branch": _named_ref(e.to_branch),
        "from_business_unit": _named_ref(e.from_business_unit),
        "to_business_unit": _named_ref(e.to_business_unit),
        "from_sub_department": _named_ref(e.from_sub_department),
        "to_sub_department": _named_ref(e.to_sub_department),
        "from_reporting_manager": manager_ref(e.from_reporting_manager_id),
        "to_reporting_manager": manager_ref(e.to_reporting_manager_id),
        "from_dotted_line_manager": manager_ref(e.from_dotted_line_manager_id),
        "to_dotted_line_manager": manager_ref(e.to_dotted_line_manager_id),
        "from_ctc": e.from_ctc,
        "to_ctc": e.to_ctc,
    }


_LIFECYCLE_EVENT_EDITABLE_FIELDS = [
    "event_date",
    "from_designation_id", "to_designation_id",
    "from_department_id", "to_department_id",
    "from_branch_id", "to_branch_id",
    "from_business_unit_id", "to_business_unit_id",
    "from_sub_department_id", "to_sub_department_id",
    "from_reporting_manager_id", "to_reporting_manager_id",
    "from_dotted_line_manager_id", "to_dotted_line_manager_id",
    "from_ctc", "to_ctc",
]


def get_lifecycle_event_for_company(
    db: Session, company_id: uuid.UUID, event_id: uuid.UUID
) -> "models.EmployeeLifecycleEvent | None":
    return db.execute(
        select(models.EmployeeLifecycleEvent)
        .join(models.Employee, models.Employee.id == models.EmployeeLifecycleEvent.employee_id)
        .where(
            models.EmployeeLifecycleEvent.id == event_id,
            models.Employee.company_id == company_id,
        )
    ).scalar_one_or_none()


def update_lifecycle_event(
    event: models.EmployeeLifecycleEvent, updates: dict
) -> bool:
    """People > Transfers & Promotions > Edit -- authorized HR/Organization
    Owner correction of a specific historical record's own fields (see
    require_people_access("transfer") at the route). Deliberately does NOT
    call record_employee_change or touch the employee's current core.employees
    row: that function is create-only (a genuinely new transfer/promotion
    always INSERTs a fresh row -- see its own docstring, "history rows are
    append-only"), and re-applying an edited historical row to the
    employee's live state could silently clobber whatever a *later* real
    transfer already set. This only ever updates the one row the caller
    already resolved via get_lifecycle_event_for_company (tenant-checked),
    and touches no other row -- existing history is never overwritten.

    Returns False (the router turns this into a 400) if none of the
    provided fields actually differ from the stored values -- a caller
    correcting a record must be making an actual change, not a no-op
    resubmission."""
    changed = False
    for field in _LIFECYCLE_EVENT_EDITABLE_FIELDS:
        if field not in updates:
            continue
        if getattr(event, field) != updates[field]:
            setattr(event, field, updates[field])
            changed = True
    return changed


def list_exit_requests(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0,
    visible_ids: "set[uuid.UUID] | None" = None,
) -> list[dict]:
    """One dict per exit request with its employee, checklist items (keyed
    by task name) and final settlement, for People > Offboarding."""
    q = (
        select(models.ExitRequestModel, models.Employee)
        .join(models.Employee, models.Employee.id == models.ExitRequestModel.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.ExitRequestModel.last_working_day.desc(), models.ExitRequestModel.id)
    )
    if visible_ids is not None:  # H-03: the caller's People visibility
        q = q.where(models.Employee.id.in_(list(visible_ids)))
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    exit_rows = db.execute(q).all()
    if not exit_rows:
        return []
    exits = [row[0] for row in exit_rows]
    employee_by_exit_id = {row[0].id: row[1] for row in exit_rows}
    exit_ids = [e.id for e in exits]

    checklist: dict[uuid.UUID, dict[str, str]] = {}
    for item in db.scalars(
        select(models.ExitChecklistItem).where(models.ExitChecklistItem.exit_id.in_(exit_ids))
    ):
        checklist.setdefault(item.exit_id, {})[item.task] = item.status

    settlements: dict[uuid.UUID, models.FinalSettlement] = {
        row.exit_id: row
        for row in db.scalars(
            select(models.FinalSettlement).where(models.FinalSettlement.exit_id.in_(exit_ids))
        )
    }

    result = []
    for e in exits:
        tasks = checklist.get(e.id, {})
        settlement = settlements.get(e.id)
        emp = employee_by_exit_id.get(e.id)
        result.append(
            {
                "id": e.id,
                "employee_id": e.employee_id,
                "last_working_day": e.last_working_day,
                "resignation_date": e.resignation_date,
                "final_settlement_review": tasks.get("Final settlement review", "pending"),
                "access_revocation": tasks.get("Access revocation", "pending"),
                "knowledge_transfer": tasks.get("Knowledge transfer", "pending"),
                "asset_return": tasks.get("Asset return", "pending"),
                "fnf_status": fnf_status(settlement),
                "employee_name": _full_name(emp),
                "employee_code": emp.employee_code if emp else None,
            }
        )
    return result


def create_exit_request(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    resignation_date: datetime.date,
    last_working_day: datetime.date | None,
    reason: str | None,
) -> models.ExitRequestModel:
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != company_id:
        raise ValueError("Employee not found")
    # M-37: joining <= resignation (<= last working day: schema), and one
    # open exit per employee -- serialised per employee so two concurrent
    # submissions can't both pass. Caller maps the subclass to 422 / 409.
    from .employee_validation import EmployeeInputError
    if employee.date_of_joining and resignation_date < employee.date_of_joining:
        raise EmployeeInputError("Resignation date can't be before the employee's date of joining.")
    if last_working_day is not None and last_working_day < resignation_date:
        raise EmployeeInputError("Last working day can't be before the resignation date.")
    _advisory_lock(db, "exit_request", str(employee_id))
    open_exit = db.scalar(
        select(models.ExitRequestModel.status).where(
            models.ExitRequestModel.employee_id == employee_id,
            models.ExitRequestModel.status.in_(("pending", "submitted", "approved", "in_clearance")),
        ).limit(1)
    )
    if open_exit is not None:
        raise ValueError(
            f"This employee already has an exit request that is {open_exit.replace('_', ' ')}; "
            "withdraw or decide it before submitting another."
        )
    request = models.ExitRequestModel(
        id=uuid.uuid4(),
        employee_id=employee_id,
        resignation_date=resignation_date,
        last_working_day=last_working_day,
        reason=reason,
        status="pending",
    )
    db.add(request)
    db.flush()
    return request


def get_exit_request_visible_ids(db: Session, user: models.User) -> set[uuid.UUID] | None:
    """H-04: whose exit requests (reason, decision notes) the caller may
    list -- own, everyone they may decide for (reporting chain, custom
    workflow steps, fallback approver) and their People visibility. None =
    whole company (HR / Owner / org-wide approvers)."""
    people = get_people_directory_visible_ids(db, user)
    if people is None:
        return None
    deciders = get_visible_employee_ids_for_requests(db, user, "exit_request")
    if deciders is None:
        return None
    own = {user.employee_id} if user.employee_id else set()
    return set(people) | set(deciders) | own


def list_exit_request_records(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0,
    visible_ids: "set[uuid.UUID] | None" = None,
) -> list[models.ExitRequestModel]:
    """Every exit request row (any status) for the Approvals inbox --
    unlike list_exit_requests (the Offboarding tab's joined reporting view,
    which has no id/status), this returns the raw domain rows the inbox
    needs to decide on. Same unscoped-by-visibility shape as
    list_hiring_requisitions -- the frontend narrows to pending + the
    caller's own reporting chain, same as every other doctype there."""
    q = (
        select(models.ExitRequestModel)
        .join(models.Employee, models.Employee.id == models.ExitRequestModel.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.ExitRequestModel.created_at.desc(), models.ExitRequestModel.id)
    )
    if visible_ids is not None:
        q = q.where(models.ExitRequestModel.employee_id.in_(list(visible_ids)))
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return list(db.scalars(q).all())


def get_exit_request_for_update(
    db: Session, request_id: uuid.UUID, company_id: uuid.UUID
) -> models.ExitRequestModel | None:
    """Scoped by company_id via the resigning employee -- same rationale as
    get_hiring_requisition_for_update: without it, any authenticated user of
    any company could decide another company's exit request by guessing/
    enumerating its id (ExitRequestModel has no company_id column of its own)."""
    return db.scalar(
        select(models.ExitRequestModel)
        .join(models.Employee, models.Employee.id == models.ExitRequestModel.employee_id)
        .where(
            models.ExitRequestModel.id == request_id,
            models.Employee.company_id == company_id,
        )
        .with_for_update()
    )


def list_interviews(
    db: Session,
    company_id: uuid.UUID,
    limit: int | None = None,
    offset: int = 0,
    interviewer_id: uuid.UUID | None = None,
) -> list[models.Interview]:
    q = (
        select(models.Interview)
        .join(models.JobApplication, models.JobApplication.id == models.Interview.application_id)
        .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
        .options(
            selectinload(models.Interview.application).selectinload(models.JobApplication.candidate),
            selectinload(models.Interview.application).selectinload(models.JobApplication.opening),
            selectinload(models.Interview.interviewer),
        )
        .where(models.JobOpening.company_id == company_id)
        .order_by(models.Interview.scheduled_at.desc(), models.Interview.id)
    )
    if interviewer_id is not None:
        q = q.where(models.Interview.interviewer_id == interviewer_id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def create_job_opening(
    db: Session,
    company_id: uuid.UUID,
    title: str,
    department_name: str,
    employment_type: str,
    openings: int,
    status: str = "open",
    posted_date: datetime.date | None = None,
    description: str | None = None,
) -> models.JobOpening:
    department = get_department_by_name(db, company_id, department_name)
    if department is None:
        raise ValueError(f"Department '{department_name}' not found")

    opening = models.JobOpening(
        id=uuid.uuid4(),
        company_id=company_id,
        title=title,
        department_id=department.id,
        vacancies=openings,
        status=status,
        employment_type=employment_type,
        posted_date=posted_date or company_today(db, company_id),
        description=description,
    )
    db.add(opening)
    db.flush()
    return opening


def create_candidate(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    years_experience: float | None,
    email: str | None = None,
    phone: str | None = None,
) -> models.Candidate:
    candidate = models.Candidate(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        years_experience=years_experience,
        email=email,
        phone=phone,
    )
    db.add(candidate)
    db.flush()
    return candidate


def create_job_application(
    db: Session, opening_id: uuid.UUID, candidate_id: uuid.UUID, stage: str
) -> models.JobApplication:
    application = models.JobApplication(
        id=uuid.uuid4(), opening_id=opening_id, candidate_id=candidate_id, stage=stage
    )
    db.add(application)
    db.flush()
    return application


def find_job_opening_by_title(
    db: Session, company_id: uuid.UUID, title: str
) -> models.JobOpening | None:
    return db.scalar(
        select(models.JobOpening).where(
            models.JobOpening.company_id == company_id, models.JobOpening.title == title
        )
    )


def find_application_by_candidate_and_opening(
    db: Session, company_id: uuid.UUID, candidate_name: str, opening_title: str
) -> models.JobApplication | None:
    """Scoped by company_id -- without it, a candidate name + job title that
    happens to match another company's application lets that other
    company's application be interviewed/offered against."""
    return db.scalar(
        select(models.JobApplication)
        .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
        .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
        .where(
            models.Candidate.company_id == company_id,
            models.Candidate.name == candidate_name,
            models.JobOpening.title == opening_title,
        )
    )


def create_interview(
    db: Session, application_id: uuid.UUID, round_no: int, scheduled_at: datetime.datetime
) -> models.Interview:
    interview = models.Interview(
        id=uuid.uuid4(), application_id=application_id, round_no=round_no, scheduled_at=scheduled_at
    )
    db.add(interview)
    db.flush()
    return interview


def create_offer(
    db: Session,
    application_id: uuid.UUID,
    offered_ctc: float,
    offer_date: datetime.date,
    proposed_joining_date: datetime.date | None,
    status: str,
) -> models.Offer:
    offer = models.Offer(
        id=uuid.uuid4(),
        application_id=application_id,
        offered_ctc=offered_ctc,
        offer_date=offer_date,
        proposed_joining_date=proposed_joining_date,
        status=status,
    )
    db.add(offer)
    db.flush()
    return offer


def list_candidate_pipeline(db: Session, company_id: uuid.UUID) -> dict[str, list[dict]]:
    """Groups every candidate's latest application by kanban stage, in the
    same column order the frontend's kanban board renders."""
    applications = db.scalars(
        select(models.JobApplication)
        .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
        .where(models.Candidate.company_id == company_id)
    ).all()

    grouped: dict[str, list[dict]] = {label: [] for label in _STAGE_DISPLAY.values()}
    for application in applications:
        label = _STAGE_DISPLAY.get(application.stage, application.stage.title())
        grouped.setdefault(label, [])
        exp = application.candidate.years_experience
        grouped[label].append(
            {
                "id": application.id,
                "name": application.candidate.name,
                "role": application.opening.title,
                "exp": f"{exp:g} yrs" if exp is not None else "—",
            }
        )
    return grouped


def update_application_stage(
    db: Session, application_id: uuid.UUID, company_id: uuid.UUID, stage: str
) -> models.JobApplication | None:
    """Moves a candidate's application to a new kanban stage -- backs the
    Candidate Pipeline board's "Move to" control, mirroring how
    move_task_card moves a project task between board columns."""
    application = db.scalar(
        select(models.JobApplication)
        .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
        .where(
            models.JobApplication.id == application_id,
            models.Candidate.company_id == company_id,
        )
    )
    if application is None:
        return None
    application.stage = stage
    db.flush()
    return application


_OFFER_STATUS_DISPLAY = {"sent": "Sent", "accepted": "Accepted", "rejected": "Rejected", "withdrawn": "Withdrawn"}


def normalize_offer_status(status: str) -> str:
    """Older/seeded rows stored offer status lowercase; OfferCreate now
    enforces the capitalized enum going forward. Normalize on read so the
    frontend's case-sensitive status badge map (lib/widgets/status_badge.dart)
    renders every offer with the correct color regardless of when it was
    written."""
    return _OFFER_STATUS_DISPLAY.get(status.lower(), status)


def list_offers(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[dict]:
    q = (
        select(models.Offer)
        .join(models.JobApplication, models.JobApplication.id == models.Offer.application_id)
        .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
        .where(models.Candidate.company_id == company_id)
        .order_by(models.Offer.offer_date.desc(), models.Offer.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    offers = db.scalars(q).all()
    return [
        {
            "id": offer.id,
            "candidate": offer.application.candidate.name,
            "role": offer.application.opening.title,
            "ctc": float(offer.offered_ctc),
            "joining": offer.proposed_joining_date.isoformat()
            if offer.proposed_joining_date
            else "—",
            "status": normalize_offer_status(offer.status),
        }
        for offer in offers
    ]


def update_offer_status(
    db: Session, offer_id: uuid.UUID, company_id: uuid.UUID, status: str
) -> models.Offer | None:
    offer = db.scalar(
        select(models.Offer)
        .join(models.JobApplication, models.JobApplication.id == models.Offer.application_id)
        .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
        .where(models.Offer.id == offer_id, models.Candidate.company_id == company_id)
    )
    if offer is None:
        return None
    offer.status = status
    db.flush()
    return offer


def get_offer_detail(db: Session, offer_id: uuid.UUID, company_id: uuid.UUID) -> dict | None:
    """Everything an Offer Letter Template needs for one offer: the offer
    itself, its application/candidate/opening chain, and the department's
    assigned leadership (the closest real stand-in this app has for
    "reporting manager"/"HR contact" on a pre-hire offer -- see
    offer_letter_html_renderer.build_offer_letter_placeholders). Scoped by
    company_id the same way list_offers/update_offer_status already are
    (Offer has no direct company_id of its own). Returns None if the offer
    doesn't exist or belongs to a different company."""
    offer = db.scalar(
        select(models.Offer)
        .join(models.JobApplication, models.JobApplication.id == models.Offer.application_id)
        .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
        .where(models.Offer.id == offer_id, models.Candidate.company_id == company_id)
    )
    if offer is None:
        return None
    application = offer.application
    candidate = application.candidate
    opening = application.opening
    # Recruitment workflow offers carry their own confirmed terms
    # (department / location / reporting manager); older offers fall back
    # to the opening / department exactly as before.
    department_id = offer.department_id or (opening.department_id if opening else None)
    department = db.get(models.Department, department_id) if department_id else None
    designation = (
        db.get(models.Designation, opening.designation_id) if opening and opening.designation_id else None
    )
    branch_id = offer.branch_id or (opening.branch_id if opening else None)
    branch = db.get(models.Branch, branch_id) if branch_id else None
    reporting_manager = (
        db.get(models.Employee, offer.reporting_manager_id)
        if offer.reporting_manager_id
        else db.get(models.Employee, department.senior_manager_id)
        if department and department.senior_manager_id
        else None
    )
    hr_contact = (
        db.get(models.Employee, department.hr_representative_id)
        if department and department.hr_representative_id
        else None
    )
    return {
        "offer": offer,
        "application": application,
        "candidate": candidate,
        "opening": opening,
        "department": department,
        "designation": designation,
        "branch": branch,
        "reporting_manager": reporting_manager,
        "hr_contact": hr_contact,
    }


def get_offer_letter_html_template(
    db: Session, company_id: uuid.UUID
) -> models.OfferLetterHtmlTemplate | None:
    """Documents > Templates > Offer Letter Template -- the company's own
    HTML+CSS offer letter design, one ACTIVE row per company (see
    get_payslip_html_template's docstring for the multi-template/one-active
    shape). None means no active template -- offer-letter generation then
    refuses with a clear message instead of guessing."""
    return db.scalar(
        select(models.OfferLetterHtmlTemplate).where(
            models.OfferLetterHtmlTemplate.company_id == company_id,
            models.OfferLetterHtmlTemplate.is_active == True,  # noqa: E712
        )
    )


def list_offer_letter_html_templates(db: Session, company_id: uuid.UUID) -> list[models.OfferLetterHtmlTemplate]:
    return _list_html_templates(db, models.OfferLetterHtmlTemplate, company_id)


def get_offer_letter_html_template_by_id(db: Session, company_id: uuid.UUID, template_id: uuid.UUID):
    return _get_html_template_by_id(db, models.OfferLetterHtmlTemplate, company_id, template_id)


def create_offer_letter_html_template(
    db: Session, company_id: uuid.UUID, *, name: str, html_body: str, css_styles: str,
    is_active: bool, created_by: uuid.UUID | None,
) -> models.OfferLetterHtmlTemplate:
    now = datetime.datetime.now(datetime.timezone.utc)
    row = models.OfferLetterHtmlTemplate(
        id=uuid.uuid4(), company_id=company_id, name=name, html_body=html_body, css_styles=css_styles,
        is_active=False, version=1, updated_by=created_by, created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    if is_active:
        _deactivate_sibling_templates(db, models.OfferLetterHtmlTemplate, company_id, row.id)
        db.flush()
        row.is_active = True
        db.flush()
    return row


def update_offer_letter_html_template_by_id(
    db: Session, company_id: uuid.UUID, template_id: uuid.UUID, *, name: str, html_body: str,
    css_styles: str, is_active: bool, updated_by: uuid.UUID | None,
) -> models.OfferLetterHtmlTemplate:
    row = get_offer_letter_html_template_by_id(db, company_id, template_id)
    if row is None:
        raise ValueError("Template not found.")
    row.name, row.html_body, row.css_styles = name, html_body, css_styles
    row.version += 1
    row.updated_by = updated_by
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    if is_active:
        _deactivate_sibling_templates(db, models.OfferLetterHtmlTemplate, company_id, row.id)
        db.flush()
    row.is_active = is_active
    db.flush()
    return row


def delete_offer_letter_html_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID) -> None:
    _delete_html_template(db, models.OfferLetterHtmlTemplate, company_id, template_id)


def activate_offer_letter_html_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID) -> models.OfferLetterHtmlTemplate:
    return _activate_html_template(db, models.OfferLetterHtmlTemplate, company_id, template_id)


def upsert_offer_letter_html_template(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    html_body: str,
    css_styles: str,
    is_active: bool,
    updated_by: uuid.UUID | None,
) -> models.OfferLetterHtmlTemplate:
    """Creates the company's one Offer Letter Template row, or updates it
    in place and bumps `version` -- identical semantics to
    upsert_payslip_html_template."""
    now = datetime.datetime.now(datetime.timezone.utc)
    row = get_offer_letter_html_template(db, company_id)
    if row is None:
        row = models.OfferLetterHtmlTemplate(
            id=uuid.uuid4(),
            company_id=company_id,
            name=name,
            html_body=html_body,
            css_styles=css_styles,
            is_active=is_active,
            version=1,
            updated_by=updated_by,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.name = name
        row.html_body = html_body
        row.css_styles = css_styles
        row.is_active = is_active
        row.version += 1
        row.updated_by = updated_by
        row.updated_at = now
    db.flush()
    return row


def record_interview_outcome(
    db: Session,
    interview_id: uuid.UUID,
    company_id: uuid.UUID,
    result: str,
    feedback: str | None,
) -> models.Interview | None:
    interview = db.scalar(
        select(models.Interview)
        .join(models.JobApplication, models.JobApplication.id == models.Interview.application_id)
        .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
        .where(models.Interview.id == interview_id, models.Candidate.company_id == company_id)
    )
    if interview is None:
        return None
    interview.result = result
    if feedback is not None:
        interview.feedback = feedback
    db.flush()
    return interview


# ---------------------------------------------------------------------------
# Performance / Learning
# ---------------------------------------------------------------------------


def list_okrs(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.CompanyOkr]:
    q = (
        select(models.CompanyOkr)
        .where(models.CompanyOkr.company_id == company_id)
        .order_by(models.CompanyOkr.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def create_okr(
    db: Session, company_id: uuid.UUID, level: str, title: str, owner_name: str,
    owner_employee_id: uuid.UUID | None = None,
) -> models.CompanyOkr:
    okr = models.CompanyOkr(
        id=uuid.uuid4(), company_id=company_id, level=level, title=title, owner_name=owner_name,
        owner_employee_id=owner_employee_id,
    )
    db.add(okr)
    db.flush()
    return okr


def list_review_cycles(db: Session, company_id: uuid.UUID) -> list[models.AppraisalCycle]:
    return db.scalars(
        select(models.AppraisalCycle)
        .where(models.AppraisalCycle.company_id == company_id)
        .order_by(models.AppraisalCycle.to_date)
    ).all()


def create_review_cycle(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    from_date: datetime.date,
    to_date: datetime.date,
    status: str,
    participant_count: int,
) -> models.AppraisalCycle:
    existing = db.scalar(
        select(models.AppraisalCycle).where(
            models.AppraisalCycle.company_id == company_id, models.AppraisalCycle.name == name
        )
    )
    if existing is not None:
        raise ValueError(f"Review cycle '{name}' already exists")
    cycle = models.AppraisalCycle(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        from_date=from_date,
        to_date=to_date,
        status=status,
        participant_count=participant_count,
    )
    db.add(cycle)
    db.flush()
    return cycle


def list_training_courses(db: Session, company_id: uuid.UUID) -> list[models.TrainingCourse]:
    return db.scalars(
        select(models.TrainingCourse).where(models.TrainingCourse.company_id == company_id)
    ).all()


def course_enrollment_stats(db: Session, course_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
    """Per-course (enrolled_count, avg_completion_pct) for the Course
    Catalog cards -- completion is 100% for 'completed' status enrollments,
    0% otherwise (hcm.training_enrollments has no partial-progress column)."""
    if not course_ids:
        return {}
    stats: dict[uuid.UUID, list[int]] = {}
    for row in db.scalars(
        select(models.TrainingEnrollment).where(models.TrainingEnrollment.course_id.in_(course_ids))
    ):
        stats.setdefault(row.course_id, []).append(100 if row.status == "completed" else 0)
    return {
        course_id: (len(pcts), round(sum(pcts) / len(pcts)))
        for course_id, pcts in stats.items()
    }


def list_certifications(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.Certification]:
    q = (
        select(models.Certification)
        .join(models.Employee, models.Employee.id == models.Certification.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.Certification.expiry_date, models.Certification.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def create_certification(
    db: Session,
    employee_id: uuid.UUID,
    name: str,
    issuer: str | None,
    issue_date: datetime.date | None,
    expiry_date: datetime.date | None,
) -> models.Certification:
    certification = models.Certification(
        id=uuid.uuid4(),
        employee_id=employee_id,
        name=name,
        issuer=issuer,
        issue_date=issue_date,
        expiry_date=expiry_date,
    )
    db.add(certification)
    db.flush()
    return certification


def create_training_course(
    db: Session,
    company_id: uuid.UUID,
    title: str,
    course_type: str,
    category: str,
    duration_hours: float | None,
    duration_label: str,
    provider: str | None = None,
) -> models.TrainingCourse:
    course = models.TrainingCourse(
        id=uuid.uuid4(),
        company_id=company_id,
        title=title,
        course_type=course_type,
        duration_hours=duration_hours,
        category=category,
        duration_label=duration_label,
        provider=provider,
    )
    db.add(course)
    db.flush()
    return course


# ---------------------------------------------------------------------------
# Benefits / Assets / Documents
# ---------------------------------------------------------------------------


def parse_inr_amount(value: str) -> float | None:
    """Add Asset's Value field (and this table's own seed data) is free
    text ('₹1,89,000', '₹28,500 / yr', '—') -- best-effort: pull the digits
    out, drop any recurring-cost suffix. None when nothing numeric is present."""
    match = re.search(r"[\d,]+(?:\.\d+)?", value)
    if not match:
        return None
    return float(match.group(0).replace(",", ""))


def parse_date_safe(text: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(text.strip())
    except ValueError:
        return None


def parse_lenient_date(text: str) -> datetime.date | None:
    """Add Employee's Date of Birth field is free text hinted 'mm/dd/yyyy'
    but never validated client-side -- try that first, then a couple of
    other common shapes, then give up quietly rather than fail the whole
    employee creation over an unparseable date."""
    text = text.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_leading_number(text: str) -> float | None:
    """Add Employee's Total Experience field is free text ('4 years',
    '6.5') -- pull the leading number out, None if there isn't one."""
    match = re.search(r"[\d.]+", text)
    return float(match.group(0)) if match else None


def list_benefit_categories(
    db: Session,
    company_id: uuid.UUID,
    include_inactive: bool = False,
    employee_id: uuid.UUID | None = None,
) -> list[models.BenefitCategory]:
    """employee_id, when given, scopes the result to only plans that
    employee is explicitly assigned to (see BenefitCategoryAssignment) --
    used for a non-admin's own Benefits & Perks view. None (the admin
    management view) returns every plan in the company regardless of
    assignment, same as before this feature existed."""
    stmt = select(models.BenefitCategory).where(models.BenefitCategory.company_id == company_id)
    if not include_inactive:
        stmt = stmt.where(models.BenefitCategory.is_active.is_(True))
    if employee_id is not None:
        stmt = stmt.where(
            models.BenefitCategory.id.in_(
                select(models.BenefitCategoryAssignment.category_id).where(
                    models.BenefitCategoryAssignment.employee_id == employee_id
                )
            )
        )
    return db.scalars(
        stmt.options(
            selectinload(models.BenefitCategory.items),
            selectinload(models.BenefitCategory.assignments),
        )
    ).all()


def create_benefit_category(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    items: list[str],
    employee_ids: list[uuid.UUID] | None = None,
) -> models.BenefitCategory:
    _advisory_lock(db, "benefit_category", str(company_id), name)
    existing = db.scalar(
        select(models.BenefitCategory).where(
            models.BenefitCategory.company_id == company_id, models.BenefitCategory.name == name
        )
    )
    if existing is not None:
        raise ValueError(f"Benefit category '{name}' already exists")

    category = models.BenefitCategory(
        id=uuid.uuid4(), company_id=company_id, name=name, is_active=True
    )
    db.add(category)
    db.flush()
    for item in items:
        db.add(models.BenefitCategoryItem(id=uuid.uuid4(), category_id=category.id, item=item))
    # Never defaults to "everyone" -- a plan with no employee_ids given is
    # visible to no one until an admin explicitly assigns someone (see
    # BenefitCategoryAssignment's docstring).
    for employee_id in set(employee_ids or []):
        db.add(
            models.BenefitCategoryAssignment(
                id=uuid.uuid4(), category_id=category.id, employee_id=employee_id
            )
        )
    db.flush()
    return category


def set_benefit_category_assignments(
    db: Session, category: models.BenefitCategory, employee_ids: list[uuid.UUID]
) -> None:
    """Replaces a plan's full assignment list with exactly employee_ids --
    adds the new ones, removes whoever was assigned but isn't in the new
    list. Used by Edit Benefit Plan's Assign to Employees control."""
    wanted = set(employee_ids)
    existing_by_employee = {a.employee_id: a for a in category.assignments}
    for employee_id, assignment in existing_by_employee.items():
        if employee_id not in wanted:
            db.delete(assignment)
    for employee_id in wanted:
        if employee_id not in existing_by_employee:
            db.add(
                models.BenefitCategoryAssignment(
                    id=uuid.uuid4(), category_id=category.id, employee_id=employee_id
                )
            )
    db.flush()


def get_benefit_category(db: Session, category_id: uuid.UUID) -> models.BenefitCategory | None:
    return db.get(models.BenefitCategory, category_id)


def get_benefit_category_for_update(
    db: Session, category_id: uuid.UUID
) -> models.BenefitCategory | None:
    """SELECT ... FOR UPDATE -- same double-write protection as
    get_leave_request_for_update/get_regularization_for_update."""
    return db.scalar(
        select(models.BenefitCategory).where(models.BenefitCategory.id == category_id).with_for_update()
    )


def update_benefit_category(
    db: Session, category: models.BenefitCategory, updates: dict
) -> models.BenefitCategory:
    if "name" in updates and updates["name"] != category.name:
        existing = db.scalar(
            select(models.BenefitCategory).where(
                models.BenefitCategory.company_id == category.company_id,
                models.BenefitCategory.name == updates["name"],
                models.BenefitCategory.id != category.id,
            )
        )
        if existing is not None:
            raise ValueError(f"Benefit category '{updates['name']}' already exists")
    for field, value in updates.items():
        setattr(category, field, value)
    db.flush()
    return category


def delete_benefit_category(db: Session, category: models.BenefitCategory) -> None:
    db.delete(category)
    db.flush()


def list_benefit_category_items(
    db: Session, category_id: uuid.UUID
) -> list[models.BenefitCategoryItem]:
    return db.scalars(
        select(models.BenefitCategoryItem).where(
            models.BenefitCategoryItem.category_id == category_id
        )
    ).all()


def create_benefit_category_item(
    db: Session, category_id: uuid.UUID, item: str
) -> models.BenefitCategoryItem:
    row = models.BenefitCategoryItem(id=uuid.uuid4(), category_id=category_id, item=item)
    db.add(row)
    db.flush()
    return row


def get_benefit_category_item_for_update(
    db: Session, item_id: uuid.UUID
) -> models.BenefitCategoryItem | None:
    return db.scalar(
        select(models.BenefitCategoryItem)
        .where(models.BenefitCategoryItem.id == item_id)
        .with_for_update()
    )


def update_benefit_category_item(
    db: Session, item: models.BenefitCategoryItem, text: str
) -> models.BenefitCategoryItem:
    item.item = text
    db.flush()
    return item


def delete_benefit_category_item(db: Session, item: models.BenefitCategoryItem) -> None:
    db.delete(item)
    db.flush()


def upsert_employee_benefits(
    db: Session,
    employee_id: uuid.UUID,
    insurance_plan: str | None = None,
    esop_units: int | None = None,
    dependents_covered: int | None = None,
    learning_budget_total: float | None = None,
    learning_budget_used: float | None = None,
    cab_facility: bool | None = None,
    meal_card: bool | None = None,
    internet_reimbursement: bool | None = None,
    wellness_program: bool | None = None,
) -> models.EmployeeBenefits:
    """Benefits > Employee Enrollment > Enroll/Update Enrollment -- partial
    update, same reasoning as update_company_profile: only the fields
    actually provided are changed."""
    benefits = db.scalar(
        select(models.EmployeeBenefits).where(models.EmployeeBenefits.employee_id == employee_id)
    )
    if benefits is None:
        benefits = models.EmployeeBenefits(id=uuid.uuid4(), employee_id=employee_id)
        db.add(benefits)
    if insurance_plan is not None:
        benefits.insurance_plan = insurance_plan
    if esop_units is not None:
        benefits.esop_units = esop_units
    if dependents_covered is not None:
        benefits.dependents_covered = dependents_covered
    if learning_budget_total is not None:
        benefits.learning_budget_total = learning_budget_total
    if learning_budget_used is not None:
        benefits.learning_budget_used = learning_budget_used
    if cab_facility is not None:
        benefits.cab_facility = cab_facility
    if meal_card is not None:
        benefits.meal_card = meal_card
    if internet_reimbursement is not None:
        benefits.internet_reimbursement = internet_reimbursement
    if wellness_program is not None:
        benefits.wellness_program = wellness_program
    db.flush()
    return benefits


def list_asset_inventory(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.AssetInventoryItem]:
    q = (
        select(models.AssetInventoryItem)
        .where(models.AssetInventoryItem.company_id == company_id)
        .order_by(models.AssetInventoryItem.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def create_asset_inventory_item(
    db: Session,
    company_id: uuid.UUID,
    asset_tag: str,
    asset_type: str,
    model: str,
    status: str,
    purchase_value: float | None,
    purchased_on: datetime.date | None,
) -> models.AssetInventoryItem:
    _advisory_lock(db, "asset_inventory", str(company_id), asset_tag)
    existing = db.scalar(
        select(models.AssetInventoryItem).where(
            models.AssetInventoryItem.company_id == company_id,
            models.AssetInventoryItem.asset_tag == asset_tag,
        )
    )
    if existing is not None:
        raise ValueError(f"Asset tag '{asset_tag}' already exists")

    asset = models.AssetInventoryItem(
        id=uuid.uuid4(),
        company_id=company_id,
        asset_tag=asset_tag,
        asset_type=asset_type,
        model=model,
        status=status,
        purchase_value=purchase_value,
        purchased_on=purchased_on,
    )
    db.add(asset)
    db.flush()
    return asset


def get_asset_inventory_item_by_tag(
    db: Session, company_id: uuid.UUID, asset_tag: str
) -> models.AssetInventoryItem | None:
    return db.scalar(
        select(models.AssetInventoryItem).where(
            models.AssetInventoryItem.company_id == company_id,
            models.AssetInventoryItem.asset_tag == asset_tag,
        )
    )


def get_asset_inventory_item(
    db: Session, company_id: uuid.UUID, item_id: uuid.UUID
) -> models.AssetInventoryItem | None:
    return db.scalar(
        select(models.AssetInventoryItem).where(
            models.AssetInventoryItem.company_id == company_id,
            models.AssetInventoryItem.id == item_id,
        )
    )


def delete_asset_inventory_item(db: Session, item_id: uuid.UUID) -> bool:
    asset = db.get(models.AssetInventoryItem, item_id)
    if asset is None:
        return False
    db.delete(asset)
    db.flush()
    return True


def list_asset_assignments(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.AssetAssignment]:
    q = (
        select(models.AssetAssignment)
        .join(models.AssetInventoryItem, models.AssetInventoryItem.id == models.AssetAssignment.asset_id)
        .where(models.AssetInventoryItem.company_id == company_id)
        .order_by(models.AssetAssignment.assigned_on.desc(), models.AssetAssignment.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def create_asset_assignment(
    db: Session,
    asset_id: uuid.UUID,
    employee_id: uuid.UUID,
    assigned_on: datetime.date,
) -> models.AssetAssignment:
    assignment = models.AssetAssignment(
        id=uuid.uuid4(), asset_id=asset_id, employee_id=employee_id, assigned_on=assigned_on
    )
    db.add(assignment)
    db.flush()
    return assignment


def get_asset_assignment_for_update(
    db: Session, assignment_id: uuid.UUID
) -> models.AssetAssignment | None:
    """SELECT ... FOR UPDATE -- same double-processing protection as
    get_regularization_for_update/get_leave_request_for_update."""
    return db.scalar(
        select(models.AssetAssignment)
        .where(models.AssetAssignment.id == assignment_id)
        .with_for_update()
    )


def list_policy_documents(
    db: Session, company_id: uuid.UUID, doc_kind: str
) -> list[models.PolicyDocument]:
    return db.scalars(
        select(models.PolicyDocument).where(
            models.PolicyDocument.company_id == company_id,
            models.PolicyDocument.doc_kind == doc_kind,
        )
    ).all()


def create_document_record(
    db: Session,
    employee_id: uuid.UUID,
    document_type: str,
    status: str,
    uploaded_on: datetime.date,
    file_url: str | None,
) -> models.DocumentRecord:
    record = models.DocumentRecord(
        id=uuid.uuid4(),
        employee_id=employee_id,
        document_type=document_type,
        status=status,
        uploaded_on=uploaded_on,
        file_url=file_url,
    )
    db.add(record)
    db.flush()
    return record


def list_document_records(
    db: Session,
    company_id: uuid.UUID,
    employee_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.DocumentRecord]:
    """Returns document records scoped to employee_ids when provided (RBAC
    enforcement); None means unrestricted (admin callers)."""
    q = (
        select(models.DocumentRecord)
        .join(models.Employee, models.Employee.id == models.DocumentRecord.employee_id)
        .where(models.Employee.company_id == company_id)
    )
    if employee_ids is not None:
        q = q.where(models.DocumentRecord.employee_id.in_(employee_ids))
    q = q.order_by(models.DocumentRecord.uploaded_on.desc(), models.DocumentRecord.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_document_record(
    db: Session, employee_id: uuid.UUID, document_id: uuid.UUID
) -> models.DocumentRecord | None:
    return db.scalar(
        select(models.DocumentRecord).where(
            models.DocumentRecord.id == document_id,
            models.DocumentRecord.employee_id == employee_id,
        )
    )


def delete_document_record(db: Session, document_id: uuid.UUID) -> None:
    record = db.get(models.DocumentRecord, document_id)
    if record is None:
        return
    if record.file_url:
        from .storage import delete_uploaded_file

        delete_uploaded_file(record.file_url)
    db.delete(record)
    db.flush()


def create_policy_document(
    db: Session,
    company_id: uuid.UUID,
    doc_kind: str,
    name: str,
    version: str | None,
    effective_date: datetime.date | None,
    acknowledgement_pct: float | None,
    file_url: str | None = None,
) -> models.PolicyDocument:
    existing = db.scalar(
        select(models.PolicyDocument).where(
            models.PolicyDocument.company_id == company_id,
            models.PolicyDocument.doc_kind == doc_kind,
            models.PolicyDocument.name == name,
        )
    )
    if existing is not None:
        raise ValueError(f"Document '{name}' already exists")
    document = models.PolicyDocument(
        id=uuid.uuid4(),
        company_id=company_id,
        doc_kind=doc_kind,
        name=name,
        version=version,
        effective_date=effective_date,
        acknowledgement_pct=acknowledgement_pct,
        file_url=file_url,
    )
    db.add(document)
    db.flush()
    return document


def get_policy_document(
    db: Session, company_id: uuid.UUID, document_id: uuid.UUID, doc_kind: str
) -> models.PolicyDocument | None:
    return db.scalar(
        select(models.PolicyDocument).where(
            models.PolicyDocument.id == document_id,
            models.PolicyDocument.company_id == company_id,
            models.PolicyDocument.doc_kind == doc_kind,
        )
    )


def delete_policy_document(db: Session, document_id: uuid.UUID) -> None:
    """Sibling of delete_document_record -- same file-cleanup pattern."""
    document = db.get(models.PolicyDocument, document_id)
    if document is None:
        return
    if document.file_url:
        from .storage import delete_uploaded_file

        delete_uploaded_file(document.file_url)
    db.delete(document)


# ---------------------------------------------------------------------------
# Projects / Work / Travel & Expense
# ---------------------------------------------------------------------------


def list_projects(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.Project]:
    q = (
        select(models.Project)
        .options(
            # PERF-01: many-to-one lookups joined into one round trip.
            joinedload(models.Project.project_manager),
            joinedload(models.Project.branch),
            # customer/category stay selectin: those tables are optional in
            # older tenant schemas (see _optional_tenant_table) and selectin
            # skips the query entirely when no project references them.
            selectinload(models.Project.customer),
            selectinload(models.Project.category),
            joinedload(models.Project.business_unit),
            joinedload(models.Project.department),
        )
        .where(models.Project.company_id == company_id)
    )
    if employee_id is not None:
        allocated_project_ids = select(models.ProjectAllocation.project_id).where(
            models.ProjectAllocation.employee_id == employee_id
        )
        q = q.where(
            or_(
                models.Project.project_manager_id == employee_id,
                models.Project.id.in_(allocated_project_ids),
            )
        )
    q = q.order_by(models.Project.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_project_by_name(db: Session, company_id: uuid.UUID, name: str) -> models.Project | None:
    return db.scalar(
        select(models.Project).where(
            models.Project.company_id == company_id, models.Project.name == name
        )
    )


def list_employees_by_role(
    db: Session, company_id: uuid.UUID, role_names: list[str] | None
) -> list[dict]:
    """Lightweight candidate list for role-scoped assignment dropdowns (Add/
    Edit Project's Project Manager / Team Lead pickers, and Edit Project's
    Team Members picker when role_names is None -- any active employee is
    eligible to be added to a project team) -- deliberately NOT gated by
    can_access_people_module. Picking who's on a project is a
    projects_access concern (the caller already passed
    require_permission_or_action("projects_access", "projects", "edit") to
    reach this), independent of this company's separate People-directory
    visibility policy, which exists to gate the Employee Directory itself,
    not narrow role-lookups used to populate an assignment dropdown.

    Same alphabetical-first tie-break as get_employee_role_name for an
    employee holding more than one of the requested roles."""
    q = (
        select(models.Employee, models.Role.name)
        .join(models.User, models.User.employee_id == models.Employee.id)
        .join(models.UserRole, models.UserRole.user_id == models.User.id)
        .join(models.Role, models.Role.id == models.UserRole.role_id)
        .options(
            selectinload(models.Employee.department),
            selectinload(models.Employee.branch),
            selectinload(models.Employee.designation),
        )
        .where(
            models.Employee.company_id == company_id,
            models.Employee.is_active.is_(True),
        )
        .order_by(models.Employee.first_name, models.Role.name)
    )
    if role_names is not None:
        q = q.where(models.Role.name.in_(role_names))
    rows = db.execute(q).all()
    seen: set[uuid.UUID] = set()
    candidates: list[dict] = []
    for employee, role_name in rows:
        if employee.id in seen:
            continue
        seen.add(employee.id)
        candidates.append({
            "id": employee.id,
            "name": _full_name(employee),
            "designation": employee.designation.name if employee.designation else "—",
            "department": employee.department.name if employee.department else "—",
            "department_id": employee.department_id,
            "business_unit_id": employee.department.business_unit_id if employee.department else None,
            "branch": employee.branch.name if employee.branch else "—",
            "branch_id": employee.branch_id,
            "role": role_name,
        })
    return candidates


def create_project(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    status: str,
    planned_start_date: datetime.date | None = None,
    planned_end_date: datetime.date | None = None,
    description: str | None = None,
    priority: str = "medium",
    is_billable: bool = False,
    project_manager_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    code: str | None = None,
    customer_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    business_unit_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
) -> models.Project:
    existing = db.scalar(
        select(models.Project).where(
            models.Project.company_id == company_id, models.Project.name == name
        )
    )
    if existing is not None:
        raise ValueError(f"Project '{name}' already exists")
    if not code:
        code = re.sub(r"[^A-Z0-9]", "", name.upper())[:10] or "PRJ"
    project = models.Project(
        id=uuid.uuid4(),
        company_id=company_id,
        code=code,
        name=name,
        status=status,
        planned_start_date=planned_start_date,
        planned_end_date=planned_end_date,
        description=description,
        priority=priority,
        is_billable=is_billable,
        is_active=True,
        project_manager_id=project_manager_id,
        branch_id=branch_id,
        customer_id=customer_id,
        category_id=category_id,
        business_unit_id=business_unit_id,
        department_id=department_id,
    )
    db.add(project)
    db.flush()
    return project


def update_project(db: Session, project: models.Project, updates: dict) -> models.Project:
    """Partial update -- same reasoning as update_department/update_branch.
    Caller (routers/projects.py) has already converted start/end/budget
    strings into real date/float values the same way create_project's
    router does, so this is a plain setattr loop."""
    for field, value in updates.items():
        setattr(project, field, value)
    db.flush()
    return project


def allocation_department_branch(allocation: models.ProjectAllocation) -> tuple:
    """Return employee's department/branch (allocation no longer carries these)."""
    return (allocation.employee.department_id, allocation.employee.branch_id)


def resolve_project_team_lead_id(db: Session, project_id: uuid.UUID) -> uuid.UUID | None:
    """Find the Team Lead allocated to this project via its project-role
    assignment (pm_project_roles.name == "Team Lead" on the allocation
    itself), not the allocated employee's unrelated company-wide RBAC role.
    An employee can be the Team Lead of one specific project while holding
    a different org-wide role (or vice versa) -- keying off
    get_employee_role_name here silently lost track of an explicitly
    assigned project Team Lead whenever the two didn't happen to match,
    and also left the Team Members roster showing role "—" for them (see
    set_project_team_lead's identical fix and create_project's call site)."""
    for allocation in list_project_allocations(db, project_id):
        if allocation.project_role is not None and allocation.project_role.name == "Team Lead":
            return allocation.employee_id
    return None


def list_project_allocations(
    db: Session, project_id: uuid.UUID, with_employee: bool = False
) -> list[models.ProjectAllocation]:
    opts = [selectinload(models.ProjectAllocation.project_role)]
    if with_employee:
        opts.append(selectinload(models.ProjectAllocation.employee))
    return db.scalars(
        select(models.ProjectAllocation)
        .options(*opts)
        .where(models.ProjectAllocation.project_id == project_id)
    ).all()


def team_lead_id_from_allocations(allocations) -> uuid.UUID | None:
    """resolve_project_team_lead_id's rule applied to an already-loaded
    allocation list (first allocation whose project role is "Team Lead")."""
    for allocation in allocations:
        if allocation.project_role is not None and allocation.project_role.name == "Team Lead":
            return allocation.employee_id
    return None


def set_project_team_lead(
    db: Session,
    company_id: uuid.UUID,
    project_id: uuid.UUID,
    new_team_lead_id: uuid.UUID,
    start_date: datetime.date | None = None,
) -> None:
    """Edit Project's Team Lead change -- clears the "Team Lead" project-role
    from any allocation carrying it on this project, then tags
    new_team_lead_id's allocation with it (creating one only if they aren't
    on the project yet). Without clearing, resolve_project_team_lead_id's
    "first Team Lead-role allocation wins" resolution could keep returning
    the old Team Lead alongside the new one. Matched by project_role_id/name (not the
    employee's company-wide RBAC role, see resolve_project_team_lead_id's
    identical fix) so this stays correct even when the assigned lead's
    global role isn't literally named "Team Lead"."""
    # The previous Team Lead stays on the project as a plain member (clearing
    # a lead elsewhere demotes too) -- deleting their allocation silently
    # removed them from the project. The new lead's EXISTING allocation is
    # re-tagged instead of adding a second row for the same person
    # (duplicates skewed allocation % and team lists).
    team_lead_role = get_or_create_project_role(db, company_id, "Team Lead")
    existing_for_new_lead = None
    for allocation in list_project_allocations(db, project_id):
        if allocation.project_role is not None and allocation.project_role.name == "Team Lead":
            allocation.project_role_id = None
        if allocation.employee_id == new_team_lead_id and existing_for_new_lead is None:
            existing_for_new_lead = allocation
    db.flush()
    if existing_for_new_lead is not None:
        existing_for_new_lead.project_role_id = team_lead_role.id if team_lead_role else None
        db.flush()
        return
    # M-26: the Team Lead tag reserves no capacity (0%) -- it used to book
    # the lead at 100% on every project they led.
    create_project_allocation(
        db, project_id, new_team_lead_id, 0, start_date=start_date,
        project_role_id=team_lead_role.id if team_lead_role else None,
    )


def delete_project_allocation(db: Session, allocation_id: uuid.UUID) -> bool:
    """Edit Project > Team Members > remove. Returns False (no-op) if the
    allocation is already gone -- lets the router 404 cleanly instead of
    erroring on a stale id."""
    allocation = db.get(models.ProjectAllocation, allocation_id)
    if allocation is None:
        return False
    db.delete(allocation)
    db.flush()
    return True


def create_project_allocation(
    db: Session,
    project_id: uuid.UUID,
    employee_id: uuid.UUID,
    allocation_pct: int,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    project_role_id: uuid.UUID | None = None,
) -> models.ProjectAllocation:
    # pm_resource_allocations has no unique constraint of its own -- without
    # this check, submitting the same employee+project+role twice (e.g. a
    # double-click on Add Team Member, or calling the endpoint again)
    # silently created a second identical row instead of rejecting the
    # duplicate, double-counting them in the Team Members roster and in
    # resolve_project_team_lead_id's scan. Scoped to the same project_role_id
    # (not just employee+project) so an employee already allocated under one
    # role can still be legitimately re-allocated under a different one --
    # e.g. set_project_team_lead promoting an existing "Developer" allocation
    # to "Team Lead" is a different role, not a duplicate.
    existing = db.scalar(
        select(models.ProjectAllocation).where(
            models.ProjectAllocation.project_id == project_id,
            models.ProjectAllocation.employee_id == employee_id,
            models.ProjectAllocation.project_role_id.is_(project_role_id)
            if project_role_id is None
            else models.ProjectAllocation.project_role_id == project_role_id,
            models.ProjectAllocation.is_active.is_(True),
        )
    )
    if existing is not None:
        raise ValueError("This employee is already allocated to this project in that role")
    # M-26: an employee's active allocations can't add up past 100%.
    from . import work_rules

    if (err := work_rules.capacity_error(db, employee_id, allocation_pct)):
        raise ValueError(err)
    allocation = models.ProjectAllocation(
        id=uuid.uuid4(),
        project_id=project_id,
        employee_id=employee_id,
        project_role_id=project_role_id,
        allocation_pct=allocation_pct,
        start_date=start_date or employee_company_today(db, employee_id),
        end_date=end_date,
        is_active=True,
    )
    db.add(allocation)
    db.flush()
    return allocation


def get_or_create_customer(db: Session, company_id: uuid.UUID, name: str) -> models.Customer | None:
    """Add/Edit Project's free-text Client field resolves to a real
    core_customers row here instead of being discarded (see
    routers/projects.py's old "Drop fields removed from DB schema" comment,
    now removed -- customer_id is a real pm_projects column). Matches
    case-insensitively so retyping the same client with different casing
    doesn't pile up duplicate rows; core_customers has no unique constraint
    on name to enforce this at the DB level. Returns None (falls back to
    "—", exactly like before this existed) if this tenant's schema doesn't
    have core_customers at all -- see _optional_tenant_table."""
    name = (name or "").strip()
    if not name:
        return None

    def _do():
        existing = db.scalar(
            select(models.Customer).where(
                models.Customer.company_id == company_id,
                func.lower(models.Customer.name) == name.lower(),
            )
        )
        if existing is not None:
            return existing
        code = re.sub(r"[^A-Z0-9]", "", name.upper())[:20] or "CUST"
        base_code, suffix = code, 1
        while db.scalar(
            select(models.Customer).where(
                models.Customer.company_id == company_id, models.Customer.code == code
            )
        ) is not None:
            suffix += 1
            code = f"{base_code[:17]}{suffix}"
        customer = models.Customer(id=uuid.uuid4(), company_id=company_id, code=code, name=name, is_active=True)
        db.add(customer)
        db.flush()
        return customer

    return _optional_tenant_table("core_customers (Client)", db, _do)


def get_or_create_project_category(db: Session, company_id: uuid.UUID, name: str) -> models.ProjectCategory | None:
    """Add/Edit Project's Type dropdown resolves to a real
    pm_project_categories row here instead of being discarded --
    UNIQUE(company_id, name) on the real table backs the lookup. Returns
    None (falls back to "—") if this tenant's schema doesn't have
    pm_project_categories -- confirmed missing on at least one real
    tenant, see _optional_tenant_table."""
    name = (name or "").strip()
    if not name:
        return None

    def _do():
        existing = db.scalar(
            select(models.ProjectCategory).where(
                models.ProjectCategory.company_id == company_id,
                models.ProjectCategory.name == name,
            )
        )
        if existing is not None:
            return existing
        category = models.ProjectCategory(id=uuid.uuid4(), company_id=company_id, name=name, is_active=True)
        db.add(category)
        db.flush()
        return category

    return _optional_tenant_table("pm_project_categories (Type)", db, _do)


_DEFAULT_PROJECT_CATEGORIES = ["Product", "Client — Fixed Bid", "Client — T&M", "Internal"]


def get_or_seed_company_project_categories(db: Session, company_id: uuid.UUID) -> list[models.ProjectCategory]:
    """Auto-provisions a company's default Project Type rows the first time
    they're needed -- same lazy-provisioning shape as
    get_or_seed_company_bands, so a brand-new company's Add Project "Type"
    dropdown still opens with the same options the app always offered
    (previously a hardcoded list) instead of an empty one, while remaining
    fully create/edit/deactivate-able per company from there on."""
    existing = db.scalars(
        select(models.ProjectCategory).where(models.ProjectCategory.company_id == company_id)
    ).all()
    if existing:
        return list(existing)

    _advisory_lock(db, "project_category_seed", str(company_id))
    # Re-check under the lock -- a concurrent first request may have already
    # seeded these while this one waited.
    existing = db.scalars(
        select(models.ProjectCategory).where(models.ProjectCategory.company_id == company_id)
    ).all()
    if existing:
        return list(existing)

    seeded: list[models.ProjectCategory] = []
    for name in _DEFAULT_PROJECT_CATEGORIES:
        category = models.ProjectCategory(id=uuid.uuid4(), company_id=company_id, name=name, is_active=True)
        db.add(category)
        seeded.append(category)
    db.flush()
    return seeded


def list_project_categories(
    db: Session, company_id: uuid.UUID, active_only: bool = False
) -> list[models.ProjectCategory]:
    """Backs GET /api/project-categories -- Add/Edit Project's Type dropdown
    (active_only=true) and the Manage Project Types dialog (active_only=
    false, so a deactivated type can still be found and reactivated)."""
    categories = get_or_seed_company_project_categories(db, company_id)
    if active_only:
        categories = [c for c in categories if c.is_active]
    return sorted(categories, key=lambda c: c.name)


def get_project_category_for_update(
    db: Session, category_id: uuid.UUID, company_id: uuid.UUID
) -> models.ProjectCategory | None:
    """Row-locked read (SELECT ... FOR UPDATE), same pattern as
    get_band_for_update -- serializes concurrent edits to the same type."""
    return db.scalar(
        select(models.ProjectCategory)
        .where(
            models.ProjectCategory.id == category_id,
            models.ProjectCategory.company_id == company_id,
        )
        .with_for_update()
    )


def create_project_category(
    db: Session, company_id: uuid.UUID, name: str, user_id: uuid.UUID | None = None
) -> models.ProjectCategory:
    """Manage Project Types > Add. Distinct from get_or_create_project_category
    above (used internally to resolve Add/Edit Project's Type selection into
    a row, tolerant of "already exists"): this one is for the management
    dialog itself and raises on a duplicate name. UNIQUE(company_id, name)
    on the real table is the authoritative race guard (see the router's own
    IntegrityError handling) -- this pre-check just gives a clean 409 with
    the actual name in the common, non-racing case."""
    name = name.strip()
    get_or_seed_company_project_categories(db, company_id)
    conflict = db.scalar(
        select(models.ProjectCategory).where(
            models.ProjectCategory.company_id == company_id,
            func.lower(models.ProjectCategory.name) == name.lower(),
        )
    )
    if conflict is not None:
        raise ValueError(f"Project Type '{name}' already exists")
    category = models.ProjectCategory(id=uuid.uuid4(), company_id=company_id, name=name, is_active=True, created_by=user_id, updated_by=user_id)
    db.add(category)
    db.flush()
    return category


def update_project_category(
    db: Session, category: models.ProjectCategory, updates: dict, user_id: uuid.UUID | None = None
) -> models.ProjectCategory:
    """Rename and/or toggle active/inactive -- both through this one PATCH,
    matching PATCH /api/bands/{id}'s shape. Re-validates name uniqueness
    within the company if it's changing."""
    if "name" in updates:
        new_name = (updates["name"] or "").strip()
        conflict = db.scalar(
            select(models.ProjectCategory).where(
                models.ProjectCategory.company_id == category.company_id,
                func.lower(models.ProjectCategory.name) == new_name.lower(),
                models.ProjectCategory.id != category.id,
            )
        )
        if conflict is not None:
            raise ValueError(f"Project Type '{new_name}' already exists")
        updates["name"] = new_name
    for field, value in updates.items():
        setattr(category, field, value)
    category.updated_by = user_id
    db.flush()
    return category


def get_or_create_project_role(db: Session, company_id: uuid.UUID, name: str) -> models.ProjectRole | None:
    """Edit Project > Team Members' typed Role resolves to a real
    pm_project_roles row here instead of being discarded --
    UNIQUE(company_id, name) on the real table backs the lookup. Returns
    None (falls back to "—") if this tenant's schema doesn't have
    pm_project_roles -- confirmed missing on at least one real tenant, see
    _optional_tenant_table."""
    name = (name or "").strip()
    if not name:
        return None

    def _do():
        existing = db.scalar(
            select(models.ProjectRole).where(
                models.ProjectRole.company_id == company_id,
                models.ProjectRole.name == name,
            )
        )
        if existing is not None:
            return existing
        role = models.ProjectRole(id=uuid.uuid4(), company_id=company_id, name=name, is_active=True)
        db.add(role)
        db.flush()
        return role

    return _optional_tenant_table("pm_project_roles (Team Member role)", db, _do)


def record_project_status_change(
    db: Session,
    project_id: uuid.UUID,
    from_status: str | None,
    to_status: str,
    changed_by: uuid.UUID | None,
    remarks: str | None = None,
) -> None:
    """Insert into pm_project_status_history -- a status-change audit trail
    table that already existed but had no model/CRUD ever writing to it.
    Best-effort, same convention as create_audit_log/create_notification:
    never the reason a status update fails -- including when this tenant's
    schema doesn't have pm_project_status_history at all (confirmed
    missing on at least one real tenant), see _optional_tenant_table."""
    if from_status == to_status:
        return

    def _do():
        db.add(models.ProjectStatusHistory(
            id=uuid.uuid4(),
            project_id=project_id,
            from_status=from_status,
            to_status=to_status,
            changed_by=changed_by,
            changed_at=datetime.datetime.now(datetime.timezone.utc),
            remarks=remarks,
        ))
        db.flush()

    _optional_tenant_table("pm_project_status_history", db, _do)


_BUDGET_SUFFIX_MULTIPLIERS = {
    "": 1,
    "k": 1_000,
    "l": 100_000, "lac": 100_000, "lacs": 100_000, "lakh": 100_000, "lakhs": 100_000,
    "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000,
    "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
}


def parse_budget_amount(text: str | None) -> float | None:
    """Parses Add/Edit Project's free-text Budget field (e.g. "₹95L",
    "9,500,000", "95 Lakh") into a plain numeric amount for
    pm_project_budgets.planned_amount -- that column is a real number, this
    field has always been free text. Returns None for blank or unparseable
    input; the caller decides whether blank means "no budget set" or
    unparseable is a validation error."""
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace("₹", "").replace("Rs.", "").replace("Rs", "")
    cleaned = cleaned.replace(",", "").strip()
    match = re.match(r"^([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)$", cleaned)
    if not match:
        return None
    number_part, suffix = match.groups()
    multiplier = _BUDGET_SUFFIX_MULTIPLIERS.get(suffix.lower())
    if multiplier is None:
        return None
    return float(number_part) * multiplier


def format_budget_amount(amount: float | None) -> str:
    if amount is None:
        return "—"
    return f"₹{amount:,.0f}"


def get_project_budget(db: Session, project_id: uuid.UUID) -> models.ProjectBudget | None:
    """The single active pm_project_budgets row this app keeps per project
    -- see models.ProjectBudget's own docstring on why only one. Returns
    None (falls back to "—") if this tenant's schema doesn't have
    pm_project_budgets -- confirmed missing on at least one real tenant,
    see _optional_tenant_table."""

    def _do():
        return db.scalar(
            select(models.ProjectBudget)
            .where(
                models.ProjectBudget.project_id == project_id,
                models.ProjectBudget.is_active.is_(True),
            )
            .order_by(models.ProjectBudget.id.desc())
        )

    return _optional_tenant_table("pm_project_budgets", db, _do, table="pm_project_budgets")


def get_project_budgets_bulk(
    db: Session, project_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, models.ProjectBudget]:
    """Bulk get_project_budget: {project_id: the active row with the highest
    id} -- same pick rule (is_active, ORDER BY id DESC, first) in ONE query
    for every project instead of one query + SAVEPOINT per project
    (PERF-01/02). Empty dict if this tenant has no pm_project_budgets."""
    ids = list({i for i in project_ids if i is not None})
    if not ids or not tenant_table_exists(db, "pm_project_budgets"):
        return {}
    rows = db.scalars(
        select(models.ProjectBudget)
        .where(
            models.ProjectBudget.project_id.in_(ids),
            models.ProjectBudget.is_active.is_(True),
        )
        .order_by(models.ProjectBudget.project_id, models.ProjectBudget.id.desc())
    ).all()
    out: dict[uuid.UUID, models.ProjectBudget] = {}
    for row in rows:
        out.setdefault(row.project_id, row)
    return out


def get_project_progress_bulk(db: Session, project_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Bulk get_project_progress: one GROUP BY over pm_tasks for every
    project. Identical arithmetic (Python round() of done/total over active
    cards, 0 with no cards) -- projects with no active cards are simply
    absent, callers use .get(id, 0)."""
    ids = list({i for i in project_ids if i is not None})
    if not ids:
        return {}
    rows = db.execute(
        select(
            models.TaskBoardCard.project_id,
            func.count().label("total"),
            func.count().filter(models.TaskBoardCard.status == "done").label("done"),
        )
        .where(
            models.TaskBoardCard.project_id.in_(ids),
            models.TaskBoardCard.is_active == True,  # noqa: E712
        )
        .group_by(models.TaskBoardCard.project_id)
    ).all()
    return {r.project_id: round(100 * r.done / r.total) for r in rows if r.total}


def resolve_project_team_lead_ids_bulk(
    db: Session, project_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Bulk resolve_project_team_lead_id: {project_id: employee_id of the
    first allocation (table order, exactly like list_project_allocations'
    unordered scan) whose project role is "Team Lead"}. Two queries total
    regardless of project count (allocations, then their roles) instead of
    2 per project (PERF-01/02/03)."""
    ids = list({i for i in project_ids if i is not None})
    if not ids:
        return {}
    allocs = db.execute(
        select(
            models.ProjectAllocation.project_id,
            models.ProjectAllocation.employee_id,
            models.ProjectAllocation.project_role_id,
        ).where(models.ProjectAllocation.project_id.in_(ids))
    ).all()
    role_ids = {a.project_role_id for a in allocs if a.project_role_id is not None}
    if not role_ids:
        return {}
    team_lead_role_ids = set(
        db.scalars(
            select(models.ProjectRole.id).where(
                models.ProjectRole.id.in_(role_ids), models.ProjectRole.name == "Team Lead"
            )
        ).all()
    )
    out: dict[uuid.UUID, uuid.UUID] = {}
    for a in allocs:
        if a.project_role_id in team_lead_role_ids and a.project_id not in out:
            out[a.project_id] = a.employee_id
    return out


def set_project_budget_amount(
    db: Session, company_id: uuid.UUID, project_id: uuid.UUID, amount: float
) -> models.ProjectBudget | None:
    """Best-effort, same convention as record_project_status_change: never
    the reason a project update fails.

    Fiscal-year resolution is deliberately a SEPARATE best-effort step from
    the budget save itself, in its own savepoint: pm_project_budgets.
    fiscal_year_id is nullable, so a tenant missing fin_fiscal_years
    entirely (confirmed live: "Infyq") should still get its budget amount
    saved, just without a fiscal-year link on that row -- bundling both
    into one savepoint (the original bug here) meant the missing
    fin_fiscal_years table silently prevented the budget from ever being
    saved at all for that tenant, even though pm_project_budgets itself
    was present and working."""
    fiscal_year_id = None
    fiscal_year = _optional_tenant_table(
        "fin_fiscal_years", db, lambda: get_or_create_fiscal_year(db, company_id)
    )
    if fiscal_year is not None:
        fiscal_year_id = fiscal_year.id

    def _do():
        budget = db.scalar(
            select(models.ProjectBudget)
            .where(
                models.ProjectBudget.project_id == project_id,
                models.ProjectBudget.is_active.is_(True),
            )
            .order_by(models.ProjectBudget.id.desc())
        )
        if budget is not None:
            budget.planned_amount = amount
            budget.fiscal_year_id = fiscal_year_id
            db.flush()
            return budget
        budget = models.ProjectBudget(
            id=uuid.uuid4(),
            project_id=project_id,
            fiscal_year_id=fiscal_year_id,
            budget_type="opex",
            planned_amount=amount,
            is_active=True,
        )
        db.add(budget)
        db.flush()
        return budget

    return _optional_tenant_table("pm_project_budgets", db, _do)


def list_project_status_history(db: Session, project_id: uuid.UUID) -> list[models.ProjectStatusHistory]:
    def _do():
        return db.scalars(
            select(models.ProjectStatusHistory)
            .where(models.ProjectStatusHistory.project_id == project_id)
            .order_by(models.ProjectStatusHistory.changed_at.desc())
        ).all()

    return _optional_tenant_table("pm_project_status_history", db, _do, default=[])


def create_work_entry(
    db: Session,
    timesheet_id: uuid.UUID,
    project_id: uuid.UUID,
    entry_date: datetime.date,
    category: str | None = None,
    start_time: datetime.time | None = None,
    end_time: datetime.time | None = None,
    hours: float = 8.0,
    description: str | None = None,
    is_billable: bool = True,
    task_id: uuid.UUID | None = None,
    task_name: str | None = None,
) -> models.WorkEntry:
    entry = models.WorkEntry(
        id=uuid.uuid4(),
        timesheet_id=timesheet_id,
        project_id=project_id,
        task_id=task_id,
        task_name=task_name,
        entry_date=entry_date,
        category=category,
        start_time=start_time,
        end_time=end_time,
        hours=hours,
        description=description,
        is_billable=is_billable,
        # Work Entry no longer requires Reporting Manager approval -- see
        # routers.work.create_work_entry's docstring. "approved" (a value
        # the pm_time_entries status CHECK constraint already allows) is
        # set immediately instead of the model's own "pending" default, so
        # every dashboard/report/list that already filters/reads `status`
        # sees a normal, final entry with no code changes needed there.
        status="approved",
    )
    db.add(entry)
    db.flush()
    return entry


def get_work_entry_backdate_days(db: Session, company_id: uuid.UUID) -> int | None:
    settings_row = get_company_settings(db, company_id)
    return settings_row.work_entry_backdate_days if settings_row else None


def validate_work_entry_date(
    db: Session, company_id: uuid.UUID, entry_date: datetime.date
) -> None:
    """Enforces the Organization Owner-configured backdate window (see
    CompanySettings.work_entry_backdate_days) for Work Entry/Timesheet
    submission: an employee who missed a day can still log/submit it
    afterward, up to however many days back the org allows, but never for
    a future date. Raises ValueError (routers/work.py turns this into a
    400) instead of silently clamping the date. A None limit (not
    configured) means unrestricted, matching every tenant that hasn't set
    this -- the same fail-open convention as every other optional
    CompanySettings field in this app."""
    today = company_today(db, company_id)  # DATA-03: not the server-local date
    if entry_date > today:
        raise ValueError("Cannot submit a Work Entry/Timesheet for a future date")
    limit_days = get_work_entry_backdate_days(db, company_id)
    if limit_days is not None and entry_date < today - datetime.timedelta(days=limit_days):
        raise ValueError(
            f"This date is too far in the past -- entries can only be "
            f"backdated up to {limit_days} day{'s' if limit_days != 1 else ''}"
        )


def list_work_entries(
    db: Session,
    company_id: uuid.UUID,
    status: str | None = None,
    employee_ids: list[uuid.UUID] | None = None,
    from_date: datetime.date | None = None,
    to_date: datetime.date | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.WorkEntry]:
    q = (
        select(models.WorkEntry)
        .join(models.Project, models.Project.id == models.WorkEntry.project_id)
        # Always an INNER join, even when employee_ids is None (org-wide
        # visibility, e.g. the Owner role): a WorkEntry whose timesheet_id
        # doesn't resolve to a real Timesheet row (orphaned -- e.g. its
        # Timesheet was deleted directly without cascading) previously
        # slipped through here whenever no employee_ids restriction was
        # applied, since only the restricted branch below used to join
        # Timesheet at all. _work_entry_out then read entry.timesheet.
        # employee_id, got None back from the unmatched relationship, and
        # WorkEntryOut's non-nullable employee_id field raised a Pydantic
        # ValidationError -- a real 500 on GET /api/work-entries for any
        # org-wide viewer, the moment even one such orphaned row existed
        # anywhere in the company's data.
        .join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
        .options(
            selectinload(models.WorkEntry.project),
            selectinload(models.WorkEntry.task),
            # Was missing -- _work_entry_out reads entry.timesheet.employee_id
            # for every row, which lazy-loaded one query per row without this.
            selectinload(models.WorkEntry.timesheet),
        )
        .where(models.Project.company_id == company_id)
    )
    if status is not None:
        q = q.where(models.WorkEntry.status == status)
    if employee_ids is not None:
        # WorkEntry has no employee_id column of its own -- only reachable
        # via its Timesheet (see _work_entry_out's identical resolution).
        # This parameter was accepted but never actually applied: every
        # caller (list_work_entries's own router) computes employee_ids
        # from get_visible_employee_ids_for_docs expecting the result
        # scoped to it, but every work entry company-wide was returned
        # regardless of who asked -- a real data-visibility gap, not just
        # a performance one.
        q = q.where(models.Timesheet.employee_id.in_(employee_ids))
    if from_date is not None:
        q = q.where(models.WorkEntry.entry_date >= from_date)
    if to_date is not None:
        q = q.where(models.WorkEntry.entry_date <= to_date)
    q = q.order_by(models.WorkEntry.entry_date.desc(), models.WorkEntry.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_work_entry_for_update(
    db: Session, work_entry_id: uuid.UUID, company_id: uuid.UUID | None = None
) -> models.WorkEntry | None:
    """SELECT ... FOR UPDATE -- same double-approval protection as
    get_timesheet_for_update/get_regularization_for_update. Company-scoped
    (C14) through the parent timesheet: explicit `company_id`, else the
    authenticated caller's company."""
    company_id = company_id or _session_company_id(db)
    q = select(models.WorkEntry).where(models.WorkEntry.id == work_entry_id)
    if company_id is not None:
        q = q.join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id).where(
            models.Timesheet.company_id == company_id
        )
    return db.scalar(q.with_for_update(of=models.WorkEntry))


def create_overtime_request(
    db: Session,
    employee_id: uuid.UUID,
    work_date: datetime.date,
    hours: float,
    reason: str | None,
    start_time: "datetime.time | None" = None,
    end_time: "datetime.time | None" = None,
) -> models.OvertimeRequest:
    request = models.OvertimeRequest(
        id=uuid.uuid4(),
        employee_id=employee_id,
        work_date=work_date,
        hours=hours,
        reason=reason,
        start_time=start_time,
        end_time=end_time,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(request)
    db.flush()
    return request


def list_overtime_requests(
    db: Session,
    company_id: uuid.UUID,
    employee_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.OvertimeRequest]:
    q = (
        select(models.OvertimeRequest)
        .join(models.Employee, models.Employee.id == models.OvertimeRequest.employee_id)
        .where(models.Employee.company_id == company_id)
    )
    if employee_ids is not None:
        q = q.where(models.OvertimeRequest.employee_id.in_(employee_ids))
    q = q.order_by(models.OvertimeRequest.created_at.desc(), models.OvertimeRequest.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_overtime_request_for_update(
    db: Session, request_id: uuid.UUID
) -> models.OvertimeRequest | None:
    return db.scalar(
        select(models.OvertimeRequest).where(models.OvertimeRequest.id == request_id).with_for_update()
    )


def create_timesheet(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    week_start: datetime.date,
    total_hours: float,
    billable_hours: float,
    status: str = "submitted",
) -> models.Timesheet:
    """pm_timesheets has a (employee_id, week_start) unique constraint.
    Pre-check + advisory lock turns a would-be IntegrityError into a clean
    409. status="draft" is what Add Work Entry's auto-vivify path passes
    (see create_work_entry) -- logging a single work entry must not put a
    timesheet in front of the Reporting Manager before the employee has
    actually clicked Submit Timesheet; only submit_timesheet below ever
    transitions a row to "submitted"."""
    _advisory_lock(db, "timesheet", str(employee_id), week_start.isoformat())

    existing = db.scalar(
        select(models.Timesheet).where(
            models.Timesheet.employee_id == employee_id,
            models.Timesheet.week_start == week_start,
        )
    )
    if existing is not None:
        raise ValueError(f"Timesheet for week '{week_start.isoformat()}' already exists")

    timesheet = models.Timesheet(
        id=uuid.uuid4(),
        company_id=company_id,
        employee_id=employee_id,
        week_start=week_start,
        status=status,
        total_hours=total_hours,
        billable_hours=billable_hours,
        is_active=True,
    )
    db.add(timesheet)
    db.flush()
    return timesheet


def compute_timesheet_hours(
    db: Session, employee_id: uuid.UUID, week_start: datetime.date
) -> tuple[float, float]:
    """Total/Billable Hours for an employee's week, summed live from their
    actual pm_time_entries (Work Entries) rather than trusting whatever a
    client sends -- Submit Timesheet, and every draft-status row in the
    Work & Timesheet list, always reflect real logged work. Returns (0.0,
    0.0) when no timesheet row exists yet for this week at all (nothing to
    sum), which is the correct answer, not a missing-data error."""
    timesheet_id = db.scalar(
        select(models.Timesheet.id).where(
            models.Timesheet.employee_id == employee_id,
            models.Timesheet.week_start == week_start,
        )
    )
    if timesheet_id is None:
        return 0.0, 0.0
    total = db.scalar(
        select(func.coalesce(func.sum(models.WorkEntry.hours), 0)).where(
            models.WorkEntry.timesheet_id == timesheet_id
        )
    )
    billable = db.scalar(
        select(func.coalesce(func.sum(models.WorkEntry.hours), 0)).where(
            models.WorkEntry.timesheet_id == timesheet_id,
            models.WorkEntry.is_billable.is_(True),
        )
    )
    return float(total), float(billable)


def compute_timesheet_hours_bulk(
    db: Session, keys: "Iterable[tuple[uuid.UUID, datetime.date]]"
) -> "dict[tuple[uuid.UUID, datetime.date], tuple[float, float]]":
    """Bulk compute_timesheet_hours for many (employee_id, week_start)
    keys: 2 queries total instead of 3 per timesheet row (PERF-09). Same
    rule per key -- the first matching pm_timesheets row, then live
    total/billable sums over its pm_time_entries; keys with no timesheet
    are absent (callers default to (0.0, 0.0))."""
    wanted = {k for k in keys if k[0] is not None and k[1] is not None}
    if not wanted:
        return {}
    ts_rows = db.execute(
        select(models.Timesheet.id, models.Timesheet.employee_id, models.Timesheet.week_start).where(
            models.Timesheet.employee_id.in_({k[0] for k in wanted}),
            models.Timesheet.week_start.in_({k[1] for k in wanted}),
        )
    ).all()
    ts_by_key: dict = {}
    for row in ts_rows:
        key = (row.employee_id, row.week_start)
        if key in wanted and key not in ts_by_key:
            ts_by_key[key] = row.id
    if not ts_by_key:
        return {}
    sums = {
        r.timesheet_id: (float(r.total), float(r.billable))
        for r in db.execute(
            select(
                models.WorkEntry.timesheet_id,
                func.coalesce(func.sum(models.WorkEntry.hours), 0).label("total"),
                func.coalesce(
                    func.sum(models.WorkEntry.hours).filter(models.WorkEntry.is_billable.is_(True)), 0
                ).label("billable"),
            )
            .where(models.WorkEntry.timesheet_id.in_(set(ts_by_key.values())))
            .group_by(models.WorkEntry.timesheet_id)
        ).all()
    }
    return {key: sums.get(ts_id, (0.0, 0.0)) for key, ts_id in ts_by_key.items()}


def resync_timesheet_hours(
    db: Session, employee_id: uuid.UUID, week_start: "datetime.date | None"
) -> None:
    """Keeps a Timesheet's STORED total_hours/billable_hours snapshot in
    sync after one of its Work Entries is edited/moved/deleted.

    _timesheet_out only recomputes live for a still-'draft' timesheet;
    once submitted/approved/rejected, the stored snapshot is frozen on
    purpose (see its own docstring) -- so without this, editing or
    deleting a Work Entry under an already-approved Timesheet (the normal
    case now that Timesheet is approved immediately on submission) would
    silently leave that Timesheet's displayed totals stale. A no-op if no
    timesheet row exists for that week (nothing to resync) or week_start
    is None (entry had no timesheet, e.g. already deleted)."""
    if week_start is None:
        return
    timesheet = db.scalar(
        select(models.Timesheet).where(
            models.Timesheet.employee_id == employee_id,
            models.Timesheet.week_start == week_start,
        )
    )
    if timesheet is None:
        return
    total, billable = compute_timesheet_hours(db, employee_id, week_start)
    timesheet.total_hours = total
    timesheet.billable_hours = billable
    db.flush()


def submit_timesheet(
    db: Session, company_id: uuid.UUID, employee_id: uuid.UUID, week_start: datetime.date
) -> models.Timesheet:
    """Submit Timesheet -- Total Hours/Billable Hours are always the live
    sum of that week's Work Entries (compute_timesheet_hours), never a
    client-supplied number. If Add Work Entry already auto-vivified a
    draft row for this week (the common case -- an employee usually logs
    entries throughout the week before submitting), this finalizes that
    same row in place instead of colliding with create_timesheet's own
    already-exists guard.

    Timesheet no longer requires Reporting Manager approval -- see
    routers.work.create_timesheet's docstring -- so submitting goes
    straight to "approved" (a value the pm_timesheets status CHECK
    constraint already allows) instead of the old "submitted"
    awaiting-decision state.

    Re-submitting an already-"approved" week is allowed (idempotent
    refresh) rather than rejected -- the old "already submitted" guard
    dates from when "submitted" meant "awaiting a manager's decision" and
    resubmitting mid-review would have been meaningless; now that there's
    no decision to wait on, Work Entries under an approved week can still
    be edited/added/removed afterward (see routers.work.edit_work_entry,
    which already keeps the stored total/billable hours in sync via
    resync_timesheet_hours), so letting the employee explicitly
    re-Submit to recompute/confirm those totals is a legitimate "update"
    action, not a stale duplicate click. Only a "rejected" week (an
    explicit admin override via update_timesheet) is still blocked here --
    that decision should require going back through the same admin
    override path to reverse, not be silently overwritten by a plain
    re-submit."""
    _advisory_lock(db, "timesheet", str(employee_id), week_start.isoformat())
    total_hours, billable_hours = compute_timesheet_hours(db, employee_id, week_start)
    now = datetime.datetime.now(datetime.timezone.utc)

    existing = db.scalar(
        select(models.Timesheet).where(
            models.Timesheet.employee_id == employee_id,
            models.Timesheet.week_start == week_start,
        )
    )
    if existing is not None:
        if existing.status == "rejected":
            raise ValueError(
                f"Timesheet for week '{week_start.isoformat()}' was rejected "
                "by an administrator; contact them before resubmitting."
            )
        existing.total_hours = total_hours
        existing.billable_hours = billable_hours
        existing.status = "approved"
        existing.submitted_at = now
        db.flush()
        return existing

    timesheet = models.Timesheet(
        id=uuid.uuid4(),
        company_id=company_id,
        employee_id=employee_id,
        week_start=week_start,
        status="approved",
        total_hours=total_hours,
        billable_hours=billable_hours,
        submitted_at=now,
        is_active=True,
    )
    db.add(timesheet)
    db.flush()
    return timesheet


def list_timesheets(
    db: Session,
    company_id: uuid.UUID,
    status: str | None = None,
    employee_ids: list[uuid.UUID] | None = None,
    from_date: datetime.date | None = None,
    to_date: datetime.date | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.Timesheet]:
    q = (
        select(models.Timesheet)
        .join(models.Employee, models.Employee.id == models.Timesheet.employee_id)
        .where(models.Employee.company_id == company_id)
    )
    if status is not None:
        q = q.where(models.Timesheet.status == status)
    if employee_ids is not None:
        q = q.where(models.Timesheet.employee_id.in_(employee_ids))
    if from_date is not None:
        # A week (week_start..week_start+6) OVERLAPS [from_date, to_date] if
        # its own start is at or before to_date and its end (start + 6) is
        # at or after from_date -- so a week that only partially falls
        # inside the requested range is still included, not dropped.
        q = q.where(models.Timesheet.week_start >= from_date - datetime.timedelta(days=6))
    if to_date is not None:
        q = q.where(models.Timesheet.week_start <= to_date)
    q = q.order_by(models.Timesheet.week_start.desc(), models.Timesheet.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_timesheet_for_update(
    db: Session, timesheet_id: uuid.UUID, company_id: uuid.UUID | None = None
) -> models.Timesheet | None:
    """SELECT ... FOR UPDATE -- same double-approval protection as
    get_regularization_for_update/get_leave_request_for_update.
    Company-scoped (C14): explicit `company_id`, else the authenticated
    caller's company."""
    company_id = company_id or _session_company_id(db)
    q = select(models.Timesheet).where(models.Timesheet.id == timesheet_id)
    if company_id is not None:
        q = q.where(models.Timesheet.company_id == company_id)
    return db.scalar(q.with_for_update())


def create_salary_revision_request(
    db: Session,
    employee_id: uuid.UUID,
    current_ctc: int,
    proposed_ctc: int,
    reason: str,
) -> models.SalaryRevisionRequest:
    request = models.SalaryRevisionRequest(
        id=uuid.uuid4(),
        employee_id=employee_id,
        current_ctc=current_ctc,
        proposed_ctc=proposed_ctc,
        reason=reason,
        status="pending",
    )
    db.add(request)
    db.flush()
    return request


def list_salary_revision_requests(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.SalaryRevisionRequest]:
    q = (
        select(models.SalaryRevisionRequest)
        .join(models.Employee, models.Employee.id == models.SalaryRevisionRequest.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.SalaryRevisionRequest.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_salary_revision_request_for_update(
    db: Session, request_id: uuid.UUID
) -> models.SalaryRevisionRequest | None:
    return db.scalar(
        select(models.SalaryRevisionRequest)
        .where(models.SalaryRevisionRequest.id == request_id)
        .with_for_update()
    )


def create_asset_request(
    db: Session, employee_id: uuid.UUID, asset_type: str, justification: str | None
) -> models.AssetRequestModel:
    request = models.AssetRequestModel(
        id=uuid.uuid4(),
        employee_id=employee_id,
        asset_type=asset_type,
        justification=justification,
        status="pending",
    )
    db.add(request)
    db.flush()
    return request


def list_asset_requests(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.AssetRequestModel]:
    q = (
        select(models.AssetRequestModel)
        .join(models.Employee, models.Employee.id == models.AssetRequestModel.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.AssetRequestModel.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_asset_request_for_update(
    db: Session, request_id: uuid.UUID
) -> models.AssetRequestModel | None:
    return db.scalar(
        select(models.AssetRequestModel).where(models.AssetRequestModel.id == request_id).with_for_update()
    )


def create_asset_recovery_deduction(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    asset_assignment_id: uuid.UUID | None,
    amount: float,
    reason: str | None,
    target_period_month: int,
    target_period_year: int,
    created_by: uuid.UUID,
) -> models.AssetRecoveryDeduction:
    """Assets > Asset Recovery Deduction. Starts 'pending' -- only once
    decide_asset_recovery_deduction approves it does it become eligible
    for get_approved_asset_recovery_amount / automatic payslip inclusion."""
    deduction = models.AssetRecoveryDeduction(
        id=uuid.uuid4(),
        company_id=company_id,
        employee_id=employee_id,
        asset_assignment_id=asset_assignment_id,
        amount=amount,
        reason=reason,
        status="pending",
        target_period_month=target_period_month,
        target_period_year=target_period_year,
        created_by=created_by,
    )
    db.add(deduction)
    db.flush()
    return deduction


def asset_recovery_deduction_out(
    db: Session, deduction: models.AssetRecoveryDeduction
) -> dict:
    asset_label = None
    if deduction.asset_assignment_id:
        assignment = db.get(models.AssetAssignment, deduction.asset_assignment_id)
        if assignment is not None:
            asset = db.get(models.AssetInventoryItem, assignment.asset_id)
            asset_label = f"{asset.asset_type} ({asset.asset_tag})" if asset else None
    return {
        "id": deduction.id,
        "employee_id": deduction.employee_id,
        "employee_name": employee_display_name(db, deduction.employee_id),
        "asset_assignment_id": deduction.asset_assignment_id,
        "asset_label": asset_label,
        "amount": float(deduction.amount),
        "reason": deduction.reason,
        "status": deduction.status,
        "target_period_month": deduction.target_period_month,
        "target_period_year": deduction.target_period_year,
        "approver_id": deduction.approver_id,
        "approver_name": employee_display_name(db, deduction.approver_id),
        "decision_notes": deduction.decision_notes,
        "decided_at": deduction.decided_at,
        "applied_payroll_run_id": deduction.applied_payroll_run_id,
        "applied_at": deduction.applied_at,
    }


def list_asset_recovery_deductions(
    db: Session, company_id: uuid.UUID, employee_id: uuid.UUID | None = None
) -> list[dict]:
    """Assets > Asset Recovery Deduction list -- the audit trail: every
    request regardless of status (pending/approved/rejected), each showing
    whether/when it was actually applied to a payslip."""
    q = select(models.AssetRecoveryDeduction).where(
        models.AssetRecoveryDeduction.company_id == company_id
    )
    if employee_id is not None:
        q = q.where(models.AssetRecoveryDeduction.employee_id == employee_id)
    q = q.order_by(models.AssetRecoveryDeduction.created_at.desc())
    rows = db.scalars(q).all()
    return [asset_recovery_deduction_out(db, r) for r in rows]


def get_asset_recovery_deduction_for_update(
    db: Session, deduction_id: uuid.UUID
) -> models.AssetRecoveryDeduction | None:
    return db.scalar(
        select(models.AssetRecoveryDeduction)
        .where(models.AssetRecoveryDeduction.id == deduction_id)
        .with_for_update()
    )


def decide_asset_recovery_deduction(
    db: Session,
    deduction: models.AssetRecoveryDeduction,
    status: str,
    decision_notes: str | None,
    approver_id: uuid.UUID,
) -> models.AssetRecoveryDeduction:
    deduction.status = status
    deduction.decision_notes = decision_notes
    deduction.approver_id = approver_id
    deduction.decided_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()
    return deduction


def create_hiring_requisition(
    db: Session,
    company_id: uuid.UUID,
    requested_by: uuid.UUID,
    designation_title: str,
    department_name: str | None,
    positions_count: int,
    justification: str | None,
) -> models.HiringRequisition:
    requester = db.get(models.Employee, requested_by)
    if requester is None or requester.company_id != company_id:
        raise ValueError("Requesting employee not found")

    department_id = None
    if department_name:
        department = get_department_by_name(db, company_id, department_name)
        if department is None:
            raise ValueError(f"Department '{department_name}' not found")
        department_id = department.id

    requisition = models.HiringRequisition(
        id=uuid.uuid4(),
        requested_by=requested_by,
        designation_title=designation_title,
        department_id=department_id,
        positions_count=positions_count,
        justification=justification,
        status="pending",
    )
    db.add(requisition)
    db.flush()
    return requisition


def list_hiring_requisitions(
    db: Session,
    company_id: uuid.UUID,
    limit: int | None = None,
    offset: int = 0,
    requested_by: uuid.UUID | None = None,
) -> list[models.HiringRequisition]:
    q = (
        select(models.HiringRequisition)
        .join(models.Employee, models.Employee.id == models.HiringRequisition.requested_by)
        .where(models.Employee.company_id == company_id)
        .order_by(models.HiringRequisition.id)
    )
    if requested_by is not None:
        q = q.where(models.HiringRequisition.requested_by == requested_by)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_hiring_requisition_for_update(
    db: Session, requisition_id: uuid.UUID, company_id: uuid.UUID
) -> models.HiringRequisition | None:
    """Scoped by company_id (via the requesting employee -- HiringRequisition
    has no company_id column of its own, same join list_hiring_requisitions
    already uses) -- without it, any authenticated user of any company could
    approve/reject another company's hiring requisition by guessing/
    enumerating its id."""
    return db.scalar(
        select(models.HiringRequisition)
        .join(models.Employee, models.Employee.id == models.HiringRequisition.requested_by)
        .where(
            models.HiringRequisition.id == requisition_id,
            models.Employee.company_id == company_id,
        )
        .with_for_update()
    )


_TASK_BOARD_COLUMNS = ["todo", "in_progress", "in_review", "done"]


def create_task_board_card(
    db: Session,
    project_id: uuid.UUID,
    status: str,
    title: str,
    company_id: uuid.UUID | None = None,
    **_ignored,
) -> models.TaskBoardCard:
    if company_id is None:
        project = db.get(models.Project, project_id)
        company_id = project.company_id if project else uuid.uuid4()
    import re as _re, random as _random, string as _string
    code = _re.sub(r"[^A-Z0-9]", "", title.upper())[:6]
    code += "".join(_random.choices(_string.ascii_uppercase + _string.digits, k=4))
    card = models.TaskBoardCard(
        id=uuid.uuid4(),
        company_id=company_id,
        project_id=project_id,
        code=code,
        title=title,
        status=status,
    )
    db.add(card)
    db.flush()
    return card


def list_task_board(db: Session, company_id: uuid.UUID) -> dict[str, list[models.TaskBoardCard]]:
    """Grouped by status (maps to kanban column_key for frontend compat)."""
    cards = db.scalars(
        select(models.TaskBoardCard)
        .join(models.Project, models.Project.id == models.TaskBoardCard.project_id)
        .where(models.Project.company_id == company_id)
    ).all()
    board: dict[str, list[models.TaskBoardCard]] = {col: [] for col in _TASK_BOARD_COLUMNS}
    for card in cards:
        board.setdefault(card.status, []).append(card)
    return board


def list_tasks_for_project(db: Session, project_id: uuid.UUID) -> list[models.TaskBoardCard]:
    """Lightweight task list for Add Work Entry's Task picker -- scoped to
    one project, unlike list_task_board's whole-company Kanban view.
    Confirmed live that at least one real tenant has no pm_tasks table at
    all (Task Board has never worked for it) -- falls back to an empty
    list rather than 500ing the work entry the picker is attached to."""

    def _do():
        return db.scalars(
            select(models.TaskBoardCard)
            .where(models.TaskBoardCard.project_id == project_id, models.TaskBoardCard.is_active.is_(True))
            .order_by(models.TaskBoardCard.title)
        ).all()

    return _optional_tenant_table("pm_tasks", db, _do, default=[])


def get_task_board_card_for_update(db: Session, card_id: uuid.UUID) -> models.TaskBoardCard | None:
    return db.scalar(
        select(models.TaskBoardCard).where(models.TaskBoardCard.id == card_id).with_for_update()
    )


def get_task_board_card_detail(
    db: Session, company_id: uuid.UUID, card_id: uuid.UUID
) -> "dict | None":
    """Task Management > (click a card) -- the complete Task Details
    screen. Tenant-isolated the same way move_task_board_card already is
    (pm_tasks carries its own company_id, no join needed): a cross-tenant
    guess is indistinguishable from a nonexistent id (404, not 403).

    pm_tasks has no assignee column (create_task_board_card/
    _task_board_card_out already hardcode "Unassigned" for the same
    reason -- see routers/projects.py) -- `activity` below is real,
    backend-derived history instead: every hcm.pm_time_entries row anyone
    has actually logged against this task, each resolved to its real
    employee via that entry's timesheet, newest first."""
    card = db.get(models.TaskBoardCard, card_id)
    if card is None or card.company_id != company_id:
        return None
    project = db.get(models.Project, card.project_id)

    activity_rows = db.execute(
        select(models.WorkEntry, models.Timesheet)
        .join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
        .where(models.WorkEntry.task_id == card.id)
        .order_by(models.WorkEntry.entry_date.desc())
    ).all()
    employee_ids = {ts.employee_id for _, ts in activity_rows}
    employees_by_id = {
        e.id: e
        for e in (
            db.scalars(select(models.Employee).where(models.Employee.id.in_(employee_ids)))
            if employee_ids
            else []
        )
    }
    activity = [
        {
            "employee_id": ts.employee_id,
            "employee_name": _full_name(employees_by_id.get(ts.employee_id)),
            "entry_date": entry.entry_date,
            "hours": float(entry.hours),
            "description": entry.description,
            "status": entry.status,
        }
        for entry, ts in activity_rows
    ]

    return {
        "id": card.id,
        "code": card.code,
        "title": card.title,
        "description": card.description,
        "status": card.status,
        "priority": card.priority,
        "task_type": card.task_type,
        "project_id": card.project_id,
        "project_name": project.name if project else "—",
        "assignee": "Unassigned",
        "due_date": card.due_date,
        "planned_start_date": card.planned_start_date,
        "estimated_hours": float(card.estimated_hours) if card.estimated_hours is not None else None,
        "actual_hours": float(card.actual_hours or 0),
        "progress_pct": card.progress_pct or 0,
        "is_active": card.is_active,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
        "activity": activity,
    }


_TASK_CARD_EDITABLE_FIELDS = (
    "title",
    "description",
    "priority",
    "task_type",
    "due_date",
    "planned_start_date",
    "estimated_hours",
)


def update_task_board_card(card: models.TaskBoardCard, updates: dict) -> bool:
    """Task Management > Task Details > Edit -- same partial-update /
    changed-detection convention as update_lifecycle_event. Deliberately
    excludes status/column_key -- that stays exclusively the existing
    "Move to" action's job (see move_task_board_card). Only ever touches
    the one card the caller already resolved (tenant-checked at the
    route), and returns False (the route turns this into a 400) if none
    of the provided fields actually differ from the stored values."""
    changed = False
    for field in _TASK_CARD_EDITABLE_FIELDS:
        if field not in updates:
            continue
        if getattr(card, field) != updates[field]:
            setattr(card, field, updates[field])
            changed = True
    return changed


def create_recognition(
    db: Session, employee_id: uuid.UUID, badge: str, reason: str | None, given_on: datetime.date
) -> models.Recognition:
    recognition = models.Recognition(
        id=uuid.uuid4(), employee_id=employee_id, badge=badge, reason=reason, given_on=given_on
    )
    db.add(recognition)
    db.flush()
    return recognition


def list_recognitions(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.Recognition]:
    q = (
        select(models.Recognition)
        .join(models.Employee, models.Employee.id == models.Recognition.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.Recognition.given_on.desc(), models.Recognition.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def create_travel_request(
    db: Session,
    employee_id: uuid.UUID,
    purpose: str,
    destination: str,
    from_date: datetime.date,
    to_date: datetime.date,
    travel_mode: str,
    estimated_cost: float | None,
) -> models.TravelRequestModel:
    request = models.TravelRequestModel(
        id=uuid.uuid4(),
        employee_id=employee_id,
        purpose=purpose,
        destination=destination,
        from_date=from_date,
        to_date=to_date,
        travel_mode=travel_mode,
        estimated_cost=estimated_cost,
        status="pending",
    )
    db.add(request)
    db.flush()
    return request


def list_travel_requests(
    db: Session,
    company_id: uuid.UUID,
    status: str | None = None,
    employee_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.TravelRequestModel]:
    q = (
        select(models.TravelRequestModel)
        .join(models.Employee, models.Employee.id == models.TravelRequestModel.employee_id)
        .where(models.Employee.company_id == company_id)
    )
    if status is not None:
        q = q.where(models.TravelRequestModel.status == status)
    if employee_ids is not None:
        q = q.where(models.TravelRequestModel.employee_id.in_(employee_ids))
    q = q.order_by(models.TravelRequestModel.from_date.desc(), models.TravelRequestModel.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_travel_request_for_update(
    db: Session, travel_request_id: uuid.UUID
) -> models.TravelRequestModel | None:
    """SELECT ... FOR UPDATE -- same double-approval protection as
    get_regularization_for_update/get_leave_request_for_update."""
    return db.scalar(
        select(models.TravelRequestModel)
        .where(models.TravelRequestModel.id == travel_request_id)
        .with_for_update()
    )


def create_expense_report(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    reference: str,
    submitted_date: datetime.date,
    line_items: list[dict],
) -> models.ExpenseClaim:
    """A multi-line hcm.expense_claims row -- the fuller Travel & Expenses
    counterpart to Payroll's single-line create_reimbursement."""
    # M-21: unique claim_no per report + double-submit replay (expense_claims.py).
    from . import expense_claims

    total = sum(item["amount"] for item in line_items)
    expense_claims.lock_employee(db, "expense_report", company_id, employee_id)
    existing = expense_claims.recent_duplicate(
        db, company_id, employee_id, "EXP", submitted_date, reference, total
    )
    if existing is not None:
        existing.idempotent_replay = True
        return existing
    claim_no = expense_claims.generate_claim_no(db, company_id, "EXP", submitted_date)
    claim = models.ExpenseClaim(
        id=uuid.uuid4(),
        company_id=company_id,
        created_at=expense_claims.now(),
        claim_no=claim_no,
        employee_id=employee_id,
        claim_date=submitted_date,
        purpose=reference,
        total_amount=total,
        status="pending",
    )
    db.add(claim)
    db.flush()

    for item in line_items:
        db.add(
            models.ExpenseClaimLine(
                id=uuid.uuid4(),
                claim_id=claim.id,
                expense_date=item["date"],
                category=item["category"],
                description=item["description"],
                amount=item["amount"],
            )
        )
    db.flush()
    return claim


def list_expense_claims(
    db: Session,
    company_id: uuid.UUID,
    employee_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[models.ExpenseClaim]:
    q = (
        select(models.ExpenseClaim)
        .options(selectinload(models.ExpenseClaim.lines))
        .where(models.ExpenseClaim.company_id == company_id)
    )
    if employee_ids is not None:
        q = q.where(models.ExpenseClaim.employee_id.in_(employee_ids))
    q = q.order_by(models.ExpenseClaim.claim_date.desc(), models.ExpenseClaim.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def get_expense_claim_for_update(db: Session, claim_id: uuid.UUID) -> models.ExpenseClaim | None:
    """SELECT ... FOR UPDATE -- same double-approval protection as
    get_regularization_for_update/get_leave_request_for_update."""
    return db.scalar(
        select(models.ExpenseClaim).where(models.ExpenseClaim.id == claim_id).with_for_update()
    )


def list_notification_preferences(db: Session, user_id: uuid.UUID) -> list[models.NotificationPreference]:
    return db.scalars(
        select(models.NotificationPreference).where(models.NotificationPreference.user_id == user_id)
    ).all()


def upsert_notification_preference(
    db: Session,
    user_id: uuid.UUID,
    event_key: str,
    channel: str,
    enabled: bool,
) -> models.NotificationPreference:
    existing = db.scalar(
        select(models.NotificationPreference).where(
            models.NotificationPreference.user_id == user_id,
            models.NotificationPreference.event_key == event_key,
            models.NotificationPreference.channel == channel,
        )
    )
    if existing is not None:
        existing.enabled = enabled
        db.flush()
        return existing
    pref = models.NotificationPreference(
        id=uuid.uuid4(),
        user_id=user_id,
        event_key=event_key,
        channel=channel,
        enabled=enabled,
    )
    db.add(pref)
    db.flush()
    return pref


def create_sprint(
    db: Session,
    project_id: uuid.UUID,
    name: str,
    start_date: datetime.date | None,
    end_date: datetime.date | None,
    velocity_points: int | None = None,  # ignored — column removed from DB
) -> models.Sprint:
    existing = db.scalars(select(models.Sprint).where(models.Sprint.project_id == project_id)).all()
    sprint = models.Sprint(
        id=uuid.uuid4(),
        project_id=project_id,
        name=name,
        sequence=len(existing) + 1,
        planned_start_date=start_date,
        planned_end_date=end_date,
    )
    db.add(sprint)
    db.flush()
    return sprint


def list_sprints(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.Sprint]:
    q = (
        select(models.Sprint)
        .join(models.Project, models.Project.id == models.Sprint.project_id)
        .where(models.Project.company_id == company_id)
        .order_by(models.Sprint.planned_start_date.desc(), models.Sprint.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def create_release(
    db: Session,
    project_id: uuid.UUID,
    name: str,
    target_date: datetime.date,
    notes: str | None = None,  # ignored — column removed from DB
) -> models.Release:
    release = models.Release(
        id=uuid.uuid4(),
        project_id=project_id,
        name=name,
        due_date=target_date,
    )
    db.add(release)
    db.flush()
    return release


def list_releases(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.Release]:
    q = (
        select(models.Release)
        .join(models.Project, models.Project.id == models.Release.project_id)
        .where(models.Project.company_id == company_id)
        .order_by(models.Release.due_date.desc(), models.Release.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def check_overdue_milestones(db: Session, company_id: uuid.UUID) -> None:
    """Auto-transitions any pending pm_milestones (Release) row past its
    due_date to 'missed' and notifies the project's PM and Team Lead. A
    check-on-read hook rather than a scheduled job -- this app has no
    background scheduler -- run from GET /releases so due_date/status
    (real columns that already existed but drove nothing) actually surface
    somewhere instead of sitting unread. Best-effort, same convention as
    create_notification itself: never the reason the read that triggered
    it fails."""
    today = company_today(db, company_id)
    overdue = db.scalars(
        select(models.Release)
        .join(models.Project, models.Project.id == models.Release.project_id)
        .where(
            models.Project.company_id == company_id,
            models.Release.status == "pending",
            models.Release.due_date < today,
        )
    ).all()
    if not overdue:
        return
    for milestone in overdue:
        milestone.status = "missed"
        project = milestone.project
        title = f'Milestone "{milestone.name}" is overdue'
        body = (
            f'"{milestone.name}" on {project.name} was due '
            f'{milestone.due_date.isoformat()} and hasn\'t been completed.'
        )
        notified_user_ids: set[uuid.UUID] = set()
        for employee_id in (project.project_manager_id, resolve_project_team_lead_id(db, project.id)):
            user_id = get_user_id_for_employee(db, employee_id)
            if user_id is None or user_id in notified_user_ids:
                continue
            notified_user_ids.add(user_id)
            create_notification(
                db, project.company_id, user_id, title, body,
                entity_type="milestone", entity_id=milestone.id,
            )
    db.flush()


# (tenant schema, company_id, date) already checked by this process today --
# PERF-10 / P5: every later GET /notifications that day skips the scan
# entirely (no queries, no DML, no commit). Bounded: only today's keys kept.
_CELEBRATIONS_CHECKED: set[tuple] = set()


def check_daily_celebrations(db: Session, company_id: uuid.UUID) -> bool:
    """Birthday / Work Anniversary celebration reminders. Same check-on-read
    pattern as check_overdue_milestones above (this app has no background
    scheduler) -- run from GET /notifications, the one endpoint every login
    always hits, so the first person in the tenant to log in (or reload) on
    the actual day triggers it for the whole company.

    Idempotent via hcm_celebration_log's UNIQUE(employee_id,
    celebration_type, year): the atomic `INSERT ... ON CONFLICT DO NOTHING
    RETURNING id` below means only the first caller each year for a given
    employee+type actually broadcasts anything -- every later call the same
    day (or a concurrent one) sees no returned row and does nothing.

    Broadcasts to the whole company, not just the celebrant's manager:
    every active User gets an in-app notification, and every active
    Employee with a work_email gets the real work-email notification (see
    email_service) -- these are FYI/team-celebration announcements, not
    approval-workflow notifications, so there's no requester/manager to
    target.

    PERF-10 / P5: runs at most once per (process, tenant, company, day);
    only employees whose birthday/joining month+day is today are loaded
    (SQL EXTRACT filter, not the whole roster). A tenant without
    hcm_celebration_log is skipped with a warning instead of a 500.
    Returns True only if it may have written rows (caller commits then)."""
    today = company_today(db, company_id)  # DATA-03: company-timezone birthday
    key = (database.get_session_tenant_slug(db), company_id, today)
    if key in _CELEBRATIONS_CHECKED:
        return False
    if not tenant_table_exists(db, "hcm_celebration_log"):
        logger.warning(
            "Tenant %s has no hcm_celebration_log table -- celebration reminders skipped "
            "(apply backend/db/add_celebration_log.sql)", key[0],
        )
        _CELEBRATIONS_CHECKED.add(key)
        return False
    month, day = today.month, today.day
    employees = db.scalars(
        select(models.Employee).where(
            models.Employee.company_id == company_id,
            models.Employee.is_active.is_(True),
            or_(
                (func.extract("month", models.Employee.date_of_birth) == month)
                & (func.extract("day", models.Employee.date_of_birth) == day),
                (func.extract("month", models.Employee.date_of_joining) == month)
                & (func.extract("day", models.Employee.date_of_joining) == day),
            ),
        )
    ).all()
    for e in employees:
        if (
            e.date_of_birth is not None
            and e.date_of_birth.month == today.month
            and e.date_of_birth.day == today.day
        ):
            _fire_celebration(db, company_id, e, "birthday", today)
        # today.year > date_of_joining.year -- a "work anniversary" implies
        # at least one full year completed; someone joining today shouldn't
        # get a same-day "congratulations on your work anniversary".
        if (
            e.date_of_joining is not None
            and e.date_of_joining.month == today.month
            and e.date_of_joining.day == today.day
            and today.year > e.date_of_joining.year
        ):
            _fire_celebration(db, company_id, e, "work_anniversary", today)
    db.flush()
    # Drop stale days, then remember today. Marked only after a successful
    # pass, so a failure retries on the next request.
    for old in [k for k in _CELEBRATIONS_CHECKED if k[2] != today]:
        _CELEBRATIONS_CHECKED.discard(old)
    _CELEBRATIONS_CHECKED.add(key)
    return bool(employees)


def _fire_celebration(
    db: Session,
    company_id: uuid.UUID,
    employee: models.Employee,
    celebration_type: str,
    today: datetime.date,
) -> None:
    won = db.execute(
        pg_insert(models.CelebrationLog)
        .values(
            id=uuid.uuid4(),
            employee_id=employee.id,
            celebration_type=celebration_type,
            year=today.year,
        )
        .on_conflict_do_nothing(
            index_elements=["employee_id", "celebration_type", "year"]
        )
        .returning(models.CelebrationLog.id)
    ).first()
    if won is None:
        return  # already broadcast for this employee/type/year

    name = _full_name(employee)
    designation = db.get(models.Designation, employee.designation_id) if employee.designation_id else None
    department = db.get(models.Department, employee.department_id) if employee.department_id else None
    extra = {
        "designation": designation.name if designation else "",
        "department": department.name if department else "",
    }
    if celebration_type == "birthday":
        title = f"\U0001f389 Happy Birthday, {name}!"
        body = (
            f"Today is {name}'s birthday \U0001f382 Wishing them a wonderful year ahead "
            f"filled with happiness, health and success. Everyone, let's make their day "
            f"special with your warm wishes!"
        )
        status_label = "Birthday \U0001f382"
    else:
        years = today.year - employee.date_of_joining.year if employee.date_of_joining else 0
        years_text = f"{years} year{'s' if years != 1 else ''}" if years > 0 else ""
        extra["years_of_service"] = years_text
        ordinal = (f"{years}{'th' if 10 <= years % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(years % 10, 'th')} "
                   if years > 0 else "")
        title = f"\U0001f389 Happy {ordinal}Work Anniversary, {name}!"
        body = (
            f"Today {name} completes {years_text} with us \U0001f38a "
            if years_text else f"Today marks {name}'s work anniversary with us \U0001f38a "
        ) + (
            "Thank you for your dedication, hard work and everything you bring to the team. "
            "Everyone, let's congratulate them on this milestone!"
        )
        status_label = "Work Anniversary \U0001f38a"

    recipients = db.scalars(
        select(models.User).where(
            models.User.company_id == company_id,
            models.User.status == "active",
        )
    ).all()
    for user in recipients:
        create_notification(
            db, company_id, user.id, title=title, body=body,
            entity_type=celebration_type, entity_id=employee.id,
        )

    all_employees = db.scalars(
        select(models.Employee).where(
            models.Employee.company_id == company_id,
            models.Employee.is_active.is_(True),
        )
    ).all()
    occurred_at = datetime.datetime.now(datetime.timezone.utc)
    for recipient in all_employees:
        email_service.send_notification_email(
            recipient.work_email,
            subject=title,
            heading=title,
            body_lines=[body],
            employee_name=name,
            employee_code=employee.employee_code,
            status_label=status_label,
            occurred_at=occurred_at,
            entity_type=celebration_type,
            cta_label="Send Your Wishes",
            db=db,
            company_id=company_id,
            entity_id=employee.id,
            extra=extra,
        )


def create_training_session(
    db: Session,
    company_id: uuid.UUID,
    title: str,
    session_date: datetime.date,
    trainer_name: str | None,
    is_mandatory: bool,
    attendee_count: int,
) -> models.TrainingSession:
    session_row = models.TrainingSession(
        id=uuid.uuid4(),
        company_id=company_id,
        title=title,
        session_date=session_date,
        trainer_name=trainer_name,
        is_mandatory=is_mandatory,
        attendee_count=attendee_count,
    )
    db.add(session_row)
    db.flush()
    return session_row


def list_training_sessions(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0
) -> list[models.TrainingSession]:
    q = (
        select(models.TrainingSession)
        .where(models.TrainingSession.company_id == company_id)
        .order_by(models.TrainingSession.session_date, models.TrainingSession.id)
    )
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def list_skill_ratings(
    db: Session, company_id: uuid.UUID, limit: int | None = None, offset: int = 0,
    visible_ids: "set[uuid.UUID] | None" = None,
) -> list[models.EmployeeSkillRating]:
    q = (
        select(models.EmployeeSkillRating)
        .join(models.Employee, models.Employee.id == models.EmployeeSkillRating.employee_id)
        .where(models.Employee.company_id == company_id)
        .order_by(models.EmployeeSkillRating.id)
    )
    if visible_ids is not None:  # L-30: self, reporting subtree, HR (None = all)
        q = q.where(models.EmployeeSkillRating.employee_id.in_(list(visible_ids) or [uuid.UUID(int=0)]))
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    return db.scalars(q).all()


def upsert_skill_rating(
    db: Session, employee_id: uuid.UUID, skill: str, level: str
) -> models.EmployeeSkillRating:
    existing = db.scalar(
        select(models.EmployeeSkillRating).where(
            models.EmployeeSkillRating.employee_id == employee_id,
            models.EmployeeSkillRating.skill == skill,
        )
    )
    if existing is not None:
        existing.level = level
        db.flush()
        return existing
    rating = models.EmployeeSkillRating(
        id=uuid.uuid4(), employee_id=employee_id, skill=skill, level=level
    )
    db.add(rating)
    db.flush()
    return rating


def list_integrations(db: Session, company_id: uuid.UUID) -> list[models.Integration]:
    return db.scalars(
        select(models.Integration).where(models.Integration.company_id == company_id)
    ).all()


def upsert_integration(
    db: Session,
    company_id: uuid.UUID,
    name: str,
    description: str | None,
    is_connected: bool,
) -> models.Integration:
    now = datetime.datetime.now(datetime.timezone.utc)
    existing = db.scalar(
        select(models.Integration).where(
            models.Integration.company_id == company_id,
            models.Integration.name == name,
        )
    )
    if existing is not None:
        existing.is_connected = is_connected
        if description is not None:
            existing.description = description
        existing.updated_at = now
        db.flush()
        return existing
    integration = models.Integration(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        description=description,
        is_connected=is_connected,
        updated_at=now,
    )
    db.add(integration)
    db.flush()
    return integration


def get_company_settings(db: Session, company_id: uuid.UUID) -> models.CompanySettings | None:
    return db.get(models.CompanySettings, company_id)


def get_company_pf_wage_ceiling(db: Session, company_id: uuid.UUID) -> float:
    settings_row = db.get(models.CompanySettings, company_id)
    return float(settings_row.pf_wage_ceiling) if settings_row is not None else 15000.0


def get_company_auto_tds_estimate_enabled(db: Session, company_id: uuid.UUID) -> bool:
    """False for every company until the Organization Owner/CEO explicitly
    turns it on (Administration > General Settings) -- see
    CompanySettings.auto_tds_estimate_enabled and its one call site in
    _compute_and_write_slip."""
    settings_row = db.get(models.CompanySettings, company_id)
    return bool(settings_row.auto_tds_estimate_enabled) if settings_row is not None else False


def get_company_lop_deduction_enabled(db: Session, company_id: uuid.UUID) -> bool:
    """Payroll Settings > Deductions Configuration. False for every company
    until an HR/Organization Owner explicitly turns it on -- see
    CompanySettings.lop_deduction_enabled and its call site in
    _compute_and_write_slip."""
    settings_row = db.get(models.CompanySettings, company_id)
    return bool(settings_row.lop_deduction_enabled) if settings_row is not None else False


def get_company_asset_deduction_enabled(db: Session, company_id: uuid.UUID) -> bool:
    """Payroll Settings > Deductions Configuration. False for every company
    until an HR/Organization Owner explicitly turns it on -- see
    CompanySettings.asset_deduction_enabled and its call site in
    _compute_and_write_slip."""
    settings_row = db.get(models.CompanySettings, company_id)
    return bool(settings_row.asset_deduction_enabled) if settings_row is not None else False


def upsert_company_settings(
    db: Session, company_id: uuid.UUID, updates: dict
) -> models.CompanySettings:
    settings_row = db.get(models.CompanySettings, company_id)
    if settings_row is None:
        settings_row = models.CompanySettings(company_id=company_id)
        db.add(settings_row)
    for key, value in updates.items():
        if value is not None:
            setattr(settings_row, key, value)
    db.flush()
    return settings_row


# Built-in fallback field state per entity, matching today's actual
# behavior exactly -- field names match backend/app/schemas.py EmployeeCreate
# 1:1 (branch_name/department_name/designation_name are name-lookups, not
# ids, since that's what the Add Employee form actually sends). Branch and
# Department are hard requirements in crud.create_employee (it raises
# ValueError if either name doesn't resolve) and stay "mandatory" here --
# making either genuinely optional would require deeper changes to employee
# creation/hierarchy-resolution than a field-state flag alone, and every org
# chart example in this feature's own spec keeps Branch+Department present
# in every company's hierarchy. Business Unit isn't an employee-level field
# at all (it lives on Department, and DepartmentCreate.business_unit_name is
# already optional there) -- only Sub-department is a real, already-nullable
# per-employee toggle point.
# core.field_rules rows (if any) override these per company, then per role
# -- see get_field_rules. Adding a new entity/field here is the only code
# change ever needed to bring a new form under this engine; no migration is
# required since the table is generic.
DEFAULT_FIELD_RULES: dict[str, dict[str, str]] = {
    "employee": {
        # employee_code is NOT here -- it's always backend-generated (see
        # crud.generate_employee_code), never a field the caller supplies,
        # so it has no place in a "did the caller fill in this UI field"
        # mandatory/optional/hidden/readonly config.
        "first_name": "mandatory",
        "work_email": "mandatory",
        "role_name": "mandatory",
        "password": "mandatory",
        "date_of_joining": "mandatory",
        "employment_type": "mandatory",
        "status": "mandatory",
        "branch_name": "mandatory",
        "department_name": "mandatory",
        "designation_name": "mandatory",
        "last_name": "optional",
        "gender": "optional",
        "date_of_birth": "optional",
        "work_mode": "optional",
        "band": "optional",
        "annual_ctc": "optional",
        "reporting_manager_id": "optional",
        "pan": "optional",
        "bank_name": "optional",
        "bank_account_no": "optional",
        "bank_ifsc": "optional",
        "blood_group": "optional",
        "nationality": "optional",
        "marital_status": "optional",
        "personal_email": "optional",
        "personal_phone": "optional",
        "current_address": "optional",
        "permanent_address": "optional",
        "sub_department_name": "optional",
    },
}

# Employee fields whose "mandatory"/"optional" state is really just a proxy
# for whether that org level is enabled at all for this company -- see
# add_org_hierarchy_toggles.sql. Only Sub-department has a real, already-
# nullable per-employee field to gate (see DEFAULT_FIELD_RULES comment
# above for why Branch/Department/Business-unit aren't included here).
_ORG_LEVEL_FIELD_TOGGLES: dict[str, str] = {
    "sub_department_name": "enable_sub_department_level",
}


def get_field_rules(
    db: Session,
    company_id: uuid.UUID,
    entity_key: str,
    role_id: uuid.UUID | None = None,
) -> dict[str, str]:
    """Effective field-state map for one entity: built-in default, overridden
    by this company's org-level toggles (employee only), overridden by any
    company-wide core.field_rules rows (role_id IS NULL), overridden last by
    role-specific rows for role_id. Never raises -- an unconfigured company
    gets exactly DEFAULT_FIELD_RULES back."""
    effective = dict(DEFAULT_FIELD_RULES.get(entity_key, {}))

    if entity_key == "employee":
        settings_row = get_company_settings(db, company_id)
        if settings_row is not None:
            for field_key, toggle_attr in _ORG_LEVEL_FIELD_TOGGLES.items():
                if not getattr(settings_row, toggle_attr):
                    effective[field_key] = "hidden"

    rows = db.scalars(
        select(models.FieldRule).where(
            models.FieldRule.company_id == company_id,
            models.FieldRule.entity_key == entity_key,
            models.FieldRule.role_id.is_(None),
        )
    ).all()
    for row in rows:
        effective[row.field_key] = row.state

    if role_id is not None:
        role_rows = db.scalars(
            select(models.FieldRule).where(
                models.FieldRule.company_id == company_id,
                models.FieldRule.entity_key == entity_key,
                models.FieldRule.role_id == role_id,
            )
        ).all()
        for row in role_rows:
            effective[row.field_key] = row.state

    return effective


def upsert_field_rules(
    db: Session,
    company_id: uuid.UUID,
    entity_key: str,
    rules: list[dict],
    actor_id: uuid.UUID | None,
) -> None:
    """rules: [{"field_key", "state", "role_id"}, ...]. Each is a real
    INSERT ... ON CONFLICT DO UPDATE keyed on (company_id, entity_key,
    field_key, role_id) via the DB's own core_field_rules_unique
    constraint (NULLS NOT DISTINCT, so two company-wide -- role_id NULL --
    rows for the same field can never coexist) -- replaces a prior
    check-then-insert that was racy under concurrent PUTs to the same
    field. Requires backend/db/add_administration_audit_columns.sql to
    have been run (it creates core_field_rules_unique)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.datetime.now(datetime.timezone.utc)
    for rule in rules:
        stmt = (
            pg_insert(models.FieldRule)
            .values(
                id=uuid.uuid4(),
                company_id=company_id,
                entity_key=entity_key,
                field_key=rule["field_key"],
                state=rule["state"],
                role_id=rule.get("role_id"),
                created_by=actor_id,
                created_at=now,
                updated_by=actor_id,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["company_id", "entity_key", "field_key", "role_id"],
                set_={"state": rule["state"], "updated_by": actor_id, "updated_at": now},
            )
        )
        db.execute(stmt)
    db.flush()


_MISSING = object()


def validate_against_field_rules(
    db: Session,
    company_id: uuid.UUID,
    entity_key: str,
    payload: dict,
    role_id: uuid.UUID | None = None,
    check_readonly: bool = False,
) -> list[dict]:
    """Returns a list of {"field", "message"} errors (empty if valid).
    `payload` should contain every field the caller is attempting to
    set/change -- absence of a key is treated the same as an explicit
    None/blank for 'mandatory' fields. `check_readonly` should only be true
    for update endpoints, where any key present in `payload` is an attempted
    change (create endpoints have nothing to compare against, so readonly
    is meaningless there)."""
    rules = get_field_rules(db, company_id, entity_key, role_id)
    errors: list[dict] = []
    for field_key, state in rules.items():
        value = payload.get(field_key, _MISSING)
        if state == "mandatory":
            if value is _MISSING or value is None or value == "":
                errors.append({"field": field_key, "message": f"{field_key} is required"})
        elif state == "readonly" and check_readonly:
            if value is not _MISSING:
                errors.append(
                    {"field": field_key, "message": f"{field_key} cannot be changed"}
                )
    return errors


def get_project_progress(db: Session, project_id: uuid.UUID) -> int:
    """% of a project's active task-board cards whose column is "done",
    rounded to the nearest whole percent. Uses `status` (the column the
    Kanban board actually writes on every card move) rather than
    `progress_pct`, which no create/update path ever sets -- averaging that
    column would always yield 0 regardless of real progress. 0 when the
    project has no cards yet (honest "nothing tracked" rather than a
    fabricated number)."""
    statuses = db.scalars(
        select(models.TaskBoardCard.status).where(
            models.TaskBoardCard.project_id == project_id,
            models.TaskBoardCard.is_active == True,  # noqa: E712
        )
    ).all()
    if not statuses:
        return 0
    done = sum(1 for s in statuses if s == "done")
    return round(100 * done / len(statuses))


def list_leave_allocations(
    db: Session,
    company_id: uuid.UUID,
    employee_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[tuple]:
    query = (
        select(models.LeaveAllocation, models.LeaveType)
        .join(models.LeaveType, models.LeaveType.id == models.LeaveAllocation.leave_type_id)
        .join(models.Employee, models.Employee.id == models.LeaveAllocation.employee_id)
        .where(models.Employee.company_id == company_id)
    )
    if employee_ids is not None:
        query = query.where(models.LeaveAllocation.employee_id.in_(employee_ids))
    query = query.order_by(models.LeaveAllocation.employee_id, models.LeaveType.name, models.LeaveAllocation.id)
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    rows = db.execute(query).all()
    return rows


def get_or_create_fiscal_year(
    db: Session, company_id: uuid.UUID, on_date: "datetime.date | None" = None
) -> models.FiscalYear:
    """Looks up the fin.fiscal_years row covering [on_date] (default: today)
    for this company, creating one if none exists yet -- fin.fiscal_years
    has always existed but nothing ever inserted into it, so every company's
    first leave allocation used to fail with a ForeignKeyViolation. The
    window is derived from core.companies.fiscal_year_start_month, matching
    the same field the Payroll/Tax Declaration screens already read."""
    today = on_date or company_today(db, company_id)  # DATA-03
    company = db.get(models.Company, company_id)
    start_month = company.fiscal_year_start_month if company else 4
    start_year = today.year if today.month >= start_month else today.year - 1
    start_date = datetime.date(start_year, start_month, 1)
    end_date = datetime.date(start_year + 1, start_month, 1) - datetime.timedelta(days=1)
    name = f"{start_year}-{str(start_year + 1)[-2:]}"

    existing = db.scalar(
        select(models.FiscalYear).where(
            models.FiscalYear.company_id == company_id,
            models.FiscalYear.start_date == start_date,
        )
    )
    if existing is not None:
        return existing

    fiscal_year = models.FiscalYear(
        id=uuid.uuid4(),
        company_id=company_id,
        name=name,
        start_date=start_date,
        end_date=end_date,
        is_closed=False,
    )
    db.add(fiscal_year)
    db.flush()
    return fiscal_year


def create_leave_allocation(
    db: Session,
    employee_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    fiscal_year_id: uuid.UUID,
    allocated_days: float,
) -> models.LeaveAllocation:
    alloc = models.LeaveAllocation(
        id=uuid.uuid4(),
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        fiscal_year_id=fiscal_year_id,
        allocated_days=allocated_days,
        carried_forward_days=0,
        used_days=0,
    )
    db.add(alloc)
    return alloc


def update_leave_allocation(
    db: Session, allocation_id: uuid.UUID, allocated_days: float
) -> models.LeaveAllocation | None:
    alloc = db.scalar(
        select(models.LeaveAllocation).where(models.LeaveAllocation.id == allocation_id)
    )
    if alloc is None:
        return None
    alloc.allocated_days = allocated_days
    return alloc


def update_leave_type(
    db: Session, leave_type_id: uuid.UUID, **kwargs
) -> models.LeaveType | None:
    lt = db.scalar(select(models.LeaveType).where(models.LeaveType.id == leave_type_id))
    if lt is None:
        return None
    for k, v in kwargs.items():
        if v is not None or k in ('max_days_per_year',):
            setattr(lt, k, v)
    return lt


def delete_leave_type(db: Session, leave_type_id: uuid.UUID) -> bool:
    """Guarded at the application level, same reasoning/pattern as
    delete_band: hcm_leave_requests.leave_type_id isn't actually enforced
    as a live FK constraint against this table, so deleting a leave type
    with existing requests against it silently orphans them -- every
    later GET /leave-requests then 500s trying to resolve
    lr.leave_type.name for the now-dangling reference. Raises ValueError
    so the router can turn it into a clean 409 instead."""
    lt = db.scalar(select(models.LeaveType).where(models.LeaveType.id == leave_type_id))
    if lt is None:
        return False
    in_use = db.scalar(
        select(func.count())
        .select_from(models.LeaveRequest)
        .where(models.LeaveRequest.leave_type_id == leave_type_id)
    )
    if in_use:
        raise ValueError(
            f"Cannot delete leave type '{lt.name}' -- {in_use} leave request(s) "
            "already reference it."
        )
    db.delete(lt)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Per-employee module CRUD — each returns just the data for one profile tab
# so the profile screen can lazy-load sections on demand instead of fetching
# the entire employee roster via list_employees_full().
# ─────────────────────────────────────────────────────────────────────────────

def _emp_or_none(db: Session, employee_id: uuid.UUID) -> "models.Employee | None":
    return db.get(models.Employee, employee_id)


def get_employee_overview(db: Session, employee_id: uuid.UUID) -> "dict | None":
    """Identity + org placement + RBAC role — everything the profile header
    needs. No satellite tables touched."""
    e = _emp_or_none(db, employee_id)
    if e is None:
        return None
    role_name = get_employee_role_name(db, employee_id) or "Employee (ESS)"
    return {
        "id": str(e.id),
        "code": e.employee_code,
        "name": _full_name(e),
        "gender": _dash(e.gender),
        "designation": e.designation.name if e.designation else "—",
        "band": e.band or 0,
        "department": e.department.name if e.department else "—",
        "subDept": e.sub_department.name if e.sub_department else "—",
        "branch": e.branch.name if e.branch else "—",
        "manager": _full_name(e.reporting_manager) if e.reporting_manager else "—",
        "reportingManagerId": str(e.reporting_manager_id) if e.reporting_manager_id else None,
        "dottedLineManagerId": str(e.dotted_line_manager_id) if e.dotted_line_manager_id else None,
        "type": e.employment_type,
        "mode": _dash(e.work_mode),
        "doj": _iso(e.date_of_joining),
        "status": e.status,
        "ctc": e.annual_ctc or 0,
        "role": role_name,
        "city": e.branch.city if e.branch and e.branch.city else "—",
        "email": _dash(e.work_email),
        "photoUrl": e.photo_url,
        # Contract employees only -- the key is absent for everyone else.
        **({"contract": {
            "endDate": _iso(e.contract_end_date),
            "daysLeft": (e.contract_end_date - company_today(db, e.company_id)).days if e.contract_end_date else None,
            "rateAmount": float(e.contract_rate_amount) if e.contract_rate_amount is not None else None,
            "rateUnit": e.contract_rate_unit,
        }} if is_contract_employee(e) else {}),
    }


def set_employee_photo(db: Session, employee_id: uuid.UUID, photo_url: str) -> str | None:
    """Sets employee.photo_url to the newly-uploaded file, returning the
    previous file_url (if any) so the caller can delete it from disk --
    an employee changing their photo shouldn't leave the old file orphaned
    on disk forever."""
    e = _emp_or_none(db, employee_id)
    if e is None:
        return None
    previous = e.photo_url
    e.photo_url = photo_url
    db.flush()
    return previous


def clear_employee_photo(db: Session, employee_id: uuid.UUID) -> str | None:
    """Removes employee.photo_url, returning the removed file_url (if any)
    so the caller can delete it from disk."""
    e = _emp_or_none(db, employee_id)
    if e is None:
        return None
    previous = e.photo_url
    e.photo_url = None
    db.flush()
    return previous


def get_employee_personal(db: Session, employee_id: uuid.UUID) -> "dict | None":
    e = _emp_or_none(db, employee_id)
    if e is None:
        return None
    emc = get_emergency_contact(db, employee_id)
    return {
        "dob": _iso(e.date_of_birth),
        "gender": _dash(e.gender),
        "blood": _dash(e.blood_group),
        "nationality": _dash(e.nationality),
        "marital": _dash(e.marital_status),
        "currentAddr": _dash(e.current_address),
        "permAddr": _dash(e.permanent_address),
        "personalEmail": _dash(e.personal_email),
        "personalPhone": _dash(e.personal_phone),
        "emergencyName": emc.name if emc else "—",
        "emergencyRel": _dash(emc.designation) if emc else "—",
        "emergencyPhone": _dash(emc.phone) if emc else "—",
    }


def _resolve_band_display(db: Session, company_id: uuid.UUID, band_number: int | None) -> str:
    """Employee Profile > Professional tab's "Grade / Band" display --
    core_employees.band is a plain int with no FK (see models.Band's
    docstring), so this resolves it against *this employee's own tenant*
    core_bands rows (list_bands lazily seeds them if the company has none
    yet, same as GET /api/bands) instead of showing the bare number the way
    get_employee_overview's raw `band` field still does. Same "Band N —
    Name" format the Add Employee and Edit Professional Info dropdowns
    already use, so the read-only view and the edit dropdown agree."""
    if not band_number:
        return "—"
    bands = list_bands(db, company_id, active_only=False)
    match = next((b for b in bands if b.band_number == band_number), None)
    return f"Band {band_number} — {match.name}" if match else f"Band {band_number}"


def get_employee_professional(db: Session, employee_id: uuid.UUID) -> "dict | None":
    e = _emp_or_none(db, employee_id)
    if e is None:
        return None
    bu_name = "—"
    if e.department and e.department.business_unit_id:
        bu = db.get(models.BusinessUnit, e.department.business_unit_id)
        if bu:
            bu_name = bu.name
    shift_row = db.execute(
        select(models.ShiftAssignment, models.Shift)
        .join(models.Shift, models.Shift.id == models.ShiftAssignment.shift_id)
        .where(
            models.ShiftAssignment.employee_id == employee_id,
            _shift_assignment_active_on(company_today(db, e.company_id)),
        )
        .order_by(models.ShiftAssignment.from_date.desc())
        .limit(1)
    ).first()
    shift_name = shift_row[1].name if shift_row else "—"
    return {
        "businessUnit": bu_name,
        "division": "—",
        "department": e.department.name if e.department else "—",
        "designation": e.designation.name if e.designation else "—",
        "band": _resolve_band_display(db, e.company_id, e.band),
        "reportingManager": _full_name(e.reporting_manager) if e.reporting_manager else "—",
        "dottedLine": _full_name(e.dotted_line_manager) if e.dotted_line_manager else "—",
        "employmentType": e.employment_type,
        "workMode": _dash(e.work_mode),
        "shift": shift_name,
        "doj": _iso(e.date_of_joining),
        "confirmationDate": _iso(e.confirmation_date),
        "probationEnd": _iso(e.probation_end_date),
        "status": e.status,
        "documents": [],
    }


def get_employee_education(db: Session, employee_id: uuid.UUID) -> "dict | None":
    if _emp_or_none(db, employee_id) is None:
        return None
    edu = db.scalars(
        select(models.EmployeeEducation).where(models.EmployeeEducation.employee_id == employee_id)
    ).first()
    exp = db.scalars(
        select(models.EmployeePriorExperience)
        .where(models.EmployeePriorExperience.employee_id == employee_id)
    ).first()
    skills = [
        r.skill
        for r in db.scalars(
            select(models.EmployeeSkill).where(models.EmployeeSkill.employee_id == employee_id)
        )
    ]
    certs = [
        r.name
        for r in db.scalars(
            select(models.Certification).where(models.Certification.employee_id == employee_id)
        )
    ]
    return {
        "qualification": _dash(edu.qualification) if edu else "—",
        "institute": _dash(edu.institute) if edu else "—",
        "specialization": _dash(edu.specialization) if edu else "—",
        "yearOfPassing": edu.year_of_passing if edu and edu.year_of_passing else 0,
        "certifications": ", ".join(certs) or "—",
        "previousEmployers": exp.employer_name if exp else "—",
        "experienceYears": str(exp.years_experience) if exp and exp.years_experience else "—",
        "skills": ", ".join(skills) or "—",
        "domain": _dash(exp.domain) if exp else "—",
        "documents": [],
    }


def update_employee_education(
    db: Session, employee_id: uuid.UUID, updates: dict
) -> None:
    """Partial update backing PATCH /api/employees/{id}/education.
    EmployeeEducation/EmployeePriorExperience are get-or-create + setattr,
    same pattern as update_employee_profile. Skills are a full replace (the
    row only ever has one field, so no data can be lost). Certifications are
    additive-only -- see EmployeeEducationUpdate's docstring for why."""
    edu_fields = {"qualification", "institute", "specialization", "year_of_passing"}
    if edu_fields & updates.keys():
        edu = db.scalar(
            select(models.EmployeeEducation).where(
                models.EmployeeEducation.employee_id == employee_id
            )
        )
        if edu is None:
            edu = models.EmployeeEducation(id=uuid.uuid4(), employee_id=employee_id)
            db.add(edu)
        for field in edu_fields:
            if field in updates:
                setattr(edu, field, updates[field])

    if {"previous_employer", "experience_years", "domain"} & updates.keys():
        exp = db.scalar(
            select(models.EmployeePriorExperience).where(
                models.EmployeePriorExperience.employee_id == employee_id
            )
        )
        if exp is None:
            exp = models.EmployeePriorExperience(id=uuid.uuid4(), employee_id=employee_id)
            db.add(exp)
        if "previous_employer" in updates:
            exp.employer_name = updates["previous_employer"]
        if "experience_years" in updates:
            exp.years_experience = parse_leading_number(updates["experience_years"] or "")
        if "domain" in updates:
            exp.domain = updates["domain"]

    if "skills" in updates:
        db.execute(
            delete(models.EmployeeSkill).where(
                models.EmployeeSkill.employee_id == employee_id
            )
        )
        seen: set[str] = set()
        for skill in (updates["skills"] or "").split(","):
            skill = skill.strip()
            if skill and skill.lower() not in seen:
                seen.add(skill.lower())
                db.add(
                    models.EmployeeSkill(
                        id=uuid.uuid4(), employee_id=employee_id, skill=skill
                    )
                )

    if "certifications" in updates:
        existing = {
            c.name.lower()
            for c in db.scalars(
                select(models.Certification).where(
                    models.Certification.employee_id == employee_id
                )
            )
        }
        for cert in (updates["certifications"] or "").split(","):
            cert = cert.strip()
            if cert and cert.lower() not in existing:
                existing.add(cert.lower())
                db.add(
                    models.Certification(
                        id=uuid.uuid4(), employee_id=employee_id, name=cert
                    )
                )
    db.flush()


def get_employee_payroll(db: Session, employee_id: uuid.UUID) -> "dict | None":
    e = _emp_or_none(db, employee_id)
    if e is None:
        return None
    tax = db.scalars(
        select(models.TaxDeclaration)
        .where(models.TaxDeclaration.employee_id == employee_id)
        .order_by(models.TaxDeclaration.fiscal_year.desc())
    ).first()
    slip_row = db.execute(
        select(models.SalarySlip, models.PayrollRun)
        .join(models.PayrollRun, models.PayrollRun.id == models.SalarySlip.payroll_run_id)
        .where(models.SalarySlip.employee_id == employee_id)
        .order_by(models.PayrollRun.period_year.desc(), models.PayrollRun.period_month.desc())
        .limit(1)
    ).first()
    lines: dict[str, float] = {}
    deductions: list[dict] = []
    slip = None
    if slip_row:
        slip, _ = slip_row
        for line in db.scalars(
            select(models.SalarySlipLine)
            .options(selectinload(models.SalarySlipLine.component))
            .where(models.SalarySlipLine.slip_id == slip.id)
        ):
            lines[line.component.name] = _num(line.amount)
            if line.component.component_type in ("deduction", "tax"):
                deductions.append({"name": line.component.name, "amount": _num(line.amount)})
    # Matched by keyword, not an exact name, since a company's Basic/
    # Variable Pay component can be named however its admin set it up (e.g.
    # "Basic" vs "Basic Salary") -- same convention
    # payslip_html_renderer.build_payslip_placeholders' find_line already
    # uses, so this card doesn't silently show 0 whenever a tenant's naming
    # doesn't match one hardcoded exact string.
    basic_amount = next(
        (amt for name, amt in lines.items() if "basic" in name.lower()), 0
    )
    variable_amount = next(
        (amt for name, amt in lines.items() if "variable" in name.lower()), 0
    )
    return {
        "ctc": e.annual_ctc or 0,
        "basic": int(basic_amount),
        "gross": int(_num(slip.gross_pay)) if slip else 0,
        "net": int(_num(slip.net_pay)) if slip else 0,
        "variable": int(variable_amount),
        "pf": _dash(e.uan),
        "esi": _dash(e.esi_number),
        "pan": _dash(e.pan),
        "taxRegime": e.tax_regime or (tax.tax_regime if tax else "—"),
        "bank": _dash(e.bank_name),
        "account": _dash(e.bank_account_no),
        "ifsc": _dash(e.bank_ifsc),
        # Employee Profile > Payroll's deduction breakdown for the latest
        # slip (includes Payroll Settings > Deductions Configuration's LOP/
        # Asset Recovery lines when present) -- empty list when there's no
        # slip yet, never fabricated.
        "deductions": deductions,
    }


def get_employee_benefits(db: Session, employee_id: uuid.UUID) -> dict:
    ben = db.scalars(
        select(models.EmployeeBenefits).where(models.EmployeeBenefits.employee_id == employee_id)
    ).first()
    if ben is None:
        return {"insurance": "—", "dependents": 0, "esop": "0",
                "cab": "No", "meal": "No", "internet": "No",
                "wellness": "No", "learningBudget": "—"}
    return {
        "insurance": _dash(ben.insurance_plan),
        "dependents": ben.dependents_covered or 0,
        "esop": str(ben.esop_units) if ben.esop_units else "0",
        "cab": "Yes" if ben.cab_facility else "No",
        "meal": "Yes" if ben.meal_card else "No",
        "internet": "Yes" if ben.internet_reimbursement else "No",
        "wellness": "Yes" if ben.wellness_program else "No",
        "learningBudget": (
            f"{_num(ben.learning_budget_used)}/{_num(ben.learning_budget_total)}"
            if ben.learning_budget_total
            else "—"
        ),
    }


def get_employee_attendance(db: Session, employee_id: uuid.UUID) -> "dict | None":
    if _emp_or_none(db, employee_id) is None:
        return None
    emp = db.get(models.Employee, employee_id)
    today = company_today(db, emp.company_id if emp else None)
    records = db.scalars(
        select(models.AttendanceRecord).where(models.AttendanceRecord.employee_id == employee_id)
    ).all()
    # UI title reads "Attendance Summary -- <this month>", so every figure
    # below is scoped to the current calendar month, not lifetime totals
    # (previously present/absent/overtime silently summed every record ever
    # created for this employee while the header implied "this month").
    this_month = [
        r for r in records
        if r.attendance_date.month == today.month and r.attendance_date.year == today.year
    ]
    # "regularized" counts as present -- see the identical comment in
    # list_employees_full: an approved Attendance Regularization means the
    # employee attended and a manager corrected the punch, not an absence.
    present = sum(
        1 for r in this_month
        if r.status in ("present", "regularized", "early_in", "off_shift")
    )
    absent = sum(1 for r in this_month if r.status == "absent")
    # AttendanceRecord.status is genuinely set to "late" elsewhere (the
    # check-in-after-grace-period logic) -- previously hardcoded to 0 here
    # despite that real data existing.
    late = sum(1 for r in this_month if r.status == "late")
    this_month_hrs = sum(_num(r.work_hours) for r in this_month)
    overtime_hrs = sum(_num(getattr(r, "overtime_hours", None)) for r in this_month)
    # Real count of days this employee manually selected "WFH" for their
    # attendance record this month (AttendanceRecord.work_mode) -- not
    # inferred from anything else.
    wfh = sum(1 for r in this_month if getattr(r, "work_mode", None) == "WFH")
    return {
        "present": present, "absent": absent, "late": late, "wfh": wfh,
        "overtimeHrs": int(overtime_hrs),
        "thisMonthHrs": int(this_month_hrs),
    }


def get_employee_projects(db: Session, employee_id: uuid.UUID, company_id: uuid.UUID) -> "list[dict]":
    """[company_id] is the caller's own company (routers/employees.py already
    verified [employee_id] belongs to it) -- pm_resource_allocations has no
    company_id column of its own, so without also filtering the joined
    Project by company_id, an allocation whose project belongs to a
    DIFFERENT company would still surface that project's data here."""
    rows = db.execute(
        select(models.ProjectAllocation, models.Project)
        .join(models.Project, models.Project.id == models.ProjectAllocation.project_id)
        .options(
            selectinload(models.ProjectAllocation.project_role),
            joinedload(models.Project.project_manager),
        )
        .where(
            models.ProjectAllocation.employee_id == employee_id,
            models.Project.company_id == company_id,
        )
    ).all()
    # PERF-03: team leads / names / department / branch resolved once for
    # the whole result instead of 3-4 queries per row (the per-row
    # department/branch db.get()s weren't even served from the identity map
    # -- only .name was kept, so each object was collected and re-fetched).
    team_leads = resolve_project_team_lead_ids_bulk(db, [proj.id for _, proj in rows])
    tl_names = employee_display_names_bulk(db, team_leads.values())
    dept_names: dict = {}
    branch_names: dict = {}
    result = []
    for pa, proj in rows:
        dept_id, branch_id = allocation_department_branch(pa)
        tl_id = team_leads.get(proj.id)
        if dept_id not in dept_names:
            dept_names[dept_id] = department_display_name(db, dept_id)
        if branch_id not in branch_names:
            branch_names[branch_id] = branch_display_name(db, branch_id)
        # (at most one department + one branch lookup per request: every
        # row is the same employee)
        result.append({
            "name": proj.name,
            "role": pa.project_role.name if pa.project_role else "—",
            "allocation": pa.allocation_pct,
            "billable": proj.is_billable,
            "teamLead": tl_names.get(tl_id, "—") if tl_id else "—",
            "department": dept_names[dept_id],
            "branch": branch_names[branch_id],
            "projectManager": (
                _full_name(proj.project_manager)
                if getattr(proj, "project_manager_id", None)
                else "—"
            ),
        })
    return result


def get_employee_performance(db: Session, employee_id: uuid.UUID) -> dict:
    appraisal_row = db.execute(
        select(models.Appraisal, models.AppraisalCycle)
        .join(models.AppraisalCycle, models.AppraisalCycle.id == models.Appraisal.cycle_id)
        .where(models.Appraisal.employee_id == employee_id)
        .order_by(models.AppraisalCycle.to_date.desc())
        .limit(1)
    ).first()
    goals = db.scalars(
        select(models.Goal).where(models.Goal.employee_id == employee_id)
    ).all()
    pcts = [_num(g.progress_pct) for g in goals]
    completed_pct = round(sum(pcts) / len(pcts)) if pcts else 0
    appraisal, cycle = appraisal_row if appraisal_row else (None, None)
    recognitions = db.scalars(
        select(models.Recognition)
        .where(models.Recognition.employee_id == employee_id)
        .order_by(models.Recognition.given_on.desc())
    ).all()
    return {
        "rating": appraisal.final_rating if appraisal and appraisal.final_rating else "—",
        "goalsCompleted": f"{completed_pct}%",
        "lastReview": _iso(cycle.to_date) if cycle else "—",
        "nextReview": "—",
        "awards": [r.badge for r in recognitions],
    }


def get_employee_lifecycle(db: Session, employee_id: uuid.UUID) -> "dict | None":
    """Employee Profile > Lifecycle tab. Previously hardcoded to a constant
    (onboarding='—', transfers=0, promotions=0, exitStatus='—') with no
    fetch at all -- now a real, if currently sparse, query:
      - transfers/promotions: derived from hcm_employee_lifecycle_events
        exactly like routers/employees.py's list_lifecycle_events endpoint
        already classifies company-wide rows (a "promotion" is a transfer
        row whose designation actually changed; everything else tagged
        'transfer' counts as a transfer). This table has zero rows for
        every tenant today (see EmployeeLifecycleEvent's own docstring),
        so both are genuinely 0 right now -- but will reflect real events
        the moment record_employee_change starts writing rows for this
        employee, instead of never updating at all.
      - exitStatus: the employee's own most recent exit request status if
        one exists, else a human-readable Active/Inactive derived from
        Employee.status -- both real fields, unlike the previous constant.
      - onboarding: no onboarding-tracking table has an ORM mapping in
        this codebase at all (hcm_onboardings/hcm_onboarding_tasks exist
        in the DB but nothing here queries them) -- left as "—" since
        there is genuinely no data source to wire, not a masked value.
    """
    employee = _emp_or_none(db, employee_id)
    if employee is None:
        return None
    events = db.scalars(
        select(models.EmployeeLifecycleEvent)
        .where(models.EmployeeLifecycleEvent.employee_id == employee_id)
    ).all()
    transfers, promotions = _employee_lifecycle_counts(events)
    latest_exit = db.scalar(
        select(models.ExitRequestModel)
        .where(models.ExitRequestModel.employee_id == employee_id)
        .order_by(models.ExitRequestModel.created_at.desc())
    )
    exit_status = latest_exit.status.replace("_", " ").title() if latest_exit else employee.status.title()
    return {
        "onboarding": "—",
        "transfers": transfers,
        "promotions": promotions,
        "exitStatus": exit_status,
    }


def get_employee_documents(db: Session, employee_id: uuid.UUID) -> "list[dict]":
    rows = db.scalars(
        select(models.DocumentRecord).where(models.DocumentRecord.employee_id == employee_id)
    ).all()
    return [
        {"id": str(r.id), "type": r.document_type, "status": r.status, "date": _iso(r.uploaded_on), "file_url": r.file_url}
        for r in rows
    ]


def get_employee_assets(db: Session, employee_id: uuid.UUID) -> "list[dict]":
    rows = db.execute(
        select(models.AssetAssignment, models.AssetInventoryItem)
        .join(models.AssetInventoryItem, models.AssetInventoryItem.id == models.AssetAssignment.asset_id)
        .where(models.AssetAssignment.employee_id == employee_id)
    ).all()
    return [
        {"type": inv.asset_type, "tag": inv.asset_tag, "assigned": _iso(aa.assigned_on)}
        for aa, inv in rows
    ]


def get_employee_leave(db: Session, employee_id: uuid.UUID) -> "list[dict]":
    rows = db.execute(
        select(models.LeaveAllocation, models.LeaveType)
        .join(models.LeaveType, models.LeaveType.id == models.LeaveAllocation.leave_type_id)
        .where(models.LeaveAllocation.employee_id == employee_id)
    ).all()
    return [
        {
            "code": lt.code,
            "name": lt.name,
            "allocated": _num(la.allocated_days),
            "used": _num(la.used_days),
            # H-07: an over-drawn balance is reported as negative, not 0.
            "remaining": _num(la.allocated_days) + _num(la.carried_forward_days) - _num(la.used_days),
        }
        for la, lt in rows
    ]


def _employee_list_item(e: "models.Employee") -> dict:
    """Lightweight dict for the paginated directory — no satellite tables."""
    return {
        "id": str(e.id),
        "code": e.employee_code,
        "name": _full_name(e),
        "designation": e.designation.name if e.designation else "—",
        "department": e.department.name if e.department else "—",
        "branch": e.branch.name if e.branch else "—",
        "manager": _full_name(e.reporting_manager) if e.reporting_manager else "—",
        "reportingManagerId": str(e.reporting_manager_id) if e.reporting_manager_id else None,
        "dottedLineManagerId": str(e.dotted_line_manager_id) if e.dotted_line_manager_id else None,
        "type": e.employment_type,
        "mode": _dash(e.work_mode),
        "status": e.status,
        "email": _dash(e.work_email),
        "city": e.branch.city if e.branch and e.branch.city else "—",
        "doj": _iso(e.date_of_joining),
        "band": e.band or 0,
        "ctc": e.annual_ctc or 0,
    }


def escape_like(text: str) -> str:
    """Escape LIKE/ILIKE wildcards (use with escape='\\\\'): L-09."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_employees_paginated(
    db: Session,
    company_id: uuid.UUID,
    limit: int = 50,
    cursor: str | None = None,
    search: str | None = None,
    dept: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    visible_ids: set[uuid.UUID] | None = None,
) -> dict:
    """Cursor-based paginated employee directory — only summary fields,
    no satellite table joins, so it stays fast regardless of roster size.
    visible_ids (None = all) restricts the rows in the query itself, so
    pages and has_more stay correct."""
    q = (
        select(models.Employee)
        .options(
            selectinload(models.Employee.branch),
            selectinload(models.Employee.department),
            selectinload(models.Employee.designation),
            selectinload(models.Employee.reporting_manager),
        )
        .where(models.Employee.company_id == company_id, models.Employee.is_active.is_(True))
    )
    if search:
        # L-09: % and _ typed by the user are literal characters, not wildcards.
        pat = f"%{escape_like(search)}%"
        full_name = func.concat_ws(" ", models.Employee.first_name, models.Employee.last_name)
        q = q.where(
            or_(
                models.Employee.first_name.ilike(pat, escape="\\"),
                models.Employee.last_name.ilike(pat, escape="\\"),
                models.Employee.employee_code.ilike(pat, escape="\\"),
                # "Raj Kumar" matched nothing when each part was only
                # compared on its own column.
                full_name.ilike(f"%{escape_like(' '.join(search.split()))}%", escape="\\"),
            )
        )
    if dept:
        q = q.join(
            models.Department, models.Department.id == models.Employee.department_id
        ).where(models.Department.name == dept)
    if branch:
        q = q.join(
            models.Branch, models.Branch.id == models.Employee.branch_id
        ).where(models.Branch.name == branch)
    if status:
        # Stored statuses are mixed case ('active' / 'Active'): match either.
        q = q.where(func.lower(models.Employee.status) == status.strip().lower())
    if visible_ids is not None:
        q = q.where(models.Employee.id.in_(visible_ids or [uuid.UUID(int=0)]))

    # Keyset pagination: cursor encodes (first_name, id) of the last seen row
    # (opaque, see encode_cursor; invalid -> InvalidCursor -> 400).
    if cursor:
        last_fname, last_id = decode_cursor(cursor)
        q = q.where(
            (models.Employee.first_name > last_fname)
            | (
                (models.Employee.first_name == last_fname)
                & (models.Employee.id > last_id)
            )
        )

    q = q.order_by(models.Employee.first_name, models.Employee.id).limit(limit + 1)
    rows = db.scalars(q).all()

    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = encode_cursor(items[-1].first_name, items[-1].id) if has_more and items else None

    return {"items": [_employee_list_item(e) for e in items], "next_cursor": next_cursor, "has_more": has_more}


# ── HRMS Super Admin (platform-level) ───────────────────────────────────────

def open_tenant_session(tenant_slug: str) -> Session:
    """Opens a dedicated Session scoped to one tenant's schema search_path --
    same pattern provision_tenant.py's own _tenant_session() already uses
    and relies on. Used by platform-level code (this section) that needs to
    read/write inside a specific tenant's schema despite running under a
    super-admin token that carries no tenant_slug at all. Caller must
    db.close() it (and db.commit()/rollback() as appropriate) when done.

    P3: uses the app's shared engine/pool (database.SessionLocal) -- it used
    to build a brand-new Engine + connection pool on every call and never
    dispose it -- and routes via the session's tenant stamp, which
    database's after_begin listener applies as a QUOTED
    `SET search_path TO "<slug>", public` on every transaction (the old
    unquoted `-c search_path=Infyq` folded to `infyq` and silently missed
    that tenant). Deliberately doesn't touch the request's tenant
    ContextVar, so platform code can't leak a tenant into later work."""
    db = database.SessionLocal()
    database.set_session_tenant_slug(db, tenant_slug)
    return db


def tenant_effective_status(tenant: models.Tenant, today: datetime.date | None = None) -> str:
    """SA-13: the one place that turns a tenant row into what the HRMS
    actually enforces (auth_state.tenant_block_reason): blocked beats
    expired beats trial; a trial past trial_ends_at is expired even though
    nothing rewrites its stored status."""
    today = today or datetime.date.today()
    if tenant.status == "blocked" or tenant.is_active is False:
        return "blocked"
    if tenant.status == "expired":
        return "expired"
    if tenant.status == "trial" and tenant.trial_ends_at is not None and tenant.trial_ends_at < today:
        return "expired"
    return tenant.status or "active"


def create_platform_audit_log(
    db: Session,
    admin: models.AdminUser | None,
    action: str,
    tenant: models.Tenant | None = None,
    changes: dict | None = None,
) -> None:
    """SA-06: best-effort audit row for a Super Admin action, written in the
    caller's transaction. Never the reason a request fails: when the table
    is missing (backend/db/add_platform_audit_logs.sql not applied) the
    savepoint is rolled back and the action is simply not recorded."""
    try:
        with db.begin_nested():
            db.add(models.PlatformAuditLog(
                id=uuid.uuid4(),
                actor_admin_id=admin.id if admin is not None else None,
                actor_email=admin.email if admin is not None else None,
                action=action,
                tenant_id=tenant.id if tenant is not None else None,
                tenant_slug=tenant.slug if tenant is not None else None,
                changes=changes,
                created_at=datetime.datetime.now(datetime.timezone.utc),
            ))
    except Exception:
        logger.warning("platform audit log not written (action=%s)", action, exc_info=True)


# public.tenants is shared with the platform's other products (Retail,
# Finance, Manufacturing, Supply Chain, ...). A tenant belongs to HRMS
# only when its HCM module is enabled in public.tenant_modules -- the
# same row provision_tenant writes for every HRMS tenant. The Super Admin
# console must never list or act on the other products' tenants.
_tenant_modules = sa_table(
    "tenant_modules",
    sa_column("tenant_slug"),
    sa_column("module_code"),
    sa_column("is_enabled"),
    schema="public",
)
HRMS_MODULE_CODE = "hcm"


def _is_hrms_tenant_clause():
    return models.Tenant.slug.in_(
        select(_tenant_modules.c.tenant_slug).where(
            _tenant_modules.c.module_code == HRMS_MODULE_CODE,
            _tenant_modules.c.is_enabled.is_(True),
        )
    )


def is_contract_employee(employee) -> bool:
    """The one contract-employment test every contract branch uses (payroll
    statutory deductions, leave eligibility, contract lifecycle) -- the same
    normalisation as the gratuity rule in the exit / F&F calculation, since
    employment_type is stored as typed ("Contract", "contract", ...)."""
    et = (getattr(employee, "employment_type", "") or "").lower().replace("-", " ").replace("_", " ").strip()
    return et in ("fixed term", "contract")


class ContractActionError(ValueError):
    """A contract renewal / conversion that can't be applied (-> 409/422)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


CONTRACT_RATE_UNITS = ("hourly", "daily", "monthly")


def renew_contract(
    db: Session, employee: models.Employee, actor: models.User, *, new_end_date: datetime.date,
    new_rate_amount: float | None = None, new_rate_unit: str | None = None, notes: str | None = None,
) -> models.EmployeeLifecycleEvent:
    """HR-triggered renewal: a new (later) end date and, optionally, a new
    rate. Never automatic. Written to the employee's history and audit log;
    the contract-end reminders then follow the new end date."""
    if not is_contract_employee(employee):
        raise ContractActionError(409, "Only contract employees have a contract to renew.")
    today = company_today(db, employee.company_id)
    if new_end_date <= today:
        raise ContractActionError(422, "The new contract end date must be in the future.")
    if employee.contract_end_date and new_end_date <= employee.contract_end_date:
        raise ContractActionError(422, "A renewal must end after the current contract end date "
                                       f"({employee.contract_end_date.isoformat()}).")
    if new_rate_amount is not None and new_rate_amount <= 0:
        raise ContractActionError(422, "The contract rate must be more than zero.")
    unit = (new_rate_unit or "").strip().lower() or None
    if unit is not None and unit not in CONTRACT_RATE_UNITS:
        raise ContractActionError(422, "Rate unit must be hourly, daily or monthly.")
    if unit is not None and new_rate_amount is None and employee.contract_rate_amount is None:
        raise ContractActionError(422, "Give the new rate together with its unit.")

    old = (employee.contract_end_date, employee.contract_rate_amount, employee.contract_rate_unit)
    employee.contract_end_date = new_end_date
    if new_rate_amount is not None:
        employee.contract_rate_amount = new_rate_amount
    if unit is not None:
        employee.contract_rate_unit = unit
    employee.updated_by = actor.id
    event = models.EmployeeLifecycleEvent(
        id=uuid.uuid4(), employee_id=employee.id, event_type="contract_renewal", event_date=today,
        from_contract_end_date=old[0], to_contract_end_date=new_end_date,
        from_contract_rate=old[1], to_contract_rate=employee.contract_rate_amount,
        contract_rate_unit=employee.contract_rate_unit, notes=(notes or "").strip() or None,
    )
    db.add(event)
    create_audit_log(db, employee.company_id, actor.id, "contract_renew", "employee", employee.id, changes={
        "before": {"contract_end_date": old[0].isoformat() if old[0] else None,
                   "contract_rate_amount": float(old[1]) if old[1] is not None else None, "contract_rate_unit": old[2]},
        "after": {"contract_end_date": new_end_date.isoformat(),
                  "contract_rate_amount": float(employee.contract_rate_amount) if employee.contract_rate_amount is not None else None,
                  "contract_rate_unit": employee.contract_rate_unit},
    })
    return event


def convert_contract_to_permanent(
    db: Session, employee: models.Employee, actor: models.User, *, notes: str | None = None,
) -> models.EmployeeLifecycleEvent:
    """HR-triggered: contract -> permanent full-time. Clears the contract
    end date and rate; from here on the employee is paid from their salary
    structure, with statutory deductions, and accrues leave like any
    full-time employee. Logged to history and the audit log."""
    if not is_contract_employee(employee):
        raise ContractActionError(409, "Only contract employees can be converted to permanent.")
    today = company_today(db, employee.company_id)
    old = (employee.employment_type, employee.contract_end_date, employee.contract_rate_amount, employee.contract_rate_unit)
    employee.employment_type = "full_time"
    employee.contract_end_date = None
    employee.contract_rate_amount = None
    employee.contract_rate_unit = None
    employee.updated_by = actor.id
    event = models.EmployeeLifecycleEvent(
        id=uuid.uuid4(), employee_id=employee.id, event_type="contract_conversion", event_date=today,
        from_contract_end_date=old[1], to_contract_end_date=None,
        from_contract_rate=old[2], to_contract_rate=None, contract_rate_unit=old[3],
        notes=(notes or "").strip() or None,
    )
    db.add(event)
    create_audit_log(db, employee.company_id, actor.id, "contract_convert", "employee", employee.id, changes={
        "before": {"employment_type": old[0], "contract_end_date": old[1].isoformat() if old[1] else None,
                   "contract_rate_amount": float(old[2]) if old[2] is not None else None, "contract_rate_unit": old[3]},
        "after": {"employment_type": "full_time", "contract_end_date": None,
                  "contract_rate_amount": None, "contract_rate_unit": None},
    })
    return event


def has_salary_structure(db: Session, employee_id: uuid.UUID) -> bool:
    return db.scalar(
        select(models.SalaryStructureAssignment.id).where(models.SalaryStructureAssignment.employee_id == employee_id).limit(1)
    ) is not None


def list_hrms_admins(db: Session) -> list[models.Tenant]:
    return list(
        db.scalars(
            select(models.Tenant)
            .where(_is_hrms_tenant_clause())
            .order_by(models.Tenant.created_at.desc())
        ).all()
    )


def get_hrms_admin(db: Session, tenant_id: uuid.UUID) -> models.Tenant | None:
    return db.scalar(
        select(models.Tenant).where(models.Tenant.id == tenant_id, _is_hrms_tenant_clause())
    )


def create_hrms_admin(db: Session, payload: "schemas.HrmsAdminCreate") -> models.Tenant:
    """Wraps provision_tenant.provision_tenant() (the exact same tested
    tenant-bootstrap used by the CLI runbook: clones the _template schema,
    registers public.tenants, creates the Company + full RBAC template +
    Head Office/Administration + the Organization Owner's real login)
    behind this API call, then layers the HRMS Super Admin's own
    plan/trial/contact fields onto the resulting public.tenants row."""
    from . import provision_tenant as provision_tenant_module

    name_parts = payload.contact_name.strip().split(None, 1)
    owner_first_name = name_parts[0] if name_parts else payload.contact_name
    owner_last_name = name_parts[1] if len(name_parts) > 1 else ""
    slug = payload.tenant_slug.strip().lower()

    # C17: plan/trial/contact fields are written by provision_tenant inside
    # the same single transaction as the schema clone, so a failure anywhere
    # leaves neither an orphan schema nor a half-configured tenant row.
    tenant_fields = {
        "contact_name": payload.contact_name,
        "contact_email": payload.contact_email,
        "contact_phone": payload.contact_phone,
        "plan_type": payload.plan_type,
    }
    if payload.plan_type == "trial":
        trial_days = payload.trial_days or get_or_create_platform_settings(db).trial_days
        tenant_fields["status"] = "trial"
        tenant_fields["trial_ends_at"] = datetime.date.today() + datetime.timedelta(days=trial_days)
    else:
        tenant_fields["status"] = "active"
        tenant_fields["trial_ends_at"] = None

    provision_tenant_module.provision_tenant(
        tenant_slug=slug,
        company_name=payload.company_name,
        company_legal_name=payload.company_name,
        owner_first_name=owner_first_name,
        owner_last_name=owner_last_name,
        owner_email=payload.contact_email,
        owner_password=payload.owner_password,
        tenant_fields=tenant_fields,
    )

    tenant = db.scalar(select(models.Tenant).where(models.Tenant.slug == slug))
    if tenant is None:
        raise ValueError("Tenant provisioning did not produce a public.tenants row")
    return tenant


def update_hrms_admin(db: Session, tenant_id: uuid.UUID, updates: dict) -> models.Tenant | None:
    tenant = get_hrms_admin(db, tenant_id)
    if tenant is None:
        return None
    if updates.get("company_name") is not None:
        tenant.name = updates["company_name"]
    for field in ("contact_name", "contact_email", "contact_phone"):
        if updates.get(field) is not None:
            setattr(tenant, field, updates[field])
    db.flush()
    return tenant


def toggle_hrms_admin_status(db: Session, tenant_id: uuid.UUID) -> models.Tenant | None:
    tenant = get_hrms_admin(db, tenant_id)
    if tenant is None:
        return None
    # SA-08: blocked means either flag (that is what tenant_block_reason
    # enforces), so one click always unblocks.
    if tenant.status == "blocked" or tenant.is_active is False:
        tenant.status = "active" if tenant.plan_type != "trial" else "trial"
        tenant.is_active = True
    else:
        tenant.status = "blocked"
        tenant.is_active = False
    db.flush()
    return tenant


def extend_hrms_admin_trial(db: Session, tenant_id: uuid.UUID, days: int) -> models.Tenant | None:
    tenant = get_hrms_admin(db, tenant_id)
    if tenant is None:
        return None
    # SA-09: extending a trial neither unblocks a blocked tenant nor turns a
    # paying tenant back into a trial (set-plan does that).
    if tenant.plan_type != "trial":
        raise ValueError("Only trial organizations can have their trial extended.")
    base = (
        tenant.trial_ends_at
        if tenant.trial_ends_at and tenant.trial_ends_at >= datetime.date.today()
        else datetime.date.today()
    )
    tenant.trial_ends_at = base + datetime.timedelta(days=days)
    if tenant.status != "blocked" and tenant.is_active is not False:
        tenant.status = "trial"
    db.flush()
    return tenant


def set_hrms_admin_plan(db: Session, tenant_id: uuid.UUID, plan_type: str) -> models.Tenant | None:
    tenant = get_hrms_admin(db, tenant_id)
    if tenant is None:
        return None
    tenant.plan_type = plan_type
    blocked = tenant.status == "blocked" or tenant.is_active is False
    if plan_type == "trial":
        if tenant.trial_ends_at is None or tenant.trial_ends_at < datetime.date.today():
            tenant.trial_ends_at = datetime.date.today() + datetime.timedelta(
                days=get_or_create_platform_settings(db).trial_days
            )
    else:
        tenant.trial_ends_at = None
    # SA-08: a plan change never unblocks; toggle-status does that.
    if not blocked:
        tenant.status = "trial" if plan_type == "trial" else "active"
    db.flush()
    return tenant


def reset_hrms_admin_owner_password(db: Session, tenant: models.Tenant, new_password: str) -> bool:
    """Resets the password of that tenant's admin contact login -- reuses
    reset_user_password (already built for the main app's own
    Administration > User Roles "Reset Password" action), run inside a
    Session scoped to this specific tenant's schema.

    Looked up by tenant.contact_email (via find_user_by_email, which
    already handles the work_email-over-core_users.email precedence),
    NOT by "the one Company row's Owner role" -- some existing tenant
    schemas (e.g. a shared/legacy multi-company schema like 'acme') hold
    more than one Company's worth of seed data, so "grab a single Company
    and its Owner role" isn't reliable there. Matching by the contact
    email the Super Admin themselves recorded for this tenant is
    unambiguous regardless of how that schema's data is shaped."""
    tenant_db = open_tenant_session(tenant.slug)
    try:
        user = find_user_by_email(tenant_db, tenant.contact_email) if tenant.contact_email else None
        if user is None:
            # SA-10: no (or edited) contact email -> the tenant's single
            # Organization Owner login, when that is unambiguous.
            owners = tenant_db.scalars(
                select(models.User)
                .join(models.UserRole, models.UserRole.user_id == models.User.id)
                .join(models.Role, models.Role.id == models.UserRole.role_id)
                .where(models.Role.name == BUILTIN_ROLES[0])
            ).unique().all()
            user = owners[0] if len(owners) == 1 else None
        if user is None:
            return False
        new_hash = security.hash_password(new_password)
        ok = reset_user_password(tenant_db, user.id, new_hash)
        if ok:
            # SA-10: a reset also lifts any failed-login lockout.
            from . import auth_state

            auth_state.reset_failed_logins(
                tenant_db, (user.email or "").strip().lower(), tenant_db.get(models.PublicUser, user.id)
            )
        tenant_db.commit()
        return ok
    except Exception:
        tenant_db.rollback()
        raise
    finally:
        tenant_db.close()


def compute_super_admin_analytics(db: Session) -> dict:
    tenants = list_hrms_admins(db)
    settings_row = get_or_create_platform_settings(db)
    today = datetime.date.today()
    # SA-13: counted by the status the HRMS actually enforces; MRR only
    # from paying tenants that can use the product.
    effective = [tenant_effective_status(t, today) for t in tenants]
    active_count = effective.count("active")
    trial_count = effective.count("trial")
    expired_count = effective.count("expired")
    blocked_count = effective.count("blocked")
    monthly_count = sum(1 for t, e in zip(tenants, effective) if t.plan_type == "monthly" and e == "active")
    yearly_count = sum(1 for t, e in zip(tenants, effective) if t.plan_type == "yearly" and e == "active")
    mrr = monthly_count * float(settings_row.monthly_price) + yearly_count * float(
        settings_row.yearly_price
    ) / 12

    by_month: dict[str, int] = {}
    for t in tenants:
        key = t.created_at.strftime("%Y-%m") if t.created_at else today.strftime("%Y-%m")
        by_month[key] = by_month.get(key, 0) + 1
    signup_by_month = [{"month": k, "count": v} for k, v in sorted(by_month.items())]

    return {
        "total_admins": len(tenants),
        "active_count": active_count,
        "trial_count": trial_count,
        "expired_count": expired_count,
        "blocked_count": blocked_count,
        "monthly_count": monthly_count,
        "yearly_count": yearly_count,
        "mrr": round(mrr, 2),
        "signup_by_month": signup_by_month,
    }


def get_or_create_platform_settings(db: Session) -> models.PlatformSettings:
    row = db.scalar(select(models.PlatformSettings).limit(1))
    if row is not None:
        return row
    row = models.PlatformSettings(id=uuid.uuid4())
    db.add(row)
    db.flush()
    return row


def update_platform_settings(db: Session, updates: dict) -> models.PlatformSettings:
    row = get_or_create_platform_settings(db)
    for field, value in updates.items():
        # SA-05: the Stripe secret is write-only; a blank field keeps it.
        if field == "stripe_secret_key" and not (value or "").strip():
            continue
        if value is not None:
            setattr(row, field, value)
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()
    return row


def list_platform_notifications(db: Session) -> list[models.PlatformNotification]:
    return list(
        db.scalars(
            select(models.PlatformNotification).order_by(models.PlatformNotification.created_at.desc())
        ).all()
    )


def create_platform_notification(
    db: Session, title: str, body: str, target_segment: str, created_by: uuid.UUID | None
) -> models.PlatformNotification:
    notif = models.PlatformNotification(
        id=uuid.uuid4(), title=title, body=body, target_segment=target_segment, created_by=created_by,
    )
    db.add(notif)
    db.flush()
    return notif


def _broadcast_targets(tenants: list[models.Tenant], segment: str) -> list[models.Tenant]:
    """Same segments the Broadcast page previews (broadcast_page.dart
    _targetAdmins), evaluated on the enforced status."""
    today = datetime.date.today()
    out = []
    for t in tenants:
        status = tenant_effective_status(t, today)
        if status in ("blocked", "expired"):
            continue
        if segment == "trial" and t.plan_type != "trial":
            continue
        if segment == "expiring" and not (
            t.plan_type == "trial"
            and t.trial_ends_at is not None
            and 0 <= (t.trial_ends_at - today).days <= 7
        ):
            continue
        if segment == "active" and t.plan_type not in ("monthly", "yearly"):
            continue
        out.append(t)
    return out


def deliver_platform_broadcast(db: Session, title: str, body: str, segment: str) -> int:
    """SA-07: puts a Super Admin broadcast into the in-app notifications of
    every Organization Owner of each targeted tenant. Best-effort per tenant
    (one tenant failing never blocks the others). Returns how many
    organizations received it."""
    delivered = 0
    for tenant in _broadcast_targets(list_hrms_admins(db), segment):
        tenant_db = open_tenant_session(tenant.slug)
        try:
            owners = tenant_db.execute(
                select(models.User.id, models.User.company_id)
                .join(models.UserRole, models.UserRole.user_id == models.User.id)
                .join(models.Role, models.Role.id == models.UserRole.role_id)
                .where(models.Role.name == BUILTIN_ROLES[0])
            ).all()
            for user_id, company_id in owners:
                create_notification(tenant_db, company_id, user_id, title, body, entity_type="platform_broadcast")
            tenant_db.commit()
            if owners:
                delivered += 1
        except Exception:
            tenant_db.rollback()
            logger.warning("broadcast not delivered to tenant %s", tenant.slug, exc_info=True)
        finally:
            tenant_db.close()
    return delivered


def mark_platform_notification_read(db: Session, notification_id: uuid.UUID) -> bool:
    notif = db.get(models.PlatformNotification, notification_id)
    if notif is None:
        return False
    notif.is_read = True
    db.flush()
    return True


def mark_all_platform_notifications_read(db: Session) -> None:
    db.execute(update(models.PlatformNotification).values(is_read=True))
    db.flush()


def delete_platform_notification(db: Session, notification_id: uuid.UUID) -> bool:
    notif = db.get(models.PlatformNotification, notification_id)
    if notif is None:
        return False
    db.delete(notif)
    db.flush()
    return True


def list_super_admin_users(db: Session) -> list[models.AdminUser]:
    return list(
        db.scalars(
            select(models.AdminUser)
            .where(models.AdminUser.role == PLATFORM_SUPER_ADMIN_ROLE)
            .where(~select(models.PublicUser.id).where(models.PublicUser.id == models.AdminUser.id).exists())
            .order_by(models.AdminUser.created_at.desc())
        ).all()
    )


def create_super_admin_user(db: Session, full_name: str, email: str, password: str) -> models.AdminUser:
    admin = models.AdminUser(
        id=uuid.uuid4(),
        email=email.strip().lower(),
        password_hash=security.hash_password(password),
        full_name=full_name,
        role="super_admin",
        is_active=True,
    )
    db.add(admin)
    db.flush()
    return admin


def list_platform_audit_logs(db: Session, limit: int = 100, offset: int = 0) -> list[dict]:
    """Cross-tenant aggregation: every tenant's core_audit_logs, merged and
    sorted newest-first, paged IN SQL (P3): one UNION ALL over every tenant
    schema that actually has the table, with a single ORDER BY/LIMIT/OFFSET
    -- previously each tenant was capped at `limit` rows before the merge and
    then sliced by `offset`, so every page after the first was wrong, and a
    new Engine was created per tenant per request. Schema names are always
    double-quoted (a slug like "Infyq" must not fold to lower case) and only
    come from public.tenants rows cross-checked against information_schema."""
    from sqlalchemy import text as sql_text

    tenants = {t.slug: t.name for t in list_hrms_admins(db)}
    if not tenants:
        return []
    present = set(
        db.execute(
            sql_text(
                "SELECT table_schema FROM information_schema.tables "
                "WHERE table_name = 'core_audit_logs' AND table_schema = ANY(:schemas)"
            ),
            {"schemas": list(tenants)},
        ).scalars().all()
    )
    slugs = [slug for slug in tenants if slug in present]
    params: dict = {"lim": limit, "off": offset}
    selects = []
    # SA-06: the Super Admin's own actions, alongside every tenant's log.
    if db.execute(sql_text("SELECT to_regclass('public.platform_audit_logs')")).scalar() is not None:
        selects.append(
            "SELECT COALESCE(tenant_slug, 'platform') AS tenant_slug, action, "
            "'platform' AS doctype, CAST(tenant_id AS text) AS document_id, "
            "jsonb_build_object('by', actor_email) || CASE WHEN jsonb_typeof(changes) = 'object' "
            "THEN changes ELSE '{}'::jsonb END AS changes, "
            "created_at FROM public.platform_audit_logs"
        )
    if not slugs and not selects:
        return []
    for i, slug in enumerate(slugs):
        params[f"slug{i}"] = slug
        quoted = '"' + slug.replace('"', '""') + '"'
        selects.append(
            f"SELECT :slug{i} AS tenant_slug, action, doctype, CAST(document_id AS text), "
            f"CAST(changes AS jsonb), created_at FROM {quoted}.core_audit_logs"
        )
    rows = db.execute(
        sql_text(
            " UNION ALL ".join(selects)
            + " ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        ),
        params,
    ).fetchall()
    return [
        {
            "tenant_slug": r.tenant_slug,
            "company_name": tenants.get(r.tenant_slug) or "Platform",
            "action": r.action,
            "doctype": r.doctype,
            "document_id": str(r.document_id) if r.document_id else None,
            "changes": r.changes,
            "created_at": r.created_at,
        }
        for r in rows
    ]


# ── Experience & Relieving Letters ───────────────────────────────────────────
# Documents > Templates (per-company HTML+CSS template per letter type) and
# People > Offboarding > Experience & Relieving Documents (generated,
# immutable, versioned letters). See exit_letter_html_renderer.py.

# experience_relieving: the combined service certificate + relieving letter.
# fnf_statement: the Full & Final Settlement Statement (see "Full & Final
# Settlement" below) -- same template / generation / history machinery.
EXIT_LETTER_TYPES = ("experience_relieving", "fnf_statement")


def get_exit_letter_template(
    db: Session, company_id: uuid.UUID, letter_type: str
) -> "models.ExitLetterHtmlTemplate | None":
    """The ACTIVE exit-letter template for [letter_type] -- a company may
    have several saved templates per letter_type (see
    list_exit_letter_templates), at most one active (see
    get_payslip_html_template's docstring for the shared shape)."""
    return db.scalar(
        select(models.ExitLetterHtmlTemplate).where(
            models.ExitLetterHtmlTemplate.company_id == company_id,
            models.ExitLetterHtmlTemplate.letter_type == letter_type,
            models.ExitLetterHtmlTemplate.is_active == True,  # noqa: E712
        )
    )


def list_exit_letter_templates(db: Session, company_id: uuid.UUID, letter_type: str) -> list["models.ExitLetterHtmlTemplate"]:
    return _list_html_templates(db, models.ExitLetterHtmlTemplate, company_id,
                                extra=(models.ExitLetterHtmlTemplate.letter_type, letter_type))


def get_exit_letter_template_by_id(db: Session, company_id: uuid.UUID, letter_type: str, template_id: uuid.UUID):
    return _get_html_template_by_id(db, models.ExitLetterHtmlTemplate, company_id, template_id,
                                    extra=(models.ExitLetterHtmlTemplate.letter_type, letter_type))


def create_exit_letter_template(
    db: Session, company_id: uuid.UUID, letter_type: str, *, name: str, html_body: str, css_styles: str,
    is_active: bool, signatory_name: str | None, signatory_designation: str | None, created_by: uuid.UUID | None,
) -> "models.ExitLetterHtmlTemplate":
    now = datetime.datetime.now(datetime.timezone.utc)
    row = models.ExitLetterHtmlTemplate(
        id=uuid.uuid4(), company_id=company_id, letter_type=letter_type, name=name, html_body=html_body,
        css_styles=css_styles, is_active=False, version=1,
        signatory_name=(signatory_name or "").strip() or None,
        signatory_designation=(signatory_designation or "").strip() or None,
        updated_by=created_by, created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    if is_active:
        _deactivate_sibling_templates(db, models.ExitLetterHtmlTemplate, company_id, row.id,
                                      extra=(models.ExitLetterHtmlTemplate.letter_type, letter_type))
        db.flush()
        row.is_active = True
        db.flush()
    return row


def update_exit_letter_template_by_id(
    db: Session, company_id: uuid.UUID, letter_type: str, template_id: uuid.UUID, *, name: str, html_body: str,
    css_styles: str, is_active: bool, signatory_name: str | None, signatory_designation: str | None,
    updated_by: uuid.UUID | None,
) -> "models.ExitLetterHtmlTemplate":
    row = get_exit_letter_template_by_id(db, company_id, letter_type, template_id)
    if row is None:
        raise ValueError("Template not found.")
    row.name, row.html_body, row.css_styles = name, html_body, css_styles
    row.signatory_name = (signatory_name or "").strip() or None
    row.signatory_designation = (signatory_designation or "").strip() or None
    row.version += 1
    row.updated_by = updated_by
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    if is_active:
        _deactivate_sibling_templates(db, models.ExitLetterHtmlTemplate, company_id, row.id,
                                      extra=(models.ExitLetterHtmlTemplate.letter_type, letter_type))
        db.flush()
    row.is_active = is_active
    db.flush()
    return row


def delete_exit_letter_template(db: Session, company_id: uuid.UUID, letter_type: str, template_id: uuid.UUID) -> None:
    _delete_html_template(db, models.ExitLetterHtmlTemplate, company_id, template_id,
                          extra=(models.ExitLetterHtmlTemplate.letter_type, letter_type))


def activate_exit_letter_template(db: Session, company_id: uuid.UUID, letter_type: str, template_id: uuid.UUID) -> "models.ExitLetterHtmlTemplate":
    return _activate_html_template(db, models.ExitLetterHtmlTemplate, company_id, template_id,
                                   extra=(models.ExitLetterHtmlTemplate.letter_type, letter_type))


# ── Email templates (Documents > Templates > Email Templates) ───────────────
# Same multi-template/one-active shape as the document templates above, keyed
# by email_kind instead of letter_type. No "get the active one" convenience
# getter is needed here the way the three document types have one --
# email_service.render_email() queries hcm_email_templates directly (see
# email_service._custom_email_template) since it already has db+company_id
# in scope at every call site.

def list_email_templates(db: Session, company_id: uuid.UUID, email_kind: str) -> list["models.EmailHtmlTemplate"]:
    return _list_html_templates(db, models.EmailHtmlTemplate, company_id,
                                extra=(models.EmailHtmlTemplate.email_kind, email_kind))


def get_email_template_by_id(db: Session, company_id: uuid.UUID, template_id: uuid.UUID):
    return _get_html_template_by_id(db, models.EmailHtmlTemplate, company_id, template_id)


def create_email_template(
    db: Session, company_id: uuid.UUID, email_kind: str, *, name: str, html_body: str, css_styles: str,
    is_active: bool, created_by: uuid.UUID | None,
) -> "models.EmailHtmlTemplate":
    now = datetime.datetime.now(datetime.timezone.utc)
    row = models.EmailHtmlTemplate(
        id=uuid.uuid4(), company_id=company_id, email_kind=email_kind, name=name, html_body=html_body,
        css_styles=css_styles, is_active=False, version=1,
        created_by=created_by, updated_by=created_by, created_at=now, updated_at=now,
    )
    db.add(row)
    db.flush()
    if is_active:
        _deactivate_sibling_templates(db, models.EmailHtmlTemplate, company_id, row.id,
                                      extra=(models.EmailHtmlTemplate.email_kind, email_kind))
        db.flush()
        row.is_active = True
        db.flush()
    return row


def update_email_template_by_id(
    db: Session, company_id: uuid.UUID, template_id: uuid.UUID, *, name: str, html_body: str,
    css_styles: str, is_active: bool, updated_by: uuid.UUID | None,
) -> "models.EmailHtmlTemplate":
    row = get_email_template_by_id(db, company_id, template_id)
    if row is None:
        raise ValueError("Template not found.")
    row.name, row.html_body, row.css_styles = name, html_body, css_styles
    row.version += 1
    row.updated_by = updated_by
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    if is_active:
        _deactivate_sibling_templates(db, models.EmailHtmlTemplate, company_id, row.id,
                                      extra=(models.EmailHtmlTemplate.email_kind, row.email_kind))
        db.flush()
    row.is_active = is_active
    db.flush()
    return row


def delete_email_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID) -> None:
    _delete_html_template(db, models.EmailHtmlTemplate, company_id, template_id)


def activate_email_template(db: Session, company_id: uuid.UUID, template_id: uuid.UUID) -> "models.EmailHtmlTemplate":
    row = get_email_template_by_id(db, company_id, template_id)
    if row is None:
        raise ValueError("Template not found.")
    return _activate_html_template(db, models.EmailHtmlTemplate, company_id, template_id,
                                   extra=(models.EmailHtmlTemplate.email_kind, row.email_kind))


def upsert_exit_letter_template(
    db: Session,
    company_id: uuid.UUID,
    letter_type: str,
    *,
    name: str,
    html_body: str,
    css_styles: str,
    is_active: bool,
    signatory_name: str | None,
    signatory_designation: str | None,
    updated_by: uuid.UUID | None,
) -> "models.ExitLetterHtmlTemplate":
    """Creates the company's template for [letter_type] or updates it in
    place and bumps `version` -- same semantics as
    upsert_offer_letter_html_template. Letters generated earlier keep their
    own snapshot, so editing never changes an issued letter."""
    now = datetime.datetime.now(datetime.timezone.utc)
    row = get_exit_letter_template(db, company_id, letter_type)
    if row is None:
        row = models.ExitLetterHtmlTemplate(
            id=uuid.uuid4(), company_id=company_id, letter_type=letter_type, version=1,
            created_at=now,
        )
        db.add(row)
    else:
        row.version += 1
    row.name = name
    row.html_body = html_body
    row.css_styles = css_styles
    row.is_active = is_active
    row.signatory_name = (signatory_name or "").strip() or None
    row.signatory_designation = (signatory_designation or "").strip() or None
    row.updated_by = updated_by
    row.updated_at = now
    db.flush()
    return row


def get_exit_letter_detail(
    db: Session, company_id: uuid.UUID, exit_request_id: uuid.UUID
) -> dict | None:
    """The real records an exit letter is built from, tenant-scoped."""
    exit_request = db.get(models.ExitRequestModel, exit_request_id)
    if exit_request is None:
        return None
    employee = db.get(models.Employee, exit_request.employee_id)
    if employee is None or employee.company_id != company_id:
        return None
    settlement = db.scalar(
        select(models.FinalSettlement)
        .where(models.FinalSettlement.exit_id == exit_request.id)
        .limit(1)
    )
    return {
        "exit_request": exit_request,
        "employee": employee,
        "designation": employee.designation,
        "department": employee.department,
        "branch": employee.branch,
        "reporting_manager": employee.reporting_manager,
        "settlement": settlement,
    }


def exit_letter_eligibility(
    db: Session,
    company_id: uuid.UUID,
    exit_request: "models.ExitRequestModel",
    letter_type: str = "experience_relieving",
) -> tuple[bool, str]:
    """Experience & Relieving Letter: eligible once the exit is complete --
    status 'completed' (clearance finished), or 'approved' with the last
    working day (company timezone) reached. Submitted / pending / sent back
    / in clearance / rejected are not.

    F&F Settlement Statement: once the settlement is approved (or paid) and
    has an itemised breakdown -- see fnf_statement_eligibility."""
    if letter_type == "fnf_statement":
        return fnf_statement_eligibility(db, exit_request)
    if exit_request.status == "completed":
        return True, "Eligible (exit completed)"
    if exit_request.status != "approved":
        return False, f"The exit is {exit_request.status.replace('_', ' ')} -- letters can be generated once it is approved and the last working day is reached, or it is completed."
    if exit_request.last_working_day is None:
        return False, "No last working day is set on the approved exit request."
    today = company_today(db, company_id)
    if exit_request.last_working_day > today:
        return False, (
            f"The employee's last working day is {exit_request.last_working_day.strftime('%d %b %Y')} -- "
            "letters can be generated from that date."
        )
    return True, "Eligible"


def list_exit_letters(
    db: Session,
    company_id: uuid.UUID,
    *,
    employee_id: uuid.UUID | None = None,
    exit_request_id: uuid.UUID | None = None,
    current_only: bool = False,
) -> list["models.ExitLetter"]:
    q = select(models.ExitLetter).where(models.ExitLetter.company_id == company_id)
    if employee_id is not None:
        q = q.where(models.ExitLetter.employee_id == employee_id)
    if exit_request_id is not None:
        q = q.where(models.ExitLetter.exit_request_id == exit_request_id)
    if current_only:
        q = q.where(models.ExitLetter.is_current.is_(True))
    return list(db.scalars(q.order_by(
        models.ExitLetter.letter_type, models.ExitLetter.version.desc()
    )).all())


def create_exit_letter(
    db: Session,
    company: "models.Company",
    detail: dict,
    letter_type: str,
    template: "models.ExitLetterHtmlTemplate",
    *,
    generated_by_user: "models.User",
    notes: str | None,
) -> "models.ExitLetter":
    """Renders [template] with the employee's real data, stores the PDF and
    records an immutable snapshot as a new version (previous versions stay,
    marked not current)."""
    from . import exit_letter_html_renderer as renderer
    from .storage import uploads_root, winlong_path

    exit_request = detail["exit_request"]
    employee = detail["employee"]
    # Serialise concurrent generations for the same exit (double click, two
    # HR users): the second waits here and then gets the next version.
    db.execute(
        select(models.ExitRequestModel.id)
        .where(models.ExitRequestModel.id == exit_request.id)
        .with_for_update()
    )
    previous = list_exit_letters(db, company.id, exit_request_id=exit_request.id)
    previous = [p for p in previous if p.letter_type == letter_type]
    version = (max((p.version for p in previous), default=0)) + 1
    issue_date = company_today(db, company.id)
    placeholders = renderer.build_exit_letter_placeholders(
        detail, company, letter_type,
        signatory_name=template.signatory_name,
        signatory_designation=template.signatory_designation,
        issue_date=issue_date, version=version,
        seal_image_path=template.seal_image_path,
        signature_image_path=template.signature_image_path,
    )
    rendered = renderer.render_exit_letter_html(template.html_body, template.css_styles, placeholders)
    pdf_bytes = renderer.render_html_to_pdf(rendered)

    letter_id = uuid.uuid4()
    folder = uploads_root() / "exit_letter" / str(employee.id)
    winlong_path(folder).mkdir(parents=True, exist_ok=True)
    file_name = f"{letter_id}.pdf"
    winlong_path(folder / file_name).write_bytes(pdf_bytes)

    for p in previous:
        p.is_current = False
    row = models.ExitLetter(
        id=letter_id,
        company_id=company.id,
        employee_id=employee.id,
        exit_request_id=exit_request.id,
        letter_type=letter_type,
        version=version,
        letter_number=placeholders["letter_number"],
        template_id=template.id,
        template_name=template.name,
        template_version=template.version,
        rendered_html=rendered,
        # Only the (non-image) values -- the logo / seal / signature data
        # URLs are not needed to know what the letter said (the rendered
        # HTML above still embeds them).
        placeholders={k: v for k, v in placeholders.items() if k not in renderer.IMAGE_PLACEHOLDERS},
        file_url=f"exit_letter/{employee.id}/{file_name}",
        file_size=len(pdf_bytes),
        is_current=True,
        notes=(notes or "").strip() or None,
        generated_by=generated_by_user.id,
        generated_by_name=(
            employee_display_name(db, generated_by_user.employee_id)
            if generated_by_user.employee_id else None
        ),
        generated_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def exit_letter_file_bytes(letter: "models.ExitLetter") -> bytes | None:
    from .storage import uploads_root, winlong_path

    root = uploads_root().resolve()
    path = (root / letter.file_url).resolve()
    if root not in path.parents:
        return None
    long_path = winlong_path(path)
    return long_path.read_bytes() if long_path.is_file() else None


# ── Full & Final Settlement ─────────────────────────────────────────────────
# Payroll > Full & Final Settlements and People > Offboarding. HR enters
# every earning / deduction (hcm_final_settlement_lines); the totals on
# hcm_final_settlements are always recomputed from those lines. Workflow:
#   draft --approve--> approved --mark paid--> paid
#   approved --reopen--> draft            (not once paid)
# The F&F Settlement Statement (letter_type 'fnf_statement') is generated
# from an approved / paid settlement with the Experience & Relieving letter
# machinery above. fnf_reference_items lists the employee's real open items
# (loans, assets, claims, leave, notice, service) so nothing is missed --
# HR decides what to add and the amount.

FNF_EXIT_STATUSES = ("approved", "in_clearance", "completed")
FNF_MAX_LINES = 60
# Suggested line names (free text is allowed too); the Flutter editor uses
# the same lists.
FNF_EARNING_COMPONENTS = (
    "Salary for days worked", "Leave encashment", "Gratuity", "Bonus / incentive",
    "Notice pay (waived by company)", "Reimbursement", "Variable pay", "Arrears", "Other earning",
)
FNF_DEDUCTION_COMPONENTS = (
    "Notice period shortfall recovery", "Loan / advance recovery", "Asset recovery",
    "TDS (income tax)", "Professional tax", "Provident fund", "ESI", "Other deduction",
)


def fnf_status(settlement: "models.FinalSettlement | None") -> str:
    """'none' | 'draft' | 'approved' | 'paid' -- older 'posted' rows read as approved."""
    if settlement is None:
        return "none"
    return "approved" if settlement.status == "posted" else settlement.status


def get_final_settlement(db: Session, exit_id: uuid.UUID) -> "models.FinalSettlement | None":
    return db.scalar(
        select(models.FinalSettlement).where(models.FinalSettlement.exit_id == exit_id).limit(1)
    )


def _actor_name(db: Session, user: "models.User") -> str:
    name = employee_display_name(db, user.employee_id) if user.employee_id else "—"
    return name if name != "—" else (getattr(user, "email", None) or "—")


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _recompute_fnf_totals(settlement: "models.FinalSettlement") -> None:
    payable = sum((_money(l.amount) for l in settlement.lines if l.line_type == "earning"), Decimal("0"))
    recovery = sum((_money(l.amount) for l in settlement.lines if l.line_type == "deduction"), Decimal("0"))
    settlement.payable_amount = payable
    settlement.recovery_amount = recovery
    settlement.net_amount = payable - recovery


def save_fnf_draft(
    db: Session,
    detail: dict,
    *,
    lines: list[dict],
    notes: str | None,
    actor: "models.User",
) -> "models.FinalSettlement":
    """Creates the exit's settlement or replaces a DRAFT's lines and notes.
    Raises ValueError with a user-facing message when not allowed."""
    exit_request = detail["exit_request"]
    if exit_request.status not in FNF_EXIT_STATUSES:
        raise ValueError(
            f"The exit is {exit_request.status.replace('_', ' ')} -- a full & final settlement can be "
            "prepared once the exit is approved."
        )
    if len(lines) > FNF_MAX_LINES:
        raise ValueError(f"A settlement can have at most {FNF_MAX_LINES} lines.")
    # Serialise concurrent saves / approvals of the same exit.
    db.execute(
        select(models.ExitRequestModel.id)
        .where(models.ExitRequestModel.id == exit_request.id)
        .with_for_update()
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    settlement = get_final_settlement(db, exit_request.id)
    if settlement is None:
        settlement = models.FinalSettlement(
            id=uuid.uuid4(), exit_id=exit_request.id, status="draft",
            payable_amount=0, recovery_amount=0, net_amount=0, created_at=now,
        )
        db.add(settlement)
    elif fnf_status(settlement) != "draft":
        raise ValueError(
            f"This settlement is {fnf_status(settlement)} and can no longer be edited"
            + (" -- reopen it first." if fnf_status(settlement) == "approved" else ".")
        )
    settlement.lines.clear()
    db.flush()
    for index, line in enumerate(lines):
        settlement.lines.append(models.FinalSettlementLine(
            id=uuid.uuid4(),
            line_type=line["line_type"],
            component=line["component"].strip(),
            description=(line.get("description") or "").strip() or None,
            amount=_money(line["amount"]),
            sort_order=index,
        ))
    _recompute_fnf_totals(settlement)
    settlement.notes = (notes or "").strip() or None
    settlement.prepared_by = actor.employee_id
    settlement.prepared_by_name = _actor_name(db, actor)
    settlement.prepared_at = now
    settlement.updated_at = now
    db.flush()
    return settlement


def approve_fnf(
    db: Session, settlement: "models.FinalSettlement", *, actor: "models.User", notes: str | None, is_owner: bool
) -> None:
    if fnf_status(settlement) != "draft":
        raise ValueError(f"Only a draft settlement can be approved (this one is {fnf_status(settlement)}).")
    if not settlement.lines:
        raise ValueError("Add at least one earning or deduction before approving.")
    # Maker-checker: the person who prepared the figures does not approve
    # them, unless they are the Organization Owner.
    if not is_owner and settlement.prepared_by is not None and settlement.prepared_by == actor.employee_id:
        raise ValueError("You prepared this settlement -- another payroll approver must approve it.")
    now = datetime.datetime.now(datetime.timezone.utc)
    settlement.status = "approved"
    settlement.approved_by = actor.employee_id
    settlement.approved_by_name = _actor_name(db, actor)
    settlement.approved_at = now
    settlement.approval_notes = (notes or "").strip() or None
    settlement.updated_at = now


def reopen_fnf(db: Session, settlement: "models.FinalSettlement", *, actor: "models.User", reason: str) -> None:
    if fnf_status(settlement) != "approved":
        raise ValueError(
            "Only an approved settlement can be reopened"
            + (" -- it has already been paid." if fnf_status(settlement) == "paid" else ".")
        )
    settlement.status = "draft"
    settlement.approved_by = None
    settlement.approved_by_name = None
    settlement.approved_at = None
    settlement.approval_notes = f"Reopened by {_actor_name(db, actor)}: {reason.strip()}"
    settlement.updated_at = datetime.datetime.now(datetime.timezone.utc)


def mark_fnf_paid(
    db: Session,
    company_id: uuid.UUID,
    settlement: "models.FinalSettlement",
    *,
    actor: "models.User",
    payment_date: datetime.date,
    payment_mode: str,
    payment_reference: str | None,
) -> None:
    if fnf_status(settlement) != "approved":
        raise ValueError(f"Only an approved settlement can be marked paid (this one is {fnf_status(settlement)}).")
    if payment_date > company_today(db, company_id):
        raise ValueError("The payment date cannot be in the future.")
    if settlement.approved_at is not None and payment_date < settlement.approved_at.date():
        raise ValueError("The payment date cannot be before the settlement was approved.")
    now = datetime.datetime.now(datetime.timezone.utc)
    settlement.status = "paid"
    settlement.payment_date = payment_date
    settlement.payment_mode = payment_mode.strip()
    settlement.payment_reference = (payment_reference or "").strip() or None
    settlement.paid_by = actor.employee_id
    settlement.paid_at = now
    settlement.updated_at = now
    # The Offboarding checklist's "Final settlement review" task, where the
    # exit has one.
    for item in db.scalars(
        select(models.ExitChecklistItem).where(
            models.ExitChecklistItem.exit_id == settlement.exit_id,
            models.ExitChecklistItem.task == "Final settlement review",
        )
    ):
        item.status = "done"


def fnf_statement_eligibility(db: Session, exit_request: "models.ExitRequestModel") -> tuple[bool, str]:
    settlement = get_final_settlement(db, exit_request.id)
    status = fnf_status(settlement)
    if status == "none":
        return False, "No full & final settlement has been prepared for this exit yet."
    if status == "draft":
        return False, "The full & final settlement is still a draft -- the statement can be issued once it is approved."
    if not settlement.lines:
        return False, (
            "This settlement was recorded without an itemised breakdown, so a statement cannot be issued from it."
        )
    return True, "Eligible (settlement paid)" if status == "paid" else "Eligible (settlement approved)"


def _latest_slip(db: Session, employee_id: uuid.UUID):
    slip_id = get_latest_salary_slip_id_for_employee(db, employee_id)
    if slip_id is None:
        return None, None
    slip = db.get(models.SalarySlip, slip_id)
    run = db.get(models.PayrollRun, slip.payroll_run_id) if slip is not None else None
    return slip, run


def _completed_service(start: datetime.date, end: datetime.date) -> tuple[int, int, int]:
    """(years, months, days) of service from joining through the last working day."""
    years = end.year - start.year
    months = end.month - start.month
    days = end.day - start.day + 1
    if days <= 0:
        months -= 1
        prev_month_end = end.replace(day=1) - datetime.timedelta(days=1)
        days += prev_month_end.day
    if months < 0:
        years -= 1
        months += 12
    last_day_of_month = (end.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    if days >= last_day_of_month.day:
        months += 1
        days = 0
        if months == 12:
            years += 1
            months = 0
    return max(years, 0), max(months, 0), max(days, 0)


def fnf_reference_items(db: Session, company_id: uuid.UUID, detail: dict) -> list[dict]:
    """The employee's real open items to consider while preparing the F&F --
    information only. Each item may carry a suggested line (type,
    component, description, amount) that HR can add and then adjust; the
    `basis` text says exactly how any suggested amount was worked out."""
    exit_request = detail["exit_request"]
    employee = detail["employee"]
    lwd = exit_request.last_working_day
    items: list[dict] = []

    def add(category, title, detail_text, *, line_type=None, component=None, description=None,
            amount=None, basis=None, severity="info"):
        items.append({
            "category": category, "title": title, "detail": detail_text, "severity": severity,
            "suggested_line_type": line_type, "suggested_component": component,
            "suggested_description": description,
            "suggested_amount": round(float(amount), 2) if amount is not None else None,
            "basis": basis,
        })

    slip, run = _latest_slip(db, employee.id)
    monthly_gross = monthly_basic = None
    if slip is not None:
        working = float(slip.working_days or 0)
        lop = float(slip.lop_days or 0)
        ratio = (working - lop) / working if working > 0 else 1
        if ratio > 0:
            monthly_gross = float(slip.gross_pay or 0) / ratio
            for line in db.scalars(select(models.SalarySlipLine).where(models.SalarySlipLine.slip_id == slip.id)):
                comp = line.component
                if comp is not None and comp.component_type == "earning" and (
                    (comp.code or "").upper() == "BASIC" or "basic" in (comp.name or "").lower()
                ):
                    monthly_basic = float(line.amount or 0) / ratio
                    break

    # ── Salary for the final period ──
    if lwd is not None:
        if run is not None and run.to_date is not None and run.to_date >= lwd:
            add("salary", "Final month salary already processed",
                f"The {calendar.month_name[run.period_month]} {run.period_year} payroll run "
                f"covers the last working day ({lwd.strftime('%d %b %Y')}). Do not add it again unless "
                "that payslip was not paid.")
        else:
            start = (run.to_date + datetime.timedelta(days=1)) if run is not None and run.to_date else lwd.replace(day=1)
            start = max(start, employee.date_of_joining or start)
            if start <= lwd:
                days = (lwd - start).days + 1
                period = f"{start.strftime('%d %b %Y')} – {lwd.strftime('%d %b %Y')}"
                amount = basis = None
                if monthly_gross is not None and start.month == lwd.month and start.year == lwd.year:
                    month_days = calendar.monthrange(lwd.year, lwd.month)[1]
                    amount = monthly_gross / month_days * days
                    basis = (f"Monthly gross {_inr(monthly_gross)} (latest payslip) ÷ {month_days} calendar days "
                             f"× {days} days. Adjust for your policy (26 / 30 days) and any loss of pay.")
                add("salary", "Salary not yet paid",
                    f"No payroll run covers {period} ({days} days)"
                    + (f"; the latest payslip is for {calendar.month_name[run.period_month]} {run.period_year}." if run else "; no payslip exists for this employee."),
                    line_type="earning", component="Salary for days worked", description=period,
                    amount=amount, basis=basis, severity="action")

    # ── Leave encashment ──
    for leave_type in db.scalars(
        select(models.LeaveType).where(models.LeaveType.company_id == company_id, models.LeaveType.is_encashable.is_(True))
    ):
        balance = get_remaining_leave_balance(db, employee.id, leave_type)
        if balance is None or balance <= 0:
            continue
        amount = basis = None
        if monthly_basic is not None:
            amount = monthly_basic / 30 * balance
            basis = (f"Monthly basic {_inr(monthly_basic)} ÷ 30 × {balance:g} days. "
                     "Adjust to your leave encashment policy (divisor, maximum days).")
        add("leave", f"{leave_type.name} balance: {balance:g} days",
            f"{leave_type.name} is encashable and {balance:g} days remain.",
            line_type="earning", component="Leave encashment",
            description=f"{leave_type.name} — {balance:g} days", amount=amount, basis=basis, severity="action")

    # ── Service / gratuity ──
    if employee.date_of_joining and lwd:
        years, months, days = _completed_service(employee.date_of_joining, lwd)
        service = f"{years} years {months} months {days} days"
        # Payment of Gratuity Act: 5 years' continuous service (1 year for
        # fixed-term employees under the Code on Social Security); a part
        # year of more than 6 months counts as a full year.
        fixed_term = (getattr(employee, "employment_type", "") or "").lower().replace("-", " ") in ("fixed term", "contract")
        eligible = years >= (1 if fixed_term else 5)
        counted_years = years + (1 if months >= 6 and (months > 6 or days > 0) else 0)
        if eligible:
            amount = basis = None
            if monthly_basic is not None:
                amount = min(monthly_basic * 15 / 26 * counted_years, 2_000_000)
                basis = (f"15 ÷ 26 × last drawn basic {_inr(monthly_basic)} × {counted_years} years "
                         "(a part year over 6 months counts as a year); ₹20,00,000 cap. Add DA if paid. "
                         "Payable within 30 days of it becoming due.")
            add("gratuity", "Eligible for gratuity",
                f"Service {service} ({employee.date_of_joining.strftime('%d %b %Y')} – {lwd.strftime('%d %b %Y')}).",
                line_type="earning", component="Gratuity", description=f"{counted_years} years of service",
                amount=amount, basis=basis, severity="action")
        else:
            add("gratuity", "Not eligible for gratuity",
                f"Service {service} is under the {'1' if fixed_term else '5'}-year minimum.")

    # ── Notice period ──
    settings_row = get_company_settings(db, company_id)
    policy_days = int(settings_row.notice_period_days) if settings_row and settings_row.notice_period_days else None
    if policy_days and exit_request.resignation_date and lwd:
        served = (lwd - exit_request.resignation_date).days
        if served < policy_days:
            short = policy_days - served
            amount = basis = None
            if monthly_gross is not None:
                amount = monthly_gross / 30 * short
                basis = f"Monthly gross {_inr(monthly_gross)} ÷ 30 × {short} days. Waive or adjust per the offer letter."
            add("notice", f"Notice period short by {short} days",
                f"Served {served} of the {policy_days}-day notice period "
                f"({exit_request.resignation_date.strftime('%d %b %Y')} – {lwd.strftime('%d %b %Y')}). "
                "Recover only if the offer letter provides for it, or record it as waived.",
                line_type="deduction", component="Notice period shortfall recovery",
                description=f"{short} days short of {policy_days}-day notice",
                amount=amount, basis=basis, severity="action")
        else:
            add("notice", "Notice period served in full", f"Served {served} of {policy_days} days.")

    # ── Loans / advances ──
    for loan in db.scalars(select(models.Loan).where(models.Loan.employee_id == employee.id)):
        outstanding = float(loan.outstanding_balance or 0)
        if loan.status != "active" or outstanding <= 0:
            continue
        add("loan", f"{loan.loan_type}: {_inr(outstanding)} outstanding",
            f"Principal {_inr(loan.principal_amount)}; recorded outstanding balance {_inr(outstanding)}.",
            line_type="deduction", component="Loan / advance recovery", description=loan.loan_type,
            amount=outstanding, basis="Outstanding balance on the loan record.", severity="action")

    # ── Assets ──
    for assignment, asset in db.execute(
        select(models.AssetAssignment, models.AssetInventoryItem)
        .join(models.AssetInventoryItem, models.AssetInventoryItem.id == models.AssetAssignment.asset_id)
        .where(models.AssetAssignment.employee_id == employee.id, models.AssetAssignment.returned_on.is_(None))
    ).all():
        label = " ".join(p for p in (asset.asset_type, asset.model) if p) or "Asset"
        add("asset", f"Not returned: {label} ({asset.asset_tag})",
            f"Assigned {assignment.assigned_on.strftime('%d %b %Y')}"
            + (f"; purchase value {_inr(asset.purchase_value)}" if asset.purchase_value else "")
            + ". Collect it, or recover its value if the employee's asset acknowledgement allows.",
            line_type="deduction", component="Asset recovery", description=f"{label} ({asset.asset_tag})",
            severity="action")
    for deduction in db.scalars(
        select(models.AssetRecoveryDeduction).where(
            models.AssetRecoveryDeduction.employee_id == employee.id,
            models.AssetRecoveryDeduction.status == "approved",
            models.AssetRecoveryDeduction.applied_payroll_run_id.is_(None),
        )
    ):
        add("asset", f"Approved asset recovery: {_inr(deduction.amount)}",
            f"{deduction.reason or 'Asset recovery'} — approved, not yet deducted in payroll.",
            line_type="deduction", component="Asset recovery", description=deduction.reason or "Asset recovery",
            amount=float(deduction.amount or 0), basis="Approved asset recovery deduction.", severity="action")

    # ── Approved reimbursements not yet paid ──
    for source_type, model, label in (
        ("expense_claim", models.ExpenseClaim, "Expense claim"),
        ("travel_request", models.TravelRequestModel, "Travel request"),
    ):
        rows = db.scalars(select(model).where(model.employee_id == employee.id, model.status == "approved")).all()
        statuses = reimbursement_payroll_statuses(db, company_id, source_type, rows) if rows else {}
        for row in rows:
            status = statuses.get(row.id, "")
            if status.startswith("Paid") or status.startswith("Excluded"):
                continue
            reference = _reimbursement_reference(source_type, row)
            amount = _reimbursement_source_amount(source_type, row)
            add("reimbursement", f"{label} {reference}: {_inr(amount)} not paid",
                f"{getattr(row, 'purpose', None) or label} — approved; payroll status: {status or 'Not scheduled'}.",
                line_type="earning", component="Reimbursement", description=f"{label} {reference}",
                amount=amount, basis="Approved amount.", severity="action")
    return items


def _inr(value) -> str:
    from . import exit_letter_html_renderer as renderer
    return renderer.format_inr(value)


# ── Full & Final Settlement notifications ───────────────────────────────────

def fnf_approver_employee_ids(
    db: Session, company_id: uuid.UUID, exclude_employee_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Employees who may approve an F&F (routers/fnf._is_approver): the
    Owner, or Payroll (Process) Admin -- never the preparer."""
    out = []
    for user in db.scalars(
        select(models.User).where(models.User.company_id == company_id, models.User.status == "active")
    ):
        if user.employee_id is None or user.employee_id == exclude_employee_id:
            continue
        role = get_user_primary_role(db, user.id)
        if role is None:
            continue
        if role.name == BUILTIN_ROLES[0] or effective_user_matrix(db, user).get("payroll_process") == "a":
            out.append(user.employee_id)
    return out


def _fnf_email_details(detail: dict, settlement: "models.FinalSettlement") -> dict[str, str]:
    employee, exit_request = detail["employee"], detail["exit_request"]
    net = _money(settlement.net_amount)
    return {
        "Employee": _full_name(employee),
        "Employee ID": employee.employee_code or "—",
        "Designation": detail["designation"].name if detail.get("designation") else "—",
        "Last Working Day": _fmt_req_date(exit_request.last_working_day),
        "Total Earnings": _inr(settlement.payable_amount),
        "Total Deductions": _inr(settlement.recovery_amount),
        ("Net Payable" if net >= 0 else "Net Recoverable"): _inr(abs(net)),
    }


def notify_fnf_event(
    db: Session,
    company_id: uuid.UUID,
    detail: dict,
    settlement: "models.FinalSettlement",
    event_name: str,
    actor: "models.User",
    *,
    note: str | None = None,
) -> None:
    """In-app + email notifications for the F&F workflow:
      submitted -> payroll approvers (once a day per approver while a draft)
      approved / reopened -> the preparer
      paid -> the employee (personal email first -- leavers lose their work
              mailbox), with the current F&F statement PDF when one exists."""
    employee, exit_request = detail["employee"], detail["exit_request"]
    name = _full_name(employee)
    actor_name = _actor_name(db, actor)
    details = _fnf_email_details(detail, settlement)
    entity_id = exit_request.id

    def notify(employee_id, title, body, email_to, subject, heading, intro, status, email_type):
        user_id = get_user_id_for_employee(db, employee_id) if employee_id else None
        if user_id is not None:
            create_notification(db, company_id, user_id, title=title, body=body,
                                entity_type="final_settlement", entity_id=entity_id)
        email_service.send_request_email(
            email_to, subject=subject, heading=heading, intro_line=intro,
            details={**details, "Status": status, **({"Notes": note} if note else {})},
            entity_type="final_settlement", entity_id=entity_id, from_display_name=actor_name,
            cta_label="Open Now", db=db, company_id=company_id, email_type=email_type,
        )

    if event_name == "submitted":
        today_start = datetime.datetime.combine(company_today(db, company_id), datetime.time.min).replace(
            tzinfo=company_tzinfo(db, company_id))
        for approver_id in fnf_approver_employee_ids(db, company_id, exclude_employee_id=settlement.prepared_by):
            user_id = get_user_id_for_employee(db, approver_id)
            if user_id is not None and db.scalar(
                select(models.Notification.id).where(
                    models.Notification.user_id == user_id,
                    models.Notification.entity_type == "final_settlement",
                    models.Notification.entity_id == entity_id,
                    models.Notification.created_at >= today_start,
                ).limit(1)
            ) is not None:
                continue  # already told today
            approver = db.get(models.Employee, approver_id)
            notify(
                approver_id, f"F&F settlement for {name} awaits approval",
                f"{actor_name} prepared the full & final settlement for {name}.",
                approver.work_email if approver else None,
                f"Full & Final Settlement for approval - {name}",
                "Full & Final Settlement Notification",
                f"{actor_name} has prepared the full & final settlement for {name}, awaiting your approval.",
                "Pending Approval", f"{email_service.FNF}_SUBMITTED",
            )
    elif event_name in ("approved", "reopened"):
        if settlement.prepared_by is None or settlement.prepared_by == actor.employee_id:
            return
        preparer = db.get(models.Employee, settlement.prepared_by)
        verb = "approved" if event_name == "approved" else "reopened"
        notify(
            settlement.prepared_by, f"F&F settlement for {name} {verb}",
            f"{actor_name} {verb} the full & final settlement for {name}." + (f' "{note}"' if note else ""),
            preparer.work_email if preparer else None,
            f"Full & Final Settlement {verb} - {name}",
            "Full & Final Settlement Notification",
            f"{actor_name} has {verb} the full & final settlement for {name}.",
            verb.capitalize(), f"{email_service.FNF}_{event_name.upper()}",
        )
    elif event_name == "paid":
        user_id = get_user_id_for_employee(db, employee.id)
        net = _money(settlement.net_amount)
        paid_line = (
            f"Paid on {_fmt_req_date(settlement.payment_date)} by {settlement.payment_mode}"
            + (f" (Ref. {settlement.payment_reference})" if settlement.payment_reference else "")
        )
        if user_id is not None:
            create_notification(
                db, company_id, user_id, title="Your full & final settlement has been paid",
                body=f"{'Net amount' if net >= 0 else 'Net recoverable'}: {_inr(abs(net))}. {paid_line}.",
                entity_type="final_settlement", entity_id=entity_id,
            )
        to = (employee.personal_email or "").strip() or (employee.work_email or "").strip()
        if not to:
            return
        statement = next((l for l in list_exit_letters(db, company_id, exit_request_id=exit_request.id, current_only=True)
                          if l.letter_type == "fnf_statement"), None)
        content = exit_letter_file_bytes(statement) if statement is not None else None
        company = db.get(models.Company, company_id)
        company_name = company.name if company else ""
        message = (
            "Your full and final settlement has been completed. "
            f"{'Net amount paid to you' if net >= 0 else 'Net amount recovered'}: {_inr(abs(net))}. {paid_line}."
        )
        if content:
            email_service.queue_hr_document(
                db, document_kind="fnf_statement", to=to,
                attachments=[email_service.make_attachment(
                    f"Full_and_Final_Settlement_Statement_{employee.employee_code or 'employee'}.pdf", content)],
                recipient_name=name, company_id=company_id, company_name=company_name,
                sender_name=actor_name, created_by=actor.id, message=message,
                subject=f"Full & Final Settlement Statement - {company_name}" if company_name else None,
                related_entity_type="final_settlement", related_entity_id=entity_id,
                idempotency_key=f"{email_service.FNF}_PAID:{settlement.id}:{to.lower()}",
            )
        else:
            email_service.send_request_email(
                to, subject="Your Full & Final Settlement has been paid",
                heading="Full & Final Settlement Notification", intro_line=message,
                details={**details, "Status": "Paid", "Payment": paid_line},
                entity_type="final_settlement", entity_id=entity_id, from_display_name=actor_name,
                cta_label="Open Now", db=db, company_id=company_id, email_type=f"{email_service.FNF}_PAID",
            )
