"""Per-tenant email configuration (public.tenant_email_settings): encryption,
providers, config store and the tenant-aware send service. See
docs/TENANT_EMAIL_SETTINGS.md."""

from .providers import PROVIDERS, EmailSendError, ProviderConfig
from .service import EmailService, email_service, send
from .store import invalidate, load_config

__all__ = ["PROVIDERS", "EmailSendError", "ProviderConfig", "EmailService", "email_service",
           "send", "invalidate", "load_config"]
