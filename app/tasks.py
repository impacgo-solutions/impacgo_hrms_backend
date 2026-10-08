"""Task Management review workflow (db/add_task_review_workflow.sql).

A task assigned to one employee goes

    Assigned -> In Progress -> Submitted for Review -> Completed
                    ^                    |
                    +-- Changes Requested <-+

Only a review (by the reviewer the assignee chose, or the assignee's
Reporting Manager) completes it. Everyone in the company sees the board
summary (title, assignee, assigned date, deadline, status); only the
assignee, their Reporting Manager and the task's reviewer(s) may open the
full Task Details (description, submissions, files, history).

An unassigned task (workflow_status NULL) keeps the old behaviour: anyone
in the company may open it and the board's "Move to" works freely.

pm_tasks.status (the Kanban column, CHECK-constrained) follows the
workflow status via PM_STATUS, so the board always shows where the task is.
"""

import datetime
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crud, models

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

ASSIGNED = "assigned"
IN_PROGRESS = "in_progress"
SUBMITTED = "submitted"
CHANGES_REQUESTED = "changes_requested"
COMPLETED = "completed"

STATUS_LABELS = {
    ASSIGNED: "Assigned",
    IN_PROGRESS: "In Progress",
    SUBMITTED: "Submitted for Review",
    CHANGES_REQUESTED: "Changes Requested",
    COMPLETED: "Completed",
}
# Workflow status -> pm_tasks.status (Kanban column).
PM_STATUS = {
    ASSIGNED: "todo",
    IN_PROGRESS: "in_progress",
    SUBMITTED: "in_review",
    CHANGES_REQUESTED: "in_progress",
    COMPLETED: "done",
}
_INACTIVE_EMPLOYEE_STATUSES = {"exited", "terminated", "resigned", "inactive", "relieved", "absconded"}

ENTITY_TYPE = "task"
SUBMISSION_ENTITY_TYPE = "task_submission"
MAX_FILES_PER_SUBMISSION = 10

DENIED_DETAIL = (
    "Only the assigned employee, their Reporting Manager and the task's reviewer "
    "can open this task's details."
)


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def today_ist() -> datetime.date:
    return datetime.datetime.now(IST).date()


def as_aware(dt: datetime.datetime) -> datetime.datetime:
    """A deadline without a timezone is the tenant's local (IST) time."""
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt


def fmt_deadline(dt: datetime.datetime | None) -> str:
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p") if dt else "—"


def is_overdue(task: models.TaskBoardCard) -> bool:
    return bool(task.deadline_at and task.workflow_status and task.workflow_status != COMPLETED
                and task.deadline_at < now_utc())


# ── people ──────────────────────────────────────────────────────────────────

def active_employee(db: Session, company_id: uuid.UUID, employee_id: uuid.UUID | None) -> models.Employee | None:
    """The employee if they are an active member of this company, else None
    (a cross-tenant / exited id is indistinguishable from a missing one)."""
    if employee_id is None:
        return None
    e = db.get(models.Employee, employee_id)
    if e is None or e.company_id != company_id or not e.is_active:
        return None
    if (e.status or "").strip().lower() in _INACTIVE_EMPLOYEE_STATUSES:
        return None
    return e


def reporting_manager_id(db: Session, task: models.TaskBoardCard) -> uuid.UUID | None:
    assignee = db.get(models.Employee, task.assignee_employee_id) if task.assignee_employee_id else None
    return assignee.reporting_manager_id if assignee else None


def _past_reviewer_ids(db: Session, task_id: uuid.UUID) -> set[uuid.UUID]:
    return {r for r in db.scalars(select(models.TaskSubmission.reviewer_employee_id)
                                  .where(models.TaskSubmission.task_id == task_id)) if r}


