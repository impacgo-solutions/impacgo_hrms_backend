"""Generic, configurable multi-step approval engine -- wires up the 4
approval_workflow* tables that existed in the schema but had no ORM
mapping or code using them (see wire_approval_workflow_engine.sql).

Every existing single-approver-by-reporting-manager behavior
(crud.can_decide_request, crud.can_act_on_travel_request) becomes, under
this engine, just the auto-provisioned DEFAULT workflow for that doctype:
one step, approver_type='reporting_manager'. A company that never touches
Administration > Approval Workflows keeps that exact behavior forever --
get_or_create_default_workflow only ever creates this default once, the
first time a doctype is needed, and never overwrites a company's own
custom workflow for that doctype.

Parallel (independent) steps: more than one ApprovalWorkflowStep row may
share the same step_order, meaning any ONE of them deciding resolves that
whole step_order -- not "wait for all of them". This is how a company
configures e.g. "Reporting Manager and Dotted-Line Manager both receive
the request at once; whichever one decides first is final" instead of a
strict A-then-B chain: both rows get step_order=1, so can_act treats
either actor as authorized for the current step, and record_action
advances (or finalizes) the request the instant the first decision of
either kind lands -- the other approver's turn simply never comes,
since the request has already moved past step_order=1 (or gone terminal)
by the time they'd act. See _steps_at/can_act/record_action."""

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

DYNAMIC_APPROVER_TYPES = {
    "reporting_manager",
    "dotted_line_manager",
    "department_head",
    "business_unit_head",
    "branch_manager",
}
STATIC_APPROVER_TYPES = {"role", "user"}
ALL_APPROVER_TYPES = DYNAMIC_APPROVER_TYPES | STATIC_APPROVER_TYPES

# Decisions record_action understands (every doctype's decision schema is a
# subset of these). Terminal request statuses can no longer be acted on.
VALID_DECISIONS = {"approved", "l1_approved", "rejected", "sent_back"}
TERMINAL_STATUSES = {"approved", "rejected", "cancelled"}


class InvalidDecision(ValueError):
    """Raised for an unknown/illegal decision -- callers map it to 422."""


def get_active_workflow(db: Session, company_id: uuid.UUID, doctype: str) -> models.ApprovalWorkflow | None:
    return db.scalar(
        select(models.ApprovalWorkflow).where(
            models.ApprovalWorkflow.company_id == company_id,
            models.ApprovalWorkflow.doctype == doctype,
            models.ApprovalWorkflow.is_active.is_(True),
        )
    )


def get_or_create_default_workflow(
    db: Session, company_id: uuid.UUID, doctype: str
) -> models.ApprovalWorkflow:
    """The active workflow for (company, doctype), auto-provisioning a
    single reporting-manager-approver step the first time this doctype is
    ever needed for this company. Never touches an already-existing
    workflow (custom or previously auto-provisioned)."""
    workflow = get_active_workflow(db, company_id, doctype)
    if workflow is not None:
        return workflow
    workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(), company_id=company_id, doctype=doctype,
        name=f"Default {doctype.replace('_', ' ').title()} Approval", is_active=True,
    )
    db.add(workflow)
    db.flush()
    db.add(
        models.ApprovalWorkflowStep(
            id=uuid.uuid4(), workflow_id=workflow.id, step_order=1,
            approver_type="reporting_manager",
        )
    )
    db.flush()
    return workflow


def _steps_at(workflow: models.ApprovalWorkflow, step_order: int) -> list[models.ApprovalWorkflowStep]:
    """Every step row sharing step_order -- more than one means a parallel
    (independent) group, where any single one of them deciding resolves
    the whole group. Exactly one row is the ordinary sequential case."""
    return [s for s in workflow.steps if s.step_order == step_order]


