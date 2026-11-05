import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user, require_permission_or_action

router = APIRouter(prefix="/api/config", tags=["config"])


# A 'user' step may name several people, any ONE of whom decides it. They
# are stored the way the approval engine already reads a parallel group:
# one core_approval_workflow_steps row per person, all sharing the step's
# step_order. These helpers convert between that storage and the API's
# one-entry-per-step shape (user_ids).

def _validate_step_users(db: Session, company_id, step: schemas.ApprovalWorkflowStepIn, label: str) -> None:
    if step.approver_type == "role" and step.role_id is None:
        raise HTTPException(status_code=422, detail=f"{label}: choose a role")
    if step.approver_type != "user":
        return
    if not step.user_ids:
        raise HTTPException(status_code=422, detail=f"{label}: choose at least one person")
    for uid in step.user_ids:
        user = db.get(models.User, uid)
        if user is None or user.company_id != company_id:
            raise HTTPException(status_code=422, detail=f"{label}: a selected person isn't in your organization")
        if user.status != "active" or user.employee_id is None:
            name = user.full_name or user.email or "A selected person"
            raise HTTPException(
                status_code=422,
                detail=f"{label}: {name} can't approve requests (no active employee login)",
            )


def _step_rows(step: schemas.ApprovalWorkflowStepIn, step_order: int) -> list[dict]:
    base = {
        "step_order": step_order, "approver_type": step.approver_type,
        "role_id": step.role_id if step.approver_type == "role" else None,
        "min_amount": step.min_amount, "max_amount": step.max_amount,
    }
    if step.approver_type == "user":
        return [{**base, "user_id": uid} for uid in step.user_ids or []]
    return [{**base, "user_id": None}]


def _grouped_steps(rows) -> list[dict]:
    """Rows -> one entry per logical step: the 'user' rows sharing a
    step_order merge into one entry (user_ids, user_id = first); every other
    row stays its own entry with its step_order."""
    out: list[dict] = []
    user_entry: dict[int, dict] = {}
    for s in sorted(rows, key=lambda r: r.step_order):
        if s.approver_type == "user" and s.user_id is not None:
            entry = user_entry.get(s.step_order)
            if entry is not None:
                if s.user_id not in entry["user_ids"]:
                    entry["user_ids"].append(s.user_id)
                continue
            entry = {
                "id": s.id, "step_order": s.step_order, "approver_type": "user", "role_id": None,
                "user_id": s.user_id, "user_ids": [s.user_id],
                "min_amount": s.min_amount, "max_amount": s.max_amount,
            }
            user_entry[s.step_order] = entry
            out.append(entry)
        else:
            out.append({
                "id": s.id, "step_order": s.step_order, "approver_type": s.approver_type,
                "role_id": s.role_id, "user_id": s.user_id,
                "user_ids": [s.user_id] if s.user_id else None,
                "min_amount": s.min_amount, "max_amount": s.max_amount,
            })
    return out


def _workflow_out(wf) -> dict:
    return {"id": wf.id, "doctype": wf.doctype, "name": wf.name, "is_active": wf.is_active,
            "steps": _grouped_steps(wf.steps)}


@router.get("/approval-workflows", response_model=list[schemas.ApprovalWorkflowOut])
def list_approval_workflows(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return [_workflow_out(wf) for wf in crud.list_approval_workflows(db, current_user.company_id)]


@router.put("/approval-workflows", response_model=schemas.ApprovalWorkflowOut)
def upsert_approval_workflow(
    payload: schemas.ApprovalWorkflowUpsertRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("system_settings_rbac", "config", "edit")
    ),
):
    """Defines (or replaces) the approval chain for one document type --
    e.g. Leave: Employee -> Manager -> HR is 2 steps, approver_type
    'reporting_manager' then 'role' (role_id = the HR role's id)."""
    steps = []
    for i, s in enumerate(payload.steps, start=1):
        _validate_step_users(db, current_user.company_id, s, f"Step {i}")
        steps.extend(_step_rows(s, s.step_order))
    if not steps:
        # An ACTIVE workflow with zero steps would leave nobody (but the
        # fallback approvers) able to decide. "No steps" means "no custom
        # chain": deactivate it so the default reporting-manager flow applies.
        from .. import approval_engine

        old = approval_engine.get_active_workflow(db, current_user.company_id, payload.doctype)
        if old is None:
            return {"id": uuid.uuid4(), "doctype": payload.doctype, "name": payload.name,
                    "is_active": False, "steps": []}
        old.is_active = False
        old.updated_by = current_user.id
        workflow = old
        crud.create_audit_log(
            db, current_user.company_id, current_user.id, "update", "approval_workflow", workflow.id,
            changes={"doctype": payload.doctype, "steps": []},
        )
        db.commit()
        db.refresh(workflow)
        return _workflow_out(workflow)
    workflow = crud.upsert_approval_workflow(
        db, current_user.company_id, payload.doctype, payload.name, steps,
        user_id=current_user.id,
    )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "approval_workflow", workflow.id,
        changes={"doctype": payload.doctype, "steps": [s.model_dump(mode="json") for s in payload.steps]},
    )
    db.commit()
    db.refresh(workflow)
    return _workflow_out(workflow)


