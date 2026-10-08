from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/api/me", tags=["me"])


@router.get("/people-access", response_model=schemas.PeopleAccessOut)
def get_people_access(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The calling user's own effective People-module access -- backs the
    frontend's people_access_provider (no WorkforceStore involved). See
    crud.effective_people_access / crud.can_access_people_module."""
    configured, actions = crud.effective_people_access(db, current_user)
    return schemas.PeopleAccessOut(configured=configured, actions=actions)


@router.get("/actions/{resource}", response_model=schemas.MyActionsOut)
def get_my_actions(
    resource: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The calling user's own granted granular actions for `resource` --
    backs frontend action-gating for modules with no rbac_columns entry
    (e.g. Learning), mirroring what require_action(resource, ...) enforces
    server-side. See crud.get_user_granted_actions."""
    return schemas.MyActionsOut(
        resource=resource,
        actions=crud.get_user_granted_actions(db, current_user.id, resource),
    )
