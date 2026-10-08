"""HRMS email service -- the single entry point every module uses to send
email (leave notifications, request/approval notifications, celebrations,
HR documents, payslips, offer letters, admin test).

Transport
    Microsoft Graph (app/graph_mail.py, client-credentials + Mail.Send) from
    MICROSOFT_FROM_EMAIL whenever MICROSOFT_TENANT_ID / MICROSOFT_CLIENT_ID /
    MICROSOFT_CLIENT_SECRET / MICROSOFT_FROM_EMAIL are all set. The legacy
    SMTP settings are used only while Graph is not configured. EMAIL_ENABLED
    =false turns sending off entirely (emails are still logged, as SKIPPED).

Transaction safety
    queue_email(db, job) writes a QUEUED row to core_email_logs inside the
    caller's transaction and hands the email to the background sender only
    AFTER that transaction commits (SQLAlchemy after_commit). If the HRMS
    change rolls back, the log row and the email are both discarded -- an
    email is never sent for a leave (or anything else) that was not saved.

Background sending
    A small thread pool (this app has no Redis/Celery/queue); each worker
    marks its log row SENT / FAILED with the provider's status and a
    classified error code. Manual sends (HR documents, payslips, test email)
    use send_now() instead so the user sees the real outcome.

Idempotency
    Automatic notifications carry an idempotency key
    (type:record:recipient); the same key is not queued again within
    _IDEMPOTENCY_WINDOW, so a retried request / double click never emails
    twice.

Credentials come only from environment variables (config.settings). No
secret, token or Authorization header is ever logged or returned; the email
log stores metadata only (never bodies or attachment contents).
"""

from __future__ import annotations

import base64
import datetime
import html
import logging
import re
import smtplib
import ssl
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from sqlalchemy import and_, event, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, undefer

from . import database, graph_mail, models, tenant_email
from . import template_rendering as tr
from .config import settings
from .graph_mail import Attachment, GraphMailError, MailMessage
from .tenant_email import EmailSendError

logger = logging.getLogger(__name__)

# Bounded pool -- keeps a burst (e.g. bulk approvals) from opening dozens of
# connections at once. Never blocks process shutdown.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="email")

_TEMPLATE_DIR = Path(__file__).with_name("email_templates")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_IDEMPOTENCY_WINDOW = datetime.timedelta(minutes=2)
_PENDING_KEY = "_pending_emails"
_FOOTER = "This is an automated message from Impacgo HRMS. Please do not reply to this email."

# Email types (core_email_logs.email_type).
LEAVE_APPLIED = "LEAVE_APPLIED"
LEAVE_APPROVED = "LEAVE_APPROVED"
LEAVE_REJECTED = "LEAVE_REJECTED"
LEAVE_WITHDRAWAL = "LEAVE_WITHDRAWAL"
REQUEST_SUBMITTED = "REQUEST_SUBMITTED"
REQUEST_DECIDED = "REQUEST_DECIDED"
CELEBRATION = "CELEBRATION"
HOLIDAY_REMINDER = "HOLIDAY_REMINDER"
FNF = "FNF"
EXIT_LETTER = "EXIT_LETTER"
HR_DOCUMENT = "HR_DOCUMENT"
PAYSLIP = "PAYSLIP"
OFFER_LETTER = "OFFER_LETTER"
GENERAL = "GENERAL"
RECRUITMENT = "RECRUITMENT"
TEST = "TEST"
PROMOTION = "PROMOTION"

# HR document kinds -> display name (also the email_type for letters).
HR_DOCUMENT_KINDS = {
    "offer_letter": "Offer Letter",
    "appointment_letter": "Appointment Letter",
    "experience_letter": "Experience Letter",
    "relieving_letter": "Relieving Letter",
    "experience_relieving_letter": "Experience & Relieving Letter",
    "fnf_statement": "Full & Final Settlement Statement",
    "payslip": "Payslip",
    "other": "HR Document",
}

# Allowed attachment types (extension -> MIME type).
ATTACHMENT_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

# Maps a notification's `entity_type` (see crud.create_notification) to the
# Flutter app's nav view id, so the emailed link opens on the same screen
# in-app notification clicks already land on. Mirrors
# lib/routing/todo_navigation.dart -- keep both in sync.
_ENTITY_TYPE_TO_VIEW = {
    "leave_request": "leave",
    "leave_withdrawal": "leave",
    "task": "work",
    "regularization": "attendance",
    "work_entry": "work",
    "timesheet": "work",
    "overtime_request": "attendance",
    "reimbursement": "payroll",
    "salary_revision_request": "payroll",
    "travel_request": "travel",
    "expense_report": "travel",
    "asset_request": "assets",
    "hiring_requisition": "recruitment",
    "recruitment_opening": "recruitment",
    "recruitment_application": "recruitment",
    "recruitment_interview": "recruitment",
    "recruitment_offer": "recruitment",
    "recruitment_preboarding": "recruitment",
    "exit_request": "people",
    "milestone": "projects",
    "birthday": "people",
    "work_anniversary": "people",
    "holiday": "leave",
    "final_settlement": "payroll",
    "exit_letter": "people",
    "employee_promotion": "people",
}


class EmailError(Exception):
    """A request-level problem with an email to send (bad recipient, bad
    attachment) -- safe to show to the user."""

    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


@dataclass
class EmailJob:
    email_type: str
    to: list[str]
    subject: str
    html_body: str | None = None
    text_body: str | None = None
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    from_name: str | None = None
    company_id: uuid.UUID | None = None
    related_entity_type: str | None = None
    related_entity_id: uuid.UUID | None = None
    created_by: uuid.UUID | None = None
    idempotency_key: str | None = None
    # Recipients dropped by EMAIL_ALLOWED_DOMAINS (filled by _clean).
    blocked: list[str] = field(default_factory=list)
    # Attachments too slow to build in the request (bulk payslip PDFs):
    # called by the background worker just before sending; must open its
    # own DB session. Its failure marks the log FAILED (render_failed).
    attachments_factory: "object | None" = None
    attachment_label: str | None = None  # log label for deferred attachments
    # Serializable stand-in for attachments_factory ({"kind": ..., ...}, see
    # register_attachment_recipe) so the outbox can rebuild the attachment
    # after a restart.
    attachment_recipe: dict | None = None
    # Tenant (slug) whose public.tenant_email_settings send this email. Always
    # taken from the authenticated session / the outbox's own tenant schema --
    # never from a request body. Stamped by queue_email / send_now / _worker.
    tenant_slug: str | None = None


def _session_slug(db: Session | None) -> str | None:
    return (database.get_session_tenant_slug(db) if db is not None else None) or database.get_tenant_slug()


# ── helpers ────────────────────────────────────────────────────────────────

def _deep_link(entity_type: str | None, entity_id=None) -> str:
    """`#/{view}/{id}` -- opens the exact record (see app.dart's
    _applyDeepLinkFromUrl); without [entity_id] just the module."""
    view_id = _ENTITY_TYPE_TO_VIEW.get(entity_type or "", "approvals")
    base = f"{settings.frontend_base_url.rstrip('/')}/#/{view_id}"
    return f"{base}/{entity_id}" if entity_id else base


def hrms_url() -> str:
    return settings.frontend_base_url.rstrip("/")


def is_valid_email(address: str | None) -> bool:
    return bool(address) and len(address) <= 254 and bool(_EMAIL_RE.match(address.strip()))


def parse_address_list(value: str | list[str] | None) -> list[str]:
    """'a@x.com, b@y.com' / list -> cleaned, de-duplicated addresses (order kept)."""
    if not value:
        return []
    items = value if isinstance(value, list) else re.split(r"[,;\s]+", value)
    seen: dict[str, None] = {}
    for item in items:
        a = (item or "").strip()
        if a and a.lower() not in {k.lower() for k in seen}:
            seen[a] = None
    return list(seen)


