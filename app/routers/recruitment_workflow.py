"""Recruitment workflow API (/api/recruitment/...): requisitions, job
openings, candidates, applications (pipeline), interviews + feedback,
offers, preboarding, joining and Create Employee. Business rules live in
app/recruitment_workflow.py and app/recruitment_onboarding.py; the older
/api/job-openings, /api/candidates ... endpoints (routers/recruitment.py)
are unchanged."""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import crud, models
from .. import recruitment_onboarding as onb
from .. import recruitment_workflow as rw
from ..database import get_db
from ..deps import get_current_user, require_module_enabled

class _BadInputAs422Route(APIRoute):
    """M-38: the loose JSON bodies here are validated by the service layer;
    a malformed value it converts with int() / uuid.UUID() / Decimal() /
    date parsing (ValueError, TypeError, InvalidOperation) is the client's
    error -> 422 with the reason, never a 500."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request: Request):
            try:
                return await handler(request)
            except (ValueError, TypeError, decimal.InvalidOperation) as exc:
                raise HTTPException(status_code=422, detail=f"Invalid value: {exc}") from exc

        return wrapped


router = APIRouter(
    prefix="/api/recruitment", tags=["recruitment-workflow"],
    dependencies=[Depends(require_module_enabled("recruitment"))],
    route_class=_BadInputAs422Route,
)


def _ctx(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)) -> rw.Ctx:
    return rw.make_ctx(db, user)


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _date(value: str | None) -> datetime.date | None:
    return rw._parse_date(value) if value else None


class Body(BaseModel):
    """Loose JSON object -- every field is validated by the service."""
    model_config = {"extra": "allow"}

    def data(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class Reason(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    comments: str | None = Field(default=None, max_length=2000)


class Decision(BaseModel):
    decision: str
    comments: str | None = Field(default=None, max_length=2000)
    # Interview-round decisions: email the candidate (next step / rejection).
    email_candidate: bool = False


# ── access / settings / reports ─────────────────────────────────────────────

@router.get("/me")
def my_access(ctx: rw.Ctx = Depends(_ctx)):
    return {"level": ctx.level, "can_view": ctx.can_view, "can_edit": ctx.can_edit, "can_admin": ctx.can_admin,
            "can_see_compensation": ctx.can_see_compensation, "employee_id": str(ctx.user.employee_id) if ctx.user.employee_id else None,
            "can_create_employee": ctx.can_edit and crud.can_access_people_module(ctx.db, ctx.user, "create")}


@router.get("/settings")
def get_settings(ctx: rw.Ctx = Depends(_ctx)):
    if not (ctx.can_view or ctx.can_edit):
        raise rw.err(403, "You don't have access to Recruitment.")
    return rw.get_settings(ctx.db, ctx.company_id)


@router.put("/settings")
def save_settings(body: Body, ctx: rw.Ctx = Depends(_ctx)):
    out = rw.save_settings(ctx, body.data())
    ctx.db.commit()
    return out


@router.get("/summary")
def summary(date_from: str | None = None, date_to: str | None = None, opening_id: uuid.UUID | None = None,
            ctx: rw.Ctx = Depends(_ctx)):
    out = onb.summary(ctx, date_from=_date(date_from), date_to=_date(date_to), opening_id=opening_id)
    ctx.db.commit()
    return out


@router.get("/my-actions")
def my_actions(ctx: rw.Ctx = Depends(_ctx)):
    return onb.my_actions(ctx)


@router.get("/lookup/{record_id}")
def lookup(record_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    return rw.lookup(ctx, record_id)


# ── requisitions ────────────────────────────────────────────────────────────

@router.get("/requisitions")
def list_requisitions(status: str | None = None, search: str | None = None, department_id: uuid.UUID | None = None,
                      limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), ctx: rw.Ctx = Depends(_ctx)):
    return rw.list_requisitions(ctx, status=_csv(status), search=search, department_id=department_id,
                                limit=limit, offset=offset)


def _req_detail(ctx: rw.Ctx, req_id: uuid.UUID) -> dict:
    r = rw.get_requisition(ctx, req_id)
    if not rw.can_view_requisition(ctx, r):
        raise rw.err(403, "You don't have access to this requisition.")
    return rw.requisition_out(ctx, r, detail=True)


@router.post("/requisitions", status_code=201)
def create_requisition(body: Body, ctx: rw.Ctx = Depends(_ctx)):
    data = body.data()
    submit = bool(data.pop("submit", False))
    r = rw.create_requisition(ctx, data, submit=submit)
    ctx.db.commit()
    return _req_detail(ctx, r.id)


@router.get("/requisitions/{req_id}")
def get_requisition(req_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    return _req_detail(ctx, req_id)


@router.patch("/requisitions/{req_id}")
def update_requisition(req_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    r = rw.get_requisition(ctx, req_id, lock=True)
    rw.update_requisition(ctx, r, body.data())
    ctx.db.commit()
    return _req_detail(ctx, req_id)


@router.post("/requisitions/{req_id}/submit")
def submit_requisition(req_id: uuid.UUID, body: Reason | None = None, ctx: rw.Ctx = Depends(_ctx)):
    r = rw.get_requisition(ctx, req_id, lock=True)
    rw.submit_requisition(ctx, r, (body.comments if body else None))
    ctx.db.commit()
    return _req_detail(ctx, req_id)


@router.post("/requisitions/{req_id}/decision")
def decide_requisition(req_id: uuid.UUID, body: Decision, ctx: rw.Ctx = Depends(_ctx)):
    r = rw.get_requisition(ctx, req_id, lock=True)
    decision = {"approve": "approved", "reject": "rejected", "request_changes": "sent_back"}.get(body.decision, body.decision)
    rw.decide_requisition(ctx, r, decision, body.comments)
    ctx.db.commit()
    return _req_detail(ctx, req_id)


@router.post("/requisitions/{req_id}/cancel")
def cancel_requisition(req_id: uuid.UUID, body: Reason, ctx: rw.Ctx = Depends(_ctx)):
    r = rw.get_requisition(ctx, req_id, lock=True)
    rw.cancel_requisition(ctx, r, body.reason)
    ctx.db.commit()
    return _req_detail(ctx, req_id)


@router.post("/requisitions/{req_id}/create-opening", status_code=201)
def create_opening_from_requisition(req_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    r = rw.get_requisition(ctx, req_id, lock=True)
    o = rw.create_opening_from_requisition(ctx, r)
    ctx.db.commit()
    return rw.opening_out(ctx, o, detail=True)


# ── job openings ────────────────────────────────────────────────────────────

@router.get("/job-openings")
def list_openings(status: str | None = None, department_id: uuid.UUID | None = None, branch_id: uuid.UUID | None = None,
                  search: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_view()
    return rw.list_openings(ctx, status=_csv(status), department_id=department_id, branch_id=branch_id,
                            search=search, limit=limit, offset=offset)


@router.post("/job-openings", status_code=201)
def create_opening(body: Body, ctx: rw.Ctx = Depends(_ctx)):
    o = rw.create_opening(ctx, body.data())
    ctx.db.commit()
    return rw.opening_out(ctx, o, detail=True)


@router.get("/job-openings/{opening_id}")
def get_opening(opening_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_view()
    return rw.opening_out(ctx, rw.get_opening(ctx, opening_id), detail=True)


@router.patch("/job-openings/{opening_id}")
def update_opening(opening_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    o = rw.get_opening(ctx, opening_id, lock=True)
    rw.update_opening(ctx, o, body.data())
    ctx.db.commit()
    return rw.opening_out(ctx, o, detail=True)


@router.post("/job-openings/{opening_id}/clone", status_code=201)
def clone_opening(opening_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    c = rw.clone_opening(ctx, rw.get_opening(ctx, opening_id))
    ctx.db.commit()
    return rw.opening_out(ctx, c, detail=True)


@router.get("/job-openings/{opening_id}/rounds")
def list_rounds(opening_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    o = rw.get_opening(ctx, opening_id)
    return {"items": rw.list_rounds(ctx, o)}


@router.post("/job-openings/{opening_id}/rounds", status_code=201)
def create_round(opening_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    o = rw.get_opening(ctx, opening_id, lock=True)
    r = rw.create_round(ctx, o, body.data())
    ctx.db.commit()
    return rw._round_out(ctx, r)


# NOTE: the routes above must stay registered before this catch-all -- FastAPI
# matches path routes in registration order, and POST /job-openings/{id}/rounds
# would otherwise be swallowed by {action}="rounds" here.
@router.post("/job-openings/{opening_id}/{action}")
def opening_action(opening_id: uuid.UUID, action: str, body: Reason | None = None, ctx: rw.Ctx = Depends(_ctx)):
    o = rw.get_opening(ctx, opening_id, lock=True)
    rw.transition_opening(ctx, o, action, (body.comments or body.reason) if body else None)
    ctx.db.commit()
    return rw.opening_out(ctx, o, detail=True)


# ── candidates ──────────────────────────────────────────────────────────────

@router.get("/candidates")
def list_candidates(search: str | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                    ctx: rw.Ctx = Depends(_ctx)):
    return rw.search_candidates(ctx, search, limit, offset)


@router.get("/candidates/duplicates")
def candidate_duplicates(email: str | None = None, phone: str | None = None, ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_view()
    return [rw.candidate_out(ctx, c, detail=True) for c in rw.find_duplicates(ctx, email, phone)]


@router.post("/candidates", status_code=201)
def create_candidate(body: Body, ctx: rw.Ctx = Depends(_ctx)):
    data = body.data()
    allow = bool(data.pop("allow_duplicate", False))
    apply_to = data.pop("opening_id", None)
    source = data.get("source")
    owner_id = data.pop("owner_id", None)
    c = rw.create_candidate(ctx, data, allow_duplicate=allow)
    app_id = None
    if apply_to:
        app_id = rw.create_application(ctx, c.id, apply_to, source=source, owner_id=owner_id).id
    ctx.db.commit()
    out = rw.candidate_out(ctx, c, detail=True)
    out["application_id"] = str(app_id) if app_id else None
    return out


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_view()
    c = rw.get_candidate(ctx, candidate_id)
    out = rw.candidate_out(ctx, c, detail=True)
    out["history"] = rw.history_for(ctx, candidate_id=c.id)
    return out


@router.patch("/candidates/{candidate_id}")
def update_candidate(candidate_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    c = rw.get_candidate(ctx, candidate_id)
    rw.update_candidate(ctx, c, body.data())
    ctx.db.commit()
    return rw.candidate_out(ctx, c, detail=True)


@router.post("/candidates/{candidate_id}/resume")
def upload_resume(candidate_id: uuid.UUID, file: UploadFile = File(...), ctx: rw.Ctx = Depends(_ctx)):
    from ..storage import delete_uploaded_file, save_uploaded_file

    ctx.require_edit()
    c = rw.get_candidate(ctx, candidate_id)
    if not (file.filename or "").lower().endswith((".pdf", ".doc", ".docx")):
        raise rw.err(400, "Upload the resume as PDF, DOC or DOCX.")
    url, _size = save_uploaded_file(file, entity_type="candidate_resume", entity_id=c.id)
    old = c.resume_url
    c.resume_url, c.resume_filename, c.resume_uploaded_at = url, (file.filename or "resume")[:255], rw.now()
    rw.record(ctx, "candidate", c.id, "Resume uploaded", candidate_id=c.id, meta={"file": c.resume_filename})
    ctx.db.commit()
    if old:
        delete_uploaded_file(old)
    return rw.candidate_out(ctx, c)


@router.get("/candidates/{candidate_id}/resume")
def download_resume(candidate_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    from ..storage import uploads_root, winlong_path
    from sqlalchemy import select

    c = rw.get_candidate(ctx, candidate_id)
    if not ctx.can_view:
        apps = ctx.db.scalars(select(models.JobApplication).where(models.JobApplication.candidate_id == c.id)).all()
        if not any(rw.is_panelist(ctx, a) for a in apps):
            raise rw.err(403, "You don't have access to this resume.")
    if not c.resume_url:
        raise rw.err(404, "No resume uploaded.")
    path = winlong_path(uploads_root() / c.resume_url.removeprefix("/media/"))
    if not path.exists():
        raise rw.err(404, "The resume file is missing.")
    return FileResponse(path, filename=c.resume_filename or "resume")


# ── applications ────────────────────────────────────────────────────────────

@router.get("/applications")
def list_applications(opening_id: uuid.UUID | None = None, status: str | None = None, source: str | None = None,
                      owner_id: uuid.UUID | None = None, department_id: uuid.UUID | None = None, search: str | None = None,
                      date_from: str | None = None, date_to: str | None = None, exp_min: float | None = None,
                      exp_max: float | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                      ctx: rw.Ctx = Depends(_ctx)):
    return rw.list_applications(ctx, opening_id=opening_id, status=_csv(status), source=source, owner_id=owner_id,
                                department_id=department_id, search=search, date_from=_date(date_from),
                                date_to=_date(date_to), exp_min=exp_min, exp_max=exp_max, limit=limit, offset=offset)


@router.get("/pipeline")
def get_pipeline(opening_id: uuid.UUID | None = None, search: str | None = None, source: str | None = None,
                 owner_id: uuid.UUID | None = None, ctx: rw.Ctx = Depends(_ctx)):
    return rw.pipeline(ctx, opening_id=opening_id, search=search, source=source, owner_id=owner_id)


class ApplicationCreate(BaseModel):
    candidate_id: uuid.UUID
    opening_id: uuid.UUID
    source: str | None = None
    owner_id: uuid.UUID | None = None
    notes: str | None = None


@router.post("/applications", status_code=201)
def create_application(body: ApplicationCreate, ctx: rw.Ctx = Depends(_ctx)):
    a = rw.create_application(ctx, body.candidate_id, body.opening_id, source=body.source, owner_id=body.owner_id,
                              notes=body.notes)
    ctx.db.commit()
    return rw.application_out(ctx, a, detail=True)


@router.get("/applications/{application_id}")
def get_application(application_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    a = rw.get_application(ctx, application_id)
    rw.require_application_view(ctx, a)
    return rw.application_out(ctx, a, detail=True)


@router.patch("/applications/{application_id}")
def update_application(application_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_edit()
    a = rw.get_application(ctx, application_id, lock=True)
    data = body.data()
    if "owner_id" in data:
        owner = rw.employee_in_company(ctx, data["owner_id"], "Recruiter")
        a.owner_id = owner.id if owner else None
    if "notes" in data:
        a.notes = data["notes"]
    if "rating" in data:
        if data["rating"] is not None and not 1 <= int(data["rating"]) <= 5:
            raise rw.err(422, "Rating must be 1-5.")
        a.rating = data["rating"]
    rw.record(ctx, "application", a.id, "Application details updated", application=a,
              meta={k: str(v) for k, v in data.items() if k in ("owner_id", "rating")})
    ctx.db.commit()
    return rw.application_out(ctx, a, detail=True)


@router.post("/applications/{application_id}/actions/{action}")
def application_action(application_id: uuid.UUID, action: str, body: Body | None = None, ctx: rw.Ctx = Depends(_ctx)):
    a = rw.get_application(ctx, application_id, lock=True)
    rw.transition(ctx, a, action, body.data() if body else {})
    ctx.db.commit()
    return rw.application_out(ctx, a, detail=True)


# ── interviews ──────────────────────────────────────────────────────────────

@router.get("/interviews")
def list_interviews(mine: bool = False, status: str | None = None, application_id: uuid.UUID | None = None,
                    opening_id: uuid.UUID | None = None, interviewer_id: uuid.UUID | None = None,
                    date_from: str | None = None, date_to: str | None = None,
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), ctx: rw.Ctx = Depends(_ctx)):
    return rw.list_interviews(ctx, mine=mine, status=_csv(status), application_id=application_id, opening_id=opening_id,
                              interviewer_id=interviewer_id, date_from=_date(date_from), date_to=_date(date_to),
                              limit=limit, offset=offset)


@router.post("/interviews", status_code=201)
def schedule_interview(body: Body, ctx: rw.Ctx = Depends(_ctx)):
    data = body.data()
    if not data.get("application_id"):
        raise rw.err(422, "application_id is required.")
    a = rw.get_application(ctx, data["application_id"], lock=True)
    i = rw.schedule_interview(ctx, a, data)
    ctx.db.commit()
    return rw.interview_out(ctx, i)


def _interview_for(ctx: rw.Ctx, interview_id: uuid.UUID, lock: bool = False) -> models.Interview:
    i = rw.get_interview(ctx, interview_id, lock=lock)
    if not (ctx.can_view or rw.is_interview_participant(ctx, i)):
        raise rw.err(403, "You don't have access to this interview.")
    return i


@router.get("/interviews/{interview_id}")
def get_interview(interview_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    i = _interview_for(ctx, interview_id)
    out = rw.interview_out(ctx, i)
    a = i.application
    out["candidate"] = {"name": a.candidate.name, "email": a.candidate.email if ctx.can_view else None,
                        "years_experience": rw._num(a.candidate.years_experience), "skills": a.candidate.skills,
                        "current_company": a.candidate.current_company, "has_resume": bool(a.candidate.resume_url),
                        "id": str(a.candidate_id)}
    out["history"] = rw.history_for(ctx, entity_type="interview", entity_id=i.id) if ctx.can_view else []
    return out


@router.post("/interviews/{interview_id}/reschedule")
def reschedule_interview(interview_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    i = rw.get_interview(ctx, interview_id, lock=True)
    rw.reschedule_interview(ctx, i, body.data())
    ctx.db.commit()
    return rw.interview_out(ctx, i)


@router.post("/interviews/{interview_id}/cancel")
def cancel_interview(interview_id: uuid.UUID, body: Reason, ctx: rw.Ctx = Depends(_ctx)):
    i = rw.get_interview(ctx, interview_id, lock=True)
    rw.cancel_interview(ctx, i, body.reason)
    ctx.db.commit()
    return rw.interview_out(ctx, i)


@router.post("/interviews/{interview_id}/complete")
def complete_interview(interview_id: uuid.UUID, body: Body | None = None, ctx: rw.Ctx = Depends(_ctx)):
    i = rw.get_interview(ctx, interview_id, lock=True)
    rw.complete_interview(ctx, i, body.data() if body else {})
    ctx.db.commit()
    return rw.interview_out(ctx, i)


@router.post("/interviews/{interview_id}/no-show")
def interview_no_show(interview_id: uuid.UUID, body: Reason | None = None, ctx: rw.Ctx = Depends(_ctx)):
    i = rw.get_interview(ctx, interview_id, lock=True)
    rw.mark_no_show(ctx, i, (body.reason or body.comments) if body else None)
    ctx.db.commit()
    return rw.interview_out(ctx, i)


class InvitationResponse(BaseModel):
    response: str
    reason: str | None = Field(default=None, max_length=2000)
    proposed_availability: str | None = Field(default=None, max_length=2000)


@router.post("/interview-invitations", status_code=201)
def invite_interviewers(body: Body, ctx: rw.Ctx = Depends(_ctx)):
    """Step 1: invite interviewers for the candidate's current stage (no time yet)."""
    data = body.data()
    if not data.get("application_id"):
        raise rw.err(422, "application_id is required.")
    a = rw.get_application(ctx, data["application_id"], lock=True)
    i = rw.invite_interviewers(ctx, a, data)
    ctx.db.commit()
    return rw.interview_out(ctx, i)


