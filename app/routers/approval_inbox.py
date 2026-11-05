"""PERF-06: the unified Approvals inbox's ten source lists in ONE request.

The app's inbox (topbar badge, dashboards' "Pending Approvals", the
Approvals screen) used to fetch ten list endpoints separately on every
login. This endpoint returns the same ten lists together. Each source is
produced by calling that endpoint's own handler with the arguments FastAPI
would pass it -- so visibility scoping, payroll scope and serialization are
exactly the same -- behind the same per-module "enabled" gate its router
applies. A source the caller may not see (403) or whose module is disabled
(404) is returned as null; a source that fails unexpectedly is listed in
`errors` (the app then warns that the inbox may be incomplete) instead of
failing the whole inbox.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud, models
from ..database import get_db
from ..deps import get_current_user, get_payroll_scope
from . import assets, attendance, employees, leave, leave_withdrawals, payroll, recruitment, travel, work

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

# key -> (approval doctype, requester field) -- exactly what each module's
# decide endpoint passes to crud.can_decide_request_configurable.
_DECISION = {
    "regularizations": ("attendance_regularization", "employee_id"),
    "leave_requests": ("leave_request", "employee_id"),
    "leave_withdrawals": ("leave_withdrawal", "employee_id"),
    "travel_requests": ("travel_request", "employee_id"),
    "expense_reports": ("expense_claim", "employee_id"),
    "overtime_requests": ("overtime_request", "employee_id"),
    "asset_requests": ("asset_request", "employee_id"),
    "salary_revisions": ("salary_revision_request", "employee_id"),
    "reimbursements": ("expense_claim", "employee_id"),
    "hiring_requisitions": ("hiring_requisition", "requested_by"),
    "exit_requests": ("exit_request", "employee_id"),
}
_CLOSED = {"approved", "rejected", "cancelled", "withdrawn", "completed", "closed", "paid"}

# key -> (module toggle or None, list handler, needs payroll scope)
_SOURCES = {
    "regularizations": ("attendance", attendance.list_regularizations, False),
    "leave_requests": ("leave", leave.list_leave_requests, False),
    "leave_withdrawals": ("leave", leave_withdrawals.list_leave_withdrawals, False),
    "travel_requests": ("travel", travel.list_travel_requests, False),
    "expense_reports": ("travel", travel.list_expense_reports, False),
    "overtime_requests": ("work", work.list_overtime_requests, False),
    "asset_requests": ("assets", assets.list_asset_requests, False),
    "salary_revisions": ("payroll", payroll.list_salary_revision_requests, True),
    "reimbursements": ("payroll", payroll.list_reimbursements, True),
    "hiring_requisitions": ("recruitment", recruitment.list_hiring_requisitions, False),
    "exit_requests": (None, employees.list_exit_request_records, False),
}


def _response_model(handler):
    """The response_model FastAPI serializes this handler's result with."""
    for mod in (assets, attendance, employees, leave, leave_withdrawals, payroll, recruitment, travel, work):
        for route in mod.router.routes:
            if getattr(route, "endpoint", None) is handler:
                return route.response_model
    return None


_ADAPTERS = {key: TypeAdapter(_response_model(h)) for key, (_m, h, _p) in _SOURCES.items()}


class _Relevance:
    """Who an inbox row concerns. The inbox shows a request only to the
    people in its approval path: the requester, whoever can decide it now,
    whoever already decided / acted on it, and managers above the requester
    in the reporting chain. Company-wide approvers (Organization Owner,
    Approvals Approve/Admin, RBAC admins) see everything. Other employees
    -- even ones whose module list shows the record -- don't get it in
    their approvals."""

    def __init__(self, db: Session, user: models.User):
        self.db, self.user = db, user
        self.me = user.employee_id
        self.sees_all = crud.is_fallback_approver(db, user)
        self._subtree: set | None = None
        self._acted: set | None = None

    def _reports(self) -> set:
        if self._subtree is None:
            self._subtree = (crud._reporting_subtree_ids(self.db, self.me, self.user.company_id)
                             if self.me else set())
        return self._subtree

    def _acted_on(self) -> set:
        if self._acted is None:
            self._acted = set(self.db.scalars(
                select(models.ApprovalRequest.document_id)
                .join(models.ApprovalAction, models.ApprovalAction.request_id == models.ApprovalRequest.id)
                .where(models.ApprovalRequest.company_id == self.user.company_id,
                       models.ApprovalAction.actor_id == self.user.id)
            ).all())
        return self._acted

    def keep(self, item: dict, requester_field: str) -> bool:
        if self.sees_all or item.get("can_decide"):
            return True
        me = str(self.me) if self.me else None
        requester = item.get(requester_field)
        if me and str(requester) == me:
            return True
        if me and any(str(item.get(f)) == me for f in ("approver_id", "approved_by", "decided_by")):
            return True
        try:
            if requester is not None and uuid.UUID(str(requester)) in self._reports():
                return True
            return item.get("id") is not None and uuid.UUID(str(item["id"])) in self._acted_on()
        except (TypeError, ValueError):
            return False


