"""SendGrid Web API v3 provider (API key; for SendGrid SMTP use provider 'smtp')."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from ...graph_mail import MailMessage
from .base import EmailProvider, EmailSendError, display_name

_URL = "https://api.sendgrid.com/v3/mail/send"


def build_payload(config, message: MailMessage) -> dict:
    def addrs(items):
        return [{"email": a} for a in items]

    personalization: dict = {"to": addrs(message.to)}
    if message.cc:
        personalization["cc"] = addrs(message.cc)
    if message.bcc:
        personalization["bcc"] = addrs(message.bcc)
    content = []
    if message.text_body:
        content.append({"type": "text/plain", "value": message.text_body})
    if message.html_body is not None:
        content.append({"type": "text/html", "value": message.html_body})
    if not content:
        content.append({"type": "text/plain", "value": " "})
    name = display_name(config, message)
    payload: dict = {
        "personalizations": [personalization], "subject": message.subject, "content": content,
        "from": {"email": config.from_email, **({"name": name} if name else {})},
    }
    reply_to = message.reply_to or ([config.reply_to] if config.reply_to else [])
    if reply_to:
        payload["reply_to"] = {"email": reply_to[0]}
    if message.attachments:
        payload["attachments"] = [
            {"content": base64.b64encode(a.content).decode("ascii"), "filename": a.name, "type": a.content_type}
            for a in message.attachments
        ]
    return payload


class SendGridProvider(EmailProvider):
    def send(self, message: MailMessage) -> int:
        c = self.config
        if not c.sendgrid_api_key or not c.from_email:
            raise EmailSendError("not_configured", "SendGrid API key and from email are required.")
        req = urllib.request.Request(
            _URL, data=json.dumps(build_payload(c, message)).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {c.sendgrid_api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status in (401, 403):
                raise EmailSendError("auth_failed", "SendGrid rejected the API key (needs Mail Send permission, and "
                                     "the from address must be a verified sender).", status=status) from None
            if status == 429 or status >= 500:
                raise EmailSendError("throttled" if status == 429 else "service_unavailable",
                                     "SendGrid is busy or unavailable; will retry.", status=status, transient=True) from None
            raise EmailSendError("send_failed", f"SendGrid rejected the message (HTTP {status}).", status=status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise EmailSendError("network_error", "Could not reach SendGrid.", transient=True) from None