def transport(tenant_slug: str | None = None) -> str | None:
    """The tenant's provider label ('smtp', 'graph', 'sendgrid', 'ses') or
    None (email disabled / this tenant has no usable configuration)."""
    cfg = tenant_email.load_config(tenant_slug)
    return cfg.transport_label if cfg else None


def sender_address(tenant_slug: str | None = None) -> str:
    cfg = tenant_email.load_config(tenant_slug)
    return cfg.from_email if cfg else ""


def status(tenant_slug: str | None = None) -> dict:
    """Admin-visible configuration status for ONE tenant -- never secrets."""
    cfg = tenant_email.load_config(tenant_slug)
    return {
        "enabled": bool(settings.email_enabled),
        "transport": cfg.transport_label if cfg else None,
        "provider": cfg.provider if cfg else None,
        "config_source": cfg.source if cfg else None,
        "sender": (cfg.from_email if cfg else "") or None,
        "allowed_domains": sorted(allowed_domains(tenant_slug)),
        "reminders_enabled": bool(settings.reminders_enabled),
        "max_attachment_bytes": settings.email_max_attachment_bytes,
    }


# ── templates ──────────────────────────────────────────────────────────────

_template_cache: dict[str, str] = {}


def _template(name: str) -> str:
    text = _template_cache.get(name)
    if text is None:
        text = (_TEMPLATE_DIR / f"{name}.html").read_text(encoding="utf-8")
        _template_cache[name] = text
    return text


def render_template(name: str, context: dict[str, object]) -> str:
    """Fills {{placeholders}} in email_templates/<name>.html. Values are
    HTML-escaped, except keys ending in `_html` (already-safe markup built
    by this module). Unknown placeholders render empty."""

    def replace(match: re.Match) -> str:
        key = match.group(1)
        value = context.get(key, "")
        if value is None:
            value = ""
        return str(value) if key.endswith("_html") else html.escape(str(value))

    return _PLACEHOLDER_RE.sub(replace, _template(name))


# The closed set of content templates render_email actually renders --
# matches hcm_email_templates.email_kind's CHECK constraint
# (backend/db/add_multi_templates.sql + add_email_kinds_leave_withdrawal_
# promotion.sql) exactly. "layout" (the wrapper) is deliberately not in
# this set / not customizable per company.
EMAIL_KINDS = (
    "request_notification",
    "birthday", "work_anniversary", "holiday_reminder", "promotion_announcement",
    "leave_applied", "leave_approved", "leave_rejected", "leave_withdrawal",
    "candidate_interview_scheduled", "candidate_interview_rescheduled", "candidate_interview_cancelled",
    "candidate_next_step", "candidate_rejected", "candidate_message",
    "hr_document", "test_email",
)


def _company_name(db: Session | None, company_id: uuid.UUID | None) -> str | None:
    if db is None or company_id is None:
        return None
    company = db.get(models.Company, company_id)
    return company.name if company is not None else None


def _custom_email_template(db: Session | None, company_id: uuid.UUID | None, content_template: str):
    if db is None or company_id is None:
        return None
    return db.scalar(
        select(models.EmailHtmlTemplate).where(
            models.EmailHtmlTemplate.company_id == company_id,
            models.EmailHtmlTemplate.email_kind == content_template,
            models.EmailHtmlTemplate.is_active == True,  # noqa: E712
        )
    )


def _company_logo_html(db: Session | None, company_id: uuid.UUID | None) -> str:
    """An email client fetches <img src> with no Authorization header and no
    short-lived signed media token, and may do so days after the email was
    sent -- so only a logo that is ALREADY a public http(s) URL can be shown.
    A locally-uploaded logo (an internal /media/... path, reachable only
    inside the authenticated app) renders nothing here, same as a company
    with no logo at all -- never a broken-image icon."""
    if db is None or company_id is None:
        return ""
    company = db.get(models.Company, company_id)
    logo_url = getattr(company, "logo_url", None) if company else None
    if not logo_url or not (logo_url.startswith("http://") or logo_url.startswith("https://")):
        return ""
    name = html.escape(company.name or "")
    return (
        f'<td style="padding-right:10px;vertical-align:middle;">'
        f'<img src="{html.escape(logo_url)}" alt="{name}" style="max-height:32px;max-width:140px;display:block;" />'
        f"</td>"
    )


def render_email(
    content_template: str, context: dict[str, object], *, preheader: str = "", brand_color: str = "#2564cf",
    db: Session | None = None, company_id: uuid.UUID | None = None,
) -> str:
    """Renders one of EMAIL_KINDS' content into the (always-fixed) layout
    wrapper. When the company has an active hcm_email_templates row for
    this content_template, that company-authored HTML+CSS is used instead
    of the built-in app/email_templates/<content_template>.html file --
    same {{key}} placeholder substitution either way (verbatim *_html keys
    unescaped), so existing callers' context dicts need no changes. No
    active custom row (the overwhelmingly common case today) renders
    exactly as before.

    {{company_name}} is filled in for every kind (the sending company), and
    the header / footer carry that company's name -- so the built-in
    templates are branded per tenant with no setup."""
    company_name = _company_name(db, company_id)
    if company_name:
        context = {"company_name": company_name, **context}
    custom = _custom_email_template(db, company_id, content_template)
    if custom is not None:
        placeholders = {k: ("" if v is None else str(v)) for k, v in context.items()}
        verbatim = tuple(k for k in context if k.endswith("_html"))
        content = tr.substitute_placeholders(custom.html_body, placeholders, verbatim_keys=verbatim)
        # A token this email has no value for renders empty, never as a
        # raw "{{token}}" in someone's inbox.
        content = _PLACEHOLDER_RE.sub("", content)
        if custom.css_styles:
            content = f"<style>{tr.sanitize_template_css(custom.css_styles)}</style>{content}"  # M-34
        source = custom.html_body
    else:
        content = render_template(content_template, context)
        source = _template(content_template)
    # Approve / Reject buttons (email_actions.py) must reach the approver
    # even when the company's own template predates the token.
    buttons = context.get("action_buttons_html")
    if buttons and "action_buttons_html" not in source:
        content = f"{content}{buttons}"
    brand = context.get("brand_name") or company_name or settings.email_from_name or "Impacgo HRMS"
    return render_template("layout", {
        "content_html": content,
        "preheader": preheader,
        "brand_color": brand_color,
        "brand_name": brand,
        "footer_note": context.get("footer_note") or (
            f"This is an automated message from {company_name} via Impacgo HRMS. Please do not reply to this email."
            if company_name else _FOOTER),
        "company_logo_html": _company_logo_html(db, company_id),
    })


def _detail_row(label: str, value: str) -> str:
    return (
        "<tr>"
        f"<td style=\"padding:4px 0;color:#64748b;font-size:12px;width:140px;vertical-align:top;\">{html.escape(label)}</td>"
        f"<td style=\"padding:4px 0;color:#0f172a;font-size:13px;font-weight:600;\">{html.escape(str(value))}</td>"
        "</tr>"
    )


def _paragraphs_html(text: str | None) -> str:
    """Plain text (user input) -> escaped <p> paragraphs."""
    if not text:
        return ""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return "".join(
        "<p style=\"margin:0 0 10px;color:#334155;font-size:14px;line-height:1.5\">"
        + html.escape(b).replace("\n", "<br>")
        + "</p>"
        for b in blocks
    )


