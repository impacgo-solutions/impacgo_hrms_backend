import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user, require_module_enabled, require_permission

router = APIRouter(
    prefix="/api", tags=["benefits"],
    dependencies=[Depends(require_module_enabled("benefits"))],
)


def _category_out(category: models.BenefitCategory) -> schemas.BenefitCategoryOut:
    return schemas.BenefitCategoryOut(
        id=category.id,
        name=category.name,
        is_active=category.is_active,
        items=[
            schemas.BenefitCategoryItemOut(id=i.id, item=i.item) for i in category.items
        ],
        employee_ids=[a.employee_id for a in category.assignments],
    )


def _is_benefits_admin(db: Session, current_user: models.User) -> bool:
    """Same rule require_permission("benefits_admin") enforces on the write
    endpoints below, but as a plain boolean for branching a GET's data scope
    instead of rejecting the request -- an Owner or a role with Edit/Admin
    on Benefits Admin sees every plan in the company (the management view);
    everyone else sees only plans they're personally assigned to."""
    role = crud.get_user_primary_role(db, current_user.id)
    if role is None:
        return False
    if role.name == crud.BUILTIN_ROLES[0]:
        return True
    matrix = crud.build_role_matrix(role)
    return matrix.get("benefits_admin") in ("e", "a")


def _item_out(item: models.BenefitCategoryItem) -> schemas.BenefitCategoryItemOut:
    return schemas.BenefitCategoryItemOut(id=item.id, item=item.item)


def _get_owned_category(
    db: Session, category_id: uuid.UUID, company_id: uuid.UUID
) -> models.BenefitCategory:
    category = crud.get_benefit_category(db, category_id)
    if category is None or category.company_id != company_id:
        raise HTTPException(status_code=404, detail="Benefit category not found")
    return category


@router.get("/benefit-categories", response_model=list[schemas.BenefitCategoryOut])
def list_benefit_categories(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    # Only employees explicitly assigned to a plan (see
    # BenefitCategoryAssignment) may see it in their own Benefits & Perks
    # view -- Benefits Admins/Owner keep seeing every plan, since they need
    # full visibility to manage assignments in the first place.
    is_admin = _is_benefits_admin(db, current_user)
    if not is_admin and current_user.employee_id is None:
        # No linked employee record and not an admin -- nothing could ever
        # have been assigned to them, so don't fall through to "no scoping"
        # (which would show every plan instead of none).
        return []
    scoping_employee_id = None if is_admin else current_user.employee_id
    return [
        _category_out(c)
        for c in crud.list_benefit_categories(
            db, company.id, include_inactive=include_inactive, employee_id=scoping_employee_id
        )
    ]


@router.get(
    "/benefit-categories/{category_id}/items",
    response_model=list[schemas.BenefitCategoryItemOut],
)
def list_benefit_category_items(
    category_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs "select a category, dynamically load its items" -- a plain
    authenticated read, same as the categories list itself."""
    category = _get_owned_category(db, category_id, current_user.company_id)
    return [_item_out(i) for i in crud.list_benefit_category_items(db, category.id)]


@router.post(
    "/benefit-categories",
    response_model=schemas.BenefitCategoryOut,
    status_code=201,
)
def create_benefit_category(
    payload: schemas.BenefitCategoryCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    company = db.get(models.Company, current_user.company_id)
    if payload.employee_ids and crud.employee_ids_outside_company(
        db, payload.employee_ids, company.id
    ):
        raise HTTPException(
            status_code=400,
            detail="One or more selected employees do not belong to your company",
        )
    try:
        category = crud.create_benefit_category(
            db, company.id, payload.name, payload.items, employee_ids=payload.employee_ids
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "benefit_category", category.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(category)
    return _category_out(category)


@router.patch(
    "/benefit-categories/{category_id}",
    response_model=schemas.BenefitCategoryOut,
)
def update_benefit_category(
    category_id: uuid.UUID,
    payload: schemas.BenefitCategoryUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    _get_owned_category(db, category_id, current_user.company_id)
    category = crud.get_benefit_category_for_update(db, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Benefit category not found")
    updates = payload.model_dump(exclude_unset=True)
    # Assignment list is a separate table, not a plain column -- handled via
    # set_benefit_category_assignments below, not the generic setattr loop
    # in crud.update_benefit_category.
    new_employee_ids = updates.pop("employee_ids", None)
    if new_employee_ids and crud.employee_ids_outside_company(
        db, new_employee_ids, current_user.company_id
    ):
        raise HTTPException(
            status_code=400,
            detail="One or more selected employees do not belong to your company",
        )
    try:
        if updates:
            crud.update_benefit_category(db, category, updates)
        if new_employee_ids is not None:
            crud.set_benefit_category_assignments(db, category, new_employee_ids)
        crud.create_audit_log(
            db, current_user.company_id, current_user.id, "update", "benefit_category", category.id
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(category)
    return _category_out(category)


@router.delete("/benefit-categories/{category_id}", status_code=204)
def delete_benefit_category(
    category_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    category = _get_owned_category(db, category_id, current_user.company_id)
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "delete", "benefit_category", category.id
    )
    crud.delete_benefit_category(db, category)
    db.commit()


@router.post(
    "/benefit-categories/{category_id}/items",
    response_model=schemas.BenefitCategoryItemOut,
    status_code=201,
)
def create_benefit_category_item(
    category_id: uuid.UUID,
    payload: schemas.BenefitCategoryItemCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    category = _get_owned_category(db, category_id, current_user.company_id)
    item = crud.create_benefit_category_item(db, category.id, payload.item)
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "create", "benefit_category_item", item.id
    )
    db.commit()
    db.refresh(item)
    return _item_out(item)


def _get_owned_item(
    db: Session, item_id: uuid.UUID, company_id: uuid.UUID
) -> models.BenefitCategoryItem:
    item = crud.get_benefit_category_item_for_update(db, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Benefit item not found")
    _get_owned_category(db, item.category_id, company_id)
    return item


@router.patch(
    "/benefit-category-items/{item_id}",
    response_model=schemas.BenefitCategoryItemOut,
)
def update_benefit_category_item(
    item_id: uuid.UUID,
    payload: schemas.BenefitCategoryItemUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    item = _get_owned_item(db, item_id, current_user.company_id)
    crud.update_benefit_category_item(db, item, payload.item)
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "benefit_category_item", item.id
    )
    db.commit()
    db.refresh(item)
    return _item_out(item)


@router.delete("/benefit-category-items/{item_id}", status_code=204)
def delete_benefit_category_item(
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    item = _get_owned_item(db, item_id, current_user.company_id)
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "delete", "benefit_category_item", item.id
    )
    crud.delete_benefit_category_item(db, item)
    db.commit()


@router.put("/employee-benefits", response_model=schemas.BenefitsEnrollmentOut)
def upsert_employee_benefits(
    payload: schemas.BenefitsEnrollmentUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    company = db.get(models.Company, current_user.company_id)
    benefits = crud.upsert_employee_benefits(
        db,
        payload.employee_id,
        insurance_plan=payload.insurance_plan,
        esop_units=payload.esop_units,
        dependents_covered=payload.dependents_covered,
        learning_budget_total=payload.learning_budget_total,
        learning_budget_used=payload.learning_budget_used,
        cab_facility=payload.cab_facility,
        meal_card=payload.meal_card,
        internet_reimbursement=payload.internet_reimbursement,
        wellness_program=payload.wellness_program,
    )
    crud.create_audit_log(db, company.id, current_user.id, "update", "employee_benefits", benefits.id)
    db.commit()
    db.refresh(benefits)
    return benefits
