"""Login access follows employment status.

An employee whose status is one of EXITED_STATUSES (or whose employee row is
is_active = false) must not be able to sign in or keep using a session:

  * sync_login_access -- called whenever an employee's status changes (and
    when an exit is approved with its last working day already reached):
    sets core_users.status / public.users.is_active to match, and on
    deactivation bumps public.users.token_version + drops the cached auth
    state, so every existing token is refused on its next request.
  * deactivate_due_exits -- the scheduled pass (app/reminders.py): exits
    approved / in clearance / completed whose last working day (company
    timezone) has been reached get the employee marked 'exited' and their
    access revoked. Once per exit request (an audit row is the marker), so
    an employee HR later reactivates is not locked out again.
  * login (routers/auth.py) and deps.authenticate_token also re-check the
    employee status directly, so access is refused even if a status was
    changed by some path that didn't sync.

Reactivation: setting the status back to an employed one (active,
probation, notice period, ...) re-enables the login; old tokens stay
invalid (token_version was bumped), so the person signs in again.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import auth_state, crud, models, ws_manager

logger = logging.getLogger(__name__)

# Statuses that end login access. Compared after normalize_status, so
# "Terminated", "Absconded " and "exited" all match.
EXITED_STATUSES = frozenset(
    {"inactive", "terminated", "exited", "resigned", "relieved", "absconded", "separated", "deactivated"}
)
# Exit request statuses after which the last working day revokes access.
_EXIT_DONE_STATUSES = ("approved", "in_clearance", "completed")
AUTO_DEACTIVATE_ACTION = "access_revoked"

ACCOUNT_INACTIVE_DETAIL = "Your account is inactive. Please contact your HR administrator."


def normalize_status(status: str | None) -> str:
    return (status or "").strip().lower().replace(" ", "_").replace("-", "_")


def is_exited_status(status: str | None) -> bool:
    return normalize_status(status) in EXITED_STATUSES


def employee_access_blocked(employee: models.Employee | None) -> bool:
    """True when this employee may not sign in / use the API. No employee row
    (platform/service logins) is not blocked here."""
    if employee is None:
        return False
    return employee.is_active is False or is_exited_status(employee.status)


def sync_login_access(db: Session, employee: models.Employee) -> str | None:
    """Brings the employee's login in line with their status. Returns
    'deactivated', 'reactivated' or None (nothing to change / no login).
    Caller commits.

    Deactivation always bumps token_version, even if the login was already
    disabled, so a session that somehow survived is ended too."""
    core_user = crud.get_core_user_by_employee_id(db, employee.id)
    if core_user is None:
        return None
    public_user_id = auth_state.public_user_id_for_core_user(db, core_user)
    if employee_access_blocked(employee):
        core_user.status = "inactive"
        if public_user_id is not None:
            db.execute(
                text("UPDATE public.users SET is_active = false WHERE id = :id"), {"id": public_user_id}
            )
            auth_state.bump_token_version(db, public_user_id)
        db.flush()
        auth_state.invalidate(public_user_id)
        ws_manager.queue_close(db, core_user.id)
        return "deactivated"
    changed = core_user.status != "active"
    core_user.status = "active"
    if public_user_id is not None:
        changed = bool(
            db.execute(
                text("UPDATE public.users SET is_active = true WHERE id = :id AND is_active IS NOT TRUE"),
                {"id": public_user_id},
            ).rowcount
        ) or changed
    db.flush()
    auth_state.invalidate(public_user_id)
    return "reactivated" if changed else None


def apply_exit(
    db: Session, company_id: uuid.UUID, exit_request: models.ExitRequestModel,
    actor_user_id: uuid.UUID | None = None,
) -> bool:
    """The employee behind an approved exit whose last working day has been
    reached: status -> 'exited' and login revoked, once per exit request.
    Returns True if it acted. Caller commits."""
    if exit_request.status not in _EXIT_DONE_STATUSES or exit_request.last_working_day is None:
        return False
    if exit_request.last_working_day > crud.company_today(db, company_id):
        return False
    if db.scalar(
        select(models.AuditLog.id).where(
            models.AuditLog.doctype == "exit_request",
            models.AuditLog.document_id == exit_request.id,
            models.AuditLog.action == AUTO_DEACTIVATE_ACTION,
        ).limit(1)
    ) is not None:
        return False
    employee = db.get(models.Employee, exit_request.employee_id)
    if employee is None or employee.company_id != company_id:
        return False
    before = employee.status
    if not is_exited_status(employee.status):
        employee.status = "exited"
    sync_login_access(db, employee)
    crud.create_audit_log(
        db, company_id, actor_user_id, AUTO_DEACTIVATE_ACTION, "exit_request", exit_request.id,
        changes={"employee_id": str(employee.id), "status_before": before, "status_after": employee.status,
                 "last_working_day": exit_request.last_working_day.isoformat()},
    )
    return True


def deactivate_due_exits(db: Session, company_id: uuid.UUID) -> int:
    """Scheduled pass: every exit in this company whose last working day has
    been reached and whose access hasn't been revoked yet. Returns how many
    employees were deactivated. Caller commits."""
    today = crud.company_today(db, company_id)
    due = db.scalars(
        select(models.ExitRequestModel)
        .join(models.Employee, models.Employee.id == models.ExitRequestModel.employee_id)
        .where(
            models.Employee.company_id == company_id,
            models.ExitRequestModel.status.in_(_EXIT_DONE_STATUSES),
            models.ExitRequestModel.last_working_day.is_not(None),
            models.ExitRequestModel.last_working_day <= today,
        )
    ).all()
    count = 0
    for exit_request in due:
        if apply_exit(db, company_id, exit_request):
            count += 1
            logger.info("Access revoked for employee %s (exit %s, last working day %s)",
                        exit_request.employee_id, exit_request.id, exit_request.last_working_day)
    return count
