from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import crud, form_config, models, schemas
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled, require_permission

router = APIRouter(
    prefix="/api", tags=["performance"],
    dependencies=[Depends(require_module_enabled("performance"))],
)

def _require_employee_in_company(db: Session, employee_id, company_id) -> None:
    """Fetches employee_id and verifies it belongs to company_id -- without
    this, any caller holding performance_reviews Edit/Admin could recognize
    or skill-rate an employee_id belonging to a completely different
    company (cross-tenant UUID guessing). Same 404-not-403 convention as
    routers/employees.py's _require_employee."""
    employee = db.get(models.Employee, employee_id)
    if employee is None or employee.company_id != company_id:
        raise HTTPException(status_code=404, detail="Employee not found")


_REVIEW_CYCLE_STATUS_DISPLAY = {
    "completed": "Completed",
    "in_progress": "In Progress",
    "scheduled": "Scheduled",
}


def _review_cycle_out(
    c: models.AppraisalCycle, db: Session | None = None, custom: dict | None = None
) -> schemas.ReviewCycleOut:
    # Display label of the status = the company's configured label for the
    # stored key (Form Configuration > Review Cycle > Status).
    label = (form_config.option_label(db, c.company_id, "review_cycle.status", c.status)
             if db is not None else None)
    return schemas.ReviewCycleOut(
        id=c.id,
        name=c.name,
        # No "type" column exists -- a review cycle spanning more than
        # ~200 days is displayed as Annual, otherwise Quarterly, mirroring
        # how the frontend's own 3 seed cycles are labeled.
        type="Annual" if (c.to_date - c.from_date).days > 200 else "Quarterly",
        status=label if label and label != c.status else _REVIEW_CYCLE_STATUS_DISPLAY.get(c.status, c.status.title()),
        status_key=c.status,
        due=c.to_date.isoformat(),
        participants=c.participant_count,
        custom_fields=(custom or {}).get(c.id, {}),
    )


def _okr_out(o: models.CompanyOkr, db: Session, custom: dict) -> schemas.OkrOut:
    return schemas.OkrOut(
        id=o.id, level=o.level,
        level_label=form_config.option_label(db, o.company_id, "goal.level", o.level),
        title=o.title, owner=o.owner_name, progress=o.progress_pct,
        owner_employee_id=o.owner_employee_id,
        custom_fields=custom.get(o.id, {}),
    )


@router.get("/okrs", response_model=list[schemas.OkrOut])
def list_okrs(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    okrs = crud.list_okrs(db, company.id, limit=limit, offset=offset)
    custom = form_config.load_custom_values(db, company.id, "goal", [o.id for o in okrs])
    return [_okr_out(o, db, custom) for o in okrs]


@router.post(
    "/okrs",
    response_model=schemas.OkrOut,
    status_code=201,
)
def create_okr(
    payload: schemas.OkrCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("performance_reviews")),
):
    company = db.get(models.Company, current_user.company_id)
    # Level must be one of the company's configured Goal Levels.
    level = form_config.require_option(db, company.id, "goal.level", payload.level, required=True)
    cleaned = form_config.validate_custom_values(db, company.id, "goal", payload.custom_fields)
    owner_name = (payload.owner or "").strip()
    if payload.owner_employee_id is not None:
        owner = db.get(models.Employee, payload.owner_employee_id)
        if owner is None or owner.company_id != company.id:
            raise HTTPException(status_code=400, detail="The goal owner must be an employee of your company.")
        owner_name = f"{owner.first_name} {owner.last_name or ''}".strip()
    if not owner_name:
        # Owner left blank: the creator (owner_name is NOT NULL).
        owner_name = (crud.employee_display_name(db, current_user.employee_id)
                      if current_user.employee_id else "") or "—"
    okr = crud.create_okr(
        db, company.id, level, payload.title, owner_name,
        owner_employee_id=payload.owner_employee_id,
    )
    form_config.save_custom_values(db, company.id, "goal", okr.id, cleaned)
    crud.create_audit_log(db, company.id, current_user.id, "create", "okr", okr.id)
    db.commit()
    db.refresh(okr)
    return _okr_out(okr, db, form_config.load_custom_values(db, company.id, "goal", [okr.id]))


