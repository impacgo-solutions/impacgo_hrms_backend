"""Travel & Expenses configuration and lookups: the form dropdown values, the
Expense Claim Type management API, employee advances, and the choices the
claim form needs (who can be Expense Approver)."""

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import crud, form_config, models, schemas, travel_expense as te
from ..database import get_db
from ..deps import get_current_user, require_module_enabled, require_permission

router = APIRouter(
    prefix="/api", tags=["travel"],
    dependencies=[Depends(require_module_enabled("travel"))],
)


@router.get("/travel-expense/options")
def travel_expense_options(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Dropdown values for the Travel Request and Expense Claim forms. The
    claim types are the company's own active Expense Claim Types."""
    types = te.active_claim_types(db, current_user.company_id)
    db.commit()  # ensure_default_claim_types / default options may have been seeded
    return {
        # Per-company Form Configuration options (stored values, admin order).
        "travelTypes": form_config.option_values(db, current_user.company_id, "travel.type"),
        "travelFunding": form_config.option_values(db, current_user.company_id, "travel.funding"),
        "sponsoredFunding": list(schemas.SPONSORED_FUNDING),
        "travelModes": form_config.option_values(db, current_user.company_id, "travel.mode"),
        "idDocumentTypes": form_config.option_values(db, current_user.company_id, "travel.id_document_type"),
        "claimTypes": sorted((t.name for t in types.values()), key=str.lower),
        "canManageClaimTypes": crud.is_fallback_approver(db, current_user)
        or crud.effective_user_matrix(db, current_user).get("travel_expense_approval") in ("e", "a"),
    }


@router.get("/travel-expense/approvers")
def expense_approver_choices(
    employee_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Who the employee may name as Expense Approver, and the default (their
    reporting manager). Only for yourself, or for someone you can see."""
    target_id = employee_id or current_user.employee_id
    employee = db.get(models.Employee, target_id) if target_id else None
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if employee.id != current_user.employee_id:
        visible = crud.get_visible_employee_ids_for_requests(db, current_user, "expense_claim")
        if visible is not None and employee.id not in visible:
            raise HTTPException(status_code=404, detail="Employee not found")
    ids = te.allowed_expense_approver_ids(db, employee)
    people = db.scalars(select(models.Employee).where(models.Employee.id.in_(ids or {uuid.UUID(int=0)}),
                                                      models.Employee.is_active.is_(True))).all()
    department = db.get(models.Department, employee.department_id) if employee.department_id else None
    return {
        "default": str(employee.reporting_manager_id) if employee.reporting_manager_id in {p.id for p in people} else None,
        "department": department.name if department else None,
        "approvers": sorted(
            ({"id": str(p.id), "name": crud.employee_display_name(db, p.id), "code": p.employee_code} for p in people),
            key=lambda x: x["name"].lower()),
    }


# ─────────────────────────── expense claim types ────────────────────────────


@router.get("/expense-claim-types", response_model=list[schemas.ExpenseClaimTypeOut])
def list_expense_claim_types(
    q: str | None = Query(default=None, max_length=80),
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    te.ensure_default_claim_types(db, current_user.company_id)
    db.commit()
    stmt = select(models.ExpenseClaimType).where(models.ExpenseClaimType.company_id == current_user.company_id)
    if q and q.strip():
        like = f"%{crud.escape_like(q.strip())}%"
        stmt = stmt.where(or_(models.ExpenseClaimType.name.ilike(like, escape="\\"),
                              models.ExpenseClaimType.description.ilike(like, escape="\\")))
    if is_active is not None:
        stmt = stmt.where(models.ExpenseClaimType.is_active.is_(is_active))
    rows = db.scalars(stmt.order_by(func.lower(models.ExpenseClaimType.name))).all()
    return [te.claim_type_out(db, current_user.company_id, t) for t in rows]


def _name_clash(db: Session, company_id: uuid.UUID, name: str, exclude: uuid.UUID | None = None) -> bool:
    stmt = select(models.ExpenseClaimType.id).where(
        models.ExpenseClaimType.company_id == company_id, func.lower(models.ExpenseClaimType.name) == name.lower())
    if exclude is not None:
        stmt = stmt.where(models.ExpenseClaimType.id != exclude)
    return db.scalar(stmt.limit(1)) is not None


@router.post("/expense-claim-types", response_model=schemas.ExpenseClaimTypeOut, status_code=201)
def create_expense_claim_type(
    payload: schemas.ExpenseClaimTypeIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("travel_expense_approval")),
):
    te.ensure_default_claim_types(db, current_user.company_id)
    if _name_clash(db, current_user.company_id, payload.name):
        raise HTTPException(status_code=409, detail=f"Expense Claim Type '{payload.name}' already exists")
    row = models.ExpenseClaimType(
        id=uuid.uuid4(), company_id=current_user.company_id, name=payload.name, description=payload.description,
        is_active=payload.is_active, created_by=current_user.id)
    db.add(row)
    db.flush()
    crud.create_audit_log(db, current_user.company_id, current_user.id, "create", "expense_claim_type", row.id)
    db.commit()
    return te.claim_type_out(db, current_user.company_id, row)


def _get_type(db: Session, company_id: uuid.UUID, type_id: uuid.UUID) -> models.ExpenseClaimType:
    row = db.get(models.ExpenseClaimType, type_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(status_code=404, detail="Expense Claim Type not found")
    return row


@router.patch("/expense-claim-types/{type_id}", response_model=schemas.ExpenseClaimTypeOut)
def update_expense_claim_type(
    type_id: uuid.UUID,
    payload: schemas.ExpenseClaimTypeUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("travel_expense_approval")),
):
    row = _get_type(db, current_user.company_id, type_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("name") and changes["name"].lower() != row.name.lower():
        if _name_clash(db, current_user.company_id, changes["name"], exclude=row.id):
            raise HTTPException(status_code=409, detail=f"Expense Claim Type '{changes['name']}' already exists")
        # Rows already filed keep their stored name; follow the rename so the
        # history, reports and the dropdown stay one consistent label.
        old = row.name
        company_id = current_user.company_id
        # Company-scoped: tenants can share these tables, so only this
        # company's own rows follow the rename.
        for line in db.scalars(
                select(models.ExpenseClaimLine)
                .join(models.ExpenseClaim, models.ExpenseClaim.id == models.ExpenseClaimLine.claim_id)
                .where(models.ExpenseClaim.company_id == company_id,
                       or_(models.ExpenseClaimLine.claim_type_id == row.id,
                           func.lower(models.ExpenseClaimLine.category) == old.lower()))):
            line.category = changes["name"]
        for c in db.scalars(
                select(models.TravelCostingRow)
                .join(models.TravelRequestModel, models.TravelRequestModel.id == models.TravelCostingRow.travel_request_id)
                .join(models.Employee, models.Employee.id == models.TravelRequestModel.employee_id)
                .where(models.Employee.company_id == company_id,
                       func.lower(models.TravelCostingRow.expense_type) == old.lower())):
            c.expense_type = changes["name"]
    for k in ("name", "description", "is_active"):
        if k in changes:
            setattr(row, k, changes[k])
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "expense_claim_type", row.id, changes=changes)
    db.commit()
    return te.claim_type_out(db, current_user.company_id, row)


@router.delete("/expense-claim-types/{type_id}", status_code=204)
def delete_expense_claim_type(
    type_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("travel_expense_approval")),
):
    row = _get_type(db, current_user.company_id, type_id)
    used = te.claim_type_usage(db, current_user.company_id, row)
    if used:
        raise HTTPException(
            status_code=409,
            detail=f"'{row.name}' is used by {used} expense row{'s' if used != 1 else ''}; deactivate it instead of deleting it.")
    crud.create_audit_log(db, current_user.company_id, current_user.id, "delete", "expense_claim_type", row.id)
    db.delete(row)
    db.commit()


# ───────────────────────────── employee advances ────────────────────────────


def _advance_out(db: Session, a: models.EmployeeAdvance, names: dict | None = None) -> schemas.EmployeeAdvanceOut:
    name = (names or {}).get(a.employee_id) or crud.employee_display_name(db, a.employee_id)
    return schemas.EmployeeAdvanceOut(
        id=a.id, employee_id=a.employee_id, employee_name=name, posting_date=a.posting_date, purpose=a.purpose,
        advance_amount=float(a.advance_amount), claimed_amount=float(a.claimed_amount),
        returned_amount=float(a.returned_amount), outstanding=a.outstanding, status=a.status,
        travel_request_id=a.travel_request_id, available=te.advance_available(db, a))


@router.get("/employee-advances", response_model=list[schemas.EmployeeAdvanceOut])
def list_employee_advances(
    employee_id: uuid.UUID | None = None,
    only_open: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Advances you may see: your own, plus those of people whose claims you can see.
    only_open keeps those with something still unclaimed."""
    stmt = select(models.EmployeeAdvance).where(models.EmployeeAdvance.company_id == current_user.company_id)
    visible = crud.get_visible_employee_ids_for_requests(db, current_user, "expense_claim")
    if employee_id is not None:
        if employee_id != current_user.employee_id and visible is not None and employee_id not in visible:
            raise HTTPException(status_code=404, detail="Employee not found")
        stmt = stmt.where(models.EmployeeAdvance.employee_id == employee_id)
    elif visible is not None:
        stmt = stmt.where(models.EmployeeAdvance.employee_id.in_(set(visible) | {current_user.employee_id}))
    rows = db.scalars(stmt.order_by(models.EmployeeAdvance.posting_date.desc(), models.EmployeeAdvance.id)).all()
    names = crud.employee_display_names_bulk(db, {a.employee_id for a in rows})
    out = [_advance_out(db, a, names) for a in rows]
    return [a for a in out if a.available > 0] if only_open else out


@router.post("/employee-advances", response_model=schemas.EmployeeAdvanceOut, status_code=201)
def create_employee_advance(
    payload: schemas.EmployeeAdvanceIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("travel_expense_approval")),
):
    """Finance / an administrator records an advance paid to an employee."""
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if payload.travel_request_id is not None:
        trip = db.get(models.TravelRequestModel, payload.travel_request_id)
        if trip is None or trip.employee_id != employee.id:
            raise HTTPException(status_code=404, detail="That travel request was not found for this employee")
    row = models.EmployeeAdvance(
        id=uuid.uuid4(), company_id=current_user.company_id, employee_id=employee.id, posting_date=payload.posting_date,
        purpose=payload.purpose, advance_amount=te.money(payload.advance_amount), status="paid",
        travel_request_id=payload.travel_request_id, created_by=current_user.id)
    db.add(row)
    db.flush()
    crud.create_audit_log(db, current_user.company_id, current_user.id, "create", "employee_advance", row.id)
    db.commit()
    return _advance_out(db, row)
