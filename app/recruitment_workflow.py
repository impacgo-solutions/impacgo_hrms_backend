"""Recruitment workflow -- the server-side state machine behind Recruitment.

Hiring Request -> Requisition Approval -> Job Opening -> Candidate +
Application -> Screening -> configurable Interview / Assessment stages
(mandatory feedback) -> Final Selection. Offers, preboarding, joining and
employee creation live in recruitment_onboarding.py.

Every transition is validated here (current status + allowed transition +
permission + tenant + required data), never by the client, and recorded
in hcm_recruitment_history (and core_audit_logs). Records are only ever
soft-closed: rejected / withdrawn applications and their history stay.

Permissions reuse the RBAC matrix column `recruitment`:
  View (v)  -- see recruitment records (compensation hidden)
  Edit (e)  -- run the pipeline, schedule, create offers, preboarding
  Admin (a) -- plus reopen closed applications, verification override,
               recruitment settings
Organization Owner / CEO = Admin. Interview panelists without any
recruitment access still see / give feedback on their own interviews.
Approvals (requisitions, offers) go through the existing configurable
approval engine (Administration > Approval Workflows)."""

from __future__ import annotations

import datetime
import re
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from . import approval_engine, crud, models

# ── vocabularies ────────────────────────────────────────────────────────────

REQUISITION_STATUSES = ("draft", "pending", "sent_back", "approved", "rejected", "cancelled", "closed")
REQUISITION_EDITABLE = {"draft", "sent_back", "rejected"}
OPENING_STATUSES = ("draft", "open", "paused", "closed")
APPLICATION_STATUSES = (
    "applied", "screening", "shortlisted", "assessment", "interview", "selected",
    "offer", "preboarding", "hired", "rejected", "on_hold", "withdrawn", "closed",
)
PIPELINE_COLUMNS = (
    "applied", "screening", "shortlisted", "assessment", "interview", "selected",
    "offer", "preboarding", "hired",
)
APPLICATION_ACTIVE = {
    "applied", "screening", "shortlisted", "assessment", "interview", "selected",
    "offer", "preboarding", "on_hold",
}
APPLICATION_TERMINAL = {"hired", "rejected", "withdrawn", "closed"}
INTERVIEW_STATUSES = (
    "draft", "invited", "ready_to_schedule", "scheduled", "reschedule_requested", "completed",
    "feedback_pending", "feedback_completed", "cancelled", "no_show",
)
# invited: interviewers asked to confirm availability, no time yet;
# ready_to_schedule: at least one accepted -- the organizer picks the time.
INVITATION_PHASE = ("invited", "ready_to_schedule")
BOOKED_INTERVIEW = ("scheduled", "reschedule_requested")
OPEN_INTERVIEW = INVITATION_PHASE + BOOKED_INTERVIEW + ("draft",)
INTERVIEW_MODES = ("in_person", "video", "phone")

# Round-based interviews (backend/db/add_interview_rounds.sql): a shared
# session per stage, applying to every candidate who reaches it -- no
# invite/accept-decline step. Used only when hcm_interviews.round_id is set;
# legacy (round_id IS NULL) interviews keep using INTERVIEW_STATUSES above.
ROUND_STATUSES = ("scheduled", "cancelled")
ROUND_INSTANCE_STATUSES = ("scheduled", "in_progress", "ended", "completed", "cancelled")
ROUND_INSTANCE_OPEN = ("scheduled", "in_progress")
HR_DECISIONS = ("advance", "reject", "hold")
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "intern")
EMPLOYMENT_TYPE_LABELS = {"full_time": "Full-time", "part_time": "Part-time", "contract": "Contract", "intern": "Intern"}
WORK_MODES = ("Office", "Remote", "Hybrid")
PRIORITIES = ("low", "medium", "high", "critical")

STATUS_LABELS = {
    "applied": "Applied", "screening": "Screening", "shortlisted": "Shortlisted",
    "assessment": "Assessment", "interview": "Interview", "selected": "Selected",
    "offer": "Offer", "preboarding": "Preboarding", "hired": "Hired",
    "rejected": "Rejected", "on_hold": "On Hold", "withdrawn": "Withdrawn", "closed": "Closed",
}

# Legacy 5-column board value (hcm_job_applications.stage) for each status,
# so the pre-existing endpoints keep reading consistent data.
_LEGACY_STAGE = {
    "applied": "applied", "screening": "screened", "shortlisted": "screened",
    "assessment": "interview", "interview": "interview", "selected": "interview",
    "offer": "offer", "preboarding": "offer", "hired": "hired",
}
_LEGACY_TO_STATUS = {"applied": "applied", "screened": "screening", "interview": "interview", "offer": "offer", "hired": "hired"}

DEFAULT_INTERVIEW_STAGES = [
    {"key": "technical", "name": "Technical Round", "type": "interview", "mandatory": True,
     "feedback_required": True, "min_interviewers": 1,
     "scorecard": ["Technical skills", "Problem solving", "Communication"]},
    {"key": "manager", "name": "Manager Round", "type": "interview", "mandatory": True,
     "feedback_required": True, "min_interviewers": 1,
     "scorecard": ["Role fit", "Ownership", "Communication"]},
    {"key": "hr", "name": "HR Round", "type": "interview", "mandatory": True,
     "feedback_required": True, "min_interviewers": 1,
     "scorecard": ["Culture fit", "Communication", "Expectations alignment"]},
]
DEFAULT_SOURCES = [
    "Career Site", "Employee Referral", "Recruiter", "Job Board", "Agency",
    "Direct Application", "Walk-in", "Import", "Other",
]
DEFAULT_REJECTION_REASONS = [
    "Skills mismatch", "Insufficient experience", "Failed interview",
    "Salary expectations too high", "Notice period too long", "Culture fit",
    "Position filled", "Candidate not reachable", "Other",
]
DEFAULT_PREBOARDING_CHECKLIST = [
    {"category": "candidate_info", "name": "Personal details", "task_type": "information", "required": True, "field_group": "personal"},
    {"category": "candidate_info", "name": "Address & contact details", "task_type": "information", "required": True, "field_group": "contact"},
    {"category": "candidate_info", "name": "Emergency contact", "task_type": "information", "required": True, "field_group": "emergency"},
    {"category": "identity", "name": "PAN card", "task_type": "document", "required": True},
    {"category": "identity", "name": "Aadhaar / National ID", "task_type": "document", "required": True},
    {"category": "identity", "name": "Passport", "task_type": "document", "required": False},
    {"category": "education", "name": "Highest degree certificate", "task_type": "document", "required": True},
    {"category": "education", "name": "Marksheets", "task_type": "document", "required": False},
    {"category": "employment", "name": "Experience / relieving letters", "task_type": "document", "required": False},
    {"category": "banking", "name": "Bank account details", "task_type": "information", "required": True, "field_group": "bank"},
    {"category": "company_documents", "name": "Signed offer acceptance", "task_type": "document", "required": True},
    {"category": "company_documents", "name": "Policy acknowledgement", "task_type": "acknowledgement", "required": True},
    {"category": "verification", "name": "Document verification", "task_type": "verification", "required": True},
    {"category": "verification", "name": "Background verification", "task_type": "verification", "required": False},
    {"category": "verification", "name": "Reference check", "task_type": "verification", "required": False},
]
PREBOARDING_CATEGORIES = (
    "candidate_info", "identity", "education", "employment", "banking", "company_documents", "verification",
)
TASK_TYPES = ("document", "information", "verification", "acknowledgement")


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def err(status: int, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"message": message, **extra} if extra else message)


# ── request context / permissions ───────────────────────────────────────────

@dataclass
class Ctx:
    db: Session
    user: models.User | None
    company_id: uuid.UUID
    level: str
    is_owner: bool
    actor_name: str

    @property
    def can_view(self) -> bool:
        return self.level in ("v", "e", "a")

    @property
    def can_edit(self) -> bool:
        return self.level in ("e", "a")

    @property
    def can_admin(self) -> bool:
        return self.level == "a"

    @property
    def can_see_compensation(self) -> bool:
        return self.can_edit

    def require_view(self) -> None:
        if not self.can_view:
            raise err(403, "You don't have access to Recruitment.")

    def require_edit(self) -> None:
        if not self.can_edit:
            raise err(403, "You need Recruitment Edit access to do this.")

    def require_admin(self) -> None:
        if not self.can_admin:
            raise err(403, "Only a Recruitment Admin (or the Organization Owner) can do this.")


def system_ctx(db: Session, company_id: uuid.UUID, actor_name: str = "System") -> Ctx:
    """Actions without a logged-in user (scheduled offer expiry, the
    candidate's own portal response)."""
    return Ctx(db=db, user=None, company_id=company_id, level="a", is_owner=False, actor_name=actor_name)


def make_ctx(db: Session, user: models.User) -> Ctx:
    role = crud.get_user_primary_role(db, user.id)
    is_owner = role is not None and role.name == crud.BUILTIN_ROLES[0]
    level = "a" if is_owner else (crud.effective_user_matrix(db, user).get("recruitment") or "n")
    name = crud.employee_display_name(db, user.employee_id) if user.employee_id else None
    if not name or name == "—":
        name = user.full_name or user.email
    return Ctx(db=db, user=user, company_id=user.company_id, level=level, is_owner=is_owner, actor_name=name)


# ── settings ────────────────────────────────────────────────────────────────

