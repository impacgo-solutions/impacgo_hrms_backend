"""Approve / Reject straight from an approval email (no HRMS login).

Each approval email sent to an approver carries two links -- Approve and
Reject -- holding a signed, expiring token bound to ONE request, ONE
approver and ONE action (plus the tenant). The token is stateless:
base64url(JSON payload) + "." + HMAC-SHA256 under a key derived from
JWT_SECRET (so it can never be used as, or confused with, a login token).

Opening a link only shows a confirmation page (GET never changes anything):
mail scanners such as Microsoft Defender Safe Links open every link in an
incoming email, so a one-click decision would be made by the scanner. The
approver confirms with a button (POST); Reject asks for a reason.

The decision itself goes through the exact same endpoint function the app
uses (routers.leave.update_leave_request), acting as the approver, so every
rule is identical: self-approval guard, "already decided" / cancelled /
withdrawn checks, current-approver / multi-level chain, linked auto-split
legs, balance adjustment, audit log and the employee's decision email.
A link for a request that has since been decided simply says so.

Off unless EMAIL_ACTIONS_ENABLED=true. Links point at PUBLIC_API_BASE_URL
(falls back to FRONTEND_BASE_URL, whose /api is the backend in deployment).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crud, database, models
from .config import settings

logger = logging.getLogger(__name__)

# Request types that can be decided from an email (leave first; others
# follow the same pattern once each has a decide function wired in below).
SUPPORTED_ENTITY_TYPES = ("leave_request",)
ACTIONS = {"approve": "approved", "reject": "rejected"}
_TOKEN_VERSION = 1


def _key() -> bytes:
    return hashlib.sha256(("impacgo-email-action:" + settings.jwt_secret).encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(*, tenant_slug: str, entity_type: str, entity_id: uuid.UUID, user_id: uuid.UUID,
               action: str, ttl_hours: int | None = None) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    payload = {
        "v": _TOKEN_VERSION, "s": tenant_slug, "e": entity_type, "i": str(entity_id),
        "u": str(user_id), "a": action,
        "x": int(time.time() + 3600 * (ttl_hours or settings.email_action_ttl_hours)),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token: str) -> dict | None:
    """The verified payload, or None if the token is malformed, tampered
    with, expired, or for an unsupported type/action."""
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("v") != _TOKEN_VERSION or payload.get("x", 0) < time.time():
            return None
        if payload.get("e") not in SUPPORTED_ENTITY_TYPES or payload.get("a") not in ACTIONS:
            return None
        payload["i"] = uuid.UUID(payload["i"])
        payload["u"] = uuid.UUID(payload["u"])
        if not isinstance(payload.get("s"), str) or not payload["s"]:
            return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def action_url(token: str) -> str:
    base = (settings.public_api_base_url or settings.frontend_base_url).rstrip("/")
    return f"{base}/api/email-actions/{token}"


def action_buttons_html(db: Session | None, *, entity_type: str, entity_id: uuid.UUID,
                        approver_employee_id: uuid.UUID | None) -> str:
    """The Approve / Reject button block for one approver's copy of an
    approval email -- "" when the feature is off, the type is unsupported,
    or the approver has no login (then the email keeps only its normal
    Open-in-HRMS button)."""
    if not settings.email_actions_enabled or db is None or entity_type not in SUPPORTED_ENTITY_TYPES:
        return ""
    if approver_employee_id is None:
        return ""
    try:
        user_id = crud.get_user_id_for_employee(db, approver_employee_id)
        slug = database.get_session_tenant_slug(db) or database.get_tenant_slug()
    except Exception:  # never let an optional extra break the email itself
        logger.exception("email actions: could not resolve approver / tenant")
        return ""
    if user_id is None or not slug:
        return ""
    approve = action_url(make_token(tenant_slug=slug, entity_type=entity_type, entity_id=entity_id,
                                    user_id=user_id, action="approve"))
    reject = action_url(make_token(tenant_slug=slug, entity_type=entity_type, entity_id=entity_id,
                                   user_id=user_id, action="reject"))
    font = "font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    btn = ("display:inline-block;padding:12px 30px;" + font +
           "font-size:14px;font-weight:700;text-decoration:none;border-radius:8px;")
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 10px;"><tr>'
        f'<td style="border-radius:8px;background-color:#16a34a;padding:0;">'
        f'<a href="{html.escape(approve)}" style="{btn}color:#ffffff;background-color:#16a34a;">&#10003;&nbsp; Approve</a></td>'
        '<td style="width:12px;">&nbsp;</td>'
        f'<td style="border-radius:8px;background-color:#ffffff;border:1px solid #fca5a5;padding:0;">'
        f'<a href="{html.escape(reject)}" style="{btn}color:#b91c1c;background-color:#ffffff;">&#10005;&nbsp; Reject</a></td>'
        '</tr></table>'
        f'<p style="margin:0 0 18px;color:#94a3b8;font-size:12px;line-height:1.5;{font}">'
        "Approve or reject right from this email &mdash; you will see a confirmation page first. "
        f"These buttons work for {settings.email_action_ttl_hours} hours and only for you; "
        "please don&rsquo;t forward this email.</p>"
    )


# ── decision pages ─────────────────────────────────────────────────────────

class ActionContext:
    """A tenant-scoped DB session plus the verified token's request and
    approver. `error` is set (and the rest may be None) when the link can't
    be used."""

    def __init__(self, token: str):
        self.db: Session | None = None
        self.payload = read_token(token) if settings.email_actions_enabled else None
        self.user: models.User | None = None
        self.leave: models.LeaveRequest | None = None
        self.company_name = ""
        self.error: str | None = None
        if not settings.email_actions_enabled:
            self.error = "Approving from email is turned off. Please open Impacgo HRMS to review this request."
            return
        if self.payload is None:
            self.error = ("This link is not valid or has expired. "
                          "Please open Impacgo HRMS to review this request.")
            return
        slug = self.payload["s"]
        self.db = database.SessionLocal()
        tenant = self.db.scalar(select(models.Tenant).where(models.Tenant.slug == slug,
                                                            models.Tenant.is_active.is_(True)))
        if tenant is None:
            self.error = "This link is no longer valid."
            return
        database.set_tenant_context(slug)
        database.set_session_tenant_slug(self.db, slug)
        database.ensure_tenant_search_path(self.db)
        user = crud.get_user_by_id(self.db, self.payload["u"])
        if user is None or user.status != "active":
            self.error = "Your HRMS account is not active, so this request can't be decided from email."
            return
        self.user = user
        database.set_session_current_user(self.db, user.id)
        company = self.db.get(models.Company, user.company_id)
        self.company_name = company.name if company else ""
        leave = self.db.get(models.LeaveRequest, self.payload["i"])
        if leave is None or leave.company_id != user.company_id:
            self.error = "This request no longer exists."
            return
        self.leave = leave

    @property
    def action(self) -> str:
        return self.payload["a"] if self.payload else ""

    def close(self) -> None:
        if self.db is not None:
            self.db.close()


def leave_summary(db: Session, leave: models.LeaveRequest) -> list[tuple[str, str]]:
    emp = leave.employee
    name = f"{emp.first_name} {emp.last_name or ''}".strip() if emp else "—"
    days = float(leave.days)
    days_text = f"{int(days) if days == int(days) else days} day{'s' if days != 1 else ''}"
    return [
        ("Employee", f"{name}{f' ({emp.employee_code})' if emp and emp.employee_code else ''}"),
        ("Leave type", leave.leave_type.name if leave.leave_type else "—"),
        ("From", leave.from_date.strftime("%d %b %Y") if leave.from_date else "—"),
        ("To", leave.to_date.strftime("%d %b %Y") if leave.to_date else "—"),
        ("Days", days_text),
        ("Reason", leave.reason or "—"),
    ]
