"""Amazon SES provider (SESv2 SendEmail API, SigV4-signed, standard library
only). For SES *SMTP* credentials use provider 'smtp' instead."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import urllib.error
import urllib.request

from ...graph_mail import MailMessage
from .base import EmailProvider, EmailSendError, build_mime


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(*, access_key: str, secret_key: str, region: str, host: str, path: str,
                  body: bytes, now: datetime.datetime | None = None, service: str = "ses") -> dict[str, str]:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    canonical = "\n".join([
        "POST", path, "", f"content-type:application/json\nhost:{host}\nx-amz-date:{amz_date}\n",
        "content-type;host;x-amz-date", hashlib.sha256(body).hexdigest(),
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json", "X-Amz-Date": amz_date,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                         f"SignedHeaders=content-type;host;x-amz-date, Signature={signature}",
    }


class SesProvider(EmailProvider):
    def send(self, message: MailMessage) -> int:
        c = self.config
        if not (c.ses_region and c.ses_access_key_id and c.ses_secret_access_key and c.from_email):
            raise EmailSendError("not_configured", "SES region, access key, secret key and from email are required.")
        host = f"email.{c.ses_region}.amazonaws.com"
        path = "/v2/email/outbound-emails"
        body = json.dumps({
            "Destination": {"ToAddresses": message.to, "CcAddresses": message.cc, "BccAddresses": message.bcc},
            "Content": {"Raw": {"Data": base64.b64encode(build_mime(c, message).as_bytes()).decode("ascii")}},
        }).encode("utf-8")
        headers = sigv4_headers(access_key=c.ses_access_key_id, secret_key=c.ses_secret_access_key,
                                region=c.ses_region, host=host, path=path, body=body)
        req = urllib.request.Request(f"https://{host}{path}", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status in (401, 403):
                raise EmailSendError("auth_failed", "Amazon SES rejected the credentials, or the IAM user lacks "
                                     "ses:SendEmail (also check the from address/domain is verified).", status=status) from None
            if status == 429 or status >= 500:
                raise EmailSendError("throttled" if status == 429 else "service_unavailable",
                                     "Amazon SES is throttling or unavailable; will retry.", status=status, transient=True) from None
            raise EmailSendError("send_failed", f"Amazon SES rejected the message (HTTP {status}).", status=status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise EmailSendError("network_error", "Could not reach Amazon SES.", transient=True) from None
