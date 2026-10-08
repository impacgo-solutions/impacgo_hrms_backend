import logging
import os
import uuid
from datetime import date, datetime, timezone

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import access_lifecycle, auth_state, crud, models, org_hierarchy, role_tiers, schemas
from .. import email_service as es
from .. import employee_validation as ev
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_people_access, require_people_access_to_employee, require_people_access_or_self, require_people_access_or_self_or_manager, require_permission
from ..storage import delete_uploaded_file, save_uploaded_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/employees", tags=["employees"])


def _to_out(db: Session, e: models.Employee) -> schemas.EmployeeOut:
    return schemas.EmployeeOut(
        id=e.id,
        employee_code=e.employee_code,
        first_name=e.first_name,
        last_name=e.last_name,
        work_email=e.work_email,
        gender=e.gender,
        date_of_joining=e.date_of_joining,
        employment_type=e.employment_type,
        status=e.status,
        band=e.band,
        annual_ctc=e.annual_ctc,
        work_mode=e.work_mode,
        branch_name=e.branch.name if e.branch else None,
        department_name=e.department.name if e.department else None,
        designation_name=e.designation.name if e.designation else None,
    )


@router.get("", response_model=schemas.EmployeeListPage)
def list_employees(
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    dept: Optional[str] = Query(None),
    branch: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access("view")),
):
    company = db.get(models.Company, current_user.company_id)
    # Same visibility as GET /full (a Manager: their reporting subtree +
    # self), applied in the query; CTC only where the caller may see pay.
    try:
        page = crud.list_employees_paginated(
            db, company.id, limit=limit, cursor=cursor,
            search=search, dept=dept, branch=branch, status=status,
            visible_ids=crud.get_people_directory_visible_ids(db, current_user),
        )
    except crud.InvalidCursor as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pay_visible = crud.get_payroll_visible_ids(db, current_user)
    if pay_visible is not None:
        allowed = {str(v) for v in pay_visible}
        for item in page["items"]:
            if item["id"] not in allowed:
                item["ctc"] = 0  # the app's placeholder for absent pay data
    return page


@router.get("/lifecycle-events", response_model=list[schemas.TransferEventOut])
def list_lifecycle_events(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access("view")),
):
    company = db.get(models.Company, current_user.company_id)
    out = []
    for row in crud.list_lifecycle_events(
        db, company.id, limit=limit, offset=offset,
        visible_ids=crud.get_people_directory_visible_ids(db, current_user),
    ):
        e = row["event"]
        is_promotion = e.to_designation_id is not None and e.to_designation_id != e.from_designation_id
        if is_promotion:
            kind = "Promotion"
            from_value = e.from_designation.name if e.from_designation else "—"
            to_value = e.to_designation.name if e.to_designation else "—"
        elif e.to_department_id is not None and e.to_department_id != e.from_department_id:
            kind = "Inter-department Transfer"
            from_value = e.from_department.name if e.from_department else "—"
            to_value = e.to_department.name if e.to_department else "—"
        elif e.event_type in ("contract_renewal", "contract_conversion"):
            kind = "Contract Renewal" if e.event_type == "contract_renewal" else "Converted to Permanent"
            from_value = (f"Contract to {e.from_contract_end_date.isoformat()}"
                          if e.from_contract_end_date else "Contract")
            to_value = (f"Contract to {e.to_contract_end_date.isoformat()}"
                        if e.event_type == "contract_renewal" else "Permanent (Full-time)")
        else:
            kind = e.event_type.replace("_", " ").title()
            from_value = "—"
            to_value = "—"
        out.append(
            schemas.TransferEventOut(
                id=e.id,
                employee_id=e.employee_id,
                type=kind,
                from_value=from_value,
                to_value=to_value,
                effective_date=e.event_date,
                employee_name=row["employee_name"],
                employee_code=row["employee_code"],
            )
        )
    return out


@router.get(
    "/lifecycle-events/{event_id}",
    response_model=schemas.LifecycleEventDetailOut,
)
def get_lifecycle_event(
    event_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access("view")),
):
    """People > Transfers & Promotions > (click a row) -- the complete
    Transfer/Promotion Details screen. Same tenant-isolated lookup as every
    other {id}-keyed endpoint here (crud._require_employee's convention):
    a cross-tenant guess 404s exactly like a nonexistent id."""
    detail = crud.get_lifecycle_event(
        db, current_user.company_id, event_id,
        visible_ids=crud.get_people_directory_visible_ids(db, current_user),
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Transfer/promotion record not found")
    return detail


@router.patch(
    "/lifecycle-events/{event_id}",
    response_model=schemas.LifecycleEventDetailOut,
)
def update_lifecycle_event(
    event_id: uuid.UUID,
    payload: schemas.LifecycleEventUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access("transfer")),
):
    """People > Transfers & Promotions > Edit -- same permission gate as
    the purpose-built Employee Transfer action (POST /{id}/transfer), i.e.
    authorized HR/Organization Owner roles only. Corrects the one specific
    historical record's own fields; never touches any other lifecycle
    event or the employee's current live record (see
    crud.update_lifecycle_event's docstring)."""
    event = crud.get_lifecycle_event_for_company(db, current_user.company_id, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Transfer/promotion record not found")
    updates = payload.model_dump(exclude_unset=True)
    changed = crud.update_lifecycle_event(event, updates)
    if not changed:
        raise HTTPException(
            status_code=400,
            detail="No changes to save — the submitted values match the existing record.",
        )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "employee_lifecycle_event", event.id,
        changes={k: str(v) if v is not None else None for k, v in updates.items()},
    )
    db.commit()
    detail = crud.get_lifecycle_event(db, current_user.company_id, event_id)
    return detail


@router.get("/exit-requests", response_model=list[schemas.ExitRequestOut])
def list_exit_requests(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access("view")),
):
    company = db.get(models.Company, current_user.company_id)
    out = []
    for row in crud.list_exit_requests(
        db, company.id, limit=limit, offset=offset,
        visible_ids=crud.get_people_directory_visible_ids(db, current_user),
    ):
        notice_days = (
            (row["last_working_day"] - row["resignation_date"]).days
            if row["last_working_day"]
            else 0
        )
        out.append(
            schemas.ExitRequestOut(
                id=row["id"],
                employee_id=row["employee_id"],
                last_working_day=row["last_working_day"],
                notice_period_days=notice_days,
                final_settlement_review=row["final_settlement_review"],
                access_revocation=row["access_revocation"],
                knowledge_transfer=row["knowledge_transfer"],
                asset_return=row["asset_return"],
                fnf_status=row["fnf_status"],
                employee_name=row["employee_name"],
                employee_code=row["employee_code"],
            )
        )
    return out


