from datetime import date, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import crud, models, schemas
from ..database import get_db
from ..deps import require_view_permission

router = APIRouter(prefix="/api", tags=["audit"])

# Display grouping for the Administration > Audit Logs "Module" column --
# core.audit_logs itself has no module column, only doctype (see
# crud.create_audit_log's call sites for the doctype values actually used).
_DOCTYPE_MODULE = {
    "employee": "People",
    "role_assignment": "People",
    "role": "Administration",
    "role_matrix": "Administration",
    "company": "Organization",
}


def _user_display_name(user: "models.User | None") -> str:
    if user is None:
        return "Unknown"
    if user.employee:
        name = f"{user.employee.first_name} {user.employee.last_name or ''}".strip()
        if name:
            return name
    return user.email


def _bulk_actor_names(db: Session, user_ids: set) -> dict:
    """One query for every distinct actor across the whole page of audit
    log rows, instead of _actor_name's one-row-at-a-time db.get() -- against
    a real (non-local) database, 200 separate round trips for a single
    Audit Logs page load added ~15s, comfortably past the frontend's 8s
    request timeout, so the screen always failed to load."""
    if not user_ids:
        return {}
    users = db.scalars(
        select(models.User)
        .options(selectinload(models.User.employee))
        .where(models.User.id.in_(user_ids))
    ).all()
    return {u.id: _user_display_name(u) for u in users}


@router.get("/audit-logs", response_model=list[schemas.AuditLogOut])
def list_audit_logs(
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    db: Session = Depends(get_db),
    # View is enough to read the log (the UI shows it at View level too).
    current_user: models.User = Depends(require_view_permission("audit_logs")),
):
    """limit/offset/from_date/to_date are all optional and default to
    exactly today's behavior (200 most recent rows, no filter) -- the
    existing frontend caller (admin_screen.dart, dashboard_org.dart) hits
    this with no query params at all and is unaffected. offset/from_date/
    to_date are new capability for a caller that wants to page past that
    200-row window or filter by date, not exposed in the UI yet."""
    rows = crud.list_audit_logs(db, current_user.company_id, limit, offset, from_date, to_date)
    actor_names = _bulk_actor_names(db, {a.user_id for a in rows if a.user_id is not None})
    return [
        schemas.AuditLogOut(
            # C20: stored as timestamptz; rendered in UTC and labelled so.
            ts=a.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            actor="System" if a.user_id is None else actor_names.get(a.user_id, "Unknown"),
            action=a.action.replace("_", " ").title(),
            entity=a.doctype.replace("_", " ").title(),
            module=_DOCTYPE_MODULE.get(a.doctype, a.doctype.replace("_", " ").title()),
            ip=a.ip_address or "—",
        )
        for a in rows
    ]