def _text_from_details(heading: str, intro: str, details: dict[str, str], link: str | None = None) -> str:
    lines = [heading, "", intro, ""]
    lines += [f"  {k}: {v}" for k, v in details.items()]
    if link:
        lines += ["", f"Open in HRMS: {link}"]
    lines += ["", _FOOTER]
    return "\n".join(lines)


# ── attachments ────────────────────────────────────────────────────────────

def make_attachment(name: str, content: bytes, content_type: str | None = None) -> Attachment:
    """Validated attachment: allowed type, not empty, PDFs must look like PDFs."""
    safe_name = Path(name or "document").name.replace("\\", "_")[:120] or "document"
    ext = Path(safe_name).suffix.lower()
    if ext not in ATTACHMENT_TYPES:
        raise EmailError(
            f"Attachment type '{ext or 'unknown'}' is not allowed. Allowed: {', '.join(sorted(ATTACHMENT_TYPES))}.",
            code="attachment_type",
        )
    if not content:
        raise EmailError("The attachment is empty or missing.", code="attachment_missing")
    if ext == ".pdf" and not content[:1024].lstrip().startswith(b"%PDF"):
        raise EmailError("The attachment is not a valid PDF file.", code="attachment_type")
    return Attachment(name=safe_name, content=content, content_type=content_type or ATTACHMENT_TYPES[ext])


def _check_attachment_size(attachments: list[Attachment]) -> None:
    total = sum(len(a.content) for a in attachments)
    if total > settings.email_max_attachment_bytes:
        limit_mb = settings.email_max_attachment_bytes / (1024 * 1024)
        raise EmailError(
            f"Attachments total {total / (1024 * 1024):.1f} MB; the limit is {limit_mb:.0f} MB per email.",
            code="attachment_too_large",
        )


# ── delivery ───────────────────────────────────────────────────────────────

def _deliver(job: EmailJob) -> tuple[str, int | None, str | None, str | None, str | None]:
    """Sends [job] now with job.tenant_slug's own provider configuration.
    Returns (status, provider_status, error_code, error_message, transport).
    Never raises."""
    slug = job.tenant_slug
    cfg = tenant_email.load_config(slug)
    if cfg is None:
        return "SKIPPED", None, "not_configured", "Email is not configured for this organisation.", None
    message = MailMessage(
        to=job.to, cc=job.cc, bcc=job.bcc, reply_to=job.reply_to,
        subject=job.subject, html_body=job.html_body, text_body=job.text_body,
        attachments=job.attachments, from_name=job.from_name,
    )
    try:
        code, _cfg = tenant_email.send(slug, message, message_type=job.email_type)
        logger.info(
            "Email sent: tenant=%s type=%s recipients=%d via=%s status=%s entity=%s:%s",
            slug, job.email_type, len(job.to), cfg.provider, code,
            job.related_entity_type, job.related_entity_id,
        )
        return "SENT", code, None, None, cfg.transport_label
    except EmailSendError as exc:
        return "FAILED", exc.status, exc.code, str(exc), cfg.transport_label
    except Exception as exc:  # never log credentials
        logger.error("Email failed: tenant=%s type=%s via=%s error=%s", slug, job.email_type, cfg.provider, type(exc).__name__)
        return "FAILED", None, "send_failed", f"{type(exc).__name__}: sending failed.", cfg.transport_label


# ── durable outbox ─────────────────────────────────────────────────────────
# A queued email is carried in its core_email_logs row (payload) until it
# reaches a final state, so nothing is lost when the in-memory worker queue
# is (server restart, crash, lost DB connection between commit and send):
# process_outbox() -- run by the outbox thread every minute and at startup --
# picks up anything due. Every send first claims its row atomically
# (QUEUED -> SENDING), so an email is never sent twice, even with several
# backend processes. Transient provider failures are retried with backoff;
# a claim that never finished (process died mid-send) is reported as
# "delivery unconfirmed" rather than resent, to never double-send.

_OUTBOX_MAX_ATTEMPTS = 5
_OUTBOX_BACKOFF_SECONDS = (60, 300, 900, 3600)  # wait before attempt 2, 3, 4, 5
_OUTBOX_LOST_AFTER = datetime.timedelta(minutes=2)    # queued, never picked up
_OUTBOX_STALE_CLAIM = datetime.timedelta(minutes=15)  # SENDING with no outcome
_OUTBOX_LEGACY_AFTER = datetime.timedelta(hours=1)    # QUEUED without a payload
_OUTBOX_BATCH = 50
_TRANSIENT_ERRORS = {"timeout", "network_error", "throttled", "service_unavailable", "send_failed",
                     "render_failed", "db_unavailable"}
_DB_RETRY_DELAYS = (1, 3, 8)

_ATTACHMENT_RECIPES: dict = {}


def register_attachment_recipe(kind: str, builder) -> None:
    """[builder](tenant_slug, recipe) -> list[Attachment]; lets the outbox
    rebuild a deferred attachment (e.g. a payslip PDF) after a restart."""
    _ATTACHMENT_RECIPES[kind] = builder


def _open_session(tenant_slug: str | None) -> Session:
    db = database.SessionLocal()
    if tenant_slug:
        database.set_session_tenant_slug(db, tenant_slug)
    return db


def _with_db(tenant_slug: str | None, fn, *, default=None):
    """Runs fn(db) in its own session; retries short DB outages (e.g. the
    server's connection limit) instead of losing the email's status."""
    for attempt in range(len(_DB_RETRY_DELAYS) + 1):
        db = _open_session(tenant_slug)
        try:
            result = fn(db)
            db.commit()
            return result
        except OperationalError:
            db.rollback()
            if attempt == len(_DB_RETRY_DELAYS):
                logger.exception("Email outbox: database unavailable")
                return default
            time.sleep(_DB_RETRY_DELAYS[attempt])
        except Exception:
            db.rollback()
            logger.exception("Email outbox: database error")
            return default
        finally:
            db.close()
    return default


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _job_payload(job: EmailJob) -> dict:
    return {
        "to": job.to, "cc": job.cc, "bcc": job.bcc, "reply_to": job.reply_to,
        "subject": job.subject, "html_body": job.html_body, "text_body": job.text_body,
        "from_name": job.from_name,
        "attachments": [{"name": a.name, "content_type": a.content_type,
                         "content": base64.b64encode(a.content).decode("ascii")} for a in job.attachments],
        "attachment_recipe": job.attachment_recipe,
    }


def _job_from_log(row: models.EmailLog) -> EmailJob:
    p = row.payload or {}
    return EmailJob(
        email_type=row.email_type, to=list(p.get("to") or []), subject=p.get("subject") or row.subject,
        html_body=p.get("html_body"), text_body=p.get("text_body"),
        cc=list(p.get("cc") or []), bcc=list(p.get("bcc") or []), reply_to=list(p.get("reply_to") or []),
        attachments=[Attachment(a["name"], base64.b64decode(a["content"]), a.get("content_type") or "application/pdf")
                     for a in p.get("attachments") or []],
        from_name=p.get("from_name"), company_id=row.company_id,
        related_entity_type=row.related_entity_type, related_entity_id=row.related_entity_id,
        created_by=row.created_by, idempotency_key=row.idempotency_key,
        attachment_recipe=p.get("attachment_recipe"),
    )


def _claim(tenant_slug: str | None, log_id: uuid.UUID) -> bool:
    """QUEUED -> SENDING, atomically. False = someone else has it, it is
    already final, or the DB is unreachable (the outbox retries later)."""
    def op(db):
        return db.execute(
            update(models.EmailLog)
            .where(models.EmailLog.id == log_id, models.EmailLog.status == "QUEUED")
            .values(status="SENDING", claimed_at=_now(), attempts=models.EmailLog.attempts + 1)
            .returning(models.EmailLog.id)
        ).first() is not None
    return bool(_with_db(tenant_slug, op, default=False))


