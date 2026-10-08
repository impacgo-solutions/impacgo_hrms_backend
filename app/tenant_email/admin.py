"""Schemas + save/read/delete logic for public.tenant_email_settings.

Used by routers/email_settings.py (the caller's own tenant) and the super-admin
routes (an explicitly selected, authorised tenant). Nothing here accepts a
tenant from a request body, and no response ever carries a secret: secrets are
write-only, reads expose only has_* booleans.
"""

from __future__ import annotations

import datetime
import logging
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import models
from . import encryption, store
from .providers import PROVIDERS

logger = logging.getLogger(__name__)

Provider = Literal["smtp", "microsoft_graph", "sendgrid", "ses"]

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_SECRET_FIELDS = {
    "smtp": ("smtp_password", "smtp_password_encrypted", "SMTP password"),
    "microsoft_graph": ("microsoft_client_secret", "microsoft_client_secret_encrypted", "Microsoft client secret"),
    "sendgrid": ("sendgrid_api_key", "sendgrid_api_key_encrypted", "SendGrid API key"),
    "ses": ("ses_secret_access_key", "ses_secret_access_key_encrypted", "AWS secret access key"),
}


class EmailSettingsIn(BaseModel):
    """Write model. A secret left blank / omitted KEEPS the stored one."""
    model_config = ConfigDict(extra="forbid")  # a stray tenant_id is rejected, not ignored

    provider: Provider
    from_email: str = Field(max_length=254)
    from_name: str | None = Field(default=None, max_length=255)
    reply_to: str | None = Field(default=None, max_length=254)
    is_enabled: bool = True
    # smtp
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = None
    smtp_username: str | None = Field(default=None, max_length=255)
    smtp_password: str | None = Field(default=None, max_length=1000, repr=False)
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    # microsoft_graph
    microsoft_tenant_id: str | None = Field(default=None, max_length=100)
    microsoft_client_id: str | None = Field(default=None, max_length=100)
    microsoft_client_secret: str | None = Field(default=None, max_length=1000, repr=False)
    # sendgrid
    sendgrid_api_key: str | None = Field(default=None, max_length=1000, repr=False)
    # ses
    ses_region: str | None = Field(default=None, max_length=30)
    ses_access_key_id: str | None = Field(default=None, max_length=128)
    ses_secret_access_key: str | None = Field(default=None, max_length=1000, repr=False)


class EmailSettingsOut(BaseModel):
    """Read model: never contains a secret, only whether one is stored."""
    configured: bool
    provider: str | None = None
    from_email: str | None = None
    from_name: str | None = None
    reply_to: str | None = None
    is_enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_use_tls: bool | None = None
    smtp_use_ssl: bool | None = None
    has_password: bool = False
    microsoft_tenant_id: str | None = None
    microsoft_client_id: str | None = None
    has_client_secret: bool = False
    has_api_key: bool = False
    ses_region: str | None = None
    ses_access_key_id: str | None = None
    has_ses_secret: bool = False
    updated_at: datetime.datetime | None = None


class EmailSettingsError(ValueError):
    """Validation problem, safe to show to the admin (never contains secrets)."""


