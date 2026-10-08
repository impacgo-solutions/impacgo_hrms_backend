import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from .. import tasks as tw
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled, require_permission, require_permission_or_action
from ..storage import save_uploaded_file

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api", tags=["projects"],
    dependencies=[Depends(require_module_enabled("projects"))],
)

_PROJECT_ENTITY_TYPE = "project"

_STATUS_DISPLAY = {
    "planning": "Planning", "active": "In Progress", "completed": "Completed",
    "draft": "Draft", "on_hold": "On Hold", "cancelled": "Cancelled",
}
_TASK_COL_TO_DB = {"Backlog": "todo", "In Progress": "in_progress", "In Review": "in_review", "Done": "done"}
_TASK_DB_TO_COL = {v: k for k, v in _TASK_COL_TO_DB.items()}
# C7: pm_tasks.status CHECK allows exactly these. Anything else used to be
# written straight into status and 500 on the CHECK violation.
_TASK_DB_STATUSES = ("todo", "in_progress", "in_review", "blocked", "done", "cancelled")
_TASK_COL_ALIASES = {"To Do": "todo", "Todo": "todo", "Blocked": "blocked", "Cancelled": "cancelled"}


def _task_status_from_column(column_key: str) -> str:
    """Board column key (Backlog / In Progress / In Review / Done -- what
    the Kanban screens send) or a raw pm_tasks status -> the DB status.
    422 for anything the pm_tasks CHECK constraint would reject."""
    key = column_key.strip()
    status = _TASK_COL_TO_DB.get(key) or _TASK_COL_ALIASES.get(key)
    if status is None and key.lower() in _TASK_DB_STATUSES:
        status = key.lower()
    if status is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid column_key '{column_key}'. Allowed: "
                + ", ".join([*_TASK_COL_TO_DB, "Blocked", "Cancelled"])
            ),
        )
    return status


def _has_budget_value(text: str | None) -> bool:
    """True only for a Budget string actually worth parsing. "—" is not
    just "blank" here: it's ProjectOut.budget's own placeholder for "no
    budget set" (see crud.format_budget_amount), and both Add Project
    (line ~93 of add_project_dialog.dart) and Edit Project (which
    pre-populates its Budget field straight from ProjectOut.budget) send
    that exact string back on every save where the user never touched the
    field. Treating it as "must parse to a number" made every create/edit
    of a budget-less project fail with a false "Enter a valid budget
    amount" error -- the actual root cause of Budget "not loading"."""
    if text is None:
        return False
    stripped = text.strip()
    return bool(stripped) and stripped != "—"


def _parse_project_date(text: str | None, label: str) -> date | None:
    """L-14: blank / "—" means "no date"; anything else must be a real
    YYYY-MM-DD date (it used to be stored as empty silently)."""
    if text is None or not text.strip() or text.strip() == "—":
        return None
    parsed = crud.parse_date_safe(text)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"{label} must be a valid date (YYYY-MM-DD).")
    return parsed


def _require_date_order(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise HTTPException(status_code=400, detail="The end date can't be before the start date.")


def _require_employee_in_company(
    db: Session, employee_id: uuid.UUID | None, company_id: uuid.UUID, field_label: str
) -> None:
    """Guards project_manager_id/team_lead_id against a missing employee or
    (via cross-tenant UUID guessing) one belonging to a different company --
    without this, create/update would either 500 on the FK constraint or,
    worse, silently succeed and mix another tenant's employee into this
    company's project data. None is always allowed (the field is optional
    or -- for team_lead_id on update -- simply not being changed)."""
    if employee_id is None:
        return
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != company_id:
        logger.warning(
            "Rejected %s=%s for company=%s: not a valid employee in this company",
            field_label, employee_id, company_id,
        )
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} does not refer to a valid employee in your company.",
        )


def _require_branch_in_company(
    db: Session, branch_id: uuid.UUID | None, company_id: uuid.UUID
) -> None:
    if branch_id is None:
        return
    branch = db.get(models.Branch, branch_id)
    if branch is None or branch.company_id != company_id:
        logger.warning(
            "Rejected branch_id=%s for company=%s: not a valid branch in this company",
            branch_id, company_id,
        )
        raise HTTPException(
            status_code=400,
            detail="Branch does not refer to a valid branch in your company.",
        )


def _require_business_unit_in_company(
    db: Session, business_unit_id: uuid.UUID | None, company_id: uuid.UUID
) -> None:
    if business_unit_id is None:
        return
    business_unit = db.get(models.BusinessUnit, business_unit_id)
    if business_unit is None or business_unit.company_id != company_id:
        logger.warning(
            "Rejected business_unit_id=%s for company=%s: not a valid business unit in this company",
            business_unit_id, company_id,
        )
        raise HTTPException(
            status_code=400,
            detail="Business Unit does not refer to a valid business unit in your company.",
        )