@router.get("/review-cycles", response_model=list[schemas.ReviewCycleOut])
def list_review_cycles(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    cycles = crud.list_review_cycles(db, company.id)
    custom = form_config.load_custom_values(db, company.id, "review_cycle", [c.id for c in cycles])
    return [_review_cycle_out(c, db, custom) for c in cycles]


@router.post(
    "/review-cycles",
    response_model=schemas.ReviewCycleOut,
    status_code=201,
)
def create_review_cycle(
    payload: schemas.ReviewCycleCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("performance_reviews")),
):
    company = db.get(models.Company, current_user.company_id)
    # Status keys drive review-cycle behaviour, so only the configured
    # (renamable / hideable, not extensible) statuses are accepted.
    status = form_config.require_option(db, company.id, "review_cycle.status", payload.status, required=True)
    cleaned = form_config.validate_custom_values(db, company.id, "review_cycle", payload.custom_fields)
    try:
        cycle = crud.create_review_cycle(
            db,
            company.id,
            payload.name,
            payload.from_date,
            payload.to_date,
            status,
            payload.participant_count,
        )
        form_config.save_custom_values(db, company.id, "review_cycle", cycle.id, cleaned)
        crud.create_audit_log(db, company.id, current_user.id, "create", "review_cycle", cycle.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(cycle)
    return _review_cycle_out(cycle, db, form_config.load_custom_values(db, company.id, "review_cycle", [cycle.id]))


@router.get("/recognitions", response_model=list[schemas.RecognitionOut])
def list_recognitions(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_recognitions(db, company.id, limit=limit, offset=offset)


@router.post(
    "/recognitions",
    response_model=schemas.RecognitionOut,
    status_code=201,
)
def create_recognition(
    payload: schemas.RecognitionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("performance_reviews")),
):
    company = db.get(models.Company, current_user.company_id)
    _require_employee_in_company(db, payload.employee_id, company.id)
    recognition = crud.create_recognition(db, payload.employee_id, payload.badge, payload.reason, payload.given_on)
    crud.create_audit_log(db, company.id, current_user.id, "create", "recognition", recognition.id)
    db.commit()
    db.refresh(recognition)
    return recognition


@router.get("/skill-ratings", response_model=list[schemas.SkillRatingOut])
def list_skill_ratings(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_skill_ratings(
        db, company.id, limit=limit, offset=offset, visible_ids=_skill_visible_ids(db, current_user)
    )


def _skill_visible_ids(db: Session, user: models.User) -> set | None:
    """L-30: skill ratings are visible to the employee, their management
    chain (anyone with them in their reporting subtree) and HR / Owner --
    the caller's People visibility (None = whole company) plus self."""
    visible = crud.get_people_directory_visible_ids(db, user)
    if visible is None:
        return None
    return set(visible) | ({user.employee_id} if user.employee_id else set())


@router.put("/skill-ratings", response_model=schemas.SkillRatingOut)
def upsert_skill_rating(
    payload: schemas.SkillRatingUpsert,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("performance_reviews")),
):
    company = db.get(models.Company, current_user.company_id)
    _require_employee_in_company(db, payload.employee_id, company.id)
    visible = _skill_visible_ids(db, current_user)
    if visible is not None and payload.employee_id not in visible:
        raise HTTPException(status_code=403, detail="You can only rate skills of employees in your reporting team.")
    rating = crud.upsert_skill_rating(db, payload.employee_id, payload.skill, payload.level)
    crud.create_audit_log(db, company.id, current_user.id, "update", "employee_skill_rating", rating.id)
    db.commit()
    return rating
