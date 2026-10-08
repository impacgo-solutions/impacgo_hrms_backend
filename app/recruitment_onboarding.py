"""Recruitment workflow, part 2: Offer (approval, send, candidate
response, expiry) -> Preboarding / New Joiner (tasks, documents with
versions, verification) -> Joining confirmation (cancel / no-show) ->
Create Employee through the existing People employee-creation logic
(crud.create_employee -- same checks as POST /api/employees).

See recruitment_workflow.py for permissions, history and the pipeline."""

from __future__ import annotations

import calendar
import datetime
import hashlib
import logging
import re
import secrets
import shutil
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import crud, database, models
from . import recruitment_workflow as rw
from .config import settings
from .recruitment_workflow import Ctx, err, now, record, notify, _emp_ref, _iso, _num

logger = logging.getLogger(__name__)

OFFER_DOCTYPE = "recruitment_offer"
OFFER_STATUSES = ("draft", "approval_pending", "approved", "sent", "viewed", "accepted", "declined", "expired", "withdrawn")
OFFER_ACTIVE = {"draft", "approval_pending", "approved", "sent", "viewed"}
OFFER_LABELS = {
    "draft": "Draft", "approval_pending": "Approval Pending", "approved": "Approved", "sent": "Sent",
    "viewed": "Viewed", "accepted": "Accepted", "declined": "Declined", "expired": "Expired", "withdrawn": "Withdrawn",
}
PREBOARDING_STATUSES = (
    "not_started", "in_progress", "documents_pending", "verification_pending", "ready_to_join",
    "completed", "failed", "cancelled",
)
DOC_DONE = {"approved", "completed", "verified"}
VERIFICATION_STATES = ("pending", "in_progress", "verified", "needs_clarification", "failed")


# ═══════════════════════════════════════════════════════════════════════════
# Candidate email
# ═══════════════════════════════════════════════════════════════════════════

def email_candidate(ctx: Ctx, candidate: models.Candidate, subject: str, heading: str, message: str, *,
                    details: dict | None = None, cta: tuple[str, str] | None = None,
                    related: tuple[str, uuid.UUID] | None = None,
                    kind: str = "candidate_message", extra: dict | None = None) -> None:
    """Queued (after commit) through the existing email service; the
    EMAIL_ALLOWED_DOMAINS allowlist applies as for every other email.
    [kind] picks the email template (candidate_interview_scheduled,
    candidate_rejected, ... -- each designable separately under Documents >
    Templates); [extra] adds kind-specific tokens (opening_title,
    stage_name, next_stage_name, ...)."""
    from . import email_service as es

    if not candidate or not candidate.email:
        return
    company = ctx.db.get(models.Company, ctx.company_id)
    details_html = ""
    if details:
        details_html = ('<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-top:1px solid #e2e8f0;'
                        'border-bottom:1px solid #e2e8f0;padding:8px 0;margin:6px 0 18px;">'
                        + "".join(es._detail_row(k, v) for k, v in details.items() if v) + "</table>")
    cta_html = ""
    if cta:
        import html as _h
        cta_html = (f'<div style="margin:6px 0 4px"><a href="{_h.escape(cta[1])}" style="display:inline-block;background:#2564cf;'
                    f'color:#ffffff;text-decoration:none;padding:11px 26px;border-radius:6px;font-size:14px;font-weight:bold;">'
                    f'{_h.escape(cta[0])}</a></div><p style="margin:8px 0 0;color:#64748b;font-size:12px">Or open: {_h.escape(cta[1])}</p>')
    body = es.render_email(kind, {
        **(extra or {}),
        "heading": heading, "candidate_name": candidate.name, "message_html": es._paragraphs_html(message),
        "details_block_html": details_html, "cta_html": cta_html, "sender_name": "HR Team",
        "company_name": company.name if company else "", "brand_name": company.name if company else None,
    }, preheader=heading, db=ctx.db, company_id=ctx.company_id)
    text = "\n".join([heading, "", f"Dear {candidate.name},", "", message]
                     + [f"  {k}: {v}" for k, v in (details or {}).items() if v]
                     + ([f"", f"{cta[0]}: {cta[1]}"] if cta else []))
    try:
        with ctx.db.begin_nested():
            es.send_email(ctx.db, to=candidate.email, subject=subject, html_body=body, text_body=None if body else text,
                          email_type=es.RECRUITMENT, company_id=ctx.company_id,
                          related_entity_type=related[0] if related else None,
                          related_entity_id=related[1] if related else None,
                          created_by=ctx.user.id if ctx.user is not None else None)
    except Exception:  # pragma: no cover - never the reason an action fails
        logger.exception("Could not queue candidate email")


# ═══════════════════════════════════════════════════════════════════════════
# Candidate portal links (offer response / preboarding uploads)
# ═══════════════════════════════════════════════════════════════════════════

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_portal_link(ctx: Ctx, purpose: str, entity_id: uuid.UUID, expires_at: datetime.datetime) -> str:
    """Revokes earlier links for the same record and returns the new URL."""
    for old in ctx.db.scalars(select(models.CandidatePortalLink).where(
            models.CandidatePortalLink.purpose == purpose, models.CandidatePortalLink.entity_id == entity_id,
            models.CandidatePortalLink.revoked_at.is_(None))).all():
        old.revoked_at = now()
    token = secrets.token_urlsafe(32)
    ctx.db.add(models.CandidatePortalLink(
        id=uuid.uuid4(), company_id=ctx.company_id, purpose=purpose, entity_id=entity_id, token_hash=_hash(token),
        expires_at=expires_at, created_by=ctx.user.id if ctx.user is not None else None, created_at=now()))
    ctx.db.flush()
    slug = database.get_session_tenant_slug(ctx.db) or database.get_tenant_slug() or ""
    base = (settings.public_api_base_url or settings.frontend_base_url).rstrip("/")
    return f"{base}/api/candidate-portal/{slug}/{token}"


def revoke_portal_links(ctx: Ctx, purpose: str, entity_id: uuid.UUID) -> None:
    for link in ctx.db.scalars(select(models.CandidatePortalLink).where(
            models.CandidatePortalLink.purpose == purpose, models.CandidatePortalLink.entity_id == entity_id,
            models.CandidatePortalLink.revoked_at.is_(None))).all():
        link.revoked_at = now()


def resolve_portal_token(db: Session, token: str) -> models.CandidatePortalLink | None:
    link = db.scalar(select(models.CandidatePortalLink).where(models.CandidatePortalLink.token_hash == _hash(token)))
    if link is None or link.revoked_at is not None or link.expires_at < now():
        return None
    return link