def _require_department_in_company(
    db: Session, department_id: uuid.UUID | None, company_id: uuid.UUID
) -> None:
    if department_id is None:
        return
    department = db.get(models.Department, department_id)
    if department is None or department.company_id != company_id:
        logger.warning(
            "Rejected department_id=%s for company=%s: not a valid department in this company",
            department_id, company_id,
        )
        raise HTTPException(
            status_code=400,
            detail="Department does not refer to a valid department in your company.",
        )


class _ProjectBatch:
    """Per-request lookups for serializing many projects at once (PERF-01):
    team leads, budgets, progress and team-lead names resolved with one
    grouped/IN query each instead of 5-6 queries per project."""

    def __init__(self, db: Session, projects) -> None:
        ids = [p.id for p in projects]
        self.team_leads = crud.resolve_project_team_lead_ids_bulk(db, ids)
        self.budgets = crud.get_project_budgets_bulk(db, ids)
        self.progress = crud.get_project_progress_bulk(db, ids)
        self.names = crud.employee_display_names_bulk(db, self.team_leads.values())


def _to_out(db: Session, p, batch: "_ProjectBatch | None" = None) -> schemas.ProjectOut:
    pm_display = crud._full_name(p.project_manager) if p.project_manager_id else "—"
    # pm_resource_allocations, not a pm_projects column -- see
    # create_project's own comment on why Team Lead is stored this way.
    # pm_project_budgets -- see crud.get_project_budget/
    # set_project_budget_amount (single amount per project, no schema
    # change to pm_projects itself).
    if batch is not None:
        team_lead_id = batch.team_leads.get(p.id)
        budget_row = batch.budgets.get(p.id)
        progress = batch.progress.get(p.id, 0)
        team_lead_name = batch.names.get(team_lead_id, "—") if team_lead_id else "—"
    else:
        team_lead_id = crud.resolve_project_team_lead_id(db, p.id)
        budget_row = crud.get_project_budget(db, p.id)
        progress = crud.get_project_progress(db, p.id)
        team_lead_name = crud.employee_display_name(db, team_lead_id)
    budget_amount = float(budget_row.planned_amount) if budget_row else None
    return schemas.ProjectOut(
        id=p.id,
        name=p.name,
        code=p.code or "—",
        description=p.description or "—",
        priority=(p.priority or "—").title(),
        # customer_id/category_id are real pm_projects columns -- see
        # crud.get_or_create_customer/get_or_create_project_category.
        client=p.customer.name if p.customer else "—",
        type=p.category.name if p.category else "—",
        pm=pm_display,
        status=_STATUS_DISPLAY.get(p.status, p.status.title()),
        start=p.planned_start_date.isoformat() if p.planned_start_date else "—",
        end=p.planned_end_date.isoformat() if p.planned_end_date else "—",
        budget=crud.format_budget_amount(budget_amount),
        progress=progress,
        pm_employee_id=p.project_manager_id,
        team_lead_id=team_lead_id,
        team_lead=team_lead_name,
        # business_unit_id/department_id are real pm_projects columns,
        # manually added by the user -- see models.Project's own comment.
        business_unit_id=p.business_unit_id,
        business_unit=p.business_unit.name if p.business_unit else "—",
        department_id=p.department_id,
        department=p.department.name if p.department else "—",
        branch_id=p.branch_id,
        branch=p.branch.name if p.branch else "—",
        is_active=p.is_active,
    )


@router.get("/projects", response_model=list[schemas.ProjectOut])
def list_projects(
    employee_id: uuid.UUID | None = None,
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    projects = crud.list_projects(
        db, company.id, employee_id=employee_id, limit=limit, offset=offset,
    )
    batch = _ProjectBatch(db, projects)
    return [_to_out(db, p, batch) for p in projects]


@router.get("/projects/employee-candidates", response_model=list[schemas.EmployeeCandidateOut])
def list_project_employee_candidates(
    roles: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("projects_access", "projects", "edit")
    ),
):
    """Role-scoped candidates for Add/Edit Project's Project Manager / Team
    Lead dropdowns -- e.g. ?roles=Manager,Project Manager or
    ?roles=Team Lead. Omit roles entirely for Edit Project's Team Members
    picker, where any active employee is eligible. Gated by the same
    permission as creating/editing a project, not by this company's
    separate People-access policy (crud.can_access_people_module), since
    that policy governs Employee Directory visibility, not who's eligible
    to be assigned to a project."""
    role_names = (
        [r.strip() for r in roles.split(",") if r.strip()] if roles else None
    )
    return crud.list_employees_by_role(db, current_user.company_id, role_names)