def roles(db: Session, user: models.User, task: models.TaskBoardCard) -> set[str]:
    """How the caller relates to an assigned task: assignee / manager (the
    assignee's Reporting Manager) / reviewer (current) / past_reviewer."""
    me = user.employee_id
    if me is None or task.company_id != user.company_id or task.assignee_employee_id is None:
        return set()
    out: set[str] = set()
    if me == task.assignee_employee_id:
        out.add("assignee")
    if me == reporting_manager_id(db, task):
        out.add("manager")
    if me == task.reviewer_employee_id:
        out.add("reviewer")
    elif me in _past_reviewer_ids(db, task.id):
        out.add("past_reviewer")
    return out


def can_view_details(db: Session, user: models.User, task: models.TaskBoardCard) -> bool:
    if task.company_id != user.company_id:
        return False
    if task.workflow_status is None:
        return True  # unassigned task: open to the company, as before
    return bool(roles(db, user, task))


def can_view_details_bulk(db: Session, user: models.User, tasks: list[models.TaskBoardCard]) -> dict[uuid.UUID, bool]:
    """can_view_details for a whole board in three queries."""
    me = user.employee_id
    assignee_ids = {t.assignee_employee_id for t in tasks if t.assignee_employee_id}
    managers = dict(db.execute(select(models.Employee.id, models.Employee.reporting_manager_id)
                               .where(models.Employee.id.in_(assignee_ids))).all()) if assignee_ids else {}
    reviewed = set(db.scalars(select(models.TaskSubmission.task_id).where(
        models.TaskSubmission.reviewer_employee_id == me))) if me else set()
    out = {}
    for t in tasks:
        if t.workflow_status is None:
            out[t.id] = True
        elif me is None:
            out[t.id] = False
        else:
            out[t.id] = me in (t.assignee_employee_id, t.reviewer_employee_id, managers.get(t.assignee_employee_id)) \
                or t.id in reviewed
    return out


def require_view(db: Session, user: models.User, task: models.TaskBoardCard | None) -> models.TaskBoardCard:
    if task is None or task.company_id != user.company_id:
        raise HTTPException(status_code=404, detail="Task not found")
    if not can_view_details(db, user, task):
        raise HTTPException(status_code=403, detail=DENIED_DETAIL)
    return task


def can_review(db: Session, user: models.User, task: models.TaskBoardCard) -> bool:
    """The reviewer the assignee chose, or the assignee's Reporting Manager
    -- never the assignee themself."""
    if task.workflow_status != SUBMITTED or user.employee_id is None:
        return False
    r = roles(db, user, task)
    return "assignee" not in r and bool(r & {"reviewer", "manager"})


def eligible_reviewers(db: Session, task: models.TaskBoardCard) -> list[models.Employee]:
    """Active employees of the task's company other than the assignee."""
    rows = db.scalars(select(models.Employee).where(
        models.Employee.company_id == task.company_id, models.Employee.is_active.is_(True),
        models.Employee.id != task.assignee_employee_id,
    ).order_by(models.Employee.first_name, models.Employee.last_name)).all()
    return [e for e in rows if (e.status or "").strip().lower() not in _INACTIVE_EMPLOYEE_STATUSES]


def require_reviewer(db: Session, task: models.TaskBoardCard, reviewer_id: uuid.UUID | None) -> models.Employee | None:
    if reviewer_id is None:
        return None
    if reviewer_id == task.assignee_employee_id:
        raise HTTPException(status_code=422, detail="You can't choose yourself as the reviewer.")
    reviewer = active_employee(db, task.company_id, reviewer_id)
    if reviewer is None:
        raise HTTPException(status_code=422, detail="The selected reviewer is not an active employee of this company.")
    return reviewer


# ── state ───────────────────────────────────────────────────────────────────

def lock_task(db: Session, user: models.User, task_id: uuid.UUID) -> models.TaskBoardCard:
    task = db.scalar(select(models.TaskBoardCard).where(models.TaskBoardCard.id == task_id).with_for_update())
    return require_view(db, user, task)


