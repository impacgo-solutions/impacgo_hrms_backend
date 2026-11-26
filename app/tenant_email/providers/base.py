"""Provider-independent pieces: the resolved per-tenant configuration, the
error type and the provider interface. The rest of the HRMS never touches a
provider directly -- it calls tenant_email.service / email_service."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr

from ...graph_mail import MailMessage

PROVIDERS = ("smtp", "microsoft_graph", "sendgrid", "ses")


class EmailSendError(Exception):
    """A classified, credential-free delivery failure. [code] is stable for
    callers / the email log; str(self) is safe to show to an admin."""

    def __init__(self, code: str, message: str, *, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.transient = transient


@dataclass(frozen=True)
class ProviderConfig:
    """One tenant's decrypted sending configuration. Held in memory only
    (never logged or serialised): every secret field is repr=False."""
    provider: str
    from_email: str = ""
    from_name: str = ""
    reply_to: str = ""
    source: str = "tenant"  # "tenant" | "global_fallback"
    # smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    # microsoft_graph
    microsoft_tenant_id: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = field(default="", repr=False)
    microsoft_on_behalf: str = ""
    microsoft_on_behalf_name: str = ""
    # sendgrid
    sendgrid_api_key: str = field(default="", repr=False)
    # ses
    ses_region: str = ""
    ses_access_key_id: str = ""
    ses_secret_access_key: str = field(default="", repr=False)

    @property
    def transport_label(self) -> str:
        """Short value for core_email_logs.transport (varchar(10))."""
        return {"microsoft_graph": "graph"}.get(self.provider, self.provider)


class EmailProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    def send(self, message: MailMessage) -> int:
        """Delivers [message]; returns the provider's status code. Raises EmailSendError."""


def display_name(config: ProviderConfig, message: MailMessage) -> str:
    if message.from_name and config.from_name:
        return f"{message.from_name} via {config.from_name}"
    return message.from_name or config.from_name or ""


def build_mime(config: ProviderConfig, message: MailMessage, *, include_bcc: bool = False) -> EmailMessage:
    mime = EmailMessage()
    mime["Subject"] = message.subject
    mime["From"] = formataddr((display_name(config, message), config.from_email))
    mime["To"] = ", ".join(message.to)
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    if include_bcc and message.bcc:
        mime["Bcc"] = ", ".join(message.bcc)
    reply_to = message.reply_to or ([config.reply_to] if config.reply_to else [])
    if reply_to:
        mime["Reply-To"] = ", ".join(reply_to)
    mime.set_content(message.text_body or "This email requires an HTML-capable client.")
    if message.html_body is not None:
        mime.add_alternative(message.html_body, subtype="html")
    for a in message.attachments:
        maintype, _, subtype = a.content_type.partition("/")
        mime.add_attachment(a.content, maintype=maintype, subtype=subtype or "octet-stream", filename=a.name)
    return mime
