"""Per-tenant email configuration API (public.tenant_email_settings).

Tenant admins (System Settings / RBAC Edit/Admin, or Owner) -- ALWAYS their own
tenant, resolved from the authenticated session (never from the request):
    GET    /api/admin/email-settings
    POST   /api/admin/email-settings        create (409 if one exists)
    PUT    /api/admin/email-settings        create or replace
    DELETE /api/admin/email-settings
    POST   /api/admin/email-settings/test   send a test email with the SAVED settings

Platform super admin (separate token type), explicit and authorised tenant:
    GET/PUT/DELETE /api/super-admin/tenants/{tenant_id}/email-settings

No response carries a secret (only has_* booleans) and none is logged.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import database, email_service, models, tenant_email
from ..database import get_db
from ..deps import require_permission, require_super_admin
from ..tenant_email import admin, encryption, store

router = APIRouter(prefix="/api", tags=["email-settings"])


def _own_tenant_id(db: Session) -> uuid.UUID:
    """The caller's tenant, from the session get_current_user stamped."""
    slug = database.get_session_tenant_slug(db)
    tenant_id = store.tenant_id_for_slug(db, slug) if slug else None
    if tenant_id is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return tenant_id


def _slug_of(db: Session, tenant_id: uuid.UUID) -> str | None:
    return store.slug_for_tenant_id(db, tenant_id)


def _save(db: Session, tenant_id: uuid.UUID, payload: admin.EmailSettingsIn, actor_id: uuid.UUID | None) -> admin.EmailSettingsOut:
    try:
        row = admin.save(db, tenant_id, payload, updated_by=actor_id)
    except admin.EmailSettingsError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except encryption.EncryptionConfigError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Email secret storage is not configured on the server "
                            "(TENANT_EMAIL_ENCRYPTION_KEY). Nothing was saved.") from None
    store.invalidate(_slug_of(db, tenant_id))
    return admin.to_out(row)


# ── tenant admin (own tenant only) ─────────────────────────────────────────

@router.get("/admin/email-settings", response_model=admin.EmailSettingsOut)
def get_email_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
) -> admin.EmailSettingsOut:
    return admin.to_out(store.get_row(db, _own_tenant_id(db)))


@router.post("/admin/email-settings", response_model=admin.EmailSettingsOut, status_code=201)
def create_email_settings(
    payload: admin.EmailSettingsIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
) -> admin.EmailSettingsOut:
    tenant_id = _own_tenant_id(db)
    if store.get_row(db, tenant_id) is not None:
        raise HTTPException(status_code=409, detail="Email settings already exist; use PUT to change them.")
    return _save(db, tenant_id, payload, current_user.id)


@router.put("/admin/email-settings", response_model=admin.EmailSettingsOut)
def put_email_settings(
    payload: admin.EmailSettingsIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
) -> admin.EmailSettingsOut:
    return _save(db, _own_tenant_id(db), payload, current_user.id)


@router.delete("/admin/email-settings", status_code=204)
def delete_email_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    tenant_id = _own_tenant_id(db)
    if not admin.delete(db, tenant_id):
        raise HTTPException(status_code=404, detail="No email settings to delete.")
    store.invalidate(_slug_of(db, tenant_id))


class TestRecipient(BaseModel):
    recipient: str = Field(max_length=254)


@router.post("/admin/email-settings/test")
def test_email_settings(
    payload: TestRecipient,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
) -> dict:
    """Sends a test email through the caller's tenant configuration. The
    response is credential-free whatever the provider answers."""
    from .email import _sender_name  # same display-name rule as the other admin email routes

    tenant_id = _own_tenant_id(db)
    slug = _slug_of(db, tenant_id)
    store.invalidate(slug)  # test what is saved right now, not a cached copy
    if tenant_email.load_config(slug) is None:
        raise HTTPException(status_code=422, detail={"message": "Email is not configured (or is disabled) for this organisation.",
                                                     "code": "not_configured"})
    try:
        result = email_service.send_test_email(
            db, to=payload.recipient.strip(), requested_by=_sender_name(db, current_user),
            company_id=current_user.company_id, created_by=current_user.id,
        )
    except email_service.EmailError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "code": exc.code}) from None
    if result.status == "SENT":
        return {"success": True, "message": "Test email sent successfully"}
    raise HTTPException(status_code=502, detail={
        "success": False, "message": result.error_message or "Sending the test email failed.",
        "code": result.error_code,
    })


# ── platform super admin (explicit, authorised tenant) ─────────────────────

def _tenant_or_404(db: Session, tenant_id: uuid.UUID) -> models.Tenant:
    tenant = db.get(models.Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return tenant


@router.get("/super-admin/tenants/{tenant_id}/email-settings", response_model=admin.EmailSettingsOut)
def sa_get_email_settings(tenant_id: uuid.UUID, db: Session = Depends(get_db),
                          _admin: models.AdminUser = Depends(require_super_admin)) -> admin.EmailSettingsOut:
    _tenant_or_404(db, tenant_id)
    return admin.to_out(store.get_row(db, tenant_id))


@router.put("/super-admin/tenants/{tenant_id}/email-settings", response_model=admin.EmailSettingsOut)
def sa_put_email_settings(tenant_id: uuid.UUID, payload: admin.EmailSettingsIn, db: Session = Depends(get_db),
                          platform_admin: models.AdminUser = Depends(require_super_admin)) -> admin.EmailSettingsOut:
    _tenant_or_404(db, tenant_id)
    return _save(db, tenant_id, payload, platform_admin.id)


@router.delete("/super-admin/tenants/{tenant_id}/email-settings", status_code=204)
def sa_delete_email_settings(tenant_id: uuid.UUID, db: Session = Depends(get_db),
                             _admin: models.AdminUser = Depends(require_super_admin)):
    _tenant_or_404(db, tenant_id)
    if not admin.delete(db, tenant_id):
        raise HTTPException(status_code=404, detail="No email settings to delete.")
    store.invalidate(_slug_of(db, tenant_id))
