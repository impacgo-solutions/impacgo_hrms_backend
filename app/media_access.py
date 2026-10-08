"""SEC-04 / C10: who may download an uploaded file under /media/.

Uploads live at uploads/<entity_type>/<entity_id>/<file> (storage.py) with
no tenant segment, so "same tenant" is established by finding the owning
record in the caller's tenant schema + company. Anything not recognised is
denied.

  employee_photo/<employee_id>     any authenticated user of the same company
  employee_document/<employee_id>  self, Owner, or document visibility
                                   (crud.get_visible_employee_ids_for_docs)
  leave_request/<leave_id>         requester, anyone who may decide it
                                   (reporting chain / Owner, crud.can_decide_request),
                                   leave_approval Admin (HR), or doc visibility
  work_entry/<work_entry_id>       owner, deciders, timesheet_approval Admin,
                                   or doc visibility
  project/<project_id>             same company + Projects access (not 'n')
  task_submission/<submission_id>  the task's assignee, their Reporting Manager, its
                                   reviewer(s) (tasks.can_view_details)
  document_template/<company_id>   same company
  company_logo/<company_id>        same company
  policy_document/<company_id>     same company
"""

import uuid

from sqlalchemy.orm import Session

from . import crud, models
from .rbac_columns import BUILTIN_ROLES

NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"

# Headers on every /media file response (main.serve_uploaded_file). L-07:
# nosniff stops a browser from content-sniffing an upload into something
# executable (e.g. HTML saved as .png rendered as a page); the type always
# comes from the stored extension. Uploaded content is additionally checked
# against its extension at save time (storage.save_uploaded_file).
MEDIA_RESPONSE_HEADERS = {
    "Cache-Control": "private, max-age=300",
    "X-Content-Type-Options": "nosniff",
}


def _is_owner(db: Session, user: models.User) -> bool:
    return any(r.name == BUILTIN_ROLES[0] for r in crud.get_user_roles(db, user.id))


def _column_level(db: Session, user: models.User, column: str) -> str:
    # The person's effective levels (role + grants + Employee Permissions),
    # the same view every API check uses.
    return crud.effective_user_matrix(db, user).get(column, "n")


def _can_see_employee_records(db: Session, user: models.User, employee_id: uuid.UUID) -> bool:
    if user.employee_id == employee_id or _is_owner(db, user):
        return True
    visible = crud.get_visible_employee_ids_for_docs(db, user)
    return visible is None or employee_id in visible


def authorize(db: Session, user: models.User, rel_path: str) -> str | None:
    """None if `user` may read /media/<rel_path>, else NOT_FOUND/FORBIDDEN."""
    parts = rel_path.split("/")
    if len(parts) < 3:
        return NOT_FOUND
    entity_type = parts[0]
    try:
        entity_id = uuid.UUID(parts[1])
    except ValueError:
        return NOT_FOUND

    if entity_type in ("document_template", "policy_document", "company_logo"):
        return None if entity_id == user.company_id else NOT_FOUND

    if entity_type in ("employee_photo", "employee_document"):
        employee = db.get(models.Employee, entity_id)
        if employee is None or employee.company_id != user.company_id:
            return NOT_FOUND
        if entity_type == "employee_photo":
            return None
        return None if _can_see_employee_records(db, user, entity_id) else FORBIDDEN

    if entity_type == "leave_request":
        leave = db.get(models.LeaveRequest, entity_id)
        if leave is None or leave.company_id != user.company_id:
            return NOT_FOUND
        if (
            _can_see_employee_records(db, user, leave.employee_id)
            or crud.can_decide_request(db, user, leave.employee_id)
            or _column_level(db, user, "leave_approval") == "a"
        ):
            return None
        return FORBIDDEN

    if entity_type == "work_entry":
        entry = db.get(models.WorkEntry, entity_id)
        timesheet = entry.timesheet if entry is not None else None
        if timesheet is None or timesheet.company_id != user.company_id:
            return NOT_FOUND
        if (
            _can_see_employee_records(db, user, timesheet.employee_id)
            or crud.can_decide_request(db, user, timesheet.employee_id)
            or _column_level(db, user, "timesheet_approval") == "a"
        ):
            return None
        return FORBIDDEN

    if entity_type == "learning_document":
        # Shared Learning material: every employee of the same company, while
        # the document exists (deleted -> gone) and Learning is enabled.
        doc = db.get(models.LearningDocument, entity_id)
        if doc is None or doc.company_id != user.company_id or doc.is_deleted:
            return NOT_FOUND
        return None if crud.is_module_enabled(db, user.company_id, "learning") else NOT_FOUND

    if entity_type == "task_submission":
        from . import tasks
        sub = db.get(models.TaskSubmission, entity_id)
        task = db.get(models.TaskBoardCard, sub.task_id) if sub is not None else None
        if task is None or task.company_id != user.company_id:
            return NOT_FOUND
        return None if tasks.can_view_details(db, user, task) else FORBIDDEN

    if entity_type == "project":
        project = db.get(models.Project, entity_id)
        if project is None or project.company_id != user.company_id:
            return NOT_FOUND
        if _is_owner(db, user) or _column_level(db, user, "projects_access") != "n":
            return None
        return FORBIDDEN

    return NOT_FOUND
