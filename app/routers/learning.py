import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import crud, form_config, models, schemas
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_action, require_module_enabled

router = APIRouter(
    prefix="/api", tags=["learning"],
    dependencies=[Depends(require_module_enabled("learning"))],
)


def _parse_duration_hours(duration: str) -> float | None:
    """Add Course's Duration field is free text (e.g. '16 hrs', '3 days') --
    best-effort parse to a hcm.training_courses.duration_hours numeric;
    duration_label always keeps the exact original string for display."""
    match = re.search(r"[\d.]+", duration)
    if not match:
        return None
    value = float(match.group(0))
    lowered = duration.lower()
    if "day" in lowered:
        return value * 24
    if "week" in lowered:
        return value * 24 * 7
    return value


@router.get("/courses", response_model=list[schemas.CourseOut])
def list_courses(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    courses = crud.list_training_courses(db, company.id)
    stats = crud.course_enrollment_stats(db, [c.id for c in courses])
    custom = form_config.load_custom_values(db, company.id, "course", [c.id for c in courses])
    return [
        schemas.CourseOut(
            id=c.id,
            custom_fields=custom.get(c.id, {}),
            name=c.title,
            # Configured label of the stored type (legacy lower-case values match case-insensitively).
            type=form_config.option_label(db, company.id, "course.type", c.course_type) or "",
            duration=c.duration_label or (f"{c.duration_hours:g} hrs" if c.duration_hours else "—"),
            category=c.category or "General",
            enrolled_count=stats.get(c.id, (0, 0))[0],
            avg_completion_pct=stats.get(c.id, (0, 0))[1],
            provider=c.provider,
        )
        for c in courses
    ]


def _certification_out(c: models.Certification) -> schemas.CertificationOut:
    return schemas.CertificationOut(
        id=c.id,
        employee_id=c.employee_id,
        name=c.name,
        issuer=c.issuer,
        issue_date=c.issue_date,
        expiry_date=c.expiry_date,
    )


@router.get("/certifications", response_model=list[schemas.CertificationOut])
def list_certifications(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [
        _certification_out(c)
        for c in crud.list_certifications(db, company.id, limit=limit, offset=offset)
    ]


@router.post(
    "/certifications",
    response_model=schemas.CertificationOut,
    status_code=201,
)
def create_certification(
    payload: schemas.CertificationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_action("learning", "create")),
):
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != company.id:
        # Cross-tenant UUID guessing guard, same 404-not-real convention as
        # routers/employees.py's _require_employee.
        raise HTTPException(status_code=404, detail="Employee not found")
    certification = crud.create_certification(
        db,
        payload.employee_id,
        payload.name,
        payload.issuer,
        payload.issue_date,
        payload.expiry_date,
    )
    crud.create_audit_log(
        db, company.id, current_user.id, "create", "certification", certification.id
    )
    db.commit()
    db.refresh(certification)
    return _certification_out(certification)


@router.post(
    "/courses",
    response_model=schemas.CourseOut,
    status_code=201,
)
def create_course(
    payload: schemas.CourseCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_action("learning", "create")),
):
    company = db.get(models.Company, current_user.company_id)
    course_type = form_config.require_option(db, company.id, "course.type", payload.type)
    cleaned = form_config.validate_custom_values(db, company.id, "course", payload.custom_fields)
    course = crud.create_training_course(
        db,
        company.id,
        payload.name,
        course_type,
        (payload.category or "").strip() or None,
        _parse_duration_hours(payload.duration) if payload.duration else None,
        payload.duration,
        provider=payload.provider,
    )
    form_config.save_custom_values(db, company.id, "course", course.id, cleaned)
    crud.create_audit_log(db, company.id, current_user.id, "create", "training_course", course.id)
    db.commit()
    db.refresh(course)
    return schemas.CourseOut(
        id=course.id,
        custom_fields=form_config.load_custom_values(db, company.id, "course", [course.id]).get(course.id, {}),
        name=course.title,
        type=form_config.option_label(db, company.id, "course.type", course_type) or course_type or "",
        duration=payload.duration or "—",
        category=(payload.category or "").strip() or "General",
        provider=course.provider,
    )


@router.get("/training-sessions", response_model=list[schemas.TrainingSessionOut])
def list_training_sessions(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return crud.list_training_sessions(db, company.id, limit=limit, offset=offset)


@router.post(
    "/training-sessions",
    response_model=schemas.TrainingSessionOut,
    status_code=201,
)
def create_training_session(
    payload: schemas.TrainingSessionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_action("learning", "create")),
):
    company = db.get(models.Company, current_user.company_id)
    session_row = crud.create_training_session(
        db,
        company.id,
        payload.title,
        payload.session_date,
        payload.trainer_name,
        payload.is_mandatory,
        payload.attendee_count,
    )
    crud.create_audit_log(
        db, company.id, current_user.id, "create", "training_session", session_row.id
    )
    db.commit()
    db.refresh(session_row)
    return session_row
