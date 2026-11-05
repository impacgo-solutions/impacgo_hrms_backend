from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, notification_settings, schemas
from ..database import get_db
from ..deps import get_current_user, require_permission

router = APIRouter(prefix="/api", tags=["notification-settings"])


@router.get("/notification-settings", response_model=list[schemas.NotificationSettingItem])
def list_notification_settings(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return notification_settings.get_settings(db, current_user.company_id)


@router.put("/notification-settings", response_model=schemas.NotificationSettingItem)
def update_notification_setting(
    payload: schemas.NotificationSettingUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_permission("system_settings_rbac")),
):
    try:
        item = notification_settings.set_setting(
            db,
            current_user.company_id,
            payload.event_key,
            payload.channel,
            payload.enabled,
            current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "notification_setting", current_user.company_id,
    )
    db.commit()
    return item
