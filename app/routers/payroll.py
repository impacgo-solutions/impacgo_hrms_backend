import calendar
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from .. import crud, models, payroll_period, schemas
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, get_payroll_scope, require_module_enabled, require_permission_or_action
from ..payslip_pdf import render_payslip_pdf
from ..payslip_html_renderer import (
    build_payslip_placeholders,
    render_html_to_pdf,
    render_payslip_html,
    validate_template_html,
)

router = APIRouter(
    prefix="/api", tags=["payroll"],
    dependencies=[Depends(require_module_enabled("payroll"))],
)

# All admin-side salary-configuration mutations (components, structures,
# assignments, the payslip template, running payroll) share this one gate
# -- same require_permission_or_action("payroll_process", ...) pattern
# create_payroll_run already used, just widened to a few new (resource,
# action) pairs. "edit"/"activate" aren't yet in the granular
# permission_actions catalog (only "view"/"generate"/"export" are), so in
# practice these currently gate purely on the existing payroll_process
# matrix column (Edit/Admin) -- exactly like require_permission alone
# would, and automatically also open up the moment "edit"/"activate" are
# added to the catalog, with zero code change needed here.
_require_payroll_edit = require_permission_or_action("payroll_process", "payroll", "edit")


def _require_payroll_view(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
) -> models.User:
    """L-22: salary components / structure templates are company payroll
    configuration -- org-wide payroll access (Owner, or Edit/Admin on
    Payroll) or explicit View on the Payroll column. A self-service role
    sees only its own structure (/employees/{id}/salary-structure)."""
    if scope == "org":
        return current_user
    if crud.effective_user_matrix(db, current_user).get("payroll_process") in ("v", "e", "a"):
        return current_user
    raise HTTPException(status_code=403, detail="You don't have permission to view payroll configuration")


def _state_error(exc: "payroll_period.PayrollStateError") -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _owned_run(db: Session, company_id: uuid.UUID, run_id: uuid.UUID, lock: bool = False) -> models.PayrollRun:
    from sqlalchemy import select

    q = select(models.PayrollRun).where(models.PayrollRun.id == run_id)
    if lock:
        q = q.with_for_update()
    run = db.scalar(q)
    if run is None or run.company_id != company_id:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    return run


@router.get(
    "/payroll/deduction-settings",
    response_model=schemas.PayrollDeductionSettingsOut,
)
def get_payroll_deduction_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Payroll Settings > Deductions Configuration."""
    return schemas.PayrollDeductionSettingsOut(
        lop_deduction_enabled=crud.get_company_lop_deduction_enabled(db, current_user.company_id),
        asset_deduction_enabled=crud.get_company_asset_deduction_enabled(db, current_user.company_id),
    )


@router.patch(
    "/payroll/deduction-settings",
    response_model=schemas.PayrollDeductionSettingsOut,
)
def update_payroll_deduction_settings(
    payload: schemas.PayrollDeductionSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Payroll Settings > Deductions Configuration -- HR/Organization Owner
    toggling whether LOP and/or Asset-recovery amounts are automatically
    deducted from payroll. Gated by the Payroll module's own edit
    permission (same as every other payroll-configuration mutation on this
    router), not System Settings/RBAC -- deliberately a separate, narrower
    endpoint from PATCH /api/company-settings so this doesn't widen who
    can touch that endpoint's other, unrelated company-wide fields."""
    updates = payload.model_dump(exclude_unset=True)
    crud.upsert_company_settings(db, current_user.company_id, updates)
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "payroll_deduction_settings",
        current_user.company_id, changes=updates,
    )
    db.commit()
    return schemas.PayrollDeductionSettingsOut(
        lop_deduction_enabled=crud.get_company_lop_deduction_enabled(db, current_user.company_id),
        asset_deduction_enabled=crud.get_company_asset_deduction_enabled(db, current_user.company_id),
    )