@router.get("/project-categories", response_model=list[schemas.ProjectCategoryOut])
def list_project_categories(
    active_only: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs both Add/Edit Project's Type dropdown (active_only=true) and
    the Manage Project Types dialog (active_only=false, so a deactivated
    type can still be found and reactivated). Tenant-scoped by
    current_user.company_id, same as every other Projects endpoint --
    auto-seeds sensible defaults on first call for a company with none yet
    (see crud.get_or_seed_company_project_categories)."""
    return crud.list_project_categories(db, current_user.company_id, active_only=active_only)


@router.post(
    "/project-categories",
    response_model=schemas.ProjectCategoryOut,
    status_code=201,
)
def create_project_category(
    payload: schemas.ProjectCategoryCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("projects_access", "projects", "edit")
    ),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        category = crud.create_project_category(db, company.id, payload.name, user_id=current_user.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "create", "project_category", category.id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project Type already exists") from exc
    db.refresh(category)
    return category


@router.patch(
    "/project-categories/{category_id}",
    response_model=schemas.ProjectCategoryOut,
)
def update_project_category(
    category_id: uuid.UUID,
    payload: schemas.ProjectCategoryUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("projects_access", "projects", "edit")
    ),
):
    """Rename and/or toggle active/inactive (Activate / Deactivate) -- both
    through this one PATCH, matching PATCH /api/bands/{id}'s shape."""
    company = db.get(models.Company, current_user.company_id)
    category = crud.get_project_category_for_update(db, category_id, company.id)
    if category is None:
        raise HTTPException(status_code=404, detail="Project Type not found")
    updates = payload.model_dump(exclude_unset=True)
    try:
        crud.update_project_category(db, category, updates, user_id=current_user.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "update", "project_category", category.id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project Type already exists") from exc
    db.refresh(category)
    return category


@router.post(
    "/projects",
    response_model=schemas.ProjectOut,
    status_code=201,
)
def create_project(
    payload: schemas.ProjectCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("projects_access", "projects", "edit")
    ),
):
    company = db.get(models.Company, current_user.company_id)
    _require_employee_in_company(db, payload.project_manager_id, company.id, "Project Manager")
    _require_employee_in_company(db, payload.team_lead_id, company.id, "Team Lead")
    _require_branch_in_company(db, payload.branch_id, company.id)
    _require_business_unit_in_company(db, payload.business_unit_id, company.id)
    _require_department_in_company(db, payload.department_id, company.id)
    start_date = _parse_project_date(payload.start, "Start date")
    end_date = _parse_project_date(payload.end, "End date")
    _require_date_order(start_date, end_date)
    # pm_projects has no budget column -- see crud.parse_budget_amount and
    # set_project_budget_amount's own docs. Validated up front, same as the
    # other *_in_company checks, so a bad value 400s before anything is
    # written rather than partway through the transaction below.
    budget_amount = None
    if _has_budget_value(payload.budget):
        budget_amount = crud.parse_budget_amount(payload.budget)
        if budget_amount is None:
            raise HTTPException(
                status_code=400,
                detail="Enter a valid budget amount (e.g. 95L, 9500000, or 95,00,000).",
            )
    try:
        # Client/Type resolve to real pm_projects columns (customer_id/
        # category_id) via get-or-create instead of being discarded -- see
        # crud.get_or_create_customer/get_or_create_project_category.
        customer = crud.get_or_create_customer(db, company.id, payload.client)
        category = crud.get_or_create_project_category(db, company.id, payload.type)
        project = crud.create_project(
            db,
            company.id,
            payload.name,
            "planning",
            planned_start_date=start_date,
            planned_end_date=end_date,
            project_manager_id=payload.project_manager_id,
            branch_id=payload.branch_id,
            customer_id=customer.id if customer else None,
            category_id=category.id if category else None,
            business_unit_id=payload.business_unit_id,
            department_id=payload.department_id,
        )
        crud.record_project_status_change(db, project.id, None, project.status, current_user.employee_id)
        if budget_amount is not None:
            crud.set_project_budget_amount(db, company.id, project.id, budget_amount)
        # pm_projects has no team_lead_id column -- this app's own
        # crud.resolve_project_team_lead_id derives a project's Team Lead by
        # scanning its pm_resource_allocations for the one tagged with the
        # "Team Lead" project-role, so the required Team Lead picked in Add
        # Project is persisted the same way: as a resource allocation
        # carrying that role, not a new column.
        team_lead_role = crud.get_or_create_project_role(db, company.id, "Team Lead")
        # M-26: 0% -- the Team Lead tag reserves no capacity (it used to
        # book the lead at 100% on every project they led).
        crud.create_project_allocation(
            db, project.id, payload.team_lead_id, 0,
            start_date=start_date,
            project_role_id=team_lead_role.id if team_lead_role else None,
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "project", project.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError:
        db.rollback()
        logger.exception(
            "create_project IntegrityError for company=%s name=%r", company.id, payload.name,
        )
        raise HTTPException(
            status_code=400,
            detail="Could not create the project — one of the selected values is invalid.",
        )
    db.refresh(project)
    return _to_out(db, project)


@router.patch("/projects/{project_id}", response_model=schemas.ProjectOut)
def update_project(
    project_id: uuid.UUID,
    payload: schemas.ProjectUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("projects_access", "projects", "edit")
    ),
):
    project = db.get(models.Project, project_id)
    # company_id check (not just "does this id exist"): tenants sharing a
    # schema (see backend/db provisioning notes) store every company's rows
    # in the same tables, so without this a caller could PATCH another
    # company's project just by guessing/enumerating its UUID. 404 (not
    # 403) so that's indistinguishable from a nonexistent id, matching
    # routers/employees.py's identical _require_employee convention.
    if project is None or project.company_id != current_user.company_id:
        logger.warning(
            "update_project: project=%s not found for company=%s",
            project_id, current_user.company_id,
        )
        raise HTTPException(status_code=404, detail="Project not found")
    company = db.get(models.Company, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    if "start" in updates:
        updates["planned_start_date"] = _parse_project_date(updates.pop("start"), "Start date")
    if "end" in updates:
        updates["planned_end_date"] = _parse_project_date(updates.pop("end"), "End date")
    _require_date_order(updates.get("planned_start_date", project.planned_start_date),
                        updates.get("planned_end_date", project.planned_end_date))
    # team_lead_id has no pm_projects column either -- handled below via
    # pm_resource_allocations, same as create_project.
    new_team_lead_id = updates.pop("team_lead_id", None)
    # Client/Type resolve to real pm_projects columns (customer_id/
    # category_id) via get-or-create -- see
    # crud.get_or_create_customer/get_or_create_project_category.
    client_name = updates.pop("client", None)
    if client_name is not None:
        customer = crud.get_or_create_customer(db, company.id, client_name)
        updates["customer_id"] = customer.id if customer else None
    type_name = updates.pop("type", None)
    if type_name is not None:
        category = crud.get_or_create_project_category(db, company.id, type_name)
        updates["category_id"] = category.id if category else None
    # pm_projects has no budget column -- see crud.parse_budget_amount and
    # set_project_budget_amount's own docs. A blank value -- or Edit
    # Project's own "—" placeholder, which it always sends back untouched
    # for a project with no budget yet, since it pre-populates straight
    # from ProjectOut.budget -- leaves whatever budget is already on file
    # untouched (no "clear the budget" gesture exists in this single-field
    # UI); a genuinely non-blank value that doesn't parse is rejected
    # rather than silently discarded.
    budget_text = updates.pop("budget", None)
    budget_amount = None
    if _has_budget_value(budget_text):
        budget_amount = crud.parse_budget_amount(budget_text)
        if budget_amount is None:
            raise HTTPException(
                status_code=400,
                detail="Enter a valid budget amount (e.g. 95L, 9500000, or 95,00,000).",
            )
    # Drop fields removed from DB schema
    for obsolete in ("pm",):
        updates.pop(obsolete, None)
    _require_employee_in_company(db, updates.get("project_manager_id"), company.id, "Project Manager")
    _require_employee_in_company(db, new_team_lead_id, company.id, "Team Lead")
    _require_branch_in_company(db, updates.get("branch_id"), company.id)
    # business_unit_id/department_id are real pm_projects columns, manually
    # added by the user -- see models.Project's own comment.
    _require_business_unit_in_company(db, updates.get("business_unit_id"), company.id)
    _require_department_in_company(db, updates.get("department_id"), company.id)
    try:
        old_status = project.status
        crud.update_project(db, project, updates)
        if budget_amount is not None:
            crud.set_project_budget_amount(db, company.id, project.id, budget_amount)
        if "status" in updates:
            crud.record_project_status_change(
                db, project.id, old_status, updates["status"], current_user.employee_id,
            )
        if new_team_lead_id is not None:
            current_team_lead_id = crud.resolve_project_team_lead_id(db, project.id)
            if current_team_lead_id != new_team_lead_id:
                crud.set_project_team_lead(
                    db, company.id, project.id, new_team_lead_id,
                    start_date=project.planned_start_date,
                )
        crud.create_audit_log(db, company.id, current_user.id, "update", "project", project.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError:
        db.rollback()
        logger.exception(
            "update_project IntegrityError for project=%s company=%s", project_id, company.id,
        )
        raise HTTPException(
            status_code=400,
            detail="Could not save changes — one of the selected values is invalid.",
        )
    db.refresh(project)
    return _to_out(db, project)


@router.get(
    "/projects/{project_id}/status-history",
    response_model=list[schemas.ProjectStatusHistoryOut],
)
def get_project_status_history(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    project = db.get(models.Project, project_id)
    if project is None or project.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return [
        schemas.ProjectStatusHistoryOut(
            id=h.id,
            from_status=_STATUS_DISPLAY.get(h.from_status, h.from_status.title()) if h.from_status else None,
            to_status=_STATUS_DISPLAY.get(h.to_status, h.to_status.title()),
            changed_by_name=crud.employee_display_name(db, h.changed_by),
            changed_at=h.changed_at,
            remarks=h.remarks,
        )
        for h in crud.list_project_status_history(db, project_id)
    ]


def _require_project_in_company(db: Session, project_id: uuid.UUID, company_id: uuid.UUID) -> models.Project:
    project = db.get(models.Project, project_id)
    if project is None or project.company_id != company_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get(
    "/projects/{project_id}/documents",
    response_model=list[schemas.AttachmentOut],
)
def list_project_documents(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Project Details > Documents -- reuses the same generic
    core_attachments table already backing Leave Request and Work Entry
    attachments (see crud.create_attachment/list_attachments_for_entity)."""
    _require_project_in_company(db, project_id, current_user.company_id)
    return [
        schemas.AttachmentOut(
            id=a.id, file_name=a.file_name, file_url=a.file_url,
            mime_type=a.mime_type, size_bytes=a.size_bytes,
        )
        for a in crud.list_attachments_for_entity(db, _PROJECT_ENTITY_TYPE, project_id)
    ]


@router.post(
    "/projects/{project_id}/documents",
    response_model=schemas.AttachmentOut,
    status_code=201,
)
def upload_project_document(  # API-06: sync def -> threadpool (blocking file/DB I/O)
    project_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("projects_access", "projects", "edit")
    ),
):
    company = db.get(models.Company, current_user.company_id)
    _require_project_in_company(db, project_id, company.id)
    file_url, size_bytes = save_uploaded_file(
        file, entity_type=_PROJECT_ENTITY_TYPE, entity_id=project_id
    )
    attachment = crud.create_attachment(
        db,
        company_id=company.id,
        entity_type=_PROJECT_ENTITY_TYPE,
        entity_id=project_id,
        file_name=file.filename or "file",
        file_url=file_url,
        mime_type=file.content_type,
        size_bytes=size_bytes,
    )
    crud.create_audit_log(db, company.id, current_user.id, "create", "project_document", attachment.id)
    db.commit()
    db.refresh(attachment)
    return schemas.AttachmentOut(
        id=attachment.id, file_name=attachment.file_name, file_url=attachment.file_url,
        mime_type=attachment.mime_type, size_bytes=attachment.size_bytes,
    )


@router.delete("/projects/{project_id}/documents/{attachment_id}", status_code=204)
def delete_project_document(
    project_id: uuid.UUID,
    attachment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("projects_access", "projects", "edit")
    ),
):
    company = db.get(models.Company, current_user.company_id)
    _require_project_in_company(db, project_id, company.id)
    attachment = crud.get_attachment_for_entity(db, _PROJECT_ENTITY_TYPE, project_id, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Document not found")
    crud.delete_attachment(db, attachment)
    crud.create_audit_log(db, company.id, current_user.id, "delete", "project_document", attachment_id)
    db.commit()


@router.get(
    "/projects/{project_id}/tasks",
    response_model=list[schemas.ProjectTaskOut],
)
def list_project_tasks(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Add Work Entry's Task picker, scoped to the entry's project."""
    _require_project_in_company(db, project_id, current_user.company_id)
    return [
        schemas.ProjectTaskOut(id=t.id, code=t.code, title=t.title)
        for t in crud.list_tasks_for_project(db, project_id)
    ]


def _task_board_card_out(
    card: models.TaskBoardCard, assignee_name: str | None = None, can_view: bool = True
) -> schemas.TaskBoardCardOut:
    """The board summary every employee sees: title, assignee, assigned
    date, deadline and status -- never the description or submitted work
    (Task Details, gated by app/tasks.can_view_details)."""
    return schemas.TaskBoardCardOut(
        id=card.id,
        column_key=_TASK_DB_TO_COL.get(card.status, card.status),
        title=card.title,
        tag="—",
        assignee=assignee_name or "Unassigned",
        story_points=None,
        code=card.code,
        assignee_employee_id=card.assignee_employee_id,
        assigned_date=card.assigned_date,
        deadline_at=card.deadline_at,
        workflow_status=card.workflow_status,
        status_label=tw.STATUS_LABELS.get(card.workflow_status or ""),
        is_overdue=tw.is_overdue(card),
        can_view_details=can_view,
    )


@router.get("/task-board", response_model=dict[str, list[schemas.TaskBoardCardOut]])
def get_task_board(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    db_board = crud.list_task_board(db, company.id)
    cards = [c for col in db_board.values() for c in col]
    names = crud.employee_display_names_bulk(db, {c.assignee_employee_id for c in cards})
    viewable = tw.can_view_details_bulk(db, current_user, cards)
    # Remap DB status keys to frontend column names
    frontend_board = {_TASK_DB_TO_COL.get(k, k): v for k, v in db_board.items()}
    return {
        col: [_task_board_card_out(c, names.get(c.assignee_employee_id), viewable.get(c.id, False)) for c in cards]
        for col, cards in frontend_board.items()
    }


@router.post(
    "/task-board-cards",
    response_model=schemas.TaskBoardCardOut,
    status_code=201,
)
def create_task_board_card(
    payload: schemas.TaskBoardCardCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("projects_access")),
):
    company = db.get(models.Company, current_user.company_id)
    project = crud.get_project_by_name(db, company.id, payload.project_name)
    if project is None:
        raise HTTPException(status_code=409, detail=f"Project '{payload.project_name}' not found")
    card = crud.create_task_board_card(
        db,
        project.id,
        _task_status_from_column(payload.column_key),
        payload.title,
        company_id=company.id,
    )
    if payload.description is not None and payload.description.strip():
        card.description = payload.description.strip()
    crud.create_audit_log(db, company.id, current_user.id, "create", "task_board_card", card.id)
    if payload.assignee_employee_id is not None:
        # Assigned task: starts the review workflow at Assigned (Backlog),
        # whatever column the dialog picked.
        tw.assign(db, card, current_user, payload.assignee_employee_id, payload.assigned_date, payload.deadline_at)
        card.due_date = card.deadline_at.astimezone(tw.IST).date()
        tw.notify_assigned(db, card, current_user)
    elif payload.assigned_date is not None or payload.deadline_at is not None:
        raise HTTPException(status_code=422, detail="Choose the employee to assign before setting an Assigned Date or Deadline.")
    db.commit()
    db.refresh(card)
    return _task_board_card_out(
        card, crud.employee_display_name(db, card.assignee_employee_id) if card.assignee_employee_id else None,
        tw.can_view_details(db, current_user, card),
    )


@router.patch(
    "/task-board-cards/{card_id}",
    response_model=schemas.TaskBoardCardOut,
)
def move_task_board_card(
    card_id: uuid.UUID,
    payload: schemas.TaskBoardCardMove,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("projects_access")),
):
    company = db.get(models.Company, current_user.company_id)
    card = crud.get_task_board_card_for_update(db, card_id)
    # pm_tasks does carry its own company_id column -- unlike allocations,
    # no join needed. Without this check, any authenticated caller could
    # move another company's task card by guessing/enumerating a card id.
    if card is None or card.company_id != company.id:
        raise HTTPException(status_code=404, detail="Task card not found")
    if card.workflow_status is not None:
        # An assigned task's column follows its review workflow; only a
        # review can complete it (app/tasks.py).
        raise HTTPException(
            status_code=409,
            detail="This task is assigned to an employee — its status changes through the task workflow "
                   "(Start, Submit for Review, Approve / Request Changes), not by moving the card.",
        )
    card.status = _task_status_from_column(payload.column_key)
    crud.create_audit_log(db, company.id, current_user.id, "update", "task_board_card", card.id)
    db.commit()
    db.refresh(card)
    return _task_board_card_out(card)


@router.get(
    "/task-board-cards/{card_id}",
    response_model=schemas.TaskCardDetailOut,
)
def get_task_board_card(
    card_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Task Management > (click a card). An unassigned task: anyone in the
    company (as before). An assigned task: only the assignee, their
    Reporting Manager and the task's reviewer(s) -- 403 for everyone else
    (they see the board summary only); 404 across tenants."""
    tw.require_view(db, current_user, db.get(models.TaskBoardCard, card_id))
    from .tasks import detail_out
    return detail_out(db, current_user, card_id)


@router.patch(
    "/task-board-cards/{card_id}/details",
    response_model=schemas.TaskCardDetailOut,
)
def update_task_board_card(
    card_id: uuid.UUID,
    payload: schemas.TaskCardUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("projects_access")),
):
    """Task Management > Task Details > Edit -- same permission gate as
    creating/moving a card. Never touches status/column (see
    move_task_board_card for that -- kept as its own dedicated "Move to"
    action instead of being folded into this general edit)."""
    card = crud.get_task_board_card_for_update(db, card_id)
    tw.require_view(db, current_user, card)
    updates = payload.model_dump(exclude_unset=True)
    assignment = {k: updates.pop(k) for k in ("assignee_employee_id", "assigned_date", "deadline_at") if k in updates}
    if card.workflow_status == tw.COMPLETED and (assignment or updates):
        raise HTTPException(status_code=409, detail="This task is completed and can no longer be edited.")
    changed = crud.update_task_board_card(card, updates)
    reassigned = False
    if assignment:
        if "assignee_employee_id" in assignment and assignment["assignee_employee_id"] is None:
            if card.assignee_employee_id is not None:
                raise HTTPException(status_code=422, detail="An assigned task can be re-assigned but not un-assigned.")
            assignment.pop("assignee_employee_id")
        assignee_id = assignment.get("assignee_employee_id", card.assignee_employee_id)
        if assignee_id is None:
            raise HTTPException(status_code=422, detail="Choose the employee to assign before setting an Assigned Date or Deadline.")
        new_assignee = assignee_id != card.assignee_employee_id
        new_assigned = assignment.get("assigned_date", card.assigned_date)
        new_deadline = assignment.get("deadline_at", card.deadline_at)
        deadline_changed = new_assignee or ("deadline_at" in assignment and (
            card.deadline_at is None or new_deadline is None or tw.as_aware(new_deadline) != card.deadline_at))
        assigned_changed = new_assignee or ("assigned_date" in assignment and new_assigned != card.assigned_date)
        before = (card.assignee_employee_id, card.assigned_date, card.deadline_at)
        reassigned = tw.assign(db, card, current_user, assignee_id, new_assigned, new_deadline,
                               deadline_changed=deadline_changed, assigned_changed=assigned_changed)
        if before != (card.assignee_employee_id, card.assigned_date, card.deadline_at):
            changed = True
            if not reassigned and before[2] != card.deadline_at:
                tw.record(db, card, current_user, "deadline_changed", card.workflow_status, card.workflow_status,
                          f"Deadline: {tw.fmt_deadline(card.deadline_at)}")
            if "due_date" not in updates:
                card.due_date = card.deadline_at.astimezone(tw.IST).date()
    if not changed:
        raise HTTPException(
            status_code=400,
            detail="No changes to save — the submitted values match the existing task.",
        )
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "task_board_card", card.id)
    if reassigned:
        tw.notify_assigned(db, card, current_user)
    db.commit()
    from .tasks import detail_out
    return detail_out(db, current_user, card_id)


def _sprint_out(sprint: models.Sprint) -> schemas.SprintOut:
    return schemas.SprintOut(
        id=sprint.id,
        name=sprint.name,
        project=sprint.project.name,
        start_date=sprint.planned_start_date,
        end_date=sprint.planned_end_date,
        status=_STATUS_DISPLAY.get(sprint.status, sprint.status.title()),
        velocity_points=None,
    )


@router.get("/sprints", response_model=list[schemas.SprintOut])
def list_sprints(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [
        _sprint_out(s) for s in crud.list_sprints(db, company.id, limit=limit, offset=offset)
    ]


@router.post(
    "/sprints",
    response_model=schemas.SprintOut,
    status_code=201,
)
def create_sprint(
    payload: schemas.SprintCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("projects_access")),
):
    company = db.get(models.Company, current_user.company_id)
    project = crud.get_project_by_name(db, company.id, payload.project_name)
    if project is None:
        raise HTTPException(status_code=409, detail=f"Project '{payload.project_name}' not found")
    # L-15: sprint names are unique per project (case-insensitive).
    crud._advisory_lock(db, "sprint_name", str(project.id))
    if db.scalar(select(models.Sprint.id).where(
            models.Sprint.project_id == project.id,
            func.lower(func.btrim(models.Sprint.name)) == payload.name.lower()).limit(1)) is not None:
        raise HTTPException(status_code=409, detail=f"Sprint '{payload.name}' already exists on this project")
    sprint = crud.create_sprint(
        db,
        project.id,
        payload.name,
        payload.start_date,
        payload.end_date,
    )
    crud.create_audit_log(db, company.id, current_user.id, "create", "sprint", sprint.id)
    db.commit()
    db.refresh(sprint)
    return _sprint_out(sprint)


def _release_out(release: models.Release) -> schemas.ReleaseOut:
    return schemas.ReleaseOut(
        id=release.id,
        name=release.name,
        project=release.project.name,
        target_date=release.due_date,
        status=_STATUS_DISPLAY.get(release.status, release.status.title()),
    )


@router.get("/releases", response_model=list[schemas.ReleaseOut])
def list_releases(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    crud.check_overdue_milestones(db, company.id)
    db.commit()
    return [
        _release_out(r) for r in crud.list_releases(db, company.id, limit=limit, offset=offset)
    ]


@router.post(
    "/releases",
    response_model=schemas.ReleaseOut,
    status_code=201,
)
def create_release(
    payload: schemas.ReleaseCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("projects_access")),
):
    company = db.get(models.Company, current_user.company_id)
    project = crud.get_project_by_name(db, company.id, payload.project_name)
    if project is None:
        raise HTTPException(status_code=409, detail=f"Project '{payload.project_name}' not found")
    release = crud.create_release(db, project.id, payload.name, payload.target_date, payload.notes)
    crud.create_audit_log(db, company.id, current_user.id, "create", "release", release.id)
    db.commit()
    db.refresh(release)
    return _release_out(release)


@router.post(
    "/project-allocations",
    response_model=schemas.ProjectAllocationOut,
    status_code=201,
)
def create_project_allocation(
    payload: schemas.ProjectAllocationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("projects_access", "projects", "assign")
    ),
):
    company = db.get(models.Company, current_user.company_id)
    project = crud.get_project_by_name(db, company.id, payload.project_name)
    if project is None:
        raise HTTPException(status_code=409, detail=f"Project '{payload.project_name}' not found")
    # Without this, a caller could staff another company's employee onto
    # this company's project (or vice versa via UUID guessing) -- same
    # reasoning as create_project's own PM/Team Lead/Branch checks, which
    # this endpoint never had despite being an equally real write path.
    _require_employee_in_company(db, payload.employee_id, company.id, "Employee")
    # Typed Role resolves to a real pm_project_roles row via get-or-create
    # instead of being discarded -- see crud.get_or_create_project_role.
    role = crud.get_or_create_project_role(db, company.id, payload.role) if payload.role else None
    try:
        allocation = crud.create_project_allocation(
            db,
            project.id,
            payload.employee_id,
            payload.allocation_pct,
            project_role_id=role.id if role else None,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "create", "project_allocation", allocation.id)
    db.commit()
    db.refresh(allocation)
    department_id, branch_id = crud.allocation_department_branch(allocation)
    return schemas.ProjectAllocationOut(
        id=allocation.id,
        employee_id=allocation.employee_id,
        employee_name=crud._full_name(allocation.employee),
        project=project.name,
        role=allocation.project_role.name if allocation.project_role else "—",
        allocation_pct=allocation.allocation_pct,
        is_billable=project.is_billable,
        team_lead=crud.employee_display_name(db, crud.resolve_project_team_lead_id(db, project.id)),
        department=crud.department_display_name(db, department_id),
        branch=crud.branch_display_name(db, branch_id),
    )


@router.get(
    "/projects/{project_id}/allocations",
    response_model=list[schemas.ProjectAllocationOut],
)
def list_project_allocations(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Edit Project > Team Members roster."""
    project = _require_project_in_company(db, project_id, current_user.company_id)
    # PERF-09: allocations (+ roles + employees) loaded once, team lead
    # derived from that same list, department/branch names in bulk --
    # instead of re-listing allocations and 3 lookups per row.
    allocations = crud.list_project_allocations(db, project_id, with_employee=True)
    team_lead_id = crud.team_lead_id_from_allocations(allocations)
    team_lead_name = crud.employee_display_name(db, team_lead_id)
    dept_branch = [crud.allocation_department_branch(a) for a in allocations]
    dept_names = crud.names_by_id_bulk(db, models.Department, [d for d, _b in dept_branch])
    branch_names = crud.names_by_id_bulk(db, models.Branch, [b for _d, b in dept_branch])
    result = []
    for a, (department_id, branch_id) in zip(allocations, dept_branch):
        result.append(
            schemas.ProjectAllocationOut(
                id=a.id,
                employee_id=a.employee_id,
                employee_name=crud._full_name(a.employee),
                project=project.name,
                role=a.project_role.name if a.project_role else "—",
                allocation_pct=a.allocation_pct,
                is_billable=project.is_billable,
                team_lead=team_lead_name,
                department=dept_names.get(department_id, "—") if department_id else "—",
                branch=branch_names.get(branch_id, "—") if branch_id else "—",
            )
        )
    return result


@router.delete("/project-allocations/{allocation_id}", status_code=204)
def remove_project_allocation(
    allocation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("projects_access", "projects", "assign")
    ),
):
    """Edit Project > Team Members > remove."""
    company = db.get(models.Company, current_user.company_id)
    # pm_resource_allocations has no company_id column of its own -- check
    # via its project, same reasoning as update_project's own company
    # guard. Without this, any authenticated caller could delete another
    # company's team member by guessing/enumerating an allocation id.
    allocation = db.get(models.ProjectAllocation, allocation_id)
    if allocation is None or allocation.project.company_id != company.id:
        raise HTTPException(status_code=404, detail="Project allocation not found")
    crud.delete_project_allocation(db, allocation_id)
    crud.create_audit_log(db, company.id, current_user.id, "delete", "project_allocation", allocation_id)
    db.commit()