def _end_of(d: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(d + datetime.timedelta(days=1), datetime.time.min, crud.company_tzinfo())


# ═══════════════════════════════════════════════════════════════════════════
# Offers
# ═══════════════════════════════════════════════════════════════════════════

def offer_status(o: models.Offer) -> str:
    s = (o.status or "sent").lower()
    return "declined" if s == "rejected" else s


def offers_for_application(ctx: Ctx, a: models.JobApplication) -> list[models.Offer]:
    return list(ctx.db.scalars(select(models.Offer).where(models.Offer.application_id == a.id)
                               .order_by(models.Offer.version, models.Offer.offer_date)).all())


def active_offer(ctx: Ctx, a: models.JobApplication) -> models.Offer | None:
    for o in offers_for_application(ctx, a):
        if offer_status(o) in OFFER_ACTIVE:
            return o
    return None


def get_offer(ctx: Ctx, offer_id, *, lock: bool = False) -> models.Offer:
    q = select(models.Offer).where(models.Offer.id == uuid.UUID(str(offer_id)))
    if lock:
        q = q.with_for_update()
    o = ctx.db.scalar(q)
    if o is None or o.application is None or o.application.opening.company_id != ctx.company_id:
        raise err(404, "Offer not found.")
    return o


def _submitter_employee_id(ctx: Ctx, o: models.Offer) -> uuid.UUID | None:
    uid = o.submitted_by or o.created_by
    user = ctx.db.get(models.User, uid) if uid else None
    return user.employee_id if user is not None else None


def can_decide_offer(ctx: Ctx, o: models.Offer, *, readonly: bool = True) -> bool:
    if ctx.user is None or offer_status(o) != "approval_pending":
        return False
    requester = _submitter_employee_id(ctx, o)
    if requester is None:
        return crud.is_fallback_approver(ctx.db, ctx.user) and o.submitted_by != ctx.user.id
    fn = crud.can_decide_request_readonly if readonly else crud.can_decide_request_configurable
    return fn(ctx.db, ctx.user, requester, OFFER_DOCTYPE, o.id)


def offer_actions(ctx: Ctx, o: models.Offer) -> list[str]:
    st = offer_status(o)
    acts: list[str] = []
    if ctx.can_edit:
        acts += {"draft": ["edit", "submit", "withdraw"],
                 "approval_pending": ["withdraw"],
                 "approved": ["send", "revise", "withdraw"],
                 "sent": ["resend", "mark_viewed", "accept", "decline", "withdraw"],
                 "viewed": ["resend", "accept", "decline", "withdraw"]}.get(st, [])
        acts.append("download")
    if can_decide_offer(ctx, o):
        acts += ["approve", "reject", "request_changes"]
    return acts


def offer_out(ctx: Ctx, o: models.Offer, *, detail: bool = False) -> dict:
    a = o.application
    comp = ctx.can_see_compensation
    st = offer_status(o)
    out = {
        "id": str(o.id), "application_id": str(o.application_id), "version": o.version or 1,
        "candidate_id": str(a.candidate_id), "candidate_name": a.candidate.name, "candidate_email": a.candidate.email,
        "opening_id": str(a.opening_id), "opening_title": a.opening.title,
        "status": st, "status_label": OFFER_LABELS.get(st, st.title()),
        "designation": o.designation or a.opening.title,
        "department_id": str(o.department_id or a.opening.department_id or "") or None,
        "department_name": rw._name_of(ctx.db, models.Department, o.department_id or a.opening.department_id),
        "branch_id": str(o.branch_id or a.opening.branch_id or "") or None,
        "branch_name": rw._name_of(ctx.db, models.Branch, o.branch_id or a.opening.branch_id),
        "work_mode": o.work_mode, "employment_type": o.employment_type or a.opening.employment_type,
        "reporting_manager": _emp_ref(ctx.db, o.reporting_manager_id),
        "offered_ctc": _num(o.offered_ctc) if comp else None,
        "salary_breakup": o.salary_breakup if comp else None,
        "benefits": o.benefits, "probation_months": o.probation_months, "notice_period_days": o.notice_period_days,
        "working_hours": o.working_hours, "terms": o.terms,
        "compensation_type": o.compensation_type or "ctc",
        "rate_amount": _num(o.rate_amount) if comp and o.rate_amount is not None else None,
        "rate_unit": o.rate_unit, "contract_duration_months": o.contract_duration_months,
        "contract_end_date": _iso(o.contract_end_date),
        "offer_date": _iso(o.offer_date), "joining_date": _iso(o.proposed_joining_date),
        "expiry_date": _iso(o.expiry_date),
        "submitted_at": _iso(o.submitted_at), "approved_at": _iso(o.approved_at),
        "decision_notes": o.decision_notes, "sent_at": _iso(o.sent_at), "sent_count": o.sent_count or 0,
        "last_sent_to": o.last_sent_to, "viewed_at": _iso(o.viewed_at), "responded_at": _iso(o.responded_at),
        "response_source": o.response_source, "accepted_at": _iso(o.accepted_at),
        "decline_reason": o.decline_reason, "withdraw_reason": o.withdraw_reason,
        "created_at": _iso(o.created_at), "actions": offer_actions(ctx, o), "compensation_visible": comp,
    }
    if detail:
        out["history"] = rw.history_for(ctx, entity_type="offer", entity_id=o.id)
        out["approval_trail"] = rw.approval_trail(ctx.db, ctx.company_id, OFFER_DOCTYPE, o.id)
    return out


def list_offers(ctx: Ctx, *, status: list[str] | None = None, opening_id=None, search: str | None = None,
                joining_from=None, joining_to=None, limit: int = 50, offset: int = 0) -> dict:
    ctx.require_view()
    expire_due_offers(ctx.db, ctx.company_id)
    q = (select(models.Offer).join(models.JobApplication, models.JobApplication.id == models.Offer.application_id)
         .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
         .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
         .where(models.JobOpening.company_id == ctx.company_id))
    if status:
        wanted = set(status) | ({"rejected", "Rejected"} if "declined" in status else set())
        wanted |= {s.title() for s in wanted}
        q = q.where(models.Offer.status.in_(wanted))
    if opening_id:
        q = q.where(models.JobApplication.opening_id == opening_id)
    if search and search.strip():
        q = q.where(models.Candidate.name.ilike(f"%{search.strip()}%"))
    if joining_from:
        q = q.where(models.Offer.proposed_joining_date >= joining_from)
    if joining_to:
        q = q.where(models.Offer.proposed_joining_date <= joining_to)
    total = ctx.db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = ctx.db.scalars(q.order_by(models.Offer.offer_date.desc(), models.Offer.version.desc())
                          .limit(limit).offset(offset)).all()
    return {"items": [offer_out(ctx, o) for o in rows], "total": total}


_OFFER_FIELDS = ("designation", "work_mode", "employment_type", "salary_breakup", "benefits", "probation_months",
                 "notice_period_days", "working_hours", "terms")


def _apply_offer_fields(ctx: Ctx, o: models.Offer, data: dict) -> None:
    rw._validate_common(ctx, data)
    if "offered_ctc" in data and data["offered_ctc"] is not None:
        ctc = float(data["offered_ctc"])
        if ctc <= 0:
            raise err(422, "Offered CTC must be more than zero.")
        o.offered_ctc = ctc
    if "joining_date" in data:
        o.proposed_joining_date = rw._parse_date(data["joining_date"])
    if "expiry_date" in data:
        o.expiry_date = rw._parse_date(data["expiry_date"])
    if "department_id" in data:
        dep = rw.department_in_company(ctx, data["department_id"])
        o.department_id = dep.id if dep else None
    if "branch_id" in data:
        br = rw.branch_in_company(ctx, data["branch_id"])
        o.branch_id = br.id if br else None
    if "reporting_manager_id" in data:
        rm = rw.employee_in_company(ctx, data["reporting_manager_id"], "Reporting manager")
        o.reporting_manager_id = rm.id if rm else None
    for f in _OFFER_FIELDS:
        if f in data:
            v = data[f]
            if isinstance(v, str):
                v = v.strip() or None
            if f in ("probation_months", "notice_period_days") and v is not None and not 0 <= int(v) <= 365:
                raise err(422, "Probation / notice period out of range.")
            setattr(o, f, v)
    _apply_contract_fields(o, data)


RATE_UNITS = ("hourly", "daily", "monthly")


def is_contract_offer(o: models.Offer) -> bool:
    """The offer's own employment type (pre-filled from the opening, which
    HR may change on the offer) -- the single switch for every contract
    branch below, like the gratuity rule in the exit / F&F calculation."""
    et = o.employment_type or (o.application.opening.employment_type if o.application is not None else None)
    return (et or "").strip().lower() == "contract"


def _add_months(d: datetime.date, months: int) -> datetime.date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _apply_contract_fields(o: models.Offer, data: dict) -> None:
    """Contract terms: only read when the request sends them, so a
    non-contract offer that never sends them is untouched."""
    if "rate_amount" in data:
        v = data["rate_amount"]
        if v in (None, ""):
            o.rate_amount = None
        else:
            amount = float(v)
            if amount <= 0:
                raise err(422, "Contract rate must be more than zero.")
            o.rate_amount = amount
    if "rate_unit" in data:
        v = (data["rate_unit"] or "").strip().lower() or None
        if v is not None and v not in RATE_UNITS:
            raise err(422, "Rate unit must be hourly, daily or monthly.")
        o.rate_unit = v
    if "contract_duration_months" in data:
        v = data["contract_duration_months"]
        if v in (None, ""):
            o.contract_duration_months = None
        else:
            months = int(v)
            if not 1 <= months <= 120:
                raise err(422, "Contract duration must be between 1 and 120 months.")
            o.contract_duration_months = months
    if "contract_end_date" in data:
        o.contract_end_date = rw._parse_date(data["contract_end_date"])


def _finalize_contract_terms(o: models.Offer) -> None:
    """Contract offers: tag compensation_type='rate' and derive the end
    date from joining date + duration when only the duration was given.
    Non-contract offers keep 'ctc' (the column default) -- no change."""
    if not is_contract_offer(o):
        o.compensation_type = "ctc"
        return
    o.compensation_type = "rate"
    if o.contract_end_date is None and o.contract_duration_months and o.proposed_joining_date:
        o.contract_end_date = _add_months(o.proposed_joining_date, o.contract_duration_months) - datetime.timedelta(days=1)


def _validate_contract_terms(o: models.Offer) -> None:
    missing = [label for label, v in (("contract rate", o.rate_amount), ("rate unit", o.rate_unit),
                                      ("contract end date", o.contract_end_date)) if not v]
    if missing:
        raise err(422, "Contract offers need: " + ", ".join(missing) + ".")
    if o.proposed_joining_date and o.contract_end_date <= o.proposed_joining_date:
        raise err(422, "The contract end date must be after the joining date.")


def _validate_offer_complete(o: models.Offer) -> None:
    if is_contract_offer(o):
        missing = [label for label, v in (("joining date", o.proposed_joining_date), ("designation", o.designation),
                                          ("expiry date", o.expiry_date)) if not v]
        if missing:
            raise err(422, "Complete the offer first: " + ", ".join(missing) + ".")
        _validate_contract_terms(o)
        if o.expiry_date and o.expiry_date < datetime.date.today():
            raise err(422, "The offer expiry date is in the past.")
        return
    missing = [label for label, v in (("offered CTC", o.offered_ctc), ("joining date", o.proposed_joining_date),
                                      ("designation", o.designation), ("expiry date", o.expiry_date)) if not v]
    if missing:
        raise err(422, "Complete the offer first: " + ", ".join(missing) + ".")
    if o.expiry_date and o.expiry_date < datetime.date.today():
        raise err(422, "The offer expiry date is in the past.")


def create_offer(ctx: Ctx, a: models.JobApplication, data: dict) -> models.Offer:
    ctx.require_edit()
    st = rw.app_status(a)
    if st != "selected":
        raise err(409, "Offers are created for selected candidates (make the final selection first).")
    if active_offer(ctx, a) is not None:
        raise err(409, "This application already has an active offer.")
    o_open = a.opening
    req = ctx.db.get(models.HiringRequisition, o_open.requisition_id) if o_open.requisition_id else None
    settings_ = rw.get_settings(ctx.db, ctx.company_id)
    version = (ctx.db.scalar(select(func.max(models.Offer.version)).where(models.Offer.application_id == a.id)) or 0) + 1
    offer = models.Offer(
        id=uuid.uuid4(), application_id=a.id, offered_ctc=0, offer_date=datetime.date.today(), status="draft",
        version=version, designation=o_open.title, department_id=o_open.department_id, branch_id=o_open.branch_id,
        work_mode=o_open.work_mode or (req.work_mode if req else None),
        employment_type=o_open.employment_type, reporting_manager_id=o_open.reporting_manager_id,
        proposed_joining_date=o_open.target_joining_date or (req.target_joining_date if req else None),
        expiry_date=datetime.date.today() + datetime.timedelta(days=settings_["offer_expiry_days"]),
        created_by=ctx.user.id, created_at=now(), sent_count=0,
    )
    _apply_offer_fields(ctx, offer, dict(data))
    _finalize_contract_terms(offer)
    if is_contract_offer(offer):
        _validate_contract_terms(offer)
    elif not offer.offered_ctc:
        raise err(422, "Offered CTC is required.")
    ctx.db.add(offer)
    ctx.db.flush()
    rw._move(ctx, a, "offer", f"Offer created (version {version})")
    record(ctx, "offer", offer.id, "Offer created", new="draft", application=a, meta={"version": version})
    return offer


def update_offer(ctx: Ctx, o: models.Offer, data: dict) -> None:
    ctx.require_edit()
    if offer_status(o) != "draft":
        raise err(409, "Only a draft offer can be edited (use Revise on an approved offer).")
    _apply_offer_fields(ctx, o, dict(data))
    _finalize_contract_terms(o)
    o.updated_at = now()
    record(ctx, "offer", o.id, "Offer edited", old="draft", new="draft", application=o.application)


def submit_offer(ctx: Ctx, o: models.Offer, comments: str | None = None) -> str:
    ctx.require_edit()
    if offer_status(o) != "draft":
        raise err(409, f"This offer is {OFFER_LABELS[offer_status(o)].lower()} -- only a draft can be submitted.")
    _validate_offer_complete(o)
    settings_ = rw.get_settings(ctx.db, ctx.company_id)
    o.submitted_at = now()
    o.submitted_by = ctx.user.id
    o.decision_notes = None
    if not settings_["offer_approval_required"]:
        o.status = "approved"
        o.approved_at = now()
        o.approved_by = ctx.user.id
        record(ctx, "offer", o.id, "Offer approved (approval not required by settings)", old="draft", new="approved",
               application=o.application, comments=comments)
        return "approved"
    rw.restart_approval(ctx, OFFER_DOCTYPE, o.id, comments)
    o.status = "approval_pending"
    record(ctx, "offer", o.id, "Offer submitted for approval", old="draft", new="approval_pending",
           application=o.application, comments=comments)
    ctx.db.flush()
    _notify_offer_approvers(ctx, o)
    return "approval_pending"


def _notify_offer_approvers(ctx: Ctx, o: models.Offer) -> None:
    requester_id = _submitter_employee_id(ctx, o)
    requester = ctx.db.get(models.Employee, requester_id) if requester_id else None
    if requester is not None:
        users = rw.current_approver_user_ids(ctx.db, ctx.company_id, OFFER_DOCTYPE, o.id, requester)
    else:
        users = rw.user_ids_for_employees(ctx.db, crud.list_fallback_approver_employee_ids(ctx.db, ctx.company_id))
    a = o.application
    notify(ctx, users, "Offer awaiting your approval",
           f"Offer for {a.candidate.name} -- {o.designation or a.opening.title}.", "recruitment_offer", o.id,
           details={"Candidate": a.candidate.name, "Position": o.designation or a.opening.title,
                    "Joining date": _iso(o.proposed_joining_date) or "—"})


def decide_offer(ctx: Ctx, o: models.Offer, decision: str, comments: str | None) -> str:
    if decision not in ("approved", "rejected", "sent_back"):
        raise err(422, "Decision must be approve, reject or request changes.")
    if offer_status(o) != "approval_pending":
        raise err(409, f"This offer is {OFFER_LABELS[offer_status(o)].lower()} -- it isn't waiting for approval.")
    if decision in ("rejected", "sent_back") and not (comments or "").strip():
        raise err(422, "A reason is required to reject or request changes.")
    if not can_decide_offer(ctx, o, readonly=False):
        raise err(403, "You are not an approver for the current step of this offer (the preparer can't approve it).")
    requester = _submitter_employee_id(ctx, o)
    effective = decision if requester is None else crud.decide_configurable_request(
        ctx.db, ctx.user, requester, OFFER_DOCTYPE, o.id, decision, comments)
    a = o.application
    o.decision_notes = comments
    if effective == "pending":
        record(ctx, "offer", o.id, "Offer approved (step) -- sent to next approver", old="approval_pending",
               new="approval_pending", application=a, comments=comments)
        ctx.db.flush()
        _notify_offer_approvers(ctx, o)
        return "approval_pending"
    submitter = [o.submitted_by] if o.submitted_by else []
    if effective == "approved":
        o.status = "approved"
        o.approved_at = now()
        o.approved_by = ctx.user.id
        record(ctx, "offer", o.id, "Offer approved", old="approval_pending", new="approved", application=a, comments=comments)
        notify(ctx, submitter + rw.recruiter_user_ids(ctx, a), "Offer approved",
               f"The offer for {a.candidate.name} was approved -- it can be sent now.", "recruitment_offer", o.id)
    else:
        o.status = "draft"
        label = "Offer approval rejected" if effective == "rejected" else "Offer changes requested"
        record(ctx, "offer", o.id, label, old="approval_pending", new="draft", application=a, comments=comments)
        notify(ctx, submitter, label, f"{a.candidate.name}: {comments}", "recruitment_offer", o.id)
    return o.status


def revise_offer(ctx: Ctx, o: models.Offer, comments: str | None) -> None:
    ctx.require_edit()
    if offer_status(o) != "approved":
        raise err(409, "Only an approved (not yet sent) offer can be revised.")
    o.status = "draft"
    o.approved_at = None
    record(ctx, "offer", o.id, "Offer reopened for revision (needs approval again)", old="approved", new="draft",
           application=o.application, comments=comments)


def send_offer(ctx: Ctx, o: models.Offer, data: dict) -> dict:
    """Send (approved) or resend (sent / viewed). With email=true the offer
    letter PDF + a response link are emailed now; the offer is only
    marked sent when that email actually went out."""
    ctx.require_edit()
    st = offer_status(o)
    resend = st in ("sent", "viewed")
    if st != "approved" and not resend:
        raise err(409, "Only an approved offer can be sent." if st in ("draft", "approval_pending")
                  else f"This offer is {OFFER_LABELS[st].lower()}.")
    if data.get("expiry_date"):
        o.expiry_date = rw._parse_date(data["expiry_date"])
    if not o.expiry_date or o.expiry_date < datetime.date.today():
        raise err(422, "Set an offer expiry date that is today or later.")
    a = o.application
    result = {"emailed": False}
    recipient = (data.get("recipient") or a.candidate.email or "").strip()
    if data.get("email", True):
        if not recipient:
            raise err(422, "The candidate has no email on file -- enter a recipient or send it outside HRMS.")
        from . import email_service as es
        from .routers.recruitment import offer_letter_pdf

        company = ctx.db.get(models.Company, ctx.company_id)
        detail = crud.get_offer_detail(ctx.db, o.id, ctx.company_id)
        pdf_bytes, safe_name = offer_letter_pdf(ctx.db, company, detail)
        link = create_portal_link(ctx, "offer", o.id, _end_of(o.expiry_date))
        message = ((data.get("message") or "").strip()
                   or f"Please find attached your offer letter for the position of {o.designation or a.opening.title}.")
        message += (f"\n\nPlease review the offer and accept or decline it by {o.expiry_date.strftime('%d %b %Y')}"
                    f" using this secure link:\n{link}")
        try:
            sent = es.send_offer_letter(
                ctx.db, to=recipient, cc=data.get("cc"), message=message,
                pdf=es.make_attachment(f"Offer_Letter_{safe_name}.pdf", pdf_bytes), candidate_name=a.candidate.name,
                company_id=ctx.company_id, company_name=company.name, sender_name=ctx.actor_name,
                created_by=ctx.user.id, related_entity_type="offer", related_entity_id=o.id)
        except es.EmailError as exc:
            raise err(422, str(exc)) from exc
        if sent.status not in ("SENT", "QUEUED"):
            raise err(502, f"The offer email could not be sent ({sent.error_message or sent.status}). "
                           "The offer was not marked as sent -- try again, or send it outside HRMS.")
        result = {"emailed": True, "email_status": sent.status, "recipient": recipient}
    old = st
    if not resend:
        o.status = "sent"
        o.sent_at = now()
    o.sent_count = (o.sent_count or 0) + 1
    o.last_sent_to = recipient or o.last_sent_to
    record(ctx, "offer", o.id, ("Offer resent" if resend else "Offer sent") + ("" if result["emailed"] else " (outside HRMS)"),
           old=old, new=offer_status(o), application=a,
           meta={"to": recipient or None, "expiry_date": _iso(o.expiry_date), "emailed": result["emailed"]})
    return result


def mark_offer_viewed(ctx: Ctx | None, o: models.Offer, *, source: str = "hr") -> None:
    if offer_status(o) != "sent":
        return
    o.status = "viewed"
    o.viewed_at = now()
    record(ctx, "offer", o.id, "Offer viewed by candidate" if source == "portal" else "Offer marked viewed",
           old="sent", new="viewed", application=o.application, meta={"source": source})


def _check_not_expired(ctx: Ctx, o: models.Offer) -> None:
    if o.expiry_date and o.expiry_date < datetime.date.today():
        expire_offer(ctx, o)
        raise err(409, "This offer has expired.")


def accept_offer(ctx: Ctx, o: models.Offer, *, source: str = "hr", notes: str | None = None,
                 accepted_on: datetime.date | None = None) -> models.Preboarding:
    st = offer_status(o)
    if st not in ("sent", "viewed"):
        raise err(409, "This offer was already accepted." if st == "accepted"
                  else f"A {OFFER_LABELS.get(st, st).lower()} offer can't be accepted.")
    _check_not_expired(ctx, o)
    a = ctx.db.scalar(select(models.JobApplication).where(models.JobApplication.id == o.application_id).with_for_update())
    ts = now()
    o.status = "accepted"
    o.accepted_at = ts
    o.responded_at = ts
    o.response_source = source
    revoke_portal_links(ctx, "offer", o.id)
    record(ctx, "offer", o.id, "Offer accepted", old=st, new="accepted", application=a, comments=notes,
           meta={"source": source, "accepted_on": _iso(accepted_on)})
    rw._move(ctx, a, "preboarding", "Offer accepted -- moved to Preboarding")
    pb = start_preboarding(ctx, a, o)
    notify(ctx, rw.recruiter_user_ids(ctx, a) + ([o.submitted_by] if o.submitted_by else []), "Offer accepted",
           f"{a.candidate.name} accepted the offer for {o.designation or a.opening.title}.", "recruitment_preboarding", pb.id)
    return pb


def decline_offer(ctx: Ctx, o: models.Offer, *, source: str = "hr", reason: str | None = None) -> None:
    st = offer_status(o)
    if st not in ("sent", "viewed"):
        raise err(409, f"A {OFFER_LABELS.get(st, st).lower()} offer can't be declined.")
    a = o.application
    o.status = "declined"
    o.responded_at = now()
    o.response_source = source
    o.decline_reason = (reason or "").strip() or None
    revoke_portal_links(ctx, "offer", o.id)
    record(ctx, "offer", o.id, "Offer declined", old=st, new="declined", application=a, comments=o.decline_reason,
           meta={"source": source, "version": o.version})
    a.closed_reason = "offer_declined"
    a.previous_status = "offer"
    rw._move(ctx, a, "closed", "Application closed -- offer declined", o.decline_reason)
    notify(ctx, rw.recruiter_user_ids(ctx, a) + ([o.submitted_by] if o.submitted_by else []), "Offer declined",
           f"{a.candidate.name} declined the offer for {o.designation or a.opening.title}.", "recruitment_offer", o.id)


def withdraw_offer(ctx: Ctx, o: models.Offer, reason: str | None, *, by_candidate: bool = False) -> None:
    if not by_candidate:
        ctx.require_edit()
    st = offer_status(o)
    if st not in OFFER_ACTIVE:
        raise err(409, f"A {OFFER_LABELS.get(st, st).lower()} offer can't be withdrawn.")
    if not (reason or "").strip():
        raise err(422, "A reason is required to withdraw an offer.")
    a = o.application
    o.status = "withdrawn"
    o.withdraw_reason = reason
    revoke_portal_links(ctx, "offer", o.id)
    if ctx.user is not None:
        from . import approval_engine
        approval_engine.close_request(ctx.db, ctx.company_id, OFFER_DOCTYPE, o.id, ctx.user, "cancelled", reason)
    record(ctx, "offer", o.id, "Offer withdrawn", old=st, new="withdrawn", application=a, comments=reason)
    if by_candidate:
        return
    if st in ("sent", "viewed"):
        a.closed_reason = "offer_withdrawn"
        a.previous_status = "offer"
        rw._move(ctx, a, "closed", "Application closed -- offer withdrawn", reason)
        email_candidate(ctx, a.candidate, f"Offer withdrawn - {o.designation or a.opening.title}", "Offer withdrawn",
                        f"We are writing to let you know that the offer for {o.designation or a.opening.title} "
                        "has been withdrawn.", related=("recruitment_offer", o.id))
    else:
        rw._move(ctx, a, "selected", "Draft offer withdrawn -- back to Selected", reason)


def expire_offer(ctx: Ctx, o: models.Offer) -> None:
    st = offer_status(o)
    if st not in ("sent", "viewed"):
        return
    a = o.application
    o.status = "expired"
    revoke_portal_links(ctx, "offer", o.id)
    record(ctx, "offer", o.id, "Offer expired (no response by the expiry date)", old=st, new="expired", application=a,
           meta={"expiry_date": _iso(o.expiry_date)})
    if rw.app_status(a) == "offer":
        a.closed_reason = "offer_expired"
        a.previous_status = "offer"
        rw._move(ctx, a, "closed", "Application closed -- offer expired")
    notify(ctx, rw.recruiter_user_ids(ctx, a) + ([o.submitted_by] if o.submitted_by else []), "Offer expired",
           f"The offer for {a.candidate.name} expired without a response.", "recruitment_offer", o.id, email=False)


def expire_due_offers(db: Session, company_id: uuid.UUID) -> int:
    """Sent / viewed offers past their expiry date -> expired (scheduler +
    every offer list read)."""
    due = db.scalars(
        select(models.Offer).join(models.JobApplication, models.JobApplication.id == models.Offer.application_id)
        .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
        .where(models.JobOpening.company_id == company_id, models.Offer.status.in_(("sent", "viewed")),
               models.Offer.expiry_date.is_not(None), models.Offer.expiry_date < datetime.date.today())
    ).all()
    if not due:
        return 0
    ctx = rw.system_ctx(db, company_id)
    for o in due:
        expire_offer(ctx, o)
    db.flush()
    return len(due)


def on_application_withdrawn(ctx: Ctx, a: models.JobApplication) -> None:
    """Candidate withdrew: close any active offer and preboarding."""
    o = active_offer(ctx, a)
    if o is not None:
        withdraw_offer(ctx, o, a.withdrawal_reason or "Candidate withdrew the application", by_candidate=True)
    pb = ctx.db.scalar(select(models.Preboarding).where(models.Preboarding.application_id == a.id))
    if pb is not None and pb.status not in ("completed", "cancelled"):
        if pb.employee_id:
            raise err(409, "The employee record was already created for this candidate.")
        old = pb.status
        pb.status = "cancelled"
        pb.cancel_reason = a.withdrawal_reason or "Candidate withdrew"
        revoke_portal_links(ctx, "preboarding", pb.id)
        record(ctx, "preboarding", pb.id, "Preboarding cancelled -- candidate withdrew", old=old, new="cancelled",
               application=a)


# ═══════════════════════════════════════════════════════════════════════════
# Preboarding / New Joiner
# ═══════════════════════════════════════════════════════════════════════════

JOINER_FIELDS = {
    "personal": ("first_name", "last_name", "gender", "date_of_birth", "blood_group", "marital_status", "nationality", "pan"),
    "contact": ("personal_email", "personal_phone", "current_address", "permanent_address"),
    "emergency": ("name", "relation", "phone"),
    "bank": ("bank_name", "account_no", "ifsc", "account_holder"),
    "education": ("qualification", "institute", "specialization", "year_of_passing"),
    "employment": ("previous_employer", "experience_years"),
}
_GROUP_REQUIRED = {
    "personal": ("first_name", "date_of_birth", "gender"),
    "contact": ("personal_phone", "current_address"),
    "emergency": ("name", "phone"),
    "bank": ("account_no", "ifsc", "account_holder"),
}
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def get_preboarding(ctx: Ctx, pb_id, *, lock: bool = False) -> models.Preboarding:
    q = select(models.Preboarding).where(models.Preboarding.id == uuid.UUID(str(pb_id)))
    if lock:
        q = q.with_for_update()
    p = ctx.db.scalar(q)
    if p is None or p.company_id != ctx.company_id:
        raise err(404, "Preboarding record not found.")
    return p


def start_preboarding(ctx: Ctx, a: models.JobApplication, o: models.Offer) -> models.Preboarding:
    p = ctx.db.scalar(select(models.Preboarding).where(models.Preboarding.application_id == a.id).with_for_update())
    name_parts = (a.candidate.name or "").split()
    details = {"personal": {"first_name": name_parts[0] if name_parts else a.candidate.name,
                            "last_name": " ".join(name_parts[1:]) or None},
               "contact": {"personal_email": a.candidate.email, "personal_phone": a.candidate.phone},
               "education": {"qualification": a.candidate.qualification},
               "employment": {"previous_employer": a.candidate.current_company,
                              "experience_years": str(a.candidate.years_experience) if a.candidate.years_experience is not None else None}}
    fresh = p is None
    if p is None:
        p = models.Preboarding(id=uuid.uuid4(), company_id=ctx.company_id, application_id=a.id,
                               candidate_id=a.candidate_id, joiner_details=details, created_at=now(),
                               created_by=ctx.user.id if ctx.user is not None else None)
        ctx.db.add(p)
    old = None if fresh else p.status
    p.offer_id = o.id
    p.status = "not_started"
    p.joining_date = o.proposed_joining_date
    p.designation = o.designation or a.opening.title
    p.department_id = o.department_id or a.opening.department_id
    p.branch_id = o.branch_id or a.opening.branch_id
    p.reporting_manager_id = o.reporting_manager_id or a.opening.reporting_manager_id
    p.employment_type = o.employment_type or a.opening.employment_type
    p.work_mode = o.work_mode or a.opening.work_mode
    p.joining_status = None
    p.cancel_reason = p.no_show_reason = None
    p.verification_status = "pending"
    p.started_at = None
    ctx.db.flush()
    if fresh or not p.tasks:
        for i, item in enumerate(rw.get_settings(ctx.db, ctx.company_id)["preboarding_checklist"]):
            ctx.db.add(models.PreboardingTask(
                id=uuid.uuid4(), preboarding_id=p.id, category=item["category"], name=item["name"],
                task_type=item["task_type"], field_group=item.get("field_group"), required=bool(item.get("required", True)),
                status="pending" if item["task_type"] == "verification" else "not_started", sort_order=i,
                due_date=o.proposed_joining_date, created_at=now()))
        ctx.db.flush()
        ctx.db.refresh(p)
    _autocomplete_info_tasks(ctx, p)
    record(ctx, "preboarding", p.id, "Preboarding record created" if fresh else "Preboarding reactivated",
           old=old, new="not_started", application=a)
    return p


def _task_done(t: models.PreboardingTask) -> bool:
    return t.status in DOC_DONE


def checklist(ctx: Ctx, p: models.Preboarding) -> list[dict]:
    tasks = p.tasks
    offer = ctx.db.get(models.Offer, p.offer_id) if p.offer_id else None
    docs_ok = all(_task_done(t) for t in tasks if t.required and t.task_type != "verification")
    ver_ok = p.verification_override or all(t.status == "verified" for t in tasks if t.required and t.task_type == "verification")
    return [
        {"key": "offer_accepted", "label": "Offer accepted", "ok": offer is not None and offer_status(offer) == "accepted"},
        {"key": "documents", "label": "Required documents & details complete", "ok": docs_ok},
        {"key": "verification", "label": "Required verification complete", "ok": bool(ver_ok)},
        {"key": "joining_date", "label": "Joining date confirmed", "ok": p.joining_date is not None},
        {"key": "reporting_manager", "label": "Reporting manager confirmed", "ok": p.reporting_manager_id is not None},
        {"key": "department", "label": "Department confirmed", "ok": p.department_id is not None},
        {"key": "designation", "label": "Designation confirmed", "ok": bool(p.designation)},
        {"key": "employment_type", "label": "Employment type confirmed", "ok": bool(p.employment_type)},
        {"key": "work_location", "label": "Work location confirmed", "ok": p.branch_id is not None},
    ]


def recompute(ctx: Ctx, p: models.Preboarding) -> None:
    ver = [t for t in p.tasks if t.task_type == "verification"]
    if any(t.status == "failed" for t in ver):
        vs = "failed"
    elif p.verification_override or all(t.status == "verified" for t in ver if t.required):
        vs = "verified"
    elif any(t.status in ("in_progress", "needs_clarification", "verified") for t in ver):
        vs = "in_progress"
    else:
        vs = "pending"
    p.verification_status = vs
    if p.status in ("completed", "cancelled", "failed"):
        return
    if p.started_at is None:
        p.status = "not_started"
        return
    old = p.status
    if not all(_task_done(t) for t in p.tasks if t.required and t.task_type != "verification"):
        p.status = "documents_pending"
    elif vs != "verified":
        p.status = "verification_pending"
    else:
        p.status = "ready_to_join"
    if old != p.status:
        record(ctx, "preboarding", p.id, {"documents_pending": "Waiting for documents",
                                          "verification_pending": "Documents complete -- verification pending",
                                          "ready_to_join": "Ready to join"}[p.status],
               old=old, new=p.status, application=p.application)
        if p.status == "ready_to_join":
            notify(ctx, rw.recruiter_user_ids(ctx, p.application), "New joiner ready to join",
                   f"{p.candidate.name}: all preboarding items are complete -- confirm the joining.",
                   "recruitment_preboarding", p.id, email=False)


def _mask(v: str | None, keep: int = 4) -> str | None:
    if not v:
        return v
    return "•" * max(0, len(v) - keep) + v[-keep:]


def preboarding_summary(ctx: Ctx, p: models.Preboarding) -> dict:
    tasks = p.tasks
    req = [t for t in tasks if t.required]
    docs = [t for t in req if t.task_type == "document"]
    ver = [t for t in req if t.task_type == "verification"]
    other = [t for t in req if t.task_type in ("information", "acknowledgement")]
    return {
        "id": str(p.id), "application_id": str(p.application_id), "candidate_id": str(p.candidate_id),
        "candidate_name": p.candidate.name, "candidate_email": p.candidate.email,
        "opening_title": p.application.opening.title if p.application and p.application.opening else None,
        "offer_id": str(p.offer_id) if p.offer_id else None, "status": p.status,
        "designation": p.designation,
        "department_id": str(p.department_id) if p.department_id else None,
        "department_name": rw._name_of(ctx.db, models.Department, p.department_id),
        "branch_id": str(p.branch_id) if p.branch_id else None,
        "branch_name": rw._name_of(ctx.db, models.Branch, p.branch_id),
        "reporting_manager": _emp_ref(ctx.db, p.reporting_manager_id),
        "employment_type": p.employment_type, "work_mode": p.work_mode,
        "joining_date": _iso(p.joining_date), "joining_status": p.joining_status,
        "verification_status": p.verification_status, "verification_override": p.verification_override,
        "employee_status": p.employee_status, "employee_id": str(p.employee_id) if p.employee_id else None,
        "employee_error": p.employee_error,
        "progress": {
            "tasks_done": sum(1 for t in other if _task_done(t)), "tasks_total": len(other),
            "documents_done": sum(1 for t in docs if _task_done(t)), "documents_total": len(docs),
            "verification_done": sum(1 for t in ver if t.status == "verified"), "verification_total": len(ver),
        },
        "started_at": _iso(p.started_at), "completed_at": _iso(p.completed_at),
    }


def preboarding_actions(ctx: Ctx, p: models.Preboarding) -> list[str]:
    if not ctx.can_edit:
        return []
    st = p.status
    acts = []
    if st in ("completed", "cancelled"):
        return acts
    if st != "failed":
        acts.append("send_tasks")
    if st == "failed":
        acts += ["reinitiate", "reject_candidate"]
        if ctx.can_admin and rw.get_settings(ctx.db, ctx.company_id)["allow_verification_override"]:
            acts.append("override_verification")
    if st == "ready_to_join" and p.joining_status in (None,) and all(c["ok"] for c in checklist(ctx, p)):
        acts.append("confirm_joining")
    if p.joining_status == "confirmed":
        acts.append("create_employee")
        if p.joining_date and p.joining_date <= datetime.date.today():
            acts.append("no_show")
    if p.joining_status == "no_show":
        acts += ["reschedule_joining", "close_no_show"]
    elif p.joining_status in (None, "confirmed"):
        acts.append("reschedule_joining")
    acts.append("cancel_joining")
    return acts


def task_out(ctx: Ctx, t: models.PreboardingTask) -> dict:
    docs = t.documents
    return {
        "id": str(t.id), "category": t.category, "name": t.name, "task_type": t.task_type,
        "field_group": t.field_group, "required": t.required, "status": t.status, "due_date": _iso(t.due_date),
        "notes": t.notes, "review_notes": t.review_notes, "completed_at": _iso(t.completed_at),
        "documents": [{
            "id": str(d.id), "version": d.version, "filename": d.original_filename, "size": d.file_size,
            "status": d.status, "review_notes": d.review_notes, "uploaded_by": d.uploaded_by_name,
            "uploaded_via": d.uploaded_via, "uploaded_at": _iso(d.uploaded_at),
            "reviewed_by": d.reviewed_by_name, "reviewed_at": _iso(d.reviewed_at),
        } for d in sorted(docs, key=lambda d: d.version, reverse=True)],
    }


def preboarding_out(ctx: Ctx, p: models.Preboarding) -> dict:
    out = preboarding_summary(ctx, p)
    details = dict(p.joiner_details or {})
    if not ctx.can_edit:
        bank = dict(details.get("bank") or {})
        if bank.get("account_no"):
            bank["account_no"] = _mask(bank["account_no"])
        details["bank"] = bank
        pers = dict(details.get("personal") or {})
        if pers.get("pan"):
            pers["pan"] = _mask(pers["pan"])
        details["personal"] = pers
    offer = ctx.db.get(models.Offer, p.offer_id) if p.offer_id else None
    out.update({
        "joiner_details": details, "tasks": [task_out(ctx, t) for t in p.tasks],
        "checklist": checklist(ctx, p), "actions": preboarding_actions(ctx, p),
        "offer": offer_out(ctx, offer) if offer else None,
        "joining_confirmed_at": _iso(p.joining_confirmed_at), "joining_notes": p.joining_notes,
        "cancel_reason": p.cancel_reason, "no_show_reason": p.no_show_reason, "override_reason": p.override_reason,
        "history": rw.history_for(ctx, entity_type="preboarding", entity_id=p.id),
        "work_email_suggestion": _work_email_suggestion(ctx, p),
    })
    return out


def _work_email_suggestion(ctx: Ctx, p: models.Preboarding) -> str | None:
    company = ctx.db.get(models.Company, ctx.company_id)
    domain = None
    for e in ctx.db.scalars(select(models.Employee.work_email).where(models.Employee.company_id == ctx.company_id).limit(20)).all():
        if e and "@" in e:
            domain = e.split("@", 1)[1]
            break
    if not domain:
        return None
    pers = (p.joiner_details or {}).get("personal") or {}
    first = re.sub(r"[^a-z]", "", (pers.get("first_name") or "").lower())
    last = re.sub(r"[^a-z]", "", (pers.get("last_name") or "").lower())
    local = f"{first}.{last}" if first and last else (first or last)
    _ = company
    return f"{local}@{domain}" if local else None


def list_preboardings(ctx: Ctx, *, status: list[str] | None = None, search: str | None = None,
                      joining_from=None, joining_to=None, limit: int = 50, offset: int = 0) -> dict:
    ctx.require_view()
    q = (select(models.Preboarding).join(models.Candidate, models.Candidate.id == models.Preboarding.candidate_id)
         .where(models.Preboarding.company_id == ctx.company_id))
    if status:
        q = q.where(models.Preboarding.status.in_(status))
    if search and search.strip():
        q = q.where(models.Candidate.name.ilike(f"%{search.strip()}%"))
    if joining_from:
        q = q.where(models.Preboarding.joining_date >= joining_from)
    if joining_to:
        q = q.where(models.Preboarding.joining_date <= joining_to)
    total = ctx.db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = ctx.db.scalars(q.order_by(models.Preboarding.joining_date.asc().nulls_last()).limit(limit).offset(offset)).all()
    return {"items": [{**preboarding_summary(ctx, p), "actions": preboarding_actions(ctx, p)} for p in rows], "total": total}


def _require_active(p: models.Preboarding) -> None:
    if p.status in ("completed", "cancelled"):
        raise err(409, f"This preboarding record is {p.status}.")


def send_tasks(ctx: Ctx, p: models.Preboarding, data: dict) -> dict:
    """Start preboarding: request the pending items from the candidate
    (email with a secure upload link)."""
    ctx.require_edit()
    _require_active(p)
    if p.status == "failed":
        raise err(409, "Verification failed -- re-initiate or decide first.")
    first = p.started_at is None
    p.started_at = p.started_at or now()
    requested = []
    for t in p.tasks:
        if t.task_type != "verification" and not _task_done(t) and t.status in ("not_started", "requested", "resubmission_required"):
            if t.status == "not_started":
                t.status = "requested"
            requested.append(t)
    recompute(ctx, p)
    result = {"emailed": False, "requested": len(requested)}
    if data.get("email", True) and p.candidate.email:
        expires = _end_of((p.joining_date or datetime.date.today()) + datetime.timedelta(days=14)) \
            if (p.joining_date or datetime.date.today()) >= datetime.date.today() else now() + datetime.timedelta(days=30)
        link = create_portal_link(ctx, "preboarding", p.id, expires)
        items = "\n".join(f"- {t.name}{'' if t.required else ' (optional)'}" for t in requested) or "- (nothing pending)"
        email_candidate(ctx, p.candidate, f"Welcome aboard - joining formalities for {p.designation}",
                        "Complete your joining formalities",
                        ((data.get("message") or "").strip() or
                         f"Congratulations again on accepting our offer for {p.designation}. Please provide the "
                         "following before your joining date:") + "\n\n" + items,
                        details={"Joining date": p.joining_date.strftime("%d %b %Y") if p.joining_date else None},
                        cta=("Upload documents", link), related=("recruitment_preboarding", p.id))
        result = {"emailed": True, "requested": len(requested), "recipient": p.candidate.email}
    record(ctx, "preboarding", p.id, "Preboarding started -- tasks sent to candidate" if first else "Pending tasks re-sent",
           old=None, new=p.status, application=p.application, meta={"items": [t.name for t in requested],
                                                                  "emailed": result["emailed"]})
    return result


def update_joiner_details(ctx: Ctx, p: models.Preboarding, details: dict, *, via: str = "hr") -> None:
    if via == "hr":
        ctx.require_edit()
    _require_active(p)
    merged = {k: dict(v) for k, v in (p.joiner_details or {}).items()}
    for group, fields in JOINER_FIELDS.items():
        if group not in (details or {}):
            continue
        section = merged.setdefault(group, {})
        for f in fields:
            if f in details[group]:
                v = details[group][f]
                v = v.strip() if isinstance(v, str) else v
                if group == "personal" and f == "pan" and v:
                    v = v.upper()
                    if not _PAN_RE.match(v):
                        raise err(422, "PAN must look like ABCDE1234F.")
                if group == "bank" and f == "ifsc" and v:
                    v = v.upper()
                    if not _IFSC_RE.match(v):
                        raise err(422, "IFSC must look like HDFC0001234.")
                if group == "personal" and f == "date_of_birth" and v:
                    dob = rw._parse_date(v)
                    if dob >= datetime.date.today():
                        raise err(422, "Date of birth must be in the past.")
                    v = dob.isoformat()
                section[f] = v if v not in ("",) else None
    p.joiner_details = merged
    _autocomplete_info_tasks(ctx, p)
    record(ctx, "preboarding", p.id, "Joiner details updated" + (" by candidate" if via == "portal" else ""),
           application=p.application, meta={"sections": sorted((details or {}).keys())})
    recompute(ctx, p)


def _autocomplete_info_tasks(ctx: Ctx, p: models.Preboarding) -> None:
    details = p.joiner_details or {}
    for t in p.tasks:
        if t.task_type != "information" or not t.field_group or t.field_group not in _GROUP_REQUIRED:
            continue
        section = details.get(t.field_group) or {}
        complete = all(section.get(f) for f in _GROUP_REQUIRED[t.field_group])
        if complete and t.status != "completed":
            t.status = "completed"
            t.completed_at = now()
            t.completed_by = ctx.user.id if ctx.user is not None else None
        elif not complete and t.status == "completed":
            t.status = "requested" if p.started_at else "not_started"
            t.completed_at = None


def get_task(ctx: Ctx, task_id) -> models.PreboardingTask:
    t = ctx.db.get(models.PreboardingTask, uuid.UUID(str(task_id)))
    if t is None or t.preboarding.company_id != ctx.company_id:
        raise err(404, "Preboarding task not found.")
    return t


def add_task(ctx: Ctx, p: models.Preboarding, data: dict) -> models.PreboardingTask:
    ctx.require_edit()
    _require_active(p)
    item = rw.normalize_checklist([data])[0]
    t = models.PreboardingTask(
        id=uuid.uuid4(), preboarding_id=p.id, category=item["category"], name=item["name"], task_type=item["task_type"],
        field_group=item["field_group"], required=item["required"],
        status="pending" if item["task_type"] == "verification" else ("requested" if p.started_at else "not_started"),
        sort_order=max([x.sort_order for x in p.tasks] or [0]) + 1, due_date=rw._parse_date(data.get("due_date")) or p.joining_date,
        notes=data.get("notes"), created_at=now())
    ctx.db.add(t)
    ctx.db.flush()
    ctx.db.refresh(p)
    record(ctx, "preboarding", p.id, f"Task added: {t.name}", application=p.application)
    recompute(ctx, p)
    return t


def update_task(ctx: Ctx, t: models.PreboardingTask, data: dict) -> None:
    """HR: required flag / due date / notes; status of information,
    acknowledgement and verification tasks (documents move through
    upload + review)."""
    ctx.require_edit()
    p = t.preboarding
    _require_active(p)
    if "required" in data and data["required"] is not None:
        t.required = bool(data["required"])
    if "due_date" in data:
        t.due_date = rw._parse_date(data["due_date"])
    if "notes" in data:
        t.notes = data["notes"]
    new = data.get("status")
    reason = (data.get("reason") or "").strip() or None
    if new and new != t.status:
        old = t.status
        if t.task_type == "document":
            raise err(409, "Document tasks change status by uploading and reviewing documents.")
        if t.task_type in ("information", "acknowledgement"):
            if new not in ("requested", "completed"):
                raise err(422, "Status must be requested or completed.")
        else:
            if new not in rw_verification_states():
                raise err(422, "Unknown verification status.")
            if new in ("failed", "needs_clarification") and not reason:
                raise err(422, "A reason is required.")
            if p.status == "failed" and new != "failed":
                raise err(409, "Verification failed -- use Re-initiate first.")
        t.status = new
        t.review_notes = reason or t.review_notes
        if new in DOC_DONE:
            t.completed_at = now()
            t.completed_by = ctx.user.id
        record(ctx, "preboarding", p.id,
               f"{t.name}: {new.replace('_', ' ')}" if t.task_type == "verification" else f"{t.name} marked {new}",
               old=old, new=new, comments=reason, application=p.application)
        if t.task_type == "verification" and new == "failed":
            _on_verification_failed(ctx, p, t, reason)
            return
        if t.task_type == "verification" and new in ("verified", "needs_clarification"):
            notify(ctx, rw.recruiter_user_ids(ctx, p.application), f"Verification: {t.name} {new.replace('_', ' ')}",
                   f"{p.candidate.name}: {reason or new.replace('_', ' ')}", "recruitment_preboarding", p.id, email=False)
    recompute(ctx, p)


def rw_verification_states() -> tuple:
    return VERIFICATION_STATES


def _on_verification_failed(ctx: Ctx, p: models.Preboarding, t: models.PreboardingTask, reason: str) -> None:
    policy = rw.get_settings(ctx.db, ctx.company_id)["verification_failure_policy"]
    old = p.status
    a = p.application
    recompute(ctx, p)
    if policy == "reject":
        p.status = "cancelled"
        p.cancel_reason = f"Verification failed: {t.name} -- {reason}"
        revoke_portal_links(ctx, "preboarding", p.id)
        record(ctx, "preboarding", p.id, "Verification failed -- candidate rejected (policy)", old=old, new="cancelled",
               comments=reason, application=a)
        a.previous_status = "preboarding"
        a.rejection_reason = "Verification failed"
        a.rejection_notes = f"{t.name}: {reason}"
        rw._move(ctx, a, "rejected", "Candidate rejected -- verification failed", reason)
    else:
        p.status = "failed"
        record(ctx, "preboarding", p.id, "Verification failed -- on hold for review (policy)", old=old, new="failed",
               comments=reason, application=a)
    notify(ctx, rw.recruiter_user_ids(ctx, a), "Verification failed",
           f"{p.candidate.name}: {t.name} failed -- {reason}", "recruitment_preboarding", p.id)


def reinitiate_verification(ctx: Ctx, p: models.Preboarding, reason: str | None) -> None:
    ctx.require_edit()
    if p.status != "failed":
        raise err(409, "Only a failed verification can be re-initiated.")
    if not (reason or "").strip():
        raise err(422, "Give a reason for re-initiating verification.")
    for t in p.tasks:
        if t.task_type == "verification" and t.status == "failed":
            t.status = "in_progress"
    p.status = "verification_pending"
    record(ctx, "preboarding", p.id, "Verification re-initiated", old="failed", new="verification_pending",
           comments=reason, application=p.application)
    recompute(ctx, p)


def override_verification(ctx: Ctx, p: models.Preboarding, reason: str | None) -> None:
    ctx.require_admin()
    _require_active(p)
    if not rw.get_settings(ctx.db, ctx.company_id)["allow_verification_override"]:
        raise err(403, "Verification override is disabled in Recruitment settings.")
    if not (reason or "").strip():
        raise err(422, "An override reason is required (it is kept in the audit trail).")
    old = p.status
    for t in p.tasks:
        if t.task_type == "verification" and t.status != "verified":
            t.review_notes = f"Overridden by {ctx.actor_name}: {reason}"
    p.verification_override = True
    p.override_reason = reason
    if p.status == "failed":
        p.status = "verification_pending"
    record(ctx, "preboarding", p.id, "Verification overridden (authorized)", old=old, new=p.status, comments=reason,
           application=p.application)
    recompute(ctx, p)


def reject_candidate_from_preboarding(ctx: Ctx, p: models.Preboarding, reason: str | None) -> None:
    ctx.require_edit()
    _require_active(p)
    if not (reason or "").strip():
        raise err(422, "A rejection reason is required.")
    if p.employee_id:
        raise err(409, "The employee record already exists.")
    a = p.application
    old = p.status
    p.status = "cancelled"
    p.cancel_reason = reason
    revoke_portal_links(ctx, "preboarding", p.id)
    record(ctx, "preboarding", p.id, "Preboarding closed -- candidate rejected", old=old, new="cancelled", comments=reason,
           application=a)
    a.previous_status = "preboarding"
    a.rejection_reason = reason[:200]
    rw._move(ctx, a, "rejected", "Candidate rejected during preboarding", reason)


# ── documents ───────────────────────────────────────────────────────────────

def upload_document(ctx: Ctx, t: models.PreboardingTask, upload: UploadFile, *, via: str = "hr",
                    uploader_name: str | None = None) -> models.PreboardingDocument:
    from .storage import save_uploaded_file

    if via == "hr":
        ctx.require_edit()
    p = t.preboarding
    _require_active(p)
    if p.status == "failed" and via != "hr":
        raise err(409, "Uploads are paused while HR reviews your verification.")
    if t.task_type != "document":
        raise err(409, "This task doesn't take a document.")
    if t.status in ("approved", "completed"):
        raise err(409, "This document is already approved.")
    latest = max(t.documents, key=lambda d: d.version) if t.documents else None
    if latest is not None and latest.status == "under_review" and via != "hr":
        raise err(409, "The previous upload is still being reviewed.")
    file_url, size = save_uploaded_file(upload, entity_type="preboarding_document", entity_id=p.id)
    if latest is not None and latest.status == "under_review":
        latest.status = "rejected"
        latest.review_notes = "Replaced by a newer upload"
    doc = models.PreboardingDocument(
        id=uuid.uuid4(), task_id=t.id, preboarding_id=p.id, version=(latest.version if latest else 0) + 1,
        file_url=file_url, original_filename=Path(upload.filename or "document").name[:255],
        content_type=upload.content_type, file_size=size, status="under_review",
        uploaded_by=ctx.user.id if ctx.user is not None else None,
        uploaded_by_name=uploader_name or ctx.actor_name, uploaded_via=via, uploaded_at=now())
    ctx.db.add(doc)
    old = t.status
    t.status = "uploaded"
    ctx.db.flush()
    ctx.db.refresh(t)
    record(ctx, "preboarding", p.id, f"Document uploaded: {t.name} (v{doc.version})", old=old, new="uploaded",
           application=p.application, meta={"via": via, "file": doc.original_filename},
           actor_name=uploader_name, actor_user_id=True if via == "hr" else None)
    if via != "hr":
        notify(ctx, rw.recruiter_user_ids(ctx, p.application), "Document uploaded by candidate",
               f"{p.candidate.name} uploaded {t.name} -- review it.", "recruitment_preboarding", p.id, email=False)
    recompute(ctx, p)
    return doc


def get_document(ctx: Ctx, doc_id) -> models.PreboardingDocument:
    d = ctx.db.get(models.PreboardingDocument, uuid.UUID(str(doc_id)))
    if d is None or d.task.preboarding.company_id != ctx.company_id:
        raise err(404, "Document not found.")
    return d


def review_document(ctx: Ctx, d: models.PreboardingDocument, decision: str, notes: str | None) -> None:
    ctx.require_edit()
    t = d.task
    p = t.preboarding
    _require_active(p)
    latest = max(t.documents, key=lambda x: x.version)
    if d.id != latest.id:
        raise err(409, "Only the latest upload can be reviewed.")
    if d.status != "under_review":
        raise err(409, f"This document was already {d.status.replace('_', ' ')}.")
    if decision not in ("approve", "reject"):
        raise err(422, "Decision must be approve or reject.")
    if decision == "reject" and not (notes or "").strip():
        raise err(422, "Say why the document is rejected so the candidate can fix it.")
    d.reviewed_by = ctx.user.id
    d.reviewed_by_name = ctx.actor_name
    d.reviewed_at = now()
    d.review_notes = notes
    old = t.status
    if decision == "approve":
        d.status = "approved"
        t.status = "approved"
        t.completed_at = now()
        t.completed_by = ctx.user.id
        record(ctx, "preboarding", p.id, f"Document approved: {t.name} (v{d.version})", old=old, new="approved",
               comments=notes, application=p.application)
    else:
        d.status = "rejected"
        t.status = "resubmission_required"
        t.review_notes = notes
        record(ctx, "preboarding", p.id, f"Document rejected: {t.name} (v{d.version}) -- resubmission required",
               old=old, new="resubmission_required", comments=notes, application=p.application)
        if p.candidate.email:
            link = create_portal_link(ctx, "preboarding", p.id, now() + datetime.timedelta(days=30))
            email_candidate(ctx, p.candidate, f"Please re-upload: {t.name}", "A document needs to be uploaded again",
                            f"The {t.name} you provided could not be accepted: {notes}\n\nPlease upload it again.",
                            cta=("Upload again", link), related=("recruitment_preboarding", p.id))
    recompute(ctx, p)


def document_path(d: models.PreboardingDocument) -> Path:
    from .storage import uploads_root, winlong_path

    return winlong_path(uploads_root() / d.file_url.removeprefix("/media/"))


# ── joining ─────────────────────────────────────────────────────────────────

def confirm_joining(ctx: Ctx, p: models.Preboarding, data: dict) -> None:
    ctx.require_edit()
    _require_active(p)
    if p.joining_status == "confirmed":
        raise err(409, "Joining is already confirmed.")
    if data.get("joining_date"):
        p.joining_date = rw._parse_date(data["joining_date"])
    if "reporting_manager_id" in data and data["reporting_manager_id"]:
        p.reporting_manager_id = rw.employee_in_company(ctx, data["reporting_manager_id"], "Reporting manager").id
    if data.get("department_id"):
        p.department_id = rw.department_in_company(ctx, data["department_id"]).id
    if data.get("branch_id"):
        p.branch_id = rw.branch_in_company(ctx, data["branch_id"]).id
    if data.get("designation"):
        p.designation = data["designation"].strip()
    if data.get("employment_type"):
        p.employment_type = rw.normalize_employment_type(data["employment_type"])
    if data.get("work_mode"):
        if data["work_mode"] not in rw.WORK_MODES:
            raise err(422, "Unknown work mode.")
        p.work_mode = data["work_mode"]
    recompute(ctx, p)
    if p.status != "ready_to_join":
        raise err(409, "Complete the required documents and verification first "
                       f"(currently {p.status.replace('_', ' ')}).")
    missing = [c["label"] for c in checklist(ctx, p) if not c["ok"]]
    if missing:
        raise err(409, "Joining checklist incomplete: " + "; ".join(missing) + ".")
    p.joining_status = "confirmed"
    p.joining_confirmed_at = now()
    p.joining_confirmed_by = ctx.user.id
    p.joining_notes = data.get("notes")
    record(ctx, "preboarding", p.id, "Joining confirmed", old=None, new="confirmed", application=p.application,
           comments=data.get("notes"), meta={"joining_date": _iso(p.joining_date)})
    notify(ctx, rw.user_ids_for_employees(ctx.db, [p.reporting_manager_id]), "New joiner in your team",
           f"{p.candidate.name} joins as {p.designation} on {p.joining_date.strftime('%d %b %Y')}.",
           "recruitment_preboarding", p.id)


def reschedule_joining(ctx: Ctx, p: models.Preboarding, new_date, reason: str | None) -> None:
    ctx.require_edit()
    _require_active(p)
    if p.joining_status not in (None, "confirmed", "no_show"):
        raise err(409, "Joining can't be rescheduled now.")
    d = rw._parse_date(new_date)
    if d is None:
        raise err(422, "Pick the new joining date.")
    if not (reason or "").strip():
        raise err(422, "Give a reason for rescheduling.")
    old_date, old_status = p.joining_date, p.joining_status
    p.joining_date = d
    if p.joining_status == "no_show":
        p.joining_status = None
        p.no_show_reason = None
    record(ctx, "preboarding", p.id, "Joining date rescheduled", old=old_status, new=p.joining_status,
           comments=reason, application=p.application, meta={"from": _iso(old_date), "to": _iso(d)})


def cancel_joining(ctx: Ctx, p: models.Preboarding, reason: str | None) -> None:
    ctx.require_edit()
    _require_active(p)
    if not (reason or "").strip():
        raise err(422, "A cancellation reason is required.")
    if p.employee_id:
        raise err(409, "The employee record already exists.")
    old = p.status
    p.status = "cancelled"
    p.joining_status = "cancelled"
    p.cancel_reason = reason
    revoke_portal_links(ctx, "preboarding", p.id)
    record(ctx, "preboarding", p.id, "Joining cancelled", old=old, new="cancelled", comments=reason, application=p.application)
    a = p.application
    a.closed_reason = "joining_cancelled"
    a.previous_status = "preboarding"
    rw._move(ctx, a, "closed", "Application closed -- joining cancelled", reason)


def mark_no_show(ctx: Ctx, p: models.Preboarding, reason: str | None) -> None:
    ctx.require_edit()
    _require_active(p)
    if p.joining_status != "confirmed":
        raise err(409, "No-show applies to a confirmed joining.")
    if p.joining_date and p.joining_date > datetime.date.today():
        raise err(409, "The joining date hasn't arrived yet.")
    p.joining_status = "no_show"
    p.no_show_reason = (reason or "").strip() or "Did not report on the joining date"
    record(ctx, "preboarding", p.id, "Candidate did not join (no-show)", old="confirmed", new="no_show",
           comments=p.no_show_reason, application=p.application)
    notify(ctx, rw.recruiter_user_ids(ctx, p.application), "New joiner no-show",
           f"{p.candidate.name} did not join on {_iso(p.joining_date)}.", "recruitment_preboarding", p.id, email=False)


def close_no_show(ctx: Ctx, p: models.Preboarding, reason: str | None) -> None:
    ctx.require_edit()
    if p.joining_status != "no_show":
        raise err(409, "Only a no-show can be closed this way.")
    old = p.status
    p.status = "cancelled"
    p.cancel_reason = (reason or "").strip() or p.no_show_reason
    revoke_portal_links(ctx, "preboarding", p.id)
    record(ctx, "preboarding", p.id, "Closed -- no-show", old=old, new="cancelled", comments=p.cancel_reason,
           application=p.application)
    a = p.application
    a.closed_reason = "no_show"
    a.previous_status = "preboarding"
    rw._move(ctx, a, "closed", "Application closed -- candidate did not join", p.cancel_reason)


# ── Create Employee (the only People integration point) ─────────────────────

class EmployeeCreationFailed(Exception):
    def __init__(self, status: int, message, *, code: str = "employee_creation_failed", extra: dict | None = None):
        super().__init__(message if isinstance(message, str) else str(message))
        self.status, self.message, self.code, self.extra = status, message, code, extra or {}


def employee_prefill(ctx: Ctx, p: models.Preboarding) -> dict:
    d = p.joiner_details or {}
    pers, cont, emer = d.get("personal") or {}, d.get("contact") or {}, d.get("emergency") or {}
    bank, edu, emp = d.get("bank") or {}, d.get("education") or {}, d.get("employment") or {}
    offer = ctx.db.get(models.Offer, p.offer_id) if p.offer_id else None
    return {
        "first_name": pers.get("first_name"), "last_name": pers.get("last_name"),
        "work_email": _work_email_suggestion(ctx, p), "gender": pers.get("gender"),
        "date_of_birth": pers.get("date_of_birth"), "date_of_joining": _iso(p.joining_date),
        "employment_type": rw.EMPLOYMENT_TYPE_LABELS.get(p.employment_type or "full_time", "Full-time"),
        "status": "Probation" if offer is not None and offer.probation_months else "Active",
        "branch_name": rw._name_of(ctx.db, models.Branch, p.branch_id),
        "department_name": rw._name_of(ctx.db, models.Department, p.department_id),
        "designation_name": p.designation, "work_mode": p.work_mode,
        "annual_ctc": int(float(offer.offered_ctc)) if offer is not None and offer.offered_ctc and ctx.can_see_compensation else None,
        "reporting_manager_id": str(p.reporting_manager_id) if p.reporting_manager_id else None,
        "pan": pers.get("pan"), "bank_name": bank.get("bank_name"), "bank_account_no": bank.get("account_no"),
        "bank_ifsc": bank.get("ifsc"), "qualification": edu.get("qualification"), "institute": edu.get("institute"),
        "specialization": edu.get("specialization"),
        "year_of_passing": int(edu["year_of_passing"]) if str(edu.get("year_of_passing") or "").isdigit() else None,
        "previous_employer": emp.get("previous_employer"), "experience_years": emp.get("experience_years"),
        "emergency_contact_name": emer.get("name"), "emergency_contact_relation": emer.get("relation"),
        "emergency_contact_phone": emer.get("phone"), "blood_group": pers.get("blood_group"),
        "nationality": pers.get("nationality"), "marital_status": pers.get("marital_status"),
        "personal_email": cont.get("personal_email"), "personal_phone": cont.get("personal_phone"),
        "current_address": cont.get("current_address"), "permanent_address": cont.get("permanent_address"),
        "skills": [s.strip() for s in (p.candidate.skills or "").split(",") if s.strip()][:30],
        # Contract hires only -- the key is absent for every other joiner.
        **({"contract_end_date": _iso(offer.contract_end_date)}
           if offer is not None and offer.contract_end_date else {}),
        **({"contract_rate_amount": _num(offer.rate_amount), "contract_rate_unit": offer.rate_unit}
           if offer is not None and offer.compensation_type == "rate" and offer.rate_amount else {}),
    }


def _existing_employee_matches(ctx: Ctx, work_email: str | None, prefill: dict) -> list[dict]:
    conds = []
    if work_email:
        conds.append(func.lower(models.Employee.work_email) == work_email.lower())
    if prefill.get("personal_email"):
        conds.append(func.lower(models.Employee.personal_email) == prefill["personal_email"].lower()) \
            if hasattr(models.Employee, "personal_email") else None
    if prefill.get("pan") and hasattr(models.Employee, "pan"):
        conds.append(models.Employee.pan == prefill["pan"])
    conds = [c for c in conds if c is not None]
    if not conds:
        return []
    from sqlalchemy import or_
    rows = ctx.db.scalars(select(models.Employee).where(models.Employee.company_id == ctx.company_id, or_(*conds)).limit(5)).all()
    return [{"id": str(e.id), "name": crud._full_name(e), "code": e.employee_code, "work_email": e.work_email} for e in rows]


def create_employee(ctx: Ctx, p: models.Preboarding, data: dict) -> dict:
    """Creates the employee through crud.create_employee with the exact
    checks POST /api/employees applies (People 'create' access, role tier,
    field rules, email uniqueness, the company's employee-ID series).
    Idempotent: a second call returns the already-created employee."""
    from pydantic import ValidationError

    from . import role_tiers, schemas
    from .employee_validation import EmployeeInputError

    ctx.require_edit()
    if not crud.can_access_people_module(ctx.db, ctx.user, "create"):
        raise EmployeeCreationFailed(403, "You don't have permission to create employees (People > create).",
                                     code="forbidden")
    if p.employee_id:
        emp = ctx.db.get(models.Employee, p.employee_id)
        return {"employee_id": str(p.employee_id), "employee_code": emp.employee_code if emp else None,
                "already_created": True}
    if p.status == "cancelled" or p.joining_status != "confirmed":
        raise EmployeeCreationFailed(409, "Confirm the joining before creating the employee.", code="not_ready")
    a = p.application
    if rw.app_status(a) != "preboarding":
        raise EmployeeCreationFailed(409, f"The application is {rw.app_status(a)} -- no employee can be created.",
                                     code="not_ready")
    missing = [c["label"] for c in checklist(ctx, p) if not c["ok"]]
    if missing:
        raise EmployeeCreationFailed(409, "Joining checklist incomplete: " + "; ".join(missing) + ".", code="not_ready")

    if data.get("link_existing_employee_id"):
        ctx.require_admin()
        emp = rw.employee_in_company(ctx, data["link_existing_employee_id"], "Employee")
        _finish_hire(ctx, p, emp, linked=True)
        return {"employee_id": str(emp.id), "employee_code": emp.employee_code, "linked_existing": True}

    payload_data = {**employee_prefill(ctx, p), **{k: v for k, v in data.items()
                                                    if k not in ("link_existing_employee_id",) and v is not None}}
    # Not an EmployeeCreate field -- set on the new row below.
    contract_end_date = rw._parse_date(payload_data.pop("contract_end_date", None))
    contract_rate = (payload_data.pop("contract_rate_amount", None), payload_data.pop("contract_rate_unit", None))
    # The offer's designation was set deliberately on the requisition/offer,
    # so the hire step may add it to the catalog (L-10 applies to typed input).
    payload_data.setdefault("create_designation", True)
    if not ctx.can_see_compensation:
        payload_data.pop("annual_ctc", None)
    matches = _existing_employee_matches(ctx, payload_data.get("work_email"), payload_data)
    if matches and not data.get("ignore_matches"):
        raise EmployeeCreationFailed(409, "An existing employee matches this joiner (same email or PAN).",
                                     code="employee_exists", extra={"matches": matches})
    try:
        payload = schemas.EmployeeCreate(**payload_data)
    except ValidationError as exc:
        # Password policy failures carry their own clear message (never the password).
        policy = next((e["msg"] for e in exc.errors() if e["type"] == "password_policy"), None)
        if policy is not None:
            raise EmployeeCreationFailed(422, policy, code="validation", extra={"fields": ["password"]}) from exc
        whole = next((e["msg"].removeprefix("Value error, ") for e in exc.errors() if not e["loc"]), None)
        if whole is not None:  # cross-field rule (e.g. date of birth vs joining)
            raise EmployeeCreationFailed(422, whole, code="validation") from exc
        fields = sorted({".".join(str(x) for x in e["loc"]) for e in exc.errors()})
        raise EmployeeCreationFailed(422, "Missing or invalid employee fields: " + ", ".join(fields) + ".",
                                     code="validation", extra={"fields": fields}) from exc
    requested_role = crud.get_role_by_name(ctx.db, ctx.company_id, payload.role_name)
    if requested_role is None:
        raise EmployeeCreationFailed(422, f"Role '{payload.role_name}' not found.", code="validation")
    role_error = role_tiers.role_assignment_error(ctx.db, ctx.user, requested_role)
    if role_error is not None:
        raise EmployeeCreationFailed(403, role_error, code="forbidden")
    actor_role = crud.get_user_primary_role(ctx.db, ctx.user.id)
    field_errors = crud.validate_against_field_rules(ctx.db, ctx.company_id, "employee", payload.model_dump(),
                                                     role_id=actor_role.id if actor_role else None)
    if field_errors:
        raise EmployeeCreationFailed(422, field_errors, code="validation")
    try:
        with ctx.db.begin_nested():
            employee = crud.create_employee(ctx.db, ctx.company_id, payload)
            employee.created_by = ctx.user.id
            if contract_end_date is not None:
                employee.contract_end_date = contract_end_date
            if contract_rate[0] is not None:
                employee.contract_rate_amount, employee.contract_rate_unit = contract_rate
            crud.create_audit_log(ctx.db, ctx.company_id, ctx.user.id, "create", "employee", employee.id)
    except EmployeeInputError as exc:
        raise EmployeeCreationFailed(422, str(exc), code="validation") from exc
    except (ValueError, IntegrityError) as exc:
        msg = str(exc) if isinstance(exc, ValueError) else \
            "Could not create the employee -- the work email may already be in use. Please try again."
        p.employee_status = "failed"
        p.employee_error = msg[:1000]
        record(ctx, "preboarding", p.id, "Employee creation failed", old="ready", new="failed", comments=msg,
               application=a)
        raise EmployeeCreationFailed(409, msg) from exc
    _copy_documents(ctx, p, employee)
    _finish_hire(ctx, p, employee, linked=False)
    return {"employee_id": str(employee.id), "employee_code": employee.employee_code, "already_created": False}


def _finish_hire(ctx: Ctx, p: models.Preboarding, employee: models.Employee, *, linked: bool) -> None:
    a = p.application
    old = p.status
    p.employee_id = employee.id
    p.employee_status = "created"
    p.employee_error = None
    p.employee_created_at = now()
    p.status = "completed"
    p.joining_status = "completed"
    p.completed_at = now()
    a.employee_id = employee.id
    revoke_portal_links(ctx, "preboarding", p.id)
    record(ctx, "preboarding", p.id, "Existing employee linked" if linked else "Employee created", old=old,
           new="completed", application=a, meta={"employee_id": str(employee.id), "employee_code": employee.employee_code})
    rw._move(ctx, a, "hired", f"Hired -- employee {employee.employee_code or ''}".strip())
    hired = rw._application_count(ctx.db, a.opening_id, ["hired"])
    notify(ctx, rw.recruiter_user_ids(ctx, a) + rw.user_ids_for_employees(ctx.db, [p.reporting_manager_id]),
           "Employee created", f"{p.candidate.name} is now employee {employee.employee_code} ({p.designation}).",
           "recruitment_preboarding", p.id, email=False)
    if hired >= (a.opening.vacancies or 1) and rw.opening_status(a.opening) != "closed":
        notify(ctx, rw.recruiter_user_ids(ctx, a), "All positions filled",
               f"{a.opening.title}: {hired} of {a.opening.vacancies} hired -- you can close the opening.",
               "recruitment_opening", a.opening_id, email=False)


def _copy_documents(ctx: Ctx, p: models.Preboarding, employee: models.Employee) -> None:
    """Approved preboarding documents -> the employee's Documents
    (People > Documents), as copies (preboarding history stays intact)."""
    from .storage import uploads_root, winlong_path

    for t in p.tasks:
        approved = [d for d in t.documents if d.status == "approved"]
        if not approved:
            continue
        d = max(approved, key=lambda x: x.version)
        src = document_path(d)
        if not src.exists():
            continue
        rel = Path("employee_document") / str(employee.id) / f"{uuid.uuid4()}{Path(d.original_filename).suffix.lower()}"
        dest = winlong_path(uploads_root() / rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        crud.create_document_record(ctx.db, employee_id=employee.id, document_type=t.name[:80], status="Verified",
                                    uploaded_on=crud.company_today(ctx.db, ctx.company_id),
                                    file_url="/media/" + rel.as_posix())


# ═══════════════════════════════════════════════════════════════════════════
# Reports, To-Do
# ═══════════════════════════════════════════════════════════════════════════

def summary(ctx: Ctx, *, date_from=None, date_to=None, opening_id=None) -> dict:
    """Recruitment report numbers -- database aggregation only."""
    ctx.require_view()
    db = ctx.db
    expire_due_offers(db, ctx.company_id)
    cid = ctx.company_id
    app_q = (select(models.JobApplication.status, func.count())
             .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
             .where(models.JobOpening.company_id == cid, models.JobApplication.deleted_at.is_(None)))
    src_q = (select(func.coalesce(models.JobApplication.source, "Unspecified"), func.count())
             .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
             .where(models.JobOpening.company_id == cid, models.JobApplication.deleted_at.is_(None)))
    if opening_id:
        app_q = app_q.where(models.JobApplication.opening_id == opening_id)
        src_q = src_q.where(models.JobApplication.opening_id == opening_id)
    if date_from:
        app_q = app_q.where(models.JobApplication.applied_at >= date_from)
        src_q = src_q.where(models.JobApplication.applied_at >= date_from)
    if date_to:
        app_q = app_q.where(models.JobApplication.applied_at < date_to + datetime.timedelta(days=1))
        src_q = src_q.where(models.JobApplication.applied_at < date_to + datetime.timedelta(days=1))
    by_status = dict(db.execute(app_q.group_by(models.JobApplication.status)).all())
    by_source = dict(db.execute(src_q.group_by(func.coalesce(models.JobApplication.source, "Unspecified"))).all())
    req_counts = dict(db.execute(
        select(models.HiringRequisition.status, func.count())
        .join(models.Employee, models.Employee.id == models.HiringRequisition.requested_by)
        .where(models.Employee.company_id == cid).group_by(models.HiringRequisition.status)).all())
    open_counts = dict(db.execute(select(models.JobOpening.status, func.count()).where(
        models.JobOpening.company_id == cid, models.JobOpening.deleted_at.is_(None)).group_by(models.JobOpening.status)).all())
    offer_counts: dict = {}
    for st, n in db.execute(
            select(models.Offer.status, func.count()).join(models.JobApplication, models.JobApplication.id == models.Offer.application_id)
            .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
            .where(models.JobOpening.company_id == cid).group_by(models.Offer.status)).all():
        key = "declined" if (st or "").lower() == "rejected" else (st or "").lower()
        offer_counts[key] = offer_counts.get(key, 0) + n
    today = now()
    upcoming = db.scalar(select(func.count()).select_from(models.Interview)
                         .join(models.JobApplication, models.JobApplication.id == models.Interview.application_id)
                         .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
                         .where(models.JobOpening.company_id == cid, models.Interview.status == "scheduled",
                                models.Interview.scheduled_at >= today)) or 0
    feedback_pending = db.scalar(select(func.count()).select_from(models.Interview)
                                 .join(models.JobApplication, models.JobApplication.id == models.Interview.application_id)
                                 .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
                                 .where(models.JobOpening.company_id == cid, models.Interview.status == "feedback_pending")) or 0
    pb_counts = dict(db.execute(select(models.Preboarding.status, func.count()).where(
        models.Preboarding.company_id == cid).group_by(models.Preboarding.status)).all())
    new_joiners = db.scalar(select(func.count()).select_from(models.Preboarding).where(
        models.Preboarding.company_id == cid, models.Preboarding.status == "completed",
        models.Preboarding.completed_at >= today - datetime.timedelta(days=30))) or 0
    # Time to hire: applied -> hired (days), from the stage history.
    hired_rows = db.execute(
        select(models.JobApplication.applied_at, func.min(models.RecruitmentHistory.created_at))
        .join(models.RecruitmentHistory, models.RecruitmentHistory.application_id == models.JobApplication.id)
        .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
        .where(models.JobOpening.company_id == cid, models.RecruitmentHistory.new_status == "hired",
               models.RecruitmentHistory.entity_type == "application")
        .group_by(models.JobApplication.id, models.JobApplication.applied_at)).all()
    tth = [(h - s).total_seconds() / 86400 for s, h in hired_rows if s and h]
    # Average days in each stage (entered -> left), from the history.
    stage_rows = db.execute(
        select(models.RecruitmentHistory.application_id, models.RecruitmentHistory.new_status, models.RecruitmentHistory.created_at)
        .where(models.RecruitmentHistory.company_id == cid, models.RecruitmentHistory.entity_type == "application",
               models.RecruitmentHistory.new_status.is_not(None))
        .order_by(models.RecruitmentHistory.application_id, models.RecruitmentHistory.created_at)).all()
    spans: dict[str, list[float]] = {}
    prev: dict = {}
    for app_id, st, at in stage_rows:
        if app_id in prev and prev[app_id][0] != st:
            spans.setdefault(prev[app_id][0], []).append((at - prev[app_id][1]).total_seconds() / 86400)
        if app_id not in prev or prev[app_id][0] != st:
            prev[app_id] = (st, at)
    sent_total = sum(offer_counts.get(k, 0) for k in ("accepted", "declined", "expired"))
    return {
        "requisitions": {"open": req_counts.get("pending", 0) + req_counts.get("sent_back", 0) + req_counts.get("draft", 0),
                         "by_status": req_counts},
        "openings": {"open": open_counts.get("open", 0), "by_status": open_counts},
        "applications": {"total": sum(by_status.values()), "active": sum(v for k, v in by_status.items() if k in rw.APPLICATION_ACTIVE),
                         "by_status": by_status, "by_source": by_source,
                         "rejected": by_status.get("rejected", 0), "on_hold": by_status.get("on_hold", 0),
                         "withdrawn": by_status.get("withdrawn", 0), "hired": by_status.get("hired", 0)},
        "interviews": {"upcoming": upcoming, "feedback_pending": feedback_pending},
        "offers": {"by_status": offer_counts,
                   "acceptance_rate": round(100 * offer_counts.get("accepted", 0) / sent_total, 1) if sent_total else None},
        "preboarding": {"pending": sum(v for k, v in pb_counts.items() if k not in ("completed", "cancelled")),
                        "by_status": pb_counts, "new_joiners_30d": new_joiners},
        "time_to_hire_days": round(sum(tth) / len(tth), 1) if tth else None,
        "avg_days_in_stage": {k: round(sum(v) / len(v), 1) for k, v in spans.items() if v},
    }


def my_actions(ctx: Ctx) -> list[dict]:
    """Recruitment items waiting on the current user, each with the exact
    record to open (To Do). Requisition approvals already come through the
    Approvals inbox, so they are not repeated here."""
    db = ctx.db
    items: list[dict] = []
    emp = ctx.user.employee_id
    if emp is not None:
        q = (select(models.Interview).join(models.InterviewParticipant, models.InterviewParticipant.interview_id == models.Interview.id)
             .join(models.JobApplication, models.JobApplication.id == models.Interview.application_id)
             .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
             .where(models.JobOpening.company_id == ctx.company_id, models.InterviewParticipant.employee_id == emp,
                    or_(models.InterviewParticipant.attendance.is_(None),
                        models.InterviewParticipant.attendance != "declined"),
                    models.Interview.status.in_(("scheduled", "feedback_pending", "completed")),
                    models.Interview.scheduled_at <= now()))
        for i in db.scalars(q).all():
            if any(f.interviewer_id == emp for f in i.feedback_entries):
                continue
            items.append({"kind": "interview_feedback", "entity_type": "recruitment_interview", "entity_id": str(i.id),
                          "application_id": str(i.application_id),
                          "title": f"Interview feedback: {i.application.candidate.name}",
                          "subtitle": f"{i.stage_name or 'Interview'} · {i.application.opening.title}",
                          "due": _iso(i.scheduled_at)})
    # Interview invitations the caller organizes that need them next.
    org_q = (select(models.Interview)
             .join(models.JobApplication, models.JobApplication.id == models.Interview.application_id)
             .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
             .where(models.JobOpening.company_id == ctx.company_id,
                    models.Interview.status.in_(rw.INVITATION_PHASE + rw.BOOKED_INTERVIEW)))
    for i in db.scalars(org_q).all():
        a = i.application
        if ctx.user.id not in (i.created_by, rw.opening_creator_user_id(ctx, a.opening)):
            continue
        declined = [p for p in i.participants if p.attendance == "declined" and not p.replaced_by]
        base = {"entity_type": "recruitment_interview", "entity_id": str(i.id), "application_id": str(a.id)}
        sub = f"{i.stage_name or 'Interview'} · {a.opening.title}"
        if declined:
            items.append({**base, "kind": "interview_replace",
                          "title": f"Interviewer declined: {a.candidate.name}",
                          "subtitle": f"{', '.join(crud.employee_display_name(db, p.employee_id) for p in declined)}"
                                      f" -- invite someone else · {sub}", "due": _iso(max(p.responded_at or now() for p in declined))})
        if i.status == "ready_to_schedule":
            items.append({**base, "kind": "interview_schedule", "title": f"Schedule interview: {a.candidate.name}",
                          "subtitle": f"Interviewer accepted · {sub}", "due": _iso(i.created_at)})
        elif i.status == "reschedule_requested":
            items.append({**base, "kind": "interview_reschedule", "title": f"Reschedule requested: {a.candidate.name}",
                          "subtitle": sub, "due": _iso(i.scheduled_at)})
    # Round-based interviews: the organizer records the internal decision
    # once an interviewer has clicked End Interview.
    round_q = (select(models.Interview)
               .join(models.JobApplication, models.JobApplication.id == models.Interview.application_id)
               .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
               .where(models.JobOpening.company_id == ctx.company_id, models.Interview.round_id.is_not(None),
                      models.Interview.status == "ended"))
    for i in db.scalars(round_q).all():
        a = i.application
        if not rw.is_round_organizer(ctx, a.opening):
            continue
        items.append({"kind": "interview_round_ended", "entity_type": "recruitment_interview", "entity_id": str(i.id),
                      "application_id": str(a.id), "title": f"Record outcome: {a.candidate.name}",
                      "subtitle": f"{i.stage_name or 'Interview'} ended · {a.opening.title}", "due": _iso(i.ended_at)})
    offer_q = (select(models.Offer).join(models.JobApplication, models.JobApplication.id == models.Offer.application_id)
               .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
               .where(models.JobOpening.company_id == ctx.company_id, models.Offer.status == "approval_pending"))
    for o in db.scalars(offer_q).all():
        if can_decide_offer(ctx, o):
            items.append({"kind": "offer_approval", "entity_type": "recruitment_offer", "entity_id": str(o.id),
                          "application_id": str(o.application_id),
                          "title": f"Approve offer: {o.application.candidate.name}",
                          "subtitle": o.designation or o.application.opening.title, "due": _iso(o.submitted_at)})
    if ctx.can_edit:
        pb_q = select(models.Preboarding).where(models.Preboarding.company_id == ctx.company_id,
                                                models.Preboarding.status.notin_(("completed", "cancelled")))
        for p in db.scalars(pb_q).all():
            uploaded = [t for t in p.tasks if t.task_type == "document" and t.status == "uploaded"]
            if uploaded:
                items.append({"kind": "document_review", "entity_type": "recruitment_preboarding", "entity_id": str(p.id),
                              "application_id": str(p.application_id),
                              "title": f"Review documents: {p.candidate.name}",
                              "subtitle": ", ".join(t.name for t in uploaded[:3]), "due": _iso(p.joining_date)})
            ver = [t for t in p.tasks if t.task_type == "verification" and t.required
                   and t.status in ("pending", "in_progress", "needs_clarification")]
            if p.status == "verification_pending" and ver:
                items.append({"kind": "verification", "entity_type": "recruitment_preboarding", "entity_id": str(p.id),
                              "application_id": str(p.application_id), "title": f"Verification: {p.candidate.name}",
                              "subtitle": ", ".join(t.name for t in ver[:3]), "due": _iso(p.joining_date)})
            if p.status == "failed":
                items.append({"kind": "verification_failed", "entity_type": "recruitment_preboarding", "entity_id": str(p.id),
                              "application_id": str(p.application_id), "title": f"Verification failed: {p.candidate.name}",
                              "subtitle": "Re-initiate, override or reject", "due": _iso(p.joining_date)})
            if p.status == "ready_to_join" and p.joining_status is None:
                items.append({"kind": "joining_confirmation", "entity_type": "recruitment_preboarding", "entity_id": str(p.id),
                              "application_id": str(p.application_id), "title": f"Confirm joining: {p.candidate.name}",
                              "subtitle": p.designation or "", "due": _iso(p.joining_date)})
            if p.joining_status == "confirmed" and p.joining_date and p.joining_date <= datetime.date.today():
                items.append({"kind": "create_employee", "entity_type": "recruitment_preboarding", "entity_id": str(p.id),
                              "application_id": str(p.application_id), "title": f"Create employee: {p.candidate.name}",
                              "subtitle": f"Joined {p.joining_date.strftime('%d %b %Y')}", "due": _iso(p.joining_date)})
        sel_q = (select(models.JobApplication).join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
                 .where(models.JobOpening.company_id == ctx.company_id, models.JobApplication.status == "selected",
                        models.JobApplication.deleted_at.is_(None)))
        for a in db.scalars(sel_q).all():
            if active_offer(ctx, a) is None and (a.owner_id == emp or ctx.can_admin):
                items.append({"kind": "prepare_offer", "entity_type": "recruitment_application", "entity_id": str(a.id),
                              "application_id": str(a.id), "title": f"Prepare offer: {a.candidate.name}",
                              "subtitle": a.opening.title, "due": _iso(a.current_stage_entered_at)})
    return items