@router.get(
    "/payroll/reimbursement-items",
    response_model=list[schemas.PayrollReimbursementItemOut],
)
def list_payroll_reimbursement_items(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Payroll > Travel & Expense Reimbursements -- every fully-approved
    Travel Requisition and Expense Report/Reimbursement, whether or not
    it's been scheduled into a payroll period yet."""
    return crud.list_payroll_reimbursement_items(db, current_user.company_id)


@router.post(
    "/payroll/reimbursement-inclusions",
    response_model=schemas.PayrollReimbursementItemOut,
    status_code=201,
)
def create_payroll_reimbursement_inclusion(
    payload: schemas.PayrollReimbursementInclusionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """HR/Organization Owner schedules one already-fully-approved Travel
    Requisition or Expense Report/Reimbursement into a specific payroll
    period. Rejects a record that isn't fully approved yet, and rejects
    scheduling the same record twice (the UNIQUE (source_type, source_id)
    constraint's friendly pre-check) -- see
    crud.create_payroll_reimbursement_inclusion."""
    result = crud.create_payroll_reimbursement_inclusion(
        db, current_user.company_id, payload.source_type, payload.source_id,
        payload.target_period_month, payload.target_period_year, current_user.id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Travel Requisition or Expense Report not found")
    if result == "not_approved":
        raise HTTPException(status_code=409, detail="This record is not fully approved yet.")
    if result == "already_scheduled":
        raise HTTPException(status_code=409, detail="This record has already been scheduled into payroll.")
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "create", "payroll_reimbursement_inclusion", result.id,
    )
    db.commit()
    items = crud.list_payroll_reimbursement_items(db, current_user.company_id)
    matching = next(
        (i for i in items if i["source_type"] == payload.source_type and i["source_id"] == payload.source_id),
        None,
    )
    if matching is None:
        raise HTTPException(status_code=404, detail="Travel Requisition or Expense Report not found")
    return matching


@router.patch(
    "/payroll/reimbursement-inclusions/{inclusion_id}",
    response_model=schemas.PayrollReimbursementItemOut,
)
def update_payroll_reimbursement_inclusion(
    inclusion_id: uuid.UUID,
    payload: schemas.PayrollReimbursementInclusionUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Review a scheduled Travel/Expense reimbursement before it is paid:
    edit the amount (never above the approved amount), include/exclude it,
    defer it to another payroll period, and/or record a note. Refused once
    the payslip it was paid on is marked paid. Affected generated-but-
    unpaid payslips are recomputed at once; every change is audit-logged
    with its before/after values."""
    updates = payload.model_dump(exclude_unset=True)
    if ("target_period_month" in updates) != ("target_period_year" in updates):
        raise HTTPException(status_code=422, detail="Send both target_period_month and target_period_year to defer.")
    result = crud.update_reimbursement_inclusion(
        db, current_user.company_id, inclusion_id, updates, current_user.id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Scheduled reimbursement not found")
    if result == "locked":
        raise HTTPException(status_code=409, detail="This reimbursement has already been paid and can no longer be changed.")
    if result == "amount_exceeds":
        raise HTTPException(status_code=422, detail="The payroll amount can't exceed the approved amount.")
    inclusion, before, after = result
    changes = {k: f"{before[k]} -> {after[k]}" for k in after if before[k] != after[k]}
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "payroll_reimbursement_inclusion",
        inclusion.id, changes=changes or None,
    )
    db.commit()
    items = crud.list_payroll_reimbursement_items(db, current_user.company_id)
    matching = next((i for i in items if i["inclusion_id"] == inclusion.id), None)
    if matching is None:
        raise HTTPException(status_code=404, detail="Travel Requisition or Expense Report not found")
    return matching


@router.post(
    "/payroll/reimbursements/identify",
    response_model=schemas.PayrollReimbursementIdentifyOut,
)
def identify_payroll_reimbursements(
    payload: schemas.PayrollReimbursementIdentifyRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Automatic identification for an upcoming payroll period (also run by
    Generate): schedules every approved Travel/Expense record not yet
    scheduled, for each Reimbursement Component that is enabled with
    auto-include on, so HR can review them before running payroll."""
    count = crud.identify_reimbursements_for_period(
        db, current_user.company_id, payload.period_month, payload.period_year, current_user.id,
    )
    if count:
        crud.create_audit_log(
            db, current_user.company_id, current_user.id, "identify", "payroll_reimbursement_inclusion",
            current_user.company_id,
            changes={"period": f"{payload.period_year}-{payload.period_month:02d}", "identified": str(count)},
        )
    db.commit()
    return schemas.PayrollReimbursementIdentifyOut(
        identified=count, items=crud.list_payroll_reimbursement_items(db, current_user.company_id),
    )


@router.get(
    "/payroll/reimbursement-components",
    response_model=list[schemas.PayrollReimbursementComponentOut],
)
def list_payroll_reimbursement_components(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Payroll > Salary Structure > Reimbursement Components -- Travel
    Reimbursement and Expense Reimbursement."""
    return list(crud.get_reimbursement_components(db, current_user.company_id).values())


@router.patch(
    "/payroll/reimbursement-components/{source_type}",
    response_model=schemas.PayrollReimbursementComponentOut,
)
def update_payroll_reimbursement_component(
    source_type: str,
    payload: schemas.PayrollReimbursementComponentUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Enable/disable, rename (payslip label), toggle automatic inclusion in
    payroll runs, or set/clear the per-request cap for one Reimbursement
    Component. Audit-logged with before/after values."""
    if source_type not in crud.REIMBURSEMENT_COMPONENT_DEFAULT_NAMES:
        raise HTTPException(status_code=404, detail="Unknown reimbursement component")
    updates = payload.model_dump(exclude_unset=True)
    before, after = crud.update_reimbursement_component(
        db, current_user.company_id, source_type, updates, current_user.id,
    )
    keys = ("display_name", "is_enabled", "auto_include", "max_amount_per_request")
    changes = {k: f"{before[k]} -> {after[k]}" for k in keys if before[k] != after[k]}
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "payroll_reimbursement_component",
        current_user.company_id, changes={"component": source_type, **changes},
    )
    db.commit()
    return crud.get_reimbursement_components(db, current_user.company_id)[source_type]


@router.get("/payroll/salary-components", response_model=list[schemas.SalaryComponentOut])
def list_salary_components(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_view),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_salary_components(db, company.id)


@router.post(
    "/payroll/salary-components",
    response_model=schemas.SalaryComponentOut,
    status_code=201,
)
def create_salary_component(
    payload: schemas.SalaryComponentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        component = crud.create_salary_component(
            db, company.id, payload.name, payload.code, payload.component_type,
            payload.calc_type, payload.is_taxable,
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "salary_component", component.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(component)
    return component


@router.patch(
    "/payroll/salary-components/{component_id}",
    response_model=schemas.SalaryComponentOut,
)
def update_salary_component(
    component_id: uuid.UUID,
    payload: schemas.SalaryComponentUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    component = crud.update_salary_component(db, component_id, **updates)
    if component is None:
        raise HTTPException(status_code=404, detail="Salary component not found")
    crud.create_audit_log(
        db, company.id, current_user.id, "update", "salary_component", component.id,
        changes={k: str(v) for k, v in updates.items()},
    )
    db.commit()
    db.refresh(component)
    return component


def _salary_structure_out(
    db: Session, structure: models.SalaryStructure
) -> schemas.SalaryStructureListItemOut:
    department = db.get(models.Department, structure.department_id) if structure.department_id else None
    designation = db.get(models.Designation, structure.designation_id) if structure.designation_id else None
    branch = db.get(models.Branch, structure.branch_id) if structure.branch_id else None
    return schemas.SalaryStructureListItemOut(
        id=structure.id,
        name=structure.name,
        effective_from=structure.effective_from,
        is_active=structure.is_active,
        lines=[
            schemas.SalaryStructureLineOut(
                component_id=line.component_id,
                component_name=line.component.name,
                component_type=line.component.component_type,
                calc_type=line.component.calc_type,
                amount=float(line.amount) if line.amount is not None else None,
                percent_of=line.percent_of,
                percent=float(line.percent) if line.percent is not None else None,
            )
            for line in structure.lines
        ],
        department_id=structure.department_id,
        department_name=department.name if department else None,
        designation_id=structure.designation_id,
        designation_name=designation.name if designation else None,
        branch_id=structure.branch_id,
        branch_name=branch.name if branch else None,
        grade=structure.grade,
    )


@router.get("/payroll/salary-structures", response_model=list[schemas.SalaryStructureListItemOut])
def list_salary_structures(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_view),
):
    company = db.get(models.Company, current_user.company_id)
    structures = crud.list_salary_structures(db, company.id)
    return [_salary_structure_out(db, s) for s in structures]


@router.post(
    "/payroll/salary-structures",
    response_model=schemas.SalaryStructureListItemOut,
    status_code=201,
)
def create_salary_structure(
    payload: schemas.SalaryStructureCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        structure = crud.create_salary_structure(
            db, company.id, payload.name, payload.effective_from,
            [line.model_dump() for line in payload.lines],
            department_id=payload.department_id, designation_id=payload.designation_id,
            branch_id=payload.branch_id, grade=payload.grade,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "create", "salary_structure", structure.id)
    db.commit()
    db.refresh(structure)
    return _salary_structure_out(db, structure)


@router.patch(
    "/payroll/salary-structures/{structure_id}",
    response_model=schemas.SalaryStructureListItemOut,
)
def update_salary_structure(
    structure_id: uuid.UUID,
    payload: schemas.SalaryStructureUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    updates = payload.model_dump(exclude_unset=True)
    lines = updates.pop("lines", None)
    try:
        structure = crud.update_salary_structure(db, structure_id, lines=lines, **updates)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if structure is None:
        raise HTTPException(status_code=404, detail="Salary structure not found")
    crud.create_audit_log(
        db, company.id, current_user.id, "update", "salary_structure", structure.id,
        changes={k: str(v) for k, v in updates.items() if k != "lines"},
    )
    db.commit()
    db.refresh(structure)
    return _salary_structure_out(db, structure)


@router.delete("/payroll/salary-structures/{structure_id}", status_code=204)
def delete_salary_structure(
    structure_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        deleted = crud.delete_salary_structure(db, structure_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Salary structure not found")
    crud.create_audit_log(db, company.id, current_user.id, "delete", "salary_structure", structure_id)
    db.commit()
    return Response(status_code=204)


@router.patch(
    "/payroll/salary-structures/{structure_id}/activate",
    response_model=schemas.SalaryStructureListItemOut,
)
def activate_salary_structure(
    structure_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    structure = crud.set_salary_structure_active(db, structure_id, True)
    if structure is None:
        raise HTTPException(status_code=404, detail="Salary structure not found")
    crud.create_audit_log(db, company.id, current_user.id, "activate", "salary_structure", structure.id)
    db.commit()
    db.refresh(structure)
    return _salary_structure_out(db, structure)


@router.patch(
    "/payroll/salary-structures/{structure_id}/deactivate",
    response_model=schemas.SalaryStructureListItemOut,
)
def deactivate_salary_structure(
    structure_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    structure = crud.set_salary_structure_active(db, structure_id, False)
    if structure is None:
        raise HTTPException(status_code=404, detail="Salary structure not found")
    crud.create_audit_log(db, company.id, current_user.id, "deactivate", "salary_structure", structure.id)
    db.commit()
    db.refresh(structure)
    return _salary_structure_out(db, structure)


@router.post(
    "/payroll/salary-structure-assignments",
    response_model=schemas.SalaryStructureAssignmentOut,
    status_code=201,
)
def create_salary_structure_assignment(
    payload: schemas.SalaryStructureAssignmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    try:
        assignment = crud.create_salary_structure_assignment(
            db, payload.employee_id, payload.structure_id, payload.from_date,
            payload.annual_ctc, payload.base_amount,
        )
        crud.create_audit_log(
            db, company.id, current_user.id, "create", "salary_structure_assignment", assignment.id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Keep "My Payslips" connected the moment a structure/CTC change takes
    # effect -- any not-yet-finalized payroll run covering this date
    # onward is refreshed now, not left stale until someone happens to
    # click "Generate" again. Already-processed/paid runs are untouched.
    crud.resync_draft_payroll_runs(db, company.id, payload.employee_id, payload.from_date)
    db.commit()
    db.refresh(assignment)
    out = schemas.SalaryStructureAssignmentOut.model_validate(assignment)
    superseding = crud.get_superseding_assignment(
        db, payload.employee_id, payload.from_date, assignment.id
    )
    if superseding is not None:
        out.superseded_by_later_from_date = superseding.from_date
        out.superseded_by_later_annual_ctc = (
            float(superseding.annual_ctc) if superseding.annual_ctc is not None else None
        )
    else:
        # Keep the plain "CTC (Annual)" reference field (core_employees.
        # annual_ctc -- what Edit Bank & Payroll Details shows/edits)
        # in sync with whatever's actually driving this employee's real
        # Basic/Gross/Net now, so the two never silently diverge. Skipped
        # when a later assignment already supersedes this one (this one
        # isn't "current" yet, so the later one's CTC should keep showing).
        employee.annual_ctc = payload.annual_ctc
        db.commit()
    return out


@router.delete("/payroll/salary-structure-assignments/{assignment_id}", status_code=204)
def delete_salary_structure_assignment(
    assignment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    assignment = _get_owned_assignment(db, company.id, assignment_id)
    employee_id, from_date = assignment.employee_id, assignment.from_date
    crud.delete_salary_structure_assignment(db, assignment_id)
    crud.create_audit_log(
        db, company.id, current_user.id, "delete", "salary_structure_assignment", assignment_id,
        changes={"employee_id": str(employee_id), "from_date": from_date.isoformat()},
    )
    # M-32: open generated runs from that date on are recomputed at once
    # (locked periods are untouched -- see payroll_period).
    payroll_period.resync_runs_from(db, company.id, employee_id, from_date, current_user.id)
    db.commit()
    return Response(status_code=204)


def _variable_pay_summary_out(summary: dict) -> schemas.VariablePaySummaryOut:
    return schemas.VariablePaySummaryOut(
        component_id=summary["component_id"],
        component_name=summary["component_name"],
        structure_assignment_id=summary["structure_assignment_id"],
        fiscal_year_start=summary["fiscal_year_start"],
        fiscal_year_end=summary["fiscal_year_end"],
        annual_entitlement=summary["annual_entitlement"],
        amount_paid=summary["amount_paid"],
        amount_remaining=summary["amount_remaining"],
        payouts=[
            schemas.VariablePayPayoutOut(
                id=p["id"],
                payroll_run_id=p["payroll_run_id"],
                period_month=p["period_month"],
                period_year=p["period_year"],
                amount=p["amount"],
                created_at=p["created_at"],
                notes=p["notes"],
            )
            for p in summary["payouts"]
        ],
    )


@router.get(
    "/payroll/employees/{employee_id}/variable-pay",
    response_model=list[schemas.VariablePaySummaryOut],
)
def get_employee_variable_pay_summary(
    employee_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Payroll > Variable Pay -- one entry per var_annual component on this
    employee's current salary structure: annual entitlement, amount
    already confirmed-paid this fiscal year, remaining eligible amount, and
    real payout history. Backs the "pick an employee" step of the
    HR/Owner/Payroll-Admin controlled payout flow."""
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    return [_variable_pay_summary_out(s) for s in crud.get_variable_pay_summary(db, employee_id)]


@router.post(
    "/payroll/variable-pay-payouts",
    response_model=schemas.VariablePaySummaryOut,
    status_code=201,
)
def create_variable_pay_payout(
    payload: schemas.VariablePayPayoutCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """HR/Organization Owner/authorized Payroll Admin confirms an actual
    Variable Pay payment for one employee in one specific payroll run, up
    to their configured annual entitlement. Immediately resyncs that
    employee's slip in that run (if not already 'paid') so Preview/PDF
    reflect it right away, then returns the updated entitlement summary."""
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    try:
        crud.create_variable_pay_payout(
            db, company.id, payload.employee_id, payload.component_id,
            payload.payroll_run_id, payload.amount, current_user.id, payload.notes,
        )
        crud.create_audit_log(
            db, company.id, current_user.id, "create", "variable_pay_payout", payload.payroll_run_id,
            changes={
                "employee_id": str(payload.employee_id),
                "component_id": str(payload.component_id),
                "payroll_run_id": str(payload.payroll_run_id),
                "amount": payload.amount,
            },
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    run = db.get(models.PayrollRun, payload.payroll_run_id)
    summaries = crud.get_variable_pay_summary(db, payload.employee_id, run.from_date)
    match = next((s for s in summaries if s["component_id"] == payload.component_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Variable Pay component not found after payout")
    return _variable_pay_summary_out(match)


def _get_owned_assignment(
    db: Session, company_id: uuid.UUID, assignment_id: uuid.UUID
) -> models.SalaryStructureAssignment:
    assignment = db.get(models.SalaryStructureAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="Salary structure assignment not found")
    employee = db.get(models.Employee, assignment.employee_id)
    if employee is None or employee.company_id != company_id:
        raise HTTPException(status_code=404, detail="Salary structure assignment not found")
    return assignment


def _override_out(
    override: models.SalaryStructureAssignmentOverride,
) -> schemas.SalaryAssignmentOverrideOut:
    return schemas.SalaryAssignmentOverrideOut(
        id=override.id,
        assignment_id=override.assignment_id,
        component_id=override.component_id,
        component_name=override.component.name,
        override_type=override.override_type,
        value=float(override.value),
        reason=override.reason,
    )


@router.get(
    "/payroll/salary-structure-assignments/{assignment_id}/overrides",
    response_model=list[schemas.SalaryAssignmentOverrideOut],
)
def list_salary_structure_assignment_overrides(
    assignment_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    _get_owned_assignment(db, company.id, assignment_id)
    overrides = crud.list_assignment_overrides(db, assignment_id)
    return [_override_out(o) for o in overrides]


@router.post(
    "/payroll/salary-structure-assignments/{assignment_id}/overrides",
    response_model=schemas.SalaryAssignmentOverrideOut,
    status_code=201,
)
def set_salary_structure_assignment_override(
    assignment_id: uuid.UUID,
    payload: schemas.SalaryAssignmentOverrideIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    assignment = _get_owned_assignment(db, company.id, assignment_id)
    override = crud.set_assignment_override(
        db, assignment_id, payload.component_id, payload.override_type,
        payload.value, payload.reason, current_user.employee_id,
    )
    crud.create_audit_log(
        db, company.id, current_user.id, "set", "salary_structure_assignment_override", override.id,
        changes={
            "component_id": str(payload.component_id),
            "override_type": payload.override_type,
            "value": str(payload.value),
            "reason": payload.reason or "",
        },
    )
    # Keep "My Payslips" connected -- see create_salary_structure_assignment.
    crud.resync_draft_payroll_runs(db, company.id, assignment.employee_id, assignment.from_date)
    db.commit()
    db.refresh(override)
    return _override_out(override)


@router.delete(
    "/payroll/salary-structure-assignments/{assignment_id}/overrides/{component_id}",
    status_code=204,
)
def delete_salary_structure_assignment_override(
    assignment_id: uuid.UUID,
    component_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    assignment = _get_owned_assignment(db, company.id, assignment_id)
    deleted = crud.delete_assignment_override(db, assignment_id, component_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Override not found")
    crud.create_audit_log(
        db, company.id, current_user.id, "delete", "salary_structure_assignment_override",
        assignment_id, changes={"component_id": str(component_id)},
    )
    # Keep "My Payslips" connected -- see create_salary_structure_assignment.
    crud.resync_draft_payroll_runs(db, company.id, assignment.employee_id, assignment.from_date)
    db.commit()
    return Response(status_code=204)


@router.post(
    "/payroll/runs/{run_id}/generate",
    response_model=schemas.PayrollGenerateResultOut,
)
def generate_payroll_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Generate / regenerate (H-11): allowed while the run is draft,
    generated or approved -- regenerating voids an approval and records
    this user as the maker. A locked / paid run is refused (409)."""
    company = db.get(models.Company, current_user.company_id)
    run = _owned_run(db, company.id, run_id, lock=True)
    try:
        slips = crud.generate_payroll_slips(db, company.id, run_id, actor_id=current_user.id)
    except payroll_period.PayrollStateError as exc:
        db.rollback()
        raise _state_error(exc) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payroll_period.record_generated(db, run, current_user.id, len(slips))
    db.commit()
    db.refresh(run)
    return schemas.PayrollGenerateResultOut(run=run, slips_generated=len(slips),
                                            flagged_slips=run.flagged_slips or 0)


def _transition(db: Session, current_user: models.User, run_id: uuid.UUID, action) -> models.PayrollRun:
    run = _owned_run(db, current_user.company_id, run_id, lock=True)
    try:
        action(db, run, current_user)
    except payroll_period.PayrollStateError as exc:
        db.rollback()
        raise _state_error(exc) from exc
    db.commit()
    db.refresh(run)
    return run


@router.post("/payroll/runs/{run_id}/approve", response_model=schemas.PayrollRunOut)
def approve_payroll_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission_or_action("payroll_process", "payroll", "approve")),
):
    """Checker step (H-11): a generated run is approved by a DIFFERENT
    authorised user than the one who generated it (no Owner exception)."""
    return _transition(db, current_user, run_id, payroll_period.approve_run)


@router.post("/payroll/runs/{run_id}/lock", response_model=schemas.PayrollRunOut)
def lock_payroll_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Freezes an approved period: no regenerate / resync afterwards; later
    corrections become arrears in the next run. Applies loan EMIs."""
    return _transition(db, current_user, run_id, payroll_period.lock_run)


@router.post("/payroll/runs/{run_id}/mark-paid", response_model=schemas.PayrollRunOut)
def mark_payroll_run_paid(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    return _transition(db, current_user, run_id, payroll_period.mark_paid)


@router.get("/payroll/payslip-template", response_model=schemas.PayslipTemplateOut)
def get_payslip_template(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return schemas.PayslipTemplateOut(template=crud.get_payslip_template(db, company.id))


@router.put("/payroll/payslip-template", response_model=schemas.PayslipTemplateOut)
def update_payslip_template(
    payload: schemas.PayslipTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    crud.set_payslip_template(db, company.id, payload.template)
    crud.create_audit_log(db, company.id, current_user.id, "update", "payslip_template", company.id)
    db.commit()
    return schemas.PayslipTemplateOut(template=payload.template)


@router.get("/payroll/payslip-html-template", response_model=schemas.PayslipHtmlTemplateOut | None)
def get_payslip_html_template(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Documents > Templates > Payslip Template -- readable by any
    authenticated user (mirrors the legacy GET /payroll/payslip-template's
    open-read convention), null when this company hasn't authored one yet."""
    company = db.get(models.Company, current_user.company_id)
    row = crud.get_payslip_html_template(db, company.id)
    return row


@router.put("/payroll/payslip-html-template", response_model=schemas.PayslipHtmlTemplateOut)
def save_payslip_html_template(
    payload: schemas.PayslipHtmlTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    error = validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    company = db.get(models.Company, current_user.company_id)
    row = crud.upsert_payslip_html_template(
        db,
        company.id,
        payload.name,
        payload.html_body,
        payload.css_styles,
        payload.is_active,
        current_user.employee_id,
    )
    crud.create_audit_log(db, company.id, current_user.id, "update", "payslip_html_template", row.id)
    db.commit()
    db.refresh(row)
    return row


# ── multiple saved templates (the singular GET/PUT above keep working as a
# "read/edit the active one" shortcut) ───────────────────────────────────────

@router.get("/payroll/payslip-html-templates", response_model=list[schemas.PayslipHtmlTemplateOut])
def list_payslip_html_templates(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_payslip_html_templates(db, company.id)


@router.post("/payroll/payslip-html-templates", response_model=schemas.PayslipHtmlTemplateOut, status_code=201)
def create_payslip_html_template(
    payload: schemas.PayslipHtmlTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    error = validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    company = db.get(models.Company, current_user.company_id)
    row = crud.create_payslip_html_template(
        db, company.id, name=payload.name, html_body=payload.html_body, css_styles=payload.css_styles,
        is_active=payload.is_active, created_by=current_user.employee_id,
    )
    crud.create_audit_log(db, company.id, current_user.id, "create", "payslip_html_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.put("/payroll/payslip-html-templates/{template_id}", response_model=schemas.PayslipHtmlTemplateOut)
def update_payslip_html_template(
    template_id: uuid.UUID,
    payload: schemas.PayslipHtmlTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    error = validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    company = db.get(models.Company, current_user.company_id)
    try:
        row = crud.update_payslip_html_template_by_id(
            db, company.id, template_id, name=payload.name, html_body=payload.html_body,
            css_styles=payload.css_styles, is_active=payload.is_active, updated_by=current_user.employee_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "update", "payslip_html_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/payroll/payslip-html-templates/{template_id}", status_code=204)
def delete_payslip_html_template(
    template_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        crud.delete_payslip_html_template(db, company.id, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "delete", "payslip_html_template", template_id)
    db.commit()


@router.post("/payroll/payslip-html-templates/{template_id}/activate", response_model=schemas.PayslipHtmlTemplateOut)
def activate_payslip_html_template(
    template_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        row = crud.activate_payslip_html_template(db, company.id, template_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "update", "payslip_html_template", row.id)
    db.commit()
    db.refresh(row)
    return row


@router.post(
    "/payroll/payslip-html-template/preview",
    response_model=schemas.PayslipHtmlTemplatePreviewOut,
)
def preview_payslip_html_template(
    payload: schemas.PayslipHtmlTemplatePreviewRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    """Live Preview -- renders the in-progress (not-yet-saved) HTML/CSS
    against the selected real employee's actual latest payslip. Never
    fabricates sample data: an employee with no generated payslip yet
    returns a clear error instead of substituting placeholder/mock values."""
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    error = validate_template_html(payload.html_body, payload.css_styles)
    if error:
        raise HTTPException(status_code=400, detail=error)
    slip_id = crud.get_latest_salary_slip_id_for_employee(db, employee.id)
    if slip_id is None:
        raise HTTPException(
            status_code=404,
            detail="This employee has no generated payslip yet -- pick an employee with at "
            "least one payroll run, or generate one first.",
        )
    detail = crud.get_salary_slip_detail(db, slip_id)
    placeholders = build_payslip_placeholders(detail, company)
    rendered = render_payslip_html(payload.html_body, payload.css_styles, placeholders)
    return schemas.PayslipHtmlTemplatePreviewOut(rendered_html=rendered)


def payslip_pdf_bytes(db: Session, company: models.Company, detail: dict) -> bytes:
    """The payslip PDF exactly as Download produces it (also emailed -- see
    routers/email.py). A company's own active HTML Payslip Template, when
    configured, always takes over payslip generation -- falls back to the
    legacy JSON-template/reportlab renderer for every company that hasn't
    authored one."""
    html_template = crud.get_payslip_html_template(db, company.id)
    if html_template is not None and html_template.is_active:
        placeholders = build_payslip_placeholders(detail, company)
        rendered = render_payslip_html(html_template.html_body, html_template.css_styles, placeholders)
        return render_html_to_pdf(rendered)
    template = crud.get_payslip_template(db, company.id)
    return render_payslip_pdf(detail, template, company.name)


@router.get("/payroll/payslips/{slip_id}/pdf")
def download_payslip_pdf(
    slip_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
):
    company = db.get(models.Company, current_user.company_id)
    detail = crud.get_salary_slip_detail(db, slip_id)
    if detail is None or detail["employee"] is None or detail["employee"].company_id != company.id:
        raise HTTPException(status_code=404, detail="Payslip not found")
    if scope == "self" and detail["employee"].id != current_user.employee_id:
        raise HTTPException(status_code=403, detail="You don't have permission to view this")
    period = f"{detail['run'].period_year}-{detail['run'].period_month:02d}" if detail["run"] else "slip"
    pdf_bytes = payslip_pdf_bytes(db, company, detail)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="payslip-{period}.pdf"'},
    )


@router.get("/payroll/payslips/{slip_id}", response_model=None)
def get_payslip_detail(
    slip_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
) -> dict:
    """JSON sibling of download_payslip_pdf's detail fetch -- same
    self-scoping, same crud.get_salary_slip_detail source, so the in-app
    payslip preview can show the real per-component Earnings/Deductions/
    Benefits breakdown instead of only the gross/deductions/net totals
    list_payslips returns. response_model=None (a plain dict), matching
    this file's existing convention for endpoints not worth a full
    Pydantic schema (see GET /employees/full elsewhere in this app)."""
    company = db.get(models.Company, current_user.company_id)
    detail = crud.get_salary_slip_detail(db, slip_id)
    if detail is None or detail["employee"] is None or detail["employee"].company_id != company.id:
        raise HTTPException(status_code=404, detail="Payslip not found")
    if scope == "self" and detail["employee"].id != current_user.employee_id:
        raise HTTPException(status_code=403, detail="You don't have permission to view this")
    slip = detail["slip"]
    run = detail["run"]
    return {
        "id": str(slip.id),
        "employee_id": str(slip.employee_id),
        "month": f"{calendar.month_name[run.period_month]} {run.period_year}" if run else None,
        "working_days": slip.working_days,
        "lop_days": slip.lop_days,
        "gross_pay": float(slip.gross_pay),
        "total_deductions": float(slip.total_deductions),
        "net_pay": float(slip.net_pay),
        "reimbursements_total": float(slip.reimbursements_total or 0),
        # What the employee actually receives: net salary + reimbursements.
        "total_payable": round(float(slip.net_pay) + float(slip.reimbursements_total or 0), 2),
        "status": slip.status,
        "run_status": run.status if run else None,
        "payable_days": float(slip.payable_days) if slip.payable_days is not None else None,
        "flags": [f for f in (slip.flags or "").split(",") if f],
        "deduction_carry_forward": float(slip.deduction_carry_forward or 0),
        "lines": detail["lines"],
        "reimbursements": detail["reimbursements"],
    }


@router.get("/payroll/payslips", response_model=list[schemas.PayslipOut])
def list_payslips(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
):
    company = db.get(models.Company, current_user.company_id)
    # self-scope filters after the DB fetch, so pagination is only applied
    # at the DB level for the org-wide (non-self) case -- otherwise a page
    # could miss this caller's own rows entirely if they aren't in the
    # first `limit` company-wide rows. The caller's own rows are paged after
    # filtering, so limit/offset mean the same thing in both scopes (API-05).
    if scope == "self":
        payslips = crud.list_payslips(db, company.id)
        own = [p for p in payslips if p["employee_id"] == current_user.employee_id]
        return own[offset: offset + limit] if limit is not None else own[offset:]
    return crud.list_payslips(db, company.id, limit=limit, offset=offset)


@router.get("/payroll/summary", response_model=schemas.PayrollSummaryOut)
def payroll_summary(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
):
    if scope == "self":
        # Company-wide aggregate figures -- not meant for a self-scoped
        # role (the frontend's own _PayrollSelf view never calls this
        # endpoint), matching the same e/a-only rule require_permission
        # already enforces for payroll mutations.
        raise HTTPException(status_code=403, detail="You don't have permission to view this")
    company = db.get(models.Company, current_user.company_id)
    return crud.payroll_dashboard_summary(db, company.id)


@router.get(
    "/employees/{employee_id}/salary-structure",
    response_model=schemas.SalaryStructureOut | None,
)
def get_employee_salary_structure(
    employee_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
):
    try:
        emp_uuid = uuid.UUID(employee_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Employee not found")
    if scope == "self" and emp_uuid != current_user.employee_id:
        raise HTTPException(status_code=403, detail="You don't have permission to view this")
    employee = db.get(models.Employee, emp_uuid)
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")  # other company in this tenant
    # L-33: "nothing assigned yet" is a normal state, not an error --
    # 200 with a null body (the Flutter app treats null as "no structure").
    # 404 stays reserved for an unknown / other-company employee.
    return crud.get_employee_salary_structure(db, emp_uuid)


@router.get("/payroll/runs", response_model=list[schemas.PayrollRunOut])
def list_payroll_runs(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_payroll_edit),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_payroll_runs(db, company.id)


@router.post(
    "/payroll/runs",
    response_model=schemas.PayrollRunOut,
    status_code=201,
)
def create_payroll_run(
    payload: schemas.PayrollRunCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("payroll_process", "payroll", "generate")
    ),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        run = crud.create_payroll_run(db, company.id, payload.period_month, payload.period_year)
        crud.create_audit_log(db, company.id, current_user.id, "create", "payroll_run", run.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(run)
    return run


def _reimbursement_out(
    db: Session,
    claim,
    names: dict[uuid.UUID, str] | None = None,
) -> schemas.ReimbursementOut:
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    return schemas.ReimbursementOut(
        id=claim.id,
        employee_id=claim.employee_id,
        category=claim.purpose or "",
        amount=float(claim.total_amount),
        expense_date=claim.claim_date,
        status=claim.status,
        approver_id=claim.approver_id,
        approver_name=name(claim.approver_id),
        decision_notes=claim.decision_notes,
        decided_at=claim.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, claim.employee_id), claim.approver_id
        ),
    )


@router.get("/reimbursements", response_model=list[schemas.ReimbursementOut])
def list_reimbursements(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
):
    company = db.get(models.Company, current_user.company_id)
    # Self-scope: the caller's own claims plus those of employees whose
    # requests they decide (reporting subtree / fallback approver) -- the
    # same scope as list_expense_reports. Own-only hid a report's claim from
    # an IC reporting manager who could still approve it.
    if scope == "self":
        visible_ids = crud.get_visible_employee_ids_for_requests(db, current_user, "expense_claim")
        if visible_ids is None:
            visible_ids = [current_user.employee_id] if current_user.employee_id else []
        claims = crud.list_reimbursements(
            db, company.id, employee_ids=visible_ids, limit=limit, offset=offset,
        )
    else:
        claims = crud.list_reimbursements(db, company.id, limit=limit, offset=offset)
    approver_ids = {c.approver_id for c in claims if c.approver_id}
    names = crud.employee_display_names_bulk(db, approver_ids)
    return [_reimbursement_out(db, claim, names=names) for claim in claims]


@router.post(
    "/reimbursements",
    response_model=schemas.ReimbursementOut,
    status_code=201,
)
def create_reimbursement(
    payload: schemas.ReimbursementCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "payroll_process", "payroll", "generate"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    # WF-03: a requester with no Reporting Manager (e.g. the CEO) is no
    # longer refused -- crud.notify_new_request routes the request to the
    # fallback approvers (Owner / System Settings RBAC holders) who can
    # decide it; self-approval stays blocked.
    claim = crud.create_reimbursement(
        db,
        company.id,
        payload.employee_id,
        payload.category,
        payload.amount,
        payload.expense_date,
    )
    # A double-submit within the replay window returns the existing pending
    # claim (app/expense_claims.py) -- no second audit entry / notification.
    replay = bool(getattr(claim, "idempotent_replay", False))
    if not replay:
        crud.create_audit_log(db, company.id, current_user.id, "create", "reimbursement", claim.id)
    db.commit()
    db.refresh(claim)
    if not replay:
        crud.notify_new_request(db, company.id, employee, "Reimbursement", "reimbursement", claim.id)
        db.commit()
    return _reimbursement_out(db, claim)


@router.patch(
    "/reimbursements/{claim_id}",
    response_model=schemas.ReimbursementOut,
)
def update_reimbursement(
    claim_id: uuid.UUID,
    payload: schemas.ReimbursementUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py.
    current_user: models.User = Depends(get_current_user),
):
    claim = crud.get_expense_claim_for_update(db, claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Reimbursement not found")
    # Two Reporting Managers: same atomic-with-lock guard as
    # update_work_entry/update_leave_request -- blocks a conflicting second
    # decision from the employee's other manager. Checked before the
    # permission gate so that manager sees a clear "already decided"
    # message instead of a confusing permission error once
    # can_decide_request_configurable (correctly) stops recognizing them
    # as the current decider.
    if claim.status in ("approved", "rejected"):
        decider = crud.employee_display_name(db, claim.approver_id)
        decider_type = crud.decided_by_manager_type(
            db.get(models.Employee, claim.employee_id), claim.approver_id
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"This request was already {claim.status} by {decider}"
                f" ({decider_type}) and cannot be decided again"
                if decider_type
                else f"This request was already {claim.status} and cannot be decided again"
            ),
        )
    if not crud.can_decide_request_configurable(
        db, current_user, claim.employee_id, "expense_claim", claim.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's assigned Reporting Manager can act on this request",
        )
    effective_status = crud.decide_configurable_request(
        db, current_user, claim.employee_id, "expense_claim", claim.id,
        payload.status, payload.decision_notes,
    )
    claim.status = effective_status
    claim.approver_id = current_user.employee_id
    claim.decision_notes = payload.decision_notes
    if effective_status != "pending":
        claim.decided_at = datetime.now(timezone.utc)
    crud.create_audit_log(db, claim.company_id, current_user.id, payload.status, "reimbursement", claim.id)
    db.commit()
    db.refresh(claim)
    if effective_status != "pending":
        crud.notify_decision(
            db, claim.company_id, claim.employee_id, "Reimbursement",
            effective_status, payload.decision_notes, "reimbursement", claim.id,
        )
        db.commit()
    return _reimbursement_out(db, claim)


def _salary_revision_out(
    db: Session,
    r: models.SalaryRevisionRequest,
    names: dict[uuid.UUID, str] | None = None,
) -> schemas.SalaryRevisionRequestOut:
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    return schemas.SalaryRevisionRequestOut(
        id=r.id,
        employee_id=r.employee_id,
        current_ctc=r.current_ctc,
        proposed_ctc=r.proposed_ctc,
        reason=r.reason,
        status=r.status,
        approver_id=r.approver_id,
        approver_name=name(r.approver_id),
        decision_notes=r.decision_notes,
        decided_at=r.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, r.employee_id), r.approver_id
        ),
    )


@router.get(
    "/salary-revision-requests", response_model=list[schemas.SalaryRevisionRequestOut]
)
def list_salary_revision_requests(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
):
    company = db.get(models.Company, current_user.company_id)
    # See list_payslips' identical comment -- self-scope filters after the
    # DB fetch, then pages the caller's own rows (API-05).
    if scope == "self":
        requests = crud.list_salary_revision_requests(db, company.id)
        requests = [r for r in requests if r.employee_id == current_user.employee_id]
        requests = requests[offset: offset + limit] if limit is not None else requests[offset:]
    else:
        requests = crud.list_salary_revision_requests(db, company.id, limit=limit, offset=offset)
    approver_ids = {r.approver_id for r in requests if r.approver_id}
    names = crud.employee_display_names_bulk(db, approver_ids)
    return [_salary_revision_out(db, r, names=names) for r in requests]


@router.post(
    "/salary-revision-requests",
    response_model=schemas.SalaryRevisionRequestOut,
    status_code=201,
)
def create_salary_revision_request(
    payload: schemas.SalaryRevisionRequestCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "payroll_process", "payroll", "generate"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    if employee.reporting_manager_id is None:
        raise HTTPException(
            status_code=400,
            detail="You don't have a Reporting Manager assigned yet. Contact HR before submitting this request.",
        )
    # M-33: the current CTC is read on the server, never taken from the client.
    request = crud.create_salary_revision_request(
        db, payload.employee_id, payroll_period.current_ctc(db, employee), payload.proposed_ctc, payload.reason
    )
    crud.create_audit_log(
        db, company.id, current_user.id, "create", "salary_revision_request", request.id
    )
    db.commit()
    db.refresh(request)
    crud.notify_new_request(
        db, company.id, employee, "Salary Revision Request", "salary_revision_request", request.id
    )
    db.commit()
    return _salary_revision_out(db, request)


@router.patch(
    "/salary-revision-requests/{request_id}",
    response_model=schemas.SalaryRevisionRequestOut,
)
def update_salary_revision_request(
    request_id: uuid.UUID,
    payload: schemas.SalaryRevisionRequestUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py.
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    request = crud.get_salary_revision_request_for_update(db, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Salary revision request not found")
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
        db, current_user, request.employee_id, "salary_revision_request", request.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's assigned Reporting Manager can act on this request",
        )
    if payload.status == "approved" and payroll_period.current_assignment(
            db, request.employee_id, crud.company_today(db, company.id)) is None:
        raise HTTPException(status_code=409, detail="This employee has no salary structure assigned -- "
                                                    "assign one before approving a salary revision.")
    effective_status = crud.decide_configurable_request(
        db, current_user, request.employee_id, "salary_revision_request", request.id,
        payload.status, payload.decision_notes,
    )
    if effective_status == "approved":
        # M-33: the approved revision actually takes effect.
        try:
            payroll_period.apply_approved_revision(db, request, current_user.id)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.status = effective_status
    request.approver_id = current_user.employee_id
    request.decision_notes = payload.decision_notes
    if effective_status != "pending":
        request.decided_at = datetime.now(timezone.utc)
    crud.create_audit_log(
        db, company.id, current_user.id, payload.status, "salary_revision_request", request.id
    )
    db.commit()
    db.refresh(request)
    if effective_status != "pending":
        crud.notify_decision(
            db, company.id, request.employee_id, "Salary Revision Request",
            effective_status, payload.decision_notes, "salary_revision_request", request.id,
        )
        db.commit()
    return _salary_revision_out(db, request)


@router.get("/loans", response_model=list[schemas.LoanOut])
def list_loans(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
):
    company = db.get(models.Company, current_user.company_id)
    # See list_payslips' identical comment -- self-scope filters after the
    # DB fetch, then pages the caller's own rows (API-05).
    if scope == "self":
        loans = crud.list_loans(db, company.id)
        loans = [l for l in loans if l.employee_id == current_user.employee_id]
        loans = loans[offset: offset + limit] if limit is not None else loans[offset:]
    else:
        loans = crud.list_loans(db, company.id, limit=limit, offset=offset)
    return loans


@router.post(
    "/loans",
    response_model=schemas.LoanOut,
    status_code=201,
)
def create_loan(
    payload: schemas.LoanCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("payroll_process", "payroll", "generate")
    ),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        loan = crud.create_loan(
            db,
            payload.employee_id,
            payload.loan_type,
            payload.principal_amount,
            payload.emi_amount,
            payload.outstanding_balance,
            company_id=company.id,  # M-31: never another company's employee
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Employee not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "create", "loan", loan.id)
    db.commit()
    db.refresh(loan)
    return loan


@router.get("/tax-declarations", response_model=list[schemas.TaxDeclarationOut])
def list_tax_declarations(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    scope: str = Depends(get_payroll_scope),
):
    company = db.get(models.Company, current_user.company_id)
    # See list_payslips' identical comment -- self-scope filters after the
    # DB fetch, then pages the caller's own rows (API-05).
    if scope == "self":
        declarations = crud.list_tax_declarations(db, company.id)
        declarations = [d for d in declarations if d.employee_id == current_user.employee_id]
        declarations = declarations[offset: offset + limit] if limit is not None else declarations[offset:]
    else:
        declarations = crud.list_tax_declarations(db, company.id, limit=limit, offset=offset)
    return declarations


@router.post(
    "/tax-declarations",
    response_model=schemas.TaxDeclarationOut,
    status_code=201,
)
def create_tax_declaration(
    payload: schemas.TaxDeclarationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "payroll_process", "payroll", "generate"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    try:
        declaration = crud.create_tax_declaration(
            db,
            payload.employee_id,
            payload.fiscal_year,
            payload.tax_regime,
            payload.hra_claimed,
            payload.section_80c,
            payload.section_80d,
            payload.section_80ccd_1b,
            payload.home_loan_interest,
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "tax_declaration", declaration.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(declaration)
    return declaration
