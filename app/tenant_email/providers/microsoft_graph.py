"""Microsoft Graph provider: app-only Mail.Send from the TENANT's OWN Entra app
registration and mailbox (never the global Impacgo credentials)."""

from __future__ import annotations

from ... import graph_mail
from ...graph_mail import GraphConfig, GraphMailError, MailMessage
from dataclasses import replace

from .base import EmailProvider, EmailSendError, ProviderConfig, display_name


def graph_config(c: ProviderConfig) -> GraphConfig:
    return GraphConfig(
        tenant_id=c.microsoft_tenant_id, client_id=c.microsoft_client_id,
        client_secret=c.microsoft_client_secret, from_email=c.from_email,
        on_behalf=c.microsoft_on_behalf, on_behalf_name=c.microsoft_on_behalf_name,
    )


class MicrosoftGraphProvider(EmailProvider):
    def send(self, message: MailMessage) -> int:
        c = self.config
        # tenant defaults: Reply-To and the sender display name
        message = replace(
            message,
            reply_to=message.reply_to or ([c.reply_to] if c.reply_to else []),
            from_name=display_name(c, message) or None,
        )
        try:
            return graph_mail.send_mail(message, graph_config(c))
        except GraphMailError as exc:
            raise EmailSendError(exc.code, str(exc), status=exc.status, transient=exc.transient) from None