def _finish(tenant_slug: str | None, log_id: uuid.UUID, result) -> None:
    """Records a claimed email's outcome: SENT, a retry (transient failure
    with attempts left) or a final FAILED/SKIPPED; clears the payload once
    final."""
    status_, provider_status, error_code, error_message, via = result

    def op(db):
        row = db.get(models.EmailLog, log_id)
        if row is None:
            return
        row.provider_status = provider_status
        row.error_code = error_code
        row.error_message = (error_message or None) and error_message[:1000]
        row.transport = via or row.transport
        row.claimed_at = None
        attempts = row.attempts or 1
        if status_ == "SENT":
            row.status, row.sent_at, row.next_attempt_at, row.payload = "SENT", _now(), None, None
        elif (status_ == "FAILED" and error_code in _TRANSIENT_ERRORS
              and attempts < _OUTBOX_MAX_ATTEMPTS and row.payload is not None):
            delay = _OUTBOX_BACKOFF_SECONDS[min(attempts, len(_OUTBOX_BACKOFF_SECONDS)) - 1]
            row.status, row.next_attempt_at = "QUEUED", _now() + datetime.timedelta(seconds=delay)
            row.error_message = (f"{row.error_message or 'Temporary failure.'} "
                                 f"Retrying automatically (attempt {attempts + 1} of {_OUTBOX_MAX_ATTEMPTS}).")[:1000]
        else:
            row.status, row.next_attempt_at, row.payload = status_, None, None
    _with_db(tenant_slug, op)


def _update_log(tenant_slug: str | None, log_id: uuid.UUID, result) -> None:
    """Outcome of a synchronous send_now (no claim / retry: the user sees the
    result and can send again)."""
    status_, provider_status, error_code, error_message, via = result

    def op(db):
        row = db.get(models.EmailLog, log_id)
        if row is None:
            return
        row.status = status_
        row.provider_status = provider_status
        row.error_code = error_code
        row.error_message = (error_message or None) and error_message[:1000]
        row.transport = via
        row.attempts = (row.attempts or 0) + 1
        if status_ == "SENT":
            row.sent_at = _now()
    _with_db(tenant_slug, op)


def _worker(job: EmailJob, log_id: uuid.UUID | None, tenant_slug: str | None) -> None:
    job.tenant_slug = tenant_slug or job.tenant_slug
    if log_id is not None and not _claim(tenant_slug, log_id):
        return
    if job.attachments_factory is None and job.attachment_recipe:
        builder = _ATTACHMENT_RECIPES.get(job.attachment_recipe.get("kind"))
        if builder is not None:
            job.attachments_factory = lambda: builder(tenant_slug, job.attachment_recipe)
    if job.attachments_factory is not None:
        try:
            job.attachments = list(job.attachments) + list(job.attachments_factory())
            _check_attachment_size(job.attachments)
        except Exception as exc:
            logger.exception("Could not build the attachment for %s email", job.email_type)
            if log_id is not None:
                code = "attachment_too_large" if isinstance(exc, EmailError) else "render_failed"
                _finish(tenant_slug, log_id, ("FAILED", None, code,
                                              f"The attachment could not be generated ({type(exc).__name__}).", None))
            return
    result = _deliver(job)
    if log_id is not None:
        _finish(tenant_slug, log_id, result)


def process_outbox(tenant_slug: str) -> dict:
    """One outbox pass for a tenant: dispatches emails that are due (a retry,
    or queued > 2 min ago and never picked up -- the worker was lost), and
    closes out rows that can't be completed so the email log stays truthful.
    Safe to run concurrently from several processes."""
    now = _now()
    T = models.EmailLog

    def op(db):
        stale = db.execute(
            update(T).where(T.status == "SENDING", T.claimed_at < now - _OUTBOX_STALE_CLAIM)
            .values(status="FAILED", error_code="delivery_unconfirmed", payload=None, claimed_at=None,
                    error_message="Delivery not confirmed: the server stopped while this email was being sent. "
                                  "It was not resent automatically to avoid a duplicate -- send it again if needed.")
        ).rowcount
        legacy = db.execute(
            update(T).where(T.status == "QUEUED", T.payload.is_(None), T.created_at < now - _OUTBOX_LEGACY_AFTER)
            .values(status="FAILED", error_code="not_dispatched",
                    error_message="Not delivered: the server restarted or lost its database connection before "
                                  "this email was sent, and its content was not stored for an automatic retry. "
                                  "Send it again if it is still needed.")
        ).rowcount
        due = db.scalars(
            select(T).options(undefer(T.payload))
            .where(T.status == "QUEUED", T.payload.is_not(None),
                   or_(and_(T.next_attempt_at.is_(None), T.created_at < now - _OUTBOX_LOST_AFTER),
                       T.next_attempt_at <= now))
            .order_by(T.created_at).limit(_OUTBOX_BATCH)
        ).all()
        return stale, legacy, [(row.id, _job_from_log(row)) for row in due]

    stale, legacy, due = _with_db(tenant_slug, op, default=(0, 0, []))
    for log_id, job in due:
        _executor.submit(_worker, job, log_id, tenant_slug)
    if stale or legacy or due:
        logger.info("Email outbox %s: %d dispatched, %d unconfirmed, %d not dispatched (legacy)",
                    tenant_slug, len(due), stale, legacy)
    return {"dispatched": len(due), "unconfirmed": stale, "not_dispatched": legacy}


_outbox_thread: threading.Thread | None = None
_outbox_stop = threading.Event()


def _outbox_loop() -> None:
    from . import reminders  # tenant list; imported late (reminders imports this module)
    delay = 10  # first pass shortly after startup: recovers what a restart dropped
    while not _outbox_stop.wait(delay):
        delay = max(15, int(settings.email_outbox_poll_seconds))
        try:
            slugs = reminders.active_tenant_slugs()
        except Exception:
            logger.exception("Email outbox: could not list tenants")
            continue
        for slug in slugs:
            if transport(slug) is None:
                continue  # this tenant has no email configured: nothing to dispatch
            try:
                process_outbox(slug)
            except Exception:
                logger.exception("Email outbox pass failed for %s", slug)


def start_outbox() -> None:
    global _outbox_thread
    if _outbox_thread is not None and _outbox_thread.is_alive():
        return
    _outbox_stop.clear()
    _outbox_thread = threading.Thread(target=_outbox_loop, name="hrms-email-outbox", daemon=True)
    _outbox_thread.start()
    logger.info("Email outbox started (every %ss)", settings.email_outbox_poll_seconds)


def stop_outbox() -> None:
    _outbox_stop.set()


@event.listens_for(Session, "after_commit")
def _send_after_commit(session: Session) -> None:
    for job, log_id, slug in session.info.pop(_PENDING_KEY, []):
        _executor.submit(_worker, job, log_id, slug)


@event.listens_for(Session, "after_rollback")
def _drop_after_rollback(session: Session) -> None:
    dropped = session.info.pop(_PENDING_KEY, [])
    if dropped:
        logger.info("Discarded %d queued email(s): the transaction was rolled back.", len(dropped))


def _new_log(job: EmailJob, status_: str) -> models.EmailLog:
    return models.EmailLog(
        id=uuid.uuid4(),
        company_id=job.company_id,
        email_type=job.email_type,
        sender=sender_address(job.tenant_slug) or None,
        recipient=", ".join(job.to),
        cc=", ".join(job.cc) or None,
        bcc=", ".join(job.bcc) or None,
        subject=job.subject[:300],
        attachment_names=", ".join(a.name for a in job.attachments) or job.attachment_label or None,
        related_entity_type=job.related_entity_type,
        related_entity_id=job.related_entity_id,
        status=status_,
        transport=transport(job.tenant_slug),
        attempts=0,
        idempotency_key=job.idempotency_key,
        created_by=job.created_by,
        created_at=datetime.datetime.now(datetime.timezone.utc),
        error_code="not_configured" if status_ == "SKIPPED" else None,
    )