@router.get("/hierarchy-bindings", response_model=schemas.HierarchyRoleBindingsResponse)
def get_hierarchy_bindings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    bindings = crud.get_hierarchy_role_bindings(db, current_user.company_id)
    return schemas.HierarchyRoleBindingsResponse(
        bindings=[
            schemas.HierarchyRoleBindingOut(
                rung_key=b.rung_key, role_id=b.role_id, role_name=b.role.name,
            )
            for b in bindings
        ]
    )


@router.put("/hierarchy-bindings", response_model=schemas.HierarchyRoleBindingsResponse)
def update_hierarchy_bindings(
    payload: schemas.HierarchyRoleBindingsUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("system_settings_rbac", "config", "edit")
    ),
):
    """Binds a reporting-hierarchy rung (team_lead/project_manager/
    senior_manager/branch_manager/branch_head) to one of this company's own
    roles -- so renaming/replacing a built-in role doesn't silently break
    org_hierarchy.py's chain resolution."""
    try:
        crud.upsert_hierarchy_role_bindings(
            db, current_user.company_id, payload.bindings, user_id=current_user.id
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "hierarchy_role_bindings",
        changes={"bindings": {k: str(v) for k, v in payload.bindings.items()}},
    )
    db.commit()
    bindings = crud.get_hierarchy_role_bindings(db, current_user.company_id)
    return schemas.HierarchyRoleBindingsResponse(
        bindings=[
            schemas.HierarchyRoleBindingOut(
                rung_key=b.rung_key, role_id=b.role_id, role_name=b.role.name,
            )
            for b in bindings
        ]
    )