def to_out(row: models.TenantEmailSettings | None) -> EmailSettingsOut:
    if row is None:
        return EmailSettingsOut(configured=False)
    return EmailSettingsOut(
        configured=True, provider=row.provider, from_email=row.from_email, from_name=row.from_name,
        reply_to=row.reply_to, is_enabled=row.is_enabled, smtp_host=row.smtp_host, smtp_port=row.smtp_port,
        smtp_username=row.smtp_username, smtp_use_tls=row.smtp_use_tls, smtp_use_ssl=row.smtp_use_ssl,
        has_password=bool(row.smtp_password_encrypted), microsoft_tenant_id=row.microsoft_tenant_id,
        microsoft_client_id=row.microsoft_client_id, has_client_secret=bool(row.microsoft_client_secret_encrypted),
        has_api_key=bool(row.sendgrid_api_key_encrypted), ses_region=row.ses_region,
        ses_access_key_id=row.ses_access_key_id, has_ses_secret=bool(row.ses_secret_access_key_encrypted),
        updated_at=row.updated_at,
    )


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def validate(payload: EmailSettingsIn, existing: models.TenantEmailSettings | None) -> None:
    """Provider-specific required fields and formats. Raises EmailSettingsError."""
    if payload.provider not in PROVIDERS:
        raise EmailSettingsError("Unsupported email provider.")
    if not _EMAIL_RE.match((payload.from_email or "").strip()):
        raise EmailSettingsError("From email is not a valid email address.")
    if _clean(payload.reply_to) and not _EMAIL_RE.match(payload.reply_to.strip()):
        raise EmailSettingsError("Reply-To is not a valid email address.")
    secret_attr, column, label = _SECRET_FIELDS[payload.provider]
    has_new = bool(_clean(getattr(payload, secret_attr)))
    keeps_old = existing is not None and existing.provider == payload.provider and bool(getattr(existing, column))
    if payload.provider == "smtp":
        host = (payload.smtp_host or "").strip()
        if not host or not _HOST_RE.match(host):
            raise EmailSettingsError("SMTP host is required and must be a valid host name.")
        if payload.smtp_port is None or not 1 <= payload.smtp_port <= 65535:
            raise EmailSettingsError("SMTP port must be between 1 and 65535.")
        if payload.smtp_use_tls and payload.smtp_use_ssl:
            raise EmailSettingsError("Choose either TLS (STARTTLS) or SSL, not both.")
        username = _clean(payload.smtp_username)
        if username and not (payload.smtp_use_tls or payload.smtp_use_ssl):
            raise EmailSettingsError("Enable TLS or SSL: credentials are not sent over a plaintext connection.")
        if username and not (has_new or keeps_old):
            raise EmailSettingsError("SMTP password is required.")
    elif payload.provider == "microsoft_graph":
        for name, value in (("Microsoft tenant ID", payload.microsoft_tenant_id), ("Microsoft client ID", payload.microsoft_client_id)):
            if not value or not _GUID_RE.match(value.strip()):
                raise EmailSettingsError(f"{name} is required and must be a GUID.")
        if not (has_new or keeps_old):
            raise EmailSettingsError(f"{label} is required.")
    elif payload.provider == "sendgrid":
        if not (has_new or keeps_old):
            raise EmailSettingsError(f"{label} is required.")
    elif payload.provider == "ses":
        if not payload.ses_region or not _REGION_RE.match(payload.ses_region.strip()):
            raise EmailSettingsError("AWS region is required (for example us-east-1).")
        if not _clean(payload.ses_access_key_id):
            raise EmailSettingsError("AWS access key ID is required.")
        if not (has_new or keeps_old):
            raise EmailSettingsError(f"{label} is required.")


def save(db: Session, tenant_id: uuid.UUID, payload: EmailSettingsIn, *, updated_by: uuid.UUID | None) -> models.TenantEmailSettings:
    """Validates, encrypts and upserts the tenant's row. Blank secret = keep.
    Fields of other providers are cleared so no stale credential lingers.
    Commits [db]. Raises EmailSettingsError / encryption.EncryptionConfigError."""
    row = store.get_row(db, tenant_id)
    validate(payload, row)
    secret_attr, column, _label = _SECRET_FIELDS[payload.provider]
    new_secret = _clean(getattr(payload, secret_attr))
    encrypted = encryption.encrypt_secret(new_secret) if new_secret else None  # fails closed without a key
    if row is None:
        row = models.TenantEmailSettings(id=uuid.uuid4(), tenant_id=tenant_id)
        db.add(row)
    same_provider = row.provider == payload.provider
    kept = getattr(row, column) if same_provider else None
    # wipe every provider-specific column, then fill the chosen provider's
    for _attr, col, _l in _SECRET_FIELDS.values():
        setattr(row, col, None)
    for col in ("smtp_host", "smtp_username", "microsoft_tenant_id", "microsoft_client_id", "ses_region", "ses_access_key_id"):
        setattr(row, col, None)
    row.provider = payload.provider
    row.from_email = payload.from_email.strip()
    row.from_name = _clean(payload.from_name)
    row.reply_to = _clean(payload.reply_to)
    row.is_enabled = payload.is_enabled
    row.smtp_port = 587
    row.smtp_use_tls, row.smtp_use_ssl = True, False
    if payload.provider == "smtp":
        row.smtp_host = payload.smtp_host.strip()
        row.smtp_port = payload.smtp_port
        row.smtp_username = _clean(payload.smtp_username)
        row.smtp_use_tls, row.smtp_use_ssl = payload.smtp_use_tls, payload.smtp_use_ssl
    elif payload.provider == "microsoft_graph":
        row.microsoft_tenant_id = payload.microsoft_tenant_id.strip()
        row.microsoft_client_id = payload.microsoft_client_id.strip()
    elif payload.provider == "ses":
        row.ses_region = payload.ses_region.strip()
        row.ses_access_key_id = payload.ses_access_key_id.strip()
    setattr(row, column, encrypted or kept)
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    row.updated_by = updated_by
    db.commit()
    logger.info("Tenant email settings saved: tenant_id=%s provider=%s enabled=%s", tenant_id, row.provider, row.is_enabled)
    return row


def delete(db: Session, tenant_id: uuid.UUID) -> bool:
    row = store.get_row(db, tenant_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    logger.info("Tenant email settings deleted: tenant_id=%s", tenant_id)
    return True