def allowed_domains(tenant_slug: str | None = None) -> set[str]:
    """EMAIL_ALLOWED_DOMAINS as a set; empty = every domain allowed.

    L-38: "*" means any domain explicitly. Left blank in a dev environment
    (APP_ENV dev/local/test -- the shared dev DB is full of demo and real
    employee addresses), the default is the tenant's sender mailbox domain, so
    a dev server never mails outside the organisation by accident. Blank in
    a real deployment (APP_ENV=production etc.) = any domain."""
    raw = (settings.email_allowed_domains or "").strip()
    if raw == "*":
        return set()
    domains = {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip() and d.strip() != "*"}
    if domains or not settings.is_dev:
        return domains
    return dev_default_allowed_domains(tenant_slug)


def dev_default_allowed_domains(tenant_slug: str | None = None) -> set[str]:
    """The tenant sender's domain; nothing configured -> a domain nobody has
    (every send skipped) rather than "anyone"."""
    sender = sender_address(tenant_slug).strip()
    if "@" in sender:
        return {sender.rsplit("@", 1)[-1].lower()}
    return {"invalid.invalid"}


def is_allowed_recipient(address: str, tenant_slug: str | None = None) -> bool:
    """EMAIL_ALLOWED_DOMAINS: True when unset, else only those domains."""
    domains = allowed_domains(tenant_slug)
    return not domains or address.rsplit("@", 1)[-1].lower() in domains


def _clean(job: EmailJob) -> EmailJob:
    domains = allowed_domains(job.tenant_slug)

    def allowed(a):
        return not domains or a.rsplit("@", 1)[-1].lower() in domains

    def keep(addresses):
        valid = [a for a in parse_address_list(addresses) if is_valid_email(a)]
        job.blocked.extend(a for a in valid if not allowed(a))
        return [a for a in valid if allowed(a)]

    job.to = keep(job.to)
    job.cc = [a for a in keep(job.cc) if a.lower() not in {t.lower() for t in job.to}]
    job.bcc = keep(job.bcc)
    if job.blocked:
        logger.info("Email %s: not sent to %d recipient(s) outside EMAIL_ALLOWED_DOMAINS", job.email_type, len(job.blocked))
    # Reply-To default comes from the tenant's own settings (the providers apply it).
    return job


def queue_email(db: Session | None, job: EmailJob) -> models.EmailLog | None:
    """Automatic (event-driven) emails: logged as QUEUED in [db]'s current
    transaction and sent after it commits. Invalid/missing recipients are
    dropped; with none left nothing is queued. Never raises into the
    caller's business logic."""
    try:
        job.tenant_slug = job.tenant_slug or _session_slug(db)
        job = _clean(job)
        if not job.to:
            if job.blocked and db is not None:
                # Visible in the email log: why a notification was not sent.
                skipped = _new_log(EmailJob(**{**job.__dict__, "to": job.blocked, "attachments": []}), "SKIPPED")
                skipped.error_code = "domain_not_allowed"
                skipped.error_message = "Recipient domain is not in EMAIL_ALLOWED_DOMAINS."
                try:
                    with db.begin_nested():
                        db.add(skipped)
                except Exception:
                    logger.warning("Email log unavailable for skipped %s", job.email_type)
            return None
        if db is None:
            _executor.submit(_worker, job, None, job.tenant_slug)
            return None
        status_ = "QUEUED" if transport(job.tenant_slug) else "SKIPPED"
        log: models.EmailLog | None = _new_log(job, status_)
        if status_ == "QUEUED":
            # Durable outbox: the email travels with its row until delivered.
            log.payload = _job_payload(job)
        # Everything touching core_email_logs runs in a SAVEPOINT: if the
        # table is missing (tenant not migrated) only the savepoint rolls
        # back, never the caller's business transaction.
        try:
            with db.begin_nested():
                if job.idempotency_key and db.scalar(
                    select(models.EmailLog.id).where(
                        models.EmailLog.idempotency_key == job.idempotency_key,
                        models.EmailLog.status.in_(("QUEUED", "SENDING", "SENT")),
                        models.EmailLog.created_at >= datetime.datetime.now(datetime.timezone.utc) - _IDEMPOTENCY_WINDOW,
                    ).limit(1)
                ) is not None:
                    logger.info("Skipped duplicate email %s (%s)", job.idempotency_key, job.email_type)
                    return None
                db.add(log)
        except Exception:
            # e.g. core_email_logs not migrated yet in this tenant -- still
            # send, just without a log row; never break the business write.
            logger.warning("Email log unavailable; sending %s without a log row", job.email_type)
            log = None
        if status_ == "SKIPPED":
            logger.debug("Email not sent (not configured): type=%s to=%s", job.email_type, ",".join(job.to))
            return log
        db.info.setdefault(_PENDING_KEY, []).append(
            (job, log.id if log is not None else None, job.tenant_slug)
        )
        return log
    except Exception:
        logger.exception("Could not queue %s email", job.email_type)
        return None


@dataclass
class SendResult:
    status: str
    log_id: uuid.UUID | None
    error_code: str | None = None
    error_message: str | None = None
    provider_status: int | None = None


def send_now(db: Session, job: EmailJob) -> SendResult:
    """Manual sends (HR documents, payslips, offer letters, test email):
    validates, records the log row, sends synchronously and records the
    outcome, so the caller can show the real result. Raises EmailError for
    invalid input; delivery failures are returned, not raised. Commits [db]."""
    job.tenant_slug = _session_slug(db)  # always the authenticated tenant
    requested = parse_address_list(job.to)
    bad = [a for a in requested + parse_address_list(job.cc) + parse_address_list(job.bcc) if not is_valid_email(a)]
    if bad:
        raise EmailError(f"Invalid email address: {bad[0]}", code="invalid_recipient")
    outside = [a for a in requested + parse_address_list(job.cc) + parse_address_list(job.bcc) if not is_allowed_recipient(a, job.tenant_slug)]
    if outside:
        raise EmailError(
            f"Email to {outside[0]} is disabled in this environment (allowed domains: "
            f"{', '.join(sorted(allowed_domains(job.tenant_slug)))}).", code="domain_not_allowed",
        )
    job = _clean(job)
    if not job.to:
        raise EmailError("A valid recipient email address is required.", code="invalid_recipient")
    _check_attachment_size(job.attachments)
    log = _new_log(job, "QUEUED" if transport(job.tenant_slug) else "SKIPPED")
    log_saved = True
    try:
        with db.begin_nested():
            db.add(log)
        db.commit()
    except Exception:
        db.rollback()
        log_saved = False
        logger.warning("Email log unavailable for %s", job.email_type)
    result = _deliver(job)
    if log_saved:
        _update_log(database.get_session_tenant_slug(db), log.id, result)
    status_, provider_status, error_code, error_message, _via = result
    return SendResult(status_, log.id if log_saved else None, error_code, error_message, provider_status)


# ── generic API ────────────────────────────────────────────────────────────

