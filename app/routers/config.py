import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user, require_permission_or_action

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/field-rules", response_model=schemas.FieldRulesResponse)
def get_field_rules(
    entity: str = Query(..., alias="entity"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Effective mandatory/optional/hidden/readonly state for every field of
    `entity`, merged for the caller's company + role. Every add/edit form
    fetches this once and toggles its own existing fields accordingly --
    Flutter never hardcodes which fields are required."""
    role = crud.get_user_primary_role(db, current_user.id)
    rules = crud.get_field_rules(
        db, current_user.company_id, entity, role_id=role.id if role else None
    )
    return schemas.FieldRulesResponse(entity_key=entity, rules=rules)


@router.put("/field-rules", response_model=schemas.FieldRulesResponse)
def update_field_rules(
    payload: schemas.FieldRulesUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("org_structure_config", "config", "edit")
    ),
):
    company_id = current_user.company_id
    crud.upsert_field_rules(
        db,
        company_id,
        payload.entity_key,
        [rule.model_dump() for rule in payload.rules],
        current_user.id,
    )
    crud.create_audit_log(
        db, company_id, current_user.id, "update", "field_rules",
        changes={"entity_key": payload.entity_key, "rules": [r.model_dump(mode="json") for r in payload.rules]},
    )
    db.commit()
    effective = crud.get_field_rules(db, company_id, payload.entity_key)
    return schemas.FieldRulesResponse(entity_key=payload.entity_key, rules=effective)