def get_settings(db: Session, company_id: uuid.UUID) -> dict:
    row = db.get(models.RecruitmentSettings, company_id)
    return {
        "offer_approval_required": True if row is None else bool(row.offer_approval_required),
        "offer_expiry_days": 7 if row is None else int(row.offer_expiry_days or 7),
        "default_interview_stages": (row.default_interview_stages if row and row.default_interview_stages is not None
                                     else DEFAULT_INTERVIEW_STAGES),
        "sources": (row.sources if row and row.sources else DEFAULT_SOURCES),
        "rejection_reasons": (row.rejection_reasons if row and row.rejection_reasons else DEFAULT_REJECTION_REASONS),
        "preboarding_checklist": (row.preboarding_checklist if row and row.preboarding_checklist
                                  else DEFAULT_PREBOARDING_CHECKLIST),
        "verification_failure_policy": "hold" if row is None else (row.verification_failure_policy or "hold"),
        "allow_verification_override": True if row is None else bool(row.allow_verification_override),
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


def save_settings(ctx: Ctx, data: dict) -> dict:
    ctx.require_admin()
    row = ctx.db.get(models.RecruitmentSettings, ctx.company_id)
    if row is None:
        row = models.RecruitmentSettings(company_id=ctx.company_id)
        ctx.db.add(row)
    if "offer_approval_required" in data and data["offer_approval_required"] is not None:
        row.offer_approval_required = bool(data["offer_approval_required"])
    if data.get("offer_expiry_days") is not None:
        days = int(data["offer_expiry_days"])
        if not 1 <= days <= 90:
            raise err(422, "Offer validity must be between 1 and 90 days.")
        row.offer_expiry_days = days
    if data.get("default_interview_stages") is not None:
        row.default_interview_stages = normalize_stages(data["default_interview_stages"])
    if data.get("sources") is not None:
        row.sources = _clean_list(data["sources"], "source")
    if data.get("rejection_reasons") is not None:
        row.rejection_reasons = _clean_list(data["rejection_reasons"], "rejection reason")
    if data.get("preboarding_checklist") is not None:
        row.preboarding_checklist = normalize_checklist(data["preboarding_checklist"])
    if data.get("verification_failure_policy") is not None:
        if data["verification_failure_policy"] not in ("hold", "reject"):
            raise err(422, "Verification failure policy must be 'hold' or 'reject'.")
        row.verification_failure_policy = data["verification_failure_policy"]
    if data.get("allow_verification_override") is not None:
        row.allow_verification_override = bool(data["allow_verification_override"])
    row.updated_by = ctx.user.id
    row.updated_at = now()
    crud.create_audit_log(ctx.db, ctx.company_id, ctx.user.id, "update", "recruitment_settings", None)
    ctx.db.flush()
    return get_settings(ctx.db, ctx.company_id)


def _clean_list(values, label: str) -> list[str]:
    out: list[str] = []
    for v in values or []:
        v = str(v).strip()
        if v and v.lower() not in {x.lower() for x in out}:
            out.append(v[:80])
    if not out:
        raise err(422, f"Add at least one {label}.")
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")[:40] or "stage"


def normalize_stages(stages) -> list[dict]:
    """Validates a hiring pipeline's interview / assessment stages."""
    out: list[dict] = []
    keys: set[str] = set()
    for i, raw in enumerate(stages or []):
        name = str((raw or {}).get("name") or "").strip()
        if not name:
            raise err(422, f"Stage {i + 1} needs a name.")
        key = _slug(str(raw.get("key") or name))
        base, n = key, 2
        while key in keys:
            key = f"{base[:36]}_{n}"
            n += 1
        keys.add(key)
        stype = raw.get("type") or "interview"
        if stype not in ("interview", "assessment"):
            raise err(422, f"Stage '{name}': type must be interview or assessment.")
        feedback_required = bool(raw.get("feedback_required", True))
        min_int = int(raw.get("min_interviewers") or 1)
        if min_int < 1 or min_int > 10:
            raise err(422, f"Stage '{name}': minimum interviewers must be 1-10.")
        scorecard = [str(c).strip()[:60] for c in (raw.get("scorecard") or []) if str(c).strip()][:12]
        stage = {
            "key": key, "name": name[:120], "type": stype,
            "mandatory": bool(raw.get("mandatory", True)),
            "feedback_required": feedback_required,
            "min_interviewers": min_int, "scorecard": scorecard,
        }
        # Optional round scheduling, set right in the stage editor (Recruitment
        # > New Job Requisition / Job Opening) so a round session (panel +
        # time) is defined in the same pass as the stage itself -- see
        # _materialize_rounds_from_stages(), called after the opening exists.
        # Purely advisory here: just carried through, not yet validated against
        # real employees/company (that happens in create_round()).
        employee_ids = [str(e) for e in (raw.get("employee_ids") or []) if str(e).strip()]
        scheduled_at = raw.get("scheduled_at") or None
        if employee_ids and scheduled_at:
            mode = raw.get("mode") or "video"
            if mode not in INTERVIEW_MODES:
                raise err(422, f"Stage '{name}': mode must be in person, video or phone.")
            stage["round"] = {
                "scheduled_at": scheduled_at, "scheduled_end": raw.get("scheduled_end") or None,
                "mode": mode, "location": (raw.get("location") or None),
                "meeting_link": (raw.get("meeting_link") or None), "notes": (raw.get("notes") or None),
                "employee_ids": employee_ids,
            }
        out.append(stage)
    if len(out) > 12:
        raise err(422, "A hiring pipeline can have at most 12 stages.")
    return out


def _materialize_rounds_from_stages(ctx: Ctx, o: models.JobOpening, stages: list[dict]) -> None:
    """A stage saved with a 'round' block (panel + schedule, filled in right
    in the stage editor) gets its hcm_interview_rounds row created right
    away -- so by the time the opening exists, HR only ever comes back to
    Edit or Cancel a round, never to "define" one from scratch. Skips a
    stage that already has a round (idempotent: safe to call on every save,
    including edits of an opening that already has some rounds). A bad
    panel/time (e.g. a double-booked interviewer) raises the same 409/422
    create_round() would -- surfaced on the opening save itself, so HR fixes
    it right there instead of finding out later that a round silently
    wasn't created."""
    if not o.id:
        return
    existing = {r for r, in ctx.db.execute(
        select(models.InterviewRound.round_key).where(models.InterviewRound.opening_id == o.id))}
    for stage in stages:
        block = stage.get("round")
        if not block or stage["key"] in existing:
            continue
        create_round(ctx, o, {**block, "round_key": stage["key"], "name": stage["name"]})


def normalize_checklist(items) -> list[dict]:
    out: list[dict] = []
    for raw in items or []:
        name = str((raw or {}).get("name") or "").strip()
        if not name:
            continue
        category = raw.get("category") or "company_documents"
        task_type = raw.get("task_type") or "document"
        if category not in PREBOARDING_CATEGORIES:
            raise err(422, f"Unknown preboarding category '{category}'.")
        if task_type not in TASK_TYPES:
            raise err(422, f"Unknown task type '{task_type}'.")
        group = raw.get("field_group") if task_type == "information" else None
        if group is not None and group not in ("personal", "contact", "emergency", "bank"):
            group = None
        out.append({"category": category, "name": name[:150], "task_type": task_type,
                    "required": bool(raw.get("required", True)), "field_group": group})
    if not out:
        raise err(422, "The preboarding checklist needs at least one item.")
    return out


# ── history / notifications ─────────────────────────────────────────────────

def record(
    ctx: Ctx, entity_type: str, entity_id: uuid.UUID, action: str, *,
    old: str | None = None, new: str | None = None, comments: str | None = None,
    meta: dict | None = None, application: models.JobApplication | None = None,
    application_id: uuid.UUID | None = None, candidate_id: uuid.UUID | None = None,
    actor_name: str | None = None, actor_user_id: uuid.UUID | None | bool = True,
) -> None:
    """Stage history + audit log entry (append-only)."""
    if application is not None:
        application_id = application.id
        candidate_id = candidate_id or application.candidate_id
    uid = (ctx.user.id if ctx.user is not None else None) if actor_user_id is True else (actor_user_id or None)
    ctx.db.add(models.RecruitmentHistory(
        id=uuid.uuid4(), company_id=ctx.company_id, entity_type=entity_type, entity_id=entity_id,
        application_id=application_id, candidate_id=candidate_id, action=action[:60],
        old_status=old, new_status=new, comments=(comments or None), meta=meta or None,
        actor_user_id=uid, actor_name=(actor_name or ctx.actor_name)[:150], created_at=now(),
    ))
    crud.create_audit_log(
        ctx.db, ctx.company_id, uid, _audit_action(action), f"recruitment_{entity_type}"[:60], entity_id,
        changes={k: v for k, v in {"action": action, "from": old, "to": new, "comments": comments}.items() if v},
    )


def _audit_action(action: str) -> str:
    a = action.lower()
    for word in ("create", "approve", "reject", "submit", "cancel", "withdraw", "publish", "close", "reopen",
                 "hold", "resume", "schedule", "reschedule", "complete", "feedback", "select", "send",
                 "accept", "decline", "expire", "upload", "verify", "confirm", "update"):
        if word in a:
            return word
    return "update"


def user_ids_for_employees(db: Session, employee_ids) -> list[uuid.UUID]:
    ids = [e for e in {*(employee_ids or [])} if e]
    if not ids:
        return []
    return list(db.scalars(select(models.User.id).where(models.User.employee_id.in_(ids))).all())


def notify(
    ctx: Ctx, user_ids, title: str, body: str, entity_type: str, entity_id: uuid.UUID,
    *, email: bool = True, details: dict | None = None, exclude_self: bool = True,
) -> None:
    """In-app notification (+ email) through the existing notification /
    email architecture. Never the reason an action fails."""
    from . import email_service

    for uid in {u for u in (user_ids or []) if u}:
        if exclude_self and ctx.user is not None and uid == ctx.user.id:
            continue
        try:
            with ctx.db.begin_nested():
                crud.create_notification(ctx.db, ctx.company_id, uid, title, body, entity_type, entity_id)
                if email:
                    target = ctx.db.get(models.User, uid)
                    email_service.send_request_email(
                        target.email if target else None, subject=title, heading=title, intro_line=body,
                        details=details or {}, entity_type=entity_type, entity_id=entity_id,
                        db=ctx.db, company_id=ctx.company_id, email_type=email_service.GENERAL,
                        cta_label="Open in HRMS",
                    )
        except Exception:  # pragma: no cover - notifications are best-effort
            import logging
            logging.getLogger(__name__).exception("Recruitment notification failed")


def recruiter_user_ids(ctx: Ctx, application: models.JobApplication | None = None,
                       opening: models.JobOpening | None = None) -> list[uuid.UUID]:
    """The application's owner/recruiter + the opening's hiring team."""
    opening = opening or (application.opening if application is not None else None)
    emp_ids: set = set()
    if application is not None and application.owner_id:
        emp_ids.add(application.owner_id)
    if opening is not None:
        emp_ids.update(_uuid_list(opening.hiring_team))
    return user_ids_for_employees(ctx.db, emp_ids)


def _uuid_list(values) -> list[uuid.UUID]:
    out = []
    for v in values or []:
        try:
            out.append(uuid.UUID(str(v)))
        except (TypeError, ValueError):
            continue
    return out


# ── approval helpers (requisitions / offers share the existing engine) ─────

def current_approver_user_ids(db: Session, company_id: uuid.UUID, doctype: str, document_id: uuid.UUID,
                              requester: models.Employee) -> list[uuid.UUID]:
    """Who can act on the document's current approval step right now."""
    if not crud._has_custom_approval_workflow(db, company_id, doctype):
        emp_ids = {requester.reporting_manager_id, requester.dotted_line_manager_id} - {None}
        if not emp_ids:
            emp_ids = set(crud.list_fallback_approver_employee_ids(db, company_id, exclude_employee_id=requester.id))
        return [u for u in user_ids_for_employees(db, emp_ids)]
    workflow = approval_engine.get_active_workflow(db, company_id, doctype)
    request = _approval_request(db, company_id, doctype, document_id)
    step = request.current_step if request is not None and request.status == "pending" else None
    orders = sorted({s.step_order for s in workflow.steps}) if workflow else []
    if step is None:
        step = orders[0] if orders else 1
    users: set[uuid.UUID] = set()
    resolvable = False
    for s in (workflow.steps if workflow else []):
        if s.step_order != step:
            continue
        if s.approver_type == "user" and s.user_id:
            users.add(s.user_id)
            resolvable = True
        elif s.approver_type == "role" and s.role_id:
            users.update(db.scalars(select(models.UserRole.user_id).where(models.UserRole.role_id == s.role_id)).all())
            resolvable = True
        else:
            emp = approval_engine._resolve_dynamic_approver_employee_id(db, requester, s.approver_type)
            if emp and emp != requester.id:
                users.update(user_ids_for_employees(db, [emp]))
                resolvable = True
    if not resolvable:
        users.update(user_ids_for_employees(
            db, crud.list_fallback_approver_employee_ids(db, company_id, exclude_employee_id=requester.id)))
    requester_user = crud.get_user_id_for_employee(db, requester.id)
    return [u for u in users if u != requester_user]


def _approval_request(db: Session, company_id, doctype, document_id) -> models.ApprovalRequest | None:
    return db.scalar(select(models.ApprovalRequest).where(
        models.ApprovalRequest.company_id == company_id,
        models.ApprovalRequest.doctype == doctype,
        models.ApprovalRequest.document_id == document_id,
    ))


def restart_approval(ctx: Ctx, doctype: str, document_id: uuid.UUID, comments: str | None = None) -> None:
    """A resubmitted document starts a fresh approval cycle at the first
    step (existing engine rows are kept, a 'resubmitted' action is added)."""
    request = _approval_request(ctx.db, ctx.company_id, doctype, document_id)
    if request is None or request.status == "pending":
        return
    workflow = ctx.db.get(models.ApprovalWorkflow, request.workflow_id)
    orders = sorted({s.step_order for s in workflow.steps}) if workflow else []
    request.current_step = orders[0] if orders else 1
    request.status = "pending"
    ctx.db.add(models.ApprovalAction(
        id=uuid.uuid4(), request_id=request.id, step_order=request.current_step,
        actor_id=ctx.user.id, action="resubmitted", comments=comments, acted_at=now(),
    ))
    ctx.db.flush()


def approval_trail(db: Session, company_id, doctype: str, document_id) -> list[dict]:
    request = _approval_request(db, company_id, doctype, document_id)
    if request is None:
        return []
    actions = db.scalars(select(models.ApprovalAction).where(models.ApprovalAction.request_id == request.id)
                         .order_by(models.ApprovalAction.acted_at)).all()
    out = []
    for a in actions:
        actor = db.get(models.User, a.actor_id) if a.actor_id else None
        name = crud.employee_display_name(db, actor.employee_id) if actor and actor.employee_id else (actor.email if actor else "—")
        out.append({"step": a.step_order, "action": a.action, "comments": a.comments,
                    "actor": name, "at": a.acted_at.isoformat() if a.acted_at else None})
    return out


# ── shared lookups ──────────────────────────────────────────────────────────

def employee_in_company(ctx: Ctx, employee_id, label: str = "Employee") -> models.Employee | None:
    if employee_id in (None, ""):
        return None
    try:
        emp = ctx.db.get(models.Employee, uuid.UUID(str(employee_id)))
    except ValueError:
        emp = None
    if emp is None or emp.company_id != ctx.company_id:
        raise err(422, f"{label} not found in your organization.")
    return emp


def department_in_company(ctx: Ctx, department_id) -> models.Department | None:
    if department_id in (None, ""):
        return None
    dep = ctx.db.get(models.Department, uuid.UUID(str(department_id)))
    if dep is None or dep.company_id != ctx.company_id:
        raise err(422, "Department not found.")
    return dep


def branch_in_company(ctx: Ctx, branch_id) -> models.Branch | None:
    if branch_id in (None, ""):
        return None
    br = ctx.db.get(models.Branch, uuid.UUID(str(branch_id)))
    if br is None or br.company_id != ctx.company_id:
        raise err(422, "Location / branch not found.")
    return br


def _emp_ref(db: Session, emp_id) -> dict | None:
    if not emp_id:
        return None
    emp = db.get(models.Employee, emp_id)
    if emp is None:
        return None
    return {"id": str(emp.id), "name": crud._full_name(emp), "code": getattr(emp, "employee_code", None)}


def _name_of(db: Session, model, obj_id) -> str | None:
    if not obj_id:
        return None
    row = db.get(model, obj_id)
    return row.name if row is not None else None


def _iso(v) -> str | None:
    return v.isoformat() if v is not None else None


def _num(v) -> float | None:
    return float(v) if v is not None else None


def _validate_common(ctx: Ctx, data: dict) -> None:
    if data.get("employment_type") is not None:
        data["employment_type"] = normalize_employment_type(data["employment_type"])
    if data.get("work_mode") not in (None, "") and data["work_mode"] not in WORK_MODES:
        raise err(422, f"Work mode must be one of {', '.join(WORK_MODES)}.")
    for lo, hi, label in (("experience_min", "experience_max", "Experience"), ("salary_min", "salary_max", "Salary")):
        a, b = data.get(lo), data.get(hi)
        if a is not None and a < 0 or b is not None and b < 0:
            raise err(422, f"{label} range cannot be negative.")
        if a is not None and b is not None and a > b:
            raise err(422, f"{label} range: minimum is more than maximum.")
    if data.get("hiring_team") is not None:
        team = []
        for e in data["hiring_team"]:
            emp = employee_in_company(ctx, e, "Hiring team member")
            if str(emp.id) not in team:
                team.append(str(emp.id))
        data["hiring_team"] = team
    if data.get("reporting_manager_id") not in (None, ""):
        employee_in_company(ctx, data["reporting_manager_id"], "Reporting manager")


def normalize_employment_type(value: str | None) -> str:
    v = (value or "full_time").strip().lower().replace("-", "_").replace(" ", "_")
    v = {"fulltime": "full_time", "parttime": "part_time", "internship": "intern"}.get(v, v)
    if v not in EMPLOYMENT_TYPES:
        raise err(422, "Employment type must be Full-time, Part-time, Contract or Intern.")
    return v


# ═══════════════════════════════════════════════════════════════════════════
# Requisitions
# ═══════════════════════════════════════════════════════════════════════════

REQ_DOCTYPE = "hiring_requisition"
_REQ_FIELDS = (
    "designation_title", "positions_count", "justification", "employment_type", "work_mode",
    "target_joining_date", "experience_min", "experience_max", "salary_min", "salary_max",
    "required_skills", "qualifications", "job_description", "budget_cost_center", "priority",
    "hiring_team", "interview_stages",
)


def get_requisition(ctx: Ctx, req_id: uuid.UUID, *, lock: bool = False) -> models.HiringRequisition:
    q = select(models.HiringRequisition).where(models.HiringRequisition.id == req_id)
    if lock:
        q = q.with_for_update()
    r = ctx.db.scalar(q)
    requester = ctx.db.get(models.Employee, r.requested_by) if r is not None else None
    if r is None or requester is None or requester.company_id != ctx.company_id:
        raise err(404, "Hiring requisition not found.")
    return r


def can_view_requisition(ctx: Ctx, r: models.HiringRequisition) -> bool:
    if ctx.can_view or r.requested_by == ctx.user.employee_id:
        return True
    return r.status == "pending" and crud.can_decide_request_readonly(ctx.db, ctx.user, r.requested_by, REQ_DOCTYPE, r.id)


def requisition_out(ctx: Ctx, r: models.HiringRequisition, *, detail: bool = False) -> dict:
    db = ctx.db
    comp = ctx.can_see_compensation or r.requested_by == ctx.user.employee_id
    can_decide = (r.status == "pending"
                  and crud.can_decide_request_readonly(db, ctx.user, r.requested_by, REQ_DOCTYPE, r.id))
    mine = r.requested_by == ctx.user.employee_id
    actions = []
    if r.status in REQUISITION_EDITABLE and (mine or ctx.can_admin) and ctx.can_edit:
        actions += ["edit", "submit"]
    if can_decide:
        actions += ["approve", "reject", "request_changes"]
    if r.status in ("draft", "pending", "sent_back", "rejected") and (mine or ctx.can_admin) and ctx.can_edit:
        actions.append("cancel")
    if r.status == "approved" and r.opening_id is None and ctx.can_edit:
        actions.append("create_opening")
    out = {
        "id": str(r.id),
        "status": r.status,
        "designation_title": r.designation_title,
        "department_id": str(r.department_id) if r.department_id else None,
        "department_name": r.department.name if r.department else None,
        "sub_department_id": str(r.sub_department_id) if r.sub_department_id else None,
        "branch_id": str(r.branch_id) if r.branch_id else None,
        "branch_name": _name_of(db, models.Branch, r.branch_id),
        "positions_count": r.positions_count,
        "employment_type": r.employment_type,
        "work_mode": r.work_mode,
        "reporting_manager": _emp_ref(db, r.reporting_manager_id),
        "target_joining_date": _iso(r.target_joining_date),
        "experience_min": _num(r.experience_min), "experience_max": _num(r.experience_max),
        "salary_min": _num(r.salary_min) if comp else None, "salary_max": _num(r.salary_max) if comp else None,
        "required_skills": r.required_skills, "qualifications": r.qualifications,
        "job_description": r.job_description, "justification": r.justification,
        "budget_cost_center": r.budget_cost_center if comp else None,
        "priority": r.priority or "medium",
        "hiring_team": [x for x in (_emp_ref(db, e) for e in _uuid_list(r.hiring_team)) if x],
        "interview_stages": r.interview_stages,
        "requested_by": _emp_ref(db, r.requested_by),
        "approver": _emp_ref(db, r.approver_id),
        "decision_notes": r.decision_notes,
        "decided_at": _iso(r.decided_at),
        "submitted_at": _iso(r.submitted_at),
        "created_at": _iso(r.created_at),
        "version": r.version or 1,
        "opening_id": str(r.opening_id) if r.opening_id else None,
        "cancel_reason": r.cancel_reason,
        "can_decide": can_decide,
        "actions": actions,
        "compensation_visible": comp,
    }
    if detail:
        out["history"] = history_for(ctx, entity_type="requisition", entity_id=r.id)
        out["approval_trail"] = approval_trail(db, ctx.company_id, REQ_DOCTYPE, r.id)
        requester = db.get(models.Employee, r.requested_by)
        if r.status == "pending" and requester is not None:
            out["current_approvers"] = [
                crud.employee_display_name(db, u.employee_id) if u and u.employee_id else (u.email if u else "—")
                for u in (db.get(models.User, x) for x in current_approver_user_ids(db, ctx.company_id, REQ_DOCTYPE, r.id, requester))
            ]
    return out


def list_requisitions(ctx: Ctx, *, status: list[str] | None = None, search: str | None = None,
                      department_id=None, limit: int = 50, offset: int = 0) -> dict:
    db = ctx.db
    q = (select(models.HiringRequisition)
         .join(models.Employee, models.Employee.id == models.HiringRequisition.requested_by)
         .where(models.Employee.company_id == ctx.company_id))
    if not ctx.can_view:
        # Own requisitions + those waiting on me (filtered below).
        q = q.where(or_(models.HiringRequisition.requested_by == ctx.user.employee_id,
                        models.HiringRequisition.status == "pending"))
    if status:
        q = q.where(models.HiringRequisition.status.in_(status))
    if department_id:
        q = q.where(models.HiringRequisition.department_id == department_id)
    if search:
        q = q.where(models.HiringRequisition.designation_title.ilike(f"%{search.strip()}%"))
    rows = db.scalars(q.order_by(models.HiringRequisition.created_at.desc().nulls_last(),
                                 models.HiringRequisition.decided_at.desc().nulls_last())).all()
    if not ctx.can_view:
        rows = [r for r in rows if can_view_requisition(ctx, r)]
    total = len(rows)
    return {"items": [requisition_out(ctx, r) for r in rows[offset:offset + limit]], "total": total}


def _apply_requisition_fields(ctx: Ctx, r: models.HiringRequisition, data: dict) -> None:
    _validate_common(ctx, data)
    if "department_id" in data:
        dep = department_in_company(ctx, data["department_id"])
        r.department_id = dep.id if dep else None
    if "sub_department_id" in data:
        sub = None
        if data["sub_department_id"]:
            sub = ctx.db.get(models.SubDepartment, uuid.UUID(str(data["sub_department_id"])))
            if sub is None or (r.department_id and sub.department_id != r.department_id):
                raise err(422, "Sub-department not found in the selected department.")
        r.sub_department_id = sub.id if sub else None
    if "branch_id" in data:
        br = branch_in_company(ctx, data["branch_id"])
        r.branch_id = br.id if br else None
    if "reporting_manager_id" in data:
        rm = employee_in_company(ctx, data["reporting_manager_id"], "Reporting manager")
        r.reporting_manager_id = rm.id if rm else None
    for f in _REQ_FIELDS:
        if f in data:
            v = data[f]
            if f == "interview_stages" and v is not None:
                v = normalize_stages(v)
            if f == "priority" and v not in (None, *PRIORITIES):
                raise err(422, "Priority must be low, medium, high or critical.")
            if f == "positions_count":
                v = int(v or 0)
                if v < 1 or v > 500:
                    raise err(422, "Number of openings must be between 1 and 500.")
            if f == "designation_title":
                v = (v or "").strip()
                if not v:
                    raise err(422, "Job title is required.")
            if f == "target_joining_date":
                v = _parse_date(v)
            setattr(r, f, v)


def _require_submittable(r: models.HiringRequisition) -> None:
    missing = [label for label, v in (("job title", r.designation_title), ("department", r.department_id),
                                      ("number of openings", r.positions_count),
                                      ("business justification", (r.justification or "").strip()))
               if not v]
    if missing:
        raise err(422, "Complete the requisition before submitting: " + ", ".join(missing) + ".")


def create_requisition(ctx: Ctx, data: dict, *, submit: bool) -> models.HiringRequisition:
    ctx.require_edit()
    requested_by = data.pop("requested_by", None) or ctx.user.employee_id
    if requested_by is None:
        raise err(422, "Your login is not linked to an employee record.")
    requested_by = uuid.UUID(str(requested_by))
    if not str(data.get("designation_title") or "").strip():  # M-40: {} is not a requisition (even a draft)
        raise err(422, "Job title is required.")
    if not crud.can_submit_self_service_request(ctx.db, ctx.user, requested_by, "recruitment", REQ_DOCTYPE, "create"):
        raise err(403, "You can only raise a hiring request for yourself or your team.")
    r = models.HiringRequisition(
        id=uuid.uuid4(), requested_by=requested_by, company_id=ctx.company_id,
        designation_title="", positions_count=1, status="draft", hiring_team=[], version=1,
        created_by=ctx.user.id, created_at=now(),
    )
    _apply_requisition_fields(ctx, r, data)
    ctx.db.add(r)
    ctx.db.flush()
    record(ctx, "requisition", r.id, "Requisition created", new="draft")
    if submit:
        submit_requisition(ctx, r)
    return r


def update_requisition(ctx: Ctx, r: models.HiringRequisition, data: dict) -> None:
    ctx.require_edit()
    if r.status not in REQUISITION_EDITABLE:
        raise err(409, f"A {r.status.replace('_', ' ')} requisition cannot be edited.")
    if r.requested_by != ctx.user.employee_id and not ctx.can_admin:
        raise err(403, "Only the requester (or a Recruitment Admin) can edit this requisition.")
    data.pop("requested_by", None)
    _apply_requisition_fields(ctx, r, data)
    r.updated_at = now()
    record(ctx, "requisition", r.id, "Requisition edited", old=r.status, new=r.status)


def submit_requisition(ctx: Ctx, r: models.HiringRequisition, comments: str | None = None) -> None:
    ctx.require_edit()
    if r.status not in REQUISITION_EDITABLE:
        raise err(409, f"This requisition is already {r.status.replace('_', ' ')}.")
    if r.requested_by != ctx.user.employee_id and not ctx.can_admin:
        raise err(403, "Only the requester (or a Recruitment Admin) can submit this requisition.")
    _require_submittable(r)
    old = r.status
    if old in ("sent_back", "rejected"):
        r.version = (r.version or 1) + 1
        restart_approval(ctx, REQ_DOCTYPE, r.id, comments)
    r.status = "pending"
    r.submitted_at = now()
    r.approver_id = None
    r.decided_at = None
    record(ctx, "requisition", r.id, "Submitted for approval" if old == "draft" else "Resubmitted for approval",
           old=old, new="pending", comments=comments, meta={"version": r.version})
    ctx.db.flush()
    requester = ctx.db.get(models.Employee, r.requested_by)
    notify(ctx, current_approver_user_ids(ctx.db, ctx.company_id, REQ_DOCTYPE, r.id, requester),
           "Hiring requisition awaiting your approval",
           f"{crud._full_name(requester)} requested {r.positions_count} x {r.designation_title}.",
           REQ_DOCTYPE, r.id,
           details={"Position": r.designation_title, "Openings": str(r.positions_count),
                    "Department": r.department.name if r.department else "—", "Requested by": crud._full_name(requester)})


def decide_requisition(ctx: Ctx, r: models.HiringRequisition, decision: str, comments: str | None,
                       *, require_reason: bool = True) -> str:
    """approved | rejected | sent_back through the configured approval
    chain. Returns the effective status ('pending' while more steps remain)."""
    if decision not in ("approved", "rejected", "sent_back"):
        raise err(422, "Decision must be approve, reject or request changes.")
    if r.status != "pending":
        raise err(409, f"This requisition is {r.status.replace('_', ' ')} -- only a pending requisition can be decided.")
    if require_reason and decision in ("rejected", "sent_back") and not (comments or "").strip():
        raise err(422, "A reason is required to reject or request changes.")
    if not crud.can_decide_request_configurable(ctx.db, ctx.user, r.requested_by, REQ_DOCTYPE, r.id):
        raise err(403, "You are not an approver for the current step of this requisition.")
    effective = crud.decide_configurable_request(ctx.db, ctx.user, r.requested_by, REQ_DOCTYPE, r.id, decision, comments)
    old = r.status
    r.status = effective
    r.approver_id = ctx.user.employee_id
    r.decision_notes = comments
    if effective != "pending":
        r.decided_at = now()
    label = {"approved": "Approved", "rejected": "Rejected", "sent_back": "Changes requested"}[decision]
    if effective == "pending":
        label = "Approved (step) -- sent to next approver"
    record(ctx, "requisition", r.id, label, old=old, new=effective, comments=comments)
    ctx.db.flush()
    requester = ctx.db.get(models.Employee, r.requested_by)
    if effective == "pending":
        notify(ctx, current_approver_user_ids(ctx.db, ctx.company_id, REQ_DOCTYPE, r.id, requester),
               "Hiring requisition awaiting your approval",
               f"{r.designation_title} ({r.positions_count}) was approved at the previous step and now needs your decision.",
               REQ_DOCTYPE, r.id)
    else:
        crud.notify_decision(ctx.db, ctx.company_id, r.requested_by, "Hiring Requisition", effective,
                             comments, REQ_DOCTYPE, r.id)
        if effective == "approved":
            create_opening_from_requisition(ctx, r, auto=True)
    return effective


def cancel_requisition(ctx: Ctx, r: models.HiringRequisition, reason: str | None) -> None:
    ctx.require_edit()
    if r.status not in ("draft", "pending", "sent_back", "rejected", "approved"):
        raise err(409, f"A {r.status} requisition cannot be cancelled.")
    if r.requested_by != ctx.user.employee_id and not ctx.can_admin:
        raise err(403, "Only the requester (or a Recruitment Admin) can cancel this requisition.")
    if not (reason or "").strip():
        raise err(422, "A cancellation reason is required.")
    if r.opening_id:
        o = ctx.db.get(models.JobOpening, r.opening_id)
        if o is not None and o.status != "closed" and _application_count(ctx.db, o.id) > 0:
            raise err(409, "Its job opening already has applications -- close the job opening instead.")
        if o is not None and o.status != "closed":
            _set_opening_status(ctx, o, "closed", reason)
    old = r.status
    approval_engine.close_request(ctx.db, ctx.company_id, REQ_DOCTYPE, r.id, ctx.user, "cancelled", reason)
    r.status = "cancelled"
    r.cancel_reason = reason
    record(ctx, "requisition", r.id, "Requisition cancelled", old=old, new="cancelled", comments=reason)


def create_opening_from_requisition(ctx: Ctx, r: models.HiringRequisition, *, auto: bool = False) -> models.JobOpening:
    # L-26: authorization first, so a caller without Recruitment edit rights
    # learns nothing about the requisition's state from a 409.
    if not auto:
        ctx.require_edit()
    if r.status != "approved":
        raise err(409, "A job opening can only be created from an approved requisition.")
    if r.opening_id:
        existing = ctx.db.get(models.JobOpening, r.opening_id)
        if existing is not None:
            return existing
    settings = get_settings(ctx.db, ctx.company_id)
    designation = crud.get_or_create_designation_by_name(ctx.db, ctx.company_id, r.designation_title)
    o = models.JobOpening(
        id=uuid.uuid4(), company_id=ctx.company_id, title=r.designation_title, department_id=r.department_id,
        designation_id=designation.id if designation is not None else None,
        branch_id=r.branch_id, vacancies=r.positions_count, status="draft", description=r.job_description,
        employment_type=r.employment_type or "full_time", posted_date=datetime.date.today(),
        requisition_id=r.id, work_mode=r.work_mode, reporting_manager_id=r.reporting_manager_id,
        created_by=ctx.user.id if ctx.user is not None else None,
        experience_min=r.experience_min, experience_max=r.experience_max,
        salary_min=r.salary_min, salary_max=r.salary_max, required_skills=r.required_skills,
        qualifications=r.qualifications, target_joining_date=r.target_joining_date,
        hiring_team=list(r.hiring_team or []),
        interview_stages=r.interview_stages if r.interview_stages is not None else settings["default_interview_stages"],
    )
    ctx.db.add(o)
    ctx.db.flush()
    _materialize_rounds_from_stages(ctx, o, o.interview_stages or [])
    r.opening_id = o.id
    record(ctx, "opening", o.id, "Job opening created from approved requisition", new="draft",
           meta={"requisition_id": str(r.id)})
    record(ctx, "requisition", r.id, "Job opening created", old="approved", new="approved",
           meta={"opening_id": str(o.id)})
    notify(ctx, recruiter_user_ids(ctx, opening=o) + user_ids_for_employees(ctx.db, [r.requested_by]),
           "Job opening ready to publish",
           f"{o.title}: the requisition was approved and a draft job opening was created.",
           "recruitment_opening", o.id)
    return o


# ═══════════════════════════════════════════════════════════════════════════
# Job openings
# ═══════════════════════════════════════════════════════════════════════════

def opening_status(o: models.JobOpening) -> str:
    return "paused" if o.status == "on_hold" else (o.status or "open")


def opening_stages(ctx: Ctx, o: models.JobOpening) -> list[dict]:
    if o.interview_stages is not None:
        return list(o.interview_stages)
    return list(get_settings(ctx.db, ctx.company_id)["default_interview_stages"])


def get_opening(ctx: Ctx, opening_id, *, lock: bool = False) -> models.JobOpening:
    q = select(models.JobOpening).where(models.JobOpening.id == uuid.UUID(str(opening_id)))
    if lock:
        q = q.with_for_update()
    o = ctx.db.scalar(q)
    if o is None or o.company_id != ctx.company_id or o.deleted_at is not None:
        raise err(404, "Job opening not found.")
    return o


def _application_count(db: Session, opening_id, statuses=None) -> int:
    q = select(func.count()).select_from(models.JobApplication).where(
        models.JobApplication.opening_id == opening_id, models.JobApplication.deleted_at.is_(None))
    if statuses:
        q = q.where(models.JobApplication.status.in_(statuses))
    return db.scalar(q) or 0


def opening_out(ctx: Ctx, o: models.JobOpening, counts: dict | None = None, *, detail: bool = False) -> dict:
    db = ctx.db
    comp = ctx.can_see_compensation
    st = opening_status(o)
    counts = counts if counts is not None else _opening_counts(db, [o.id]).get(o.id, {})
    hired = counts.get("hired", 0)
    active = sum(v for k, v in counts.items() if k in APPLICATION_ACTIVE)
    actions = []
    if ctx.can_edit:
        actions.append("edit")
        actions += {"draft": ["publish", "close"], "open": ["unpublish", "pause", "close"],
                    "paused": ["reopen", "close"], "closed": ["reopen"]}[st]
        actions.append("clone")
    out = {
        "id": str(o.id), "title": o.title, "status": st,
        "department_id": str(o.department_id) if o.department_id else None,
        "department_name": o.department.name if o.department else None,
        "branch_id": str(o.branch_id) if o.branch_id else None,
        "branch_name": _name_of(db, models.Branch, o.branch_id),
        "vacancies": o.vacancies, "hired": hired, "active_applications": active,
        "total_applications": sum(counts.values()),
        "employment_type": o.employment_type, "work_mode": o.work_mode,
        "reporting_manager": _emp_ref(db, o.reporting_manager_id),
        "experience_min": _num(o.experience_min), "experience_max": _num(o.experience_max),
        "salary_min": _num(o.salary_min) if comp else None, "salary_max": _num(o.salary_max) if comp else None,
        "required_skills": o.required_skills, "qualifications": o.qualifications,
        "description": o.description, "target_joining_date": _iso(o.target_joining_date),
        "hiring_team": [x for x in (_emp_ref(db, e) for e in _uuid_list(o.hiring_team)) if x],
        "interview_stages": opening_stages(ctx, o),
        "publish_scope": o.publish_scope or "internal",
        "posted_date": _iso(o.posted_date), "published_at": _iso(o.published_at), "closed_at": _iso(o.closed_at),
        "requisition_id": str(o.requisition_id) if o.requisition_id else None,
        "actions": actions, "compensation_visible": comp,
    }
    if detail:
        out["counts_by_status"] = counts
        out["history"] = history_for(ctx, entity_type="opening", entity_id=o.id)
    return out


def _opening_counts(db: Session, opening_ids) -> dict:
    if not opening_ids:
        return {}
    rows = db.execute(
        select(models.JobApplication.opening_id, models.JobApplication.status, func.count())
        .where(models.JobApplication.opening_id.in_(list(opening_ids)), models.JobApplication.deleted_at.is_(None))
        .group_by(models.JobApplication.opening_id, models.JobApplication.status)
    ).all()
    out: dict = {}
    for oid, st, n in rows:
        out.setdefault(oid, {})[st or "applied"] = n
    return out


def list_openings(ctx: Ctx, *, status: list[str] | None = None, department_id=None, branch_id=None,
                  search: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    db = ctx.db
    q = select(models.JobOpening).where(models.JobOpening.company_id == ctx.company_id,
                                        models.JobOpening.deleted_at.is_(None))
    if status:
        wanted = set(status)
        if "paused" in wanted:
            wanted.add("on_hold")
        q = q.where(models.JobOpening.status.in_(wanted))
    if department_id:
        q = q.where(models.JobOpening.department_id == department_id)
    if branch_id:
        q = q.where(models.JobOpening.branch_id == branch_id)
    if search:
        q = q.where(models.JobOpening.title.ilike(f"%{search.strip()}%"))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(models.JobOpening.posted_date.desc(), models.JobOpening.title)
                      .limit(limit).offset(offset)).all()
    counts = _opening_counts(db, [o.id for o in rows])
    return {"items": [opening_out(ctx, o, counts.get(o.id, {})) for o in rows], "total": total}


_OPENING_FIELDS = (
    "title", "vacancies", "description", "employment_type", "work_mode", "experience_min", "experience_max",
    "salary_min", "salary_max", "required_skills", "qualifications", "target_joining_date",
    "hiring_team", "publish_scope",
)


def _apply_opening_fields(ctx: Ctx, o: models.JobOpening, data: dict) -> None:
    _validate_common(ctx, data)
    if "department_id" in data:
        dep = department_in_company(ctx, data["department_id"])
        o.department_id = dep.id if dep else None
    if "branch_id" in data:
        br = branch_in_company(ctx, data["branch_id"])
        o.branch_id = br.id if br else None
    if "reporting_manager_id" in data:
        rm = employee_in_company(ctx, data["reporting_manager_id"], "Reporting manager")
        o.reporting_manager_id = rm.id if rm else None
    if "interview_stages" in data and data["interview_stages"] is not None:
        stages = normalize_stages(data["interview_stages"])
        if o.id is not None and stages != opening_stages(ctx, o) and _application_count(ctx.db, o.id, ["assessment", "interview"]):
            raise err(409, "Candidates are already in this opening's interview stages -- the stages can't be changed now.")
        o.interview_stages = stages
    for f in _OPENING_FIELDS:
        if f in data:
            v = data[f]
            if f == "title":
                v = (v or "").strip()
                if not v:
                    raise err(422, "Job title is required.")
            if f == "vacancies":
                v = int(v or 0)
                if v < 1 or v > 500:
                    raise err(422, "Number of openings must be between 1 and 500.")
                if o.requisition_id:
                    req = ctx.db.get(models.HiringRequisition, o.requisition_id)
                    if req is not None and v > req.positions_count:
                        raise err(422, f"The approved requisition allows {req.positions_count} opening(s).")
            if f == "publish_scope" and v not in (None, "internal", "external", "both"):
                raise err(422, "Publish scope must be internal, external or both.")
            if f == "target_joining_date":
                v = _parse_date(v)
            setattr(o, f, v)


def create_opening(ctx: Ctx, data: dict) -> models.JobOpening:
    """A job opening without a requisition (the pre-existing direct
    'Add Job Opening' path, Recruitment Edit)."""
    ctx.require_edit()
    if data.get("requisition_id"):
        req = get_requisition(ctx, uuid.UUID(str(data["requisition_id"])))
        return create_opening_from_requisition(ctx, req)
    o = models.JobOpening(id=uuid.uuid4(), company_id=ctx.company_id, title="", vacancies=1, status="draft",
                          employment_type="full_time", posted_date=datetime.date.today(), hiring_team=[],
                          created_by=ctx.user.id if ctx.user is not None else None)
    if data.get("interview_stages") is None:
        o.interview_stages = get_settings(ctx.db, ctx.company_id)["default_interview_stages"]
    _apply_opening_fields(ctx, o, data)
    if not o.department_id:
        raise err(422, "Department is required.")
    ctx.db.add(o)
    ctx.db.flush()
    _materialize_rounds_from_stages(ctx, o, o.interview_stages or [])
    record(ctx, "opening", o.id, "Job opening created (no requisition)", new="draft")
    if data.get("publish"):
        transition_opening(ctx, o, "publish")
    return o


def update_opening(ctx: Ctx, o: models.JobOpening, data: dict) -> None:
    ctx.require_edit()
    if opening_status(o) == "closed":
        raise err(409, "Reopen the job opening before editing it.")
    _apply_opening_fields(ctx, o, data)
    _materialize_rounds_from_stages(ctx, o, o.interview_stages or [])
    record(ctx, "opening", o.id, "Job opening edited", old=opening_status(o), new=opening_status(o))


def _set_opening_status(ctx: Ctx, o: models.JobOpening, new: str, comments: str | None = None, label: str | None = None) -> None:
    old = opening_status(o)
    o.status = new
    if new == "open":
        o.published_at = o.published_at or now()
        o.closed_at = None
    if new == "closed":
        o.closed_at = now()
    record(ctx, "opening", o.id, label or f"Job opening {new}", old=old, new=new, comments=comments)


def transition_opening(ctx: Ctx, o: models.JobOpening, action: str, comments: str | None = None) -> None:
    ctx.require_edit()
    st = opening_status(o)
    allowed = {"publish": ({"draft"}, "open", "Published"), "unpublish": ({"open"}, "draft", "Unpublished"),
               "pause": ({"open"}, "paused", "Paused"), "reopen": ({"paused", "closed"}, "open", "Reopened"),
               "close": ({"draft", "open", "paused"}, "closed", "Closed")}
    if action not in allowed:
        raise err(422, "Unknown job opening action.")
    sources, target, label = allowed[action]
    if st not in sources:
        raise err(409, f"A {st} job opening can't be {label.lower()}.")
    if action == "publish":
        missing = [x for x, v in (("department", o.department_id), ("description", (o.description or "").strip()))
                   if not v]
        if missing:
            raise err(422, "Complete the job opening before publishing: " + ", ".join(missing) + ".")
        if o.interview_stages is None:
            o.interview_stages = opening_stages(ctx, o)
    if action == "reopen" and o.requisition_id:
        req = ctx.db.get(models.HiringRequisition, o.requisition_id)
        if req is not None and req.status == "cancelled":
            raise err(409, "Its requisition was cancelled -- raise a new requisition.")
    _set_opening_status(ctx, o, target, comments, f"Job opening {label.lower()}")
    if action == "publish":
        o.posted_date = datetime.date.today()
    if o.requisition_id:
        req = ctx.db.get(models.HiringRequisition, o.requisition_id)
        if req is not None:
            if target == "closed" and req.status == "approved":
                req.status = "closed"
                record(ctx, "requisition", req.id, "Requisition closed (job opening closed)", old="approved", new="closed")
            elif target == "open" and req.status == "closed":
                req.status = "approved"
                record(ctx, "requisition", req.id, "Requisition reopened (job opening reopened)", old="closed", new="approved")


def clone_opening(ctx: Ctx, o: models.JobOpening) -> models.JobOpening:
    ctx.require_edit()
    c = models.JobOpening(
        id=uuid.uuid4(), company_id=ctx.company_id, title=f"{o.title} (Copy)"[:150], department_id=o.department_id,
        designation_id=o.designation_id, branch_id=o.branch_id, vacancies=o.vacancies, status="draft",
        description=o.description, employment_type=o.employment_type, posted_date=datetime.date.today(),
        work_mode=o.work_mode, reporting_manager_id=o.reporting_manager_id, experience_min=o.experience_min,
        experience_max=o.experience_max, salary_min=o.salary_min, salary_max=o.salary_max,
        required_skills=o.required_skills, qualifications=o.qualifications, hiring_team=list(o.hiring_team or []),
        interview_stages=opening_stages(ctx, o), publish_scope=o.publish_scope,
        created_by=ctx.user.id if ctx.user is not None else None,
    )
    ctx.db.add(c)
    ctx.db.flush()
    record(ctx, "opening", c.id, "Job opening cloned", new="draft", meta={"cloned_from": str(o.id)})
    return c


# ═══════════════════════════════════════════════════════════════════════════
# Candidates
# ═══════════════════════════════════════════════════════════════════════════

_CANDIDATE_FIELDS = (
    "name", "email", "phone", "years_experience", "current_company", "current_designation", "current_ctc",
    "expected_ctc", "notice_period_days", "current_location", "skills", "qualification",
    "work_authorization", "linkedin_url", "notes",
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _norm_phone(p: str | None) -> str:
    return re.sub(r"\D", "", p or "")[-10:]


def get_candidate(ctx: Ctx, candidate_id) -> models.Candidate:
    c = ctx.db.get(models.Candidate, uuid.UUID(str(candidate_id)))
    if c is None or c.company_id != ctx.company_id or c.deleted_at is not None:
        raise err(404, "Candidate not found.")
    return c


def find_duplicates(ctx: Ctx, email: str | None, phone: str | None, exclude_id=None) -> list[models.Candidate]:
    conds = []
    if (email or "").strip():
        conds.append(func.lower(models.Candidate.email) == email.strip().lower())
    ph = _norm_phone(phone)
    if len(ph) >= 7:
        conds.append(func.right(func.regexp_replace(models.Candidate.phone, r"\D", "", "g"), 10) == ph)
    if not conds:
        return []
    q = select(models.Candidate).where(models.Candidate.company_id == ctx.company_id,
                                       models.Candidate.deleted_at.is_(None), or_(*conds))
    if exclude_id:
        q = q.where(models.Candidate.id != exclude_id)
    return list(ctx.db.scalars(q.limit(10)).all())


def candidate_out(ctx: Ctx, c: models.Candidate, *, detail: bool = False) -> dict:
    comp = ctx.can_see_compensation
    out = {
        "id": str(c.id), "name": c.name, "email": c.email, "phone": c.phone,
        "years_experience": _num(c.years_experience), "current_company": c.current_company,
        "current_designation": c.current_designation,
        "current_ctc": _num(c.current_ctc) if comp else None,
        "expected_ctc": _num(c.expected_ctc) if comp else None,
        "notice_period_days": c.notice_period_days, "current_location": c.current_location,
        "skills": c.skills, "qualification": c.qualification, "work_authorization": c.work_authorization,
        "linkedin_url": c.linkedin_url, "notes": c.notes, "source": c.source,
        "has_resume": bool(c.resume_url), "resume_filename": c.resume_filename,
        "resume_uploaded_at": _iso(c.resume_uploaded_at), "created_at": _iso(c.created_at),
        "compensation_visible": comp,
    }
    if detail:
        apps = ctx.db.scalars(select(models.JobApplication).where(
            models.JobApplication.candidate_id == c.id, models.JobApplication.deleted_at.is_(None))
            .order_by(models.JobApplication.applied_at.desc().nulls_last())).all()
        out["applications"] = [application_summary(ctx, a) for a in apps]
    return out


def search_candidates(ctx: Ctx, search: str | None, limit: int = 20, offset: int = 0) -> dict:
    ctx.require_view()
    q = select(models.Candidate).where(models.Candidate.company_id == ctx.company_id,
                                       models.Candidate.deleted_at.is_(None))
    if search and search.strip():
        s = f"%{search.strip()}%"
        q = q.where(or_(models.Candidate.name.ilike(s), models.Candidate.email.ilike(s), models.Candidate.phone.ilike(s)))
    total = ctx.db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = ctx.db.scalars(q.order_by(models.Candidate.name).limit(limit).offset(offset)).all()
    return {"items": [candidate_out(ctx, c) for c in rows], "total": total}


def _apply_candidate_fields(ctx: Ctx, c: models.Candidate, data: dict) -> None:
    for f in _CANDIDATE_FIELDS:
        if f not in data:
            continue
        v = data[f]
        if isinstance(v, str):
            v = v.strip() or None
        if f == "name" and not v:
            raise err(422, "Candidate name is required.")
        if f == "email" and v and not _EMAIL_RE.match(v):
            raise err(422, "Enter a valid email address.")
        if f in ("years_experience", "current_ctc", "expected_ctc", "notice_period_days") and v is not None and v < 0:
            raise err(422, "Values cannot be negative.")
        if f in ("current_ctc", "expected_ctc") and not ctx.can_see_compensation:
            continue
        setattr(c, f, v)


def create_candidate(ctx: Ctx, data: dict, *, allow_duplicate: bool = False) -> models.Candidate:
    ctx.require_edit()
    dupes = find_duplicates(ctx, data.get("email"), data.get("phone"))
    if dupes and not allow_duplicate:
        raise err(409, "A candidate with this email or phone already exists.", code="duplicate_candidate",
                  existing=[candidate_out(ctx, d) for d in dupes])
    if not str(data.get("name") or "").strip():  # M-40: {} is not a candidate
        raise err(422, "Candidate name is required.")
    c = models.Candidate(id=uuid.uuid4(), company_id=ctx.company_id, name="", source=data.get("source"),
                         created_at=now(), created_by=ctx.user.id)
    _apply_candidate_fields(ctx, c, data)
    ctx.db.add(c)
    ctx.db.flush()
    record(ctx, "candidate", c.id, "Candidate created", candidate_id=c.id)
    return c


def update_candidate(ctx: Ctx, c: models.Candidate, data: dict) -> None:
    ctx.require_edit()
    if "email" in data or "phone" in data:
        dupes = find_duplicates(ctx, data.get("email", c.email), data.get("phone", c.phone), exclude_id=c.id)
        if dupes:
            raise err(409, f"Another candidate ({dupes[0].name}) already has this email or phone.",
                      code="duplicate_candidate", existing=[candidate_out(ctx, d) for d in dupes])
    _apply_candidate_fields(ctx, c, data)
    c.updated_at = now()
    record(ctx, "candidate", c.id, "Candidate profile updated", candidate_id=c.id)


# ═══════════════════════════════════════════════════════════════════════════
# Applications (the pipeline entity)
# ═══════════════════════════════════════════════════════════════════════════

def app_status(a: models.JobApplication) -> str:
    return a.status or _LEGACY_TO_STATUS.get((a.stage or "applied").lower(), "applied")


def get_application(ctx: Ctx, application_id, *, lock: bool = False) -> models.JobApplication:
    q = select(models.JobApplication).where(models.JobApplication.id == uuid.UUID(str(application_id)))
    if lock:
        q = q.with_for_update()
    a = ctx.db.scalar(q)
    if a is None or a.deleted_at is not None or a.opening is None or a.opening.company_id != ctx.company_id:
        raise err(404, "Application not found.")
    if a.status is None:
        a.status = app_status(a)
    return a


def is_panelist(ctx: Ctx, a: models.JobApplication) -> bool:
    if ctx.user is None or ctx.user.employee_id is None:
        return False
    return bool(ctx.db.scalar(
        select(func.count()).select_from(models.InterviewParticipant)
        .join(models.Interview, models.Interview.id == models.InterviewParticipant.interview_id)
        .where(models.Interview.application_id == a.id,
               models.InterviewParticipant.employee_id == ctx.user.employee_id)))


def require_application_view(ctx: Ctx, a: models.JobApplication) -> None:
    if ctx.can_view or is_panelist(ctx, a):
        return
    raise err(403, "You don't have access to this application.")


def _set_status(ctx: Ctx, a: models.JobApplication, new: str) -> None:
    a.status = new
    if new in _LEGACY_STAGE:
        a.stage = _LEGACY_STAGE[new]
    a.current_stage_entered_at = now()
    a.updated_at = now()


def _stage_at(ctx: Ctx, a: models.JobApplication, index: int | None) -> dict | None:
    stages = opening_stages(ctx, a.opening)
    if index is None or index < 0 or index >= len(stages):
        return None
    return stages[index]


def _has_active_round(ctx: Ctx, opening_id, stage_key: str) -> bool:
    """A stage whose round was already defined (panel + schedule, set right
    in the stage editor or via Define Round) has nothing left to manually
    schedule or invite interviewers for -- candidates just auto-attach."""
    return ctx.db.scalar(select(models.InterviewRound.id).where(
        models.InterviewRound.opening_id == opening_id, models.InterviewRound.round_key == stage_key,
        models.InterviewRound.status == "scheduled")) is not None


def _stage_interviews(ctx: Ctx, a: models.JobApplication, stage_key: str) -> list[models.Interview]:
    return list(ctx.db.scalars(select(models.Interview).where(
        models.Interview.application_id == a.id, models.Interview.stage_key == stage_key)).all())


def stage_requirement(ctx: Ctx, a: models.JobApplication, stage: dict) -> tuple[bool, str]:
    """Is the current stage's requirement met (so it can be passed)?"""
    ivs = [i for i in _stage_interviews(ctx, a, stage["key"]) if i.status not in ("cancelled", "no_show")]
    if not ivs:
        return False, f"Schedule and complete the {stage['name']} first."
    round_ivs = [i for i in ivs if i.round_id is not None]
    if round_ivs:
        # Round-based stage: the organizer's internal decision drives it,
        # not per-interviewer feedback -- see complete_round_decision().
        if any(i.status in ROUND_INSTANCE_OPEN for i in round_ivs):
            return False, f"Complete (or cancel) the {stage['name']} interview first."
        if any(i.status == "ended" for i in round_ivs):
            return False, f"Record the internal decision for the {stage['name']} first."
        if not any(i.status == "completed" and i.hr_decision == "advance" for i in round_ivs):
            return False, f"The {stage['name']} hasn't been marked as Advance yet."
        return True, ""
    if any(i.status in INVITATION_PHASE for i in ivs):
        return False, f"The {stage['name']} is still waiting on interviewers / a time -- schedule it (or cancel it) first."
    if any(i.status in OPEN_INTERVIEW for i in ivs):
        return False, f"Complete (or cancel) the scheduled {stage['name']} interview first."
    if stage.get("feedback_required", True):
        if not any(i.status == "feedback_completed" for i in ivs):
            return False, f"Mandatory feedback for the {stage['name']} is still pending."
        given = {f.interviewer_id for i in ivs for f in i.feedback_entries}
        need = int(stage.get("min_interviewers") or 1)
        if len(given) < need:
            return False, f"The {stage['name']} needs feedback from at least {need} interviewer(s) ({len(given)} so far)."
    return True, ""


def mandatory_stages_done(ctx: Ctx, a: models.JobApplication) -> tuple[bool, list[str]]:
    results = a.stage_results or {}
    missing = [s["name"] for s in opening_stages(ctx, a.opening)
               if s.get("mandatory", True) and (results.get(s["key"]) or {}).get("result") != "pass"]
    return not missing, missing


def allowed_actions(ctx: Ctx, a: models.JobApplication) -> list[str]:
    if not ctx.can_edit:
        return []
    st = app_status(a)
    stages = opening_stages(ctx, a.opening)
    idx = a.stage_index
    acts: list[str] = []
    if st == "applied":
        acts = ["start_screening", "reject", "hold", "withdraw"]
    elif st == "screening":
        acts = ["shortlist", "request_info", "reject", "hold", "withdraw"]
    elif st == "shortlisted":
        if stages:
            if not _has_active_round(ctx, a.opening_id, stages[0]["key"]):
                acts.append("schedule_interview")
                acts.append("invite_interviewers")
            acts.append("advance")
        if mandatory_stages_done(ctx, a)[0]:
            acts.append("select")
        acts += ["reject", "hold", "withdraw"]
    elif st in ("assessment", "interview"):
        stage = _stage_at(ctx, a, idx)
        if idx is None and stages:
            # Legacy application (moved to Interview before stages existed):
            # enter the opening's first configured stage.
            acts.append("advance")
        if stage is not None:
            if not _has_active_round(ctx, a.opening_id, stage["key"]):
                acts.append("schedule_interview")
                if not any(x.status in OPEN_INTERVIEW for x in _stage_interviews(ctx, a, stage["key"])):
                    acts.append("invite_interviewers")
            if stage_requirement(ctx, a, stage)[0]:
                acts.append("pass_stage")
            if not stage.get("mandatory", True):
                acts.append("skip_stage")
        if stage is None and mandatory_stages_done(ctx, a)[0]:
            acts.append("select")
        acts += ["reject", "hold", "withdraw"]
    elif st == "selected":
        acts = ["create_offer", "reject", "hold", "withdraw"]
    elif st in ("offer", "preboarding"):
        acts = ["withdraw"]
    elif st == "on_hold":
        acts = ["resume", "reject", "withdraw"]
    elif st in ("rejected", "withdrawn", "closed") and ctx.can_admin:
        if opening_status(a.opening) != "closed":
            acts = ["reopen"]
    return acts


def application_summary(ctx: Ctx, a: models.JobApplication) -> dict:
    st = app_status(a)
    stage = _stage_at(ctx, a, a.stage_index) if st in ("assessment", "interview") else None
    return {
        "id": str(a.id), "status": st, "status_label": STATUS_LABELS.get(st, st.title()),
        "opening_id": str(a.opening_id), "opening_title": a.opening.title if a.opening else None,
        "opening_status": opening_status(a.opening) if a.opening else None,
        "department_name": a.opening.department.name if a.opening and a.opening.department else None,
        "candidate_id": str(a.candidate_id), "candidate_name": a.candidate.name if a.candidate else None,
        "candidate_email": a.candidate.email if a.candidate else None,
        "years_experience": _num(a.candidate.years_experience) if a.candidate else None,
        "source": a.source or (a.candidate.source if a.candidate else None),
        "owner": _emp_ref(ctx.db, a.owner_id),
        "current_stage": stage["name"] if stage else None,
        "stage_index": a.stage_index,
        "stages_total": len(opening_stages(ctx, a.opening)) if a.opening else 0,
        "applied_at": _iso(a.applied_at or a.created_at),
        "current_stage_entered_at": _iso(a.current_stage_entered_at),
        "rejection_reason": a.rejection_reason, "hold_reason": a.hold_reason,
        "hold_review_date": _iso(a.hold_review_date), "previous_status": a.previous_status,
        "closed_reason": a.closed_reason, "employee_id": str(a.employee_id) if a.employee_id else None,
        "rating": a.rating,
    }


def application_out(ctx: Ctx, a: models.JobApplication, *, detail: bool = False) -> dict:
    out = application_summary(ctx, a)
    out["actions"] = allowed_actions(ctx, a)
    if not detail:
        return out
    from . import recruitment_onboarding as onb

    stages = opening_stages(ctx, a.opening)
    results = a.stage_results or {}
    in_stages = app_status(a) in ("assessment", "interview")
    stage_rows = []
    for i, s in enumerate(stages):
        r = results.get(s["key"]) or {}
        if r.get("result"):
            state = r["result"]
        elif in_stages and a.stage_index == i:
            state = "current"
        else:
            state = "upcoming"
        row = {**s, "state": state, "decided_at": r.get("at"), "decided_by": r.get("by"), "comments": r.get("comments")}
        if in_stages and a.stage_index == i:
            ok, why = stage_requirement(ctx, a, s)
            row["requirement_met"], row["requirement_message"] = ok, why
        stage_rows.append(row)
    done, missing = mandatory_stages_done(ctx, a)
    out.update({
        "candidate": candidate_out(ctx, a.candidate),
        "opening": opening_out(ctx, a.opening),
        "notes": a.notes, "rejection_notes": a.rejection_notes,
        "withdrawal_reason": a.withdrawal_reason, "withdrawal_source": a.withdrawal_source,
        "stages": stage_rows, "mandatory_stages_complete": done, "missing_stages": missing,
        "interviews": [interview_out(ctx, i) for i in ctx.db.scalars(
            select(models.Interview).where(models.Interview.application_id == a.id)
            .order_by(models.Interview.scheduled_at)).all()],
        "other_applications": [application_summary(ctx, x) for x in ctx.db.scalars(
            select(models.JobApplication).where(models.JobApplication.candidate_id == a.candidate_id,
                                                models.JobApplication.id != a.id,
                                                models.JobApplication.deleted_at.is_(None))).all()],
        "history": history_for(ctx, application_id=a.id),
        "rejection_reasons": get_settings(ctx.db, ctx.company_id)["rejection_reasons"],
    })
    if ctx.can_view:
        out["offers"] = [onb.offer_out(ctx, o) for o in onb.offers_for_application(ctx, a)]
        pb = ctx.db.scalar(select(models.Preboarding).where(models.Preboarding.application_id == a.id))
        out["preboarding"] = onb.preboarding_summary(ctx, pb) if pb else None
    return out


def list_applications(ctx: Ctx, *, opening_id=None, status: list[str] | None = None, source: str | None = None,
                      owner_id=None, search: str | None = None, department_id=None, date_from=None, date_to=None,
                      exp_min: float | None = None, exp_max: float | None = None,
                      limit: int = 50, offset: int = 0) -> dict:
    ctx.require_view()
    q = (select(models.JobApplication)
         .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
         .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
         .where(models.JobOpening.company_id == ctx.company_id, models.JobApplication.deleted_at.is_(None)))
    if opening_id:
        q = q.where(models.JobApplication.opening_id == opening_id)
    if status:
        q = q.where(models.JobApplication.status.in_(status))
    if source:
        q = q.where(models.JobApplication.source == source)
    if owner_id:
        q = q.where(models.JobApplication.owner_id == owner_id)
    if department_id:
        q = q.where(models.JobOpening.department_id == department_id)
    if search and search.strip():
        s = f"%{search.strip()}%"
        q = q.where(or_(models.Candidate.name.ilike(s), models.Candidate.email.ilike(s), models.JobOpening.title.ilike(s)))
    if date_from:
        q = q.where(models.JobApplication.applied_at >= date_from)
    if date_to:
        q = q.where(models.JobApplication.applied_at < date_to + datetime.timedelta(days=1))
    if exp_min is not None:
        q = q.where(models.Candidate.years_experience >= exp_min)
    if exp_max is not None:
        q = q.where(models.Candidate.years_experience <= exp_max)
    total = ctx.db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = ctx.db.scalars(q.order_by(models.JobApplication.current_stage_entered_at.desc().nulls_last())
                          .limit(limit).offset(offset)).all()
    return {"items": [application_out(ctx, a) for a in rows], "total": total}


def pipeline(ctx: Ctx, *, opening_id=None, search: str | None = None, source: str | None = None,
             owner_id=None, per_column: int = 50) -> dict:
    """Kanban columns (active stages) + counts for the off-board statuses."""
    ctx.require_view()
    base = (select(models.JobApplication.status, func.count())
            .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
            .join(models.Candidate, models.Candidate.id == models.JobApplication.candidate_id)
            .where(models.JobOpening.company_id == ctx.company_id, models.JobApplication.deleted_at.is_(None)))
    if opening_id:
        base = base.where(models.JobApplication.opening_id == opening_id)
    if source:
        base = base.where(models.JobApplication.source == source)
    if owner_id:
        base = base.where(models.JobApplication.owner_id == owner_id)
    if search and search.strip():
        s = f"%{search.strip()}%"
        base = base.where(or_(models.Candidate.name.ilike(s), models.Candidate.email.ilike(s)))
    counts = dict(ctx.db.execute(base.group_by(models.JobApplication.status)).all())
    columns = []
    for st in PIPELINE_COLUMNS:
        items = list_applications(ctx, opening_id=opening_id, status=[st], source=source, owner_id=owner_id,
                                  search=search, limit=per_column)["items"] if counts.get(st) else []
        columns.append({"status": st, "label": STATUS_LABELS[st], "count": counts.get(st, 0), "items": items})
    return {"columns": columns,
            "off_board": {st: counts.get(st, 0) for st in ("on_hold", "rejected", "withdrawn", "closed")}}


def create_application(ctx: Ctx, candidate_id, opening_id, *, source: str | None, owner_id=None,
                       notes: str | None = None) -> models.JobApplication:
    ctx.require_edit()
    c = get_candidate(ctx, candidate_id)
    o = get_opening(ctx, opening_id, lock=True)
    if opening_status(o) != "open":
        raise err(409, f"This job opening is {opening_status(o)} -- publish / reopen it to add applications.")
    existing = ctx.db.scalar(select(models.JobApplication).where(
        models.JobApplication.candidate_id == c.id, models.JobApplication.opening_id == o.id,
        models.JobApplication.deleted_at.is_(None)))
    if existing is not None:
        st = app_status(existing)
        raise err(409, f"{c.name} already has an application for {o.title} ({STATUS_LABELS.get(st, st)})"
                  + (" -- reopen it instead." if st in APPLICATION_TERMINAL and st != "hired" else "."),
                  code="duplicate_application", application_id=str(existing.id))
    sources = get_settings(ctx.db, ctx.company_id)["sources"]
    if source and source not in sources:
        raise err(422, "Unknown candidate source.")
    owner = employee_in_company(ctx, owner_id, "Recruiter") if owner_id else None
    a = models.JobApplication(
        id=uuid.uuid4(), opening_id=o.id, candidate_id=c.id, stage="applied", status="applied",
        stage_results={}, source=source or c.source, owner_id=owner.id if owner else ctx.user.employee_id,
        applied_at=now(), current_stage_entered_at=now(), notes=notes, created_at=now(), created_by=ctx.user.id,
    )
    if c.source is None and source:
        c.source = source
    ctx.db.add(a)
    ctx.db.flush()
    ctx.db.refresh(a)
    record(ctx, "application", a.id, "Application created", new="applied", application=a,
           meta={"opening": o.title, "source": a.source})
    return a


def transition(ctx: Ctx, a: models.JobApplication, action: str, data: dict | None = None) -> None:
    """The single entry point for every application stage change."""
    data = data or {}
    ctx.require_edit()
    st = app_status(a)
    if action == "move":
        action = _action_for_target(ctx, a, data.get("target"))
    if action not in allowed_actions(ctx, a):
        raise err(409, _why_not(ctx, a, action))
    comments = (data.get("comments") or "").strip() or None
    stages = opening_stages(ctx, a.opening)

    if action == "start_screening":
        _move(ctx, a, "screening", "Moved to Screening", comments)
    elif action == "shortlist":
        _move(ctx, a, "shortlisted", "Shortlisted", comments)
    elif action == "request_info":
        msg = (data.get("message") or comments or "").strip()
        if not msg:
            raise err(422, "Say what information is needed.")
        record(ctx, "application", a.id, "More information requested", old=st, new=st, comments=msg, application=a)
        if data.get("email_candidate") and a.candidate and a.candidate.email:
            from . import recruitment_onboarding as onb
            onb.email_candidate(ctx, a.candidate, f"Additional information requested - {a.opening.title}",
                                "We need a little more information", msg, related=("recruitment_application", a.id))
    elif action == "advance":
        if not stages:
            raise err(409, "Nothing to advance to.")
        interview = _enter_stage(ctx, a, 0, comments)
        if data.get("email_candidate"):
            _email_next_step(ctx, a, None, stages[0], interview)
    elif action == "pass_stage":
        stage = _stage_at(ctx, a, a.stage_index)
        ok, why = stage_requirement(ctx, a, stage)
        if not ok:
            raise err(409, why)
        _record_stage_result(ctx, a, stage, "pass", comments)
        record(ctx, "application", a.id, f"Passed {stage['name']}", old=st, new=st, comments=comments, application=a)
        next_stage, interview = _next_stage(ctx, a, comments)
        if data.get("email_candidate"):
            _email_next_step(ctx, a, stage["name"], next_stage, interview)
    elif action == "skip_stage":
        stage = _stage_at(ctx, a, a.stage_index)
        if not comments:
            raise err(422, "Give a reason for skipping this optional stage.")
        _record_stage_result(ctx, a, stage, "skipped", comments)
        record(ctx, "application", a.id, f"Skipped {stage['name']} (optional)", old=st, new=st, comments=comments, application=a)
        _next_stage(ctx, a, comments)
    elif action == "select":
        done, missing = mandatory_stages_done(ctx, a)
        if not done:
            raise err(409, "Mandatory stages are not complete: " + ", ".join(missing) + ".")
        a.stage_index = None
        _move(ctx, a, "selected", "Candidate selected", comments)
        notify(ctx, recruiter_user_ids(ctx, a), "Candidate selected",
               f"{a.candidate.name} was selected for {a.opening.title} -- prepare the offer.",
               "recruitment_application", a.id, email=False)
    elif action == "reject":
        reason = (data.get("reason") or "").strip()
        if not reason:
            raise err(422, "A rejection reason is required.")
        a.previous_status = st if st != "on_hold" else a.previous_status
        a.rejection_reason = reason[:200]
        a.rejection_notes = comments
        stage = _stage_at(ctx, a, a.stage_index) if st in ("assessment", "interview") else None
        if stage is not None:
            _record_stage_result(ctx, a, stage, "fail", reason)
        _cancel_open_interviews(ctx, a, "Candidate rejected")
        _move(ctx, a, "rejected", "Candidate rejected" + (f" at {stage['name']}" if stage else ""),
              reason + (f" -- {comments}" if comments else ""))
        if data.get("email_candidate"):
            _email_candidate_rejected(ctx, a)
    elif action == "hold":
        reason = (data.get("reason") or "").strip()
        if not reason:
            raise err(422, "A hold reason is required.")
        a.previous_status = st
        a.hold_reason = reason
        a.hold_review_date = _parse_date(data.get("review_date"))
        _move(ctx, a, "on_hold", "Put on hold", reason, meta={"review_date": _iso(a.hold_review_date)})
    elif action == "resume":
        target = a.previous_status or "screening"
        a.hold_reason = None
        a.hold_review_date = None
        a.previous_status = None
        _move(ctx, a, target, f"Resumed -- back to {STATUS_LABELS.get(target, target)}", comments)
    elif action == "withdraw":
        from . import recruitment_onboarding as onb
        a.previous_status = st if st != "on_hold" else a.previous_status
        a.withdrawal_source = (data.get("source") or "candidate")[:20]
        a.withdrawal_reason = (data.get("reason") or comments or "").strip() or None
        _cancel_open_interviews(ctx, a, "Candidate withdrew")
        onb.on_application_withdrawn(ctx, a)
        _move(ctx, a, "withdrawn", "Application withdrawn", a.withdrawal_reason,
              meta={"source": a.withdrawal_source})
    elif action == "reopen":
        if not comments:
            raise err(422, "Give a reason for reopening this application.")
        target = a.previous_status if a.previous_status in APPLICATION_ACTIVE and a.previous_status != "on_hold" else "screening"
        if target in ("offer", "preboarding"):
            target = "selected"
        if target in ("assessment", "interview") and a.stage_index is None:
            target = "shortlisted"
        old = st
        a.rejection_reason = None
        a.rejection_notes = None
        a.closed_reason = None
        a.previous_status = None
        _move(ctx, a, target, f"Reopened ({STATUS_LABELS.get(old, old)} -> {STATUS_LABELS.get(target, target)})", comments)
    elif action in ("schedule_interview", "invite_interviewers", "create_offer"):
        raise err(409, "Use the interview / offer endpoints for this action.")
    else:
        raise err(422, "Unknown action.")


def _parse_date(v) -> datetime.date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime.date):
        return v
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except ValueError as exc:
        raise err(422, "Invalid date.") from exc


def _move(ctx: Ctx, a: models.JobApplication, new: str, label: str, comments: str | None = None, meta: dict | None = None) -> None:
    old = app_status(a)
    _set_status(ctx, a, new)
    record(ctx, "application", a.id, label, old=old, new=new, comments=comments, application=a, meta=meta)


def _record_stage_result(ctx: Ctx, a: models.JobApplication, stage: dict, result: str, comments: str | None) -> None:
    results = dict(a.stage_results or {})
    results[stage["key"]] = {"result": result, "at": now().isoformat(), "by": ctx.actor_name, "comments": comments}
    a.stage_results = results


def _enter_stage(ctx: Ctx, a: models.JobApplication, index: int,
                 comments: str | None = None) -> models.Interview | None:
    """Returns the round interview the candidate was given for this stage
    (its schedule email already went out), if any."""
    stage = opening_stages(ctx, a.opening)[index]
    if a.opening.interview_stages is None:
        a.opening.interview_stages = opening_stages(ctx, a.opening)
    a.stage_index = index
    new = "assessment" if stage["type"] == "assessment" else "interview"
    _move(ctx, a, new, f"Moved to {stage['name']}", comments, meta={"stage": stage["key"]})
    return _attach_round_if_configured(ctx, a, stage)


def _next_stage(ctx: Ctx, a: models.JobApplication, comments: str | None
                ) -> tuple[dict | None, models.Interview | None]:
    """Moves on to the next stage. Returns (that stage or None when every
    stage is done, the round interview it gave the candidate or None)."""
    stages = opening_stages(ctx, a.opening)
    nxt = (a.stage_index or 0) + 1
    if nxt < len(stages):
        return stages[nxt], _enter_stage(ctx, a, nxt, comments)
    else:
        a.stage_index = len(stages)
        a.current_stage_entered_at = now()
        record(ctx, "application", a.id, "All interview stages completed -- awaiting final selection",
               old=app_status(a), new=app_status(a), application=a)
        notify(ctx, recruiter_user_ids(ctx, a), "Ready for final selection",
               f"{a.candidate.name} completed every interview stage for {a.opening.title}.",
               "recruitment_application", a.id, email=False)
        return None, None


def _cancel_open_interviews(ctx: Ctx, a: models.JobApplication, reason: str) -> None:
    for i in ctx.db.scalars(select(models.Interview).where(
            models.Interview.application_id == a.id,
            models.Interview.status.in_(OPEN_INTERVIEW))).all():
        _close_invitation_notices(ctx, i)
        i.status = "cancelled"
        i.cancel_reason = reason
        record(ctx, "interview", i.id, "Interview cancelled", old="scheduled", new="cancelled", comments=reason, application=a)
        notify(ctx, user_ids_for_employees(ctx.db, [p.employee_id for p in i.participants]),
               "Interview cancelled", f"{a.candidate.name} -- {i.stage_name or 'interview'}: {reason}.",
               "recruitment_interview", i.id)


_TARGET_ACTION = {
    ("applied", "screening"): "start_screening", ("screening", "shortlisted"): "shortlist",
    ("shortlisted", "interview"): "advance", ("shortlisted", "assessment"): "advance",
    ("interview", "interview"): "pass_stage", ("interview", "assessment"): "pass_stage",
    ("assessment", "interview"): "pass_stage", ("assessment", "assessment"): "pass_stage",
    ("shortlisted", "selected"): "select", ("interview", "selected"): "select",
    ("assessment", "selected"): "select",
}


def _action_for_target(ctx: Ctx, a: models.JobApplication, target: str | None) -> str:
    """Drag-and-drop: a target column -> the one business action that gets
    there, or 409 (the board can never bypass the rules)."""
    st = app_status(a)
    if not target or (target == st and st not in ("interview", "assessment")):
        raise err(409, "The candidate is already in that stage.")
    if target in ("rejected", "on_hold", "withdrawn"):
        raise err(409, "Use Reject / Hold / Withdraw -- they need a reason.")
    if target in ("offer", "preboarding", "hired"):
        raise err(409, {"offer": "Create an offer from the candidate's Offer step.",
                        "preboarding": "Preboarding starts automatically when the candidate accepts the offer.",
                        "hired": "Candidates become Hired when their employee record is created."}[target])
    if st == "on_hold":
        if target != (a.previous_status or "screening"):
            raise err(409, f"This candidate resumes to {STATUS_LABELS.get(a.previous_status or 'screening')}.")
        return "resume"
    action = _TARGET_ACTION.get((st, target))
    if action is None:
        raise err(409, f"A candidate can't move from {STATUS_LABELS.get(st, st)} to {STATUS_LABELS.get(target, target)}"
                  " directly -- required stages can't be skipped.")
    return action


def _why_not(ctx: Ctx, a: models.JobApplication, action: str) -> str:
    st = app_status(a)
    if action == "pass_stage":
        stage = _stage_at(ctx, a, a.stage_index)
        if stage is not None:
            return stage_requirement(ctx, a, stage)[1] or "This stage can't be passed yet."
    if action == "select":
        done, missing = mandatory_stages_done(ctx, a)
        if not done:
            return "Mandatory stages are not complete: " + ", ".join(missing) + "."
    if action == "reopen" and not ctx.can_admin:
        return "Only a Recruitment Admin can reopen an application."
    if action == "reopen" and st in ("rejected", "withdrawn", "closed"):
        return "Reopen the job opening first."
    if action == "reject" and st == "offer":
        return "Withdraw the active offer before rejecting the candidate."
    return f"'{action.replace('_', ' ')}' isn't allowed while the application is {STATUS_LABELS.get(st, st)}."


# ═══════════════════════════════════════════════════════════════════════════
# Interviews + feedback
# ═══════════════════════════════════════════════════════════════════════════

def get_interview(ctx: Ctx, interview_id, *, lock: bool = False) -> models.Interview:
    q = select(models.Interview).where(models.Interview.id == uuid.UUID(str(interview_id)))
    if lock:
        q = q.with_for_update()
    i = ctx.db.scalar(q)
    if i is None or i.application is None or i.application.opening.company_id != ctx.company_id:
        raise err(404, "Interview not found.")
    if i.status is None:
        i.status = "scheduled" if i.result is None else "feedback_completed"
    return i


def _participant_ids(i: models.Interview) -> list[uuid.UUID]:
    """The active panel: everyone invited except interviewers who declined
    (their row is kept, with the reason, for the history)."""
    if not i.participants:
        return [i.interviewer_id] if i.interviewer_id else []
    return [p.employee_id for p in i.participants if p.attendance != "declined"]


def _participant(i: models.Interview, employee_id) -> models.InterviewParticipant | None:
    return next((p for p in i.participants if p.employee_id == employee_id), None)


def is_interview_participant(ctx: Ctx, i: models.Interview) -> bool:
    """On the panel -- or invited and declined (they still see their answer)."""
    if ctx.user is None or ctx.user.employee_id is None:
        return False
    me = ctx.user.employee_id
    return me in _participant_ids(i) or _participant(i, me) is not None


def _is_active_participant(ctx: Ctx, i: models.Interview) -> bool:
    return ctx.user is not None and ctx.user.employee_id is not None and ctx.user.employee_id in _participant_ids(i)


# An interviewer's answer, kept on their participant row
# (hcm_interview_participants.attendance): None/"pending" = not answered yet.
INVITE_RESPONSES = {"accept": "accepted", "decline": "declined", "reject": "declined", "reschedule": "reschedule"}


def _responses(i: models.Interview) -> dict:
    return {p.employee_id: (p.attendance or "pending") for p in i.participants}


def _interview_end(i: models.Interview) -> datetime.datetime | None:
    return i.scheduled_end or (i.scheduled_at + datetime.timedelta(hours=1) if i.scheduled_at else None)


def response_options(ctx: Ctx, i: models.Interview) -> list[str]:
    """What the caller can answer right now: before a time is set, accept /
    decline (availability); once scheduled, accept / decline / reschedule."""
    if not _is_active_participant(ctx, i) or i.status not in INVITATION_PHASE + BOOKED_INTERVIEW:
        return []
    end = _interview_end(i)
    if end is not None and end <= now():
        return []
    part = _participant(i, ctx.user.employee_id)
    mine = (part.attendance if part is not None else None) or "pending"
    if i.scheduled_at is None:
        return ["accept", "decline"] if mine == "pending" else ["decline"]
    opts = [] if mine == "accepted" else ["accept"]
    opts.append("decline")
    if mine != "reschedule":
        opts.append("reschedule")
    return opts


def can_respond_to_invitation(ctx: Ctx, i: models.Interview) -> bool:
    return bool(response_options(ctx, i))


def opening_creator_user_id(ctx: Ctx, o: models.JobOpening) -> uuid.UUID | None:
    """Who created the job opening (openings made before created_by was
    recorded: the actor of its first history entry)."""
    if o.created_by:
        return o.created_by
    memo = ctx.db.info.setdefault("_opening_creator", {})
    if o.id not in memo:
        memo[o.id] = ctx.db.scalar(
            select(models.RecruitmentHistory.actor_user_id)
            .where(models.RecruitmentHistory.entity_type == "opening", models.RecruitmentHistory.entity_id == o.id,
                   models.RecruitmentHistory.actor_user_id.is_not(None))
            .order_by(models.RecruitmentHistory.created_at).limit(1))
    return memo[o.id]


def is_interview_organizer(ctx: Ctx, a: models.JobApplication) -> bool:
    """Recruitment editors and the job opening's creator run the invitations."""
    if ctx.can_edit:
        return True
    return ctx.user is not None and a is not None and opening_creator_user_id(ctx, a.opening) == ctx.user.id


def require_interview_organizer(ctx: Ctx, a: models.JobApplication) -> None:
    if not is_interview_organizer(ctx, a):
        raise err(403, "Only the job opening's creator or a Recruitment editor can manage interview invitations.")


def _organizer_user_ids(ctx: Ctx, i: models.Interview) -> set:
    """Told about every answer: the job opening's creator and whoever sent
    the invitations -- not the whole hiring team."""
    ids = {opening_creator_user_id(ctx, i.application.opening), i.created_by}
    ids.update(p.invited_by for p in i.participants if p.invited_by)
    return {u for u in ids if u}


def _sync_invitation_status(i: models.Interview) -> None:
    """Interview status follows the panel's answers."""
    if i.status not in INVITATION_PHASE + BOOKED_INTERVIEW:
        return
    active = [p for p in i.participants if p.attendance != "declined"]
    if i.scheduled_at is None:
        i.status = "ready_to_schedule" if any(p.attendance == "accepted" for p in active) else "invited"
    else:
        i.status = "reschedule_requested" if any(p.attendance == "reschedule" for p in active) else "scheduled"


def _close_invitation_notices(ctx: Ctx, i: models.Interview, employee_ids=None) -> None:
    """An answered / withdrawn invitation's notifications are marked read."""
    uids = user_ids_for_employees(ctx.db, employee_ids if employee_ids is not None
                                  else [p.employee_id for p in i.participants])
    if uids:
        ctx.db.execute(update(models.Notification).where(
            models.Notification.user_id.in_(uids), models.Notification.entity_type == "interview_invitation",
            models.Notification.entity_id == i.id, models.Notification.read_at.is_(None)).values(read_at=now()))


def _fmt_when(dt: datetime.datetime | None) -> str:
    return dt.astimezone(crud.company_tzinfo()).strftime("%d %b %Y, %I:%M %p") if dt else "time to be scheduled"


def _check_conflicts(ctx: Ctx, a: models.JobApplication, employee_ids, start: datetime.datetime,
                     end: datetime.datetime, exclude_id=None) -> None:
    """No interviewer (and not the candidate) in two booked interviews at once."""
    I = models.Interview
    q = (select(I).join(models.JobApplication, models.JobApplication.id == I.application_id)
         .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
         .where(models.JobOpening.company_id == ctx.company_id, I.status.in_(BOOKED_INTERVIEW),
                I.scheduled_at.is_not(None), I.scheduled_at < end,
                I.scheduled_at > start - datetime.timedelta(days=1)))
    if exclude_id is not None:
        q = q.where(I.id != exclude_id)
    wanted = set(employee_ids or [])
    for other in ctx.db.scalars(q).all():
        if _interview_end(other) <= start:
            continue
        when = _fmt_when(other.scheduled_at)
        if other.application_id == a.id:
            raise err(409, f"{a.candidate.name} already has the {other.stage_name or 'interview'} at {when} "
                           f"-- pick another time.")
        busy = wanted & set(_participant_ids(other))
        if busy:
            names = ", ".join(sorted(crud.employee_display_name(ctx.db, e) for e in busy))
            raise err(409, f"{names} already has an interview at {when} "
                           f"({other.application.candidate.name}, {other.stage_name or 'interview'}) "
                           f"-- pick another time or interviewer.")


def _panel(ctx: Ctx, ids, *, required: bool = True) -> list[uuid.UUID]:
    panel: list[uuid.UUID] = []
    for e in ids or []:
        emp = employee_in_company(ctx, e, "Interviewer")
        if emp is not None and emp.id not in panel:
            panel.append(emp.id)
    if required and not panel:
        raise err(422, "Add at least one interviewer.")
    return panel


def _invite_participant(ctx: Ctx, i: models.Interview, employee_id: uuid.UUID) -> models.InterviewParticipant:
    p = models.InterviewParticipant(id=uuid.uuid4(), interview_id=i.id, employee_id=employee_id, attendance="pending",
                                    invited_at=now(), invited_by=ctx.user.id if ctx.user is not None else None)
    i.participants.append(p)
    return p


def _send_invites(ctx: Ctx, i: models.Interview, employee_ids, title: str | None = None) -> None:
    a = i.application
    if i.scheduled_at is None:
        title = title or "Interview invitation"
        body = (f"Can you interview {a.candidate.name} for {a.opening.title} ({i.stage_name})? "
                f"Accept if you're available, or reject.")
    else:
        title = title or "Interview scheduled"
        body = (f"{a.candidate.name} ({a.opening.title}) -- {i.stage_name} on {_fmt_when(i.scheduled_at)}. "
                f"Accept, decline or request a reschedule.")
    notify(ctx, user_ids_for_employees(ctx.db, employee_ids), title, body, "interview_invitation", i.id,
           details=_interview_details(i))


def interview_out(ctx: Ctx, i: models.Interview) -> dict:
    a = i.application
    parts = _participant_ids(i)
    fb_by = {f.interviewer_id: f for f in i.feedback_entries}
    responses = _responses(i)
    mine = is_interview_participant(ctx, i)
    active = _is_active_participant(ctx, i)
    is_round = i.round_id is not None
    status = i.status or ("scheduled" if i.result is None else "feedback_completed")
    started = i.scheduled_at is not None and i.scheduled_at <= now()
    organizer = (is_round_organizer(ctx, a.opening) if is_round else is_interview_organizer(ctx, a)) if a is not None else False
    actions: list[str] = []
    options: list[str] = []
    unreplaced: list = []
    round_out = None
    disp_at, disp_end, disp_mode, disp_loc, disp_link = (
        i.scheduled_at, i.scheduled_end, i.mode, i.location, i.meeting_link)
    if is_round:
        r = i.round
        if r is not None:
            round_out = _round_out(ctx, r)
            disp_at = i.scheduled_at or r.scheduled_at
            disp_end = i.scheduled_end or r.scheduled_end
            disp_mode = i.mode or r.mode
            disp_loc = i.location or r.location
            disp_link = i.meeting_link or r.meeting_link
        if active and status == "scheduled":
            actions.append("start_interview")
        if active and status in ROUND_INSTANCE_OPEN:
            actions.append("end_interview")
        if organizer and status in ROUND_INSTANCE_OPEN:
            actions += ["reschedule_round", "cancel_round", "manage_interviewers"]
        if organizer and status == "ended":
            actions.append("complete_round")
    else:
        if ctx.can_edit and status in BOOKED_INTERVIEW:
            actions += ["reschedule", "cancel"] + (["complete", "no_show"] if started else [])
        unreplaced = [p for p in i.participants if p.attendance == "declined" and not p.replaced_by]
        if organizer and status in INVITATION_PHASE + BOOKED_INTERVIEW and not started:
            actions.append("invite")
            if unreplaced:
                actions.append("replace")
        if organizer and status == "ready_to_schedule":
            actions.append("schedule_time")
        if organizer and status in INVITATION_PHASE and "cancel" not in actions:
            actions.append("cancel")
        stage_locked = bool(i.stage_key and (a.stage_results or {}).get(i.stage_key, {}).get("result"))
        if active and not stage_locked and (status in ("completed", "feedback_pending", "feedback_completed")
                                            or (status == "scheduled" and started)):
            actions.append("feedback")
        options = response_options(ctx, i)
        if options:
            actions.append("respond")
    stage_scorecard = []
    if i.stage_key and a is not None:
        for s in opening_stages(ctx, a.opening):
            if s["key"] == i.stage_key:
                stage_scorecard = s.get("scorecard") or []
    my_emp = ctx.user.employee_id if ctx.user is not None else None
    part_rows = {p.employee_id: p for p in i.participants}
    everyone = [p.employee_id for p in i.participants] or parts
    my_part = part_rows.get(my_emp)
    return {
        "id": str(i.id), "application_id": str(i.application_id),
        "candidate_id": str(a.candidate_id) if a else None,
        "candidate_name": a.candidate.name if a and a.candidate else None,
        "opening_id": str(a.opening_id) if a else None,
        "opening_title": a.opening.title if a and a.opening else None,
        "stage_key": i.stage_key, "stage_name": i.stage_name or f"Round {i.round_no}",
        "interview_type": i.interview_type or "interview", "round_no": i.round_no,
        "round_id": str(i.round_id) if i.round_id else None,
        "round_name": round_out["name"] if round_out else None,
        "started_at": _iso(i.started_at), "ended_at": _iso(i.ended_at),
        "hr_decision": i.hr_decision, "hr_decision_notes": i.hr_decision_notes, "hr_decision_at": _iso(i.hr_decision_at),
        "status": status, "scheduled_at": _iso(disp_at),
        "scheduled_end": _iso(disp_end or (disp_at + datetime.timedelta(hours=1) if disp_at else None)),
        "mode": disp_mode, "location": disp_loc, "meeting_link": disp_link,
        "candidate_attendance": i.candidate_attendance, "notes": i.notes, "cancel_reason": i.cancel_reason,
        "reschedule_count": i.reschedule_count or 0, "feedback_required": i.feedback_required is not False,
        "scorecard": stage_scorecard,
        "participants": [_participant_out(ctx, e, part_rows.get(e), e in fb_by, responses) for e in everyone],
        "awaiting_time": i.scheduled_at is None, "is_organizer": organizer,
        "needs_replacement": [crud.employee_display_name(ctx.db, p.employee_id) for p in unreplaced],
        "feedback": [feedback_out(ctx, f) for f in i.feedback_entries
                     if ctx.can_view or f.interviewer_id == my_emp],
        "my_feedback": feedback_out(ctx, fb_by[my_emp]) if my_emp in fb_by else None,
        "my_response": responses.get(my_emp, "pending") if mine else None,
        "my_response_reason": my_part.response_reason if my_part is not None else None,
        "response_options": options,
        "is_participant": mine, "result": i.result, "legacy_feedback": i.feedback if not i.feedback_entries else None,
        "actions": actions,
    }


def _participant_out(ctx: Ctx, e, p: models.InterviewParticipant | None, feedback: bool, responses: dict) -> dict:
    return {
        **(_emp_ref(ctx.db, e) or {"id": str(e), "name": "—"}), "feedback_submitted": feedback,
        "response": responses.get(e, "pending"),
        "reason": p.response_reason if p is not None else None,
        "proposed_availability": p.proposed_availability if p is not None else None,
        "responded_at": _iso(p.responded_at) if p is not None else None,
        "invited_at": _iso(p.invited_at) if p is not None else None,
        "replaced_by": (_emp_ref(ctx.db, p.replaced_by) if p is not None and p.replaced_by else None),
    }


def feedback_out(ctx: Ctx, f: models.InterviewFeedback) -> dict:
    return {"id": str(f.id), "interviewer": _emp_ref(ctx.db, f.interviewer_id), "recommendation": f.recommendation,
            "rating": f.rating, "scores": f.scores or {}, "strengths": f.strengths, "concerns": f.concerns,
            "comments": f.comments, "submitted_at": _iso(f.submitted_at), "updated_at": _iso(f.updated_at)}


def list_interviews(ctx: Ctx, *, mine: bool = False, status: list[str] | None = None, application_id=None,
                    opening_id=None, interviewer_id=None, date_from=None, date_to=None,
                    limit: int = 50, offset: int = 0) -> dict:
    q = (select(models.Interview)
         .join(models.JobApplication, models.JobApplication.id == models.Interview.application_id)
         .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
         .where(models.JobOpening.company_id == ctx.company_id))
    if mine or not ctx.can_view:
        emp = ctx.user.employee_id or uuid.uuid4()
        q = q.where(or_(models.Interview.interviewer_id == emp, models.Interview.id.in_(
            select(models.InterviewParticipant.interview_id).where(models.InterviewParticipant.employee_id == emp))))
    if status:
        q = q.where(models.Interview.status.in_(status))
    if application_id:
        q = q.where(models.Interview.application_id == application_id)
    if opening_id:
        q = q.where(models.JobApplication.opening_id == opening_id)
    if interviewer_id:
        q = q.where(models.Interview.id.in_(select(models.InterviewParticipant.interview_id)
                                            .where(models.InterviewParticipant.employee_id == interviewer_id)))
    if date_from:
        q = q.where(models.Interview.scheduled_at >= datetime.datetime.combine(date_from, datetime.time.min, datetime.timezone.utc))
    if date_to:
        q = q.where(models.Interview.scheduled_at < datetime.datetime.combine(date_to + datetime.timedelta(days=1), datetime.time.min, datetime.timezone.utc))
    total = ctx.db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = ctx.db.scalars(q.order_by(models.Interview.scheduled_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [interview_out(ctx, i) for i in rows], "total": total}


def _parse_dt(v, label: str) -> datetime.datetime:
    if isinstance(v, datetime.datetime):
        dt = v
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise err(422, f"Invalid {label}.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=crud.company_tzinfo())
    return dt


def _interview_details(i: models.Interview) -> dict:
    """Schedule details for the candidate; a round-based interview reads
    whatever it doesn't override from its round."""
    r = i.round if i.round_id else None
    at = i.scheduled_at or (r.scheduled_at if r else None)
    mode = i.mode or (r.mode if r else None)
    link = i.meeting_link or (r.meeting_link if r else None)
    location = i.location or (r.location if r else None)
    return {"Candidate": i.application.candidate.name, "Position": i.application.opening.title,
            "Stage": i.stage_name or "", "When": _fmt_when(at),
            **({"Mode": mode.replace("_", " ").title()} if mode else {}),
            **({"Link": link} if link else {}), **({"Location": location} if location else {})}


def _candidate_reachable(a: models.JobApplication) -> bool:
    return bool(a.candidate and a.candidate.email)


def _candidate_extra(a: models.JobApplication, stage_name: str | None = None,
                     next_stage_name: str | None = None) -> dict:
    return {"opening_title": a.opening.title, "stage_name": stage_name or "", "next_stage_name": next_stage_name or ""}


def _email_interview_scheduled(ctx: Ctx, i: models.Interview, *, rescheduled: bool = False,
                               note: str | None = None) -> None:
    a = i.application
    if not _candidate_reachable(a):
        return
    from . import recruitment_onboarding as onb
    stage = i.stage_name or "interview"
    if rescheduled:
        onb.email_candidate(ctx, a.candidate, f"Interview rescheduled - {a.opening.title}",
                            "Your interview has been rescheduled",
                            note or f"Your {stage} for {a.opening.title} has a new time. Please note the updated "
                                    "details below.",
                            details=_interview_details(i), related=("recruitment_interview", i.id),
                            kind="candidate_interview_rescheduled", extra=_candidate_extra(a, i.stage_name))
    else:
        onb.email_candidate(ctx, a.candidate, f"Interview scheduled - {a.opening.title}", "Your interview is scheduled",
                            f"Your {stage} for {a.opening.title} is scheduled. Please find the details below -- "
                            "we look forward to speaking with you.",
                            details=_interview_details(i), related=("recruitment_interview", i.id),
                            kind="candidate_interview_scheduled", extra=_candidate_extra(a, i.stage_name))


def _email_interview_cancelled(ctx: Ctx, i: models.Interview) -> None:
    """Only for an interview the candidate was actually told about. The
    internal cancellation reason is never shared with the candidate."""
    a = i.application
    if not _candidate_reachable(a):
        return
    from . import recruitment_onboarding as onb
    stage = i.stage_name or "interview"
    onb.email_candidate(ctx, a.candidate, f"Interview cancelled - {a.opening.title}", "Your interview has been cancelled",
                        f"We're sorry -- your {stage} for {a.opening.title} has been cancelled. We apologise for the "
                        "inconvenience and will be in touch with you shortly about the next steps.",
                        details={k: v for k, v in _interview_details(i).items() if k in ("Position", "Stage", "When")},
                        related=("recruitment_interview", i.id),
                        kind="candidate_interview_cancelled", extra=_candidate_extra(a, i.stage_name))


def _email_candidate_rejected(ctx: Ctx, a: models.JobApplication) -> None:
    if not _candidate_reachable(a):
        return
    from . import recruitment_onboarding as onb
    onb.email_candidate(ctx, a.candidate, f"Your application for {a.opening.title}", "Thank you for your interest",
                        f"Thank you for the time you invested in applying for {a.opening.title}. "
                        "After careful consideration we will not be moving forward with your application "
                        "at this time. We will keep your profile on file for future opportunities.",
                        related=("recruitment_application", a.id),
                        kind="candidate_rejected", extra=_candidate_extra(a))


def _email_next_step(ctx: Ctx, a: models.JobApplication, cleared: str | None,
                     next_stage: dict | None, interview: models.Interview | None) -> None:
    """Tells the candidate they've moved forward. Skipped when entering the
    next stage already sent them its interview schedule (that email says
    what's next, with the details)."""
    if not _candidate_reachable(a):
        return
    if interview is not None and (interview.scheduled_at or (interview.round and interview.round.scheduled_at)):
        return
    from . import recruitment_onboarding as onb
    title = a.opening.title
    if next_stage is not None:
        nxt = next_stage["name"]
        heading = "Good news -- you're moving forward!"
        message = (f"Thank you for attending the {cleared} for {title}. We're pleased to let you know you have "
                   f"progressed to the next stage: {nxt}. We'll share the schedule with you shortly."
                   if cleared else
                   f"Thank you for your interest in {title}. We're pleased to let you know your application has "
                   f"been shortlisted. Your next step is the {nxt} -- we'll share the schedule with you shortly.")
    else:
        nxt = None
        heading = "You've completed all interview rounds"
        message = (f"Thank you for attending the {cleared or 'interviews'} for {title}. You've now completed all "
                   "interview rounds -- our team is reviewing the outcome and will contact you about the next "
                   "steps soon.")
    onb.email_candidate(ctx, a.candidate, f"Update on your application - {title}", heading, message,
                        related=("recruitment_application", a.id),
                        kind="candidate_next_step", extra=_candidate_extra(a, cleared, nxt))


def schedule_interview(ctx: Ctx, a: models.JobApplication, data: dict) -> models.Interview:
    ctx.require_edit()
    st = app_status(a)
    stages = opening_stages(ctx, a.opening)
    if st == "shortlisted" and stages:
        _enter_stage(ctx, a, 0, "Interview scheduled")
        st = app_status(a)
    if st not in ("assessment", "interview"):
        raise err(409, f"Interviews can be scheduled for shortlisted candidates (this one is {STATUS_LABELS.get(st, st)}).")
    stage = _stage_at(ctx, a, a.stage_index)
    if stage is None:
        raise err(409, "Every interview stage is complete -- make the final selection.")
    if data.get("stage_key") and data["stage_key"] != stage["key"]:
        raise err(409, f"The candidate is at {stage['name']} -- stages can't be skipped.")
    start = _parse_dt(data.get("scheduled_at"), "start time")
    end = _parse_dt(data["scheduled_end"], "end time") if data.get("scheduled_end") else start + datetime.timedelta(hours=1)
    if end <= start:
        raise err(422, "The end time must be after the start time.")
    mode = data.get("mode") or "video"
    if mode not in INTERVIEW_MODES:
        raise err(422, "Mode must be in person, video or phone.")
    panel = _panel(ctx, data.get("participant_ids"))
    if any(x.status in INVITATION_PHASE for x in _stage_interviews(ctx, a, stage["key"])):
        raise err(409, f"Interviewers have already been invited for the {stage['name']} -- schedule that "
                       f"interview once they accept instead of creating another.")
    _check_conflicts(ctx, a, panel, start, end)
    i = models.Interview(
        id=uuid.uuid4(), application_id=a.id, round_no=(a.stage_index or 0) + 1, scheduled_at=start,
        scheduled_end=end, interviewer_id=panel[0], stage_key=stage["key"], stage_name=stage["name"],
        interview_type=stage["type"], status="scheduled", mode=mode,
        location=(data.get("location") or None), meeting_link=(data.get("meeting_link") or None),
        notes=data.get("notes"), feedback_required=bool(stage.get("feedback_required", True)),
        reschedule_count=0, created_by=ctx.user.id, created_at=now(),
    )
    ctx.db.add(i)
    ctx.db.flush()
    for e in panel:
        ctx.db.add(models.InterviewParticipant(id=uuid.uuid4(), interview_id=i.id, employee_id=e, attendance="pending",
                                               invited_at=now(), invited_by=ctx.user.id))
    ctx.db.flush()
    ctx.db.refresh(i)
    record(ctx, "interview", i.id, f"Interview scheduled -- {stage['name']}", new="scheduled", application=a,
           meta={"at": start.isoformat(), "mode": mode, "panel": [str(p) for p in panel]})
    _send_invites(ctx, i, panel, title="Interview invitation")
    if data.get("notify_candidate", True):
        _email_interview_scheduled(ctx, i)
    return i


def reschedule_interview(ctx: Ctx, i: models.Interview, data: dict) -> None:
    ctx.require_edit()
    if i.status not in ("scheduled", "reschedule_requested"):
        raise err(409, f"A {i.status.replace('_', ' ')} interview can't be rescheduled.")
    start = _parse_dt(data.get("scheduled_at"), "start time")
    end = _parse_dt(data["scheduled_end"], "end time") if data.get("scheduled_end") else start + (
        (i.scheduled_end - i.scheduled_at) if i.scheduled_end else datetime.timedelta(hours=1))
    if end <= start:
        raise err(422, "The end time must be after the start time.")
    old_status = i.status
    old_at = i.scheduled_at
    i.scheduled_at, i.scheduled_end = start, end
    for f in ("mode", "location", "meeting_link"):
        if data.get(f) is not None:
            if f == "mode" and data[f] not in INTERVIEW_MODES:
                raise err(422, "Mode must be in person, video or phone.")
            setattr(i, f, data[f] or None)
    if data.get("participant_ids"):
        wanted = _panel(ctx, data["participant_ids"])
        have = {p.employee_id: p for p in i.participants}
        for e in wanted:
            if e in have and have[e].attendance == "declined":
                raise err(409, f"{crud.employee_display_name(ctx.db, e)} declined this interview -- choose someone else.")
        for e, p in have.items():
            if (e not in wanted and p.attendance != "declined"
                    and not any(f.interviewer_id == e for f in i.feedback_entries)):
                i.participants.remove(p)
        for e in wanted:
            if e not in have:
                _invite_participant(ctx, i, e)
        i.interviewer_id = wanted[0]
        ctx.db.flush()
    _check_conflicts(ctx, i.application, _participant_ids(i), start, end, exclude_id=i.id)
    for p in i.participants:
        if p.attendance != "declined":  # a new time needs a new answer
            p.attendance, p.response_reason, p.proposed_availability, p.responded_at = "pending", None, None, None
    i.status = "scheduled"
    i.reschedule_count = (i.reschedule_count or 0) + 1
    reason = (data.get("reason") or "").strip() or None
    record(ctx, "interview", i.id, "Interview rescheduled", old=old_status, new="scheduled", comments=reason,
           application=i.application, meta={"from": old_at.isoformat() if old_at else None, "to": start.isoformat()})
    _close_invitation_notices(ctx, i)
    _send_invites(ctx, i, _participant_ids(i), title="Interview rescheduled")
    if data.get("notify_candidate", True):
        _email_interview_scheduled(ctx, i, rescheduled=True)


def cancel_interview(ctx: Ctx, i: models.Interview, reason: str | None, notify_candidate: bool = True) -> None:
    if i.status in INVITATION_PHASE:
        require_interview_organizer(ctx, i.application)
    else:
        ctx.require_edit()
    if i.status not in OPEN_INTERVIEW:
        raise err(409, f"A {i.status.replace('_', ' ')} interview can't be cancelled.")
    if not (reason or "").strip():
        raise err(422, "A cancellation reason is required.")
    old = i.status
    i.status = "cancelled"
    i.cancel_reason = reason
    record(ctx, "interview", i.id, "Interview cancelled", old=old, new="cancelled", comments=reason, application=i.application)
    _close_invitation_notices(ctx, i)
    notify(ctx, user_ids_for_employees(ctx.db, _participant_ids(i)), "Interview cancelled",
           f"{i.application.candidate.name} -- {i.stage_name}: {reason}", "recruitment_interview", i.id)
    # Only an interview with a time was ever announced to the candidate (an
    # invitation-phase one, or a round with no date yet, wasn't).
    announced = i.scheduled_at or (i.round_id and i.round and i.round.scheduled_at)
    if notify_candidate and old not in INVITATION_PHASE and old != "draft" and announced:
        _email_interview_cancelled(ctx, i)


def respond_to_invitation(ctx: Ctx, i: models.Interview, response: str, reason: str | None,
                          proposed_availability: str | None = None) -> None:
    """An interviewer answers: before a time is set, accept / reject on
    availability; once scheduled, accept / decline / request a reschedule
    (reason + when they're free). The organizers are told either way."""
    if not is_interview_participant(ctx, i):
        raise err(403, "Only the interviewers invited to this interview can respond.")
    new = INVITE_RESPONSES.get((response or "").strip().lower())
    if new is None:
        raise err(422, "Response must be accept, reject or reschedule.")
    key = {"accepted": "accept", "declined": "decline", "reschedule": "reschedule"}[new]
    options = response_options(ctx, i)
    if key not in options:
        if not options:
            mine = _participant(i, ctx.user.employee_id)
            if mine is not None and mine.attendance == "declined":
                raise err(409, "You've already declined this interview.")
            raise err(409, f"This interview is {i.status.replace('_', ' ')} -- it can no longer be answered.")
        if key == "reschedule":
            raise err(409, "A reschedule can be requested once the interview has a time.")
        raise err(409, "You've already given this answer.")
    reason = (reason or "").strip() or None
    proposed = (proposed_availability or "").strip() or None
    if new == "declined" and not reason:
        raise err(422, "Please give a reason (e.g. not available at this time).")
    if new == "reschedule" and not (reason and proposed):
        raise err(422, "Give a reason and when you're available so the organizer can pick a new time.")
    me = ctx.user.employee_id
    part = _participant(i, me)
    if part is None:  # legacy single-interviewer row
        part = models.InterviewParticipant(id=uuid.uuid4(), interview_id=i.id, employee_id=me)
        i.participants.append(part)
    old = part.attendance or "pending"
    part.attendance, part.response_reason, part.responded_at = new, reason, now()
    part.proposed_availability = proposed if new == "reschedule" else None
    ctx.db.flush()
    _sync_invitation_status(i)
    a = i.application
    phase = "availability" if i.scheduled_at is None else "schedule"
    label, title, next_step = {
        ("accepted", "availability"): ("Interview invitation accepted", "Interviewer accepted",
                                       "Schedule the interview."),
        ("declined", "availability"): ("Interview invitation rejected", "Interviewer rejected the invitation",
                                       "Invite another interviewer."),
        ("accepted", "schedule"): ("Interview time accepted", "Interviewer confirmed", ""),
        ("declined", "schedule"): ("Interview declined", "Interviewer declined",
                                   "Invite another interviewer or reschedule."),
        ("reschedule", "schedule"): ("Reschedule requested", "Reschedule requested", "Pick a new time."),
    }[(new, phase)]
    record(ctx, "interview", i.id, label, old=old, new=new, comments=reason, application=a,
           meta={"interviewer": str(me), **({"proposed_availability": proposed} if proposed else {})})
    _close_invitation_notices(ctx, i, [me])
    body = (f"{ctx.actor_name} -- {i.stage_name or 'interview'} for {a.candidate.name} ({a.opening.title}), "
            f"{_fmt_when(i.scheduled_at)}."
            + (f" Reason: {reason}." if reason else "")
            + (f" Available: {proposed}." if proposed else "")
            + (f" {next_step}" if next_step else ""))
    notify(ctx, _organizer_user_ids(ctx, i), title, body, "recruitment_interview", i.id,
           details={**_interview_details(i), "Interviewer": ctx.actor_name,
                    **({"Reason": reason} if reason else {}), **({"Available": proposed} if proposed else {})})


def invite_interviewers(ctx: Ctx, a: models.JobApplication, data: dict) -> models.Interview:
    """Step 1: ask interviewers for the candidate's current stage whether
    they can take it -- no time yet. One open interview per stage."""
    require_interview_organizer(ctx, a)
    st = app_status(a)
    stages = opening_stages(ctx, a.opening)
    if st == "shortlisted" and stages:
        _enter_stage(ctx, a, 0, "Interview invitations sent")
        st = app_status(a)
    if st not in ("assessment", "interview"):
        raise err(409, f"Interviewers can be invited for shortlisted candidates (this one is {STATUS_LABELS.get(st, st)}).")
    stage = _stage_at(ctx, a, a.stage_index)
    if stage is None:
        raise err(409, "Every interview stage is complete -- make the final selection.")
    if data.get("stage_key") and data["stage_key"] != stage["key"]:
        raise err(409, f"The candidate is at {stage['name']} -- stages can't be skipped.")
    if any(x.status in OPEN_INTERVIEW for x in _stage_interviews(ctx, a, stage["key"])):
        raise err(409, f"{a.candidate.name} already has an open {stage['name']} -- invite interviewers to it "
                       f"(or replace one who declined) instead of creating another.")
    mode = data.get("mode") or "video"
    if mode not in INTERVIEW_MODES:
        raise err(422, "Mode must be in person, video or phone.")
    panel = _panel(ctx, data.get("participant_ids"))
    i = models.Interview(
        id=uuid.uuid4(), application_id=a.id, round_no=(a.stage_index or 0) + 1, scheduled_at=None,
        scheduled_end=None, interviewer_id=panel[0], stage_key=stage["key"], stage_name=stage["name"],
        interview_type=stage["type"], status="invited", mode=mode,
        location=(data.get("location") or None), meeting_link=(data.get("meeting_link") or None),
        notes=data.get("notes"), feedback_required=bool(stage.get("feedback_required", True)),
        reschedule_count=0, created_by=ctx.user.id, created_at=now(),
    )
    ctx.db.add(i)
    ctx.db.flush()
    for e in panel:
        _invite_participant(ctx, i, e)
    ctx.db.flush()
    ctx.db.refresh(i)
    record(ctx, "interview", i.id, f"Interviewers invited -- {stage['name']}", new="invited", application=a,
           meta={"panel": [str(p) for p in panel]})
    _send_invites(ctx, i, panel)
    return i


def add_interviewers(ctx: Ctx, i: models.Interview, data: dict) -> None:
    """Invite more interviewers -- or replace one who declined (their row
    and answer stay in the history; the candidate is untouched)."""
    a = i.application
    require_interview_organizer(ctx, a)
    if i.status not in INVITATION_PHASE + BOOKED_INTERVIEW:
        raise err(409, f"A {i.status.replace('_', ' ')} interview can't take new interviewers.")
    replaced = None
    if data.get("replaces"):
        try:
            replaced = _participant(i, uuid.UUID(str(data["replaces"])))
        except ValueError:
            replaced = None
        if replaced is None or replaced.attendance != "declined":
            raise err(409, "Only an interviewer who declined can be replaced.")
        if replaced.replaced_by:
            raise err(409, f"{crud.employee_display_name(ctx.db, replaced.employee_id)} has already been replaced.")
    new = []
    for e in _panel(ctx, data.get("participant_ids"), required=False):
        p = _participant(i, e)
        name = crud.employee_display_name(ctx.db, e)
        if p is not None and p.attendance == "declined":
            raise err(409, f"{name} already declined this interview -- choose someone else.")
        if p is not None or (not i.participants and e == i.interviewer_id):
            raise err(409, f"{name} is already invited to this interview.")
        new.append(e)
    if not new:
        raise err(422, "Choose at least one interviewer.")
    if replaced is not None and len(new) != 1:
        raise err(422, "Choose one interviewer to take the declined place.")
    if i.scheduled_at is not None:
        _check_conflicts(ctx, a, new, i.scheduled_at, _interview_end(i), exclude_id=i.id)
    for e in new:
        _invite_participant(ctx, i, e)
    if replaced is not None:
        replaced.replaced_by = new[0]
        if i.interviewer_id == replaced.employee_id:
            i.interviewer_id = new[0]
    ctx.db.flush()
    _sync_invitation_status(i)
    names = ", ".join(crud.employee_display_name(ctx.db, e) for e in new)
    if replaced is not None:
        label = f"Interviewer replaced -- {crud.employee_display_name(ctx.db, replaced.employee_id)} -> {names}"
    else:
        label = f"Interviewers invited -- {names}"
    record(ctx, "interview", i.id, label[:60], old=i.status, new=i.status, application=a,
           comments=label if len(label) > 60 else None,
           meta={"invited": [str(e) for e in new], **({"replaces": str(replaced.employee_id)} if replaced else {})})
    _send_invites(ctx, i, new)


def schedule_invited_interview(ctx: Ctx, i: models.Interview, data: dict) -> None:
    """Step 2: once an interviewer accepted, the organizer picks the time;
    the panel is asked to confirm it (accept / decline / reschedule)."""
    a = i.application
    require_interview_organizer(ctx, a)
    if i.status not in INVITATION_PHASE:
        raise err(409, f"This interview is {i.status.replace('_', ' ')} -- use Reschedule to change its time.")
    if not any(p.attendance == "accepted" for p in i.participants):
        raise err(409, "No interviewer has accepted yet -- wait for an acceptance or invite someone else.")
    start = _parse_dt(data.get("scheduled_at"), "start time")
    end = _parse_dt(data["scheduled_end"], "end time") if data.get("scheduled_end") else start + datetime.timedelta(hours=1)
    if end <= start:
        raise err(422, "The end time must be after the start time.")
    for f in ("mode", "location", "meeting_link", "notes"):
        if data.get(f) is not None:
            if f == "mode" and data[f] not in INTERVIEW_MODES:
                raise err(422, "Mode must be in person, video or phone.")
            setattr(i, f, data[f] or None)
    panel = _participant_ids(i)
    _check_conflicts(ctx, a, panel, start, end, exclude_id=i.id)
    old = i.status
    i.scheduled_at, i.scheduled_end = start, end
    for p in i.participants:
        if p.attendance != "declined":  # confirm the actual time
            p.attendance, p.response_reason, p.proposed_availability, p.responded_at = "pending", None, None, None
    i.status = "scheduled"
    record(ctx, "interview", i.id, f"Interview scheduled -- {i.stage_name}", old=old, new="scheduled", application=a,
           meta={"at": start.isoformat(), "mode": i.mode, "panel": [str(e) for e in panel]})
    _close_invitation_notices(ctx, i)
    _send_invites(ctx, i, panel)
    if data.get("notify_candidate", True):
        _email_interview_scheduled(ctx, i)


def pending_invitations(ctx: Ctx) -> list[dict]:
    """The caller's interview invitations awaiting their answer, shaped like
    an approvals-inbox row."""
    me = ctx.user.employee_id if ctx.user is not None else None
    if me is None:
        return []
    q = (select(models.Interview)
         .join(models.InterviewParticipant, models.InterviewParticipant.interview_id == models.Interview.id)
         .join(models.JobApplication, models.JobApplication.id == models.Interview.application_id)
         .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
         .where(models.JobOpening.company_id == ctx.company_id,
                models.InterviewParticipant.employee_id == me,
                or_(models.InterviewParticipant.attendance.is_(None),
                    models.InterviewParticipant.attendance == "pending"),
                models.Interview.status.in_(INVITATION_PHASE + BOOKED_INTERVIEW))
         .order_by(models.Interview.created_at))
    out = []
    for i in ctx.db.scalars(q).unique().all():
        options = response_options(ctx, i)
        if not options:
            continue
        a = i.application
        sender_id = (_participant(i, me).invited_by if _participant(i, me) is not None else None) or i.created_by
        sender = ctx.db.get(models.User, sender_id) if sender_id else None
        invited_by = None
        if sender is not None:
            invited_by = (crud.employee_display_name(ctx.db, sender.employee_id) if sender.employee_id
                          else None) or sender.full_name or sender.email
        out.append({
            "id": str(i.id), "status": "pending", "can_decide": True,
            "phase": "availability" if i.scheduled_at is None else "schedule", "options": options,
            "application_id": str(a.id), "candidate_name": a.candidate.name, "opening_title": a.opening.title,
            "stage_name": i.stage_name or f"Round {i.round_no}", "scheduled_at": _iso(i.scheduled_at),
            "scheduled_end": _iso(_interview_end(i)),
            "mode": i.mode, "location": i.location, "meeting_link": i.meeting_link, "notes": i.notes,
            "employee_id": str(me), "invited_by_name": invited_by, "created_at": _iso(i.created_at),
        })
    return out


def complete_interview(ctx: Ctx, i: models.Interview, data: dict) -> None:
    ctx.require_edit()
    if i.status not in ("scheduled", "reschedule_requested"):
        raise err(409, f"This interview is already {i.status.replace('_', ' ')}.")
    if i.scheduled_at and i.scheduled_at > now():
        raise err(409, "This interview hasn't started yet.")
    old = i.status
    i.completed_at = now()
    i.candidate_attendance = "present"
    if data.get("notes"):
        i.notes = data["notes"]
    _refresh_feedback_status(i)
    record(ctx, "interview", i.id, "Interview completed", old=old, new=i.status, application=i.application)
    pending = [e for e in _participant_ids(i) if e not in {f.interviewer_id for f in i.feedback_entries}]
    if i.status == "feedback_pending":
        notify(ctx, user_ids_for_employees(ctx.db, pending), "Interview feedback pending",
               f"Please submit your feedback for {i.application.candidate.name} ({i.stage_name}).",
               "recruitment_interview", i.id)


def mark_no_show(ctx: Ctx, i: models.Interview, notes: str | None) -> None:
    ctx.require_edit()
    if i.status not in ("scheduled", "reschedule_requested"):
        raise err(409, f"This interview is already {i.status.replace('_', ' ')}.")
    if i.scheduled_at and i.scheduled_at > now():
        raise err(409, "This interview hasn't started yet.")
    old = i.status
    i.status = "no_show"
    i.candidate_attendance = "absent"
    i.notes = notes or i.notes
    record(ctx, "interview", i.id, "Candidate did not attend (no-show)", old=old, new="no_show", comments=notes,
           application=i.application)


def _refresh_feedback_status(i: models.Interview) -> None:
    parts = set(_participant_ids(i))
    given = {f.interviewer_id for f in i.feedback_entries}
    if i.completed_at is None:
        return
    if not i.feedback_required:
        i.status = "feedback_completed" if parts and parts <= given else "completed"
    else:
        i.status = "feedback_completed" if parts and parts <= given else "feedback_pending"
    if i.status == "feedback_completed" and i.feedback_entries:
        recs = {f.recommendation for f in i.feedback_entries}
        i.result = "fail" if "fail" in recs else ("hold" if "hold" in recs else "pass")
        i.feedback = "\n\n".join(
            f"{crud._full_name(f.interviewer)}: {f.recommendation.upper()}"
            + (f" ({f.rating}/5)" if f.rating else "") + (f" -- {f.comments}" if f.comments else "")
            for f in i.feedback_entries)[:4000]


def submit_feedback(ctx: Ctx, i: models.Interview, data: dict) -> models.InterviewFeedback:
    """Only a panelist of this interview can give feedback (their own)."""
    if not _is_active_participant(ctx, i):
        raise err(403, "Only the interviewers on this panel can submit feedback.")
    if i.status in INVITATION_PHASE:
        raise err(409, "Feedback can be submitted once the interview has been scheduled and held.")
    if i.status in ("cancelled", "no_show", "draft"):
        raise err(409, f"Feedback can't be given for a {i.status.replace('_', ' ')} interview.")
    if i.status in ("scheduled", "reschedule_requested") and i.scheduled_at and i.scheduled_at > now():
        raise err(409, "Feedback can be submitted once the interview has started.")
    a = i.application
    if i.stage_key and (a.stage_results or {}).get(i.stage_key, {}).get("result") in ("pass", "fail", "skipped"):
        raise err(409, "This stage has already been decided -- feedback is locked.")
    rec = data.get("recommendation")
    if rec not in ("pass", "fail", "hold"):
        raise err(422, "Choose a recommendation: pass, fail or hold.")
    rating = data.get("rating")
    if rating is not None and not 1 <= int(rating) <= 5:
        raise err(422, "Rating must be between 1 and 5.")
    comments = (data.get("comments") or "").strip()
    if not comments:
        raise err(422, "Feedback comments are required.")
    scores = {}
    for k, v in (data.get("scores") or {}).items():
        if v is None:
            continue
        v = int(v)
        if not 1 <= v <= 5:
            raise err(422, "Scorecard ratings must be between 1 and 5.")
        scores[str(k)[:60]] = v
    fb = next((f for f in i.feedback_entries if f.interviewer_id == ctx.user.employee_id), None)
    first = fb is None
    if fb is None:
        fb = models.InterviewFeedback(id=uuid.uuid4(), interview_id=i.id, interviewer_id=ctx.user.employee_id,
                                      submitted_at=now(), submitted_by=ctx.user.id)
        i.feedback_entries.append(fb)
    else:
        fb.updated_at = now()
    fb.recommendation, fb.rating, fb.scores = rec, (int(rating) if rating is not None else None), scores
    fb.strengths = (data.get("strengths") or "").strip() or None
    fb.concerns = (data.get("concerns") or "").strip() or None
    fb.comments = comments
    old = i.status
    if i.completed_at is None:
        # Feedback after the start time marks the interview as held.
        i.completed_at = now()
        i.candidate_attendance = i.candidate_attendance or "present"
    ctx.db.flush()
    _refresh_feedback_status(i)
    record(ctx, "interview", i.id, "Feedback submitted" if first else "Feedback updated", old=old, new=i.status,
           application=a, comments=f"{rec.upper()}" + (f" ({fb.rating}/5)" if fb.rating else ""))
    if i.status == "feedback_completed" and old != "feedback_completed":
        notify(ctx, recruiter_user_ids(ctx, a), "Interview feedback complete",
               f"All feedback is in for {a.candidate.name} -- {i.stage_name}.", "recruitment_application", a.id, email=False)
    return fb


# ═══════════════════════════════════════════════════════════════════════════
# Interview rounds -- shared sessions (backend/db/add_interview_rounds.sql)
# ═══════════════════════════════════════════════════════════════════════════
#
# A round is defined once per job opening (Round 1, Technical, HR, Final...),
# with its own date/time and a checkbox-picked interviewer pool, and applies
# to every candidate who reaches that stage (a walk-in / panel day) -- no
# invite/accept-decline step. When a candidate enters the matching stage
# (_attach_round_if_configured, called from _enter_stage) they get their own
# hcm_interviews row (round_id set) that inherits the round's schedule/panel
# and can be individually overridden. Interviewers just Start/End; ending
# tells the Owner/HR to record the internal decision (complete_round_decision),
# which drives the same pipeline functions (_next_stage / transition) used
# by the legacy flow.

def is_round_organizer(ctx: Ctx, o: models.JobOpening) -> bool:
    """Same rule as interview invitations: a Recruitment editor or the job
    opening's creator runs its interview rounds."""
    if ctx.can_edit:
        return True
    return ctx.user is not None and o is not None and opening_creator_user_id(ctx, o) == ctx.user.id


def require_round_organizer(ctx: Ctx, o: models.JobOpening) -> None:
    if not is_round_organizer(ctx, o):
        raise err(403, "Only the job opening's creator or a Recruitment editor can manage interview rounds.")


def get_round(ctx: Ctx, round_id, *, lock: bool = False) -> models.InterviewRound:
    q = select(models.InterviewRound).where(models.InterviewRound.id == uuid.UUID(str(round_id)))
    if lock:
        q = q.with_for_update()
    r = ctx.db.scalar(q)
    if r is None or r.company_id != ctx.company_id:
        raise err(404, "Interview round not found.")
    return r


def _round_end(r: models.InterviewRound) -> datetime.datetime | None:
    return r.scheduled_end or (r.scheduled_at + datetime.timedelta(hours=1) if r.scheduled_at else None)


def _active_round_pool(r: models.InterviewRound) -> list[uuid.UUID]:
    return [ri.employee_id for ri in r.interviewers if ri.removed_at is None]


def _check_round_conflicts(ctx: Ctx, employee_ids, start: datetime.datetime | None,
                           end: datetime.datetime | None, *, exclude_round_id=None) -> None:
    """No interviewer double-booked across round sessions, and not already
    booked into a legacy per-candidate interview, at the same time."""
    wanted = {e for e in (employee_ids or []) if e}
    if not wanted or start is None or end is None:
        return
    R, RI = models.InterviewRound, models.InterviewRoundInterviewer
    rq = (select(models.InterviewRound).join(RI, RI.round_id == R.id)
          .where(R.company_id == ctx.company_id, R.status == "scheduled", RI.removed_at.is_(None),
                 RI.employee_id.in_(wanted), R.scheduled_at.is_not(None), R.scheduled_at < end,
                 R.scheduled_at > start - datetime.timedelta(days=1)))
    if exclude_round_id is not None:
        rq = rq.where(R.id != exclude_round_id)
    for other in ctx.db.scalars(rq).unique().all():
        other_end = _round_end(other)
        if other_end is None or other_end <= start:
            continue
        busy = wanted & set(_active_round_pool(other))
        if busy:
            names = ", ".join(sorted(crud.employee_display_name(ctx.db, e) for e in busy))
            raise err(409, f"{names} already has the {other.name} round at {_fmt_when(other.scheduled_at)} "
                           f"-- pick another time or interviewer.")
    I = models.Interview
    iq = (select(I).join(models.JobApplication, models.JobApplication.id == I.application_id)
          .join(models.JobOpening, models.JobOpening.id == models.JobApplication.opening_id)
          .where(models.JobOpening.company_id == ctx.company_id, I.status.in_(BOOKED_INTERVIEW),
                 I.scheduled_at.is_not(None), I.scheduled_at < end,
                 I.scheduled_at > start - datetime.timedelta(days=1)))
    for other in ctx.db.scalars(iq).all():
        if _interview_end(other) <= start:
            continue
        busy = wanted & set(_participant_ids(other))
        if busy:
            names = ", ".join(sorted(crud.employee_display_name(ctx.db, e) for e in busy))
            raise err(409, f"{names} already has an interview at {_fmt_when(other.scheduled_at)} "
                           f"({other.application.candidate.name}, {other.stage_name or 'interview'}) "
                           f"-- pick another time or interviewer.")


def _round_out(ctx: Ctx, r: models.InterviewRound) -> dict:
    pool = _active_round_pool(r)
    candidate_count = ctx.db.scalar(select(func.count()).select_from(models.Interview).where(
        models.Interview.round_id == r.id, models.Interview.status != "cancelled")) or 0
    return {
        "id": str(r.id), "opening_id": str(r.opening_id), "round_key": r.round_key, "name": r.name,
        "sort_order": r.sort_order, "interview_type": r.interview_type,
        "scheduled_at": _iso(r.scheduled_at), "scheduled_end": _iso(r.scheduled_end or _round_end(r)),
        "mode": r.mode, "location": r.location, "meeting_link": r.meeting_link, "notes": r.notes,
        "status": r.status, "cancel_reason": r.cancel_reason, "reschedule_count": r.reschedule_count or 0,
        "interviewers": [_emp_ref(ctx.db, e) or {"id": str(e), "name": "—"} for e in pool],
        "candidate_count": candidate_count,
        "can_manage": is_round_organizer(ctx, r.opening),
    }


def list_rounds(ctx: Ctx, o: models.JobOpening) -> list[dict]:
    ctx.require_view()
    rows = ctx.db.scalars(select(models.InterviewRound).where(models.InterviewRound.opening_id == o.id)
                          .order_by(models.InterviewRound.sort_order)).all()
    return [_round_out(ctx, r) for r in rows]


def _round_mode(data: dict) -> str | None:
    mode = data.get("mode")
    if mode is not None and mode not in INTERVIEW_MODES:
        raise err(422, "Mode must be in person, video or phone.")
    return mode or None


def create_round(ctx: Ctx, o: models.JobOpening, data: dict) -> models.InterviewRound:
    """Define a round for a job opening's stage (Recruitment > Job Opening
    > Interview Rounds). round_key must match an existing interview_stages
    stage key -- a round is 1:1 with a pipeline stage."""
    require_round_organizer(ctx, o)
    key = str(data.get("round_key") or "").strip()
    stage = next((s for s in opening_stages(ctx, o) if s["key"] == key), None)
    if stage is None:
        raise err(422, "round_key must match one of this opening's interview/assessment stages.")
    if ctx.db.scalar(select(models.InterviewRound).where(
            models.InterviewRound.opening_id == o.id, models.InterviewRound.round_key == key)) is not None:
        raise err(409, f"{stage['name']} already has a round defined -- edit it instead.")
    start = _parse_dt(data["scheduled_at"], "start time") if data.get("scheduled_at") else None
    end = (_parse_dt(data["scheduled_end"], "end time") if data.get("scheduled_end")
           else (start + datetime.timedelta(hours=1) if start else None))
    if start and end and end <= start:
        raise err(422, "The end time must be after the start time.")
    panel = _panel(ctx, data.get("employee_ids"), required=False)
    if start:
        _check_round_conflicts(ctx, panel, start, end)
    r = models.InterviewRound(
        id=uuid.uuid4(), company_id=ctx.company_id, opening_id=o.id, round_key=key,
        name=(data.get("name") or stage["name"])[:120], sort_order=int(data.get("sort_order") or 1),
        interview_type=stage["type"], scheduled_at=start, scheduled_end=end, mode=_round_mode(data),
        location=(data.get("location") or None), meeting_link=(data.get("meeting_link") or None),
        notes=(data.get("notes") or None), status="scheduled",
        created_by=ctx.user.id if ctx.user is not None else None, created_at=now(),
    )
    ctx.db.add(r)
    ctx.db.flush()
    for e in panel:
        ctx.db.add(models.InterviewRoundInterviewer(id=uuid.uuid4(), round_id=r.id, employee_id=e,
                                                    assigned_at=now(), assigned_by=r.created_by))
    ctx.db.flush()
    ctx.db.refresh(r)
    record(ctx, "interview_round", r.id, f"Round created -- {r.name}", new="scheduled",
           meta={"opening_id": str(o.id), "panel": [str(p) for p in panel]})
    if panel and start:
        _notify_round_pool(ctx, r, panel, "Interview round scheduled")
    return r


def _notify_round_pool(ctx: Ctx, r: models.InterviewRound, employee_ids, title: str) -> None:
    when = _fmt_when(r.scheduled_at)
    body = (f"{r.name} for {r.opening.title} -- {when}"
            + (f" ({(r.mode or '').replace('_', ' ')})" if r.mode else "") + ".")
    notify(ctx, user_ids_for_employees(ctx.db, employee_ids), title, body, "interview_round", r.id,
           details={"Round": r.name, "Position": r.opening.title, "When": when,
                    **({"Mode": (r.mode or "").replace("_", " ").title()} if r.mode else {}),
                    **({"Link": r.meeting_link} if r.meeting_link else {}),
                    **({"Location": r.location} if r.location else {})})


def update_round(ctx: Ctx, r: models.InterviewRound, data: dict) -> None:
    """Edit a round: date/time, mode/location/notes. Candidate instances
    that weren't individually overridden read the round's schedule live
    (see interview_out's disp_at/disp_end/... fallback), so nothing needs
    to be copied to them. Any change re-notifies the active pool and logs a
    reschedule if the time moved."""
    require_round_organizer(ctx, r.opening)
    if r.status != "scheduled":
        raise err(409, "A cancelled round can't be edited.")
    old_at = r.scheduled_at
    old_visible = (r.scheduled_at, r.mode, r.location, r.meeting_link)
    start =_parse_dt(data["scheduled_at"], "start time") if "scheduled_at" in data and data["scheduled_at"] else r.scheduled_at
    end = (_parse_dt(data["scheduled_end"], "end time") if data.get("scheduled_end")
           else (start + datetime.timedelta(hours=1) if start else None))
    if start and end and end <= start:
        raise err(422, "The end time must be after the start time.")
    if start:
        _check_round_conflicts(ctx, _active_round_pool(r), start, end, exclude_round_id=r.id)
    r.scheduled_at, r.scheduled_end = start, end
    if "mode" in data:
        r.mode = _round_mode(data)
    for f in ("location", "meeting_link", "notes"):
        if f in data:
            setattr(r, f, data.get(f) or None)
    if "name" in data and data["name"]:
        r.name = str(data["name"])[:120]
    rescheduled = bool(old_at) and start != old_at
    if rescheduled:
        r.reschedule_count = (r.reschedule_count or 0) + 1
    r.updated_by = ctx.user.id if ctx.user is not None else None
    r.updated_at = now()
    record(ctx, "interview_round", r.id, "Round rescheduled" if rescheduled else "Round edited",
           old="scheduled", new="scheduled",
           meta={"from": old_at.isoformat() if old_at else None, "to": start.isoformat() if start else None})
    pool = _active_round_pool(r)
    if pool and r.scheduled_at:
        _notify_round_pool(ctx, r, pool, "Interview round rescheduled" if rescheduled else "Interview round updated")
    # Candidates waiting in this round hear about anything they'd act on:
    # a first date (scheduled), or a new time / mode / link / location.
    if r.scheduled_at and (r.scheduled_at, r.mode, r.location, r.meeting_link) != old_visible:
        for i in ctx.db.scalars(select(models.Interview).where(
                models.Interview.round_id == r.id, models.Interview.status == "scheduled",
                models.Interview.scheduled_at.is_(None))).all():
            _email_interview_scheduled(ctx, i, rescheduled=old_at is not None)


def add_round_interviewers(ctx: Ctx, r: models.InterviewRound, employee_ids) -> None:
    require_round_organizer(ctx, r.opening)
    if r.status != "scheduled":
        raise err(409, "A cancelled round can't take new interviewers.")
    existing = {ri.employee_id for ri in r.interviewers if ri.removed_at is None}
    new = [e for e in _panel(ctx, employee_ids, required=False) if e not in existing]
    if not new:
        raise err(422, "Choose at least one interviewer who isn't already on this round.")
    if r.scheduled_at:
        _check_round_conflicts(ctx, new, r.scheduled_at, _round_end(r), exclude_round_id=r.id)
    for e in new:
        ctx.db.add(models.InterviewRoundInterviewer(id=uuid.uuid4(), round_id=r.id, employee_id=e, assigned_at=now(),
                                                    assigned_by=ctx.user.id if ctx.user is not None else None))
    ctx.db.flush()
    names = ", ".join(crud.employee_display_name(ctx.db, e) for e in new)
    record(ctx, "interview_round", r.id, f"Interviewer(s) added -- {names}"[:60], old="scheduled", new="scheduled",
           meta={"added": [str(e) for e in new]})
    if r.scheduled_at:
        _notify_round_pool(ctx, r, new, "Interview round assignment")


def remove_round_interviewer(ctx: Ctx, r: models.InterviewRound, employee_id: uuid.UUID) -> None:
    require_round_organizer(ctx, r.opening)
    ri = next((x for x in r.interviewers if x.employee_id == employee_id and x.removed_at is None), None)
    if ri is None:
        raise err(404, "That interviewer isn't on this round.")
    started = ctx.db.scalar(select(func.count()).select_from(models.Interview).where(
        models.Interview.round_id == r.id, models.Interview.started_by == employee_id)) or 0
    if started:
        raise err(409, "This interviewer has already started an interview for this round -- reassign new "
                       "candidates instead of removing them.")
    ri.removed_at, ri.removed_by = now(), (ctx.user.id if ctx.user is not None else None)
    record(ctx, "interview_round", r.id, f"Interviewer removed -- {crud.employee_display_name(ctx.db, employee_id)}",
           old="scheduled", new="scheduled", meta={"removed": str(employee_id)})
    notify(ctx, user_ids_for_employees(ctx.db, [employee_id]), "Removed from interview round",
           f"You've been removed from {r.name} for {r.opening.title}.", "interview_round", r.id, email=False)


def cancel_round(ctx: Ctx, r: models.InterviewRound, reason: str | None) -> None:
    require_round_organizer(ctx, r.opening)
    if r.status != "scheduled":
        raise err(409, "This round is already cancelled.")
    reason = (reason or "").strip()
    if not reason:
        raise err(422, "A cancellation reason is required.")
    r.status, r.cancel_reason = "cancelled", reason
    r.updated_by = ctx.user.id if ctx.user is not None else None
    r.updated_at = now()
    record(ctx, "interview_round", r.id, "Round cancelled", old="scheduled", new="cancelled", comments=reason)
    for i in ctx.db.scalars(select(models.Interview).where(
            models.Interview.round_id == r.id, models.Interview.status.in_(ROUND_INSTANCE_OPEN))).all():
        old = i.status
        i.status, i.cancel_reason = "cancelled", reason
        record(ctx, "interview", i.id, "Interview cancelled (round cancelled)", old=old, new="cancelled",
               comments=reason, application=i.application)
        if old == "scheduled" and (i.scheduled_at or r.scheduled_at):
            _email_interview_cancelled(ctx, i)
    pool = _active_round_pool(r)
    if pool:
        notify(ctx, user_ids_for_employees(ctx.db, pool), "Interview round cancelled",
               f"{r.name} for {r.opening.title}: {reason}", "interview_round", r.id)


def _attach_round_if_configured(ctx: Ctx, a: models.JobApplication, stage: dict) -> models.Interview | None:
    """When a candidate enters a stage that has a configured round, give
    them their own instance of it automatically -- inheriting the round's
    schedule and interviewer pool, editable per-candidate afterwards."""
    r = ctx.db.scalar(select(models.InterviewRound).where(
        models.InterviewRound.opening_id == a.opening_id, models.InterviewRound.round_key == stage["key"],
        models.InterviewRound.status == "scheduled"))
    if r is None:
        return None
    existing = ctx.db.scalar(select(models.Interview).where(
        models.Interview.application_id == a.id, models.Interview.round_id == r.id,
        models.Interview.status != "cancelled"))
    if existing is not None:
        return existing
    return attach_application_to_round(ctx, a, r)


def attach_application_to_round(ctx: Ctx, a: models.JobApplication, r: models.InterviewRound) -> models.Interview:
    i = models.Interview(
        id=uuid.uuid4(), application_id=a.id, round_no=(a.stage_index or 0) + 1, round_id=r.id,
        stage_key=r.round_key, stage_name=r.name, interview_type=r.interview_type, status="scheduled",
        feedback_required=False, reschedule_count=0,
        created_by=ctx.user.id if ctx.user is not None else None, created_at=now(),
    )
    ctx.db.add(i)
    ctx.db.flush()
    pool = _active_round_pool(r)
    for e in pool:
        ctx.db.add(models.InterviewParticipant(id=uuid.uuid4(), interview_id=i.id, employee_id=e,
                                               invited_at=now(), invited_by=i.created_by))
    ctx.db.flush()
    ctx.db.refresh(i)
    record(ctx, "interview", i.id, f"Added to round -- {r.name}", new="scheduled", application=a,
           meta={"round_id": str(r.id)})
    if pool and r.scheduled_at:
        notify(ctx, user_ids_for_employees(ctx.db, pool), "Candidate added to interview round",
               f"{a.candidate.name} joins {r.name} for {a.opening.title} -- {_fmt_when(r.scheduled_at)}.",
               "interview_round", r.id, email=False)
    if r.scheduled_at:
        _email_interview_scheduled(ctx, i)
    return i


def start_interview_round(ctx: Ctx, i: models.Interview) -> None:
    if i.round_id is None:
        raise err(409, "This isn't a round-based interview.")
    if not _is_active_participant(ctx, i):
        raise err(403, "Only an interviewer assigned to this round can start it.")
    if i.status != "scheduled":
        raise err(409, f"This interview is {(i.status or '').replace('_', ' ')} -- it can't be started.")
    old = i.status
    i.status, i.started_at = "in_progress", now()
    i.started_by = ctx.user.employee_id if ctx.user is not None else None
    record(ctx, "interview", i.id, "Interview started", old=old, new="in_progress", application=i.application)


def end_interview_round(ctx: Ctx, i: models.Interview, notes: str | None = None) -> None:
    if i.round_id is None:
        raise err(409, "This isn't a round-based interview.")
    if not _is_active_participant(ctx, i):
        raise err(403, "Only an interviewer assigned to this round can end it.")
    if i.status not in ROUND_INSTANCE_OPEN:
        raise err(409, f"This interview is {(i.status or '').replace('_', ' ')} -- it can't be ended.")
    who = ctx.user.employee_id if ctx.user is not None else None
    old = i.status
    if i.started_at is None:
        i.started_at, i.started_by = now(), who
    i.status, i.ended_at, i.ended_by = "ended", now(), who
    if notes:
        i.notes = notes
    a = i.application
    record(ctx, "interview", i.id, "Interview ended", old=old, new="ended", application=a)
    organizer_ids = {opening_creator_user_id(ctx, a.opening), i.round.created_by if i.round else None}
    notify(ctx, {u for u in organizer_ids if u}, "Interview ended -- decision needed",
           f"{ctx.actor_name} finished {i.stage_name or 'the interview'} with {a.candidate.name} "
           f"({a.opening.title}). Record the outcome to move them forward.",
           "recruitment_interview", i.id, email=False)


def complete_round_decision(ctx: Ctx, i: models.Interview, decision: str, notes: str | None,
                            email_candidate: bool = False) -> None:
    """The Owner/HR's internal call after the interviewer(s) are done:
    advance (passes the stage via the normal pipeline), reject or hold.
    [email_candidate]: tell the candidate -- the next-step email on
    advance, the polite rejection email on reject (never the internal
    notes or reason)."""
    if i.round_id is None:
        raise err(409, "This isn't a round-based interview.")
    a = i.application
    require_round_organizer(ctx, a.opening)
    if i.status != "ended":
        raise err(409, f"This interview is {(i.status or '').replace('_', ' ')} -- it must be ended first.")
    if decision not in HR_DECISIONS:
        raise err(422, "Decision must be advance, reject or hold.")
    notes = (notes or "").strip() or None
    old = i.status
    i.hr_decision, i.hr_decision_notes = decision, notes
    i.hr_decision_at = now()
    i.hr_decision_by = ctx.user.id if ctx.user is not None else None
    i.status = "completed"
    i.result = {"advance": "pass", "reject": "fail", "hold": "hold"}[decision]
    record(ctx, "interview", i.id, f"Round completed -- {decision}", old=old, new="completed", comments=notes,
           application=a)
    notify(ctx, user_ids_for_employees(ctx.db, _participant_ids(i)), "Interview outcome recorded",
           f"{a.candidate.name} -- {i.stage_name or 'interview'}: {decision}.", "recruitment_interview", i.id,
           email=False)
    if decision == "advance":
        stage = _stage_at(ctx, a, a.stage_index)
        if stage is not None and stage["key"] == i.stage_key:
            ok, why = stage_requirement(ctx, a, stage)
            if not ok:
                raise err(409, why)
            _record_stage_result(ctx, a, stage, "pass", notes)
            record(ctx, "application", a.id, f"Passed {stage['name']}", old=app_status(a), new=app_status(a),
                   comments=notes, application=a)
            next_stage, interview = _next_stage(ctx, a, notes)
            if email_candidate:
                _email_next_step(ctx, a, stage["name"], next_stage, interview)
    elif decision == "reject":
        ctx.require_edit()
        st = app_status(a)
        reason = notes or f"Did not pass {i.stage_name or 'the interview'}"
        a.previous_status = st if st != "on_hold" else a.previous_status
        a.rejection_reason = reason[:200]
        a.rejection_notes = notes
        stage = _stage_at(ctx, a, a.stage_index)
        if stage is not None:
            _record_stage_result(ctx, a, stage, "fail", reason)
        _cancel_open_interviews(ctx, a, "Candidate rejected")
        _move(ctx, a, "rejected", f"Candidate rejected at {i.stage_name or 'interview'}", reason)
        if email_candidate:
            _email_candidate_rejected(ctx, a)
    else:  # hold
        ctx.require_edit()
        st = app_status(a)
        reason = notes or f"On hold after {i.stage_name or 'the interview'}"
        a.previous_status = st
        a.hold_reason = reason
        _move(ctx, a, "on_hold", "Put on hold", reason)


# ═══════════════════════════════════════════════════════════════════════════
# History, lookup
# ═══════════════════════════════════════════════════════════════════════════

def history_for(ctx: Ctx, *, application_id=None, entity_type: str | None = None, entity_id=None,
                candidate_id=None, limit: int = 300) -> list[dict]:
    q = select(models.RecruitmentHistory).where(models.RecruitmentHistory.company_id == ctx.company_id)
    if application_id is not None:
        q = q.where(models.RecruitmentHistory.application_id == application_id)
    if entity_type is not None:
        q = q.where(models.RecruitmentHistory.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(models.RecruitmentHistory.entity_id == entity_id)
    if candidate_id is not None:
        q = q.where(models.RecruitmentHistory.candidate_id == candidate_id)
    rows = ctx.db.scalars(q.order_by(models.RecruitmentHistory.created_at.desc()).limit(limit)).all()
    return [{"id": str(h.id), "entity_type": h.entity_type, "entity_id": str(h.entity_id), "action": h.action,
             "old_status": h.old_status, "new_status": h.new_status, "comments": h.comments,
             "metadata": h.meta, "actor": h.actor_name, "at": _iso(h.created_at)} for h in rows]


def lookup(ctx: Ctx, record_id: uuid.UUID) -> dict:
    """Which recruitment record an id (deep link / notification) is."""
    db = ctx.db
    a = db.get(models.JobApplication, record_id)
    if a is not None and a.opening and a.opening.company_id == ctx.company_id:
        return {"kind": "application", "id": str(a.id), "application_id": str(a.id)}
    i = db.get(models.Interview, record_id)
    if i is not None and i.application and i.application.opening.company_id == ctx.company_id:
        return {"kind": "interview", "id": str(i.id), "application_id": str(i.application_id)}
    o = db.get(models.Offer, record_id)
    if o is not None and o.application and o.application.opening.company_id == ctx.company_id:
        return {"kind": "offer", "id": str(o.id), "application_id": str(o.application_id)}
    p = db.get(models.Preboarding, record_id)
    if p is not None and p.company_id == ctx.company_id:
        return {"kind": "preboarding", "id": str(p.id), "application_id": str(p.application_id)}
    op = db.get(models.JobOpening, record_id)
    if op is not None and op.company_id == ctx.company_id:
        return {"kind": "opening", "id": str(op.id)}
    r = db.get(models.HiringRequisition, record_id)
    if r is not None:
        req_emp = db.get(models.Employee, r.requested_by)
        if req_emp is not None and req_emp.company_id == ctx.company_id:
            return {"kind": "requisition", "id": str(r.id)}
    c = db.get(models.Candidate, record_id)
    if c is not None and c.company_id == ctx.company_id:
        return {"kind": "candidate", "id": str(c.id)}
    raise err(404, "Recruitment record not found.")
