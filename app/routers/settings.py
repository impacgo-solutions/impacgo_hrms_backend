from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user, require_people_access, require_permission

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
    settings_row = crud.upsert_company_settings(
        db, company.id, payload.model_dump(exclude_unset=True)
    )
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
        next_code=crud.preview_next_employee_code(series),
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
        next_code=crud.preview_next_employee_code(series),
    )