def _exit_request_out(db: Session, request) -> schemas.ExitRequestRecordOut:
    return schemas.ExitRequestRecordOut(
        id=request.id,
        employee_id=request.employee_id,
        resignation_date=request.resignation_date,
        last_working_day=request.last_working_day,
        reason=request.reason,
        status=request.status,
        approver_id=request.approver_id,
        approver_name=crud.employee_display_name(db, request.approver_id)
        if request.approver_id
        else "—",
        decision_notes=request.decision_notes,
        decided_at=request.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, request.employee_id), request.approver_id
        ),
    )


@router.get(
    "/exit-requests/records",
    response_model=list[schemas.ExitRequestRecordOut],
)
def list_exit_request_records(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Raw exit request rows (any status) for the Approvals inbox -- distinct
    from GET /exit-requests, which is the Offboarding tab's read-only
    joined reporting view (no id/status)."""
    return [
        _exit_request_out(db, r)
        for r in crud.list_exit_request_records(
            db, current_user.company_id, limit=limit, offset=offset,
            visible_ids=crud.get_exit_request_visible_ids(db, current_user),
        )
    ]


@router.post(
    "/exit-requests",
    response_model=schemas.ExitRequestRecordOut,
    status_code=201,
)
def create_exit_request(
    payload: schemas.ExitRequestCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Self-service resignation submission: an employee may only submit
    their own, unless the caller has People edit access (HR logging one on
    an employee's behalf) -- same rule shape as require_people_access_or_self,
    just checked by hand since that dependency resolves employee_id from
    the URL path, not a POST body."""
    if payload.employee_id != current_user.employee_id and not crud.can_access_people_module(
        db, current_user, "edit"
    ):
        raise HTTPException(status_code=403, detail="You don't have permission to do this")
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if employee.reporting_manager_id is None:
        raise HTTPException(
            status_code=400,
            detail="This employee has no assigned Reporting Manager -- contact HR before submitting this request",
        )
    try:
        request = crud.create_exit_request(
            db,
            current_user.company_id,
            payload.employee_id,
            payload.resignation_date,
            payload.last_working_day,
            payload.reason,
        )
        crud.create_audit_log(
            db, current_user.company_id, current_user.id, "create", "exit_request", request.id
        )
        db.commit()
    except ev.EmployeeInputError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(request)
    crud.notify_new_request(
        db, current_user.company_id, employee, "Exit Request", "exit_request", request.id
    )
    db.commit()
    return _exit_request_out(db, request)


@router.patch(
    "/exit-requests/{request_id}",
    response_model=schemas.ExitRequestRecordOut,
)
def decide_exit_request(
    request_id: uuid.UUID,
    payload: schemas.ExitRequestDecisionUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py.
    current_user: models.User = Depends(get_current_user),
):
    request = crud.get_exit_request_for_update(db, request_id, current_user.company_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Exit request not found")
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
        db, current_user, request.employee_id, "exit_request", request.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's assigned Reporting Manager can act on this request",
        )
    effective_status = crud.decide_configurable_request(
        db, current_user, request.employee_id, "exit_request", request.id,
        payload.status, payload.decision_notes,
    )
    request.status = effective_status
    request.approver_id = current_user.employee_id
    request.decision_notes = payload.decision_notes
    if effective_status != "pending":
        request.decided_at = datetime.now(timezone.utc)
    if payload.last_working_day is not None:
        request.last_working_day = payload.last_working_day
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, payload.status, "exit_request", request.id
    )
    # Approved with the last working day already reached (or today): login
    # access ends now. A future last working day is picked up by the
    # scheduled pass (access_lifecycle.deactivate_due_exits) on that date.
    if effective_status == "approved":
        db.flush()
        access_lifecycle.apply_exit(db, current_user.company_id, request, current_user.id)
    db.commit()
    db.refresh(request)
    if effective_status != "pending":
        crud.notify_decision(
            db, current_user.company_id, request.employee_id, "Exit Request",
            effective_status, payload.decision_notes, "exit_request", request.id,
        )
        db.commit()
    return _exit_request_out(db, request)