def _distinct_step_orders(workflow: models.ApprovalWorkflow) -> list[int]:
    """Ascending, de-duplicated step_order values -- the real "how many
    stages does this workflow have" count once a stage can hold more than
    one parallel row (len(workflow.steps) alone can't tell a 2-parallel-
    approver single stage apart from 2 sequential stages)."""
    return sorted({s.step_order for s in workflow.steps})


def _resolve_dynamic_approver_employee_id(
    db: Session, employee: models.Employee, approver_type: str
) -> uuid.UUID | None:
    """The single employee_id expected to approve at a dynamic step, or None
    if that rung isn't currently assigned for this employee (e.g. no
    reporting manager set) -- a None result means nobody can act via this
    step's dynamic resolution (the request simply waits)."""
    if approver_type == "reporting_manager":
        return employee.reporting_manager_id
    if approver_type == "dotted_line_manager":
        return employee.dotted_line_manager_id
    if approver_type == "branch_manager":
        return employee.branch.branch_manager_id if employee.branch else None
    if approver_type == "department_head":
        return employee.department.head_employee_id if employee.department else None
    if approver_type == "business_unit_head":
        if employee.department is None or employee.department.business_unit_id is None:
            return None
        bu = db.get(models.BusinessUnit, employee.department.business_unit_id)
        return bu.head_employee_id if bu else None
    return None


def _step_matches(db: Session, step: models.ApprovalWorkflowStep, actor: models.User, employee: models.Employee) -> bool:
    if step.approver_type == "user":
        return step.user_id == actor.id
    if step.approver_type == "role":
        from . import crud
        role = crud.get_user_primary_role(db, actor.id)
        return role is not None and role.id == step.role_id
    if step.approver_type in DYNAMIC_APPROVER_TYPES:
        approver_employee_id = _resolve_dynamic_approver_employee_id(db, employee, step.approver_type)
        return approver_employee_id is not None and approver_employee_id == actor.employee_id
    return False


def can_act(
    db: Session, request: models.ApprovalRequest, actor: models.User, employee: models.Employee
) -> bool:
    """True if `actor` may decide the current step of `request`. `employee`
    is the employee the request was raised for (its reporting_manager_id/
    branch/department drive the dynamic approver types).

    Self-approval is never permitted, checked first and unconditionally --
    covers every step kind, including a 'user' step an admin configured to
    point at the requester themselves, a 'role' step the requester also
    holds, or a dynamic step whose resolved approver_employee_id happens to
    equal the requester's own id (e.g. a data anomaly like a self-referencing
    reporting_manager_id). Mirrors crud.can_decide_request's identical guard
    for the single-step default workflow.

    Parallel group: True if `actor` matches ANY row sharing the current
    step_order, not just the first one -- e.g. Reporting Manager and
    Dotted-Line Manager both authorized at once (see module docstring)."""
    if actor.employee_id is not None and actor.employee_id == employee.id:
        return False
    # SEC-09: never across companies sharing one schema.
    if request.company_id != actor.company_id or employee.company_id != actor.company_id:
        return False
    if request.status in ("approved", "rejected", "cancelled"):
        return False
    workflow = db.get(models.ApprovalWorkflow, request.workflow_id)
    if workflow is None:
        return False
    step_order = request.current_step
    if request.status == "sent_back":
        # A sent-back request restarts at the first step when decided again
        # (see record_action).
        orders = _distinct_step_orders(workflow)
        step_order = orders[0] if orders else 1
    steps = _steps_at(workflow, step_order)
    if any(_step_matches(db, step, actor, employee) for step in steps):
        return True
    # WF-03: nobody is resolvable at this step for this requester (e.g. the
    # CEO has no reporting/dotted-line manager) -- fall back to the same
    # company-wide approvers crud.can_decide_request already honours
    # (Owner / System Settings RBAC holders) instead of leaving the request
    # undecidable forever. Self-approval was already refused above.
    if not any(_step_has_resolvable_approver(db, step, employee) for step in steps):
        from . import crud, leave_policy
        if crud.is_fallback_approver(db, actor):
            return True
        # M-08: for leave, the leave administrators (HR) are fallbacks too.
        return request.doctype in leave_policy.LEAVE_DOCTYPES and leave_policy.is_leave_admin(db, actor)
    return False


