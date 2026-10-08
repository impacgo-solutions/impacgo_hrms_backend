"""Microsoft Graph mail client -- the ONE place the HRMS talks to Microsoft
Entra ID / Microsoft Graph (email_service.py is its only caller).

* Authentication: OAuth 2.0 client-credentials flow against
  https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token with
  scope=https://graph.microsoft.com/.default -- an app-only token, so the
  app registration needs exactly one Microsoft Graph APPLICATION permission:
  Mail.Send (admin-consented). No delegated auth, no user passwords.
* The token is cached in process memory only (never persisted) and
  refreshed 5 minutes before it expires, or immediately after Graph rejects
  it with 401.
* Sending: POST https://graph.microsoft.com/v1.0/users/{from}/sendMail with
  file attachments inline (#microsoft.graph.fileAttachment). Graph caps
  that request at ~4 MB, so callers limit attachments (see
  settings.email_max_attachment_bytes); larger files would need an upload
  session, which requires Mail.ReadWrite -- deliberately not requested.
* Transient failures (429 throttling honouring Retry-After, 500/502/503/504,
  timeouts, connection errors) are retried with backoff; everything else
  fails fast with a GraphMailError whose `code` says what is wrong.
* Standard library only (no new dependency); one keep-alive HTTPS
  connection per worker thread and host.

Security: the client secret, access tokens and Authorization headers are
never logged, never put in exception messages and never returned by any
API. Error messages are built from Microsoft's error *codes*, not echoed
response bodies.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import random
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from .config import settings

logger = logging.getLogger(__name__)

_LOGIN_HOST = "login.microsoftonline.com"
_GRAPH_HOST = "graph.microsoft.com"
_SCOPE = "https://graph.microsoft.com/.default"
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_REFRESH_MARGIN_SECONDS = 300


class GraphMailError(Exception):
    """A classified, credential-free failure. [code] is stable for callers /
    the email log; str(self) is a safe, human-readable explanation."""

    def __init__(self, code: str, message: str, *, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.transient = transient


@dataclass
class Attachment:
    name: str
    content: bytes
    content_type: str = "application/pdf"


@dataclass
class MailMessage:
    to: list[str]
    subject: str
    html_body: str | None = None
    text_body: str | None = None
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    # Friendly display name shown for the sender (the mailbox itself never
    # changes -- Exchange may still show the mailbox's own display name).
    from_name: str | None = None


# ── configuration ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GraphConfig:
    """One Microsoft Graph mailbox configuration. Per-tenant configs are built
    from public.tenant_email_settings (secret already decrypted, held in
    memory only); global_config() is the legacy MICROSOFT_* .env fallback.
    repr() hides the secret."""
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    from_email: str = ""
    on_behalf: str = ""
    on_behalf_name: str = ""

    @property
    def missing(self) -> list[str]:
        return [n for n, v in (("tenant_id", self.tenant_id), ("client_id", self.client_id),
                               ("client_secret", self.client_secret), ("from_email", self.from_email))
                if not (v or "").strip()]


def global_config() -> GraphConfig:
    return GraphConfig(
        tenant_id=settings.microsoft_tenant_id, client_id=settings.microsoft_client_id,
        client_secret=settings.microsoft_client_secret, from_email=settings.microsoft_from_email,
        on_behalf=settings.microsoft_on_behalf_of or "",
        on_behalf_name=settings.microsoft_on_behalf_of_name or "",
    )


def is_configured(cfg: GraphConfig | None = None) -> bool:
    return not (cfg or global_config()).missing


def sender_address(cfg: GraphConfig | None = None) -> str:
    return (cfg or global_config()).from_email.strip()


def on_behalf_address(cfg: GraphConfig | None = None) -> str:
    """The on-behalf-of address, or "" when off / same as the sender."""
    cfg = cfg or global_config()
    address = (cfg.on_behalf or "").strip()
    return address if address and address.lower() != sender_address(cfg).lower() else ""


# ── HTTP (keep-alive connection per thread + host) ──────────────────────────

_local = threading.local()


def _connection(host: str) -> http.client.HTTPSConnection:
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    conn = conns.get(host)
    if conn is None:
        conn = http.client.HTTPSConnection(
            host, timeout=settings.microsoft_graph_timeout_seconds,
            context=ssl.create_default_context(),
        )
        conns[host] = conn
    return conn


def _drop_connection(host: str) -> None:
    conn = getattr(_local, "conns", {}).pop(host, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


def _request(host: str, method: str, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    """One HTTPS round trip on the thread's pooled connection; reconnects once
    if the server closed an idle keep-alive connection."""
    for attempt in (1, 2):
        conn = _connection(host)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        except (http.client.RemoteDisconnected, http.client.CannotSendRequest, BrokenPipeError, ConnectionResetError):
            _drop_connection(host)
            if attempt == 2:
                raise
        except Exception:
            _drop_connection(host)
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


# ── token (client credentials, cached in memory) ────────────────────────────

_token_lock = threading.Lock()
# One cached token per (Entra tenant, client, secret): HRMS tenants never share
# a token, and rotating a secret naturally invalidates the old entry.
_tokens: dict[tuple[str, str, int], dict[str, object]] = {}


def _config_key(cfg: GraphConfig) -> tuple[str, str, int]:
    return (cfg.tenant_id.strip(), cfg.client_id.strip(), hash(cfg.client_secret))


def invalidate_token(cfg: GraphConfig | None = None) -> None:
    with _token_lock:
        _tokens.pop(_config_key(cfg or global_config()), None)


def _aad_error(status: int, data: bytes) -> GraphMailError:
    """Maps Microsoft Entra ID token errors to clear, secret-free messages."""
    try:
        payload = json.loads(data or b"{}")
    except ValueError:
        payload = {}
    description = str(payload.get("error_description") or "")
    error = str(payload.get("error") or "")
    codes = {int(c) for c in payload.get("error_codes") or [] if str(c).isdigit()}
    aadsts = next((w.rstrip(":") for w in description.split() if w.startswith("AADSTS")), "")

    def has(code: int) -> bool:
        return code in codes or aadsts == f"AADSTS{code}"

    if has(7000215):
        return GraphMailError("invalid_client_secret", "Microsoft rejected the client secret. MICROSOFT_CLIENT_SECRET must be the secret's VALUE (not the Secret ID) of this app registration.", status=status)
    if has(7000222):
        return GraphMailError("expired_client_secret", "The client secret has expired. Create a new client secret in Microsoft Entra and update MICROSOFT_CLIENT_SECRET.", status=status)
    if has(700016):
        return GraphMailError("invalid_client_id", "No application with MICROSOFT_CLIENT_ID was found in this tenant. Check the Application (client) ID and MICROSOFT_TENANT_ID.", status=status)
    if has(90002) or has(900023) or has(90013):
        return GraphMailError("invalid_tenant_id", "MICROSOFT_TENANT_ID is not a valid Microsoft Entra tenant. Use the Directory (tenant) ID of your organization.", status=status)
    if has(700024) or has(7000112) or has(700027):
        return GraphMailError("auth_failed", "The app registration is disabled or its credentials are not valid.", status=status)
    if error == "unauthorized_client" or has(700013):
        return GraphMailError("auth_failed", "The application is not allowed to use the client-credentials flow in this tenant.", status=status)
    if status in _TRANSIENT_STATUS:
        return GraphMailError("service_unavailable", "Microsoft sign-in is temporarily unavailable.", status=status, transient=True)
    return GraphMailError("auth_failed", f"Microsoft Entra ID authentication failed ({aadsts or error or status}).", status=status)


def _fetch_token(cfg: GraphConfig) -> tuple[str, float]:
    tenant = urllib.parse.quote(cfg.tenant_id.strip(), safe="")
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cfg.client_id.strip(),
        "client_secret": cfg.client_secret,
        "scope": _SCOPE,
    }).encode()
    status, _headers, data = _request(
        _LOGIN_HOST, "POST", f"/{tenant}/oauth2/v2.0/token", form,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    if status != 200:
        raise _aad_error(status, data)
    payload = json.loads(data)
    token = payload.get("access_token")
    if not token:
        raise GraphMailError("auth_failed", "Microsoft Entra ID returned no access token.", status=status)
    return str(token), time.time() + float(payload.get("expires_in") or 3599)


def get_access_token(force_refresh: bool = False, cfg: GraphConfig | None = None) -> str:
    """App-only Graph token, reused until 5 minutes before it expires."""
    cfg = cfg or global_config()
    if not is_configured(cfg):
        raise GraphMailError("not_configured", "Microsoft Graph email configuration is incomplete.")
    key = _config_key(cfg)
    with _token_lock:
        cached = _tokens.get(key)
        if (
            not force_refresh
            and cached
            and float(cached["expires_at"]) - _REFRESH_MARGIN_SECONDS > time.time()
        ):
            return str(cached["value"])
        value, expires_at = _with_retries(lambda: _fetch_token(cfg), what="token")
        _tokens[key] = {"value": value, "expires_at": expires_at}
        return value


def diagnose(cfg: GraphConfig | None = None) -> dict:
    """Admin diagnostics without sending anything: can the app get a token,
    and does that token carry the Mail.Send APPLICATION permission? Returns
    only non-secret facts (never the token)."""
    cfg = cfg or global_config()
    out: dict = {"configured": is_configured(cfg), "sender": sender_address(cfg) or None,
                 "on_behalf_of": on_behalf_address(cfg) or None,
                 "token_ok": False, "roles": [], "mail_send_granted": False, "error": None}
    if not is_configured(cfg):
        out["error"] = "Microsoft Graph email configuration is incomplete."
        return out
    try:
        token = get_access_token(force_refresh=True, cfg=cfg)
    except GraphMailError as exc:
        out["error"] = str(exc)
        out["error_code"] = exc.code
        return out
    out["token_ok"] = True
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        claims = {}
    out["roles"] = sorted(claims.get("roles") or [])
    out["mail_send_granted"] = "Mail.Send" in out["roles"]
    out["tenant_matches"] = claims.get("tid") == cfg.tenant_id.strip()
    if not out["mail_send_granted"]:
        out["error"] = (
            "The token has no Mail.Send application permission. In Microsoft Entra admin center > App "
            "registrations > this app > API permissions: Add a permission > Microsoft Graph > Application "
            "permissions > Mail.Send, then 'Grant admin consent'."
        )
    return out


# ── retries ────────────────────────────────────────────────────────────────

def _with_retries(fn, *, what: str):
    attempts = max(1, settings.microsoft_graph_max_retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except GraphMailError as exc:
            if not exc.transient or attempt == attempts:
                raise
            delay = getattr(exc, "retry_after", None) or min(30.0, 2 ** attempt + random.random())
            logger.warning("Microsoft Graph %s attempt %d failed (%s); retrying in %.1fs", what, attempt, exc.code, delay)
            time.sleep(delay)
        except (socket.timeout, TimeoutError) as exc:
            if attempt == attempts:
                raise GraphMailError("timeout", "Microsoft Graph did not respond in time.", transient=True) from exc
            time.sleep(min(30.0, 2 ** attempt))
        except (OSError, http.client.HTTPException) as exc:
            if attempt == attempts:
                raise GraphMailError("network_error", f"Could not reach Microsoft ({type(exc).__name__}).", transient=True) from exc
            time.sleep(min(30.0, 2 ** attempt))
    raise RuntimeError("unreachable")  # pragma: no cover


# ── sendMail ───────────────────────────────────────────────────────────────

def _recipients(addresses: list[str]) -> list[dict]:
    return [{"emailAddress": {"address": a}} for a in addresses if a]


def build_payload(message: MailMessage, *, on_behalf: bool = True, cfg: GraphConfig | None = None) -> dict:
    """The Graph sendMail JSON body (exposed for tests). With
    MICROSOFT_ON_BEHALF_OF set (and [on_behalf]): From = that address,
    Sender = the MICROSOFT_FROM_EMAIL mailbox -- "sent on behalf of"; replies
    go to the on-behalf address unless the message sets its own Reply-To.
    Without it (or on the fallback after Exchange denied Send on Behalf),
    From = the mailbox and Reply-To falls back to the on-behalf address."""
    cfg = cfg or global_config()
    if message.html_body is not None:
        body = {"contentType": "HTML", "content": message.html_body}
    else:
        body = {"contentType": "Text", "content": message.text_body or ""}
    msg: dict = {
        "subject": message.subject,
        "body": body,
        "toRecipients": _recipients(message.to),
    }
    if message.cc:
        msg["ccRecipients"] = _recipients(message.cc)
    if message.bcc:
        msg["bccRecipients"] = _recipients(message.bcc)
    behalf = on_behalf_address(cfg)
    reply_to = message.reply_to or ([behalf] if behalf else [])
    if reply_to:
        msg["replyTo"] = _recipients(reply_to)
    if behalf and on_behalf:
        display = (cfg.on_behalf_name or "").strip()
        msg["from"] = {"emailAddress": {"address": behalf, **({"name": display} if display else {})}}
        msg["sender"] = {"emailAddress": {"address": sender_address(cfg)}}
    elif message.from_name:
        msg["from"] = {"emailAddress": {"address": sender_address(cfg), "name": message.from_name}}
    if message.attachments:
        msg["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": a.name,
                "contentType": a.content_type,
                "contentBytes": base64.b64encode(a.content).decode("ascii"),
            }
            for a in message.attachments
        ]
    return {"message": msg, "saveToSentItems": True}


def _graph_error(status: int, headers: dict[str, str], data: bytes) -> GraphMailError:
    try:
        err = (json.loads(data or b"{}").get("error") or {})
    except ValueError:
        err = {}
    code = str(err.get("code") or "")
    if status == 429 or code in {"ApplicationThrottled", "MailboxConcurrency", "TooManyRequests"}:
        exc = GraphMailError("throttled", "Microsoft Graph is throttling requests; will retry.", status=status, transient=True)
        try:
            exc.retry_after = min(60.0, float(headers.get("retry-after", "0")))  # type: ignore[attr-defined]
        except ValueError:
            pass
        return exc
    if status == 401:
        return GraphMailError("unauthorized", "Microsoft Graph rejected the access token.", status=status)
    if code in {"ErrorSendAsDenied", "ErrorSendOnBehalfOfDenied"}:
        return GraphMailError(
            "send_on_behalf_denied",
            "Exchange refused to send on behalf of MICROSOFT_ON_BEHALF_OF: give MICROSOFT_FROM_EMAIL "
            "Send on Behalf on that mailbox (Set-Mailbox <on-behalf> -GrantSendOnBehalfTo <from>).",
            status=status,
        )
    if status == 403 or code in {"ErrorAccessDenied", "AccessDenied", "Authorization_RequestDenied"}:
        return GraphMailError(
            "permission_denied",
            "The app is not allowed to send as this mailbox: grant the Microsoft Graph APPLICATION "
            "permission Mail.Send with admin consent, and check any Exchange application access "
            "policy includes MICROSOFT_FROM_EMAIL.",
            status=status,
        )
    if status == 404 or code in {"ErrorInvalidUser", "ResourceNotFound", "MailboxNotEnabledForRESTAPI", "ErrorNonExistentMailbox"}:
        return GraphMailError("mailbox_not_found", "The sender mailbox (MICROSOFT_FROM_EMAIL) was not found or has no Exchange Online mailbox.", status=status)
    if code in {"ErrorInvalidRecipients", "ErrorInvalidEmailAddress"}:
        return GraphMailError("invalid_recipient", "Microsoft Graph rejected a recipient email address.", status=status)
    if status == 413 or code in {"ErrorMessageSizeExceeded", "RequestBodyTooLarge"}:
        return GraphMailError("attachment_too_large", "The email is too large for Microsoft Graph (limit ~4 MB including attachments).", status=status)
    if status in _TRANSIENT_STATUS:
        exc = GraphMailError("service_unavailable", "Microsoft Graph is temporarily unavailable; will retry.", status=status, transient=True)
        try:
            exc.retry_after = min(60.0, float(headers.get("retry-after", "0"))) or None  # type: ignore[attr-defined]
        except ValueError:
            pass
        return exc
    return GraphMailError("graph_error", f"Microsoft Graph returned {status} {code}".strip(), status=status)


def send_mail(message: MailMessage, cfg: GraphConfig | None = None) -> int:
    """Sends [message] from MICROSOFT_FROM_EMAIL. Returns Graph's HTTP status
    (202 = accepted). Raises GraphMailError."""
    if not message.to:
        raise GraphMailError("invalid_recipient", "At least one recipient is required.")
    cfg = cfg or global_config()
    try:
        return _send(message, on_behalf=True, cfg=cfg)
    except GraphMailError as exc:
        if exc.code != "send_on_behalf_denied" or not on_behalf_address(cfg):
            raise
        # Never lose the email over a missing Exchange right: send it from the
        # mailbox itself (Reply-To still the on-behalf address) and say why.
        logger.warning("Send on behalf of %s was denied by Exchange; sent from %s instead. %s",
                       on_behalf_address(cfg), sender_address(cfg), exc)
        return _send(message, on_behalf=False, cfg=cfg)


def _send(message: MailMessage, *, on_behalf: bool, cfg: GraphConfig) -> int:
    body = json.dumps(build_payload(message, on_behalf=on_behalf, cfg=cfg)).encode("utf-8")
    path = f"/v1.0/users/{urllib.parse.quote(sender_address(cfg), safe='@.')}/sendMail"
    refreshed = False

    def attempt() -> int:
        nonlocal refreshed
        token = get_access_token(cfg=cfg)
        status, headers, data = _request(
            _GRAPH_HOST, "POST", path, body,
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if status in (200, 202):
            return status
        err = _graph_error(status, headers, data)
        if err.code in ("unauthorized", "permission_denied") and not refreshed:
            # Expired / revoked token -- or one issued before Mail.Send was
            # granted (a token's permissions are fixed when it is issued):
            # fetch a new one and try again once.
            refreshed = True
            invalidate_token(cfg)
            err.transient = True
        raise err

    return _with_retries(attempt, what="sendMail")
