import calendar
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud, models, overtime, schemas, work_rules
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled, require_permission
from ..storage import save_uploaded_file

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api", tags=["work"],
    dependencies=[Depends(require_module_enabled("work"))],
)

_WORK_ENTRY_ENTITY_TYPE = "work_entry"


def _work_entry_out(
    db: Session,
    entry: models.WorkEntry,
    names: dict[uuid.UUID, str] | None = None,
) -> schemas.WorkEntryOut:
    """names, when given, is a bulk-resolved lookup for a whole result set
    (see list_work_entries) -- performance only, same output either way."""
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    employee_id = entry.timesheet.employee_id if entry.timesheet else None
    return schemas.WorkEntryOut(
        id=entry.id,
        employee_id=employee_id,
        entry_date=entry.entry_date,
        project=entry.project.name if entry.project else None,
        # task_name is the free-text title the user typed (schemas.WorkEntryCreate.task);
        # fall back to the linked task-card's title for older rows saved
        # before task_name existed.
        task=entry.task_name or (entry.task.title if entry.task else None),
        task_id=entry.task_id,
        category=entry.category,
        start_time=entry.start_time,
        end_time=entry.end_time,
        hours=float(entry.hours),
        description=entry.description,
        is_billable=entry.is_billable,
        status=entry.status,
        approver_id=entry.approver_id,
        approver_name=name(entry.approver_id),
        decision_notes=entry.decision_notes,
        decided_at=entry.decided_at,
    )


