"""SMTP provider (Microsoft 365, Google Workspace, Amazon SES SMTP, SendGrid
SMTP, Mailgun, any standard server). The password lives only in the in-memory
ProviderConfig and is used solely in server.login()."""

from __future__ import annotations

import smtplib
import socket
import ssl

from ...graph_mail import MailMessage
from .base import EmailProvider, EmailSendError, build_mime


class SmtpProvider(EmailProvider):
    def send(self, message: MailMessage) -> int:
        c = self.config
        if not c.smtp_host or not c.from_email:
            raise EmailSendError("not_configured", "SMTP host and from email are required.")
        if c.smtp_username and not (c.smtp_use_tls or c.smtp_use_ssl):
            # never put a password on a plaintext connection
            raise EmailSendError("insecure_smtp", "Enable TLS or SSL: SMTP credentials are not sent over a plaintext connection.")
        mime = build_mime(c, message)
        recipients = message.to + message.cc + message.bcc
        try:
            if c.smtp_use_ssl:
                server = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, context=ssl.create_default_context(), timeout=15)
            else:
                server = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15)
            with server:
                if c.smtp_use_tls and not c.smtp_use_ssl:
                    server.starttls(context=ssl.create_default_context())
                if c.smtp_username:
                    server.login(c.smtp_username, c.smtp_password)
                server.send_message(mime, to_addrs=recipients)
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailSendError(
                "auth_failed",
                "SMTP authentication failed: check the username and password (Microsoft 365 / Gmail may "
                "need an app password and SMTP AUTH enabled).", status=exc.smtp_code) from None
        except smtplib.SMTPRecipientsRefused:
            raise EmailSendError("invalid_recipient", "The SMTP server refused the recipient address.") from None
        except smtplib.SMTPSenderRefused:
            raise EmailSendError("sender_refused", "The SMTP server refused the from address (it must be allowed for this account).") from None
        except (socket.timeout, TimeoutError):
            raise EmailSendError("timeout", "The SMTP server did not respond in time.", transient=True) from None
        except ssl.SSLError:
            raise EmailSendError("tls_error", "TLS/SSL negotiation with the SMTP server failed (check port and TLS/SSL settings).") from None
        except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError):
            raise EmailSendError("network_error", "Could not connect to the SMTP server (check host, port and TLS/SSL).", transient=True) from None
        except smtplib.SMTPException as exc:
            raise EmailSendError("send_failed", f"SMTP error ({type(exc).__name__}).") from None
        return 250
