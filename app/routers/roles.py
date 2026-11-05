import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import crud, models, role_tiers, schemas
from ..database import get_db, get_session_tenant_slug
from ..deps import get_current_user, require_action, require_permission

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RBAC scope tables — mirrors rbac_seed.dart constants.
# These are config values: moving them here makes the backend the single
# source of truth. A future admin screen can persist overrides to a DB table.
# ---------------------------------------------------------------------------

_DASHBOARD_SCOPE: dict[str, str] = {
    "Organization Owner / CEO": "org",
    "C-Level Executive": "org",
    "VP / Director": "org",
    "General Manager / Sr. Manager": "team",
    "Manager": "team",
    "Team Lead": "team",
    "HR / Recruitment Staff": "org",
    "Finance / Payroll Staff": "org",
    "IT / System Admin": "org",
    "Branch Manager": "branch",
    "Branch Head": "org",
    "Project Manager": "project",
    "Professional / IC Employee": "self",
    "Associate / Intern": "self",
}

_PEOPLE_SCOPE: dict[str, str] = {
    "Organization Owner / CEO": "org-full",
    "C-Level Executive": "org-view",
    "VP / Director": "org-view",
    "General Manager / Sr. Manager": "team",
    "Manager": "team",
    "Team Lead": "team",
    "HR / Recruitment Staff": "branch",
    "Finance / Payroll Staff": "org-view",
    "IT / System Admin": "org-view",
    "Branch Manager": "branch",
    "Branch Head": "org-view",
    "Project Manager": "project",
    "Professional / IC Employee": "self",
    "Associate / Intern": "self",
}

_ROLE_BLURBS: dict[str, str] = {
    "Organization Owner / CEO": (
        "Full visibility and control across every module, branch and approval chain."
    ),
    "C-Level Executive": (
        "Company-wide visibility with edit rights over org structure, benefits and platform settings."
    ),
    "VP / Director": (
        "Company-wide visibility across your function, with edit rights on performance and org structure."
    ),
    "General Manager / Sr. Manager": (
        "Your reporting line only — attendance, leave and timesheet approvals for your team."
    ),
    "Manager": (
        "Your direct reports only — day-to-day approvals, no org-wide administration."
    ),
    "Team Lead": (
        "Your squad only — first-line attendance & timesheet review, view-only on leave and org data."
    ),
    "HR / Recruitment Staff": (
        "Full People, Recruitment and Benefits administration; no org-structure or platform settings access."
    ),
    "Finance / Payroll Staff": (
        "Full payroll processing and benefits administration; no attendance, performance or recruitment access."
    ),
    "IT / System Admin": (
        "Full platform administration and asset management; no HR, payroll or performance access."
    ),
    "Branch Manager": (
        "Branch-scoped view of people, attendance and performance; no payroll, recruitment or platform access."
    ),
    "Branch Head": (
        "Oversees every Branch Manager company-wide — cross-branch visibility and approvals, no platform administration."
    ),
    "Project Manager": (
        "Approve leave, timesheets and travel for your project team; view-only on broader org and payroll data."
    ),
    "Professional / IC Employee": (
        "Self-service only — your profile, attendance, leave, payslip and assigned work."
    ),
    "Associate / Intern": (
        "Self-service only — your profile, attendance, leave, payslip and assigned work."
    ),
}

router = APIRouter(prefix="/api/roles", tags=["roles"])


def _require_role_editable(
    db: Session, current_user: models.User, role: models.Role, new_matrix: dict | None = None
) -> None:
    """C14 + SEC-01/RBAC-02: the role must belong to the caller's company,
    only the Owner may change the Owner role, and nobody may raise a role
    above their own access (see role_tiers.role_edit_error)."""
    if role.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Role not found")
    error = role_tiers.role_edit_error(db, current_user, role, new_matrix)
    if error is not None:
        raise HTTPException(status_code=403, detail=error)