def _step_has_resolvable_approver(
    db: Session, step: models.ApprovalWorkflowStep, employee: models.Employee
) -> bool:
    """True if this step points at someone other than the requester."""
    if step.approver_type == "user":
        return step.user_id is not None
    if step.approver_type == "role":
        return step.role_id is not None
    if step.approver_type in DYNAMIC_APPROVER_TYPES:
        approver = _resolve_dynamic_approver_employee_id(db, employee, step.approver_type)
        return approver is not None and approver != employee.id
    return False


def start_request(
    db: Session, company_id: uuid.UUID, doctype: str, document_id: uuid.UUID,
    requested_by: uuid.UUID | None,
) -> models.ApprovalRequest:
    workflow = get_or_create_default_workflow(db, company_id, doctype)
    request = models.ApprovalRequest(
        id=uuid.uuid4(), company_id=company_id, workflow_id=workflow.id,
        doctype=doctype, document_id=document_id, status="pending",
        current_step=1, requested_by=requested_by,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(request)
    db.flush()
    employee = db.get(models.Employee, requested_by) if requested_by else None
    if employee is not None:
        from . import crud
        actor_user_id = crud.get_user_id_for_employee(db, employee.id)
        _skip_steps_without_decider(db, request, workflow, employee, actor_user_id)
    return request


def _step_needs_no_decision(
    db: Session, steps: list[models.ApprovalWorkflowStep], employee: models.Employee,
    already_approved: set[uuid.UUID],
) -> str | None:
    """Why this step can be skipped in a chain, or None if someone must
    decide it: nobody resolves to approve it for this requester (e.g. no
    Second Reporting Manager assigned), or everyone who would approve it
    already approved an earlier step (the same person is Reporting Manager
    and Department Head -- they are never asked twice). Role / user steps
    are never skipped (they always have a decider)."""
    if any(st.approver_type not in DYNAMIC_APPROVER_TYPES for st in steps):
        return None
    resolved = set()
    for st in steps:
        emp_id = _resolve_dynamic_approver_employee_id(db, employee, st.approver_type)
        if emp_id is not None and emp_id != employee.id:
            resolved.add(emp_id)
    if not resolved:
        return "No approver assigned for this step"
    if resolved <= already_approved:
        return "Already approved at an earlier step by the same person"
    return None


def _skip_steps_without_decider(
    db: Session, request: models.ApprovalRequest, workflow: models.ApprovalWorkflow,
    employee: models.Employee, actor_user_id: uuid.UUID | None,
) -> None:
    """Moves a pending request past chain steps that need no decision
    (see _step_needs_no_decision), recording each as 'skipped' in the
    approval history; finalizes it as approved if no step is left. If the
    FIRST step itself has nobody and neither does any later step, the
    request stays put so the WF-03 fallback approvers (Owner / System
    Settings) can decide it, exactly as before."""
    orders = _distinct_step_orders(workflow)
    if request.status != "pending" or request.current_step not in orders:
        return
    already_approved: set[uuid.UUID] = set()
    for a in db.scalars(select(models.ApprovalAction).where(
            models.ApprovalAction.request_id == request.id,
            models.ApprovalAction.action.in_(("approved", "l1_approved")))):
        u = db.get(models.User, a.actor_id)
        if u is not None and u.employee_id is not None:
            already_approved.add(u.employee_id)
    idx = orders.index(request.current_step)
    first_pending = idx
    skipped: list[tuple[int, str]] = []
    while idx < len(orders):
        reason = _step_needs_no_decision(db, _steps_at(workflow, orders[idx]), employee, already_approved)
        if reason is None:
            break
        skipped.append((orders[idx], reason))
        idx += 1
    if not skipped:
        return
    if idx >= len(orders) and not already_approved:
        # Nobody anywhere in the chain (e.g. the CEO) -- keep the first
        # step so the fallback approvers decide it (WF-03), unchanged.
        request.current_step = orders[first_pending]
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    if actor_user_id is not None:
        for order, reason in skipped:
            db.add(models.ApprovalAction(
                id=uuid.uuid4(), request_id=request.id, step_order=order,
                actor_id=actor_user_id, action="skipped", comments=reason, acted_at=now,
            ))
    if idx >= len(orders):
        request.status = "approved"
    else:
        request.current_step = orders[idx]
    db.flush()


def record_action(
    db: Session, request: models.ApprovalRequest, actor: models.User,
    decision: str, comments: str | None = None,
) -> models.ApprovalRequest:
    """Apply one decision to the current step of `request`.

    decision:
      - 'approved'    -- advance to the next step-order group, or finalize
                         the request as 'approved' after the last group.
      - 'l1_approved' -- interim approval: advances like 'approved' but can
                         never finalize a request (422 at the last step).
      - 'rejected'    -- terminal 'rejected' immediately.
      - 'sent_back'   -- terminal 'sent_back' immediately: the request goes
                         back to the requester for revision (it must NOT
                         advance or approve -- WF-02/C4).
    Anything else raises InvalidDecision (routers answer 422). Deciding a
    request that is already approved/rejected raises InvalidDecision too.
    A request that was sent back and is decided again restarts at the
    first step (the requester resubmitted / the approver reconsidered).

    Parallel group: whichever of the group's rows `actor` matched, this
    single call is the ONLY decision that step_order group needs -- it
    advances past the group (or finalizes the request) immediately,
    exactly as if the group had just one approver. The other row(s) in
    that same group never get a turn: can_act checks request.current_step,
    which has already moved on (or the request is already terminal) by
    the time anyone else looks."""
    if decision not in VALID_DECISIONS:
        raise InvalidDecision(
            f"Unsupported decision '{decision}'. Expected one of: "
            + ", ".join(sorted(VALID_DECISIONS))
        )
    if request.status in ("approved", "rejected"):
        raise InvalidDecision(f"This request was already {request.status} and cannot be decided again")
    workflow = db.get(models.ApprovalWorkflow, request.workflow_id)
    step_orders = _distinct_step_orders(workflow) if workflow is not None else []
    if request.status == "sent_back":
        request.current_step = step_orders[0] if step_orders else 1
        request.status = "pending"
    is_last = not step_orders or request.current_step >= step_orders[-1]
    if decision == "l1_approved" and is_last:
        raise InvalidDecision(
            "This is the final approval step -- use 'approved' (or reject / send back)."
        )
    db.add(
        models.ApprovalAction(
            id=uuid.uuid4(), request_id=request.id, step_order=request.current_step,
            actor_id=actor.id, action=decision,
            comments=comments, acted_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    if decision == "rejected":
        request.status = "rejected"
    elif decision == "sent_back":
        request.status = "sent_back"
    elif is_last:
        request.status = "approved"
    else:
        idx = step_orders.index(request.current_step) if request.current_step in step_orders else -1
        request.current_step = step_orders[idx + 1]
        request.status = "pending"
        db.flush()
        # Chain: skip any next step that has no decider or whose decider
        # just approved (never ask the same person twice).
        employee = db.get(models.Employee, request.requested_by) if request.requested_by else None
        if employee is not None and workflow is not None:
            _skip_steps_without_decider(db, request, workflow, employee, actor.id)
    db.flush()
    return request


def resubmit_request(
    db: Session, company_id: uuid.UUID, doctype: str, document_id: uuid.UUID,
    actor: models.User, comments: str | None = None,
) -> models.ApprovalRequest | None:
    """WF-04: the requester edited a sent-back request and resubmitted it --
    the (open) ApprovalRequest goes back to 'pending' at the first step and
    the resubmission is recorded. No-op if none exists or it isn't sent
    back (a pending request keeps its current step)."""
    request = db.scalar(
        select(models.ApprovalRequest).where(
            models.ApprovalRequest.company_id == company_id,
            models.ApprovalRequest.doctype == doctype,
            models.ApprovalRequest.document_id == document_id,
        )
    )
    if request is None or request.status != "sent_back":
        return request
    workflow = db.get(models.ApprovalWorkflow, request.workflow_id)
    step_orders = _distinct_step_orders(workflow) if workflow is not None else []
    request.current_step = step_orders[0] if step_orders else 1
    request.status = "pending"
    db.add(
        models.ApprovalAction(
            id=uuid.uuid4(), request_id=request.id, step_order=request.current_step,
            actor_id=actor.id, action="resubmitted",
            comments=comments, acted_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    db.flush()
    return request


def close_request(
    db: Session, company_id: uuid.UUID, doctype: str, document_id: uuid.UUID,
    actor: models.User, status: str = "cancelled", comments: str | None = None,
) -> models.ApprovalRequest | None:
    """Close the (open) ApprovalRequest behind a document, e.g. when the
    requester cancels it (WF-04). No-op if none exists or it is terminal."""
    request = db.scalar(
        select(models.ApprovalRequest).where(
            models.ApprovalRequest.company_id == company_id,
            models.ApprovalRequest.doctype == doctype,
            models.ApprovalRequest.document_id == document_id,
        )
    )
    if request is None or request.status in ("approved", "rejected", "cancelled"):
        return request
    db.add(
        models.ApprovalAction(
            id=uuid.uuid4(), request_id=request.id, step_order=request.current_step,
            actor_id=actor.id, action=status,
            comments=comments, acted_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    request.status = status
    db.flush()
    return request


def current_step_approver_employee_ids(
    db: Session, request: models.ApprovalRequest, employee: models.Employee
) -> set[uuid.UUID]:
    """Employee ids of everyone who may decide `request`'s CURRENT step --
    exactly the people can_act would authorize (same per-step matching,
    same self-exclusion, same WF-03 fallback when nobody is resolvable).
    Used to notify only the active step in a sequential chain (step 2 is
    told only once step 1 has approved). Empty for a finished request."""
    if request.status in TERMINAL_STATUSES:
        return set()
    workflow = db.get(models.ApprovalWorkflow, request.workflow_id)
    if workflow is None:
        return set()
    step_order = request.current_step
    if request.status == "sent_back":
        orders = _distinct_step_orders(workflow)
        step_order = orders[0] if orders else 1
    ids: set[uuid.UUID] = set()
    steps = _steps_at(workflow, step_order)
    for step in steps:
        if step.approver_type in DYNAMIC_APPROVER_TYPES:
            approver = _resolve_dynamic_approver_employee_id(db, employee, step.approver_type)
            if approver is not None:
                ids.add(approver)
        elif step.approver_type == "user" and step.user_id is not None:
            user = db.get(models.User, step.user_id)
            if user is not None and user.employee_id is not None and user.status == "active":
                ids.add(user.employee_id)
        elif step.approver_type == "role" and step.role_id is not None:
            from . import crud
            for user in db.scalars(select(models.User).where(
                    models.User.company_id == request.company_id,
                    models.User.employee_id.is_not(None),
                    models.User.status == "active")):
                role = crud.get_user_primary_role(db, user.id)
                if role is not None and role.id == step.role_id:
                    ids.add(user.employee_id)
    ids.discard(employee.id)
    if not ids and not any(_step_has_resolvable_approver(db, s, employee) for s in steps):
        from . import crud, leave_policy
        ids = set(crud.list_fallback_approver_employee_ids(db, request.company_id, exclude_employee_id=employee.id))
        if request.doctype in leave_policy.LEAVE_DOCTYPES:
            ids |= leave_policy.leave_admin_employee_ids(db, request.company_id, exclude_employee_id=employee.id)
    return ids
