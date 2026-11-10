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

import datetime
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
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


# ── Recent Decisions: search, filters, pagination ───────────────────────────

_TYPE_LABEL = {
    "regularizations": "Attendance Regularization", "leave_requests": "Leave Request",
    "leave_withdrawals": "Leave Withdrawal", "travel_requests": "Travel Request",
    "expense_reports": "Expense Report", "overtime_requests": "Overtime Request",
    "asset_requests": "Asset Request", "salary_revisions": "Salary Revision",
    "reimbursements": "Reimbursement", "hiring_requisitions": "Hiring Requisition",
    "exit_requests": "Exit Request",
}
_DECIDED = {"approved": "Approved", "rejected": "Rejected", "sent_back": "Sent Back"}


def _s(v) -> str:
    return "" if v is None else str(v)


def _detail(key: str, j: dict) -> str:
    """Same "Details" text the Approvals screen has always shown."""
    if key == "regularizations":
        return f"{_s(j.get('attendance_date'))} · {j.get('reason') or '—'}"
    if key in ("leave_requests", "leave_withdrawals"):
        return f"{j.get('leave_type_name') or 'Leave'} · {_s(j.get('from_date'))}" + (
            f" → {_s(j.get('to_date'))}" if j.get("to_date") and j.get("to_date") != j.get("from_date") else "")
    if key == "travel_requests":
        return f"{j.get('destination') or '—'} · {_s(j.get('from_date'))} → {_s(j.get('to_date'))}"
    if key == "expense_reports":
        return f"{j.get('reference') or '—'} · {_s(j.get('claim_date'))}"
    if key == "reimbursements":
        return f"{_s(j.get('category'))} · ₹{_s(j.get('amount'))} · {_s(j.get('expense_date'))}"
    if key == "overtime_requests":
        return f"{_s(j.get('work_date'))} · {_s(j.get('hours'))} hrs"
    if key == "asset_requests":
        return f"{(j.get('asset_type') or '').strip() or 'Type not specified'} · {j.get('justification') or '—'}"
    if key == "salary_revisions":
        return f"Current ₹{j.get('current_ctc') or '—'} → Proposed ₹{j.get('proposed_ctc') or '—'}"
    if key == "hiring_requisitions":
        return f"{_s(j.get('positions_count'))}x {_s(j.get('designation_title'))}"
    if key == "exit_requests":
        return f"Resigning {_s(j.get('resignation_date'))}"
    return ""


def _request_no(key: str, j: dict) -> str:
    """The human request ID when the module has one (HR-TR-…, HR-EXP-…)."""
    return _s(j.get("request_no") or j.get("series") or j.get("claim_no") or j.get("code") or "")


def _decision_date(j: dict) -> str:
    """When it was decided; migrated / older rows without decided_at fall
    back to their other timestamps so they still sort and filter."""
    for k in ("decided_at", "approved_at", "updated_at", "submitted_at", "created_at",
              "from_date", "attendance_date", "work_date", "claim_date", "expense_date", "resignation_date"):
        if j.get(k):
            return _s(j[k])
    return ""


def _decided_rows(db: Session, user: models.User) -> list[dict]:
    """Every decided request the caller may see in their approvals (same
    sources, scoping and relevance rule as /inbox-sources), minus their own,
    normalised to one row shape."""
    payroll_scope = None
    relevance = _Relevance(db, user)
    raw: list[tuple[str, dict]] = []
    for key, (module, handler, needs_scope) in _SOURCES.items():
        if module is not None and not crud.is_module_enabled(db, user.company_id, module):
            continue
        kwargs = {"limit": None, "offset": 0, "db": db, "current_user": user}
        if handler in (attendance.list_regularizations, leave.list_leave_requests, travel.list_travel_requests,
                       leave_withdrawals.list_leave_withdrawals):
            kwargs["status"] = None
        if needs_scope:
            if payroll_scope is None:
                payroll_scope = get_payroll_scope(user, db)
            kwargs["scope"] = payroll_scope
        try:
            rows = handler(**kwargs)
            items = jsonable_encoder(_ADAPTERS[key].dump_python(
                _ADAPTERS[key].validate_python(rows, from_attributes=True), mode="json"))
        except HTTPException:
            continue
        except Exception:  # noqa: BLE001 -- one bad source must not blank the list
            logger.exception("recent decisions source %s failed", key)
            db.rollback()
            continue
        _doctype, requester_field = _DECISION[key]
        for item in items:
            if not isinstance(item, dict):
                continue
            status = _s(item.get("status")).lower().replace(" ", "_")
            if status not in _DECIDED or item.get(requester_field) is None:
                continue
            item["can_decide"] = False
            if str(item.get(requester_field)) == _s(user.employee_id):
                continue  # the viewer's own requests aren't "decisions" for them
            if relevance.keep(item, requester_field):
                raw.append((key, item))

    # one bulk lookup for names / codes / departments / approvers
    ids: set[uuid.UUID] = set()
    for key, j in raw:
        for f in (_DECISION[key][1], "approver_id", "approved_by", "decided_by"):
            try:
                if j.get(f):
                    ids.add(uuid.UUID(str(j[f])))
            except (TypeError, ValueError):
                pass
    emps = {e.id: e for e in db.scalars(select(models.Employee).where(models.Employee.id.in_(ids)))} if ids else {}
    depts = {d.id: d.name for d in db.scalars(select(models.Department).where(
        models.Department.id.in_({e.department_id for e in emps.values() if e.department_id})))} if emps else {}

    def emp_of(v):
        try:
            return emps.get(uuid.UUID(str(v))) if v else None
        except (TypeError, ValueError):
            return None

    out = []
    for key, j in raw:
        req = emp_of(j.get(_DECISION[key][1]))
        approver = emp_of(j.get("approver_id") or j.get("approved_by") or j.get("decided_by"))
        approver_name = j.get("approver_name") or (crud._full_name(approver) if approver else None) or "—"
        status = _s(j.get("status")).lower().replace(" ", "_")
        out.append({
            "id": _s(j.get("id")),
            "source": key,
            "type": _TYPE_LABEL[key],
            "request_no": _request_no(key, j),
            "employee_id": _s(j.get(_DECISION[key][1])),
            "employee_name": (crud._full_name(req) if req else None) or j.get("employee_name") or "—",
            "employee_code": req.employee_code if req else None,
            "department_id": _s(req.department_id) if req and req.department_id else None,
            "department": depts.get(req.department_id) if req and req.department_id else None,
            "details": _detail(key, j),
            "status": _DECIDED[status],
            "status_key": status,
            "decided_by": approver_name,
            "decided_by_manager_type": j.get("decided_by_manager_type"),
            "decision_notes": j.get("decision_notes"),
            "decided_at": _decision_date(j),
        })
    return out