def send_email(
    db: Session | None,
    *,
    to: str | list[str],
    subject: str,
    html_body: str | None = None,
    text_body: str | None = None,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    email_type: str = GENERAL,
    company_id: uuid.UUID | None = None,
    related_entity_type: str | None = None,
    related_entity_id: uuid.UUID | None = None,
    attachments: list[Attachment] | None = None,
    idempotency_key: str | None = None,
    created_by: uuid.UUID | None = None,
) -> models.EmailLog | None:
    """Queue any email (sent after [db] commits). HTML, or plain text when
    only [text_body] is given (Graph carries a single body)."""
    return queue_email(db, EmailJob(
        email_type=email_type, to=parse_address_list(to), subject=subject,
        html_body=html_body, text_body=text_body,
        cc=parse_address_list(cc), bcc=parse_address_list(bcc),
        attachments=list(attachments or []), company_id=company_id,
        related_entity_type=related_entity_type, related_entity_id=related_entity_id,
        idempotency_key=idempotency_key, created_by=created_by,
    ))


def send_email_with_attachment(db: Session | None, *, attachment: Attachment, **kwargs) -> models.EmailLog | None:
    return send_email(db, attachments=[attachment], **kwargs)


def send_email_with_attachments(db: Session | None, *, attachments: list[Attachment], **kwargs) -> models.EmailLog | None:
    _check_attachment_size(attachments)
    return send_email(db, attachments=attachments, **kwargs)


def send_notification(db: Session | None, *, to: str | list[str], subject: str, heading: str, message: str,
                      company_id: uuid.UUID | None = None, entity_type: str | None = None,
                      entity_id: uuid.UUID | None = None) -> models.EmailLog | None:
    """General HR notification: heading + message + optional HRMS link."""
    link = _deep_link(entity_type, entity_id) if entity_type else hrms_url()
    body = render_email("request_notification", {
        "heading": heading, "intro_line": message, "details_html": "",
        "hrms_url": link, "cta_label": "Open in HRMS",
    }, preheader=message[:120], db=db, company_id=company_id)
    return send_email(
        db, to=to, subject=subject, html_body=body, email_type=GENERAL, company_id=company_id,
        related_entity_type=entity_type, related_entity_id=entity_id,
    )


# ── existing workflow emails (crud.notify_new_request / notify_decision) ─────

def send_request_email(
    to_address: str | None,
    *,
    subject: str,
    heading: str,
    intro_line: str,
    details: dict[str, str],
    entity_type: str | None,
    entity_id=None,
    from_display_name: str | None = None,
    cta_label: str = "Open Now",
    db: Session | None = None,
    company_id: uuid.UUID | None = None,
    email_type: str = REQUEST_SUBMITTED,
    idempotency_key: str | None = None,
) -> None:
    """The Reporting Manager Approval Workflow email (every request type --
    see crud.notify_new_request / notify_decision). Silently no-ops without
    a recipient; queued until the caller's transaction commits."""
    if not to_address:
        return
    link = _deep_link(entity_type, entity_id)
    body = render_email("request_notification", {
        "heading": heading, "intro_line": intro_line,
        "details_html": "".join(_detail_row(k, v) for k, v in details.items()),
        "hrms_url": link, "cta_label": cta_label,
    }, preheader=intro_line[:120], db=db, company_id=company_id)
    queue_email(db, EmailJob(
        email_type=email_type, to=[to_address], subject=subject, html_body=body,
        text_body=_text_from_details(heading, intro_line, details, link),
        from_name=from_display_name, company_id=company_id,
        related_entity_type=entity_type, related_entity_id=_as_uuid(entity_id),
        idempotency_key=(f"{idempotency_key}:{to_address.lower()}" if idempotency_key
                         else f"{email_type}:{entity_id}:{to_address.lower()}" if entity_id else None),
    ))


def send_notification_email(
    to_address: str | None,
    *,
    subject: str,
    heading: str,
    body_lines: list[str],
    employee_name: str | None,
    employee_code: str | None,
    status_label: str,
    occurred_at,
    entity_type: str | None,
    cta_label: str = "Open in Impacgo HRMS",
    db: Session | None = None,
    company_id: uuid.UUID | None = None,
    entity_id=None,
    extra: dict[str, str] | None = None,
) -> None:
    """Birthday / Work Anniversary celebration broadcast email -- each its
    own email kind (birthday / work_anniversary), so a company can design
    them separately. [extra] adds kind-specific tokens (designation,
    department, years_of_service, ...)."""
    if not to_address:
        return
    when = occurred_at.strftime("%d %b %Y") if occurred_at else ""
    details: dict[str, str] = {}
    if employee_name:
        details["Employee"] = employee_name + (f" ({employee_code})" if employee_code else "")
    for label, key in (("Designation", "designation"), ("Department", "department"), ("Years with us", "years_of_service")):
        if extra and extra.get(key):
            details[label] = extra[key]
    details["Occasion"] = status_label
    if when:
        details["Date"] = when
    link = _deep_link(entity_type)
    kind = "work_anniversary" if entity_type == "work_anniversary" else "birthday"
    body = render_email(kind, {
        **(extra or {}),
        "heading": heading, "employee_name": employee_name or "",
        "details_html": "".join(_detail_row(k, v) for k, v in details.items()),
        "body_html": _paragraphs_html("\n\n".join(l for l in body_lines if l)),
        "hrms_url": link, "cta_label": cta_label,
    }, preheader=heading, brand_color="#4338ca", db=db, company_id=company_id)
    queue_email(db, EmailJob(
        email_type=CELEBRATION, to=[to_address], subject=subject, html_body=body,
        text_body=_text_from_details(heading, "\n".join(body_lines), details, link),
        company_id=company_id, related_entity_type=entity_type, related_entity_id=_as_uuid(entity_id),
        idempotency_key=f"{CELEBRATION}:{entity_type}:{entity_id}:{to_address.lower()}:{datetime.date.today()}" if entity_id else None,
    ))


def send_promotion_announcement_email(
    to_address: str | None,
    *,
    db: Session | None,
    company_id: uuid.UUID | None,
    employee_id: uuid.UUID,
    employee_name: str,
    old_designation: str | None,
    new_designation: str,
    effective_date: str | None = None,
    decided_by: str | None = None,
) -> None:
    """Congratulations email sent when an employee's designation actually
    changes (People > Employee Profile > Edit Professional Info) -- same
    broadcast-style layout as send_notification_email (celebration), its
    own email_kind (PROMOTION) so a company can author its own design
    independently of the birthday/anniversary one."""
    if not to_address:
        return
    heading = f"Congratulations, {employee_name}!"
    details: dict[str, str] = {"New Designation": new_designation}
    if old_designation and old_designation != new_designation:
        details["Previous Designation"] = old_designation
    if effective_date:
        details["Effective From"] = effective_date
    if decided_by:
        details["Announced By"] = decided_by
    body_text = (
        f"We're delighted to announce that {employee_name} has been promoted to "
        f"{new_designation}. Please join us in congratulating them on this well-deserved achievement!"
    )
    link = _deep_link("employee_promotion", employee_id)
    body = render_email("promotion_announcement", {
        "heading": heading, "employee_name": employee_name, "new_designation": new_designation,
        "previous_designation": old_designation or "",
        "details_html": "".join(_detail_row(k, v) for k, v in details.items()),
        "body_html": _paragraphs_html(body_text),
        "hrms_url": link, "cta_label": "View Profile",
    }, preheader=heading, brand_color="#16a34a", db=db, company_id=company_id)
    queue_email(db, EmailJob(
        email_type=PROMOTION, to=[to_address], subject=f"Congratulations on Your Promotion, {employee_name}!",
        html_body=body, text_body=_text_from_details(heading, body_text, details, link),
        company_id=company_id, related_entity_type="employee_promotion", related_entity_id=employee_id,
        idempotency_key=f"{PROMOTION}:{employee_id}:{new_designation}:{to_address.lower()}",
    ))


