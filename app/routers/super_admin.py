"""Backs the standalone HRMS Super Admin Flutter app (hrms_super_admin/),
a platform-level console distinct from anything a tenant company's own
Owner/Admin can reach. Every endpoint here is gated by
deps.require_super_admin (a public.admin_users login with role
'super_admin'), never deps.get_current_user -- there is no tenant/company
context for any of this. See crud.py's "HRMS Super Admin" section for the
underlying logic; this router is intentionally thin."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import auth_state, crud, models, provision_tenant, schemas, security
from ..database import get_db
from ..deps import require_super_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["super-admin"])


def _admin_out(tenant: models.Tenant) -> schemas.HrmsAdminOut:
    return schemas.HrmsAdminOut(
        id=tenant.id,
        slug=tenant.slug,
        company_name=tenant.name,
        contact_name=tenant.contact_name,
        contact_email=tenant.contact_email,
        contact_phone=tenant.contact_phone,
        plan_type=tenant.plan_type,
        status=tenant.status,
        trial_ends_at=tenant.trial_ends_at,
        is_active=tenant.is_active,
        created_at=tenant.created_at,
        effective_status=crud.tenant_effective_status(tenant),
    )


# SA-04: a real bcrypt hash to verify against when the email is unknown, so
# response time doesn't reveal which platform accounts exist.
_DUMMY_HASH = security.hash_password("not-a-real-password")


# ── Auth ─────────────────────────────────────────────────────────────────

@router.post("/super-admin/login", response_model=schemas.SuperAdminLoginResponse)
def super_admin_login(payload: schemas.SuperAdminLoginRequest, db: Session = Depends(get_db)):
    # SA-04: per-account lockout, same policy as tenant login (in-memory,
    # keyed apart from tenant accounts).
    lock_key = "sa:" + payload.email.strip().lower()
    remaining = auth_state.lockout_remaining_seconds(db, lock_key, None)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {max(1, remaining // 60)} minute(s).",
            headers={"Retry-After": str(remaining)},
        )
    admin = crud.find_admin_user_by_email(db, payload.email)
    is_operator = crud.is_platform_super_admin(db, admin)
    password_ok = security.verify_password(
        payload.password, admin.password_hash if is_operator else _DUMMY_HASH
    )
    # bcrypt 72-byte limit: refused (no fallback); an operator resets it with
    # `python -m app.password_admin reset-platform-admin --email ...`.
    if not password_ok and security.matches_legacy_truncated_hash(
        payload.password, admin.password_hash if is_operator else None
    ) and is_operator:
        raise HTTPException(
            status_code=403,
            detail="This password must be reset before you can sign in. Ask another platform operator to reset it.",
        )
    if not (is_operator and password_ok):
        auth_state.record_failed_login(db, lock_key, None)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    auth_state.reset_failed_logins(db, lock_key, None)
    import datetime

    admin.last_login_at = datetime.datetime.now(datetime.timezone.utc)
    crud.create_platform_audit_log(db, admin, "super_admin_login")
    db.commit()
    token = security.create_access_token(
        user_id=admin.id, roles=["super_admin"], password_hash=admin.password_hash
    )
    return schemas.SuperAdminLoginResponse(
        access_token=token,
        full_name=admin.full_name or "Super Admin",
        email=admin.email,
        role=admin.role,
    )


@router.get("/super-admin/profile", response_model=schemas.SuperAdminProfileOut)
def get_super_admin_profile(admin: models.AdminUser = Depends(require_super_admin)):
    return admin


@router.post("/super-admin/change-password")
def change_super_admin_password(
    payload: schemas.SuperAdminChangePasswordRequest,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(require_super_admin),
):
    if not security.verify_password(payload.current_password, admin.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    admin.password_hash = security.hash_password(payload.new_password)
    crud.create_platform_audit_log(db, admin, "super_admin_password_change")
    db.commit()
    # SA-03: every earlier token is now invalid; hand this device a new one.
    return {
        "success": True,
        "access_token": security.create_access_token(
            user_id=admin.id, roles=["super_admin"], password_hash=admin.password_hash
        ),
    }


@router.post("/super-admin/logout", status_code=204)
def super_admin_logout(
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(require_super_admin),
):
    """SA-03: ends this session server-side (the token's jti is revoked)."""
    token_data = db.info.get("sa_token_data")
    if token_data is not None:
        security.revoke_token(token_data.jti, token_data.expires_at)
    crud.create_platform_audit_log(db, admin, "super_admin_logout")
    db.commit()
    return Response(status_code=204)


# ── Platform operators (Super Admin's own team) ─────────────────────────────

@router.get("/super-admin/users", response_model=list[schemas.SuperAdminProfileOut])
def list_super_admin_users(
    db: Session = Depends(get_db), _admin: models.AdminUser = Depends(require_super_admin)
):
    return crud.list_super_admin_users(db)


@router.post("/super-admin/users", response_model=schemas.SuperAdminProfileOut, status_code=201)
def create_super_admin_user(
    payload: schemas.SuperAdminUserCreate,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    if crud.find_admin_user_by_email(db, payload.email) is not None:
        raise HTTPException(status_code=409, detail="Email already in use")
    created = crud.create_super_admin_user(db, payload.full_name, payload.email, payload.password)
    crud.create_platform_audit_log(db, _admin, "super_admin_create", changes={"email": created.email})
    try:
        db.commit()
    except IntegrityError as exc:  # SA-23: lost a race with the same email
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already in use") from exc
    db.refresh(created)
    return created


# ── HRMS tenant/"Admin" management ──────────────────────────────────────────

@router.get("/hrms-admins", response_model=list[schemas.HrmsAdminOut])
def list_hrms_admins(
    db: Session = Depends(get_db), _admin: models.AdminUser = Depends(require_super_admin)
):
    return [_admin_out(t) for t in crud.list_hrms_admins(db)]


@router.post("/hrms-admins", response_model=schemas.HrmsAdminOut, status_code=201)
def create_hrms_admin(
    payload: schemas.HrmsAdminCreate,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    existing = db.scalar(
        select(models.Tenant).where(models.Tenant.slug == payload.tenant_slug.strip().lower())
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="This tenant slug is already in use")
    # SA-11: the Owner's email is a global login -- say so instead of a 500.
    email = payload.contact_email.strip().lower()
    if db.scalar(
        select(models.PublicUser.id).where(func.lower(models.PublicUser.email) == email)
    ) is not None or crud.find_admin_user_by_email(db, email) is not None:
        raise HTTPException(status_code=409, detail="This email already has an HRMS login")
    try:
        tenant = crud.create_hrms_admin(db, payload)
        crud.create_platform_audit_log(
            db, _admin, "admin_create", tenant, {"plan_type": tenant.plan_type, "contact_email": email}
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except provision_tenant.ProvisioningError as exc:
        # Client-fixable (bad/duplicate slug): the message is safe to show.
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # C17: never leak raw exception text (SQL, paths) to the client --
        # provisioning is atomic, so nothing was left behind; log the detail.
        db.rollback()
        logger.exception("Tenant provisioning failed for slug=%r", payload.tenant_slug)
        raise HTTPException(
            status_code=500,
            detail="Failed to provision the tenant. Nothing was created -- please try again or contact support.",
        ) from exc
    db.refresh(tenant)
    return _admin_out(tenant)


@router.get("/hrms-admins/{tenant_id}", response_model=schemas.HrmsAdminOut)
def get_hrms_admin(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    tenant = crud.get_hrms_admin(db, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _admin_out(tenant)


@router.put("/hrms-admins/{tenant_id}", response_model=schemas.HrmsAdminOut)
def update_hrms_admin(
    tenant_id: uuid.UUID,
    payload: schemas.HrmsAdminUpdate,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    changes = payload.model_dump(exclude_unset=True)
    tenant = crud.update_hrms_admin(db, tenant_id, changes)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Not found")
    crud.create_platform_audit_log(db, _admin, "admin_update", tenant, changes)
    db.commit()
    db.refresh(tenant)
    return _admin_out(tenant)


@router.delete("/hrms-admins/{tenant_id}", status_code=204)
def delete_hrms_admin(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    # Deliberately not deleting a tenant's real schema/data from here --
    # "blocked" (toggle-status) is the safe, reversible equivalent. This
    # endpoint exists only because the reference app's service defines it;
    # it is never actually called from that app's UI either.
    raise HTTPException(
        status_code=400,
        detail="Deleting a tenant's data isn't supported here -- use toggle-status to block it instead.",
    )


@router.patch("/hrms-admins/{tenant_id}/toggle-status", response_model=schemas.HrmsAdminOut)
def toggle_hrms_admin_status(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    tenant = crud.toggle_hrms_admin_status(db, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Not found")
    crud.create_platform_audit_log(
        db, _admin, "admin_block" if tenant.is_active is False else "admin_unblock", tenant
    )
    db.commit()
    db.refresh(tenant)
    return _admin_out(tenant)


@router.post("/hrms-admins/{tenant_id}/extend-trial", response_model=schemas.HrmsAdminOut)
def extend_hrms_admin_trial(
    tenant_id: uuid.UUID,
    payload: schemas.HrmsAdminExtendTrialRequest,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    try:
        tenant = crud.extend_hrms_admin_trial(db, tenant_id, payload.days)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if tenant is None:
        raise HTTPException(status_code=404, detail="Not found")
    crud.create_platform_audit_log(
        db, _admin, "trial_extend", tenant,
        {"days": payload.days, "trial_ends_at": tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None},
    )
    db.commit()
    db.refresh(tenant)
    return _admin_out(tenant)


@router.put("/hrms-admins/{tenant_id}/plan", response_model=schemas.HrmsAdminOut)
def set_hrms_admin_plan(
    tenant_id: uuid.UUID,
    payload: schemas.HrmsAdminPlanUpdate,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    tenant = crud.set_hrms_admin_plan(db, tenant_id, payload.plan_type)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Not found")
    crud.create_platform_audit_log(db, _admin, "plan_change", tenant, {"plan_type": payload.plan_type})
    db.commit()
    db.refresh(tenant)
    return _admin_out(tenant)


@router.post("/hrms-admins/{tenant_id}/reset-password")
def reset_hrms_admin_password(
    tenant_id: uuid.UUID,
    payload: schemas.HrmsAdminPasswordResetRequest,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    tenant = crud.get_hrms_admin(db, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Not found")
    ok = crud.reset_hrms_admin_owner_password(db, tenant, payload.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="Could not find this tenant's Owner login")
    crud.create_platform_audit_log(db, _admin, "owner_password_reset", tenant)
    db.commit()
    return {"success": True}


# ── Analytics / Dashboard ────────────────────────────────────────────────

@router.get("/super-admin/analytics", response_model=schemas.SuperAdminAnalyticsOut)
def get_super_admin_analytics(
    db: Session = Depends(get_db), _admin: models.AdminUser = Depends(require_super_admin)
):
    return crud.compute_super_admin_analytics(db)


# ── Audit log ────────────────────────────────────────────────────────────

@router.get("/super-admin/audit-logs", response_model=list[schemas.PlatformAuditLogEntryOut])
def get_platform_audit_logs(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    offset = (page - 1) * limit
    return crud.list_platform_audit_logs(db, limit=limit, offset=offset)


# ── Broadcast / notifications ───────────────────────────────────────────

@router.post("/super-admin/broadcast", response_model=schemas.PlatformNotificationOut, status_code=201)
def broadcast_notification(
    payload: schemas.BroadcastRequest,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(require_super_admin),
):
    notif = crud.create_platform_notification(
        db, payload.title, payload.body, payload.target_segment, admin.id
    )
    db.commit()
    db.refresh(notif)
    # SA-07: actually reach the organizations, not just this feed.
    delivered = crud.deliver_platform_broadcast(db, payload.title, payload.body, payload.target_segment)
    crud.create_platform_audit_log(
        db, admin, "broadcast_sent", None,
        {"title": payload.title, "segment": payload.target_segment, "delivered_count": delivered},
    )
    db.commit()
    out = schemas.PlatformNotificationOut.model_validate(notif)
    out.delivered_count = delivered
    return out


@router.get("/super-admin/notifications", response_model=list[schemas.PlatformNotificationOut])
def list_super_admin_notifications(
    db: Session = Depends(get_db), _admin: models.AdminUser = Depends(require_super_admin)
):
    return crud.list_platform_notifications(db)


@router.patch("/super-admin/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    if not crud.mark_platform_notification_read(db, notification_id):
        raise HTTPException(status_code=404, detail="Not found")
    db.commit()
    return {"success": True}


@router.patch("/super-admin/notifications/read-all")
def mark_all_notifications_read(
    db: Session = Depends(get_db), _admin: models.AdminUser = Depends(require_super_admin)
):
    crud.mark_all_platform_notifications_read(db)
    db.commit()
    return {"success": True}


@router.delete("/super-admin/notifications/{notification_id}", status_code=204)
def delete_notification(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    if not crud.delete_platform_notification(db, notification_id):
        raise HTTPException(status_code=404, detail="Not found")
    db.commit()


# ── Platform settings ────────────────────────────────────────────────────

@router.get("/settings/platform", response_model=schemas.PlatformSettingsOut)
def get_platform_settings(
    db: Session = Depends(get_db), _admin: models.AdminUser = Depends(require_super_admin)
):
    row = crud.get_or_create_platform_settings(db)
    db.commit()
    return _settings_out(row)


@router.put("/settings/platform", response_model=schemas.PlatformSettingsOut)
def update_platform_settings(
    payload: schemas.PlatformSettingsUpdate,
    db: Session = Depends(get_db),
    _admin: models.AdminUser = Depends(require_super_admin),
):
    changes = payload.model_dump(exclude_unset=True)
    row = crud.update_platform_settings(db, changes)
    crud.create_platform_audit_log(
        db, _admin, "settings_update", None,
        {k: ("***" if k == "stripe_secret_key" else v) for k, v in changes.items()},
    )
    db.commit()
    db.refresh(row)
    return _settings_out(row)


def _settings_out(row: models.PlatformSettings) -> schemas.PlatformSettingsOut:
    out = schemas.PlatformSettingsOut.model_validate(row)
    out.has_stripe_secret = bool((row.stripe_secret_key or "").strip())
    return out