def _get_role_or_404(db: Session, role_id: uuid.UUID) -> models.Role:
    role = db.scalar(
        select(models.Role)
        .options(selectinload(models.Role.role_permissions).selectinload(models.RolePermission.permission))
        .where(models.Role.id == role_id)
    )
    if role is None:
        # Logged distinctly from a "the role list doesn't include a role I
        # just created" report -- the frontend's Roles & Permissions screen
        # fetches GET /api/roles then, per role, GET /api/roles/{id}/matrix
        # (roles_api_repository.dart's fetchRoleMatrices); if this ever 404s
        # for a role that genuinely exists, it points at a tenant/schema
        # mismatch (role_id belongs to a different tenant's schema than the
        # caller's current search_path) rather than a frontend rendering bug.
        logger.warning(
            "GET /api/roles/%s/matrix (or a related role lookup): no role "
            "found with that id in tenant=%s -- either a stale id or a "
            "cross-tenant/schema mismatch",
            role_id, get_session_tenant_slug(db),
        )
        raise HTTPException(status_code=404, detail="Role not found")
    return role


@router.get("", response_model=list[schemas.RoleOut])
def list_roles(
    band_id: uuid.UUID | None = Query(
        default=None,
        description="Filter to roles mapped to this Band -- backs the Add "
        "Employee Band -> Role cascade. Ignored (returns every role) until "
        "this company has mapped at least one role to a band.",
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    if company is None:
        logger.error(
            "GET /api/roles: current_user %s has company_id %s which does not "
            "resolve to a row in core_companies (tenant=%s)",
            current_user.id, current_user.company_id, get_session_tenant_slug(db),
        )
        raise HTTPException(status_code=500, detail="Your account is not linked to a valid company")
    roles = crud.list_roles(db, company.id, band_id=band_id)
    logger.info(
        "GET /api/roles: tenant=%s company_id=%s band_id=%s -> %d role(s) [%s]",
        get_session_tenant_slug(db), company.id, band_id, len(roles),
        ", ".join(r.name for r in roles),
    )
    return roles


@router.post(
    "",
    response_model=schemas.RoleOut,
    status_code=201,
)
def create_role(
    payload: schemas.RoleCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    """Mirrors the frontend's Administration > Custom Role dialog: adds a
    new role with every RBAC column defaulted to 'n' (no access) until the
    matrix is edited. `band_id` optionally maps it into the Band -> Role
    hierarchy right away.

    The duplicate-name check below is scoped to `company.id` -- i.e. to the
    tenant schema `deps.get_current_user` already switched `search_path` to
    for this request -- so two different companies (whether in the same
    Postgres schema or, as is the norm here, different schemas entirely)
    can freely use the same role name; only a second role with that name
    *inside the same company* is rejected. Matching is case-insensitive and
    whitespace-trimmed on both sides (the stored name is trimmed too) so
    "Sales Manager", "sales manager", and " Sales Manager " are all treated
    as the same name -- the most common real-world cause of a confusing
    "this name already exists" report is a near-duplicate that looked
    different to the person typing it but wasn't, once you account for case
    or a stray space (frequently a system role like "Manager" or "Team
    Lead", already seeded into every company by ensure_roles/DEFAULT_MATRIX)."""
    company = db.get(models.Company, current_user.company_id)
    if company is None:
        # Shouldn't happen (current_user.company_id is set at login from a
        # real core_companies row in this same schema) -- logged distinctly
        # from the 409 case below so this doesn't get misdiagnosed as a
        # duplicate-name issue if it ever does happen.
        logger.error(
            "POST /api/roles: current_user %s has company_id %s which does not "
            "resolve to a row in core_companies (tenant=%s)",
            current_user.id, current_user.company_id, get_session_tenant_slug(db),
        )
        raise HTTPException(status_code=500, detail="Your account is not linked to a valid company")

    normalized_name = payload.name.strip()
    if not normalized_name:
        raise HTTPException(status_code=400, detail="Role name must not be blank")

    existing = db.scalar(
        select(models.Role).where(
            models.Role.company_id == company.id,
            func.lower(models.Role.name) == normalized_name.lower(),
        )
    )
    if existing is not None:
        logger.warning(
            "POST /api/roles 409: tenant=%s company_id=%s company_name=%r "
            "requested_name=%r conflicts with existing role id=%s name=%r is_system=%s "
            "(requested_by user_id=%s)",
            get_session_tenant_slug(db), company.id, company.name,
            payload.name, existing.id, existing.name, existing.is_system,
            current_user.id,
        )
        detail = f"A role named '{existing.name}' already exists for this company"
        if existing.is_system:
            detail += " (it's one of the built-in system roles)"
        raise HTTPException(status_code=409, detail=detail)

    if payload.band_id is not None:
        band = db.get(models.Band, payload.band_id)
        if band is None or band.company_id != company.id:
            raise HTTPException(status_code=400, detail="Invalid band_id")

    role = models.Role(
        id=uuid.uuid4(),
        company_id=company.id,
        name=normalized_name,
        description=payload.description,
        is_system=False,
        band_id=payload.band_id,
        created_by=current_user.id,
        updated_by=current_user.id,
    )
    db.add(role)
    db.flush()
    crud.create_audit_log(db, company.id, current_user.id, "create", "role", role.id)
    db.commit()
    db.refresh(role)
    logger.info(
        "POST /api/roles: created role id=%s name=%r for tenant=%s company_id=%s (created_by=%s)",
        role.id, role.name, get_session_tenant_slug(db), company.id, current_user.id,
    )
    return role


@router.patch("/{role_id}", response_model=schemas.RoleOut)
def update_role(
    role_id: uuid.UUID,
    payload: schemas.RoleUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    """Administration > Roles & Permissions -- (re)assign or clear which
    Band this role belongs to, and/or set its dashboard_scope/people_scope/
    blurb (see backend/db/add_role_scope_columns.sql and
    GET /api/roles/scopes below). Name/description aren't editable here (no
    existing UI supports renaming a role); the permission matrix has its own
    PUT /api/roles/{id}/matrix."""
    role = _get_role_or_404(db, role_id)
    company = db.get(models.Company, current_user.company_id)
    if role.company_id != company.id:
        raise HTTPException(status_code=404, detail="Role not found")
    _require_role_editable(db, current_user, role)

    data = payload.model_dump(exclude_unset=True)
    if "band_id" in data:
        band_id = data["band_id"]
        if band_id is not None:
            band = db.get(models.Band, band_id)
            if band is None or band.company_id != company.id:
                raise HTTPException(status_code=400, detail="Invalid band_id")
        role.band_id = band_id
    if "dashboard_scope" in data:
        role.dashboard_scope = data["dashboard_scope"]
    if "people_scope" in data:
        role.people_scope = data["people_scope"]
    if "blurb" in data:
        role.blurb = data["blurb"]

    role.updated_by = current_user.id
    crud.create_audit_log(db, company.id, current_user.id, "update", "role", role.id)
    db.commit()
    db.refresh(role)
    return role


@router.delete("/{role_id}", status_code=204)
def delete_role(
    role_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    role = _get_role_or_404(db, role_id)
    company = db.get(models.Company, current_user.company_id)
    if role.company_id != company.id:
        raise HTTPException(status_code=404, detail="Role not found")
    _require_role_editable(db, current_user, role)

    try:
        crud.delete_role(db, role)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    crud.create_audit_log(db, company.id, current_user.id, "delete", "role", role_id)
    db.commit()


@router.get("/{role_id}/matrix", response_model=schemas.RoleMatrixOut)
def get_role_matrix(
    role_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    role = _get_role_or_404(db, role_id)
    return _matrix_out(role)


def _matrix_out(role: models.Role) -> schemas.RoleMatrixOut:
    """The matrix the editor shows = the levels authorization actually
    applies (crud.effective_role_matrix). For the built-in self-service
    roles (IC, Associate / Intern) that is the stored matrix capped at the
    role template (SEC-01), and `caps` tells the editor each column's
    maximum -- previously the raw stored value was shown, so an edit above
    the cap looked saved but had no effect."""
    from ..rbac_columns import SELF_SERVICE_ROLES, template_levels

    caps = (template_levels(role.name) or {}) if role.name in SELF_SERVICE_ROLES else None
    return schemas.RoleMatrixOut(role=role, matrix=crud.effective_role_matrix(role), caps=caps)


def _reject_above_caps(role: models.Role, updates: dict[str, str]) -> None:
    """A self-service role can't be raised above its template: say so (422)
    instead of storing a level authorization will ignore."""
    from ..rbac_columns import COLUMN_LABELS, LEVEL_RANK, SELF_SERVICE_ROLES, template_levels

    if role.name not in SELF_SERVICE_ROLES:
        return
    caps = template_levels(role.name) or {}
    names = {"n": "No Access", "s": "Self", "v": "View", "e": "Edit", "a": "Admin"}
    over = [
        f"{COLUMN_LABELS.get(col, col)} (max {names.get(caps.get(col, 'n'), caps.get(col, 'n'))})"
        for col, level in updates.items()
        if LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(caps.get(col, "n"), 0)
    ]
    if over:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{role.name}' is a built-in self-service role, so these permissions can't go above its "
                f"limit: {', '.join(over)}. To give specific employees more access, use Employee Module Access "
                "(Administration > Roles & Permissions), or assign them a different or custom role."
            ),
        )


@router.put(
    "/{role_id}/matrix",
    response_model=schemas.RoleMatrixOut,
)
def update_role_matrix(
    role_id: uuid.UUID,
    payload: schemas.MatrixUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    role = _get_role_or_404(db, role_id)
    _require_role_editable(db, current_user, role, payload.matrix)
    _reject_above_caps(role, payload.matrix)
    try:
        crud.apply_matrix_update(db, role, payload.matrix, user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    role.updated_by = current_user.id
    crud.create_audit_log(db, role.company_id, current_user.id, "update", "role_matrix", role.id,
                          changes={"matrix": payload.matrix})
    db.commit()
    db.refresh(role)
    crud.clear_rbac_memo(db)
    return _matrix_out(role)


@router.get("/{role_id}/actions", response_model=schemas.RoleActionsOut)
def get_role_actions(
    role_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The new granular (resource, action) grants for this role -- additive
    to /matrix's existing v/s/e/a columns, which this endpoint never reads
    or changes."""
    role = _get_role_or_404(db, role_id)
    return schemas.RoleActionsOut(role=role, granted=crud.build_role_actions(role))


@router.put("/{role_id}/actions", response_model=schemas.RoleActionsOut)
def update_role_actions(
    role_id: uuid.UUID,
    payload: schemas.RoleActionsUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    role = _get_role_or_404(db, role_id)
    _require_role_editable(db, current_user, role)
    # Same ceiling as individual grants: a non-Owner can't add (or strip)
    # actions above their own level -- e.g. an IT / System Admin adding
    # payroll actions to a role, including one they hold themselves.
    current = crud.build_role_actions(role)
    for resource, actions in payload.granted.items():
        changed = set(actions or []) ^ set(current.get(resource, []))
        error = role_tiers.grant_ceiling_error(db, current_user, resource, changed)
        if error:
            raise HTTPException(status_code=403, detail=error)
    try:
        crud.apply_actions_update(db, role, payload.granted, user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    role.updated_by = current_user.id
    crud.create_audit_log(
        db, role.company_id, current_user.id, "update", "role_actions", role.id,
        changes={"granted": payload.granted},
    )
    db.commit()
    db.refresh(role)
    return schemas.RoleActionsOut(role=role, granted=crud.build_role_actions(role))


@router.get("/scopes", response_model=schemas.RbacScopesOut)
def get_rbac_scopes(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Dashboard scope, people scope, and role blurbs keyed by role name --
    sourced from core_roles.dashboard_scope/people_scope/blurb (see
    backend/db/add_role_scope_columns.sql) for this company's roles,
    layered on top of the hardcoded dicts below as a baseline. A role name
    the migration didn't backfill (NULL columns -- e.g. a brand-new custom
    role) simply doesn't override the baseline, so it falls through to the
    baseline's value (or the frontend's own further fallback) exactly as it
    did before these columns existed. The frontend already sends a bearer
    token on every call to this endpoint today even though it wasn't
    required until now, so requiring auth here doesn't change any caller's
    behavior."""
    dashboard_scope = dict(_DASHBOARD_SCOPE)
    people_scope = dict(_PEOPLE_SCOPE)
    role_blurbs = dict(_ROLE_BLURBS)
    rows = db.execute(
        select(
            models.Role.name,
            models.Role.dashboard_scope,
            models.Role.people_scope,
            models.Role.blurb,
        ).where(models.Role.company_id == current_user.company_id)
    ).all()
    for name, ds, ps, blurb in rows:
        if ds is not None:
            dashboard_scope[name] = ds
        if ps is not None:
            people_scope[name] = ps
        if blurb is not None:
            role_blurbs[name] = blurb
    return schemas.RbacScopesOut(
        dashboard_scope=dashboard_scope,
        people_scope=people_scope,
        role_blurbs=role_blurbs,
    )
