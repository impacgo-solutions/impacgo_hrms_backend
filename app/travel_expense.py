"""Travel Requests and Expense Claims (full forms).

Rules and builders shared by routers/travel.py and routers/travel_expense_config.py:

* Expense Claim Types -- configurable per company, seeded with Calls / Food /
  Medical / Others / Travel the first time they are needed.
* Travel Request -- header + itinerary rows + costing rows. destination,
  from_date, to_date, travel_mode and estimated_cost are DERIVED from those
  rows, so payroll reimbursement and the approvals inbox keep reading the
  columns they always read.
* Expense Claim -- expenses + taxes & charges + advances applied. The total is
  worked out here, never taken from the client: total_claimed (expenses) +
  total_taxes - advance_amount = total_amount, which is what payroll pays.
  Advances are only marked claimed when the claim is APPROVED.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import crud, form_config, models, schemas

DEFAULT_CLAIM_TYPES = ("Calls", "Food", "Medical", "Others", "Travel")
# A claim / request in one of these can still be edited or deleted by its owner.
EDITABLE_STATUSES = ("draft", "sent_back")
# Statuses in which a claim still holds a reservation on an advance.
OPEN_CLAIM_STATUSES = ("draft", "pending", "sent_back")


def money(value: float | None) -> float:
    return round(float(value or 0), 2)


def _bad(detail: str, status: int = 400):
    raise HTTPException(status_code=status, detail=detail)


# ─────────────────────────── expense claim types ───────────────────────────


def ensure_default_claim_types(db: Session, company_id: uuid.UUID) -> None:
    """Seeds the default types once for a company that has none (new tenants
    and companies created after the migration). Never touches existing rows."""
    exists = db.scalar(
        select(models.ExpenseClaimType.id).where(models.ExpenseClaimType.company_id == company_id).limit(1)
    )
    if exists is not None:
        return
    for name in DEFAULT_CLAIM_TYPES:
        db.add(models.ExpenseClaimType(id=uuid.uuid4(), company_id=company_id, name=name, is_active=True))
    db.flush()


def claim_type_usage(db: Session, company_id: uuid.UUID, type_row: models.ExpenseClaimType) -> int:
    lines = db.scalar(
        select(func.count()).select_from(models.ExpenseClaimLine)
        .join(models.ExpenseClaim, models.ExpenseClaim.id == models.ExpenseClaimLine.claim_id)
        .where(
            models.ExpenseClaim.company_id == company_id,
            or_(
                models.ExpenseClaimLine.claim_type_id == type_row.id,
                func.lower(models.ExpenseClaimLine.category) == type_row.name.lower(),
            ),
        )
    ) or 0
    costing = db.scalar(
        select(func.count()).select_from(models.TravelCostingRow)
        .join(models.TravelRequestModel, models.TravelRequestModel.id == models.TravelCostingRow.travel_request_id)
        .join(models.Employee, models.Employee.id == models.TravelRequestModel.employee_id)
        .where(
            models.Employee.company_id == company_id,
            func.lower(models.TravelCostingRow.expense_type) == type_row.name.lower(),
        )
    ) or 0
    return int(lines) + int(costing)


def claim_type_out(db: Session, company_id: uuid.UUID, t: models.ExpenseClaimType) -> schemas.ExpenseClaimTypeOut:
    return schemas.ExpenseClaimTypeOut(
        id=t.id, name=t.name, description=t.description, is_active=t.is_active,
        usage_count=claim_type_usage(db, company_id, t), created_at=t.created_at,
    )


def active_claim_types(db: Session, company_id: uuid.UUID) -> dict[str, models.ExpenseClaimType]:
    ensure_default_claim_types(db, company_id)
    return {
        t.name.lower(): t
        for t in db.scalars(select(models.ExpenseClaimType).where(
            models.ExpenseClaimType.company_id == company_id, models.ExpenseClaimType.is_active.is_(True)))
    }


def claim_types_for(db: Session, company_id: uuid.UUID, claim_id: uuid.UUID | None) -> dict[str, models.ExpenseClaimType]:
    """The active types plus, when editing, the types this claim already
    uses -- a type deactivated after the claim was drafted stays valid on it
    (new rows still need an active type)."""
    types = active_claim_types(db, company_id)
    if claim_id is not None:
        for t in db.scalars(
            select(models.ExpenseClaimType)
            .join(models.ExpenseClaimLine, models.ExpenseClaimLine.claim_type_id == models.ExpenseClaimType.id)
            .where(models.ExpenseClaimLine.claim_id == claim_id)
        ):
            types.setdefault(t.name.lower(), t)
    return types


def _resolve_type(types: dict[str, models.ExpenseClaimType], name: str, where: str) -> models.ExpenseClaimType:
    t = types.get((name or "").strip().lower())
    if t is None:
        _bad(f"'{name}' is not an active Expense Claim Type ({where}). Pick one from the list, "
             "or ask an administrator to add or re-activate it.")
    return t


# ─────────────────────────────── travel request ────────────────────────────


def derive_travel_header(itinerary: list[schemas.TravelItineraryIn], costing: list[schemas.TravelCostingIn]) -> dict:
    legs = sorted(itinerary, key=lambda r: r.departure_at)
    first, last = legs[0], legs[-1]
    # A round trip (ends where it began) is "to" its furthest stop, not home.
    destination = first.travel_to if last.travel_to.lower() == first.travel_from.lower() else last.travel_to
    modes = list(dict.fromkeys(r.mode_of_travel for r in legs))
    return {
        "destination": destination[:150],
        "from_date": first.departure_at.date(),
        "to_date": max((r.arrival_at or r.departure_at) for r in legs).date(),
        "travel_mode": ("/".join(modes))[:40],
        "estimated_cost": money(sum(c.sponsored_amount + c.funded_amount for c in costing)),
    }


def find_travel_overlap(
    db: Session, employee_id: uuid.UUID, start: datetime.date, end: datetime.date, exclude_id: uuid.UUID | None = None
) -> models.TravelRequestModel | None:
    q = select(models.TravelRequestModel).where(
        models.TravelRequestModel.employee_id == employee_id,
        models.TravelRequestModel.status.in_(("pending", "approved", "sent_back")),
        models.TravelRequestModel.from_date <= end,
        models.TravelRequestModel.to_date >= start,
    )
    if exclude_id is not None:
        q = q.where(models.TravelRequestModel.id != exclude_id)
    return db.scalar(q.limit(1))


def check_project(db: Session, company_id: uuid.UUID, project_id: uuid.UUID | None) -> None:
    if project_id is None:
        return
    project = db.get(models.Project, project_id)
    if project is None or project.company_id != company_id:
        _bad("The selected project was not found.", 404)


# Funding values that need "Details of Sponsor". This rule is tied to the two
# built-in stored values; a funding option an admin adds in Form Configuration
# is treated as NOT sponsored (it cannot trigger the sponsor-details rule).
SPONSORED_FUNDING = tuple(x.lower() for x in schemas.SPONSORED_FUNDING)


def require_custom_complete(db: Session, company_id: uuid.UUID, form_key: str, entity_id: uuid.UUID) -> None:
    """On SUBMIT every required custom field of the form must hold a value
    (drafts may be incomplete). Checkbox fields are never 'missing'."""
    required = [d for d in form_config.custom_field_defs(db, company_id, form_key)
                if d.is_required and d.field_type != "checkbox"]
    if not required:
        return
    have = form_config.load_custom_values(db, company_id, form_key, [entity_id]).get(entity_id, {})
    for d in required:
        if not (have.get(d.field_key) or "").strip():
            raise HTTPException(status_code=422, detail=f"{d.label} is required.")


def clean_custom(db: Session, company_id: uuid.UUID, form_key: str, raw: dict | None, draft: bool,
                 entity_id: uuid.UUID | None = None) -> dict | None:
    """Validates the custom values a form sent (None = not sent -> None),
    BEFORE anything is written. A draft may be incomplete: a blank REQUIRED
    field is simply cleared. A submit ([draft] False) needs every required
    field to hold a value, sent now or already stored for [entity_id].
    Type errors, unknown keys and unavailable dropdown choices are always
    refused."""
    if not draft:
        required = [d for d in form_config.custom_field_defs(db, company_id, form_key)
                    if d.is_required and d.field_type != "checkbox"]
        if required:
            stored = (form_config.load_custom_values(db, company_id, form_key, [entity_id]).get(entity_id, {})
                      if entity_id else {})
            sent = raw or {}
            for d in required:
                value = sent[d.field_key] if d.field_key in sent else stored.get(d.field_key)
                if value is None or str(value).strip() == "":
                    raise HTTPException(status_code=422, detail=f"{d.label} is required.")
    if raw is None:
        return None
    raw = dict(raw)
    blank_required = []
    if draft:
        defs = {d.field_key: d for d in form_config.custom_field_defs(db, company_id, form_key)}
        blank_required = [k for k, v in raw.items()
                          if k in defs and defs[k].is_required and (v is None or str(v).strip() == "")]
        for k in blank_required:
            del raw[k]
    cleaned = form_config.validate_custom_values(db, company_id, form_key, raw, partial=True)
    for k in blank_required:
        cleaned[k] = None
    return cleaned


def save_custom(db: Session, company_id: uuid.UUID, form_key: str, entity_id: uuid.UUID, raw: dict | None,
                draft: bool = False) -> None:
    """Validates and stores the custom values a form sent (None = not sent)."""
    cleaned = clean_custom(db, company_id, form_key, raw, draft, entity_id)
    if cleaned is not None:
        form_config.save_custom_values(db, company_id, form_key, entity_id, cleaned)


def apply_travel_form(
    db: Session, req: models.TravelRequestModel, p: schemas._TravelFields, company_id: uuid.UUID
) -> None:
    """Writes the whole Travel Request form onto [req], replacing its
    itinerary and costing rows. Validates references; the pydantic model has
    already validated the shape."""
    check_project(db, company_id, p.project_id)
    types = active_claim_types(db, company_id)
    for c in p.costing:
        _resolve_type(types, c.expense_type, "Costing Details")
    # Dropdowns come from the company's Form Configuration. The record's own
    # current (possibly since-hidden) value stays acceptable on edit.
    p.travel_type = form_config.require_option(db, company_id, "travel.type", p.travel_type, current=req.travel_type)
    p.travel_funding = form_config.require_option(
        db, company_id, "travel.funding", p.travel_funding, current=req.travel_funding)
    p.id_document_type = form_config.require_option(
        db, company_id, "travel.id_document_type", p.id_document_type, current=req.id_document_type)
    had_modes = {r.mode_of_travel.lower() for r in (req.itinerary or [])}
    for leg in p.itinerary:
        leg.mode_of_travel = form_config.require_option(
            db, company_id, "travel.mode", leg.mode_of_travel, required=True,
            current=leg.mode_of_travel if leg.mode_of_travel.lower() in had_modes else None)
    # Custom fields are validated before anything is written.
    custom = clean_custom(db, company_id, "travel_request", p.custom_fields, p.save_as_draft, req.id)
    req.purpose = p.purpose
    for field in (
        "travel_type", "travel_funding", "sponsor_details", "contact_number", "contact_email",
        "id_document_type", "id_document_number", "passport_number", "description", "cost_center",
        "project_id", "organizer_name", "organizer_address", "event_details",
    ):
        setattr(req, field, getattr(p, field))
    if (p.travel_funding or "").lower() not in SPONSORED_FUNDING:
        req.sponsor_details = None if p.travel_funding else req.sponsor_details
    if p.itinerary:
        for k, v in derive_travel_header(p.itinerary, p.costing).items():
            setattr(req, k, v)
        req.itinerary = [
            models.TravelItineraryRow(
                id=uuid.uuid4(), sort_order=i, travel_from=r.travel_from, travel_to=r.travel_to,
                mode_of_travel=r.mode_of_travel, departure_at=r.departure_at, arrival_at=r.arrival_at,
            )
            for i, r in enumerate(p.itinerary)
        ]
        req.costing = [
            models.TravelCostingRow(
                id=uuid.uuid4(), sort_order=i, expense_type=types[c.expense_type.lower()].name, comments=c.comments,
                sponsored_amount=money(c.sponsored_amount), funded_amount=money(c.funded_amount),
                total_amount=money(c.sponsored_amount + c.funded_amount),
            )
            for i, c in enumerate(p.costing)
        ]
    else:  # older single-row client
        req.destination, req.from_date, req.to_date = p.destination, p.from_date, p.to_date
        req.travel_mode, req.estimated_cost = p.travel_mode, p.estimated_cost
        req.itinerary, req.costing = [], []
    req.updated_at = datetime.datetime.now(datetime.timezone.utc)
    if custom is not None:
        form_config.save_custom_values(db, company_id, "travel_request", req.id, custom)


def travel_request_out(
    db: Session,
    r: models.TravelRequestModel,
    names: dict[uuid.UUID, str] | None = None,
    codes: dict[uuid.UUID, str] | None = None,
    viewer: models.User | None = None,
    projects: dict[uuid.UUID, str] | None = None,
    custom_values: dict[uuid.UUID, dict[str, str]] | None = None,
) -> schemas.TravelRequestOut:
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    employee = db.get(models.Employee, r.employee_id)
    code = (codes.get(r.employee_id) if codes is not None else (employee.employee_code if employee else None))
    project_name = None
    if r.project_id:
        project_name = projects.get(r.project_id) if projects is not None else None
        if project_name is None and projects is None:
            proj = db.get(models.Project, r.project_id)
            project_name = proj.name if proj else None
    department = db.get(models.Department, employee.department_id) if employee and employee.department_id else None
    sponsored = money(sum(float(c.sponsored_amount) for c in r.costing))
    funded = money(sum(float(c.funded_amount) for c in r.costing))
    return schemas.TravelRequestOut(
        id=r.id, employee_id=r.employee_id, purpose=r.purpose, destination=r.destination,
        from_date=r.from_date, to_date=r.to_date, travel_mode=r.travel_mode,
        estimated_cost=float(r.estimated_cost) if r.estimated_cost is not None else None,
        status=r.status, approver_id=r.approver_id, approver_name=name(r.approver_id),
        decision_notes=r.decision_notes, decided_at=r.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(employee, r.approver_id),
        employee_name=name(r.employee_id), employee_code=code,
        travel_type=r.travel_type, travel_funding=r.travel_funding, sponsor_details=r.sponsor_details,
        contact_number=r.contact_number, contact_email=r.contact_email, id_document_type=r.id_document_type,
        id_document_number=r.id_document_number, passport_number=r.passport_number, description=r.description,
        cost_center=r.cost_center, project_id=r.project_id, project_name=project_name,
        organizer_name=r.organizer_name, organizer_address=r.organizer_address, event_details=r.event_details,
        itinerary=[schemas.TravelItineraryOut.model_validate(i) for i in r.itinerary],
        costing=[schemas.TravelCostingOut(
            id=c.id, expense_type=c.expense_type, comments=c.comments, sponsored_amount=float(c.sponsored_amount),
            funded_amount=float(c.funded_amount), total_amount=float(c.total_amount)) for c in r.costing],
        total_sponsored=sponsored, total_funded=funded, total_cost=money(sponsored + funded),
        submitted_at=r.submitted_at, created_at=r.created_at,
        department_name=department.name if department else None,
        can_edit=bool(viewer and viewer.employee_id == r.employee_id and r.status in EDITABLE_STATUSES),
        custom_fields=(custom_values.get(r.id, {}) if custom_values is not None else
                       form_config.load_custom_values(
                           db, employee.company_id, "travel_request", [r.id]).get(r.id, {})) if employee else {},
    )


# ─────────────────────────────── expense claim ─────────────────────────────


def advance_reserved(db: Session, advance_id: uuid.UUID, exclude_claim_id: uuid.UUID | None = None) -> float:
    """What other open claims have already set aside from this advance."""
    q = (
        select(func.coalesce(func.sum(models.ExpenseClaimAdvance.allocated_amount), 0))
        .join(models.ExpenseClaim, models.ExpenseClaim.id == models.ExpenseClaimAdvance.claim_id)
        .where(
            models.ExpenseClaimAdvance.advance_id == advance_id,
            models.ExpenseClaimAdvance.applied.is_(False),
            models.ExpenseClaim.status.in_(OPEN_CLAIM_STATUSES),
        )
    )
    if exclude_claim_id is not None:
        q = q.where(models.ExpenseClaimAdvance.claim_id != exclude_claim_id)
    return money(db.scalar(q))


def advance_available(db: Session, adv: models.EmployeeAdvance, exclude_claim_id: uuid.UUID | None = None) -> float:
    return money(adv.outstanding - advance_reserved(db, adv.id, exclude_claim_id))


def allowed_expense_approver_ids(db: Session, employee: models.Employee) -> set[uuid.UUID]:
    """Who may be named Expense Approver: the employee's reporting managers
    and everyone above them in the chain (they can already decide the claim),
    plus the company-wide fallback approvers (Owner / RBAC admins). Never the
    claimant."""
    ids: set[uuid.UUID] = set()
    seen: set[uuid.UUID] = set()
    current: uuid.UUID | None = employee.id
    for _ in range(10):
        emp = db.get(models.Employee, current) if current else None
        if emp is None or emp.id in seen:
            break
        seen.add(emp.id)
        for m in (emp.reporting_manager_id, emp.dotted_line_manager_id):
            if m:
                ids.add(m)
        current = emp.reporting_manager_id
    ids |= set(crud.list_fallback_approver_employee_ids(db, employee.company_id, exclude_employee_id=employee.id))
    ids.discard(employee.id)
    return ids


class ClaimTotals:
    def __init__(self, lines_total: float, taxes: list[dict], tax_total: float, advances: list[tuple], advance_total: float):
        self.lines_total, self.taxes, self.tax_total = lines_total, taxes, tax_total
        self.advances, self.advance_total = advances, advance_total
        self.grand_total = money(lines_total + tax_total)
        self.net_payable = money(self.grand_total - advance_total)


def compute_claim(
    db: Session, company_id: uuid.UUID, employee: models.Employee, p: schemas.ExpenseReportApiCreate,
    claim_id: uuid.UUID | None = None,
) -> ClaimTotals:
    """Validates the claim and works out every total. Raises 400/404/409."""
    types = claim_types_for(db, company_id, claim_id)
    for i, item in enumerate(p.items, 1):
        if item.category:  # optional; a given one must be an active type
            _resolve_type(types, item.category, f"expense row {i}")
    # Custom fields are checked with the rest of the claim, before any write.
    clean_custom(db, company_id, "expense_claim", p.custom_fields, p.save_as_draft, claim_id)
    seen = set()
    for item in p.items:
        key = (item.date, (item.category or "").lower(), round(item.amount, 2), (item.description or "").strip().lower())
        if key in seen:
            _bad(f"The expense '{item.description or item.category or 'row'}' on {item.date.isoformat()} for {item.amount:.2f} is entered twice.", 409)
        seen.add(key)
    # The same expense must not be claimed in another open or approved claim.
    for item in p.items:
        dup = db.scalar(
            select(models.ExpenseClaim.claim_no)
            .join(models.ExpenseClaimLine, models.ExpenseClaimLine.claim_id == models.ExpenseClaim.id)
            .where(
                models.ExpenseClaim.employee_id == employee.id,
                models.ExpenseClaim.status.in_(("draft", "pending", "approved", "sent_back")),
                models.ExpenseClaim.id != (claim_id or uuid.UUID(int=0)),
                models.ExpenseClaimLine.expense_date == item.date,
                func.lower(func.coalesce(models.ExpenseClaimLine.category, "")) == (item.category or "").lower(),
                models.ExpenseClaimLine.amount == round(item.amount, 2),
                func.lower(func.coalesce(models.ExpenseClaimLine.description, "")) == (item.description or "").strip().lower(),
            ).limit(1)
        )
        if dup is not None:
            _bad(f"'{item.description or item.category or 'Expense'}' ({item.date.isoformat()}, {item.amount:.2f}) is already claimed in {dup}.", 409)

    lines_total = money(sum(i.amount for i in p.items))
    taxes, tax_total = [], 0.0
    for t in p.taxes:
        amount = money(lines_total * t.rate / 100) if (t.rate is not None and (t.rate > 0 or t.tax_amount is None)) else money(t.tax_amount)
        taxes.append({"account_head": t.account_head, "description": t.description, "rate": t.rate, "tax_amount": amount})
        tax_total += amount
    tax_total = money(tax_total)
    grand = money(lines_total + tax_total)
    if grand > schemas.MAX_NUMERIC_14_2:
        _bad("The expense report total is too large")

    advances, advance_total = [], 0.0
    for a in p.advances:
        adv = db.get(models.EmployeeAdvance, a.advance_id)
        if adv is None or adv.company_id != company_id or adv.employee_id != employee.id:
            _bad("That advance does not belong to this employee.", 404)
        free = advance_available(db, adv, claim_id)
        if money(a.allocated_amount) > free + 0.001:
            _bad(f"Only {free:.2f} is still available on the advance of {float(adv.advance_amount):.2f} "
                 f"({adv.purpose or adv.posting_date.isoformat()}).", 409)
        advances.append((adv, money(a.allocated_amount)))
        advance_total += money(a.allocated_amount)
    advance_total = money(advance_total)
    if advance_total > grand + 0.001:
        _bad(f"The advances applied ({advance_total:.2f}) are more than the report total ({grand:.2f}).")
    return ClaimTotals(lines_total, taxes, tax_total, advances, advance_total)


def check_claim_links(
    db: Session, company_id: uuid.UUID, employee: models.Employee, p: schemas.ExpenseReportApiCreate
) -> None:
    check_project(db, company_id, p.project_id)
    if p.task_id is not None:
        task = db.get(models.TaskBoardCard, p.task_id)
        if task is None or task.company_id != company_id:
            _bad("The selected task was not found.", 404)
        if p.project_id is not None and task.project_id != p.project_id:
            _bad("The selected task does not belong to the selected project.")
    if p.travel_request_id is not None:
        trip = db.get(models.TravelRequestModel, p.travel_request_id)
        if trip is None or trip.employee_id != employee.id:
            _bad("The linked travel request was not found for this employee.", 404)
        if trip.status != "approved":
            _bad("Only an approved travel request can be linked to an expense report.", 409)
    if p.expense_approver_id is not None:
        if p.expense_approver_id not in allowed_expense_approver_ids(db, employee):
            _bad("The Expense Approver must be this employee's reporting manager (or someone above them), "
                 "or an administrator who can approve claims.", 400)


def apply_claim(
    db: Session, claim: models.ExpenseClaim, p: schemas.ExpenseReportApiCreate, totals: ClaimTotals,
    employee: models.Employee, can_set_payment_fields: bool, editing: bool = False,
) -> None:
    default_approver = employee.reporting_manager_id
    claim.expense_approver_id = p.expense_approver_id or claim.expense_approver_id or default_approver
    # An edit keeps the claim's own reference and filing date: the form never
    # sends a reference, and "submitted_date" there is just today.
    if p.reference or not (editing and claim.purpose):
        claim.purpose = p.reference or f"Expense claim {(p.posting_date or p.submitted_date).strftime('%d %b %Y')}"
    if not (editing and claim.claim_date):
        claim.claim_date = p.submitted_date
    claim.posting_date = p.posting_date or p.submitted_date
    claim.payable_account = p.payable_account
    claim.remark = p.remark
    claim.project_id, claim.cost_center = p.project_id, p.cost_center
    claim.task_id, claim.travel_request_id = p.task_id, p.travel_request_id
    if can_set_payment_fields:
        claim.is_paid, claim.clearance_date = p.is_paid, p.clearance_date
    claim.total_claimed, claim.total_taxes = totals.lines_total, totals.tax_total
    claim.advance_amount, claim.total_amount = totals.advance_total, totals.net_payable
    types = claim_types_for(db, claim.company_id, claim.id if editing else None)
    claim.lines = [
        models.ExpenseClaimLine(
            id=uuid.uuid4(), claim_id=claim.id, sort_order=i, expense_date=it.date,
            category=types[it.category.lower()].name if it.category else None,
            claim_type_id=types[it.category.lower()].id if it.category else None,
            description=it.description, amount=money(it.amount))
        for i, it in enumerate(p.items)
    ]
    claim.taxes = [
        models.ExpenseClaimTax(id=uuid.uuid4(), claim_id=claim.id, sort_order=i, **t) for i, t in enumerate(totals.taxes)
    ]
    claim.advances = [
        models.ExpenseClaimAdvance(id=uuid.uuid4(), claim_id=claim.id, advance_id=adv.id, allocated_amount=amount)
        for adv, amount in totals.advances
    ]
    claim.updated_at = datetime.datetime.now(datetime.timezone.utc)
    save_custom(db, claim.company_id, "expense_claim", claim.id, p.custom_fields, p.save_as_draft)


def apply_advances_on_approval(db: Session, claim: models.ExpenseClaim) -> None:
    """Called once, when the claim is finally approved: the advances it used
    become 'claimed' (and the advance row closes when nothing is left)."""
    for link in claim.advances:
        if link.applied:
            continue
        adv = db.get(models.EmployeeAdvance, link.advance_id)
        if adv is None:
            continue
        adv.claimed_amount = money(float(adv.claimed_amount) + float(link.allocated_amount))
        if adv.outstanding <= 0.001:
            adv.status = "claimed"
        link.applied = True
    db.flush()


def can_record_payment(db: Session, user: models.User, employee_id: uuid.UUID | None = None) -> bool:
    """Finance / an administrator records payments: the company-wide
    fallback approvers (Owner / RBAC admins / Approvals admins), Travel &
    Expense Approval at Admin, or Payroll (Process) Edit/Admin -- never an
    ordinary approving manager (Edit), and never on their own claim."""
    if employee_id is not None and user.employee_id == employee_id:
        return False
    matrix = crud.effective_user_matrix(db, user)
    return (crud.is_fallback_approver(db, user)
            or matrix.get("travel_expense_approval") == "a"
            or matrix.get("payroll_process") in ("e", "a"))


def expense_report_out(
    db: Session,
    claim: models.ExpenseClaim,
    names: dict[uuid.UUID, str] | None = None,
    codes: dict[uuid.UUID, str] | None = None,
    viewer: models.User | None = None,
    custom_values: dict[uuid.UUID, dict[str, str]] | None = None,
) -> schemas.ExpenseReportOut:
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    employee = db.get(models.Employee, claim.employee_id)
    code = codes.get(claim.employee_id) if codes is not None else (employee.employee_code if employee else None)
    department = db.get(models.Department, employee.department_id) if employee and employee.department_id else None
    company = db.get(models.Company, claim.company_id)
    project = db.get(models.Project, claim.project_id) if claim.project_id else None
    task = db.get(models.TaskBoardCard, claim.task_id) if claim.task_id else None
    advances = []
    for link in claim.advances:
        adv = db.get(models.EmployeeAdvance, link.advance_id)
        advances.append(schemas.ExpenseAdvanceOut(
            advance_id=link.advance_id, allocated_amount=float(link.allocated_amount),
            purpose=adv.purpose if adv else None, posting_date=adv.posting_date if adv else None,
            advance_amount=float(adv.advance_amount) if adv else None))
    return schemas.ExpenseReportOut(
        id=claim.id, employee_id=claim.employee_id, reference=claim.purpose, claim_date=claim.claim_date,
        status=claim.status,
        items=[schemas.ExpenseLineItemOut(
            expense_date=l.expense_date, category=l.category, description=l.description, amount=float(l.amount),
            claim_type_id=l.claim_type_id) for l in claim.lines],
        approver_id=claim.approver_id, approver_name=name(claim.approver_id), decision_notes=claim.decision_notes,
        decided_at=claim.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(employee, claim.approver_id),
        employee_name=name(claim.employee_id), employee_code=code,
        series=claim.claim_no, expense_approver_id=claim.expense_approver_id,
        expense_approver_name=name(claim.expense_approver_id) if claim.expense_approver_id else None,
        department_name=department.name if department else None, company_name=company.name if company else None,
        posting_date=claim.posting_date or claim.claim_date, clearance_date=claim.clearance_date,
        is_paid=bool(claim.is_paid), payable_account=claim.payable_account, remark=claim.remark,
        project_id=claim.project_id, project_name=project.name if project else None, cost_center=claim.cost_center,
        task_id=claim.task_id, task_title=task.title if task else None, travel_request_id=claim.travel_request_id,
        taxes=[schemas.ExpenseTaxOut(account_head=t.account_head, description=t.description,
                                     rate=float(t.rate) if t.rate is not None else None, tax_amount=float(t.tax_amount))
               for t in claim.taxes],
        advances=advances,
        total_claimed=float(claim.total_claimed or 0) or money(sum(float(l.amount) for l in claim.lines)),
        total_taxes=float(claim.total_taxes or 0), advance_amount=float(claim.advance_amount or 0),
        net_payable=float(claim.total_amount or 0), submitted_at=claim.submitted_at,
        can_edit=bool(viewer and viewer.employee_id == claim.employee_id and claim.status in EDITABLE_STATUSES),
        can_record_payment=bool(viewer and claim.status == "approved"
                                and can_record_payment(db, viewer, claim.employee_id)),
        custom_fields=(custom_values.get(claim.id, {}) if custom_values is not None else
                       form_config.load_custom_values(db, claim.company_id, "expense_claim", [claim.id]).get(claim.id, {})),
    )


SERIES_PREFIX = "HR-EXP"


def next_claim_series(db: Session, company_id: uuid.UUID, on: datetime.date) -> str:
    """Series 'HR-EXP-<year>-<5 digit number>' (the Expense Claim form's
    Series field): the next free number for the year, allocated under a
    company-wide lock so two claims saved at once never share one."""
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"expense_series:{company_id}:{on.year}"))))
    prefix = f"{SERIES_PREFIX}-{on.year}-"
    taken = db.scalars(
        select(models.ExpenseClaim.claim_no).where(
            models.ExpenseClaim.company_id == company_id, models.ExpenseClaim.claim_no.like(f"{prefix}%"))
    ).all()
    highest = 0
    for no in taken:
        tail = no[len(prefix):]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return f"{prefix}{highest + 1:05d}"


def quick_net_total(p: schemas.ExpenseReportApiCreate) -> float:
    """The claim's net total from the payload alone (no database), used to
    recognise a double-submit before the full validation runs."""
    lines = money(sum(i.amount for i in p.items))
    taxes = money(sum(
        (lines * t.rate / 100) if (t.rate is not None and (t.rate > 0 or t.tax_amount is None)) else (t.tax_amount or 0)
        for t in p.taxes))
    return money(lines + taxes - sum(a.allocated_amount for a in p.advances))


def history_for(db: Session, company_id: uuid.UUID, doctype: str | tuple[str, ...], document_id: uuid.UUID) -> list[dict]:
    """The audit trail of one request / claim, oldest first. [doctype] may
    name several audit doctypes (a claim filed from Payroll > Reimbursements
    is audited as 'reimbursement')."""
    doctypes = (doctype,) if isinstance(doctype, str) else tuple(doctype)
    rows = db.scalars(
        select(models.AuditLog)
        .where(models.AuditLog.company_id == company_id, models.AuditLog.doctype.in_(doctypes),
               models.AuditLog.document_id == document_id)
        .order_by(models.AuditLog.created_at, models.AuditLog.id)
    ).all()
    actor_ids = {r.user_id for r in rows if r.user_id}
    actors = {}
    if actor_ids:
        for u in db.scalars(select(models.User).where(models.User.id.in_(actor_ids))):
            actors[u.id] = u.full_name or u.email
    return [
        {
            "at": r.created_at.isoformat(), "actor": actors.get(r.user_id, "System") if r.user_id else "System",
            "action": r.action.replace("_", " ").title(),
            "notes": ((r.changes or {}).get("notes") if isinstance(r.changes, dict) else None),
        }
        for r in rows
    ]