def set_status(task: models.TaskBoardCard, status: str) -> None:
    task.workflow_status = status
    task.status = PM_STATUS[status]


def record(db: Session, task: models.TaskBoardCard, user: models.User | None, action: str,
           from_status: str | None, to_status: str | None, comments: str | None = None) -> models.TaskHistory:
    row = models.TaskHistory(
        id=uuid.uuid4(), company_id=task.company_id, task_id=task.id, action=action,
        from_status=from_status, to_status=to_status,
        actor_user_id=user.id if user else None, actor_employee_id=user.employee_id if user else None,
        comments=comments, created_at=now_utc())
    db.add(row)
    db.flush()
    if user is not None:
        crud.create_audit_log(db, task.company_id, user.id, action[:20], "task_board_card", task.id,
                              changes={"from": from_status, "to": to_status})
    return row


def open_submission(db: Session, task_id: uuid.UUID) -> models.TaskSubmission | None:
    return db.scalar(select(models.TaskSubmission).where(
        models.TaskSubmission.task_id == task_id, models.TaskSubmission.decision.is_(None)))


def validate_schedule(assigned_date: datetime.date | None, deadline_at: datetime.datetime | None,
                      *, deadline_changed: bool, assigned_changed: bool) -> datetime.datetime | None:
    """Assigned Date + Deadline rules. Both are required for an assigned task;
    the assigned date can't be in the future; the deadline must be after the
    start of the assigned date and -- when it is being set -- in the future."""
    if assigned_date is None:
        raise HTTPException(status_code=422, detail="Assigned Date is required when the task is assigned to an employee.")
    if deadline_at is None:
        raise HTTPException(status_code=422, detail="Deadline is required when the task is assigned to an employee.")
    if assigned_changed and assigned_date > today_ist():
        raise HTTPException(status_code=422, detail="Assigned Date can't be in the future.")
    deadline = as_aware(deadline_at)
    if deadline <= datetime.datetime.combine(assigned_date, datetime.time.min, IST):
        raise HTTPException(status_code=422, detail="Deadline must be after the Assigned Date.")
    if deadline_changed and deadline <= now_utc():
        raise HTTPException(status_code=422, detail="Deadline must be in the future.")
    return deadline


def assign(db: Session, task: models.TaskBoardCard, user: models.User, assignee_id: uuid.UUID,
           assigned_date: datetime.date | None, deadline_at: datetime.datetime | None,
           *, deadline_changed: bool = True, assigned_changed: bool = True) -> bool:
    """Assign (or re-assign) [task]. Returns True when the assignee changed
    (the workflow restarts at Assigned and the new assignee is notified)."""
    if active_employee(db, task.company_id, assignee_id) is None:
        raise HTTPException(status_code=422, detail="The selected assignee is not an active employee of this company.")
    deadline = validate_schedule(assigned_date, deadline_at, deadline_changed=deadline_changed,
                                 assigned_changed=assigned_changed)
    task.assigned_date = assigned_date
    task.deadline_at = deadline
    if task.assignee_employee_id == assignee_id and task.workflow_status is not None:
        return False
    if task.workflow_status == COMPLETED or (task.workflow_status is None and task.status in ("done", "cancelled")):
        raise HTTPException(status_code=409, detail="A completed task can't be assigned to someone else.")
    if task.workflow_status == SUBMITTED:
        raise HTTPException(status_code=409, detail="This task is waiting for review — review it before re-assigning.")
    before = task.workflow_status
    reassign = task.assignee_employee_id is not None
    task.assignee_employee_id = assignee_id
    task.reviewer_employee_id = None
    task.submitted_at = None
    set_status(task, ASSIGNED)
    record(db, task, user, "reassigned" if reassign else "assigned", before, ASSIGNED,
           f"Assigned to {crud.employee_display_name(db, assignee_id)}")
    return True


# ── notifications (in-app + email) ─────────────────────────────────────────