@router.get("/work-entries", response_model=list[schemas.WorkEntryOut])
def list_work_entries(
    status: str | None = None,
    # Optional -- omitted (the default) returns every visible entry
    # regardless of date, unchanged from before these were added, so no
    # existing caller's behavior changes just by this becoming available.
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    rows = crud.list_work_entries(
        db, company.id, status=status, employee_ids=visible_ids,
        from_date=from_date, to_date=to_date, limit=limit, offset=offset,
    )
    approver_ids = {e.approver_id for e in rows if e.approver_id}
    names = crud.employee_display_names_bulk(db, approver_ids)
    return [_work_entry_out(db, e, names=names) for e in rows]


@router.post("/work-entries", response_model=schemas.WorkEntryOut, status_code=201)
def create_work_entry(
    payload: schemas.WorkEntryCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs Add Work Entry — always the logged-in user (self-service)."""
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "timesheet_approval", "work_entry", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    # No Reporting Manager requirement here -- Work Entry no longer goes
    # through an approval workflow at all (see update_work_entry's
    # docstring), so unlike leave/regularization/overtime there's no
    # decision step that would otherwise have nobody to route to.
    try:
        crud.validate_work_entry_date(db, company.id, payload.entry_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # project_id (a real, dynamically-fetched selection) is preferred over
    # the legacy name lookup -- resolving by name is fragile (rename,
    # casing, or a stale client-side list that no longer matches a real
    # row) and, unlike the id path, isn't guaranteed to reject a project
    # from a different company by construction.
    if payload.project_id is not None:
        project = db.get(models.Project, payload.project_id)
        if project is not None and project.company_id != company.id:
            project = None
    elif payload.project_name:
        project = crud.get_project_by_name(db, company.id, payload.project_name)
    else:
        project = None
    if project is None:
        raise HTTPException(status_code=400, detail="A valid project is required for work entries.")

    task_id = payload.task_id
    if task_id is not None:
        task = db.get(models.TaskBoardCard, task_id)
        if task is None or task.project_id != project.id:
            raise HTTPException(
                status_code=400,
                detail="Selected task does not belong to this project.",
            )
    # M-25: only on a project the employee is allocated to that day.
    if (err := work_rules.allocation_error(db, employee.id, project, payload.entry_date)):
        raise HTTPException(status_code=403, detail=err)
    # M-24: approved / rejected weeks are locked.
    if (err := work_rules.locked_week_error(db, employee.id, payload.entry_date)):
        raise HTTPException(status_code=409, detail=err)
    # L-16 / M-20: valid time window, no duplicate / overlap, <= 24 h a day.
    crud._advisory_lock(db, "work_entry_day", str(employee.id), payload.entry_date.isoformat())
    if (err := work_rules.entry_error(
            db, employee.id, payload.entry_date, project_id=project.id, task_id=task_id,
            task_name=payload.task, start_time=payload.start_time, end_time=payload.end_time,
            hours=payload.hours)):
        raise HTTPException(status_code=400, detail=err)

    # Find or create timesheet for the week containing entry_date
    import datetime as _dt
    week_start = payload.entry_date - _dt.timedelta(days=payload.entry_date.weekday())
    from sqlalchemy import select as _select
    timesheet = db.scalar(
        _select(models.Timesheet).where(
            models.Timesheet.employee_id == employee.id,
            models.Timesheet.week_start == week_start,
        )
    )
    if timesheet is None:
        try:
            # status="draft" -- logging a single Work Entry must not put a
            # timesheet in front of the Reporting Manager before the
            # employee has actually clicked Submit Timesheet (see
            # crud.create_timesheet / crud.submit_timesheet).
            timesheet = crud.create_timesheet(
                db, company.id, employee.id, week_start, 0.0, 0.0, status="draft"
            )
        except ValueError:
            db.rollback()
            timesheet = db.scalar(
                _select(models.Timesheet).where(
                    models.Timesheet.employee_id == employee.id,
                    models.Timesheet.week_start == week_start,
                )
            )

    entry = crud.create_work_entry(
        db,
        timesheet.id,
        project.id,
        payload.entry_date,
        category=payload.category,
        start_time=payload.start_time,
        end_time=payload.end_time,
        hours=payload.hours,
        description=payload.description,
        is_billable=payload.is_billable,
        task_id=task_id,
        task_name=payload.task,
    )
    crud.create_audit_log(db, company.id, current_user.id, "create", "work_entry", entry.id)
    # Keeps the week's Timesheet total/billable hours immediately accurate
    # -- a no-op for a brand-new 'draft' row (GET already recomputes those
    # live for draft, see _timesheet_out), but essential when this entry
    # lands in a week whose Timesheet was already submitted/"approved":
    # without this, the Weekly Timesheet view would keep showing the old
    # total until the employee happened to re-Submit that week. Same
    # resync edit_work_entry/delete_work_entry already do.
    crud.resync_timesheet_hours(db, employee.id, week_start)
    db.commit()
    db.refresh(entry)
    crud.notify_new_request(db, company.id, employee, "Work Entry", "work_entry", entry.id)
    db.commit()
    return _work_entry_out(db, entry)


@router.patch("/work-entries/{work_entry_id}", response_model=schemas.WorkEntryOut)
def update_work_entry(
    work_entry_id: uuid.UUID,
    payload: schemas.WorkEntryUpdate,
    db: Session = Depends(get_db),
    # Work Entry no longer goes through Reporting Manager approval (see
    # create_work_entry's docstring) -- entries are approved immediately on
    # creation. This endpoint is now an ADMIN OVERRIDE/management tool only
    # (require_permission, not a peer/manager decision), for the rare case
    # an authorized admin needs to correct or flag an entry after the fact.
    current_user: models.User = Depends(require_permission("timesheet_approval")),
):
    entry = crud.get_work_entry_for_update(db, work_entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Work entry not found")
    # pm_time_entries has no employee_id column of its own -- the employee
    # is only reachable via its timesheet (see _work_entry_out's identical
    # resolution).
    requester_employee_id = entry.timesheet.employee_id if entry.timesheet else None
    if (
        requester_employee_id is not None
        and current_user.employee_id is not None
        and current_user.employee_id == requester_employee_id
    ):
        raise HTTPException(
            status_code=403, detail="You may not override your own work entry"
        )
    # WF-06/E5: the first human decision on an (auto-approved) entry needs no
    # reason; flipping an entry an admin has ALREADY decided requires one,
    # which is kept in the audit log and sent to the employee.
    is_override = entry.decided_at is not None
    if is_override:
        _refuse_other_managers_override(
            db, requester_employee_id, entry.approver_id, entry.status, current_user.employee_id
        )
    reason = _require_override_reason(is_override, payload.decision_notes)
    previous_status = entry.status
    # pm_time_entries' status CHECK only allows pending/approved/rejected --
    # writing "sent_back" raw was a CheckViolation 500 (FUNC-04). "Send Back
    # for Revision" returns the entry to the employee as "pending" (no
    # longer approved, so it drops out of approved-hours totals); the
    # employee's own edit (PATCH /work-entries/{id}/edit) resubmits it,
    # which re-applies the create-time auto-approval. Mirrors
    # update_timesheet mapping sent_back -> draft.
    effective_status = _WORK_ENTRY_STATUS_MAP[payload.status]
    entry.status = effective_status
    entry.approver_id = current_user.employee_id
    entry.decision_notes = payload.decision_notes
    entry.decided_at = datetime.now(timezone.utc)
    company = db.get(models.Company, current_user.company_id)
    crud.create_audit_log(
        db, company.id, current_user.id, payload.status, "work_entry", entry.id,
        changes={
            "before": previous_status,
            "after": effective_status,
            "decision": payload.status,
            "override": is_override,
            "reason": reason,
        },
    )
    db.commit()
    db.refresh(entry)
    if requester_employee_id is not None:
        crud.notify_decision(
            db, company.id, requester_employee_id, "Work Entry",
            payload.status, _notification_notes(is_override, payload.decision_notes),
            "work_entry", entry.id,
        )
        db.commit()
    return _work_entry_out(db, entry)


# Decision value (WorkEntryUpdate.status) -> value allowed by the
# pm_time_entries_status_check constraint (pending/approved/rejected).
_WORK_ENTRY_STATUS_MAP = {"approved": "approved", "rejected": "rejected", "sent_back": "pending"}
# Decision value (TimesheetUpdate.status) -> value allowed by the
# pm_timesheets_status_check constraint (draft/submitted/approved/rejected).
_TIMESHEET_STATUS_MAP = {"approved": "approved", "rejected": "rejected", "sent_back": "draft"}


def _refuse_other_managers_override(
    db: Session,
    requester_employee_id: uuid.UUID | None,
    decided_by: uuid.UUID | None,
    decided_status: str,
    actor_employee_id: uuid.UUID | None,
) -> None:
    """Two Reporting Managers: once one of the employee's two managers
    (reporting / dotted-line) has decided a work entry or timesheet, the
    OTHER one cannot overturn it -- the same "first decision is final" rule
    every approval doctype enforces (see update_overtime_request). The WF-06
    override itself is unchanged for anyone else: the deciding manager can
    still correct their own decision, and an admin who is not one of the
    employee's managers can still override with a reason."""
    if requester_employee_id is None or decided_by is None or actor_employee_id is None:
        return
    requester = db.get(models.Employee, requester_employee_id)
    if requester is None:
        return
    managers = {requester.reporting_manager_id, requester.dotted_line_manager_id} - {None}
    if decided_by in managers and actor_employee_id in managers and decided_by != actor_employee_id:
        decider = crud.employee_display_name(db, decided_by)
        decider_type = crud.decided_by_manager_type(requester, decided_by)
        # A Send Back is stored as pending (work entry) / draft (timesheet).
        verb = {"pending": "sent back", "draft": "sent back"}.get(decided_status, decided_status)
        raise HTTPException(
            status_code=409,
            detail=f"This request was already {verb} by {decider} ({decider_type}) and cannot be decided again",
        )


def _require_override_reason(is_override: bool, decision_notes: str | None) -> str | None:
    """WF-06: an admin override of an already-decided work entry/timesheet
    must carry a non-empty reason (decision_notes)."""
    reason = (decision_notes or "").strip() or None
    if is_override and reason is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "This entry has already been decided. A reason (decision_notes) "
                "is required to override an existing decision."
            ),
        )
    return reason


def _notification_notes(is_override: bool, decision_notes: str | None) -> str | None:
    if is_override:
        return f"Decision overridden by an administrator. Reason: {decision_notes.strip()}"
    return decision_notes


