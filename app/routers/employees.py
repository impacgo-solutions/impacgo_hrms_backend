import logging
import mimetypes
import os
import uuid
from datetime import date, datetime, timezone

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import access_lifecycle, auth_state, crud, currencies, form_config, models, notification_settings, org_hierarchy, role_tiers, schemas
from .. import email_service as es
from .. import employee_validation as ev
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, _OWNER_ROLE_NAME, require_employee_visible, get_current_user, require_people_access, require_people_access_to_employee, require_people_access_or_self, require_people_access_or_self_or_manager, require_permission, require_permission_or_action
from ..storage import delete_uploaded_file, save_uploaded_file, uploads_root, winlong_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/employees", tags=["employees"])


def _to_out(db: Session, e: models.Employee) -> schemas.EmployeeOut:
    custom = form_config.load_custom_values(db, e.company_id, "employee", [e.id]).get(e.id, {})
    return schemas.EmployeeOut(
        custom_fields=custom,
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


@router.get("/education-options")
def education_options(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """Dropdown values for Employee Details > Education & Experience. The
    Domain / Industry list is the company's Form Configuration list
    (employee.experience_domain); level / range are fixed (validated closed sets)."""
    return {
        "educationLevels": [{"value": v, "label": label} for v, label in schemas.EDUCATION_LEVELS],
        "experienceRanges": [{"value": v, "label": label} for v, label in schemas.EXPERIENCE_RANGES],
        "domains": [v for v, _ in form_config.options(db, current_user.company_id, "employee.experience_domain")],
        "maxExperienceYears": schemas.MAX_EXPERIENCE_YEARS,
    }


@router.get("", response_model=schemas.EmployeeListPage)
def list_employees(
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    # Employee lookup: match the Employee ID and / or the Name on their own
    # columns. Both given = both must match (AND); either alone narrows by
    # just that one. `search` (any of code / name) keeps working unchanged.
    employee_code: Optional[str] = Query(None, max_length=50),
    name: Optional[str] = Query(None, max_length=120),
    dept: Optional[str] = Query(None),
    branch: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    # Directory filters backed by Form Configuration options.
    employment_type: Optional[str] = Query(None, max_length=40),
    work_mode: Optional[str] = Query(None, max_length=40),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access("view")),
):
    company = db.get(models.Company, current_user.company_id)
    # Same visibility as GET /full (a Manager: their reporting subtree +
    # self), applied in the query; CTC only where the caller may see pay.
    try:
        page = crud.list_employees_paginated(
            db, company.id, limit=limit, cursor=cursor,
            search=search, employee_code=employee_code, name=name,
            dept=dept, branch=branch, status=status,
            employment_type=employment_type, work_mode=work_mode,
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
            kind = "Contract Extended" if e.event_type == "contract_renewal" else "Converted to Permanent"
            from_value = (f"Contract to {e.from_contract_end_date.isoformat()}"
                          if e.from_contract_end_date else "Contract")
            to_value = (f"Contract to {e.to_contract_end_date.isoformat()}"
                        if e.event_type == "contract_renewal" else "Permanent (Full-time)")
        elif e.event_type == "contract_expired":
            kind = "Contract Expired"
            from_value = (f"Contract to {e.from_contract_end_date.isoformat()}"
                          if e.from_contract_end_date else "Contract")
            to_value = f"Expired on {e.event_date.isoformat()}" + (f" — {e.notes}" if e.notes else "")
        else:
            # Branch / business-unit / sub-department transfers, manager
            # changes, CTC-only promotions: whatever this row actually changed.
            kind = e.event_type.replace("_", " ").title()
            from_value, to_value = crud.lifecycle_event_from_to(e, row["manager_names"])
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


_LIFECYCLE_REF_MODELS = {
    "designation_id": (models.Designation, "Designation"),
    "department_id": (models.Department, "Department"),
    "branch_id": (models.Branch, "Branch"),
    "business_unit_id": (models.BusinessUnit, "Business Unit"),
}


def _validate_lifecycle_refs(db: Session, company_id: uuid.UUID, updates: dict) -> None:
    """Every id written into a lifecycle record must belong to the caller's
    company (400 otherwise) -- never another tenant's row."""
    for field, value in updates.items():
        if value is None or field in ("event_date", "from_ctc", "to_ctc"):
            continue
        suffix = field.split("_", 1)[1]  # drop the from_/to_ prefix
        if suffix in _LIFECYCLE_REF_MODELS:
            model, label = _LIFECYCLE_REF_MODELS[suffix]
            row = db.get(model, value)
            valid = row is not None and row.company_id == company_id
        elif suffix == "sub_department_id":
            label = "Sub-department"
            row = db.get(models.SubDepartment, value)
            parent = db.get(models.Department, row.department_id) if row is not None else None
            valid = parent is not None and parent.company_id == company_id
        elif suffix in ("reporting_manager_id", "dotted_line_manager_id"):
            label = "Manager"
            row = db.get(models.Employee, value)
            valid = row is not None and row.company_id == company_id
        else:
            continue
        if not valid:
            raise HTTPException(
                status_code=400, detail=f"{label} does not refer to a valid record in your company."
            )


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
    # Same visibility the read endpoint applies: a transfer-capable caller
    # may not edit the history of an employee outside their People scope.
    require_employee_visible(db, current_user, event.employee_id)
    updates = payload.model_dump(exclude_unset=True)
    _validate_lifecycle_refs(db, current_user.company_id, updates)
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
    "accountHolder": "—", "bankBranch": "—",
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
    # Form Configuration: Employment Type / Work Mode must be ACTIVE options of
    # this company, and the admin-defined custom fields must validate -- both
    # checked before anything is written.
    payload.employment_type, payload.work_mode = ev.check_employee_options(
        db, company.id, employment_type=payload.employment_type, work_mode=payload.work_mode)
    # Gender / Marital Status come from the configured lists (Nationality is a
    # free-text box on this form, so only the profile editor's list checks it).
    _personal = ev.check_personal_options(
        db, company.id, {"gender": payload.gender, "marital_status": payload.marital_status})
    payload.gender, payload.marital_status = _personal["gender"], _personal["marital_status"]
    if payload.domain:
        payload.domain = ev.check_experience_domains(db, company.id, [payload.domain])[0]
    custom_values = form_config.validate_custom_values(db, company.id, "employee", payload.custom_fields)
    # Freelancer and Contract relax every business-mandatory field
    # (schemas.EmployeeCreate._freelancer_relaxation already enforces this at
    # the Pydantic layer) -- an admin's Field Rules config (e.g. Blood Group/
    # Nationality marked mandatory) must not override that and re-block them.
    # Every other type is only held to fields an admin explicitly marked
    # mandatory there (the built-in defaults only require login identity).
    employment_type = ev.normalize_employment_type(payload.employment_type)
    if employment_type == "Contract":
        # A Contract is defined by its term: both dates stay required here.
        missing = [name for name, value in (("contract_start_date", payload.contract_start_date),
                                            ("contract_end_date", payload.contract_end_date)) if value is None]
        if missing:
            verb = "is" if len(missing) == 1 else "are"
            raise HTTPException(status_code=422, detail=f"{', '.join(missing)} {verb} required.")
        if payload.contract_end_date < crud.company_today(db, company.id):
            raise HTTPException(status_code=422, detail="The contract end date can't be in the past.")
    if employment_type not in ("Freelancer", "Contract"):
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
    # Administration > General Settings > Employee ID Format: a type set to
    # Manual gets no generated ID from this form -- one must be typed in.
    # Enforced here only (not in crud.create_employee), so recruitment's
    # Create Employee and tenant provisioning still auto-generate as before.
    id_format = crud.get_employee_id_format(db, company.id, employment_type=payload.employment_type)
    if (id_format.generation_mode or "auto") == "manual" and not (payload.employee_code or "").strip():
        raise HTTPException(
            status_code=422,
            detail=f"Employee ID is required -- it is entered manually for "
                   f"{payload.employment_type} employees.",
        )
    if payload.rate_amount is not None and ev.normalize_employment_type(payload.employment_type) != "Freelancer":
        raise HTTPException(status_code=422, detail="An hourly rate can only be set for Freelancer employees.")
    try:
        rate_currency = currencies.normalize_code(payload.rate_currency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        employee = crud.create_employee(db, company.id, payload)
        employee.created_by = current_user.id
        form_config.save_custom_values(db, company.id, "employee", employee.id, custom_values)
        if employment_type == "Contract":
            employee.contract_start_date = payload.contract_start_date
            employee.contract_end_date = payload.contract_end_date
            employee.contract_status = "active"
        if payload.rate_amount is not None:
            crud.set_employee_rate(
                db, company.id, employee, payload.rate_amount, payload.rate_unit or "hourly",
                employee.date_of_joining, None, current_user.id, currency_code=rate_currency,
            )
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
        # The unique index on (company_id, employee_code) caught a duplicate
        # that was inserted concurrently, after the checks above had passed.
        if "employee_code" in str(exc.orig):
            typed = (payload.employee_code or "").strip()
            raise HTTPException(
                status_code=409,
                detail=f"Employee ID '{typed}' already exists." if typed else
                       "That Employee ID was just taken by another new employee — please save again.",
            ) from exc
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
        hr_representatives=[_node_out(n) for n in chain.hr_representatives],
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
    # A status change can end the account's login (Inactive / Terminated ...):
    # same guard as the contract actions -- never your own, and never an
    # account with more access than yours (e.g. the Owner's).
    def _canonical_status(value):
        try:
            return access_lifecycle.normalize_status(ev.normalize_status(value))
        except ValueError:
            return access_lifecycle.normalize_status(value)

    if payload.status is not None and _canonical_status(payload.status) != _canonical_status(employee.status):
        if current_user.employee_id is not None and current_user.employee_id == employee.id:
            raise HTTPException(status_code=403, detail="You can't change your own employment status.")
        status_target = crud.get_core_user_by_employee_id(db, employee_id)
        if status_target is not None:
            status_error = role_tiers.user_management_error(db, current_user, status_target)
            if status_error is not None:
                raise HTTPException(status_code=403, detail=status_error)
    # M-03: every name must resolve (case-insensitive, this company only) --
    # an unknown one is a 422, never a silent 200 that changes nothing.
    # All lookups and checks happen before anything is written.
    branch = dept = desig = None
    if payload.branch_name:
        branch = _by_name(db, models.Branch, company.id, payload.branch_name, "Branch")
    if payload.department_name:
        # The same name can exist once per Business Unit: the employee's own
        # department wins, else a "Name (Business Unit)" label picks one.
        try:
            dept = crud.get_department_by_name(db, company.id, payload.department_name,
                                               current=employee.department)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if dept is None:
            raise HTTPException(status_code=422, detail=f"Department '{payload.department_name}' not found.")
    if payload.designation_name:
        desig = _by_name(db, models.Designation, company.id, payload.designation_name, "Designation")
    band_problem = ev.band_error(db, company.id, payload.band)
    if band_problem:
        raise HTTPException(status_code=422, detail=band_problem)
    # Form Configuration: Employment Type / Work Mode must be ACTIVE options
    # (an unchanged existing value still passes) and custom fields validate.
    payload.employment_type, payload.work_mode = ev.check_employee_options(
        db, company.id, employment_type=payload.employment_type, work_mode=payload.work_mode,
        current_employment_type=employee.employment_type, current_work_mode=employee.work_mode)
    custom_values = (
        form_config.validate_custom_values(db, company.id, "employee", payload.custom_fields, partial=True)
        if payload.custom_fields is not None else {})
    new_doj = ev.as_date(payload.date_of_joining) if payload.date_of_joining else None
    date_problem = (
        (ev.joining_date_error(new_doj) or ev.dob_error(employee.date_of_birth, new_doj)) if new_doj else None
    )
    if date_problem:
        raise HTTPException(status_code=422, detail=date_problem)
    # Identity, managers and shift (Edit Profile): all checked before any
    # write, like everything above.
    fields_set = payload.model_fields_set
    new_code = (payload.employee_code or "").strip() if "employee_code" in fields_set else None
    if new_code is not None:
        if not new_code:
            raise HTTPException(status_code=422, detail="Employee ID can't be blank.")
        if new_code.lower() != (employee.employee_code or "").lower() and db.scalar(
                select(models.Employee.id).where(
                    models.Employee.company_id == company.id, models.Employee.id != employee.id,
                    func.lower(models.Employee.employee_code) == new_code.lower()).limit(1)) is not None:
            raise HTTPException(status_code=409, detail=f"Employee ID '{new_code}' already exists.")
    new_email = (payload.work_email or "").strip().lower() if "work_email" in fields_set and payload.work_email else None
    login_user = crud.get_core_user_by_employee_id(db, employee_id)
    if new_email and new_email != (employee.work_email or "").lower():
        if db.scalar(select(models.Employee.id).where(
                models.Employee.company_id == company.id, models.Employee.id != employee.id,
                func.lower(models.Employee.work_email) == new_email).limit(1)) is not None:
            raise HTTPException(status_code=409, detail=f"Work email '{new_email}' is already used by another employee.")
        if db.scalar(select(models.User.id).where(
                func.lower(models.User.email) == new_email,
                models.User.id != (login_user.id if login_user else None)).limit(1)) is not None:
            raise HTTPException(status_code=409, detail=f"'{new_email}' is already the login of another account.")
    if "first_name" in fields_set and not (payload.first_name or "").strip():
        raise HTTPException(status_code=422, detail="First name can't be blank.")
    for key, label in (("reporting_manager_id", "Reporting Manager"),
                       ("dotted_line_manager_id", "Second Reporting Manager")):
        if key in fields_set and getattr(payload, key) is not None:
            _check_manager(db, company.id, getattr(payload, key), label, employee_id=employee_id)
            if crud.is_manager_of_employee(db, employee_id, getattr(payload, key)):
                raise HTTPException(400, "This assignment would create a circular reporting relationship.")
    new_rm = payload.reporting_manager_id if "reporting_manager_id" in fields_set else employee.reporting_manager_id
    new_dl = payload.dotted_line_manager_id if "dotted_line_manager_id" in fields_set else employee.dotted_line_manager_id
    if new_rm is not None and new_rm == new_dl:
        raise HTTPException(400, "Second Reporting Manager must be different from the Reporting Manager.")
    new_shift = None
    if "shift_id" in fields_set and payload.shift_id is not None:
        new_shift = db.get(models.Shift, payload.shift_id)
        if new_shift is None or new_shift.company_id != company.id:
            raise HTTPException(status_code=422, detail="Shift not found in your company.")
    # Org-unit changes are recorded in the lifecycle history (old vs new).
    old_org_values = {
        "branch_id": employee.branch_id,
        "business_unit_id": employee.department.business_unit_id if employee.department else None,
        "department_id": employee.department_id,
        "reporting_manager_id": employee.reporting_manager_id,
        "dotted_line_manager_id": employee.dotted_line_manager_id,
    }
    new_org_values: dict = {}
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
    if "first_name" in fields_set:
        employee.first_name = " ".join(payload.first_name.split())
    if "last_name" in fields_set:
        employee.last_name = " ".join((payload.last_name or "").split()) or None
    if new_code is not None:
        employee.employee_code = new_code
    if new_email and new_email != (employee.work_email or "").lower():
        old_email = (employee.work_email or "").lower()
        employee.work_email = new_email
        # Their login follows the work email it was created from.
        if login_user is not None and (login_user.email or "").lower() == old_email:
            login_user.email = new_email
    if "first_name" in fields_set or "last_name" in fields_set:
        if login_user is not None and hasattr(login_user, "full_name"):
            login_user.full_name = f"{employee.first_name} {employee.last_name or ''}".strip()
    if "reporting_manager_id" in fields_set:
        employee.reporting_manager_id = payload.reporting_manager_id
        new_org_values["reporting_manager_id"] = payload.reporting_manager_id
    if "dotted_line_manager_id" in fields_set:
        employee.dotted_line_manager_id = payload.dotted_line_manager_id
        new_org_values["dotted_line_manager_id"] = payload.dotted_line_manager_id
    if "shift_id" in fields_set:
        today = crud.company_today(db, company.id)
        if new_shift is not None:
            current = crud.get_active_shift_assignment(db, employee.id, today)
            if current is None or current.shift_id != new_shift.id:
                crud.create_shift_assignment(db, employee.id, new_shift.id, today)
        else:
            # No shift from today: end any assignment still running.
            for row in db.scalars(select(models.ShiftAssignment).where(
                    models.ShiftAssignment.employee_id == employee.id,
                    or_(models.ShiftAssignment.to_date.is_(None), models.ShiftAssignment.to_date >= today))):
                row.to_date = today - timedelta(days=1) if row.from_date < today else row.from_date
                if row.from_date >= today:
                    db.delete(row)
    employee.updated_by = current_user.id
    form_config.save_custom_values(db, company.id, "employee", employee.id, custom_values)
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
        if notification_settings.channel_enabled(db, company.id, "promotion_announcement", "email"):
            es.send_promotion_announcement_email(
                employee.work_email or employee.personal_email,
                db=db, company_id=company.id, employee_id=employee.id,
                employee_name=f"{employee.first_name} {employee.last_name}".strip(),
                old_designation=old_designation_name, new_designation=promoted_to,
                decided_by=f"{actor.first_name} {actor.last_name}".strip() if actor else None,
            )
        if notification_settings.channel_enabled(db, company.id, "promotion_announcement", "inapp"):
            crud.create_notification(
                db, company.id, crud.get_user_id_for_employee(db, employee.id),
                title="Promotion",
                body=f"Congratulations on your promotion to {promoted_to}!",
                entity_type="employee_promotion", entity_id=employee.id,
            )
            db.commit()
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
        contract_status=employee.contract_status,
    )


def _require_contract_actor(db: Session, current_user: models.User, employee: models.Employee) -> None:
    """Extend / Expire Contract: on top of People 'edit' access to this
    employee -- never your own contract, and never an account with more
    access than yours (e.g. the Owner's)."""
    if current_user.employee_id is not None and current_user.employee_id == employee.id:
        raise HTTPException(status_code=403, detail="You can't change your own contract.")
    target_user = crud.get_core_user_by_employee_id(db, employee.id)
    if target_user is not None:
        error = role_tiers.user_management_error(db, current_user, target_user)
        if error is not None:
            raise HTTPException(status_code=403, detail=error)


@router.post("/{employee_id}/contract/extend", response_model=schemas.ContractActionOut)
@router.post("/{employee_id}/contract/renew", response_model=schemas.ContractActionOut)
def extend_employee_contract(
    employee_id: uuid.UUID,
    payload: schemas.ContractRenewRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Extend Contract (/renew is the older name): a new future end date and,
    optionally, a new rate. Reactivates an expired contract and restores
    the employee's login unless something else still blocks it. Logged to
    employee history and the audit log."""
    from .. import contract_lifecycle

    employee = _require_employee(db, employee_id, current_user.company_id)
    _require_contract_actor(db, current_user, employee)
    try:
        event = contract_lifecycle.extend_contract(
            db, employee, current_user, new_end_date=payload.new_end_date,
            new_rate_amount=payload.new_rate_amount, new_rate_unit=payload.new_rate_unit, notes=payload.notes,
        )
    except crud.ContractActionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    db.commit()
    db.refresh(employee)
    return _contract_action_out(db, employee, event)


@router.post("/{employee_id}/contract/expire", response_model=schemas.ContractActionOut)
def expire_employee_contract(
    employee_id: uuid.UUID,
    payload: schemas.ContractExpireRequest | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_to_employee("edit")),
):
    """Expire Contract: ends the contract now, even before its end date --
    the employee's login is blocked and every open session ends on its next
    request, until the contract is extended. Logged to history and audit."""
    from .. import contract_lifecycle

    employee = _require_employee(db, employee_id, current_user.company_id)
    _require_contract_actor(db, current_user, employee)
    try:
        event = contract_lifecycle.expire_contract(
            db, employee, actor_user_id=current_user.id,
            reason=payload.reason if payload else None, source="manual",
        )
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
    from .. import contract_lifecycle

    employee = _require_employee(db, employee_id, current_user.company_id)
    old_end = employee.contract_end_date
    try:
        event = crud.convert_contract_to_permanent(db, employee, current_user,
                                                   notes=payload.notes if payload else None)
    except crud.ContractActionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    # An expired contract no longer blocks a permanent employee's login.
    contract_lifecycle.restore_after_conversion(db, employee, current_user, old_end)
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
    for rep_id in updates.get("hr_representative_ids") or []:
        _check_manager(db, current_user.company_id, rep_id, "HR Representative")
    # HR Representatives belong to the employee's department (several allowed);
    # the old single field still works and replaces them with that one.
    if employee.department:
        if updates.get("hr_representative_ids") is not None:
            crud.set_department_hr_reps(db, employee.department, updates["hr_representative_ids"])
        elif "hr_representative_id" in updates:
            single = updates["hr_representative_id"]
            crud.set_department_hr_reps(db, employee.department, [single] if single else [])
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
        if allocation is None:
            raise HTTPException(status_code=409, detail="Employee has no project allocation")
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
        hr_representatives=[_node_out(n) for n in chain.hr_representatives],
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
    # Form Configuration lists: Gender / Marital Status / Nationality (an
    # unchanged existing value always saves).
    updates = ev.check_personal_options(
        db, employee.company_id, updates,
        current={"gender": employee.gender, "marital_status": employee.marital_status,
                 "nationality": employee.nationality})
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
    document_type: str = Query(..., min_length=1, max_length=120),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs Upload Document (People > Employee Documents) and the Educational
    / Professional Documents sections of Add / Edit Employee. Every call adds
    a NEW record (several files can be added any time, nothing is replaced).
    Allowed for the employee, HR / admins / People editors, whoever added the
    employee, the Reporting Manager chain and the department head -- see
    crud.employee_document_access."""
    _require_employee(db, employee_id, current_user.company_id)
    if not crud.employee_document_access(db, current_user, employee_id)["canUpload"]:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to upload documents for this employee.",
        )
    company = db.get(models.Company, current_user.company_id)
    document_type = document_type.strip()
    # Form Configuration: Upload Document's type is a configurable list. The
    # two section types of the Employee profile are system values.
    if document_type not in crud.EMPLOYEE_DOC_CATEGORIES:
        document_type = form_config.require_option(
            db, company.id, "employee_document.type", document_type, required=True)
    file_url, size_bytes = save_uploaded_file(
        file, entity_type="employee_document", entity_id=employee_id
    )
    record = crud.create_document_record(
        db,
        employee_id=employee_id,
        document_type=document_type,
        status="Verified",
        uploaded_on=crud.company_today(db, company.id),
        file_url=file_url,
        file_name=file.filename,
        file_size_bytes=size_bytes,
        content_type=file.content_type,
        uploaded_by=current_user.id,
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
    # Domain / Industry must be a configured option (a domain this employee
    # already has stays valid even if an admin has since hidden it).
    existing_domains = [
        r.domain for r in crud._experience_rows(db, employee_id) if r.domain]
    if updates.get("experience_records") is not None:
        recs = updates["experience_records"]
        checked = ev.check_experience_domains(
            db, current_user.company_id, [r.get("domain") for r in recs], existing_domains)
        for r, d in zip(recs, checked):
            r["domain"] = d
    if updates.get("domain"):
        updates["domain"] = ev.check_experience_domains(
            db, current_user.company_id, [updates["domain"]], existing_domains)[0]
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


def _can_edit_payroll(db: Session, user: models.User) -> bool:
    """Same rule as routers/payroll.py's _require_payroll_edit, without raising."""
    role = crud.get_user_primary_role(db, user.id)
    if role is not None and role.name == _OWNER_ROLE_NAME:
        return True
    return crud.permission_satisfied(db, user, "payroll_process", "payroll", "edit")


@router.get("/{employee_id}/rate", response_model=schemas.EmployeeRateOut)
def get_employee_rate(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_people_access_or_self("view")),
):
    """A Freelancer's pay rate and its effective-dated history -- pay data,
    so the same visibility as the employee's payroll details."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    if not crud.can_view_employee_payroll(db, current_user, employee_id):
        raise HTTPException(status_code=403, detail="You don't have permission to do this")
    return crud.employee_rate_out(db, employee, _can_edit_payroll(db, current_user))


@router.post("/{employee_id}/rate", response_model=schemas.EmployeeRateOut)
def set_employee_rate(
    employee_id: uuid.UUID,
    payload: schemas.EmployeeRateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission_or_action("payroll_process", "payroll", "edit")),
):
    """Sets / changes a Freelancer's rate from [effective_from] (default
    today). Earlier days keep their earlier rate; open payroll runs from
    that date on are recomputed."""
    employee = _require_employee(db, employee_id, current_user.company_id)
    if not crud.is_freelancer(employee):
        raise HTTPException(status_code=409, detail="Hourly rates are only for Freelancer employees.")
    try:
        currency = currencies.normalize_code(payload.currency_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    before = {
        "rate_amount": float(employee.contract_rate_amount) if employee.contract_rate_amount is not None else None,
        "rate_unit": employee.contract_rate_unit,
        "currency_code": crud.employee_rate_currency(db, employee),
    }
    effective_from = payload.effective_from or crud.company_today(db, current_user.company_id)
    row = crud.set_employee_rate(
        db, current_user.company_id, employee, payload.rate_amount, payload.rate_unit,
        effective_from, payload.notes, current_user.id, currency_code=currency,
    )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "employee_rate", employee.id,
        changes={"before": before, "after": {"rate_amount": payload.rate_amount, "rate_unit": payload.rate_unit,
                                             "currency_code": row.currency_code,
                                             "effective_from": effective_from.isoformat()}},
    )
    crud.resync_draft_payroll_runs(db, current_user.company_id, employee.id, effective_from)
    db.commit()
    db.refresh(employee)
    return crud.employee_rate_out(db, employee, True)


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
    # A blank text field ('' / '—') clears it (stored NULL, never '').
    updates = {field_map.get(key, key): (None if isinstance(value, str) and value.strip() in ("", "—") else value)
               for key, value in updates.items()}
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
    current_user: models.User = Depends(get_current_user),
):
    _require_employee(db, employee_id, current_user.company_id)
    if not crud.employee_document_access(db, current_user, employee_id)["canView"]:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to view documents for this employee.",
        )
    return crud.get_employee_documents(db, employee_id)


@router.get("/{employee_id}/documents/access")
def get_employee_document_access(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """What the caller may do with this employee's documents -- the app shows
    the upload controls only when canUpload is true."""
    _require_employee(db, employee_id, current_user.company_id)
    return crud.employee_document_access(db, current_user, employee_id)


@router.get("/{employee_id}/documents/{document_id}/file")
def get_employee_document_file(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    download: bool = True,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """View / Download on the Educational, Professional and Other Documents
    sections: the file under its original name -- attachment (download) or
    inline (in-app preview). Same view gate as listing the documents."""
    _require_employee(db, employee_id, current_user.company_id)
    if not crud.employee_document_access(db, current_user, employee_id)["canView"]:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to view documents for this employee.",
        )
    record = crud.get_document_record(db, employee_id, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found")
    root = uploads_root().resolve()
    path = (root / (record.file_url or "").removeprefix("/media/")).resolve()
    if not (record.file_url or "").startswith("/media/") or root not in path.parents:
        raise HTTPException(status_code=404, detail="This document has no file attached.")
    path = winlong_path(path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The file is no longer available.")
    meta = db.get(models.EmployeeDocumentMeta, record.id)
    file_name = (meta.file_name if meta and meta.file_name else None) or path.name
    media_type = (meta.content_type if meta and meta.content_type else None) \
        or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=file_name,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, max-age=60"})


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