@router.post("/interviews/{interview_id}/invite")
def add_interviewers(interview_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    """Invite more interviewers, or replace one who declined (`replaces`)."""
    i = rw.get_interview(ctx, interview_id, lock=True)
    rw.add_interviewers(ctx, i, body.data())
    ctx.db.commit()
    ctx.db.refresh(i)
    return rw.interview_out(ctx, i)


@router.post("/interviews/{interview_id}/schedule")
def schedule_invited_interview(interview_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    """Step 2: set the time once an interviewer has accepted."""
    i = rw.get_interview(ctx, interview_id, lock=True)
    rw.schedule_invited_interview(ctx, i, body.data())
    ctx.db.commit()
    ctx.db.refresh(i)
    return rw.interview_out(ctx, i)


@router.get("/interviews/invitations/pending")
def pending_interview_invitations(ctx: rw.Ctx = Depends(_ctx)):
    return {"items": rw.pending_invitations(ctx)}


@router.post("/interviews/{interview_id}/respond")
def respond_to_interview(interview_id: uuid.UUID, body: InvitationResponse, ctx: rw.Ctx = Depends(_ctx)):
    """An interviewer accepts, rejects/declines or asks to reschedule (invitees only)."""
    i = rw.get_interview(ctx, interview_id, lock=True)
    rw.respond_to_invitation(ctx, i, body.response, body.reason, body.proposed_availability)
    ctx.db.commit()
    ctx.db.refresh(i)
    return rw.interview_out(ctx, i)


@router.post("/interviews/{interview_id}/feedback")
def submit_feedback(interview_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    i = _interview_for(ctx, interview_id, lock=True)
    rw.submit_feedback(ctx, i, body.data())
    ctx.db.commit()
    ctx.db.refresh(i)
    return rw.interview_out(ctx, i)


# ── interview rounds (shared sessions) ──────────────────────────────────────
# (the /job-openings/{opening_id}/rounds list+create routes are registered
# earlier, alongside the other job-opening routes -- see above -- so they are
# matched before the generic POST /job-openings/{opening_id}/{action} catch-all)

class RoundInterviewers(BaseModel):
    employee_ids: list[uuid.UUID] = Field(default_factory=list)


@router.patch("/interview-rounds/{round_id}")
def update_round(round_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    r = rw.get_round(ctx, round_id, lock=True)
    rw.update_round(ctx, r, body.data())
    ctx.db.commit()
    ctx.db.refresh(r)
    return rw._round_out(ctx, r)


@router.post("/interview-rounds/{round_id}/interviewers")
def add_round_interviewers(round_id: uuid.UUID, body: RoundInterviewers, ctx: rw.Ctx = Depends(_ctx)):
    r = rw.get_round(ctx, round_id, lock=True)
    rw.add_round_interviewers(ctx, r, body.employee_ids)
    ctx.db.commit()
    ctx.db.refresh(r)
    return rw._round_out(ctx, r)


@router.delete("/interview-rounds/{round_id}/interviewers/{employee_id}")
def remove_round_interviewer(round_id: uuid.UUID, employee_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    r = rw.get_round(ctx, round_id, lock=True)
    rw.remove_round_interviewer(ctx, r, employee_id)
    ctx.db.commit()
    ctx.db.refresh(r)
    return rw._round_out(ctx, r)


@router.post("/interview-rounds/{round_id}/cancel")
def cancel_round(round_id: uuid.UUID, body: Reason, ctx: rw.Ctx = Depends(_ctx)):
    r = rw.get_round(ctx, round_id, lock=True)
    rw.cancel_round(ctx, r, body.reason or body.comments)
    ctx.db.commit()
    ctx.db.refresh(r)
    return rw._round_out(ctx, r)


@router.post("/applications/{application_id}/attach-round", status_code=201)
def attach_round(application_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    a = rw.get_application(ctx, application_id, lock=True)
    data = body.data()
    if not data.get("round_id"):
        raise rw.err(422, "round_id is required.")
    r = rw.get_round(ctx, data["round_id"], lock=True)
    if r.opening_id != a.opening_id:
        raise rw.err(409, "That round belongs to a different job opening.")
    rw.require_round_organizer(ctx, a.opening)
    i = rw.attach_application_to_round(ctx, a, r)
    ctx.db.commit()
    return rw.interview_out(ctx, i)


@router.post("/interviews/{interview_id}/start")
def start_interview(interview_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    i = _interview_for(ctx, interview_id, lock=True)
    rw.start_interview_round(ctx, i)
    ctx.db.commit()
    ctx.db.refresh(i)
    return rw.interview_out(ctx, i)


@router.post("/interviews/{interview_id}/end")
def end_interview(interview_id: uuid.UUID, body: Body | None = None, ctx: rw.Ctx = Depends(_ctx)):
    i = _interview_for(ctx, interview_id, lock=True)
    rw.end_interview_round(ctx, i, body.data().get("notes") if body else None)
    ctx.db.commit()
    ctx.db.refresh(i)
    return rw.interview_out(ctx, i)


@router.post("/interviews/{interview_id}/complete-round")
def complete_round(interview_id: uuid.UUID, body: Decision, ctx: rw.Ctx = Depends(_ctx)):
    i = rw.get_interview(ctx, interview_id, lock=True)
    rw.complete_round_decision(ctx, i, body.decision, body.comments, email_candidate=body.email_candidate)
    ctx.db.commit()
    ctx.db.refresh(i)
    return rw.interview_out(ctx, i)


# ── offers ──────────────────────────────────────────────────────────────────

@router.get("/offers")
def list_offers(status: str | None = None, opening_id: uuid.UUID | None = None, search: str | None = None,
                joining_from: str | None = None, joining_to: str | None = None,
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), ctx: rw.Ctx = Depends(_ctx)):
    out = onb.list_offers(ctx, status=_csv(status), opening_id=opening_id, search=search,
                          joining_from=_date(joining_from), joining_to=_date(joining_to), limit=limit, offset=offset)
    ctx.db.commit()
    return out


def _offer_detail(ctx: rw.Ctx, offer_id) -> dict:
    o = onb.get_offer(ctx, offer_id)
    if not (ctx.can_view or onb.can_decide_offer(ctx, o)):
        raise rw.err(403, "You don't have access to this offer.")
    return onb.offer_out(ctx, o, detail=True)


@router.post("/offers", status_code=201)
def create_offer(body: Body, ctx: rw.Ctx = Depends(_ctx)):
    data = body.data()
    if not data.get("application_id"):
        raise rw.err(422, "application_id is required.")
    a = rw.get_application(ctx, data.pop("application_id"), lock=True)
    o = onb.create_offer(ctx, a, data)
    ctx.db.commit()
    return _offer_detail(ctx, o.id)


@router.get("/offers/{offer_id}")
def get_offer(offer_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    out = _offer_detail(ctx, offer_id)
    ctx.db.commit()
    return out


@router.patch("/offers/{offer_id}")
def update_offer(offer_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    o = onb.get_offer(ctx, offer_id, lock=True)
    onb.update_offer(ctx, o, body.data())
    ctx.db.commit()
    return _offer_detail(ctx, offer_id)


@router.post("/offers/{offer_id}/submit")
def submit_offer(offer_id: uuid.UUID, body: Reason | None = None, ctx: rw.Ctx = Depends(_ctx)):
    o = onb.get_offer(ctx, offer_id, lock=True)
    onb.submit_offer(ctx, o, body.comments if body else None)
    ctx.db.commit()
    return _offer_detail(ctx, offer_id)


@router.post("/offers/{offer_id}/decision")
def decide_offer(offer_id: uuid.UUID, body: Decision, ctx: rw.Ctx = Depends(_ctx)):
    o = onb.get_offer(ctx, offer_id, lock=True)
    decision = {"approve": "approved", "reject": "rejected", "request_changes": "sent_back"}.get(body.decision, body.decision)
    onb.decide_offer(ctx, o, decision, body.comments)
    ctx.db.commit()
    return onb.offer_out(ctx, o, detail=True)


@router.post("/offers/{offer_id}/revise")
def revise_offer(offer_id: uuid.UUID, body: Reason | None = None, ctx: rw.Ctx = Depends(_ctx)):
    o = onb.get_offer(ctx, offer_id, lock=True)
    onb.revise_offer(ctx, o, body.comments if body else None)
    ctx.db.commit()
    return _offer_detail(ctx, offer_id)


@router.post("/offers/{offer_id}/send")
def send_offer(offer_id: uuid.UUID, body: Body | None = None, ctx: rw.Ctx = Depends(_ctx)):
    o = onb.get_offer(ctx, offer_id, lock=True)
    result = onb.send_offer(ctx, o, body.data() if body else {})
    ctx.db.commit()
    return {**_offer_detail(ctx, offer_id), "send_result": result}


@router.post("/offers/{offer_id}/mark-viewed")
def mark_viewed(offer_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_edit()
    o = onb.get_offer(ctx, offer_id, lock=True)
    onb.mark_offer_viewed(ctx, o)
    ctx.db.commit()
    return _offer_detail(ctx, offer_id)


@router.post("/offers/{offer_id}/accept")
def accept_offer(offer_id: uuid.UUID, body: Body | None = None, ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_edit()
    o = onb.get_offer(ctx, offer_id, lock=True)
    data = body.data() if body else {}
    try:
        pb = onb.accept_offer(ctx, o, source=data.get("source") or "hr", notes=data.get("notes"),
                              accepted_on=rw._parse_date(data.get("accepted_on")))
    except HTTPException:
        ctx.db.commit()  # keeps an automatic expiry recorded by the check
        raise
    ctx.db.commit()
    return {**_offer_detail(ctx, offer_id), "preboarding_id": str(pb.id)}


@router.post("/offers/{offer_id}/decline")
def decline_offer(offer_id: uuid.UUID, body: Reason | None = None, ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_edit()
    o = onb.get_offer(ctx, offer_id, lock=True)
    onb.decline_offer(ctx, o, source="hr", reason=body.reason if body else None)
    ctx.db.commit()
    return _offer_detail(ctx, offer_id)


@router.post("/offers/{offer_id}/withdraw")
def withdraw_offer(offer_id: uuid.UUID, body: Reason, ctx: rw.Ctx = Depends(_ctx)):
    o = onb.get_offer(ctx, offer_id, lock=True)
    onb.withdraw_offer(ctx, o, body.reason)
    ctx.db.commit()
    return _offer_detail(ctx, offer_id)


@router.get("/offers/{offer_id}/pdf")
def offer_pdf(offer_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    from .recruitment import offer_letter_pdf

    ctx.require_edit()
    onb.get_offer(ctx, offer_id)
    company = ctx.db.get(models.Company, ctx.company_id)
    pdf_bytes, safe_name = offer_letter_pdf(ctx.db, company, crud.get_offer_detail(ctx.db, offer_id, company.id))
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="offer-letter-{safe_name}.pdf"'})


# ── preboarding ─────────────────────────────────────────────────────────────

@router.get("/preboarding")
def list_preboarding(status: str | None = None, search: str | None = None, joining_from: str | None = None,
                     joining_to: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     ctx: rw.Ctx = Depends(_ctx)):
    return onb.list_preboardings(ctx, status=_csv(status), search=search, joining_from=_date(joining_from),
                                 joining_to=_date(joining_to), limit=limit, offset=offset)


def _pb_detail(ctx: rw.Ctx, pb_id) -> dict:
    ctx.require_view()
    return onb.preboarding_out(ctx, onb.get_preboarding(ctx, pb_id))


@router.get("/preboarding/{pb_id}")
def get_preboarding(pb_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    return _pb_detail(ctx, pb_id)


@router.post("/preboarding/{pb_id}/send-tasks")
def send_tasks(pb_id: uuid.UUID, body: Body | None = None, ctx: rw.Ctx = Depends(_ctx)):
    p = onb.get_preboarding(ctx, pb_id, lock=True)
    result = onb.send_tasks(ctx, p, body.data() if body else {})
    ctx.db.commit()
    return {**_pb_detail(ctx, pb_id), "send_result": result}


@router.put("/preboarding/{pb_id}/details")
def update_details(pb_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    p = onb.get_preboarding(ctx, pb_id, lock=True)
    onb.update_joiner_details(ctx, p, body.data())
    ctx.db.commit()
    return _pb_detail(ctx, pb_id)


@router.post("/preboarding/{pb_id}/tasks", status_code=201)
def add_task(pb_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    p = onb.get_preboarding(ctx, pb_id, lock=True)
    onb.add_task(ctx, p, body.data())
    ctx.db.commit()
    return _pb_detail(ctx, pb_id)


@router.patch("/preboarding/tasks/{task_id}")
def update_task(task_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    t = onb.get_task(ctx, task_id)
    pb_id = t.preboarding_id
    onb.update_task(ctx, t, body.data())
    ctx.db.commit()
    return _pb_detail(ctx, pb_id)


@router.post("/preboarding/tasks/{task_id}/documents", status_code=201)
def upload_document(task_id: uuid.UUID, file: UploadFile = File(...), ctx: rw.Ctx = Depends(_ctx)):
    t = onb.get_task(ctx, task_id)
    pb_id = t.preboarding_id
    onb.upload_document(ctx, t, file)
    ctx.db.commit()
    return _pb_detail(ctx, pb_id)


@router.get("/preboarding/documents/{doc_id}/file")
def document_file(doc_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_edit()
    d = onb.get_document(ctx, doc_id)
    path = onb.document_path(d)
    if not path.exists():
        raise rw.err(404, "The file is missing.")
    return FileResponse(path, filename=d.original_filename, media_type=d.content_type or None)


@router.post("/preboarding/documents/{doc_id}/review")
def review_document(doc_id: uuid.UUID, body: Decision, ctx: rw.Ctx = Depends(_ctx)):
    d = onb.get_document(ctx, doc_id)
    pb_id = d.preboarding_id
    onb.review_document(ctx, d, body.decision, body.comments)
    ctx.db.commit()
    return _pb_detail(ctx, pb_id)


_PB_ACTIONS = {
    "reinitiate": lambda ctx, p, b: onb.reinitiate_verification(ctx, p, b.get("reason")),
    "override-verification": lambda ctx, p, b: onb.override_verification(ctx, p, b.get("reason")),
    "reject": lambda ctx, p, b: onb.reject_candidate_from_preboarding(ctx, p, b.get("reason")),
    "confirm-joining": lambda ctx, p, b: onb.confirm_joining(ctx, p, b),
    "reschedule-joining": lambda ctx, p, b: onb.reschedule_joining(ctx, p, b.get("joining_date"), b.get("reason")),
    "cancel-joining": lambda ctx, p, b: onb.cancel_joining(ctx, p, b.get("reason")),
    "no-show": lambda ctx, p, b: onb.mark_no_show(ctx, p, b.get("reason")),
    "close-no-show": lambda ctx, p, b: onb.close_no_show(ctx, p, b.get("reason")),
}


@router.get("/preboarding/{pb_id}/employee-prefill")
def employee_prefill(pb_id: uuid.UUID, ctx: rw.Ctx = Depends(_ctx)):
    ctx.require_edit()
    p = onb.get_preboarding(ctx, pb_id)
    return onb.employee_prefill(ctx, p)


@router.post("/preboarding/{pb_id}/create-employee")
def create_employee(pb_id: uuid.UUID, body: Body, ctx: rw.Ctx = Depends(_ctx)):
    p = onb.get_preboarding(ctx, pb_id, lock=True)
    try:
        result = onb.create_employee(ctx, p, body.data())
    except onb.EmployeeCreationFailed as exc:
        ctx.db.commit()  # keeps the failure status + history; nothing else changed
        detail = {"message": exc.message if isinstance(exc.message, str) else "Validation failed",
                  "code": exc.code, **exc.extra}
        if not isinstance(exc.message, str):
            detail["errors"] = exc.message
        raise HTTPException(status_code=exc.status, detail=detail) from exc
    ctx.db.commit()
    return {**_pb_detail(ctx, pb_id), "result": result}


@router.post("/preboarding/{pb_id}/{action}")
def preboarding_action(pb_id: uuid.UUID, action: str, body: Body | None = None, ctx: rw.Ctx = Depends(_ctx)):
    fn = _PB_ACTIONS.get(action)
    if fn is None:
        raise rw.err(404, "Unknown preboarding action.")
    p = onb.get_preboarding(ctx, pb_id, lock=True)
    fn(ctx, p, body.data() if body else {})
    ctx.db.commit()
    return _pb_detail(ctx, pb_id)