@router.get("/inbox-sources")
def approval_inbox_sources(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
) -> dict:
    """Each row also carries `can_decide`: whether the caller may approve or
    reject it right now (same rule as that module's decide endpoint,
    including configurable multi-step workflows; always false for the
    caller's own requests and for closed rows)."""
    payroll_scope = None
    sources: dict[str, list | None] = {}
    errors: list[str] = []
    leave_grant = None
    crud.prime_custom_workflow_memo(db, current_user.company_id, (d for d, _f in _DECISION.values()))
    relevance = _Relevance(db, current_user)
    for key, (module, handler, needs_scope) in _SOURCES.items():
        if module is not None and not crud.is_module_enabled(db, current_user.company_id, module):
            sources[key] = None
            continue
        kwargs = {"limit": None, "offset": 0, "db": db, "current_user": current_user}
        if handler in (attendance.list_regularizations, leave.list_leave_requests, travel.list_travel_requests,
                       leave_withdrawals.list_leave_withdrawals):
            kwargs["status"] = None
        if needs_scope:
            if payroll_scope is None:
                payroll_scope = get_payroll_scope(current_user, db)
            kwargs["scope"] = payroll_scope
        try:
            rows = handler(**kwargs)
            items = jsonable_encoder(
                _ADAPTERS[key].dump_python(
                    _ADAPTERS[key].validate_python(rows, from_attributes=True), mode="json"
                )
            )
            doctype, requester_field = _DECISION[key]
            crud.prime_approval_requests(db, current_user.company_id, [
                (doctype, uuid.UUID(str(i["id"]))) for i in items
                if isinstance(i, dict) and i.get("id") is not None
                and str(i.get("status") or "").lower().replace(" ", "_") not in _CLOSED
            ])
            for item in items:
                if not isinstance(item, dict):
                    continue
                requester = item.get(requester_field)
                status = str(item.get("status") or "").lower().replace(" ", "_")
                if requester is None or item.get("id") is None or status in _CLOSED:
                    item["can_decide"] = False
                    continue
                requester_id = uuid.UUID(str(requester))
                if key in ("leave_requests", "leave_withdrawals") and requester_id != current_user.employee_id:
                    # leave.py (N-07): an org-wide leave administrator also
                    # decides; a granular approve/reject action alone doesn't.
                    if leave_grant is None:
                        from .. import leave_policy

                        leave_grant = leave_policy.is_leave_admin(db, current_user)
                    # Same rule as leave.py: the grant only shortcuts the
                    # default flow, never a configured chain's active step.
                    if leave_grant and not crud._has_custom_approval_workflow(
                        db, current_user.company_id, doctype
                    ):
                        item["can_decide"] = True
                        continue
                item["can_decide"] = crud.can_decide_request_readonly(
                    db, current_user, requester_id, doctype, uuid.UUID(str(item["id"]))
                )
            sources[key] = [i for i in items if not isinstance(i, dict) or relevance.keep(i, requester_field)]
        except HTTPException as exc:
            if exc.status_code in (403, 404):
                sources[key] = None
            else:
                sources[key] = []
                errors.append(key)
        except Exception:  # noqa: BLE001 -- one bad source must not blank the inbox
            logger.exception("approval inbox source %s failed", key)
            db.rollback()
            sources[key] = []
            errors.append(key)
    # Interview invitations no longer need an accept/reject inbox row: new
    # interviews are round-based (Recruitment > Job Opening > Interview
    # Rounds, see recruitment_workflow.py's round-management section) and
    # notify interviewers directly with no decision to make here. Any
    # still-open legacy invite-flow interview remains answerable from its
    # own Interview card in Recruitment (rw.respond_to_invitation), just not
    # surfaced in this combined inbox.
    return {"sources": sources, "errors": errors}