def _details(db: Session, task: models.TaskBoardCard, extra: dict[str, str] | None = None) -> dict[str, str]:
    project = db.get(models.Project, task.project_id)
    d = {
        "Task": task.title,
        "Task ID": task.code,
        "Project": project.name if project else "—",
        "Assigned To": crud.employee_display_name(db, task.assignee_employee_id),
        "Assigned Date": crud._fmt_req_date(task.assigned_date),
        "Deadline": fmt_deadline(task.deadline_at),
        "Status": STATUS_LABELS.get(task.workflow_status or "", "—"),
    }
    if task.reviewer_employee_id:
        d["Reviewer"] = crud.employee_display_name(db, task.reviewer_employee_id)
    d.update(extra or {})
    return d


def notify(db: Session, task: models.TaskBoardCard, recipients: list[uuid.UUID | None], *, title: str, body: str,
           heading: str, intro: str, key: str, actor: models.User | None, email: bool = True,
           extra: dict[str, str] | None = None, decided: bool = False) -> None:
    """In-app notification (live push after commit) + email to each distinct
    recipient employee, skipping the person who acted. Idempotent per
    [key] + recipient, so a retry never emails twice."""
    from . import email_service
    actor_emp = actor.employee_id if actor else None
    actor_name = crud.employee_display_name(db, actor_emp) if actor_emp else None
    details = _details(db, task, extra)
    for emp_id in dict.fromkeys(r for r in recipients if r and r != actor_emp):
        crud.create_notification(db, task.company_id, crud.get_user_id_for_employee(db, emp_id), title, body,
                                 entity_type=ENTITY_TYPE, entity_id=task.id)
        if not email:
            continue
        emp = db.get(models.Employee, emp_id)
        email_service.send_request_email(
            emp.work_email if emp else None, subject=f"{title} - {task.title}", heading=heading, intro_line=intro,
            details=details, entity_type=ENTITY_TYPE, entity_id=task.id, from_display_name=actor_name,
            cta_label="Open Task", db=db, company_id=task.company_id,
            email_type=email_service.REQUEST_DECIDED if decided else email_service.REQUEST_SUBMITTED,
            idempotency_key=f"TASK:{key}")


def notify_assigned(db: Session, task: models.TaskBoardCard, user: models.User) -> None:
    name = crud.employee_display_name(db, task.assignee_employee_id)
    deadline = fmt_deadline(task.deadline_at)
    hist = db.scalar(select(models.TaskHistory.id).where(models.TaskHistory.task_id == task.id)
                     .order_by(models.TaskHistory.created_at.desc()).limit(1))
    notify(db, task, [task.assignee_employee_id], title="New task assigned to you",
           body=f"{task.title} ({task.code}) — deadline {deadline}.",
           heading="A Task Has Been Assigned to You",
           intro=f"Hi {name}, you have been assigned the task \"{task.title}\". Please complete it by {deadline}.",
           key=f"assigned:{task.id}:{hist}", actor=user)
    notify(db, task, [reporting_manager_id(db, task)], title=f"Task assigned to {name}",
           body=f"{task.title} ({task.code}) — deadline {deadline}.",
           heading="Task Assigned to Your Team Member",
           intro=f"{name} has been assigned the task \"{task.title}\", due {deadline}.",
           key=f"assigned-rm:{task.id}:{hist}", actor=user)


# ── Task Details payload ───────────────────────────────────────────────────

def can_edit(db: Session, user: models.User) -> bool:
    """Projects Edit/Admin (or Owner) -- the existing task-edit gate."""
    from .rbac_columns import BUILTIN_ROLES
    if any(r.name == BUILTIN_ROLES[0] for r in crud.get_user_roles(db, user.id)):
        return True
    return crud.effective_user_matrix(db, user).get("projects_access") in ("e", "a")


