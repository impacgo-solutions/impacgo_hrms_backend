import calendar
import datetime
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user, require_permission
from ..storage import delete_uploaded_file, save_uploaded_file

router = APIRouter(prefix="/api", tags=["organization"])


# ── shared validation (L-05, L-06, H-21) ───────────────────────────────────

def _name_taken(db: Session, model, name: str, *, exclude_id: uuid.UUID | None = None, **scope) -> bool:
    """Case-insensitive duplicate check among ACTIVE rows in `scope`
    (company_id=... or department_id=...)."""
    q = select(model.id).where(func.lower(func.trim(model.name)) == name.strip().lower(), model.is_active.is_(True))
    for column, value in scope.items():
        q = q.where(getattr(model, column) == value)
    if exclude_id is not None:
        q = q.where(model.id != exclude_id)
    return db.scalar(q.limit(1)) is not None


def _check_people(db: Session, company_id: uuid.UUID, data: dict, labels: dict[str, str]) -> None:
    """H-21: every employee reference (branch manager, department head, HR
    representative, ...) must be an active employee of this company."""
    from ..employee_validation import manager_error

    for key, label in labels.items():
        error = manager_error(db, company_id, data.get(key), label)
        if error:
            raise HTTPException(status_code=422, detail=error)


def _check_branch(db: Session, company_id: uuid.UUID, branch_id: uuid.UUID | None) -> None:
    if branch_id is None:
        return
    branch = db.get(models.Branch, branch_id)
    if branch is None or branch.company_id != company_id:
        raise HTTPException(status_code=422, detail="Branch not found in your company.")


def _resolve_business_unit(db: Session, company_id: uuid.UUID, data: dict) -> None:
    """L-06: business_unit_name must name an existing unit (case-insensitive)
    -- an unknown one is a 422, never a silently created unit. Rewrites the
    name to the stored spelling so crud's exact-name lookup finds it."""
    name = data.get("business_unit_name")
    if not name:
        return
    unit = db.scalar(
        select(models.BusinessUnit).where(
            models.BusinessUnit.company_id == company_id,
            func.lower(models.BusinessUnit.name) == name.strip().lower(),
        )
    )
    if unit is None:
        raise HTTPException(status_code=422, detail=f"Business unit '{name}' not found. Create it first.")
    data["business_unit_name"] = unit.name


def _own(db: Session, model, row_id: uuid.UUID, company_id: uuid.UUID, label: str):
    row = db.get(model, row_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return row


_DEPT_PEOPLE = {
    "head_employee_id": "Department Head", "hr_representative_id": "HR Representative",
    "senior_manager_id": "Senior Manager", "project_manager_id": "Project Manager",
}


@router.get("/organization/dashboard-summary", response_model=schemas.OrganizationDashboardSummaryOut)
def get_organization_dashboard_summary(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("reports_analytics")),
):
    """Organization Dashboard summary cards -- pure aggregation over
    tables that already exist (employees, branches, business units,
    departments, sub-departments, designations, roles, pending approvals,
    employee transfers); no new storage."""
    summary = crud.get_organization_dashboard_summary(db, current_user.company_id)
    return schemas.OrganizationDashboardSummaryOut(**summary)