@router.get("/directory", response_model=list[schemas.EmployeeDirectoryEntry])
def list_employees_directory(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Non-sensitive id/name/department/designation/branch/reporting-manager
    for every active employee -- available to any authenticated user in the
    company, unlike GET /full (which carries real salary/bank/PAN data and
    is correctly gated behind People-module access). Backs the app's shared
    employee roster for basic identity resolution (Leave, Attendance,
    org-chart-adjacent displays, "who is my manager" etc.) so those don't
    silently break for a role without full People-module rights. limit/
    offset default to unbounded -- see list_employees_directory's own
    docstring for why."""
    return crud.list_employees_directory(
        db, current_user.company_id, limit=limit, offset=offset,
        visible_ids=crud.get_directory_visible_ids(db, current_user),
    )


@router.get("/full", response_model=None)
def list_employees_full(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access("view")),
    limit: Optional[int] = Query(None, ge=1, le=200),
    cursor: Optional[str] = Query(None),
) -> list[dict] | dict:
    """Everything the Employee Directory / Employee Profile screens need in
    one call. With limit+cursor: returns paginated {"items", "next_cursor",
    "has_more"}. Without: returns plain list[dict] (backward-compatible)."""
    company = db.get(models.Company, current_user.company_id)
    # C2: deny-by-default People gate above, plus visibility scoping -- only
    # Owner / HR / payroll processors (and document-admin tiers) get the whole
    # company; everyone else gets their subtree + self. Applied in the query
    # (it used to filter after paging: short / empty pages).
    try:
        result = crud.list_employees_full(
            db, company.id, limit=limit, cursor=cursor,
            visible_ids=crud.get_people_directory_visible_ids(db, current_user),
        )
    except crud.InvalidCursor as exc:
        raise HTTPException(status_code=400, detail="Invalid pagination cursor") from exc
    # Pay data (CTC, salary, PAN, bank) only where the caller may see it --
    # same rule as GET /{id}/payroll.
    pay_visible = crud.get_payroll_visible_ids(db, current_user)
    if pay_visible is not None:
        allowed = {str(v) for v in pay_visible}
        for row in (result["items"] if isinstance(result, dict) else result):
            if row.get("id") not in allowed:
                _mask_pay(row)
    return result


# Placeholders the app already shows for absent data ('—' text, 0 numbers).
_MASKED_PAYROLL = {
    "ctc": 0, "basic": 0, "gross": 0, "net": 0, "variable": 0,
    "pf": "—", "esi": "—", "pan": "—", "taxRegime": "—", "bank": "—", "account": "—", "ifsc": "—",
    "periodMonth": None, "periodYear": None,
}


def _mask_pay(row: dict) -> None:
    row["ctc"] = 0
    if isinstance(row.get("payrollInfo"), dict):
        row["payrollInfo"] = dict(_MASKED_PAYROLL)
    row["payrollVisible"] = False


@router.post(
    "",
    response_model=schemas.EmployeeOut,
    status_code=201,
)
def create_employee(
    payload: schemas.EmployeeCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access("create")),
):
    company = db.get(models.Company, current_user.company_id)
    # SEC-05: role_name is validated against the caller's own tier -- only the
    # Owner may create an "Organization Owner / CEO" login, and nobody may
    # create a login with more access than their own (role_tiers).
    requested_role = crud.get_role_by_name(db, company.id, payload.role_name) if payload.role_name else None
    if requested_role is not None:
        role_error = role_tiers.role_assignment_error(db, current_user, requested_role)
        if role_error is not None:
            raise HTTPException(status_code=403, detail=role_error)
    actor_role = crud.get_user_primary_role(db, current_user.id)
    field_errors = crud.validate_against_field_rules(
        db, company.id, "employee", payload.model_dump(),
        role_id=actor_role.id if actor_role else None,
    )
    if field_errors:
        logger.warning(
            "create_employee field-rules validation failed for company=%s work_email=%s: %s",
            company.id, payload.work_email, field_errors,
        )
        raise HTTPException(status_code=422, detail=field_errors)
    try:
        employee = crud.create_employee(db, company.id, payload)
        employee.created_by = current_user.id
        crud.create_audit_log(db, company.id, current_user.id, "create", "employee", employee.id)
        db.commit()
    except ev.EmployeeInputError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        logger.warning(
            "create_employee rejected for company=%s work_email=%s: %s",
            company.id, payload.work_email, exc,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        # A duplicate that slipped past the advisory-locked checks in
        # crud.create_employee (or a constraint on a column that check didn't
        # cover) -- logged with the full DB error so the real cause is
        # diagnosable, since the client only ever sees the generic message
        # below (never raw SQL/constraint details).
        logger.exception(
            "create_employee IntegrityError for company=%s work_email=%s",
            company.id, payload.work_email,
        )
        raise HTTPException(
            status_code=409,
            detail="Could not create employee — the work email may already be in use, or a unique employee code could not be generated. Please try again.",
        ) from exc
    db.refresh(employee)
    return _to_out(db, employee)


def _node_out(node: org_hierarchy.HierarchyNode | None) -> schemas.HierarchyNodeOut | None:
    if node is None:
        return None
    return schemas.HierarchyNodeOut(id=node.id, name=node.name, role=node.role)


@router.get("/{employee_id}/hierarchy", response_model=schemas.EmployeeHierarchyOut)
def get_employee_hierarchy(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    """Dynamic reporting hierarchy (HR Representative, Team Lead, Project
    Manager, Senior Manager/GM, Branch Manager, Branch Head) resolved live
    from the employee's current role/department/branch/project assignment --
    see org_hierarchy.resolve_employee_hierarchy."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    chain = org_hierarchy.resolve_employee_hierarchy(db, employee)
    return schemas.EmployeeHierarchyOut(
        employee_id=employee.id,
        hr_representative=_node_out(chain.hr_representative),
        team_lead=_node_out(chain.team_lead),
        project_manager=_node_out(chain.project_manager),
        senior_manager=_node_out(chain.senior_manager),
        branch_manager=_node_out(chain.branch_manager),
        branch_head=_node_out(chain.branch_head),
    )


@router.patch("/{employee_id}/reporting", response_model=schemas.EmployeeOut)
def update_employee_reporting(
    employee_id: uuid.UUID,
    payload: schemas.EmployeeReportingUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Employee Profile > Edit Reporting Manager -- updates
    core.employees.reporting_manager_id and, when the caller sends it,
    dotted_line_manager_id (Two Reporting Managers). Gated at the UI layer
    (HR/Branch Manager/System Admin), same pattern as project-allocation
    edits below.

    reporting_manager_id keeps this endpoint's long-standing contract of
    always being set outright (including to null, to clear it) -- every
    existing caller already relies on that. dotted_line_manager_id is
    exclude_unset instead: a caller that never mentions it (an older
    client, a script) leaves an existing second manager untouched, while
    the Edit Reporting Manager dialog -- which now edits both fields in one
    form -- can still send an explicit null to clear it."""
    payload_fields = payload.model_dump(exclude_unset=True)
    employee = _require_employee(db, employee_id, current_user.company_id)
    changes_dotted_line = "dotted_line_manager_id" in payload_fields
    new_dotted_line_manager_id = (
        payload.dotted_line_manager_id if changes_dotted_line else employee.dotted_line_manager_id
    )

    # Validate BEFORE mutating anything -- self-assignment, a circular
    # reporting chain (this employee already an ancestor of the proposed
    # manager, which would close a loop), and both manager slots pointing
    # at the same person are all rejected here rather than left to whatever
    # the dropdown happened to exclude client-side (a direct API call must
    # be just as safe as the UI).
    if payload.reporting_manager_id is not None:
        if payload.reporting_manager_id == employee_id:
            raise HTTPException(400, "An employee cannot be their own Reporting Manager.")
        _check_manager(db, current_user.company_id, payload.reporting_manager_id, "Reporting Manager")
        if crud.is_manager_of_employee(db, employee_id, payload.reporting_manager_id):
            raise HTTPException(400, "This assignment would create a circular reporting relationship.")
    if changes_dotted_line and payload.dotted_line_manager_id is not None:
        if payload.dotted_line_manager_id == employee_id:
            raise HTTPException(400, "An employee cannot be their own Second Reporting Manager.")
        _check_manager(db, current_user.company_id, payload.dotted_line_manager_id, "Second Reporting Manager")
        if crud.is_manager_of_employee(db, employee_id, payload.dotted_line_manager_id):
            raise HTTPException(400, "This assignment would create a circular reporting relationship.")
    if (
        payload.reporting_manager_id is not None
        and new_dotted_line_manager_id is not None
        and payload.reporting_manager_id == new_dotted_line_manager_id
    ):
        raise HTTPException(400, "Second Reporting Manager must be different from the Reporting Manager.")

    old_reporting_manager_id = employee.reporting_manager_id
    old_dotted_line_manager_id = employee.dotted_line_manager_id
    employee.reporting_manager_id = payload.reporting_manager_id
    if changes_dotted_line:
        employee.dotted_line_manager_id = payload.dotted_line_manager_id
    employee.updated_by = current_user.id
    company = db.get(models.Company, current_user.company_id)
    old_values = {"reporting_manager_id": old_reporting_manager_id}
    new_values = {"reporting_manager_id": payload.reporting_manager_id}
    if changes_dotted_line:
        old_values["dotted_line_manager_id"] = old_dotted_line_manager_id
        new_values["dotted_line_manager_id"] = payload.dotted_line_manager_id
    crud.record_employee_change(db, employee, old_values, new_values)
    audit_changes = {
        "reporting_manager_id": {
            "before": str(old_reporting_manager_id) if old_reporting_manager_id else None,
            "after": str(payload.reporting_manager_id) if payload.reporting_manager_id else None,
        }
    }
    if changes_dotted_line:
        audit_changes["dotted_line_manager_id"] = {
            "before": str(old_dotted_line_manager_id) if old_dotted_line_manager_id else None,
            "after": str(payload.dotted_line_manager_id) if payload.dotted_line_manager_id else None,
        }
    crud.create_audit_log(
        db, company.id, current_user.id, "update", "employee", employee.id,
        changes=audit_changes,
    )
    db.commit()
    db.refresh(employee)
    return _to_out(db, employee)


@router.patch("/{employee_id}/org", response_model=schemas.EmployeeOut)
def update_employee_org(
    employee_id: uuid.UUID,
    payload: schemas.EmployeeOrgUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Employee Profile > Edit Professional Info -- updates branch,
    department, designation, band, employment type/work mode/status,
    and joining/confirmation/probation-end dates. Gated at the UI layer (HR
    on same branch, Branch Manager, or System Admin).

    Business Unit and Shift are intentionally NOT handled here: Business
    Unit isn't a direct employee column (it's derived from the employee's
    Department via Department.business_unit_id, so it changes implicitly
    when department_name changes), and Shift is a separate, date-ranged
    hcm.shift_assignments row rather than a plain field -- both need their
    own dedicated handling rather than a field-update endpoint."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    company = db.get(models.Company, current_user.company_id)
    # F20: validate the optional role change BEFORE writing anything, so the
    # whole Professional Info save is all-or-nothing.
    new_role = None
    if payload.role_name is not None:
        if not role_tiers.is_owner(db, current_user.id):
            acting = crud.get_user_primary_role(db, current_user.id)
            if acting is None or crud.effective_user_matrix(db, current_user).get("system_settings_rbac") not in ("e", "a"):
                raise HTTPException(status_code=403, detail="You don't have permission to change roles")
        new_role = crud.get_role_by_name(db, company.id, payload.role_name)
        if new_role is None:
            raise HTTPException(status_code=409, detail=f"Role '{payload.role_name}' not found")
        target_user = crud.get_core_user_by_employee_id(db, employee_id)
        role_changing = _current_role_id(db, employee_id) != new_role.id
        tier_error = (role_tiers.self_role_change_error(db, current_user, target_user) if role_changing else None) or (
            role_tiers.role_assignment_error(db, current_user, new_role)
        ) or (role_tiers.user_management_error(db, current_user, target_user) if target_user else None)
        if tier_error is not None:
            raise HTTPException(status_code=403, detail=tier_error)
    old_org_values = {
        "branch_id": employee.branch_id, "department_id": employee.department_id,
    }
    new_org_values = {}
    # M-03: every name must resolve (case-insensitive, this company only) --
    # an unknown one is a 422, never a silent 200 that changes nothing.
    # All lookups and checks happen before anything is written.
    branch = dept = desig = None
    if payload.branch_name:
        branch = _by_name(db, models.Branch, company.id, payload.branch_name, "Branch")
    if payload.department_name:
        dept = _by_name(db, models.Department, company.id, payload.department_name, "Department")
    if payload.designation_name:
        desig = _by_name(db, models.Designation, company.id, payload.designation_name, "Designation")
    band_problem = ev.band_error(db, company.id, payload.band)
    if band_problem:
        raise HTTPException(status_code=422, detail=band_problem)
    new_doj = ev.as_date(payload.date_of_joining) if payload.date_of_joining else None
    date_problem = (
        (ev.joining_date_error(new_doj) or ev.dob_error(employee.date_of_birth, new_doj)) if new_doj else None
    )
    if date_problem:
        raise HTTPException(status_code=422, detail=date_problem)
    if branch is not None:
        employee.branch_id = branch.id
        new_org_values["branch_id"] = branch.id
    if dept is not None:
        employee.department_id = dept.id
        new_org_values["department_id"] = dept.id
    promoted_to: str | None = None
    old_designation_name: str | None = None
    if desig is not None and desig.id != employee.designation_id:
        old = db.get(models.Designation, employee.designation_id) if employee.designation_id else None
        old_designation_name = old.name if old else None
        employee.designation_id = desig.id
        promoted_to = desig.name
    if payload.band is not None:
        employee.band = payload.band
    if payload.employment_type is not None:
        employee.employment_type = payload.employment_type
    if payload.work_mode is not None:
        employee.work_mode = payload.work_mode
    status_changed = False
    if payload.status is not None:
        status_changed = access_lifecycle.normalize_status(payload.status) != access_lifecycle.normalize_status(
            employee.status
        )
        employee.status = payload.status
    if payload.date_of_joining is not None:
        parsed = crud.parse_lenient_date(payload.date_of_joining)
        if parsed is not None:
            employee.date_of_joining = parsed
    if payload.confirmation_date is not None:
        employee.confirmation_date = crud.parse_lenient_date(payload.confirmation_date)
    if payload.probation_end_date is not None:
        employee.probation_end_date = crud.parse_lenient_date(payload.probation_end_date)
    employee.updated_by = current_user.id
    crud.record_employee_change(db, employee, old_org_values, new_org_values)
    crud.create_audit_log(
        db, company.id, current_user.id, "update", "employee", employee.id,
        changes={k: str(v) for k, v in new_org_values.items()} or None,
    )
    if new_role is not None and _current_role_id(db, employee_id) != new_role.id:
        try:
            crud.reassign_employee_role(db, employee_id, payload.role_name, company.id)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        crud.create_audit_log(db, company.id, current_user.id, "update", "role_assignment", employee_id)
        _end_sessions_after_role_change(db, employee_id)
    if status_changed:
        # Inactive / terminated / exited / ... ends login access and every
        # open session; back to an employed status re-enables the login.
        access_change = access_lifecycle.sync_login_access(db, employee)
        if access_change is not None:
            crud.create_audit_log(
                db, company.id, current_user.id,
                "access_revoked" if access_change == "deactivated" else "access_restored",
                "employee", employee.id, changes={"status": employee.status},
            )
    db.commit()
    db.refresh(employee)
    if promoted_to is not None:
        actor = db.get(models.Employee, current_user.employee_id) if current_user.employee_id else None
        es.send_promotion_announcement_email(
            employee.work_email or employee.personal_email,
            db=db, company_id=company.id, employee_id=employee.id,
            employee_name=f"{employee.first_name} {employee.last_name}".strip(),
            old_designation=old_designation_name, new_designation=promoted_to,
            decided_by=f"{actor.first_name} {actor.last_name}".strip() if actor else None,
        )
    return _to_out(db, employee)


def _current_role_id(db: Session, employee_id: uuid.UUID) -> uuid.UUID | None:
    """The employee's current (primary) role id, or None without a login."""
    target_user = crud.get_core_user_by_employee_id(db, employee_id)
    role = crud.get_user_primary_role(db, target_user.id) if target_user is not None else None
    return role.id if role is not None else None


def _end_sessions_after_role_change(db: Session, employee_id: uuid.UUID) -> None:
    """AUTH-A2: a role change ends that user's existing sessions, same as
    POST /api/users/{id}/roles (their next request re-authenticates and
    picks up the new role). Caller commits."""
    target_user = crud.get_core_user_by_employee_id(db, employee_id)
    if target_user is not None:
        auth_state.bump_token_version(db, auth_state.public_user_id_for_core_user(db, target_user))


def _contract_action_out(db: Session, employee: models.Employee, event, *, converted: bool = False):
    return schemas.ContractActionOut(
        employee_id=employee.id, employment_type=employee.employment_type,
        contract_end_date=employee.contract_end_date,
        contract_rate_amount=float(employee.contract_rate_amount) if employee.contract_rate_amount is not None else None,
        contract_rate_unit=employee.contract_rate_unit, event_id=event.id,
        needs_salary_structure=converted and not crud.has_salary_structure(db, employee.id),
    )


@router.post("/{employee_id}/contract/renew", response_model=schemas.ContractActionOut)
def renew_employee_contract(
    employee_id: uuid.UUID,
    payload: schemas.ContractRenewRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """HR renews a contract employee's contract: a later end date and,
    optionally, a new rate. Never automatic. Logged to employee history."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    try:
        event = crud.renew_contract(db, employee, current_user, new_end_date=payload.new_end_date,
                                    new_rate_amount=payload.new_rate_amount, new_rate_unit=payload.new_rate_unit,
                                    notes=payload.notes)
    except crud.ContractActionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    db.commit()
    db.refresh(employee)
    return _contract_action_out(db, employee, event)


@router.post("/{employee_id}/contract/convert-to-permanent", response_model=schemas.ContractActionOut)
def convert_employee_contract_to_permanent(
    employee_id: uuid.UUID,
    payload: schemas.ContractConvertRequest | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """HR converts a contract employee to permanent full-time: employment
    type -> full_time, contract end date and rate cleared. Logged to
    employee history. A contract that is NOT renewed goes through the
    existing offboarding / exit flow instead (unchanged)."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    try:
        event = crud.convert_contract_to_permanent(db, employee, current_user,
                                                   notes=payload.notes if payload else None)
    except crud.ContractActionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    db.commit()
    db.refresh(employee)
    return _contract_action_out(db, employee, event, converted=True)


@router.post("/{employee_id}/transfer", response_model=schemas.EmployeeOut)
def transfer_employee(
    employee_id: uuid.UUID,
    payload: schemas.EmployeeTransferRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("transfer")),
):
    """Purpose-built Employee Transfer action -- reassigns any of
    branch/business unit/department/sub-department/reporting manager/
    dotted-line manager in one call and always leaves a
    hcm.employee_lifecycle_events row behind (crud.record_employee_change),
    unlike PATCH /org and PATCH /hierarchy which only cover a subset of
    these fields each. Only fields present in the request are changed."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    old_values = {
        "branch_id": employee.branch_id,
        "business_unit_id": employee.department.business_unit_id if employee.department else None,
        "department_id": employee.department_id,
        "sub_department_id": employee.sub_department_id,
        "reporting_manager_id": employee.reporting_manager_id,
        "dotted_line_manager_id": employee.dotted_line_manager_id,
    }
    # H-21: every target must belong to this company; managers active, not
    # the employee, not in a loop. Validated before anything is written.
    cid = current_user.company_id
    for key, model, label in (("branch_id", models.Branch, "Branch"),
                              ("department_id", models.Department, "Department")):
        if updates.get(key) is not None:
            row = db.get(model, updates[key])
            if row is None or row.company_id != cid:
                raise HTTPException(status_code=422, detail=f"{label} not found in your company.")
    if updates.get("sub_department_id") is not None:
        sub = db.get(models.SubDepartment, updates["sub_department_id"])
        dept_id = updates.get("department_id", employee.department_id)
        if sub is None or sub.department_id != dept_id:
            raise HTTPException(status_code=422, detail="Sub-department not found in the employee's department.")
    for key, label in (("reporting_manager_id", "Reporting Manager"),
                       ("dotted_line_manager_id", "Second Reporting Manager")):
        if updates.get(key) is not None:
            _check_manager(db, cid, updates[key], label, employee_id=employee_id)
            if crud.is_manager_of_employee(db, employee_id, updates[key]):
                raise HTTPException(400, "This assignment would create a circular reporting relationship.")
    new_rm = updates.get("reporting_manager_id", employee.reporting_manager_id)
    new_dl = updates.get("dotted_line_manager_id", employee.dotted_line_manager_id)
    if new_rm is not None and new_rm == new_dl:
        raise HTTPException(400, "Second Reporting Manager must be different from the Reporting Manager.")
    if "branch_id" in updates:
        employee.branch_id = updates["branch_id"]
    if "department_id" in updates:
        employee.department_id = updates["department_id"]
    if "sub_department_id" in updates:
        employee.sub_department_id = updates["sub_department_id"]
    if "reporting_manager_id" in updates:
        employee.reporting_manager_id = updates["reporting_manager_id"]
    if "dotted_line_manager_id" in updates:
        employee.dotted_line_manager_id = updates["dotted_line_manager_id"]
    # business_unit_id lives on Department, not Employee -- a transfer that
    # only changes business_unit_id (employee stays in the same department)
    # isn't representable as an employee-row field change, so it's recorded
    # in history but has no employee column of its own to write.
    new_values = {k: v for k, v in updates.items() if k in crud._TRACKED_TRANSFER_FIELDS}
    employee.updated_by = current_user.id
    crud.record_employee_change(db, employee, old_values, new_values)
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "employee_transfer", employee.id,
        changes={k: str(v) if v is not None else None for k, v in updates.items()},
    )
    db.commit()
    db.refresh(employee)
    return _to_out(db, employee)


@router.patch("/{employee_id}/hierarchy", response_model=schemas.EmployeeHierarchyOut)
def update_employee_hierarchy(
    employee_id: uuid.UUID,
    payload: schemas.EmployeeHierarchyUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Reporting Hierarchy > Edit -- routes each FK to the owning model
    (department / branch / company / project allocation). Gated at the UI
    layer (HR on same branch, Branch Manager, or System Admin)."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    for key, label in (("hr_representative_id", "HR Representative"), ("senior_manager_id", "Senior Manager"),
                       ("branch_manager_id", "Branch Manager"), ("branch_head_id", "Branch Head")):
        _check_manager(db, current_user.company_id, updates.get(key), label)  # H-21
    if "hr_representative_id" in updates and employee.department:
        employee.department.hr_representative_id = updates["hr_representative_id"]
    if "senior_manager_id" in updates and employee.department:
        employee.department.senior_manager_id = updates["senior_manager_id"]
    if "branch_manager_id" in updates and employee.branch:
        employee.branch.branch_manager_id = updates["branch_manager_id"]
    if "branch_head_id" in updates:
        company = db.get(models.Company, current_user.company_id)
        if company:
            company.branch_head_id = updates["branch_head_id"]
    if "team_lead_id" in updates or "project_manager_id" in updates:
        allocation = crud.get_primary_project_allocation(db, employee_id)
        if allocation:
            if "team_lead_id" in updates:
                _apply_project_team_lead(
                    db, current_user.company_id, allocation, updates["team_lead_id"]
                )
            if "project_manager_id" in updates:
                _require_company_employee(
                    db, updates["project_manager_id"], current_user.company_id, "Project Manager"
                )
                project = db.get(models.Project, allocation.project_id)
                if project:
                    project.project_manager_id = updates["project_manager_id"]
    company = db.get(models.Company, current_user.company_id)
    employee.updated_by = current_user.id
    crud.create_audit_log(
        db, company.id, current_user.id, "update", "employee_hierarchy", employee.id,
        changes={k: str(v) if v is not None else None for k, v in updates.items()},
    )
    db.commit()
    db.refresh(employee)
    chain = org_hierarchy.resolve_employee_hierarchy(db, employee)
    return schemas.EmployeeHierarchyOut(
        employee_id=employee.id,
        hr_representative=_node_out(chain.hr_representative),
        team_lead=_node_out(chain.team_lead),
        project_manager=_node_out(chain.project_manager),
        senior_manager=_node_out(chain.senior_manager),
        branch_manager=_node_out(chain.branch_manager),
        branch_head=_node_out(chain.branch_head),
    )


@router.patch("/{employee_id}/team-lead", response_model=schemas.EmployeeOut)
def update_employee_team_lead(
    employee_id: uuid.UUID,
    payload: schemas.ProjectAllocationTeamLeadUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Edit Project Allocation > Team Lead -- updates team_lead_id on the
    employee's primary project allocation."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    allocation = crud.get_primary_project_allocation(db, employee_id)
    if allocation is None:
        raise HTTPException(status_code=409, detail="Employee has no project allocation")
    allocation_id = allocation.id
    _apply_project_team_lead(db, current_user.company_id, allocation, payload.team_lead_id)
    employee.updated_by = current_user.id
    company = db.get(models.Company, current_user.company_id)
    crud.create_audit_log(
        db, company.id, current_user.id, "update", "project_allocation", allocation_id,
        changes={"team_lead_id": str(payload.team_lead_id) if payload.team_lead_id else None},
    )
    db.commit()
    db.refresh(employee)
    return _to_out(db, employee)


@router.patch("/{employee_id}/project-manager", response_model=schemas.EmployeeOut)
def update_employee_project_manager(
    employee_id: uuid.UUID,
    payload: schemas.ProjectAllocationPmUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Edit Project Allocation > Project Manager -- updates
    project_manager_id on the project behind the employee's primary
    allocation (shared by every employee on that project)."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    allocation = crud.get_primary_project_allocation(db, employee_id)
    if allocation is None:
        raise HTTPException(status_code=409, detail="Employee has no project allocation")
    _require_company_employee(db, payload.project_manager_id, current_user.company_id, "Project Manager")
    project = db.get(models.Project, allocation.project_id)
    project.project_manager_id = payload.project_manager_id
    employee.updated_by = current_user.id
    company = db.get(models.Company, current_user.company_id)
    crud.create_audit_log(db, company.id, current_user.id, "update", "project", project.id)
    db.commit()
    db.refresh(employee)
    return _to_out(db, employee)


@router.patch(
    "/{employee_id}/role",
    response_model=schemas.RoleOut,
)
def update_employee_role(
    employee_id: uuid.UUID,
    payload: schemas.EmployeeRoleUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    company = db.get(models.Company, current_user.company_id)
    # SEC-01: Owner-tier reservation -- same rules as POST /api/users/{id}/roles.
    new_role = crud.get_role_by_name(db, company.id, payload.role_name)
    target_user = crud.get_core_user_by_employee_id(db, employee_id)
    role_changing = new_role is not None and _current_role_id(db, employee_id) != new_role.id
    tier_error = (role_tiers.self_role_change_error(db, current_user, target_user) if role_changing else None) or (
        role_tiers.role_assignment_error(db, current_user, new_role) if new_role else None
    ) or (role_tiers.user_management_error(db, current_user, target_user) if target_user else None)
    if tier_error is not None:
        raise HTTPException(status_code=403, detail=tier_error)
    try:
        unchanged = new_role is not None and _current_role_id(db, employee_id) == new_role.id
        role = crud.reassign_employee_role(db, employee_id, payload.role_name, company.id)
        if not unchanged:
            crud.create_audit_log(db, company.id, current_user.id, "update", "role_assignment", employee_id)
            _end_sessions_after_role_change(db, employee_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(role)
    return role


@router.patch(
    "/{employee_id}/profile",
    response_model=schemas.EmployeeProfileOut,
)
def update_employee_profile(
    employee_id: uuid.UUID,
    payload: schemas.EmployeeProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self_or_manager("edit")),
):
    """Backs Employee Profile > Edit Profile (previously decorative). Only
    non-sensitive personal-info/emergency-contact fields live here, so this
    is also reachable by the employee's Reporting Manager / Dotted-Line
    Manager (see require_people_access_or_self_or_manager)."""
    employee = _require_employee(db, employee_id, current_user.company_id)

    emergency_fields = {
        "emergency_contact_name",
        "emergency_contact_relation",
        "emergency_contact_phone",
    }
    updates = payload.model_dump(exclude_unset=True, exclude=emergency_fields)
    # date_of_birth arrives as a lenient string (same formats EmployeeCreate
    # accepts) but core.employees.date_of_birth is a real Date column --
    # parse before the generic setattr loop in crud.update_employee_profile
    # would otherwise try to write a raw string into it.
    if "date_of_birth" in updates:
        # M-02: the schema already 422s a non-empty value that isn't a date;
        # '' / null clears it on purpose.
        updates["date_of_birth"] = (
            crud.parse_lenient_date(updates["date_of_birth"])
            if updates["date_of_birth"]
            else None
        )
        dob_problem = ev.dob_error(updates["date_of_birth"], employee.date_of_joining)
        if dob_problem:
            raise HTTPException(status_code=422, detail=dob_problem)
    crud.update_employee_profile(db, employee, updates)

    contact = None
    provided_emergency = payload.model_dump(exclude_unset=True, include=emergency_fields)
    if provided_emergency:
        contact = crud.upsert_emergency_contact(
            db,
            employee.company_id,
            employee.id,
            payload.emergency_contact_name,
            payload.emergency_contact_relation,
            payload.emergency_contact_phone,
        )
    else:
        contact = crud.get_emergency_contact(db, employee.id)

    crud.create_audit_log(db, employee.company_id, current_user.id, "update", "employee_profile", employee.id)
    db.commit()
    db.refresh(employee)

    return schemas.EmployeeProfileOut(
        date_of_birth=employee.date_of_birth.isoformat() if employee.date_of_birth else None,
        gender=employee.gender,
        personal_email=employee.personal_email,
        personal_phone=employee.personal_phone,
        current_address=employee.current_address,
        permanent_address=employee.permanent_address,
        blood_group=employee.blood_group,
        nationality=employee.nationality,
        marital_status=employee.marital_status,
        emergency_contact_name=contact.name if contact else None,
        emergency_contact_relation=contact.designation if contact else None,
        emergency_contact_phone=contact.phone if contact else None,
    )


@router.post(
    "/{employee_id}/documents",
    response_model=schemas.DocumentRecordOut,
    status_code=201,
)
def upload_employee_document(  # API-06: sync def -> threadpool (blocking file/DB I/O)
    employee_id: uuid.UUID,
    document_type: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Backs Upload Document (People > Employee Documents)."""
    _require_employee(db, employee_id, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    if visible_ids is not None and employee_id not in visible_ids:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to upload documents for this employee.",
        )
    company = db.get(models.Company, current_user.company_id)
    file_url, _size_bytes = save_uploaded_file(
        file, entity_type="employee_document", entity_id=employee_id
    )
    record = crud.create_document_record(
        db,
        employee_id=employee_id,
        document_type=document_type,
        status="Verified",
        uploaded_on=crud.company_today(db, company.id),
        file_url=file_url,
    )
    crud.create_audit_log(db, company.id, current_user.id, "create", "employee_document", record.id)
    db.commit()
    db.refresh(record)
    return record


@router.delete("/{employee_id}/documents/{document_id}", status_code=204)
def delete_employee_document(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Sibling of upload_employee_document above -- same visibility gate,
    for removing a document uploaded in error (e.g. wrong file/type)."""
    _require_employee(db, employee_id, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    if visible_ids is not None and employee_id not in visible_ids:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to delete documents for this employee.",
        )
    company = db.get(models.Company, current_user.company_id)
    record = crud.get_document_record(db, employee_id, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found")
    crud.create_audit_log(db, company.id, current_user.id, "delete", "employee_document", document_id)
    crud.delete_document_record(db, document_id)
    db.commit()


# Deliberately tighter/narrower than storage.py's shared
# _ALLOWED_EXTENSIONS (which also permits pdf/doc/xls/etc for document
# uploads) -- a profile picture must actually be an image, and the error
# message below should say so specifically rather than listing every
# document type this app happens to accept elsewhere.
_ALLOWED_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_MAX_PHOTO_BYTES = 5 * 1024 * 1024


@router.post("/{employee_id}/photo")
def upload_employee_photo(  # API-06: sync def -> threadpool (blocking file/DB I/O)
    employee_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self_or_manager("edit")),
):
    """Employee Profile > Profile Picture. Self-service (or HR/manager-on-
    their-behalf), same non-sensitive-field gate as Education & Experience.
    Replaces any existing photo -- the old file is deleted from disk so
    changing your picture repeatedly doesn't leak storage."""
    _require_employee(db, employee_id, current_user.company_id)
    extension = os.path.splitext(file.filename or "")[1].lower()
    if extension not in _ALLOWED_PHOTO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Only JPG and PNG images are supported for profile pictures.",
        )
    file_url, size_bytes = save_uploaded_file(
        file, entity_type="employee_photo", entity_id=employee_id
    )
    if size_bytes > _MAX_PHOTO_BYTES:
        delete_uploaded_file(file_url)
        raise HTTPException(
            status_code=400,
            detail=f"Profile picture must be smaller than {_MAX_PHOTO_BYTES // (1024 * 1024)}MB.",
        )
    company = db.get(models.Company, current_user.company_id)
    previous_url = crud.set_employee_photo(db, employee_id, file_url)
    crud.create_audit_log(db, company.id, current_user.id, "update", "employee_photo", employee_id)
    db.commit()
    if previous_url:
        delete_uploaded_file(previous_url)
    return {"photo_url": file_url}


@router.delete("/{employee_id}/photo", status_code=204)
def delete_employee_photo(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self_or_manager("edit")),
):
    """Sibling of upload_employee_photo above -- same gate, for removing the
    current profile picture (falls back to the initials avatar)."""
    _require_employee(db, employee_id, current_user.company_id)
    company = db.get(models.Company, current_user.company_id)
    previous_url = crud.clear_employee_photo(db, employee_id)
    crud.create_audit_log(db, company.id, current_user.id, "update", "employee_photo", employee_id)
    db.commit()
    if previous_url:
        delete_uploaded_file(previous_url)


# ─── Per-module profile endpoints (lazy-load on the profile screen) ──────────

def _require_employee(
    db: Session, employee_id: uuid.UUID, company_id: uuid.UUID
) -> models.Employee:
    """Fetches employee_id and verifies it belongs to company_id -- every
    endpoint keyed on {employee_id} must call this (or the equivalent inline
    check) before touching the row. Without the company_id check, any
    authenticated user of any company could read/edit any other company's
    employee record by UUID. Returns 404 (not 403) so a cross-tenant guess
    is indistinguishable from a nonexistent id."""
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != company_id:
        raise HTTPException(404, "Employee not found")
    return employee


def _require_company_employee(
    db: Session, employee_id: uuid.UUID | None, company_id: uuid.UUID, field_label: str
) -> None:
    """Body-FK counterpart to _require_employee: a Team Lead / Project
    Manager id must be an employee of the caller's company (None = clear)."""
    if employee_id is None:
        return
    target = db.get(models.Employee, employee_id)
    if target is None or target.company_id != company_id:
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} does not refer to a valid employee in your company.",
        )
    if access_lifecycle.employee_access_blocked(target):
        raise HTTPException(status_code=422, detail=f"{field_label} must be an active employee.")


