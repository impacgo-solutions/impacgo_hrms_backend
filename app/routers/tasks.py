"""Task Management review workflow API (see app/tasks.py).

  GET   /api/tasks/{id}/eligible-reviewers   assignee / Reporting Manager
  POST  /api/tasks/{id}/start                assignee: Assigned | Changes Requested -> In Progress
  POST  /api/tasks/{id}/submit               assignee: description + files + reviewer -> Submitted for Review
  PATCH /api/tasks/{id}/reviewer             assignee: change the reviewer while under review
  POST  /api/tasks/{id}/review               reviewer / Reporting Manager: approve -> Completed,
                                             or send back -> Changes Requested (comments required)

Every action locks the task row, re-checks the caller's relation to it and
its current status (so a double click / two reviewers can't both act), and
returns the refreshed Task Details. Board summary / create / edit / move
stay in routers/projects.py.
"""

import pathlib
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from .. import tasks as tw
from ..database import get_db
from ..deps import get_current_user, require_module_enabled
from ..storage import _ALLOWED_EXTENSIONS, save_uploaded_file

router = APIRouter(
    prefix="/api/tasks", tags=["tasks"],
    dependencies=[Depends(require_module_enabled("projects"))],
)

_DESCRIPTION_MIN = 10
_DESCRIPTION_MAX = 5000


def detail_out(db: Session, user: models.User, task_id: uuid.UUID) -> dict:
    from .projects import _TASK_DB_TO_COL
    task = db.get(models.TaskBoardCard, task_id)
    detail = crud.get_task_board_card_detail(db, user.company_id, task_id)
    detail["column_key"] = _TASK_DB_TO_COL.get(detail["status"], detail["status"])
    return tw.enrich_detail(db, user, task, detail)


