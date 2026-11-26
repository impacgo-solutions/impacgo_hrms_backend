from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user
from ..rbac_columns import LEVEL_LABELS, LEVELS, RBAC_COLUMNS

router = APIRouter(prefix="/api", tags=["permissions"])


@router.get("/permissions", response_model=list[schemas.PermissionOut])
def list_permissions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return db.scalars(select(models.Permission).order_by(models.Permission.resource, models.Permission.action)).all()


@router.get("/rbac-columns", response_model=schemas.RbacColumnsResponse)
def get_rbac_columns(current_user: models.User = Depends(get_current_user)):
    """The 14 Administration > Roles & Permissions matrix columns, in the
    exact order the frontend renders them, plus the 5 access levels."""
    return schemas.RbacColumnsResponse(
        columns=[
            schemas.RbacColumnOut(key=key, label=label, module=module)
            for key, label, module in RBAC_COLUMNS
        ],
        levels=["n", *LEVELS],
        level_labels=LEVEL_LABELS,
    )


@router.get("/permission-actions", response_model=schemas.GranularPermissionsResponse)
def get_permission_actions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The granular (resource, action) permission catalog -- additive to
    /rbac-columns' 16-column matrix, used by Administration > Add Custom
    Role's Module Access section. Sourced from the metadata-driven
    public.permission_actions catalog (crud.list_permission_actions), not
    a hardcoded list -- a module/action added there needs no code change
    to appear here. Filtered to the caller's own company's currently
    enabled modules (tenant-specific, same rule Administration > Modules
    already uses) -- a module disabled for this company isn't offered as
    grantable either."""
    enabled = crud.get_enabled_module_keys(db, current_user.company_id)
    return schemas.GranularPermissionsResponse(
        permissions=[
            schemas.GranularPermissionOut(
                resource=pa.resource, action=pa.action, label=pa.label,
                module=pa.module.key, module_name=pa.module.name,
            )
            for pa in crud.list_permission_actions(db)
            if pa.module.key in enabled
        ]
    )
