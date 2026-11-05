"""Tenant-aware, provider-independent email service.

    tenant (slug from the authenticated session) -> public.tenant_email_settings
        -> decrypt -> provider (smtp / microsoft_graph / sendgrid / ses) -> send

The rest of the HRMS calls only this (via email_service). A message is always
sent with the configuration of the tenant it is addressed from; there is no
code path that takes a tenant from a request body.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from .. import database
from ..graph_mail import MailMessage
from . import store
from .providers import EmailSendError, ProviderConfig, get_provider

logger = logging.getLogger(__name__)


def _recipient_domains(message: MailMessage) -> str:
    return ",".join(sorted({a.rsplit("@", 1)[-1].lower() for a in message.to + message.cc + message.bcc if "@" in a}))


def send(tenant_slug: str | None, message: MailMessage, *, message_type: str = "general") -> tuple[int, ProviderConfig]:
    """Sends [message] with [tenant_slug]'s own configuration. Returns
    (provider status, config used). Raises EmailSendError (credential-free)."""
    config = store.load_config(tenant_slug)
    if config is None:
        raise EmailSendError("not_configured", "Email is not configured for this organisation.")
    try:
        status = get_provider(config).send(message)
    except EmailSendError as exc:
        logger.error("Email failed tenant=%s provider=%s recipient_domain=%s message_type=%s status=failed code=%s",
                     tenant_slug, config.provider, _recipient_domains(message), message_type, exc.code)
        raise
    except Exception as exc:  # defensive: never leak provider internals / credentials
        logger.error("Email failed tenant=%s provider=%s recipient_domain=%s message_type=%s status=failed error=%s",
                     tenant_slug, config.provider, _recipient_domains(message), message_type, type(exc).__name__)
        raise EmailSendError("send_failed", f"Sending failed ({type(exc).__name__}).") from None
    logger.info("Email sent tenant=%s provider=%s recipient_domain=%s message_type=%s status=success",
                tenant_slug, config.provider, _recipient_domains(message), message_type)
    return status, config


class EmailService:
    """Object form of the same thing, keyed by tenant id (UUID)."""

    async def send_email(self, tenant_id: uuid.UUID, to, subject: str, body: str, html: bool = True,
                         reply_to=None, *, message_type: str = "general") -> int:
        def run() -> int:
            db = database.SessionLocal()
            try:
                slug = store.slug_for_tenant_id(db, tenant_id)
            finally:
                db.close()
            if slug is None:
                raise EmailSendError("not_configured", "Unknown tenant.")
            recipients = [to] if isinstance(to, str) else list(to)
            reply = [reply_to] if isinstance(reply_to, str) else list(reply_to or [])
            message = MailMessage(to=recipients, subject=subject, reply_to=reply,
                                  html_body=body if html else None, text_body=None if html else body)
            return send(slug, message, message_type=message_type)[0]

        return await asyncio.to_thread(run)


email_service = EmailService()