def send_holiday_reminder_email(
    to_address: str | None,
    *,
    db: Session | None,
    company_id: uuid.UUID | None,
    holiday_id: uuid.UUID,
    subject: str,
    heading: str,
    body: str,
    holiday_name: str,
    holiday_date: str,
    optional: bool,
    branch_name: str | None = None,
) -> None:
    """Holiday reminder (app/reminders.py): one email per employee per holiday."""
    if not to_address:
        return
    details = {"Holiday": holiday_name, "Date": holiday_date, "Type": "Optional holiday" if optional else "Company holiday"}
    if branch_name:
        details["Branch"] = branch_name
    link = _deep_link("holiday")
    html_body = render_email("holiday_reminder", {
        "heading": heading, "holiday_name": holiday_name, "holiday_date": holiday_date,
        "holiday_type": details["Type"], "branch_name": branch_name or "",
        "details_html": "".join(_detail_row(k, v) for k, v in details.items()),
        "body_html": _paragraphs_html(body),
        "hrms_url": link, "cta_label": "View Holiday Calendar",
    }, preheader=heading, brand_color="#0e7490", db=db, company_id=company_id)
    queue_email(db, EmailJob(
        email_type=HOLIDAY_REMINDER, to=[to_address], subject=subject, html_body=html_body,
        text_body=_text_from_details(heading, body, details, link),
        company_id=company_id, related_entity_type="holiday", related_entity_id=holiday_id,
        idempotency_key=f"{HOLIDAY_REMINDER}:{holiday_id}:{to_address.lower()}",
    ))


# ── leave ──────────────────────────────────────────────────────────────────

def _leave_context(**kw) -> dict[str, object]:
    return {k: ("—" if v in (None, "") else v) for k, v in kw.items()}


def send_leave_applied_email(
    db: Session | None, *, to: str | list[str], employee_name: str, employee_code: str | None,
    leave_type: str, start_date: str, end_date: str, number_of_days: str, reason: str | None,
    status: str, leave_request_id: uuid.UUID, company_id: uuid.UUID | None,
    from_display_name: str | None = None, approver_employee_id: uuid.UUID | None = None,
) -> None:
    """New leave application -> the employee's manager(s) / HR.

    [approver_employee_id]: the approver this copy goes to -- adds their
    personal Approve / Reject buttons (app/email_actions.py; only when
    EMAIL_ACTIONS_ENABLED). HR-inbox copies (no single approver) keep just
    the Open-in-HRMS button."""
    from . import email_actions

    link = _deep_link("leave_request", leave_request_id)
    ctx = _leave_context(
        employee_name=employee_name, employee_id=employee_code, leave_type=leave_type,
        start_date=start_date, end_date=end_date, number_of_days=number_of_days,
        reason=reason, status=status,
    )
    ctx["hrms_url"] = link
    ctx["action_buttons_html"] = email_actions.action_buttons_html(
        db, entity_type="leave_request", entity_id=leave_request_id, approver_employee_id=approver_employee_id,
    )
    subject = f"New Leave Application - {employee_name}"
    body = render_email("leave_applied", ctx, preheader=f"{employee_name}: {leave_type}, {start_date} to {end_date}",
                       db=db, company_id=company_id)
    for address in parse_address_list(to):
        queue_email(db, EmailJob(
            email_type=LEAVE_APPLIED, to=[address], subject=subject, html_body=body,
            text_body=_text_from_details("New Leave Application", f"{employee_name} has applied for leave.", {
                "Employee": employee_name, "Employee ID": str(ctx["employee_id"]), "Leave Type": leave_type,
                "Start Date": start_date, "End Date": end_date, "Days": number_of_days,
                "Reason": str(ctx["reason"]), "Status": status,
            }, link),
            from_name=from_display_name, company_id=company_id,
            related_entity_type="leave_request", related_entity_id=leave_request_id,
            idempotency_key=f"{LEAVE_APPLIED}:{leave_request_id}:{address.lower()}",
        ))


def send_leave_approved_email(
    db: Session | None, *, to: str, employee_name: str, employee_code: str | None, leave_type: str,
    start_date: str, end_date: str, number_of_days: str, approved_by: str, notes: str | None,
    leave_request_id: uuid.UUID, company_id: uuid.UUID | None,
) -> None:
    link = _deep_link("leave_request", leave_request_id)
    ctx = _leave_context(
        employee_name=employee_name, employee_id=employee_code, leave_type=leave_type,
        start_date=start_date, end_date=end_date, number_of_days=number_of_days,
        approved_by=approved_by, status="Approved",
    )
    ctx["hrms_url"] = link
    ctx["notes_html"] = (
        _paragraphs_html(f"Comments: {notes}") if notes else ""
    )
    queue_email(db, EmailJob(
        email_type=LEAVE_APPROVED, to=[to], subject=f"Leave Approved - {employee_name}",
        html_body=render_email("leave_approved", ctx, preheader=f"Approved by {approved_by}", db=db, company_id=company_id),
        text_body=_text_from_details("Leave Approved", f"Your leave was approved by {approved_by}.", {
            "Leave Type": leave_type, "Start Date": start_date, "End Date": end_date,
            "Days": number_of_days, "Approved By": approved_by, "Status": "Approved",
        }, link),
        from_name=approved_by, company_id=company_id,
        related_entity_type="leave_request", related_entity_id=leave_request_id,
        idempotency_key=f"{LEAVE_APPROVED}:{leave_request_id}:{to.lower()}",
    ))


def send_leave_withdrawal_email(
    db: Session | None, *, to: str | list[str] | None, kind: str, employee_name: str, employee_code: str | None,
    leave_type: str, start_date: str, end_date: str, number_of_days: str, reason: str | None,
    status: str, decided_by: str | None, restored: str | None, notes: str | None,
    withdrawal_id: uuid.UUID, company_id: uuid.UUID | None, from_display_name: str | None = None,
) -> None:
    """Leave Withdrawal emails -- the same leave-email flow as Leave Applied /
    Approved / Rejected (leave layout, leave details, HRMS link, queued after
    commit, one per recipient). [kind]: requested (-> reporting manager(s) /
    HR), approved | rejected (-> the employee), notice (-> reporting
    manager(s): decided / withdrawn without approval). Idempotent per
    withdrawal + kind + recipient, so a retry never sends twice."""
    headings = {
        "requested": ("Leave Withdrawal Request", f"{employee_name} has asked to withdraw their leave. It stays active until the withdrawal is approved."),
        "approved": ("Your Leave Withdrawal Has Been Approved", f"Hi {employee_name}, your leave withdrawal was approved by {decided_by or '—'}. The leave is withdrawn and the days are back in your balance."),
        "rejected": ("Your Leave Withdrawal Has Been Rejected", f"Hi {employee_name}, your leave withdrawal was rejected by {decided_by or '—'}. Your leave stays as approved."),
        "notice": ("Leave Withdrawal Update", f"The leave withdrawal of {employee_name} is now: {status}."),
    }
    heading, intro = headings[kind]
    link = _deep_link("leave_withdrawal", withdrawal_id)
    ctx = _leave_context(employee_name=employee_name, employee_id=employee_code, leave_type=leave_type,
                         start_date=start_date, end_date=end_date, number_of_days=number_of_days, reason=reason,
                         status=status, decided_by=decided_by, restored=restored)
    ctx.update(heading=heading, intro_line=intro, hrms_url=link,
               status_color={"Approved": "#16a34a", "Rejected": "#dc2626"}.get(status, "#b45309"),
               notes_html=_paragraphs_html(f"Comments: {notes}") if notes else "")
    subject = {"requested": f"Leave Withdrawal Request - {employee_name}",
               "approved": f"Leave Withdrawal Approved - {employee_name}",
               "rejected": f"Leave Withdrawal Rejected - {employee_name}",
               "notice": f"Leave Withdrawal {status} - {employee_name}"}[kind]
    body = render_email("leave_withdrawal", ctx, preheader=f"{employee_name}: {leave_type}, {start_date} to {end_date}",
                       db=db, company_id=company_id)
    for address in parse_address_list(to):
        queue_email(db, EmailJob(
            email_type=LEAVE_WITHDRAWAL, to=[address], subject=subject, html_body=body,
            text_body=_text_from_details(heading, intro, {
                "Employee": employee_name, "Employee ID": str(ctx["employee_id"]), "Leave Type": leave_type,
                "Start Date": start_date, "End Date": end_date, "Days": number_of_days,
                "Withdrawal Reason": str(ctx["reason"]), "Decided By": str(ctx["decided_by"]),
                "Balance Restored": str(ctx["restored"]), "Status": status,
            }, link),
            from_name=from_display_name, company_id=company_id,
            related_entity_type="leave_withdrawal", related_entity_id=withdrawal_id,
            idempotency_key=f"{LEAVE_WITHDRAWAL}:{kind}:{withdrawal_id}:{address.lower()}",
        ))


