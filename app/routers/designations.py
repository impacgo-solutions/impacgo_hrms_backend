import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user, require_permission

router = APIRouter(prefix="/api/designations", tags=["designations"])


@router.get("/by-role", response_model=list[schemas.DesignationOut])
def list_designations_by_role(
    role_id: uuid.UUID | None = Query(
        default=None,
        description="Filter to designations mapped to this Role -- backs "
        "the Add Employee Role -> Designation cascade.",
    ),
    band_id: uuid.UUID | None = Query(
        default=None,
        description="Filter to designations mapped straight to this Band "
        "(no Role in between) -- combined with role_id above via OR, so a "
        "designation mapped either way is included.",
    ),
    legacy_band_name: str | None = Query(
        default=None,
        description="Fallback filter (the free-text Designation.band label "
        "of the currently selected Band) used only until this company has "
        "mapped at least one designation to a role/band -- preserves the "
        "exact pre-hierarchy Add Employee behavior for unconfigured "
        "companies.",
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Separate from GET /api/designations (which always returns the
    legacy band-string-grouped shape the Designations tab renders) -- this
    is a flat list for the Employee Creation cascade, keyed on the real
    Role/Band -> Designation mapping instead."""
    company = db.get(models.Company, current_user.company_id)
    return crud.list_designations_for_role(
        db, company.id, role_id=role_id, band_id=band_id, legacy_band_name=legacy_band_name
    )


@router.get("", response_model=list[schemas.DesignationBandOut])
def list_designations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs the Organization > Designations tab. Groups every active
    designation by its band and returns bands in the same order the
    frontend's kDesignationBands renders them in."""
    company = db.get(models.Company, current_user.company_id)
    designations = crud.list_designation_bands(db, company.id)

    grouped: dict[str, list] = {}
    for d in designations:
        grouped.setdefault(d.band or "Unassigned", []).append(d)

    bands = [
        schemas.DesignationBandOut(
            band=crud.band_sort_key(band_name)[0],
            name=band_name,
            designations=sorted(
                members, key=lambda d: crud.designation_sort_key(band_name, d.name)
            ),
        )
        for band_name, members in grouped.items()
    ]
    bands.sort(key=lambda b: b.band)
    return bands


@router.post(
    "",
    response_model=schemas.DesignationOut,
    status_code=201,
)
def create_designation(
    payload: schemas.DesignationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    if payload.band is None and payload.band_id is None:
        raise HTTPException(status_code=400, detail="Either band or band_id is required")
    if payload.role_id is not None:
        role = db.get(models.Role, payload.role_id)
        if role is None or role.company_id != company.id:
            raise HTTPException(status_code=400, detail="Invalid role_id")
    if payload.band_id is not None:
        band = db.get(models.Band, payload.band_id)
        if band is None or band.company_id != company.id:
            raise HTTPException(status_code=400, detail="Invalid band_id")
    try:
        designation = crud.create_designation(
            db,
            company.id,
            payload.name,
            payload.band,
            role_id=payload.role_id,
            band_id=payload.band_id,
            user_id=current_user.id,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "create", "designation", designation.id)
    try:
        db.commit()
    except IntegrityError as exc:
        # Belt-and-braces: if a unique constraint is ever added to the table
        # later, a race that slips past the advisory lock still fails clean
        # instead of raising a raw 500.
        db.rollback()
        raise HTTPException(status_code=409, detail="Designation already exists") from exc
    db.refresh(designation)
    return designation


@router.patch(
    "/{designation_id}",
    response_model=schemas.DesignationOut,
)
def update_designation(
    designation_id: uuid.UUID,
    payload: schemas.DesignationUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    """Rename a designation and/or toggle it active/inactive (soft delete —
    core.employees.designation_id references this table, so rows are never
    hard-deleted here)."""
    company = db.get(models.Company, current_user.company_id)
    designation = crud.get_designation_for_update(db, designation_id, company.id)
    if designation is None:
        raise HTTPException(status_code=404, detail="Designation not found")

    if payload.name is not None:
        designation.name = payload.name
    if payload.is_active is not None:
        designation.is_active = payload.is_active
    if payload.role_id is not None:
        role = db.get(models.Role, payload.role_id)
        if role is None or role.company_id != company.id:
            raise HTTPException(status_code=400, detail="Invalid role_id")
        designation.role_id = payload.role_id
    if payload.band_id is not None:
        band = db.get(models.Band, payload.band_id)
        if band is None or band.company_id != company.id:
            raise HTTPException(status_code=400, detail="Invalid band_id")
        designation.band_id = payload.band_id
        # Keep the legacy free-text label in sync so it never contradicts
        # the real Band this designation is now mapped to.
        designation.band = band.name

    designation.updated_by = current_user.id
    crud.create_audit_log(db, company.id, current_user.id, "update", "designation", designation.id)
    db.commit()
    db.refresh(designation)
    return designation


def _get_designation_or_404(db: Session, designation_id: uuid.UUID, company_id: uuid.UUID) -> models.Designation:
    designation = db.scalar(
        select(models.Designation).where(
            models.Designation.id == designation_id, models.Designation.company_id == company_id
        )
    )
    if designation is None:
        raise HTTPException(status_code=404, detail="Designation not found")
    return designation


@router.get("/{designation_id}/actions", response_model=schemas.DesignationDeniedActionsOut)
def get_designation_actions(
    designation_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The (resource, action) pairs EXPLICITLY DENIED for this Designation --
    see models.DesignationPermission. Absence of a resource here means "not
    restricted by Designation", not "no access" -- effective access is
    still governed by whatever the employee's Role grants."""
    designation = _get_designation_or_404(db, designation_id, current_user.company_id)
    return schemas.DesignationDeniedActionsOut(
        designation=designation, denied=crud.build_designation_denials(db, designation.id)
    )


@router.put("/{designation_id}/actions", response_model=schemas.DesignationDeniedActionsOut)
def update_designation_actions(
    designation_id: uuid.UUID,
    payload: schemas.DesignationDeniedActionsUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("system_settings_rbac")),
):
    designation = _get_designation_or_404(db, designation_id, current_user.company_id)
    try:
        crud.apply_designation_denials_update(db, designation, payload.denied, user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "update", "designation_actions", designation.id,
        changes={"denied": payload.denied},
    )
    db.commit()
    return schemas.DesignationDeniedActionsOut(
        designation=designation, denied=crud.build_designation_denials(db, designation.id)
    )
