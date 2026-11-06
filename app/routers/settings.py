from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import crud, document_numbers, employee_validation, models, schemas, weekly_off
from ..database import get_db
from ..deps import (
    get_current_user,
    require_people_access,
    require_permission,
    require_permission_or_action,
)

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/notification-preferences", response_model=list[schemas.NotificationPreferenceOut])
def list_notification_preferences(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return crud.list_notification_preferences(db, current_user.id)


@router.put("/notification-preferences", response_model=schemas.NotificationPreferenceOut)
def update_notification_preference(
    payload: schemas.NotificationPreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    pref = crud.upsert_notification_preference(
        db, current_user.id, payload.event_key, payload.channel, payload.enabled
    )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "notification_preference", pref.id
    )
    db.commit()
    return pref


@router.get("/integrations", response_model=list[schemas.IntegrationOut])
def list_integrations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_integrations(db, company.id)


@router.put("/integrations", response_model=schemas.IntegrationOut)
def update_integration(
    payload: schemas.IntegrationUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    company = db.get(models.Company, current_user.company_id)
    integration = crud.upsert_integration(
        db, company.id, payload.name, payload.description, payload.is_connected
    )
    crud.create_audit_log(db, company.id, current_user.id, "update", "integration", integration.id)
    db.commit()
    return integration


@router.get("/company-settings", response_model=schemas.CompanySettingsOut)
def get_company_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    settings_row = crud.get_company_settings(db, company.id)
    if settings_row is None:
        return schemas.CompanySettingsOut(
            default_currency="INR",
            default_timezone="IST (UTC+5:30)",
            working_days_per_week=5,
            pf_wage_ceiling=15000,
        )
    return settings_row


@router.patch("/company-settings", response_model=schemas.CompanySettingsOut)
def update_company_settings(
    payload: schemas.CompanySettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    company = db.get(models.Company, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    # Weekly Off Days and Working Days / Week describe the same week: saving
    # the days sets the count; a count alone that no longer fits the saved
    # days drops them back to the default for that count (weekly_off.py).
    off_days = updates.pop("weekly_off_days", None)
    if off_days is not None:
        try:
            off_days = weekly_off.normalize(off_days)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        working = 7 - len(off_days)
        if updates.get("working_days_per_week") not in (None, working):
            raise HTTPException(
                status_code=422,
                detail=f"Working Days / Week must be {working} with {len(off_days)} weekly off day(s).",
            )
        updates["working_days_per_week"] = working
    settings_row = crud.upsert_company_settings(db, company.id, updates)
    if off_days is not None:
        settings_row.weekly_off_days = ",".join(off_days)
    elif updates.get("working_days_per_week") is not None:
        saved = weekly_off.parse(settings_row.weekly_off_days)
        if saved is not None and len(saved) != 7 - settings_row.working_days_per_week:
            settings_row.weekly_off_days = None
    # An explicit null on this one optional limit means "no limit" (the
    # generic upsert skips None, so clear it here).
    if "work_entry_backdate_days" in payload.model_fields_set and payload.work_entry_backdate_days is None:
        settings_row.work_entry_backdate_days = None
    crud.create_audit_log(
        db, company.id, current_user.id, "update", "company_settings", company.id
    )
    db.commit()
    return settings_row


@router.get("/settings/employee-id-format", response_model=schemas.EmployeeIdFormatOut)
def get_employee_id_format(
    db: Session = Depends(get_db),
    # Same gate as POST /api/employees itself -- whoever can create an
    # employee needs to see the suggested next ID, not just admins.
    current_user: models.User = Depends(require_people_access("create")),
):
    series = crud.get_employee_id_format(db, current_user.company_id)
    db.commit()  # persists get_or_create's auto-provisioned row, if this was the first call
    return schemas.EmployeeIdFormatOut(
        prefix=series.prefix,
        suffix=series.suffix,
        padding=series.padding,
        next_code=crud.preview_next_employee_code(series, db, current_user.company_id),
        generation_mode=series.generation_mode or "auto",
    )


@router.patch("/settings/employee-id-format", response_model=schemas.EmployeeIdFormatOut)
def update_employee_id_format(
    payload: schemas.EmployeeIdFormatUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    series = crud.update_employee_id_format(
        db, current_user.company_id,
        prefix=payload.prefix, suffix=payload.suffix,
        padding=payload.padding, next_number=payload.next_number,
        generation_mode=payload.generation_mode,
    )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id,
        "update", "employee_id_format", series.id,
    )
    db.commit()
    return schemas.EmployeeIdFormatOut(
        prefix=series.prefix,
        suffix=series.suffix,
        padding=series.padding,
        next_code=crud.preview_next_employee_code(series, db, current_user.company_id),
        generation_mode=series.generation_mode or "auto",
    )


def _id_format_out(db: Session, company_id, key: str) -> schemas.IdFormatOut:
    d = document_numbers.doctype(key)
    series = document_numbers.get_series(db, company_id, key)
    return schemas.IdFormatOut(
        doctype=key, label=d.label, prefix=series.prefix, suffix=series.suffix, padding=series.padding,
        include_year=bool(series.fiscal_infix), next_number=series.current_no + 1,
        next_code=document_numbers.preview(db, company_id, key),
    )


@router.get("/settings/id-formats", response_model=list[schemas.IdFormatOut])
def list_id_formats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    """Administration > General Settings > Document ID Formats."""
    out = [_id_format_out(db, current_user.company_id, key) for key in document_numbers.DOCTYPES]
    db.commit()  # persists default rows created on first use
    return out


@router.get("/settings/id-formats/{doctype}/preview", response_model=schemas.IdPreviewOut)
def preview_id(
    doctype: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The ID the next record would get -- shown on the "new" forms. A
    preview only: nothing is reserved."""
    if doctype not in document_numbers.DOCTYPES:
        raise HTTPException(status_code=404, detail="Unknown document type")
    code = document_numbers.preview(db, current_user.company_id, doctype)
    db.commit()
    return schemas.IdPreviewOut(doctype=doctype, next_code=code)


@router.patch("/settings/id-formats/{doctype}", response_model=schemas.IdFormatOut)
def update_id_format(
    doctype: str,
    payload: schemas.IdFormatUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    if doctype not in document_numbers.DOCTYPES:
        raise HTTPException(status_code=404, detail="Unknown document type")
    prefix = payload.prefix.strip() if payload.prefix is not None else None
    suffix = payload.suffix.strip() if payload.suffix is not None else None
    series = document_numbers.update_format(
        db, current_user.company_id, doctype, prefix=prefix, suffix=suffix, padding=payload.padding,
        next_number=payload.next_number, include_year=payload.include_year)
    if not series.prefix and not series.suffix and not series.fiscal_infix and series.padding < 3:
        raise HTTPException(status_code=422, detail="Add a prefix or suffix, or use at least 3 digits.")
    year = "2026-" if series.fiscal_infix else ""
    if len(f"{series.prefix}{year}{'9' * series.padding}{series.suffix}") > 30:
        raise HTTPException(status_code=422, detail="The ID would be longer than 30 characters.")
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", f"id_format:{doctype}", series.id)
    db.commit()
    return _id_format_out(db, current_user.company_id, doctype)


def _project_id_format_out(db: Session, company_id, series: models.NumberSeries) -> schemas.ProjectIdFormatOut:
    return schemas.ProjectIdFormatOut(
        prefix=series.prefix, suffix=series.suffix, padding=series.padding,
        next_number=series.current_no + 1,
        next_code=crud.preview_next_project_code(db, company_id, series),
    )


@router.get("/settings/project-id-format", response_model=schemas.ProjectIdFormatOut)
def get_project_id_format(
    db: Session = Depends(get_db),
    # Whoever can create a project sees the next Project ID (Add Project).
    current_user: models.User = Depends(require_permission_or_action("projects_access", "projects", "edit")),
):
    series = crud.get_or_create_project_number_series(db, current_user.company_id)
    db.commit()  # persists the default row on first use
    return _project_id_format_out(db, current_user.company_id, series)


@router.get("/settings/project-id-format/admin", response_model=schemas.ProjectIdFormatOut)
def get_project_id_format_admin(
    db: Session = Depends(get_db),
    # The General Settings card (settings admins, who may not manage projects).
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    series = crud.get_or_create_project_number_series(db, current_user.company_id)
    db.commit()
    return _project_id_format_out(db, current_user.company_id, series)


@router.patch("/settings/project-id-format", response_model=schemas.ProjectIdFormatOut)
def update_project_id_format(
    payload: schemas.ProjectIdFormatUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    prefix = payload.prefix.strip() if payload.prefix is not None else None
    suffix = payload.suffix.strip() if payload.suffix is not None else None
    if prefix is not None and suffix is not None and not prefix and not suffix and (payload.padding or 1) < 3:
        raise HTTPException(status_code=422, detail="Add a prefix or suffix, or use at least 3 digits.")
    series = crud.update_project_id_format(
        db, current_user.company_id, prefix=prefix, suffix=suffix,
        padding=payload.padding, next_number=payload.next_number,
    )
    sample = f"{series.prefix}{'9' * series.padding}{series.suffix}"
    if len(sample) > 30:
        raise HTTPException(status_code=422, detail="The Project ID would be longer than 30 characters.")
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "project_id_format", series.id)
    db.commit()
    return _project_id_format_out(db, current_user.company_id, series)


@router.get("/settings/employee-id-availability", response_model=schemas.EmployeeIdAvailabilityOut)
def employee_id_availability(
    code: str = Query(..., min_length=1, max_length=20),
    db: Session = Depends(get_db),
    # Same gate as the format GETs -- anyone who can create an employee.
    current_user: models.User = Depends(require_people_access("create")),
):
    """Lets Add Employee flag an already-used Employee ID while it's being
    typed, instead of only after Save."""
    code = code.strip()
    return schemas.EmployeeIdAvailabilityOut(
        code=code,
        available=bool(code) and not crud.employee_code_taken(db, current_user.company_id, code),
    )


@router.get("/settings/employee-id-formats", response_model=list[schemas.EmployeeIdFormatItem])
def list_employee_id_formats(
    db: Session = Depends(get_db),
    # Same gate as the singular GET above -- whoever can create an employee
    # needs to see every type's suggested next ID, not just admins.
    current_user: models.User = Depends(require_people_access("create")),
):
    """Administration > General Settings' per-Employment-Type Employee ID
    Format list. One row per canonical employment type (Full-time included)
    -- Full-time always reads the shared default series directly (is_custom
    always True for it, since it IS that series, not an inheritor of it);
    every other type reads its own override if an admin configured one, else
    the same shared default (is_custom False in that case)."""
    canonical_types = sorted(set(employee_validation.EMPLOYMENT_TYPES.values()))
    items = []
    for emp_type in canonical_types:
        series = crud.get_employee_id_format(db, current_user.company_id, employment_type=emp_type)
        items.append(schemas.EmployeeIdFormatItem(
            employment_type=emp_type,
            prefix=series.prefix,
            suffix=series.suffix,
            padding=series.padding,
            next_code=crud.preview_next_employee_code(series, db, current_user.company_id),
            is_custom=crud.employee_number_series_is_custom(db, current_user.company_id, emp_type),
            generation_mode=series.generation_mode or "auto",
        ))
    db.commit()  # persists any series get_employee_id_format had to auto-provision above
    return items


@router.patch(
    "/settings/employee-id-format/{employment_type}",
    response_model=schemas.EmployeeIdFormatItem,
)
def update_employee_id_format_for_type(
    employment_type: str,
    payload: schemas.EmployeeIdFormatUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    """Sets (creating, if this is the first time) a dedicated Employee ID
    format for one employment type. Full-time maps to the same shared
    default series the singular PATCH above edits -- this endpoint is a
    strict superset of it, not a separate concept."""
    canonical = employee_validation.normalize_employment_type(employment_type)
    if canonical is None:
        raise HTTPException(status_code=422, detail=f"Unknown employment type '{employment_type}'")
    series = crud.update_employee_id_format(
        db, current_user.company_id,
        prefix=payload.prefix, suffix=payload.suffix,
        padding=payload.padding, next_number=payload.next_number,
        employment_type=canonical, generation_mode=payload.generation_mode,
    )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id,
        "update", "employee_id_format", series.id,
    )
    db.commit()
    return schemas.EmployeeIdFormatItem(
        employment_type=canonical,
        prefix=series.prefix,
        suffix=series.suffix,
        padding=series.padding,
        next_code=crud.preview_next_employee_code(series, db, current_user.company_id),
        is_custom=crud.employee_number_series_is_custom(db, current_user.company_id, canonical),
        generation_mode=series.generation_mode or "auto",
    )