def enrich_detail(db: Session, user: models.User, task: models.TaskBoardCard, detail: dict) -> dict:
    """Adds the review workflow (assignee, schedule, reviewer, submissions
    with files, history, what the caller may do) to
    crud.get_task_board_card_detail's payload."""
    names = crud.employee_display_names_bulk
    subs = db.scalars(select(models.TaskSubmission).where(models.TaskSubmission.task_id == task.id)
                      .order_by(models.TaskSubmission.round.desc())).all()
    hist = db.scalars(select(models.TaskHistory).where(models.TaskHistory.task_id == task.id)
                      .order_by(models.TaskHistory.created_at.desc())).all()
    assignee = db.get(models.Employee, task.assignee_employee_id) if task.assignee_employee_id else None
    rm_id = assignee.reporting_manager_id if assignee else None
    ids = {task.assignee_employee_id, task.reviewer_employee_id, task.completed_by, rm_id}
    for s in subs:
        ids |= {s.submitted_by, s.reviewer_employee_id, s.reviewed_by}
    for h in hist:
        ids.add(h.actor_employee_id)
    n = names(db, ids)
    files: dict[uuid.UUID, list[models.Attachment]] = {}
    if subs:
        for a in db.scalars(select(models.Attachment).where(
                models.Attachment.entity_type == SUBMISSION_ENTITY_TYPE,
                models.Attachment.entity_id.in_([s.id for s in subs])).order_by(models.Attachment.file_name)):
            files.setdefault(a.entity_id, []).append(a)
    my = roles(db, user, task)
    status = task.workflow_status
    detail.update(
        assignee=n.get(task.assignee_employee_id, "Unassigned") if task.assignee_employee_id else "Unassigned",
        assignee_employee_id=task.assignee_employee_id,
        assignee_code=assignee.employee_code if assignee else None,
        reporting_manager_id=rm_id,
        reporting_manager=n.get(rm_id) if rm_id else None,
        assigned_date=task.assigned_date,
        deadline_at=task.deadline_at,
        workflow_status=status,
        status_label=STATUS_LABELS.get(status or ""),
        is_overdue=is_overdue(task),
        reviewer_employee_id=task.reviewer_employee_id,
        reviewer=n.get(task.reviewer_employee_id) if task.reviewer_employee_id else None,
        submitted_at=task.submitted_at,
        completed_at=task.completed_at,
        completed_by=n.get(task.completed_by) if task.completed_by else None,
        my_roles=sorted(my),
        permissions=dict(
            can_start="assignee" in my and status in (ASSIGNED, CHANGES_REQUESTED),
            can_submit="assignee" in my and status in (IN_PROGRESS, CHANGES_REQUESTED),
            can_change_reviewer="assignee" in my and status == SUBMITTED,
            can_review=can_review(db, user, task),
            can_edit=can_edit(db, user) and status != COMPLETED,
        ),
        submissions=[dict(
            id=s.id, round=s.round, description=s.description, submitted_by=n.get(s.submitted_by, "—"),
            submitted_at=s.submitted_at, reviewer_employee_id=s.reviewer_employee_id,
            reviewer=n.get(s.reviewer_employee_id) if s.reviewer_employee_id else None,
            decision=s.decision, review_comments=s.review_comments,
            reviewed_by=n.get(s.reviewed_by) if s.reviewed_by else None, reviewed_at=s.reviewed_at,
            files=[dict(id=a.id, file_name=a.file_name, file_url=a.file_url, mime_type=a.mime_type,
                        size_bytes=a.size_bytes) for a in files.get(s.id, [])],
        ) for s in subs],
        history=[dict(
            id=h.id, action=h.action, from_status=h.from_status, to_status=h.to_status,
            from_label=STATUS_LABELS.get(h.from_status or ""), to_label=STATUS_LABELS.get(h.to_status or ""),
            actor=n.get(h.actor_employee_id, "System") if h.actor_employee_id else "System",
            comments=h.comments, created_at=h.created_at,
        ) for h in hist],
    )
    return detail