@router.get("/decisions")
def recent_decisions(
    q: str | None = Query(default=None, max_length=200),
    types: list[str] | None = Query(default=None),
    statuses: list[str] | None = Query(default=None),
    employee_ids: list[str] | None = Query(default=None),
    department_ids: list[str] | None = Query(default=None),
    details: str | None = Query(default=None, max_length=200),
    decided_by: str | None = Query(default=None, max_length=200),
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    sort: str = Query(default="decided_at", pattern="^(decided_at|type|employee|status)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
) -> dict:
    """Approvals > Recent Decisions with search + filters + pagination.

    q matches employee name / code, request ID (number or id), request type,
    details, decided-by and decision notes; every filter combines (AND), a
    multi-value filter matches any of its values. Visibility is exactly the
    inbox's (same module handlers, scoping and relevance rule). `facets`
    lists the types / departments / employees present (before filtering)
    for the filter dropdowns."""
    rows = _decided_rows(db, current_user)
    facets = {
        "types": sorted({r["type"] for r in rows}),
        "statuses": [s for s in _DECIDED.values() if any(r["status"] == s for r in rows)],
        "departments": sorted(({"id": r["department_id"], "name": r["department"]} for r in rows
                               if r["department_id"]), key=lambda d: d["name"] or ""),
        "employees": sorted(({"id": r["employee_id"], "name": r["employee_name"], "code": r["employee_code"]}
                             for r in rows), key=lambda e: e["name"].lower()),
    }
    facets["departments"] = list({d["id"]: d for d in facets["departments"]}.values())
    facets["employees"] = list({e["id"]: e for e in facets["employees"]}.values())

    def norm(v):
        return " ".join(_s(v).lower().split())

    if q and q.strip():
        terms = norm(q).split()
        def hay(r):
            return norm(" ".join(_s(r[k]) for k in ("employee_name", "employee_code", "request_no", "id", "type",
                                                    "details", "decided_by", "decision_notes", "department",
                                                    "status")))
        rows = [r for r in rows if all(t in hay(r) for t in terms)]
    if types:
        wanted = {norm(t) for t in types}
        rows = [r for r in rows if norm(r["type"]) in wanted or r["source"] in types]
    if statuses:
        wanted = {norm(s).replace(" ", "_") for s in statuses}
        rows = [r for r in rows if r["status_key"] in wanted]
    if employee_ids:
        rows = [r for r in rows if r["employee_id"] in set(employee_ids)]
    if department_ids:
        rows = [r for r in rows if r["department_id"] in set(department_ids)]
    if details and details.strip():
        rows = [r for r in rows if norm(details) in norm(r["details"])]
    if decided_by and decided_by.strip():
        rows = [r for r in rows if norm(decided_by) in norm(r["decided_by"])]
    if date_from or date_to:
        def day(r):
            try:
                return datetime.date.fromisoformat(r["decided_at"][:10])
            except ValueError:
                return None
        rows = [r for r in rows if (d := day(r)) is not None
                and (date_from is None or d >= date_from) and (date_to is None or d <= date_to)]

    key = {"decided_at": lambda r: r["decided_at"], "type": lambda r: r["type"].lower(),
           "employee": lambda r: r["employee_name"].lower(), "status": lambda r: r["status"]}[sort]
    rows.sort(key=lambda r: (key(r), r["id"]), reverse=order == "desc")
    total = len(rows)
    start = (page - 1) * page_size
    return {"items": rows[start:start + page_size], "total": total, "page": page, "page_size": page_size,
            "facets": facets}