def _check_manager(
    db: Session, company_id: uuid.UUID, manager_id: uuid.UUID | None, label: str,
    employee_id: uuid.UUID | None = None,
) -> None:
    """H-21: a manager / leader reference must be an ACTIVE employee of the
    caller's company (422 otherwise; None = clear)."""
    error = ev.manager_error(db, company_id, manager_id, label, employee_id=employee_id)
    if error:
        raise HTTPException(status_code=422, detail=error)


def _by_name(db: Session, model, company_id: uuid.UUID, name: str, label: str):
    """Case-insensitive lookup of a Branch / Department / Designation by
    name within the company; 422 when it doesn't exist (M-03)."""
    from sqlalchemy import func, select

    row = db.scalar(
        select(model).where(model.company_id == company_id, func.lower(model.name) == name.strip().lower())
    )
    if row is None:
        raise HTTPException(status_code=422, detail=f"{label} '{name}' not found.")
    return row


def _apply_project_team_lead(
    db: Session,
    company_id: uuid.UUID,
    allocation: models.ProjectAllocation,
    team_lead_id: uuid.UUID | None,
) -> None:
    """Persist a Team Lead change for the project behind `allocation`.

    pm_resource_allocations has no team_lead_id column (the old
    `allocation.team_lead_id = ...` only set a plain Python attribute and
    was silently lost). A project's Team Lead is the allocation carrying
    the "Team Lead" project-role -- the same model routers/projects.py
    uses -- so route through crud.set_project_team_lead. Clearing (None)
    demotes the current Team Lead allocation(s) to a plain member instead
    of removing the person from the project."""
    _require_company_employee(db, team_lead_id, company_id, "Team Lead")
    project_id = allocation.project_id
    current = crud.resolve_project_team_lead_id(db, project_id)
    if team_lead_id == current:
        return
    try:
        if team_lead_id is None:
            for alloc in crud.list_project_allocations(db, project_id):
                if alloc.project_role is not None and alloc.project_role.name == "Team Lead":
                    alloc.project_role_id = None
            db.flush()
        else:
            project = db.get(models.Project, project_id)
            crud.set_project_team_lead(
                db, company_id, project_id, team_lead_id,
                start_date=project.planned_start_date if project else None,
            )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{employee_id}/overview")