@router.get("/company-profile", response_model=schemas.CompanyProfileOut)
def get_company_profile(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    address = ", ".join(
        part
        for part in (
            company.address_line1,
            company.address_line2,
            company.city,
            f"{company.state} {company.pincode}".strip() if company.state or company.pincode else None,
            company.country,
        )
        if part
    ) or "—"
    fiscal_start = calendar.month_name[company.fiscal_year_start_month]
    fiscal_end = calendar.month_name[(company.fiscal_year_start_month + 10) % 12 + 1]
    active_users = db.scalar(
        select(func.count(models.Employee.id)).where(
            models.Employee.company_id == company.id,
            models.Employee.is_active.is_(True),
        )
    )
    return schemas.CompanyProfileOut(
        legal_name=company.legal_name or company.name,
        display_name=company.name,
        fiscal_year=f"{fiscal_start} – {fiscal_end}",
        fiscal_year_start_month=company.fiscal_year_start_month,
        registered_address=address,
        cin=company.cin or "—",
        gstin=company.gstin or "—",
        pan=company.pan or "—",
        active_users=active_users or 0,
        address_line1=company.address_line1,
        address_line2=company.address_line2,
        city=company.city,
        state=company.state,
        pincode=company.pincode,
        country=company.country,
        industry=company.industry or "—",
        founded_date=company.founded_date.isoformat() if company.founded_date else "—",
        email_domain=crud.derive_company_email_domain(company),
        branch_head=crud.employee_display_name(db, company.branch_head_id),
        branch_head_id=company.branch_head_id,
        logo_url=company.logo_url or None,
    )


@router.patch(
    "/company-profile",
    response_model=schemas.CompanyProfileOut,
)
def update_company_profile(
    payload: schemas.CompanyProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    if "display_name" in updates:
        updates["name"] = updates.pop("display_name")
    if "founded_date" in updates:
        if updates["founded_date"]:
            try:
                updates["founded_date"] = datetime.date.fromisoformat(updates["founded_date"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="founded_date must be YYYY-MM-DD") from exc
        else:
            # Blank means "leave untouched" -- an empty string can't be
            # written to a real date column.
            del updates["founded_date"]
    crud.update_company_profile(db, company, updates)
    company.updated_by = current_user.employee_id
    company.updated_at = datetime.datetime.now(datetime.timezone.utc)
    crud.create_audit_log(db, company.id, current_user.id, "update", "company", company.id)
    db.commit()
    db.refresh(company)
    return get_company_profile(db, current_user)


# Company logo: printed on letterheads (Experience & Relieving letters,
# offer letters, payslips) via template_rendering.resolve_company_logo.
# PNG/JPG only, verified by content as well as extension (no SVG: it can
# carry script).
_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg"}
_LOGO_MAX_BYTES = 2 * 1024 * 1024


def _is_png_or_jpeg(head: bytes) -> bool:
    return head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")


@router.post("/company-profile/logo", response_model=schemas.CompanyProfileOut)
def upload_company_logo(  # sync def -> threadpool (blocking file/DB I/O)
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    """Organization > Company Profile > Company Logo. Replaces any existing
    logo; the old file is deleted only after the new one is committed."""
    extension = Path(file.filename or "").suffix.lower()
    if extension not in _LOGO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only PNG and JPG images are supported for the company logo.")
    if not _is_png_or_jpeg(file.file.read(8)):
        raise HTTPException(status_code=400, detail="The selected file is not a valid PNG or JPG image.")
    file.file.seek(0)
    company = db.get(models.Company, current_user.company_id)
    file_url, size_bytes = save_uploaded_file(file, entity_type="company_logo", entity_id=company.id)
    if size_bytes > _LOGO_MAX_BYTES:
        delete_uploaded_file(file_url)
        raise HTTPException(
            status_code=400,
            detail=f"The company logo must be smaller than {_LOGO_MAX_BYTES // (1024 * 1024)}MB.",
        )
    previous_url = company.logo_url
    company.logo_url = file_url
    company.updated_by = current_user.employee_id
    company.updated_at = datetime.datetime.now(datetime.timezone.utc)
    crud.create_audit_log(db, company.id, current_user.id, "update", "company_logo", company.id)
    try:
        db.commit()
    except Exception:
        db.rollback()
        delete_uploaded_file(file_url)
        raise
    if previous_url:
        delete_uploaded_file(previous_url)
    db.refresh(company)
    return get_company_profile(db, current_user)


@router.delete("/company-profile/logo", response_model=schemas.CompanyProfileOut)
def delete_company_logo(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    previous_url = company.logo_url
    if previous_url:
        company.logo_url = None
        company.updated_by = current_user.employee_id
        company.updated_at = datetime.datetime.now(datetime.timezone.utc)
        crud.create_audit_log(db, company.id, current_user.id, "update", "company_logo", company.id)
        db.commit()
        delete_uploaded_file(previous_url)
        db.refresh(company)
    return get_company_profile(db, current_user)


@router.get("/org-hierarchy", response_model=list[schemas.OrgHierarchyTierOut])
def get_org_hierarchy(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.org_hierarchy_summary(db, company.id)


@router.get("/band-hierarchy", response_model=schemas.BandHierarchyOut)
def get_band_hierarchy(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs the Organization > Designations and Org Chart tabs' real,
    tenant-configured Band -> Role -> Designation tree (core_bands ->
    core_roles.band_id -> core_designations.role_id) -- both tabs render
    from this same endpoint so they always stay aligned. `configured: false`
    means this company hasn't mapped any role to a band yet; callers keep
    showing their pre-hierarchy legacy view in that case."""
    company = db.get(models.Company, current_user.company_id)
    return crud.band_role_designation_hierarchy(db, company.id)


@router.get("/branches", response_model=list[schemas.BranchOut])
def list_branches(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    branches = crud.list_branches(db, company.id)
    # PERF-08: one GROUP BY count + one name lookup for all branches.
    emp_counts = crud.count_active_employees_by(db, models.Employee.branch_id, [b.id for b in branches])
    manager_names = crud.employee_display_names_bulk(db, [b.branch_manager_id for b in branches])
    return [
        schemas.BranchOut(
            id=b.id,
            code=b.code,
            name=b.name,
            country=b.country or "—",
            state=b.state,
            city=b.city,
            tz=b.tz or "—",
            tax=b.gstin or "—",
            emp=emp_counts.get(b.id, 0),
            address_line1=b.address_line1,
            pincode=b.pincode,
            branch_manager=manager_names.get(b.branch_manager_id, "—") if b.branch_manager_id else "—",
            branch_manager_id=b.branch_manager_id,
        )
        for b in branches
    ]


@router.post(
    "/branches",
    response_model=schemas.BranchOut,
    status_code=201,
)
def create_branch(
    payload: schemas.BranchCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    if _name_taken(db, models.Branch, payload.name, company_id=company.id):
        raise HTTPException(status_code=409, detail=f"A branch named '{payload.name}' already exists")
    _check_people(db, company.id, payload.model_dump(), {"branch_manager_id": "Branch Manager"})
    try:
        branch = crud.create_branch(db, company.id, payload)
        crud.create_audit_log(db, company.id, current_user.id, "create", "branch", branch.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Branch already exists") from exc
    db.refresh(branch)
    return schemas.BranchOut(
        id=branch.id,
        code=branch.code,
        name=branch.name,
        country=branch.country or "—",
        state=branch.state,
        city=branch.city,
        tz=branch.tz or "—",
        tax=branch.gstin or "—",
        emp=0,
        address_line1=branch.address_line1,
        pincode=branch.pincode,
        branch_manager=crud.employee_display_name(db, branch.branch_manager_id),
        branch_manager_id=branch.branch_manager_id,
    )


@router.patch("/branches/{branch_id}", response_model=schemas.BranchOut)
def update_branch(
    branch_id: uuid.UUID,
    payload: schemas.BranchUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    branch = _own(db, models.Branch, branch_id, current_user.company_id, "Branch")
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("name") and _name_taken(
        db, models.Branch, updates["name"], exclude_id=branch.id, company_id=branch.company_id
    ):
        raise HTTPException(status_code=409, detail=f"A branch named '{updates['name']}' already exists")
    _check_people(db, branch.company_id, updates, {"branch_manager_id": "Branch Manager"})
    if "tax" in updates:
        updates["gstin"] = updates.pop("tax")
    crud.update_branch(db, branch, updates)
    company = db.get(models.Company, current_user.company_id)
    crud.create_audit_log(db, company.id, current_user.id, "update", "branch", branch.id)
    db.commit()
    db.refresh(branch)
    return schemas.BranchOut(
        id=branch.id,
        code=branch.code,
        name=branch.name,
        country=branch.country or "—",
        state=branch.state,
        city=branch.city,
        tz=branch.tz or "—",
        tax=branch.gstin or "—",
        emp=crud.count_employees_in_branch(db, branch.id),
        address_line1=branch.address_line1,
        pincode=branch.pincode,
        branch_manager=crud.employee_display_name(db, branch.branch_manager_id),
        branch_manager_id=branch.branch_manager_id,
    )


@router.get("/business-units", response_model=list[schemas.BusinessUnitOut])
def list_business_units(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    units = crud.list_business_units(db, company.id)
    # PERF-08: bulk head/branch names instead of 2 lookups per unit.
    head_names = crud.employee_display_names_bulk(db, [u.head_employee_id for u in units])
    branch_names = crud.names_by_id_bulk(db, models.Branch, [u.branch_id for u in units])
    return [
        schemas.BusinessUnitOut(
            id=u.id,
            name=u.name,
            head=head_names.get(u.head_employee_id, "—") if u.head_employee_id else "—",
            head_employee_id=u.head_employee_id,
            cost_center=u.cost_center or "—",
            branch=branch_names.get(u.branch_id, "—") if u.branch_id else "—",
            branch_id=u.branch_id,
        )
        for u in units
    ]


@router.post(
    "/business-units",
    response_model=schemas.BusinessUnitOut,
    status_code=201,
)
def create_business_unit(
    payload: schemas.BusinessUnitCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    if _name_taken(db, models.BusinessUnit, payload.name, company_id=company.id):
        raise HTTPException(status_code=409, detail=f"Business unit '{payload.name}' already exists")
    _check_people(db, company.id, payload.model_dump(), {"head_employee_id": "Business Unit Head"})
    _check_branch(db, company.id, payload.branch_id)
    try:
        unit = crud.create_business_unit(
            db,
            company.id,
            payload.name,
            payload.cost_center,
            payload.head_employee_id,
            payload.branch_id,
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "business_unit", unit.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(unit)
    return schemas.BusinessUnitOut(
        id=unit.id,
        name=unit.name,
        head=crud.employee_display_name(db, unit.head_employee_id),
        head_employee_id=unit.head_employee_id,
        cost_center=unit.cost_center or "—",
        branch=crud.branch_display_name(db, unit.branch_id),
        branch_id=unit.branch_id,
    )


@router.patch("/business-units/{business_unit_id}", response_model=schemas.BusinessUnitOut)
def update_business_unit(
    business_unit_id: uuid.UUID,
    payload: schemas.BusinessUnitUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    unit = _own(db, models.BusinessUnit, business_unit_id, current_user.company_id, "Business unit")
    company = db.get(models.Company, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("name") and _name_taken(
        db, models.BusinessUnit, updates["name"], exclude_id=unit.id, company_id=company.id
    ):
        raise HTTPException(status_code=409, detail=f"Business unit '{updates['name']}' already exists")
    _check_people(db, company.id, updates, {"head_employee_id": "Business Unit Head"})
    _check_branch(db, company.id, updates.get("branch_id"))
    try:
        crud.update_business_unit(db, unit, updates)
        crud.create_audit_log(db, company.id, current_user.id, "update", "business_unit", unit.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Business unit name already exists") from exc
    db.refresh(unit)
    return schemas.BusinessUnitOut(
        id=unit.id,
        name=unit.name,
        head=crud.employee_display_name(db, unit.head_employee_id),
        head_employee_id=unit.head_employee_id,
        cost_center=unit.cost_center or "—",
        branch=crud.branch_display_name(db, unit.branch_id),
        branch_id=unit.branch_id,
    )


@router.post(
    "/business-units/{business_unit_id}/departments",
    response_model=list[str],
)
def assign_departments_to_business_unit(
    business_unit_id: uuid.UUID,
    payload: schemas.BusinessUnitDepartmentsAssign,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    _own(db, models.BusinessUnit, business_unit_id, company.id, "Business unit")
    foreign = db.scalar(
        select(func.count(models.Department.id)).where(
            models.Department.id.in_(payload.department_ids), models.Department.company_id != company.id
        )
    )
    if foreign:
        raise HTTPException(status_code=422, detail="One or more departments don't belong to your company.")
    names = crud.assign_departments_to_business_unit(
        db, business_unit_id, payload.department_ids
    )
    crud.create_audit_log(db, company.id, current_user.id, "update", "business_unit", business_unit_id)
    db.commit()
    return names


@router.get("/departments", response_model=list[schemas.DepartmentOut])
def list_departments(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    departments = crud.list_departments(db, company.id)
    # PERF-08: every per-department lookup (business unit, sub-departments,
    # headcount, branch, 4 employee names) batched into one query each.
    dept_ids = [d.id for d in departments]
    unit_names = crud.names_by_id_bulk(db, models.BusinessUnit, [d.business_unit_id for d in departments])
    subs_by_dept = crud.list_sub_departments_bulk(db, dept_ids)
    emp_counts = crud.count_active_employees_by(db, models.Employee.department_id, dept_ids)
    branch_names = crud.names_by_id_bulk(db, models.Branch, [d.branch_id for d in departments])
    people = crud.employee_display_names_bulk(
        db,
        [
            eid
            for d in departments
            for eid in (d.head_employee_id, d.hr_representative_id, d.senior_manager_id, d.project_manager_id)
        ],
    )

    def _name(eid):
        return people.get(eid, "—") if eid is not None else "—"

    result = []
    for d in departments:
        parent_name = "—"
        if d.business_unit_id is not None:
            parent_name = unit_names.get(d.business_unit_id, "—")
        sub_rows = subs_by_dept.get(d.id, [])
        result.append(
            schemas.DepartmentOut(
                id=d.id,
                code=d.code,
                name=d.name,
                head=_name(d.head_employee_id),
                head_employee_id=d.head_employee_id,
                budget=crud.format_inr_budget(d.annual_budget),
                parent=parent_name,
                emp=emp_counts.get(d.id, 0),
                subs=[s.name for s in sub_rows],
                sub_departments=[schemas.SubDepartmentOut(id=s.id, name=s.name) for s in sub_rows],
                annual_budget=d.annual_budget,
                branch=branch_names.get(d.branch_id, "—") if d.branch_id else "—",
                branch_id=d.branch_id,
                business_unit_id=d.business_unit_id,
                hr_representative=_name(d.hr_representative_id),
                hr_representative_id=d.hr_representative_id,
                senior_manager=_name(d.senior_manager_id),
                senior_manager_id=d.senior_manager_id,
                project_manager=_name(d.project_manager_id),
                project_manager_id=d.project_manager_id,
            )
        )
    return result


@router.post(
    "/departments",
    response_model=schemas.DepartmentOut,
    status_code=201,
)
def create_department(
    payload: schemas.DepartmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    if _name_taken(db, models.Department, payload.name, company_id=company.id):
        raise HTTPException(status_code=409, detail=f"A department named '{payload.name}' already exists")
    data = payload.model_dump()
    _resolve_business_unit(db, company.id, data)
    payload.business_unit_name = data["business_unit_name"]
    _check_people(db, company.id, data, _DEPT_PEOPLE)
    _check_branch(db, company.id, payload.branch_id)
    try:
        department = crud.create_department(db, company.id, payload)
        crud.create_audit_log(db, company.id, current_user.id, "create", "department", department.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(department)
    return schemas.DepartmentOut(
        id=department.id,
        code=department.code,
        name=department.name,
        head=crud.employee_display_name(db, department.head_employee_id),
        head_employee_id=department.head_employee_id,
        budget=crud.format_inr_budget(department.annual_budget),
        parent=payload.business_unit_name or "—",
        emp=0,
        subs=[],
        annual_budget=department.annual_budget,
        branch=crud.branch_display_name(db, department.branch_id),
        branch_id=department.branch_id,
        business_unit_id=department.business_unit_id,
        hr_representative=crud.employee_display_name(db, department.hr_representative_id),
        hr_representative_id=department.hr_representative_id,
        senior_manager=crud.employee_display_name(db, department.senior_manager_id),
        senior_manager_id=department.senior_manager_id,
        project_manager=crud.employee_display_name(db, department.project_manager_id),
        project_manager_id=department.project_manager_id,
    )


@router.patch("/departments/{department_id}", response_model=schemas.DepartmentOut)
def update_department(
    department_id: uuid.UUID,
    payload: schemas.DepartmentUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    department = _own(db, models.Department, department_id, current_user.company_id, "Department")
    company = db.get(models.Company, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("name") and _name_taken(
        db, models.Department, updates["name"], exclude_id=department.id, company_id=company.id
    ):
        raise HTTPException(status_code=409, detail=f"A department named '{updates['name']}' already exists")
    _resolve_business_unit(db, company.id, updates)
    _check_people(db, company.id, updates, _DEPT_PEOPLE)
    _check_branch(db, company.id, updates.get("branch_id"))
    crud.update_department(db, department, updates, company.id)
    crud.create_audit_log(db, company.id, current_user.id, "update", "department", department.id)
    db.commit()
    db.refresh(department)
    parent_name = "—"
    if department.business_unit_id is not None:
        unit = db.get(models.BusinessUnit, department.business_unit_id)
        parent_name = unit.name if unit is not None else "—"
    sub_rows = crud.list_sub_departments(db, department.id)
    return schemas.DepartmentOut(
        id=department.id,
        code=department.code,
        name=department.name,
        head=crud.employee_display_name(db, department.head_employee_id),
        head_employee_id=department.head_employee_id,
        budget=crud.format_inr_budget(department.annual_budget),
        parent=parent_name,
        emp=crud.count_employees_in_department(db, department.id),
        subs=[s.name for s in sub_rows],
        sub_departments=[schemas.SubDepartmentOut(id=s.id, name=s.name) for s in sub_rows],
        annual_budget=department.annual_budget,
        branch=crud.branch_display_name(db, department.branch_id),
        branch_id=department.branch_id,
        business_unit_id=department.business_unit_id,
        hr_representative=crud.employee_display_name(db, department.hr_representative_id),
        hr_representative_id=department.hr_representative_id,
        senior_manager=crud.employee_display_name(db, department.senior_manager_id),
        senior_manager_id=department.senior_manager_id,
        project_manager=crud.employee_display_name(db, department.project_manager_id),
        project_manager_id=department.project_manager_id,
    )


@router.post(
    "/departments/{department_id}/sub-departments",
    response_model=list[schemas.SubDepartmentOut],
    status_code=201,
)
def add_sub_department(
    department_id: uuid.UUID,
    payload: schemas.SubDepartmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    _own(db, models.Department, department_id, company.id, "Department")
    if _name_taken(db, models.SubDepartment, payload.name, department_id=department_id):
        raise HTTPException(status_code=409, detail=f"Sub-department '{payload.name}' already exists in this department")
    sub = crud.create_sub_department(db, department_id, payload.name)
    crud.create_audit_log(db, company.id, current_user.id, "create", "sub_department", sub.id)
    db.commit()
    rows = crud.list_sub_departments(db, department_id)
    return [schemas.SubDepartmentOut(id=r.id, name=r.name) for r in rows]


@router.patch(
    "/departments/{department_id}/sub-departments/{sub_department_id}",
    response_model=list[schemas.SubDepartmentOut],
)
def update_sub_department(
    department_id: uuid.UUID,
    sub_department_id: uuid.UUID,
    payload: schemas.SubDepartmentUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    _own(db, models.Department, department_id, current_user.company_id, "Department")
    sub = db.get(models.SubDepartment, sub_department_id)
    if sub is None or sub.department_id != department_id:
        raise HTTPException(status_code=404, detail="Sub-department not found")
    if _name_taken(db, models.SubDepartment, payload.name, exclude_id=sub.id, department_id=department_id):
        raise HTTPException(status_code=409, detail=f"Sub-department '{payload.name}' already exists in this department")
    company = db.get(models.Company, current_user.company_id)
    crud.update_sub_department(db, sub, payload.name)
    crud.create_audit_log(db, company.id, current_user.id, "update", "sub_department", sub.id)
    db.commit()
    rows = crud.list_sub_departments(db, department_id)
    return [schemas.SubDepartmentOut(id=r.id, name=r.name) for r in rows]