@router.get("/{task_id}/eligible-reviewers", response_model=list[schemas.TaskReviewerOption])
def eligible_reviewers(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    task = tw.require_view(db, current_user, db.get(models.TaskBoardCard, task_id))
    if not tw.roles(db, current_user, task) & {"assignee", "manager"}:
        raise HTTPException(status_code=403, detail="Only the assigned employee can choose a reviewer.")
    rm = tw.reporting_manager_id(db, task)
    return [
        schemas.TaskReviewerOption(
            id=e.id, name=crud.employee_display_name(db, e.id), employee_code=e.employee_code,
            is_reporting_manager=e.id == rm,
        )
        for e in tw.eligible_reviewers(db, task)
    ]


@router.post("/{task_id}/start", response_model=schemas.TaskCardDetailOut)
def start_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    task = tw.lock_task(db, current_user, task_id)
    if "assignee" not in tw.roles(db, current_user, task):
        raise HTTPException(status_code=403, detail="Only the assigned employee can start this task.")
    if task.workflow_status not in (tw.ASSIGNED, tw.CHANGES_REQUESTED):
        raise HTTPException(status_code=409, detail=f"This task is already {tw.STATUS_LABELS.get(task.workflow_status, task.workflow_status)}.")
    before = task.workflow_status
    tw.set_status(task, tw.IN_PROGRESS)
    task.actual_start_date = task.actual_start_date or tw.now_utc()
    h = tw.record(db, task, current_user, "started", before, tw.IN_PROGRESS)
    name = crud.employee_display_name(db, task.assignee_employee_id)
    tw.notify(db, task, [tw.reporting_manager_id(db, task)], title=f"{name} started a task",
              body=f"{task.title} ({task.code}) is now In Progress.", heading="Task Started",
              intro=f"{name} started working on \"{task.title}\".", key=f"started:{h.id}",
              actor=current_user, email=False)
    db.commit()
    return detail_out(db, current_user, task_id)


@router.post("/{task_id}/submit", response_model=schemas.TaskCardDetailOut)
def submit_task(  # sync def -> threadpool (blocking file/DB I/O)
    task_id: uuid.UUID,
    description: str = Form(...),
    reviewer_employee_id: str | None = Form(default=None),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    task = tw.lock_task(db, current_user, task_id)
    if "assignee" not in tw.roles(db, current_user, task):
        raise HTTPException(status_code=403, detail="Only the assigned employee can submit this task.")
    if task.workflow_status not in (tw.IN_PROGRESS, tw.CHANGES_REQUESTED):
        msg = ("Start the task before submitting it." if task.workflow_status == tw.ASSIGNED
               else f"This task is already {tw.STATUS_LABELS.get(task.workflow_status, task.workflow_status)}.")
        raise HTTPException(status_code=409, detail=msg)
    text = (description or "").strip()
    if len(text) < _DESCRIPTION_MIN:
        raise HTTPException(status_code=422, detail=f"Describe the completed work (at least {_DESCRIPTION_MIN} characters).")
    if len(text) > _DESCRIPTION_MAX:
        raise HTTPException(status_code=422, detail=f"The completion description can be at most {_DESCRIPTION_MAX} characters.")
    files = [f for f in files if f is not None and (f.filename or "").strip()]
    if len(files) > tw.MAX_FILES_PER_SUBMISSION:
        raise HTTPException(status_code=422, detail=f"Attach at most {tw.MAX_FILES_PER_SUBMISSION} files.")
    for f in files:  # all-or-nothing: check every file type before storing any
        ext = pathlib.Path(f.filename).suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"File type '{ext or 'unknown'}' is not allowed "
                                f"({f.filename}). Allowed types: {', '.join(sorted(_ALLOWED_EXTENSIONS))}")
    reviewer_uuid = None
    if reviewer_employee_id and reviewer_employee_id.strip():
        try:
            reviewer_uuid = uuid.UUID(reviewer_employee_id.strip())
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid reviewer.")
    tw.require_reviewer(db, task, reviewer_uuid)
    rm = tw.reporting_manager_id(db, task)
    if reviewer_uuid is None and rm is None:
        raise HTTPException(status_code=422, detail="Choose a reviewer — you have no Reporting Manager to review this task.")
    if tw.open_submission(db, task.id) is not None:
        raise HTTPException(status_code=409, detail="This task is already waiting for review.")

    now = tw.now_utc()
    rnd = (db.scalar(select(func.max(models.TaskSubmission.round)).where(models.TaskSubmission.task_id == task.id)) or 0) + 1
    sub = models.TaskSubmission(
        id=uuid.uuid4(), company_id=task.company_id, task_id=task.id, round=rnd, description=text,
        submitted_by=current_user.employee_id, submitted_at=now, reviewer_employee_id=reviewer_uuid)
    db.add(sub)
    db.flush()
    for f in files:
        file_url, size = save_uploaded_file(f, entity_type=tw.SUBMISSION_ENTITY_TYPE, entity_id=sub.id)
        crud.create_attachment(db, company_id=task.company_id, entity_type=tw.SUBMISSION_ENTITY_TYPE,
                               entity_id=sub.id, file_name=f.filename or "file", file_url=file_url,
                               mime_type=f.content_type, size_bytes=size)
    before = task.workflow_status
    tw.set_status(task, tw.SUBMITTED)
    task.submitted_at = now
    task.reviewer_employee_id = reviewer_uuid
    task.progress_pct = max(task.progress_pct or 0, 90)
    tw.record(db, task, current_user, "submitted", before, tw.SUBMITTED,
              f"Round {rnd} · {len(files)} file(s)" + (f" · reviewer {crud.employee_display_name(db, reviewer_uuid)}" if reviewer_uuid else ""))
    name = crud.employee_display_name(db, task.assignee_employee_id)
    common = dict(heading="Task Submitted for Review", key=f"submitted:{sub.id}", actor=current_user,
                  extra={"Submitted": tw.fmt_deadline(now), "Files": str(len(files))})
    tw.notify(db, task, [reviewer_uuid], title=f"{name} asked you to review a task",
              body=f"{task.title} ({task.code}) is ready for your review.",
              intro=f"{name} completed \"{task.title}\" and chose you to review it.", **common)
    tw.notify(db, task, [rm], title=f"Task submitted for review by {name}",
              body=f"{task.title} ({task.code}) is ready for review.",
              intro=f"{name} completed \"{task.title}\" and submitted it for review. As their Reporting Manager you can review it.",
              **common)
    db.commit()
    return detail_out(db, current_user, task_id)


@router.patch("/{task_id}/reviewer", response_model=schemas.TaskCardDetailOut)
def change_reviewer(
    task_id: uuid.UUID,
    payload: schemas.TaskReviewerChange,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    task = tw.lock_task(db, current_user, task_id)
    if "assignee" not in tw.roles(db, current_user, task):
        raise HTTPException(status_code=403, detail="Only the assigned employee can change the reviewer.")
    if task.workflow_status != tw.SUBMITTED:
        raise HTTPException(status_code=409, detail="The reviewer can only be changed while the task is waiting for review.")
    tw.require_reviewer(db, task, payload.reviewer_employee_id)
    if payload.reviewer_employee_id is None and tw.reporting_manager_id(db, task) is None:
        raise HTTPException(status_code=422, detail="Choose a reviewer — you have no Reporting Manager to review this task.")
    if payload.reviewer_employee_id == task.reviewer_employee_id:
        raise HTTPException(status_code=400, detail="That is already the reviewer.")
    sub = tw.open_submission(db, task.id)
    task.reviewer_employee_id = payload.reviewer_employee_id
    if sub is not None:
        sub.reviewer_employee_id = payload.reviewer_employee_id
    new = crud.employee_display_name(db, payload.reviewer_employee_id) if payload.reviewer_employee_id else "Reporting Manager only"
    h = tw.record(db, task, current_user, "reviewer_changed", task.workflow_status, task.workflow_status,
                  f"Reviewer: {new}")
    name = crud.employee_display_name(db, task.assignee_employee_id)
    tw.notify(db, task, [payload.reviewer_employee_id], title=f"{name} asked you to review a task",
              body=f"{task.title} ({task.code}) is ready for your review.", heading="Task Submitted for Review",
              intro=f"{name} completed \"{task.title}\" and chose you to review it.",
              key=f"reviewer:{h.id}", actor=current_user)
    db.commit()
    return detail_out(db, current_user, task_id)


@router.post("/{task_id}/review", response_model=schemas.TaskCardDetailOut)
def review_task(
    task_id: uuid.UUID,
    payload: schemas.TaskReviewDecision,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    task = tw.lock_task(db, current_user, task_id)
    if task.workflow_status != tw.SUBMITTED:
        raise HTTPException(status_code=409, detail=f"This task is not waiting for review (it is {tw.STATUS_LABELS.get(task.workflow_status, task.workflow_status)}).")
    if not tw.can_review(db, current_user, task):
        raise HTTPException(status_code=403, detail="Only the selected reviewer or the employee's Reporting Manager can review this task.")
    comments = (payload.comments or "").strip() or None
    if payload.decision == "changes_requested" and (comments is None or len(comments) < 3):
        raise HTTPException(status_code=422, detail="Add comments explaining what needs to change.")
    sub = tw.open_submission(db, task.id)
    if sub is None:
        raise HTTPException(status_code=409, detail="There is no submission waiting for review.")
    now = tw.now_utc()
    approved = payload.decision == "approve"
    sub.decision = "approved" if approved else "changes_requested"
    sub.review_comments = comments
    sub.reviewed_by = current_user.employee_id
    sub.reviewed_at = now
    before = task.workflow_status
    if approved:
        tw.set_status(task, tw.COMPLETED)
        task.completed_at = now
        task.completed_by = current_user.employee_id
        task.progress_pct = 100
        task.actual_end_date = now
    else:
        tw.set_status(task, tw.CHANGES_REQUESTED)
    h = tw.record(db, task, current_user, "approved" if approved else "changes_requested", before,
                  task.workflow_status, comments)
    reviewer_name = crud.employee_display_name(db, current_user.employee_id)
    name = crud.employee_display_name(db, task.assignee_employee_id)
    extra = {"Reviewed By": reviewer_name, **({"Comments": comments} if comments else {})}
    if approved:
        tw.notify(db, task, [task.assignee_employee_id], title="Your task was approved and completed",
                  body=f"{reviewer_name} approved {task.title} ({task.code}). It is now Completed.",
                  heading="Task Completed", intro=f"Hi {name}, {reviewer_name} reviewed and approved \"{task.title}\". The task is now Completed.",
                  key=f"approved:{h.id}", actor=current_user, extra=extra, decided=True)
        # The RM and everyone who reviewed any round (not just this one).
        tw.notify(db, task, [tw.reporting_manager_id(db, task), sub.reviewer_employee_id,
                             *sorted(tw._past_reviewer_ids(db, task.id), key=str)],
                  title=f"Task completed: {task.title}", body=f"{reviewer_name} approved {name}'s task {task.code}.",
                  heading="Task Completed", intro=f"{reviewer_name} approved {name}'s task \"{task.title}\".",
                  key=f"approved-others:{h.id}", actor=current_user, extra=extra, decided=True)
    else:
        tw.notify(db, task, [task.assignee_employee_id], title="Changes requested on your task",
                  body=f"{reviewer_name}: {comments}", heading="Changes Requested",
                  intro=f"Hi {name}, {reviewer_name} reviewed \"{task.title}\" and sent it back for corrections.",
                  key=f"changes:{h.id}", actor=current_user, extra=extra, decided=True)
        tw.notify(db, task, [tw.reporting_manager_id(db, task), sub.reviewer_employee_id],
                  title=f"Changes requested: {task.title}", body=f"{reviewer_name} sent {name}'s task {task.code} back.",
                  heading="Changes Requested", intro=f"{reviewer_name} sent {name}'s task \"{task.title}\" back for corrections.",
                  key=f"changes-others:{h.id}", actor=current_user, extra=extra, decided=True)
    db.commit()
    return detail_out(db, current_user, task_id)
