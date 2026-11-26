"""Apply correct RBAC matrices to the 11 HCM built-in roles for
the 'impacgo people' company (id 5b2e9307-9764-441b-a70d-29b7769d75e8).

Run once:
    python -m app.fix_hcm_role_matrices --tenant acme
"""

import argparse
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from . import crud, models
from .config import settings
from .database import set_tenant_context
from .rbac_columns import COLUMN_KEYS, DEFAULT_MATRIX

COMPANY_ID = "5b2e9307-9764-441b-a70d-29b7769d75e8"

HCM_ROLE_MATRICES: dict[str, str] = {
    role: DEFAULT_MATRIX[role]
    for role in DEFAULT_MATRIX
    if role not in ("Branch Manager", "Branch Head", "Project Manager")
}


def fix_matrices(db, company: models.Company) -> None:
    for role_name, row_csv in HCM_ROLE_MATRICES.items():
        role = db.scalar(
            select(models.Role).where(
                models.Role.company_id == company.id,
                models.Role.name == role_name,
            )
        )
        if role is None:
            print(f"  Role not found: {role_name!r} — skipping")
            continue

        role.role_permissions.clear()
        db.flush()

        levels = row_csv.split(",")
        if len(levels) != len(COLUMN_KEYS):
            print(f"  Matrix length mismatch for {role_name!r}: {len(levels)} vs {len(COLUMN_KEYS)}")
            continue

        updates = dict(zip(COLUMN_KEYS, levels))
        crud.apply_matrix_update(db, role, updates)
        db.commit()
        print(f"  Applied matrix to role '{role_name}'")


def run(tenant_slug: str | None = None) -> None:
    if tenant_slug:
        set_tenant_context(tenant_slug)
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"options": f"-c search_path={tenant_slug},public"},
        )
        _Session = sessionmaker(bind=_engine, autocommit=False, autoflush=True)
        db = _Session()
    else:
        from .database import SessionLocal
        db = SessionLocal()

    try:
        company = db.get(models.Company, uuid.UUID(COMPANY_ID))
        if company is None:
            print(f"Company {COMPANY_ID} not found")
            return
        print(f"Fixing RBAC matrices for '{company.name}'...")
        fix_matrices(db, company)
        print("Done.")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix RBAC matrices for impacgo people HCM roles."
    )
    parser.add_argument("--tenant", metavar="SLUG", default=None)
    args = parser.parse_args()
    run(tenant_slug=args.tenant)
