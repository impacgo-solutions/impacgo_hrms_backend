import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user, require_permission

router = APIRouter(prefix="/api/bands", tags=["bands"])


@router.get("", response_model=list[schemas.BandOut])
def list_bands(
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Backs both the Organization > Bands tab (active_only=false, shows
    everything so admins can reactivate a deactivated band) and the Add
    Employee "Grade / Band" dropdown (active_only=true)."""
    company = db.get(models.Company, current_user.company_id)
    return crud.list_bands(db, company.id, active_only=active_only)


@router.post(
    "",
    response_model=schemas.BandOut,
    status_code=201,
)
def create_band(
    payload: schemas.BandCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    try:
        band = crud.create_band(db, company.id, payload, user_id=current_user.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crud.create_audit_log(db, company.id, current_user.id, "create", "band", band.id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Band already exists") from exc
    db.refresh(band)
    return band


@router.patch(
    "/{band_id}",
    response_model=schemas.BandOut,
)
def update_band(
    band_id: uuid.UUID,
    payload: schemas.BandUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    """Rename/re-code a band, edit its description, and/or toggle it
    active/inactive (Activate / Deactivate) -- all through this one PATCH,
    matching PATCH /api/designations/{id}'s shape."""
    company = db.get(models.Company, current_user.company_id)
    band = crud.get_band_for_update(db, band_id, company.id)
    if band is None:
        raise HTTPException(status_code=404, detail="Band not found")

    updates = payload.model_dump(exclude_unset=True)
    try:
        crud.update_band(db, band, updates, user_id=current_user.id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    crud.create_audit_log(db, company.id, current_user.id, "update", "band", band.id)
    db.commit()
    db.refresh(band)
    return band


@router.delete("/{band_id}", status_code=204)
def delete_band(
    band_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("org_structure_config")),
):
    company = db.get(models.Company, current_user.company_id)
    band = crud.get_band_for_update(db, band_id, company.id)
    if band is None:
        raise HTTPException(status_code=404, detail="Band not found")

    # M-05: never leave designations / roles pointing at a deleted band --
    # block the delete while anything is mapped to it.
    from sqlalchemy import func, select

    designations = db.scalar(select(func.count(models.Designation.id)).where(
        models.Designation.company_id == company.id, models.Designation.band_id == band.id))
    roles = db.scalar(select(func.count(models.Role.id)).where(models.Role.band_id == band.id))
    if designations or roles:
        parts = [f"{n} {label}" for n, label in ((designations, "designation(s)"), (roles, "role(s)")) if n]
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete band '{band.name}' -- {' and '.join(parts)} are mapped to it. "
                   "Re-map them or deactivate the band instead.",
        )
    try:
        crud.delete_band(db, band)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    crud.create_audit_log(db, company.id, current_user.id, "delete", "band", band_id)
    db.commit()