@router.get("/approval-history/{doctype}/{document_id}")
def approval_history(
    doctype: str,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Step-by-step approval trail of one request under a configured
    workflow: every step with its approver(s), state (approved / rejected /
    active / waiting / not reached), who acted, when and their comment.
    mode = 'sequential' | 'parallel' | 'default' (no workflow configured --
    the request is decided by either reporting manager, nothing to show).

    Visible to the requester, anyone in the chain, and company-wide
    approvers (Owner / System Settings) -- the same people who can see the
    request itself."""
    from .. import approval_engine
    from sqlalchemy import select

    request = db.scalar(select(models.ApprovalRequest).where(
        models.ApprovalRequest.company_id == current_user.company_id,
        models.ApprovalRequest.doctype == doctype,
        models.ApprovalRequest.document_id == document_id,
    ))
    if request is None:
        return {"mode": "default", "status": None, "steps": []}
    requester = db.get(models.Employee, request.requested_by) if request.requested_by else None
    workflow = db.get(models.ApprovalWorkflow, request.workflow_id)
    if requester is None or workflow is None:
        return {"mode": "default", "status": request.status, "steps": []}

    actions = db.scalars(select(models.ApprovalAction).where(
        models.ApprovalAction.request_id == request.id).order_by(models.ApprovalAction.acted_at)).all()
    actor_user_ids = {a.actor_id for a in actions}
    allowed = (
        current_user.employee_id == requester.id
        or current_user.id in actor_user_ids
        or crud.is_fallback_approver(db, current_user)
        or crud.can_decide_request(db, current_user, requester.id)
        or approval_engine.can_act(db, request, current_user, requester)
        or (current_user.employee_id is not None and current_user.employee_id in {
            approval_engine._resolve_dynamic_approver_employee_id(db, requester, s.approver_type)
            for s in workflow.steps if s.approver_type in approval_engine.DYNAMIC_APPROVER_TYPES
        })
        or any(s.approver_type == "user" and s.user_id == current_user.id for s in workflow.steps)
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="You can't view this approval history")

    def emp_name(emp_id):
        e = db.get(models.Employee, emp_id) if emp_id else None
        return f"{e.first_name} {e.last_name or ''}".strip() if e else None

    def user_name(user_id):
        u = db.get(models.User, user_id) if user_id else None
        if u is None:
            return None
        return emp_name(u.employee_id) or u.full_name or u.email

    orders = sorted({s.step_order for s in workflow.steps})
    mode = "sequential" if len(orders) > 1 else "parallel"
    labels = {
        "reporting_manager": "Reporting Manager", "dotted_line_manager": "Second Reporting Manager",
        "department_head": "Department Head", "business_unit_head": "Business Unit Head",
        "branch_manager": "Branch Manager", "role": "Role", "user": "User",
    }
    out_steps = []
    for idx, order in enumerate(orders, start=1):
        rows = [s for s in workflow.steps if s.step_order == order]
        approvers = []
        # Several named people in one step -> one entry, "A or B or C" (any
        # one of them decides).
        people = [user_name(s.user_id) or "—" for s in rows if s.approver_type == "user"]
        if people:
            approvers.append({"type": "User", "name": " or ".join(people)})
        for s in rows:
            if s.approver_type == "user":
                continue
            if s.approver_type in approval_engine.DYNAMIC_APPROVER_TYPES:
                emp_id = approval_engine._resolve_dynamic_approver_employee_id(db, requester, s.approver_type)
                approvers.append({"type": labels[s.approver_type], "name": emp_name(emp_id) or "Not assigned"})
            elif s.approver_type == "role":
                role = db.get(models.Role, s.role_id) if s.role_id else None
                approvers.append({"type": "Role", "name": role.name if role else "—"})
            else:
                approvers.append({"type": "User", "name": user_name(s.user_id) or "—"})
        done = [a for a in actions if a.step_order == order]
        last = done[-1] if done else None
        if last is not None:
            state = {"approved": "approved", "l1_approved": "approved", "rejected": "rejected",
                     "sent_back": "sent_back", "skipped": "skipped"}.get(last.action, last.action)
        elif request.status in ("pending", "sent_back") and request.current_step == order:
            state = "active"
        elif request.status in ("rejected", "cancelled", "approved"):
            state = "not_reached"
        else:
            state = "waiting"
        out_steps.append({
            "step": idx, "step_order": order, "state": state, "approvers": approvers,
            "action": last.action if last else None,
            "acted_by": user_name(last.actor_id) if last and last.action != "skipped" else None,
            "acted_at": last.acted_at.isoformat() if last and last.acted_at else None,
            "comments": last.comments if last else None,
        })
    return {"mode": mode, "status": request.status, "current_step": request.current_step, "steps": out_steps}


# ── Company-wide approval mode (Administration > Approval Workflows) ──────

# Every request type that goes through the approval engine, in display order.
APPROVAL_DOCTYPES = [
    ("leave_request", "Leave Request"),
    ("leave_withdrawal", "Leave Withdrawal"),
    ("attendance_regularization", "Attendance Regularization"),
    ("overtime_request", "Overtime Request"),
    ("travel_request", "Travel Request"),
    ("expense_claim", "Reimbursement / Expense Report"),
    ("salary_revision_request", "Salary Revision"),
    ("asset_request", "Asset Request"),
    ("exit_request", "Resignation / Exit Request"),
    ("hiring_requisition", "Hiring Requisition"),
    ("recruitment_offer", "Recruitment Offer"),
]
_DOCTYPE_KEYS = {d for d, _ in APPROVAL_DOCTYPES}


def _type_summary(db, company_id, doctype: str, label: str) -> dict:
    from .. import approval_engine

    wf = approval_engine.get_active_workflow(db, company_id, doctype)
    if not crud._is_custom_workflow(wf):
        return {"doctype": doctype, "label": label, "mode": "default", "steps": []}
    # Stable order: by step, then (for parallel approvers sharing a step) by
    # a fixed approver-type order -- never by random row ids, so identical
    # configurations compare equal across request types.
    rank = {t: i for i, t in enumerate(("reporting_manager", "dotted_line_manager", "department_head",
                                        "business_unit_head", "branch_manager", "role", "user"))}
    # One entry per logical step: the people of a 'user' step (several rows
    # sharing a step_order) come back together as user_ids, sorted so equal
    # configurations compare equal.
    entries = []
    for g in _grouped_steps(wf.steps):
        ids = sorted(str(u) for u in (g["user_ids"] or [])) if g["approver_type"] == "user" else []
        entries.append({
            "step_order": g["step_order"], "approver_type": g["approver_type"],
            "role_id": str(g["role_id"]) if g["role_id"] else None,
            "user_id": ids[0] if ids else None, "user_ids": ids or None,
        })
    entries.sort(key=lambda e: (e["step_order"], rank.get(e["approver_type"], 99),
                                e["role_id"] or "", ",".join(e["user_ids"] or [])))
    orders = {e["step_order"] for e in entries}
    return {
        "doctype": doctype, "label": label,
        "mode": "sequential" if len(orders) > 1 else "parallel",
        "steps": [{k: v for k, v in e.items() if k != "step_order"} for e in entries],
    }


@router.get("/approval-mode")
def get_approval_mode(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The company's approval mode across every request type:
    mode = 'parallel' (every type parallel or default) | 'sequential'
    (every type a chain) | 'mixed'; steps = the shared approver list when
    every configured type uses the same one."""
    types = [_type_summary(db, current_user.company_id, d, label) for d, label in APPROVAL_DOCTYPES]
    modes = {t["mode"] for t in types}
    if modes <= {"default", "parallel"}:
        mode = "parallel"
    elif modes == {"sequential"}:
        mode = "sequential"
    else:
        mode = "mixed"
    configured = [t["steps"] for t in types if t["mode"] != "default"]
    shared = configured[0] if configured and all(c == configured[0] for c in configured) else []
    return {"mode": mode, "steps": shared, "types": types}


@router.put("/approval-mode")
def set_approval_mode(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("system_settings_rbac", "config", "edit")
    ),
):
    """Applies one approval mode + approver list to the selected request
    types in one transaction.

    {mode: 'parallel' | 'sequential', steps: [{approver_type, role_id?,
     user_id?}, ...], doctypes: [...]}

    Sequential: approvers decide in the listed order (each step notified
    only after the previous approves). Parallel: all listed approvers are
    notified at once and the first decision is final; an EMPTY list in
    Parallel resets the types to the default (each employee's reporting
    manager(s)). Requests already in progress keep the flow they started
    with."""
    mode = payload.get("mode")
    if mode not in ("parallel", "sequential"):
        raise HTTPException(status_code=422, detail="mode must be 'parallel' or 'sequential'")
    doctypes = payload.get("doctypes") or [d for d, _ in APPROVAL_DOCTYPES]
    bad = [d for d in doctypes if d not in _DOCTYPE_KEYS]
    if bad:
        raise HTTPException(status_code=422, detail=f"Unknown request type(s): {', '.join(bad)}")
    raw_steps = payload.get("steps") or []
    steps = []
    for i, s in enumerate(raw_steps, start=1):
        try:
            step = schemas.ApprovalWorkflowStepIn(step_order=1 if mode == "parallel" else i, **{
                k: s.get(k) for k in ("approver_type", "role_id", "user_id", "user_ids")})
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Approver {i}: {exc}") from exc
        _validate_step_users(db, current_user.company_id, step, f"Approver {i}")
        steps.append(step)
    if mode == "sequential" and not steps:
        raise HTTPException(status_code=422, detail="A chain needs at least one approver")
    labels = dict(APPROVAL_DOCTYPES)
    from .. import approval_engine

    for doctype in doctypes:
        if mode == "parallel" and not steps:
            wf = approval_engine.get_active_workflow(db, current_user.company_id, doctype)
            if wf is not None:
                wf.is_active = False
                wf.updated_by = current_user.id
            continue
        crud.upsert_approval_workflow(
            db, current_user.company_id, doctype, labels[doctype],
            [row for s in steps for row in _step_rows(s, s.step_order)],
            user_id=current_user.id,
        )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "approval_mode", None,
        changes={"mode": mode, "doctypes": doctypes, "steps": [s.model_dump(mode="json") for s in steps]},
    )
    db.commit()
    db.info.pop(crud._RBAC_MEMO_KEY, None)
    return get_approval_mode(db=db, current_user=current_user)
