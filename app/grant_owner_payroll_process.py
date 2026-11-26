"""One-off script: grant the "Organization Owner / CEO" role in the
impacgo-solutions tenant Edit-level access to the "Payroll (Process)" RBAC
column, so canActProvider('Payroll (Process)') on the frontend starts
returning true for that role (today it's only 'v' -- View -- which isn't
enough to see/use Run Payroll; see rbac_decision_providers.dart's
canActProvider, which requires Edit or Admin).

Scoped to exactly one tenant's one role's one column via the same real
apply_matrix_update() the Administration > Roles & Permissions screen
itself calls -- no other role, column, or tenant is touched, and the
DEFAULT_MATRIX template used to seed *new* companies is left alone (this
only fixes the already-provisioned impacgo-solutions tenant).

Usage (from backend/):
    python -m app.grant_owner_payroll_process
"""

from sqlalchemy import select, text

from . import crud, models
from .database import SessionLocal

TENANT_SLUG = "impacgo-solutions"
ROLE_NAME = "Organization Owner / CEO"
COLUMN_KEY = "payroll_process"
NEW_LEVEL = "e"  # Edit -- sufficient for canActProvider's e/a check.


def main() -> None:
    db = SessionLocal()
    try:
        db.execute(text(f'SET search_path TO "{TENANT_SLUG}", public'))

        role = db.scalar(
            select(models.Role).where(models.Role.name == ROLE_NAME)
        )
        if role is None:
            raise SystemExit(f"Role '{ROLE_NAME}' not found in tenant '{TENANT_SLUG}'")

        before = next(
            (
                rp.permission.action
                for rp in role.role_permissions
                if rp.permission.resource == COLUMN_KEY and len(rp.permission.action) == 1
            ),
            "n",
        )

        crud.apply_matrix_update(db, role, {COLUMN_KEY: NEW_LEVEL})
        db.commit()

        print(
            f"OK: '{ROLE_NAME}' in '{TENANT_SLUG}' -- {COLUMN_KEY}: "
            f"'{before}' -> '{NEW_LEVEL}'"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