def get_employee_overview(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    # require_people_access_or_self, not require_people_access: this is the
    # Employee Profile screen's own tabs -- a caller must always be able to
    # view their OWN record here regardless of whether their company has
    # since configured a People access policy that excludes their Role
    # (e.g. after the RBAC default template grants some other Role an
    # 'employee.*' action, which flips can_access_people_module's
    # company-wide switch from "open to everyone" to "restricted").
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    overview = crud.get_employee_overview(db, employee_id)
    if not crud.can_view_employee_payroll(db, current_user, employee_id):
        overview["ctc"] = 0  # pay data only for those who may see it (C2)
    return overview


@router.get("/{employee_id}/personal")
def get_employee_personal(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_personal(db, employee_id)


@router.get("/{employee_id}/professional")
def get_employee_professional(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_professional(db, employee_id)


@router.get("/{employee_id}/education")
def get_employee_education(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_education(db, employee_id)


@router.patch("/{employee_id}/education")
def update_employee_education(
    employee_id: uuid.UUID,
    payload: schemas.EmployeeEducationUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self_or_manager("edit")),
):
    """Backs Employee Profile > Edit Education & Experience (previously
    decorative -- the dialog called this exact route/body shape but it
    never existed). Non-sensitive/non-financial, so also reachable by the
    employee's Reporting Manager / Dotted-Line Manager (see
    require_people_access_or_self_or_manager)."""
    _require_employee(db, employee_id, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    crud.update_employee_education(db, employee_id, updates)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "employee_education", employee_id)
    db.commit()
    return crud.get_employee_education(db, employee_id)


@router.get("/{employee_id}/payroll")
def get_employee_payroll(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    # C2: CTC / PAN / bank details -- deny by default (crud.can_view_employee_payroll).
    if not crud.can_view_employee_payroll(db, current_user, employee_id):
        raise HTTPException(status_code=403, detail="You don't have permission to do this")
    return crud.get_employee_payroll(db, employee_id)


@router.patch(
    "/{employee_id}/payroll",
    response_model=schemas.EmployeePayrollOut,
)
def update_employee_payroll(
    employee_id: uuid.UUID,
    payload: schemas.EmployeePayrollUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Backs Employee Profile > Edit Bank Details (previously decorative --
    the dialog called this exact route/body shape but it never existed)."""
    employee = _require_employee(db, employee_id, current_user.company_id)

    updates = payload.model_dump(exclude_unset=True)
    field_map = {"pf": "uan", "esi": "esi_number"}
    updates = {field_map.get(key, key): value for key, value in updates.items()}
    crud.update_employee_profile(db, employee, updates)

    crud.create_audit_log(db, employee.company_id, current_user.id, "update", "employee_payroll", employee.id)
    db.commit()
    db.refresh(employee)

    return crud.get_employee_payroll(db, employee.id)


@router.get("/{employee_id}/benefits")
def get_employee_benefits(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_benefits(db, employee_id)


@router.get("/{employee_id}/attendance")
def get_employee_attendance(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_attendance(db, employee_id)


@router.get("/{employee_id}/projects")
def get_employee_projects(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_projects(db, employee_id, current_user.company_id)


@router.get("/{employee_id}/performance")
def get_employee_performance(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_performance(db, employee_id)


@router.get("/{employee_id}/lifecycle")
def get_employee_lifecycle(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_lifecycle(db, employee_id)


@router.get("/{employee_id}/documents")
def get_employee_documents(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    if visible_ids is not None and employee_id not in visible_ids:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to view documents for this employee.",
        )
    return crud.get_employee_documents(db, employee_id)


@router.get("/{employee_id}/assets")
def get_employee_assets(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_assets(db, employee_id)


@router.get("/{employee_id}/leave")
def get_employee_leave(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    _require_employee(db, employee_id, current_user.company_id)
    return crud.get_employee_leave(db, employee_id)