@router.post(
    "/work-entries/{work_entry_id}/attachments",
    response_model=schemas.AttachmentOut,
    status_code=201,
)
def upload_work_entry_attachment(  # API-06: sync def -> threadpool (blocking file/DB I/O)
    work_entry_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs Add Work Entry's optional Documentation upload — called after
    the work entry itself is created, once its id is known."""
    # M-23: only the entry's owner may attach files (was: anyone).
    _require_own_work_entry(db, work_entry_id, current_user)
    company = db.get(models.Company, current_user.company_id)
    file_url, size_bytes = save_uploaded_file(
        file, entity_type=_WORK_ENTRY_ENTITY_TYPE, entity_id=work_entry_id
    )
    attachment = crud.create_attachment(
        db,
        company_id=company.id,
        entity_type=_WORK_ENTRY_ENTITY_TYPE,
        entity_id=work_entry_id,
        file_name=file.filename or "file",
        file_url=file_url,
        mime_type=file.content_type,
        size_bytes=size_bytes,
    )
    crud.create_audit_log(
        db, company.id, current_user.id, "create", "work_entry_attachment", attachment.id
    )
    db.commit()
    db.refresh(attachment)
    return schemas.AttachmentOut(
        id=attachment.id,
        file_name=attachment.file_name,
        file_url=attachment.file_url,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
    )


@router.get(
    "/work-entries/{work_entry_id}/attachments",
    response_model=list[schemas.AttachmentOut],
)
def list_work_entry_attachments(
    work_entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # M-23: the owner, their manager chain, or an admin who can see the
    # owner's work entries (same visibility as GET /work-entries).
    entry = db.scalar(
        select(models.WorkEntry)
        .join(models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
        .where(models.WorkEntry.id == work_entry_id, models.Timesheet.company_id == current_user.company_id)
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Work entry not found")
    owner_id = entry.timesheet.employee_id
    if owner_id != current_user.employee_id:
        visible = crud.get_visible_employee_ids_for_docs(db, current_user)
        if visible is not None and owner_id not in visible:
            raise HTTPException(status_code=403, detail="You can't view this work entry's attachments")
    return [
        schemas.AttachmentOut(
            id=a.id,
            file_name=a.file_name,
            file_url=a.file_url,
            mime_type=a.mime_type,
            size_bytes=a.size_bytes,
        )
        for a in crud.list_attachments_for_entity(db, _WORK_ENTRY_ENTITY_TYPE, work_entry_id)
    ]


def _require_own_work_entry(
    db: Session, work_entry_id: uuid.UUID, current_user: models.User
) -> models.WorkEntry:
    """Shared ownership guard for edit/delete below -- pm_time_entries has
    no employee_id column of its own (see _work_entry_out's identical
    resolution), so ownership is only reachable via entry.timesheet.
    Raises the same 404/403 either endpoint would need."""
    entry = crud.get_work_entry_for_update(db, work_entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Work entry not found")
    owner_employee_id = entry.timesheet.employee_id if entry.timesheet else None
    if owner_employee_id is None or current_user.employee_id != owner_employee_id:
        raise HTTPException(
            status_code=403, detail="You can only manage your own work entries"
        )
    return entry


@router.patch("/work-entries/{work_entry_id}/edit", response_model=schemas.WorkEntryOut)
def edit_work_entry(
    work_entry_id: uuid.UUID,
    payload: schemas.WorkEntryFieldsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Employee self-edit of their OWN Work Entry's field values -- lets an
    employee correct/update a previously submitted entry any time (subject
    to the same Organization Owner-configured backdate window new entries
    use, see crud.validate_work_entry_date). Distinct from update_work_entry
    above, which is an admin-only status override, not a field edit."""
    entry = _require_own_work_entry(db, work_entry_id, current_user)
    company = db.get(models.Company, current_user.company_id)

    new_entry_date = payload.entry_date or entry.entry_date
    try:
        crud.validate_work_entry_date(db, company.id, new_entry_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project = None
    if payload.project_id is not None:
        project = db.get(models.Project, payload.project_id)
        if project is not None and project.company_id != company.id:
            project = None
        if project is None:
            raise HTTPException(status_code=400, detail="A valid project is required for work entries.")
    elif payload.project_name:
        project = crud.get_project_by_name(db, company.id, payload.project_name)
        if project is None:
            raise HTTPException(status_code=400, detail="A valid project is required for work entries.")

    if payload.task_id is not None:
        target_project_id = project.id if project is not None else entry.project_id
        task = db.get(models.TaskBoardCard, payload.task_id)
        if task is None or task.project_id != target_project_id:
            raise HTTPException(
                status_code=400, detail="Selected task does not belong to this project."
            )

    owner_employee_id = entry.timesheet.employee_id
    old_week_start = entry.timesheet.week_start
    new_week_start = new_entry_date - timedelta(days=new_entry_date.weekday())

    # M-24: neither the entry's current week nor its new week may be locked.
    for day in {entry.entry_date, new_entry_date}:
        if (err := work_rules.locked_week_error(db, owner_employee_id, day)):
            raise HTTPException(status_code=409, detail=err)
    target_project = project or db.get(models.Project, entry.project_id)
    # M-25: still allocated to the (possibly new) project on the (new) date.
    if (project is not None or payload.entry_date is not None) and target_project is not None:
        if (err := work_rules.allocation_error(db, owner_employee_id, target_project, new_entry_date)):
            raise HTTPException(status_code=403, detail=err)
    # L-16 / M-20 on the merged values (the entry itself excluded).
    crud._advisory_lock(db, "work_entry_day", str(owner_employee_id), new_entry_date.isoformat())
    if (err := work_rules.entry_error(
            db, owner_employee_id, new_entry_date, project_id=target_project.id if target_project else entry.project_id,
            task_id=payload.task_id if payload.task_id is not None else entry.task_id,
            task_name=payload.task if payload.task is not None else entry.task_name,
            start_time=payload.start_time if payload.start_time is not None else entry.start_time,
            end_time=payload.end_time if payload.end_time is not None else entry.end_time,
            hours=payload.hours if payload.hours is not None else float(entry.hours),
            exclude_id=entry.id)):
        raise HTTPException(status_code=400, detail=err)

    if project is not None:
        entry.project_id = project.id
    if payload.task is not None:
        entry.task_name = payload.task
    if payload.task_id is not None:
        entry.task_id = payload.task_id
    if payload.category is not None:
        entry.category = payload.category
    if payload.start_time is not None:
        entry.start_time = payload.start_time
    if payload.end_time is not None:
        entry.end_time = payload.end_time
    if payload.hours is not None:
        entry.hours = payload.hours
    if payload.description is not None:
        entry.description = payload.description
    if payload.is_billable is not None:
        entry.is_billable = payload.is_billable
    entry.entry_date = new_entry_date
    # An entry an admin "sent back for revision" is stored as "pending"
    # (see update_work_entry). The employee's edit IS the resubmission: it
    # re-applies the same auto-approval create_work_entry uses and clears
    # the old decision, so the next admin decision is a first decision
    # again (no override reason needed).
    resubmitted = entry.status == "pending" and entry.decided_at is not None
    if resubmitted:
        entry.status = "approved"
        entry.approver_id = None
        entry.decided_at = None
        entry.decision_notes = None

    # Re-link to the correct week's Timesheet if the date moved to a
    # different week -- find-or-create, same as create_work_entry's own
    # auto-vivify path, so an edited entry always sums into the timesheet
    # that actually matches its (possibly new) date.
    if new_week_start != old_week_start:
        target_timesheet = db.scalar(
            select(models.Timesheet).where(
                models.Timesheet.employee_id == owner_employee_id,
                models.Timesheet.week_start == new_week_start,
            )
        )
        if target_timesheet is None:
            target_timesheet = crud.create_timesheet(
                db, company.id, owner_employee_id, new_week_start, 0.0, 0.0, status="draft",
            )
        entry.timesheet_id = target_timesheet.id

    db.flush()
    crud.create_audit_log(
        db, company.id, current_user.id, "resubmit" if resubmitted else "update",
        "work_entry", entry.id,
    )
    # Keeps both the entry's old and (possibly new) week's stored Timesheet
    # totals accurate immediately -- see resync_timesheet_hours's docstring
    # for why this can't be skipped once Timesheets are approved on submit.
    crud.resync_timesheet_hours(db, owner_employee_id, old_week_start)
    if new_week_start != old_week_start:
        crud.resync_timesheet_hours(db, owner_employee_id, new_week_start)
    db.commit()
    db.refresh(entry)
    return _work_entry_out(db, entry)


@router.delete("/work-entries/{work_entry_id}", status_code=204)
def delete_work_entry(
    work_entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Employee self-delete of their OWN Work Entry -- e.g. one created by
    mistake or no longer needed. Same ownership guard as edit_work_entry;
    an admin wanting to remove someone else's entry isn't in scope here
    (only the owning employee may delete their own record, per spec)."""
    entry = _require_own_work_entry(db, work_entry_id, current_user)
    company = db.get(models.Company, current_user.company_id)
    owner_employee_id = entry.timesheet.employee_id
    week_start = entry.timesheet.week_start
    if (err := work_rules.locked_week_error(db, owner_employee_id, entry.entry_date)):  # M-24
        raise HTTPException(status_code=409, detail=err)
    entry_id = entry.id
    crud.create_audit_log(db, company.id, current_user.id, "delete", "work_entry", entry_id)
    db.delete(entry)
    db.flush()
    crud.resync_timesheet_hours(db, owner_employee_id, week_start)
    db.commit()


def _timesheet_out(
    db: Session,
    t: models.Timesheet,
    names: dict[uuid.UUID, str] | None = None,
    hours: dict | None = None,
) -> schemas.TimesheetOut:
    # A draft row's stored total_hours/billable_hours are only as fresh as
    # the last time something wrote them (0/0 at auto-vivify time, see
    # create_work_entry) -- recompute live from Work Entries so the Work &
    # Timesheet list always reflects reality while still a draft. Once
    # submitted/approved/rejected, the stored value is the frozen snapshot
    # taken at submission time and is intentionally left alone here.
    if t.status == "draft" and hours is not None:  # PERF-09: bulk-computed by the list endpoint
        total_hours, billable_hours = hours.get((t.employee_id, t.week_start), (0.0, 0.0))
    elif t.status == "draft":
        total_hours, billable_hours = crud.compute_timesheet_hours(db, t.employee_id, t.week_start)
    else:
        total_hours, billable_hours = float(t.total_hours), float(t.billable_hours)

    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    return schemas.TimesheetOut(
        id=t.id,
        employee_id=t.employee_id,
        week_start=t.week_start,
        status=t.status,
        total_hours=total_hours,
        billable_hours=billable_hours,
        approver_id=t.approver_id,
        approver_name=name(t.approver_id),
        decision_notes=t.decision_notes,
        decided_at=t.decided_at,
        submitted_at=t.submitted_at,
    )


@router.get("/timesheets", response_model=list[schemas.TimesheetOut])
def list_timesheets(
    status: str | None = None,
    # Optional -- omitted (the default) returns every visible timesheet
    # regardless of date, unchanged from before these were added. A
    # timesheet whose week only PARTIALLY overlaps [from_date, to_date] is
    # still included (see crud.list_timesheets), not dropped at the edge.
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    rows = crud.list_timesheets(
        db, company.id, status=status, employee_ids=visible_ids,
        from_date=from_date, to_date=to_date, limit=limit, offset=offset,
    )
    approver_ids = {t.approver_id for t in rows if t.approver_id}
    names = crud.employee_display_names_bulk(db, approver_ids)
    hours = crud.compute_timesheet_hours_bulk(
        db, [(t.employee_id, t.week_start) for t in rows if t.status == "draft"]
    )
    return [
        _timesheet_out(db, t, names=names, hours=hours)
        for t in rows
    ]


@router.post(
    "/timesheets",
    response_model=schemas.TimesheetOut,
    status_code=201,
)
def create_timesheet(
    payload: schemas.TimesheetCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Submit Timesheet. Total Hours/Billable Hours are never taken from
    the request body -- crud.submit_timesheet always computes them live
    from the employee's actual Work Entries for this week
    (crud.compute_timesheet_hours). If Add Work Entry already
    auto-vivified a draft row for this week (the common case), this
    finalizes that same row instead of raising a spurious "already
    exists" conflict."""
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "timesheet_approval", "timesheet", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    # No Reporting Manager requirement here -- Timesheet no longer goes
    # through an approval workflow at all (see update_timesheet's
    # docstring).
    # M-22: a week always starts on Monday (any other day used to create an
    # overlapping duplicate timesheet).
    week_start = work_rules.week_start_of(payload.week_start)
    try:
        crud.validate_work_entry_date(db, company.id, week_start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # L-17: an empty week can't be submitted.
    total_hours, _billable = crud.compute_timesheet_hours(db, payload.employee_id, week_start)
    if total_hours <= 0:
        raise HTTPException(status_code=400, detail="Log at least one work entry for this week before submitting it.")
    try:
        timesheet = crud.submit_timesheet(
            db, company.id, payload.employee_id, week_start,
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "timesheet", timesheet.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(timesheet)
    crud.notify_new_request(db, company.id, employee, "Timesheet", "timesheet", timesheet.id)
    db.commit()
    return _timesheet_out(db, timesheet)


@router.get("/timesheets/hours-preview", response_model=schemas.TimesheetHoursOut)
def preview_timesheet_hours(
    employee_id: uuid.UUID,
    week_start: date,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Submit Timesheet's Total Hours/Billable Hours fields -- read-only,
    populated live from whatever Work Entries already exist for this
    employee/week (usually logged via Add Work Entry earlier in the week),
    recomputed every time the dialog's Week Start changes. Same
    computation submit_timesheet itself uses, so what the employee sees
    here is exactly what gets stored on submit."""
    # L-18: self, the manager chain, or an admin who may see that employee's
    # work entries (same visibility as GET /work-entries).
    if employee_id != current_user.employee_id:
        target = db.get(models.Employee, employee_id)
        if target is None or target.company_id != current_user.company_id:
            raise HTTPException(status_code=404, detail="Employee not found")
        visible = crud.get_visible_employee_ids_for_docs(db, current_user)
        if visible is not None and employee_id not in visible:
            raise HTTPException(status_code=403, detail="You can't view this employee's hours")
    week_start = work_rules.week_start_of(week_start)  # M-22
    total_hours, billable_hours = crud.compute_timesheet_hours(db, employee_id, week_start)
    return schemas.TimesheetHoursOut(total_hours=total_hours, billable_hours=billable_hours)


@router.patch(
    "/timesheets/{timesheet_id}",
    response_model=schemas.TimesheetOut,
)
def update_timesheet(
    timesheet_id: uuid.UUID,
    payload: schemas.TimesheetUpdate,
    db: Session = Depends(get_db),
    # Timesheet no longer goes through Reporting Manager approval (see
    # create_timesheet's docstring) -- submissions are approved immediately.
    # This endpoint is now an ADMIN OVERRIDE/management tool only
    # (require_permission, not a peer/manager decision), for the rare case
    # an authorized admin needs to correct or flag a timesheet afterward.
    current_user: models.User = Depends(require_permission("timesheet_approval")),
):
    company = db.get(models.Company, current_user.company_id)
    timesheet = crud.get_timesheet_for_update(db, timesheet_id)
    if timesheet is None:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    if (
        current_user.employee_id is not None
        and current_user.employee_id == timesheet.employee_id
    ):
        raise HTTPException(
            status_code=403, detail="You may not override your own timesheet submission"
        )
    # pm_timesheets' own status CHECK constraint only allows
    # draft/submitted/approved/rejected -- unlike every sibling doctype it
    # has no "sent_back" state. "Send Back for Revision" (still a valid
    # WorkEntryUpdate/TimesheetUpdate payload value) is represented here by
    # reverting the timesheet to "draft" so the employee can edit and
    # resubmit it.
    # WF-06: an override = an admin decision already recorded AFTER the
    # latest submission (a resubmission after "sent back" starts fresh).
    is_override = timesheet.decided_at is not None and (
        timesheet.submitted_at is None or timesheet.decided_at >= timesheet.submitted_at
    )
    if is_override:
        _refuse_other_managers_override(
            db, timesheet.employee_id, timesheet.approver_id, timesheet.status, current_user.employee_id
        )
    reason = _require_override_reason(is_override, payload.decision_notes)
    previous_status = timesheet.status
    effective_status = _TIMESHEET_STATUS_MAP[payload.status]
    timesheet.status = effective_status
    timesheet.approver_id = current_user.employee_id
    timesheet.decision_notes = payload.decision_notes
    timesheet.decided_at = datetime.now(timezone.utc)
    crud.create_audit_log(
        db, company.id, current_user.id, payload.status, "timesheet", timesheet.id,
        changes={
            "before": previous_status,
            "after": effective_status,
            "decision": payload.status,
            "override": is_override,
            "reason": reason,
        },
    )
    db.commit()
    db.refresh(timesheet)
    crud.notify_decision(
        db, company.id, timesheet.employee_id, "Timesheet",
        # "draft" has no verb -- notify with the decision ("sent back for
        # revision") rather than the stored value.
        payload.status if payload.status == "sent_back" else effective_status,
        _notification_notes(is_override, payload.decision_notes), "timesheet", timesheet.id,
    )
    db.commit()
    return _timesheet_out(db, timesheet)


class TimesheetUnlockIn(BaseModel):
    decision_notes: str | None = Field(default=None, max_length=2000)


@router.post("/timesheets/{timesheet_id}/unlock", response_model=schemas.TimesheetOut)
def unlock_timesheet(
    timesheet_id: uuid.UUID,
    payload: TimesheetUnlockIn | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("timesheet_approval")),
):
    """M-24: an approved / rejected week is locked (no new, edited or
    deleted work entries). An administrator (Timesheet Approval edit/admin,
    for an employee they can see -- never their own week) unlocks it: the
    week goes back to draft so the employee can correct and re-submit."""
    company = db.get(models.Company, current_user.company_id)
    timesheet = crud.get_timesheet_for_update(db, timesheet_id, company.id)
    if timesheet is None:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    if current_user.employee_id is not None and current_user.employee_id == timesheet.employee_id:
        raise HTTPException(status_code=403, detail="You may not unlock your own timesheet")
    visible = crud.get_visible_employee_ids_for_docs(db, current_user)
    if visible is not None and timesheet.employee_id not in visible:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    if timesheet.status not in work_rules.LOCKED_TIMESHEET_STATUSES:
        raise HTTPException(status_code=409, detail=f"This week isn't locked (it is {timesheet.status}).")
    previous = timesheet.status
    notes = (payload.decision_notes or "").strip() or None if payload else None
    timesheet.status = "draft"
    timesheet.approver_id = current_user.employee_id
    timesheet.decision_notes = notes
    timesheet.decided_at = datetime.now(timezone.utc)
    crud.create_audit_log(db, company.id, current_user.id, "unlock", "timesheet", timesheet.id,
                          changes={"before": previous, "after": "draft", "reason": notes})
    db.commit()
    db.refresh(timesheet)
    crud.notify_decision(db, company.id, timesheet.employee_id, "Timesheet", "unlocked", notes,
                         "timesheet", timesheet.id)
    db.commit()
    return _timesheet_out(db, timesheet)


@router.delete("/timesheets/{timesheet_id}", status_code=204)
def delete_timesheet(
    timesheet_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Employee self-delete of their OWN Timesheet -- e.g. a
    create_work_entry-auto-vivified draft row created by mistake, or a
    week the employee no longer wants tracked. Blocked with 409 if the
    timesheet still has Work Entries logged against it (pm_time_entries.
    timesheet_id has no ON DELETE CASCADE, so deleting it would orphan
    those rows) -- delete or move the entries first via
    delete_work_entry/edit_work_entry, both already self-service."""
    timesheet = crud.get_timesheet_for_update(db, timesheet_id)
    if timesheet is None:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    if (
        current_user.employee_id is None
        or current_user.employee_id != timesheet.employee_id
    ):
        raise HTTPException(status_code=403, detail="You can only delete your own timesheets")
    has_entries = db.scalar(
        select(models.WorkEntry.id)
        .where(models.WorkEntry.timesheet_id == timesheet_id)
        .limit(1)
    )
    if has_entries is not None:
        raise HTTPException(
            status_code=409,
            detail="This timesheet still has Work Entries logged against it -- delete or move those first.",
        )
    company = db.get(models.Company, current_user.company_id)
    crud.create_audit_log(db, company.id, current_user.id, "delete", "timesheet", timesheet_id)
    db.delete(timesheet)
    db.commit()


def _compensation_out(db: Session, c: "models.OvertimeCompensation | None") -> schemas.OvertimeCompensationOut | None:
    if c is None:
        return None
    leave_type = db.get(models.LeaveType, c.leave_type_id) if c.leave_type_id else None
    period = None
    if c.applied_payroll_run_id:
        run = db.get(models.PayrollRun, c.applied_payroll_run_id)
        if run is not None:
            period = f"{calendar.month_name[run.period_month]} {run.period_year}"
    elif c.target_period_month and c.status == "pending_payroll":
        period = f"{calendar.month_name[c.target_period_month]} {c.target_period_year} or next run"
    return schemas.OvertimeCompensationOut(
        mode=c.mode, status=c.status, worked_minutes=c.worked_minutes, counted_minutes=c.counted_minutes,
        hourly_rate=float(c.hourly_rate) if c.hourly_rate is not None else None,
        rate_multiplier=float(c.rate_multiplier) if c.rate_multiplier is not None else None,
        amount=float(c.amount) if c.amount is not None else None,
        leave_type_name=leave_type.name if leave_type else None,
        leave_days=float(c.leave_days) if c.leave_days is not None else None,
        payroll_period=period, calculation=c.calculation,
    )


def _overtime_request_out(
    db: Session,
    r: models.OvertimeRequest,
    names: dict[uuid.UUID, str] | None = None,
    compensations: "dict[uuid.UUID, models.OvertimeCompensation] | None" = None,
    viewer: "models.User | None" = None,
) -> schemas.OvertimeRequestOut:
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    if compensations is not None:
        compensation = compensations.get(r.id)
    else:
        compensation = db.scalar(select(models.OvertimeCompensation).where(
            models.OvertimeCompensation.overtime_request_id == r.id))
    state = overtime.session_state(r)
    now = datetime.now(timezone.utc)
    is_self = viewer is not None and viewer.employee_id is not None and viewer.employee_id == r.employee_id
    can_out = state in (overtime.CLOCKED_IN, overtime.MISSED_CLOCK_OUT)
    can_end = bool(
        viewer is not None and can_out and viewer.employee_id is not None
        and viewer.employee_id in (r.employee_id, r.approver_id)
    )
    return schemas.OvertimeRequestOut(
        start_time=r.start_time,
        end_time=overtime.end_time_of(db, r),
        planned_start=r.planned_start,
        planned_end=r.planned_end,
        session_status=state,
        can_clock_in=bool(is_self and r.status == "approved" and state in (overtime.SCHEDULED, overtime.MISSED_CLOCK_IN)
                          and r.planned_start is not None and r.planned_start <= now < r.planned_end),
        can_clock_out=bool(is_self and can_out),
        missed_clock_in_at=r.missed_in_notified_at,
        missed_clock_out_at=r.missed_out_notified_at,
        actual_start=r.actual_start,
        actual_end=r.actual_end,
        actual_minutes=r.actual_minutes,
        actual_duration=overtime.fmt_minutes(r.actual_minutes) if r.actual_minutes is not None else None,
        compensation=_compensation_out(db, compensation),
        can_end_early=can_end,
        id=r.id,
        employee_id=r.employee_id,
        work_date=r.work_date,
        hours=float(r.hours),
        approved_hours=float(r.hours) if r.status == "approved" else None,
        reason=r.reason,
        status=r.status,
        approver_id=r.approver_id,
        approver_name=name(r.approver_id),
        employee_name=name(r.employee_id),
        decision_notes=r.decision_notes,
        decided_at=r.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, r.employee_id), r.approver_id
        ),
    )


@router.get("/overtime-requests/day-context", response_model=schemas.OvertimeDayContextOut)
def overtime_day_context(
    work_date: date,
    employee_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The Request Overtime form shows this for the chosen date and blocks
    times that overlap the regular shift -- the same rule create / approve
    enforce (overtime.shift_overlap_error)."""
    emp_id = employee_id or current_user.employee_id
    employee = db.get(models.Employee, emp_id) if emp_id else None
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if emp_id != current_user.employee_id:
        visible = crud.get_visible_employee_ids_for_requests(db, current_user, "overtime_request")
        if visible is not None and emp_id not in visible:
            raise HTTPException(status_code=404, detail="Employee not found")
    ctx = overtime.day_context(db, current_user.company_id, employee, work_date)
    return schemas.OvertimeDayContextOut(
        work_date=work_date, working_day=ctx["working_day"], day_type=ctx["day_type"], reason=ctx["reason"],
        windows=[schemas.OvertimeShiftWindowOut(**w) for w in ctx["windows"]],
    )


@router.get("/overtime-requests", response_model=list[schemas.OvertimeRequestOut])
def list_overtime_requests(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    # Sessions start / end by the clock -- bring them up to date first so
    # every screen shows the real state (the scheduler does this too).
    if overtime.advance_sessions(db, company.id):
        db.commit()
    visible_ids = crud.get_visible_employee_ids_for_requests(db, current_user, "overtime_request")
    rows = crud.list_overtime_requests(
        db, company.id, employee_ids=visible_ids, limit=limit, offset=offset,
    )
    ids = {r.approver_id for r in rows if r.approver_id} | {r.employee_id for r in rows}
    names = crud.employee_display_names_bulk(db, ids)
    crud.preload_employees(db, {r.employee_id for r in rows})  # PERF-09 / P10: no per-row db.get
    compensations = {c.overtime_request_id: c for c in db.scalars(select(models.OvertimeCompensation).where(
        models.OvertimeCompensation.overtime_request_id.in_([r.id for r in rows])))} if rows else {}
    return [_overtime_request_out(db, r, names=names, compensations=compensations, viewer=current_user)
            for r in rows]


@router.post(
    "/overtime-requests",
    response_model=schemas.OvertimeRequestOut,
    status_code=201,
)
def create_overtime_request(
    payload: schemas.OvertimeRequestCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs Request Overtime — self-service, same Reporting Manager
    Approval Workflow gating as every other request-creation endpoint."""
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "timesheet_approval", "overtime_request", "create"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")

    # WF-03: a requester with no Reporting Manager (e.g. the CEO) is no
    # longer refused -- crud.notify_new_request routes the request to the
    # fallback approvers (Owner / System Settings RBAC holders) who can
    # decide it; self-approval stays blocked.
    # H-19: every request (start_time is now required) goes through the
    # enabled, past-window, overlap and shift checks -- the old start-less
    # path skipped them all and credited hours to future dates on approval.
    if employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    settings = overtime.get_settings(db, company.id)
    if not settings.enabled:
        raise HTTPException(status_code=409, detail="Overtime requests are turned off for this organization.")
    start, end = overtime.planned_window(db, company.id, payload.work_date, payload.start_time, payload.hours)
    if end <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="This overtime window has already passed -- "
                                                    "overtime is requested before it is worked.")
    # One employee cannot have two overlapping overtime windows.
    for other in db.scalars(select(models.OvertimeRequest).where(
            models.OvertimeRequest.employee_id == payload.employee_id,
            models.OvertimeRequest.status.in_(("pending", "approved", "sent_back")),
            models.OvertimeRequest.start_time.is_not(None))):
        o_start, o_end = other.planned_start, other.planned_end
        if o_start is None:
            o_start, o_end = overtime.planned_window(db, company.id, other.work_date, other.start_time, float(other.hours))
        if start < o_end and o_start < end:
            raise HTTPException(
                status_code=409,
                detail=f"This overlaps another overtime request on {other.work_date.strftime('%d %b %Y')} "
                       f"({other.status.replace('_', ' ')}).",
            )
    # Overtime is always outside the regular shift (holidays / weekly
    # offs have none, so the whole period is overtime there).
    clash = overtime.shift_overlap_error(db, company.id, employee, payload.work_date, start, end)
    if clash:
        raise HTTPException(status_code=409, detail=clash)
    request = crud.create_overtime_request(
        db, payload.employee_id, payload.work_date, payload.hours, payload.reason, payload.start_time,
        payload.end_time,
    )
    if request.start_time is not None:
        request.planned_start, request.planned_end = overtime.planned_window(
            db, company.id, request.work_date, request.start_time, float(request.hours))
    crud.create_audit_log(db, company.id, current_user.id, "create", "overtime_request", request.id)
    db.commit()
    db.refresh(request)
    crud.notify_new_request(db, company.id, employee, "Overtime Request", "overtime_request", request.id)
    db.commit()
    return _overtime_request_out(db, request, viewer=current_user)


@router.patch("/overtime-requests/{request_id}/edit", response_model=schemas.OvertimeRequestOut)
def edit_overtime_request(
    request_id: uuid.UUID,
    payload: schemas.OvertimeRequestEdit,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The employee edits their own PENDING / SENT-BACK request (date, Start
    / End Time, reason) -- same overlap and shift rules as create; a
    sent-back request goes back to pending for the same approver chain."""
    company = db.get(models.Company, current_user.company_id)
    request = crud.get_overtime_request_for_update(db, request_id)
    employee = db.get(models.Employee, request.employee_id) if request else None
    if request is None or employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Overtime request not found")
    if current_user.employee_id != request.employee_id:
        raise HTTPException(status_code=403, detail="You can only edit your own overtime request.")
    if request.status not in ("pending", "sent_back"):
        raise HTTPException(status_code=409, detail=f"Only a pending or sent-back request can be edited (this one is {request.status}).")
    if not overtime.get_settings(db, company.id).enabled:  # H-19
        raise HTTPException(status_code=409, detail="Overtime requests are turned off for this organization.")
    hours = overtime.hours_between(payload.start_time, payload.end_time)
    start, end = overtime.planned_window(db, company.id, payload.work_date, payload.start_time, hours)
    if end <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="This overtime window has already passed.")
    for other in db.scalars(select(models.OvertimeRequest).where(
            models.OvertimeRequest.employee_id == request.employee_id,
            models.OvertimeRequest.id != request.id,
            models.OvertimeRequest.status.in_(("pending", "approved", "sent_back")),
            models.OvertimeRequest.start_time.is_not(None))):
        o_start, o_end = other.planned_start, other.planned_end
        if o_start is None:
            o_start, o_end = overtime.planned_window(db, company.id, other.work_date, other.start_time, float(other.hours))
        if start < o_end and o_start < end:
            raise HTTPException(status_code=409, detail=f"This overlaps another overtime request on "
                                                        f"{other.work_date.strftime('%d %b %Y')} ({other.status.replace('_', ' ')}).")
    clash = overtime.shift_overlap_error(db, company.id, employee, payload.work_date, start, end)
    if clash:
        raise HTTPException(status_code=409, detail=clash)
    before = {"work_date": request.work_date.isoformat(), "start_time": str(request.start_time),
              "end_time": str(request.end_time), "hours": str(request.hours)}
    request.work_date, request.start_time, request.end_time = payload.work_date, payload.start_time, payload.end_time
    request.hours, request.planned_start, request.planned_end = hours, start, end
    request.reason = payload.reason
    resubmitted = request.status == "sent_back"
    if resubmitted:
        request.status, request.decided_at, request.decision_notes = "pending", None, None
    crud.create_audit_log(db, company.id, current_user.id, "resubmit" if resubmitted else "update", "overtime_request",
                          request.id, changes={"before": before, "after": {"work_date": payload.work_date.isoformat(),
                                                                         "start_time": str(payload.start_time),
                                                                         "end_time": str(payload.end_time), "hours": str(hours)}})
    db.commit()
    db.refresh(request)
    if resubmitted:
        crud.notify_new_request(db, company.id, employee, "Overtime Request", "overtime_request", request.id)
        db.commit()
    return _overtime_request_out(db, request, viewer=current_user)


def _own_session_for_punch(db: Session, request_id: uuid.UUID, current_user: models.User):
    company = db.get(models.Company, current_user.company_id)
    request = crud.get_overtime_request_for_update(db, request_id)
    employee = db.get(models.Employee, request.employee_id) if request else None
    if request is None or employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Overtime request not found")
    if current_user.employee_id != request.employee_id:
        raise HTTPException(status_code=403, detail="Only the employee can clock in / out of their own overtime.")
    return company, request, employee


@router.post("/overtime-requests/{request_id}/clock-in", response_model=schemas.OvertimeRequestOut)
def overtime_clock_in(
    request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Overtime Clock In -- from the approved start time; records the real
    start. Never happens automatically."""
    company, request, _employee = _own_session_for_punch(db, request_id, current_user)
    overtime.check_request(db, company.id, request)  # bring a missed state up to date first
    try:
        overtime.clock_in(db, company.id, request)
    except ValueError as exc:
        db.commit()  # keep a just-recorded missed-punch state / reminder
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "overtime_clock_in", "overtime_request", request.id,
                          changes={"actual_start": request.actual_start.isoformat()})
    db.commit()
    db.refresh(request)
    return _overtime_request_out(db, request, viewer=current_user)


@router.post("/overtime-requests/{request_id}/clock-out", response_model=schemas.OvertimeRequestOut)
def overtime_clock_out(
    request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Overtime Clock Out -- records the real end and the worked hours, then
    the compensation (up to the approved hours). Never automatic."""
    company, request, employee = _own_session_for_punch(db, request_id, current_user)
    try:
        overtime.clock_out(db, company.id, request, current_user.employee_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "overtime_clock_out", "overtime_request", request.id,
                          changes={"actual_end": request.actual_end.isoformat(), "actual_minutes": str(request.actual_minutes)})
    _notify_overtime_completed(db, company.id, employee, request)
    db.commit()
    db.refresh(request)
    return _overtime_request_out(db, request, viewer=current_user)


def _notify_overtime_completed(db: Session, company_id: uuid.UUID, employee: models.Employee,
                               request: models.OvertimeRequest) -> None:
    """Clock Out done: the reporting manager(s) (from the hierarchy, any role)
    get an in-app notification + email with the hours actually worked."""
    from .. import email_service
    tz = crud.company_tzinfo(db, company_id)
    name = f"{employee.first_name} {employee.last_name or ''}".strip()
    worked = overtime.fmt_minutes(request.actual_minutes)
    day = request.work_date.strftime("%d %b %Y")
    span = (f"{request.actual_start.astimezone(tz).strftime('%I:%M %p')} – "
            f"{request.actual_end.astimezone(tz).strftime('%I:%M %p')}")
    body = f"{name} clocked out of overtime on {day}: worked {worked} ({span}), approved {float(request.hours):g} h."
    for user_id, email, role in overtime.reminder_recipients(db, employee):
        if role != "manager":
            continue
        crud.create_notification(db, company_id, user_id, "Overtime completed", body,
                                 entity_type="overtime_request", entity_id=request.id)
        email_service.send_request_email(
            email, subject=f"Overtime completed — {name}, {day}", heading="Overtime completed", intro_line=body,
            details={"Employee": name, "Work Date": day, "Clocked In – Out": span, "Worked": worked,
                     "Approved Hours": f"{float(request.hours):g}"},
            entity_type="overtime_request", entity_id=request.id, db=db, company_id=company_id,
            email_type=email_service.GENERAL, idempotency_key=f"OVERTIME_COMPLETED:{request.id}",
        )


@router.post("/overtime-requests/{request_id}/end", response_model=schemas.OvertimeRequestOut)
def end_overtime_session(
    request_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Clocks a clocked-in overtime session out now: the employee
    themselves, or the manager who approved it (a manual action -- the same
    as Overtime Clock Out)."""
    company = db.get(models.Company, current_user.company_id)
    request = crud.get_overtime_request_for_update(db, request_id)
    employee = db.get(models.Employee, request.employee_id) if request else None
    if request is None or employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Overtime request not found")
    if current_user.employee_id not in (request.employee_id, request.approver_id):
        raise HTTPException(status_code=403, detail="Only the employee or the approving manager can end this session.")
    try:
        overtime.end_early(db, company.id, request, current_user.employee_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "end_session", "overtime_request", request.id,
                          changes={"actual_minutes": str(request.actual_minutes)})
    db.commit()
    db.refresh(request)
    return _overtime_request_out(db, request, viewer=current_user)


@router.patch("/overtime-requests/{request_id}", response_model=schemas.OvertimeRequestOut)
def update_overtime_request(
    request_id: uuid.UUID,
    payload: schemas.OvertimeRequestUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py.
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    request = crud.get_overtime_request_for_update(db, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Overtime request not found")
    # Two Reporting Managers: same atomic-with-lock guard as
    # update_work_entry/update_leave_request -- blocks a conflicting second
    # decision from the employee's other manager. Checked before the
    # permission gate so that manager sees a clear "already decided"
    # message instead of a confusing permission error once
    # can_decide_request_configurable (correctly) stops recognizing them
    # as the current decider.
    if request.status in ("approved", "rejected"):
        decider = crud.employee_display_name(db, request.approver_id)
        decider_type = crud.decided_by_manager_type(
            db.get(models.Employee, request.employee_id), request.approver_id
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"This request was already {request.status} by {decider}"
                f" ({decider_type}) and cannot be decided again"
                if decider_type
                else f"This request was already {request.status} and cannot be decided again"
            ),
        )
    if not crud.can_decide_request_configurable(
        db, current_user, request.employee_id, "overtime_request", request.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's assigned Reporting Manager can act on this request",
        )
    old_status = request.status
    if payload.status == "approved" and request.start_time is None:
        # H-19: a legacy request without a time window can't be approved
        # (approval used to credit its hours straight to attendance, even
        # for a future date, with no overlap / enabled checks).
        raise HTTPException(status_code=409, detail="This request has no start / end time -- send it back so the "
                                                    "employee can add the overtime window, then approve it.")
    if payload.status == "approved" and request.planned_start is not None:
        clash = overtime.shift_overlap_error(
            db, company.id, db.get(models.Employee, request.employee_id), request.work_date,
            request.planned_start, request.planned_end)
        if clash:
            raise HTTPException(status_code=409, detail=f"Can't approve: {clash}")
    effective_status = crud.decide_configurable_request(
        db, current_user, request.employee_id, "overtime_request", request.id,
        payload.status, payload.decision_notes,
    )
    request.status = effective_status
    request.approver_id = current_user.employee_id
    request.decision_notes = payload.decision_notes
    if effective_status != "pending":
        request.decided_at = datetime.now(timezone.utc)
    if old_status != "approved" and effective_status == "approved":
        # Session is SCHEDULED; the employee clocks in / out manually and
        # it is compensated once when they clock out.
        overtime.start_session_on_approval(db, company.id, request)
    crud.create_audit_log(db, company.id, current_user.id, payload.status, "overtime_request", request.id)
    db.commit()
    db.refresh(request)
    if effective_status != "pending":
        crud.notify_decision(
            db, company.id, request.employee_id, "Overtime Request",
            effective_status, payload.decision_notes, "overtime_request", request.id,
        )
        db.commit()
    return _overtime_request_out(db, request)
