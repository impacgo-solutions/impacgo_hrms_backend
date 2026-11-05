from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import get_current_user, require_permission_or_action

router = APIRouter(prefix="/api/config", tags=["config"])

# public.modules is a global catalog shared with other, unrelated products on
# this database (e.g. 'retail') -- this app only ever shows/toggles its own
# 18 HRMS modules, confirmed against the live app's kNav
# (lib/data/seed/nav_seed.dart).
_HRMS_MODULE_KEYS = {
    "dashboard", "people", "organization", "attendance", "leave", "payroll",
    "performance", "recruitment", "benefits", "learning", "projects", "work",
    "travel", "assets", "documents", "reports", "approvals", "admin",
}


@router.get("/modules", response_model=schemas.ModulesConfigResponse)
def get_modules(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Every module in the global catalog plus whether the caller's company
    has it enabled -- a missing core.company_modules row means enabled, so
    an unconfigured company sees `is_enabled: true` for everything."""
    states = crud.get_company_module_states(db, current_user.company_id)
    return schemas.ModulesConfigResponse(
        modules=[
            schemas.ModuleOut(
                key=m.key, name=m.name, icon=m.icon, is_core=m.is_core,
                is_enabled=states.get(m.key, True),
                nav_group=m.nav_group, permission_columns=m.permission_columns,
            )
            for m in crud.list_modules(db)
            if m.key in _HRMS_MODULE_KEYS
        ]
    )


@router.put("/modules", response_model=schemas.ModulesConfigResponse)
def update_modules(
    payload: schemas.ModulesUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(
        require_permission_or_action("system_settings_rbac", "config", "edit")
    ),
):
    company_id = current_user.company_id
    before = crud.get_company_module_states(db, company_id)
    try:
        for toggle in payload.modules:
            crud.set_company_module_enabled(
                db, company_id, toggle.key, toggle.is_enabled, current_user.id
            )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    after = crud.get_company_module_states(db, company_id)
    crud.create_audit_log(
        db, company_id, current_user.id, "update", "company_modules",
        changes={"before": before, "after": after},
    )
    db.commit()
    states = crud.get_company_module_states(db, company_id)
    return schemas.ModulesConfigResponse(
        modules=[
            schemas.ModuleOut(
                key=m.key, name=m.name, icon=m.icon, is_core=m.is_core,
                is_enabled=states.get(m.key, True),
                nav_group=m.nav_group, permission_columns=m.permission_columns,
            )
            for m in crud.list_modules(db)
            if m.key in _HRMS_MODULE_KEYS
        ]
    )
