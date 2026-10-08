"""Loads / saves public.tenant_email_settings and turns a row into a decrypted,
in-memory ProviderConfig.

Tenant resolution: callers pass the tenant SLUG that auth already put on the
request/session (database.get_session_tenant_slug) -- never a client-supplied
value. Decrypted configs are cached in process memory for a short TTL (never
persisted, never logged); a save/delete invalidates the cache of this
process, other workers pick the change up within CONFIG_TTL_SECONDS.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from .. import database, graph_mail, models
from ..config import settings
from . import encryption
from .providers import ProviderConfig

logger = logging.getLogger(__name__)

CONFIG_TTL_SECONDS = 30
_NONE = object()  # cached "no usable configuration"
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def invalidate(tenant_slug: str | None = None) -> None:
    with _lock:
        if tenant_slug is None:
            _cache.clear()
        else:
            _cache.pop(tenant_slug, None)


def _public_session() -> Session:
    return database.SessionLocal()  # no tenant slug: public tables only


def tenant_id_for_slug(db: Session, slug: str) -> uuid.UUID | None:
    return db.scalar(select(models.Tenant.id).where(models.Tenant.slug == slug))


def slug_for_tenant_id(db: Session, tenant_id: uuid.UUID) -> str | None:
    return db.scalar(select(models.Tenant.slug).where(models.Tenant.id == tenant_id))


def get_row(db: Session, tenant_id: uuid.UUID) -> models.TenantEmailSettings | None:
    return db.scalar(select(models.TenantEmailSettings).where(models.TenantEmailSettings.tenant_id == tenant_id))


def _decrypt(value: str | None) -> str:
    return encryption.decrypt_secret(value) if value else ""


def config_from_row(row: models.TenantEmailSettings) -> ProviderConfig:
    """Decrypts [row]'s secrets (only those the provider needs)."""
    common = dict(provider=row.provider, from_email=row.from_email or "", from_name=row.from_name or "",
                  reply_to=row.reply_to or "", source="tenant")
    if row.provider == "smtp":
        return ProviderConfig(
            **common, smtp_host=row.smtp_host or "", smtp_port=row.smtp_port or 587,
            smtp_username=row.smtp_username or "", smtp_password=_decrypt(row.smtp_password_encrypted),
            smtp_use_tls=bool(row.smtp_use_tls), smtp_use_ssl=bool(row.smtp_use_ssl),
        )
    if row.provider == "microsoft_graph":
        return ProviderConfig(
            **common, microsoft_tenant_id=row.microsoft_tenant_id or "",
            microsoft_client_id=row.microsoft_client_id or "",
            microsoft_client_secret=_decrypt(row.microsoft_client_secret_encrypted),
        )
    if row.provider == "sendgrid":
        return ProviderConfig(**common, sendgrid_api_key=_decrypt(row.sendgrid_api_key_encrypted))
    if row.provider == "ses":
        return ProviderConfig(
            **common, ses_region=row.ses_region or "", ses_access_key_id=row.ses_access_key_id or "",
            ses_secret_access_key=_decrypt(row.ses_secret_access_key_encrypted),
        )
    raise ValueError("unsupported provider")


def global_fallback_allowed(tenant_slug: str) -> bool:
    raw = (settings.email_global_fallback_tenants or "").strip()
    if not raw:
        return False
    allowed = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return "*" in allowed or tenant_slug.lower() in allowed


def global_fallback_config() -> ProviderConfig | None:
    """The legacy .env transport (Graph preferred, else SMTP), or None."""
    if graph_mail.is_configured():
        g = graph_mail.global_config()
        return ProviderConfig(
            provider="microsoft_graph", from_email=g.from_email.strip(),
            from_name=settings.email_from_name, reply_to=settings.email_reply_to, source="global_fallback",
            microsoft_tenant_id=g.tenant_id, microsoft_client_id=g.client_id,
            microsoft_client_secret=g.client_secret, microsoft_on_behalf=g.on_behalf,
            microsoft_on_behalf_name=g.on_behalf_name,
        )
    if settings.smtp_host and settings.email_from_address:
        return ProviderConfig(
            provider="smtp", from_email=settings.email_from_address, from_name=settings.email_from_name,
            reply_to=settings.email_reply_to, source="global_fallback",
            smtp_host=settings.smtp_host, smtp_port=settings.smtp_port, smtp_username=settings.smtp_username,
            smtp_password=settings.smtp_password, smtp_use_tls=settings.smtp_use_tls,
            smtp_use_ssl=settings.smtp_use_ssl,
        )
    return None


def load_config(tenant_slug: str | None) -> ProviderConfig | None:
    """The sending configuration of [tenant_slug], or None when the tenant
    cannot send email (no row and no allowed fallback, row disabled, or the
    row cannot be decrypted). Never raises; never returns another tenant's
    configuration."""
    if not settings.email_enabled:
        return None
    if not tenant_slug:
        # No tenant context (scripts / legacy callers): only the explicit
        # EMAIL_GLOBAL_FALLBACK_TENANTS=* opt-in may use the global transport.
        return global_fallback_config() if global_fallback_allowed("") else None
    now = time.monotonic()
    with _lock:
        hit = _cache.get(tenant_slug)
        if hit and hit[0] > now:
            return None if hit[1] is _NONE else hit[1]  # type: ignore[return-value]
    config: ProviderConfig | None = None
    try:
        db = _public_session()
        try:
            tenant_id = tenant_id_for_slug(db, tenant_slug)
            try:
                row = get_row(db, tenant_id) if tenant_id else None
            except ProgrammingError:
                # Migration not run yet (public.tenant_email_settings missing): behave as
                # "no settings" so the explicit fallback keeps working during the transition.
                db.rollback()
                row = None
                logger.warning("public.tenant_email_settings is missing -- run python -m app.migrate_tenant_email --execute")
        finally:
            db.close()
        if row is not None:
            config = config_from_row(row) if row.is_enabled else None
        elif global_fallback_allowed(tenant_slug):
            config = global_fallback_config()
    except encryption.EncryptionConfigError as exc:
        logger.error("Tenant email: tenant=%s configuration unusable: %s", tenant_slug, exc)
    except Exception:
        logger.exception("Tenant email: could not load configuration for tenant=%s", tenant_slug)
        return None  # transient (DB) problem: do not cache
    with _lock:
        _cache[tenant_slug] = (now + CONFIG_TTL_SECONDS, config if config is not None else _NONE)
    return config