def send_leave_rejected_email(
    db: Session | None, *, to: str, employee_name: str, employee_code: str | None, leave_type: str,
    start_date: str, end_date: str, number_of_days: str, rejected_by: str, rejection_reason: str | None,
    leave_request_id: uuid.UUID, company_id: uuid.UUID | None,
) -> None:
    link = _deep_link("leave_request", leave_request_id)
    ctx = _leave_context(
        employee_name=employee_name, employee_id=employee_code, leave_type=leave_type,
        start_date=start_date, end_date=end_date, number_of_days=number_of_days,
        rejected_by=rejected_by, rejection_reason=rejection_reason, status="Rejected",
    )
    ctx["hrms_url"] = link
    queue_email(db, EmailJob(
        email_type=LEAVE_REJECTED, to=[to], subject=f"Leave Rejected - {employee_name}",
        html_body=render_email("leave_rejected", ctx, preheader=f"Rejected by {rejected_by}", db=db, company_id=company_id),
        text_body=_text_from_details("Leave Rejected", f"Your leave was rejected by {rejected_by}.", {
            "Leave Type": leave_type, "Start Date": start_date, "End Date": end_date,
            "Days": number_of_days, "Rejected By": rejected_by,
            "Rejection Reason": str(ctx["rejection_reason"]), "Status": "Rejected",
        }, link),
        from_name=rejected_by, company_id=company_id,
        related_entity_type="leave_request", related_entity_id=leave_request_id,
        idempotency_key=f"{LEAVE_REJECTED}:{leave_request_id}:{to.lower()}",
    ))


# ── HR documents / payslips / offer letters (manual, synchronous) ───────────

def send_hr_document(
    db: Session,
    *,
    document_kind: str,
    to: str | list[str],
    attachments: list[Attachment],
    recipient_name: str | None,
    company_id: uuid.UUID,
    company_name: str,
    sender_name: str | None,
    created_by: uuid.UUID | None,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    subject: str | None = None,
    message: str | None = None,
    document_name: str | None = None,
    related_entity_type: str | None = None,
    related_entity_id: uuid.UUID | None = None,
) -> SendResult:
    """Offer / appointment / experience / relieving letters, payslips and
    other HR documents, attached as real file attachments -- sent now."""
    return send_now(db, _hr_document_job(
        document_kind=document_kind, to=to, attachments=attachments, recipient_name=recipient_name,
        company_id=company_id, company_name=company_name, sender_name=sender_name, created_by=created_by,
        cc=cc, bcc=bcc, subject=subject, message=message, document_name=document_name,
        related_entity_type=related_entity_type, related_entity_id=related_entity_id, db=db,
    ))


def queue_hr_document(db: Session, *, idempotency_key: str | None = None,
                      attachments_factory=None, attachment_names: str | None = None,
                      attachment_recipe: dict | None = None,
                      **kwargs) -> models.EmailLog | None:
    """Same email as send_hr_document, queued (sent after [db] commits) --
    bulk payslips and documents emailed automatically on generation. With
    [attachments_factory] the attachment is built by the background worker
    ([attachment_names] is what the email body / log show meanwhile)."""
    if attachments_factory is not None:
        kwargs.setdefault("attachments", [])
    job = _hr_document_job(allow_deferred=attachments_factory is not None,
                           deferred_names=attachment_names, db=db, **kwargs)
    _check_attachment_size(job.attachments)
    job.idempotency_key = idempotency_key
    job.attachments_factory = attachments_factory
    job.attachment_label = attachment_names
    job.attachment_recipe = attachment_recipe
    return queue_email(db, job)


def _hr_document_job(
    *,
    document_kind: str,
    to: str | list[str],
    attachments: list[Attachment],
    recipient_name: str | None,
    company_id: uuid.UUID,
    company_name: str,
    sender_name: str | None,
    created_by: uuid.UUID | None,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    subject: str | None = None,
    message: str | None = None,
    document_name: str | None = None,
    related_entity_type: str | None = None,
    related_entity_id: uuid.UUID | None = None,
    allow_deferred: bool = False,
    deferred_names: str | None = None,
    db: Session | None = None,
) -> EmailJob:
    if document_kind not in HR_DOCUMENT_KINDS:
        raise EmailError("Unknown document type.")
    if not attachments and not allow_deferred:
        raise EmailError("At least one attachment is required.", code="attachment_missing")
    name = (document_name or "").strip() or HR_DOCUMENT_KINDS[document_kind]
    email_type = {
        "payslip": PAYSLIP, "offer_letter": OFFER_LETTER,
    }.get(document_kind, document_kind.upper() if document_kind != "other" else HR_DOCUMENT)
    names = ", ".join(a.name for a in attachments) or (deferred_names or "")
    body = render_email("hr_document", {
        "document_name": name,
        "recipient_name": recipient_name or "Sir/Madam",
        "message_html": _paragraphs_html(message),
        "attachment_names": names,
        "sender_name": sender_name or "HR Team",
        "company_name": company_name,
        "brand_name": company_name,
        "footer_note": f"Sent by {company_name} through Impacgo HRMS.",
    }, preheader=f"{name} attached", db=db, company_id=company_id)
    return EmailJob(
        email_type=email_type, to=parse_address_list(to), cc=parse_address_list(cc), bcc=parse_address_list(bcc),
        subject=(subject or "").strip() or f"{name} - {company_name}",
        html_body=body, attachments=attachments, company_id=company_id, created_by=created_by,
        related_entity_type=related_entity_type, related_entity_id=related_entity_id,
    )


def send_offer_letter(db: Session, *, to: str, pdf: Attachment, candidate_name: str, **kwargs) -> SendResult:
    return send_hr_document(db, document_kind="offer_letter", to=to, attachments=[pdf],
                            recipient_name=candidate_name, **kwargs)


def send_payslip(db: Session, *, to: str, pdf: Attachment, employee_name: str, period: str, **kwargs) -> SendResult:
    return send_hr_document(db, document_kind="payslip", to=to, attachments=[pdf], recipient_name=employee_name,
                            document_name=f"Payslip - {period}", **kwargs)


def send_test_email(db: Session, *, to: str, requested_by: str, company_id: uuid.UUID | None,
                    created_by: uuid.UUID | None) -> SendResult:
    body = render_email("test_email", {
        "sender": sender_address(_session_slug(db)) or "—", "requested_by": requested_by,
        "sent_at": datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y, %I:%M %p UTC"),
    }, preheader="Your organisation's email configuration is working.", db=db, company_id=company_id)
    return send_now(db, EmailJob(
        email_type=TEST, to=[to], subject="HRMS Email Test", html_body=body,
        company_id=company_id, created_by=created_by,
    ))


def _as_uuid(value) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None
