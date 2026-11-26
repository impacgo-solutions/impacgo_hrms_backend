"""One-shot fix: apply correct RBAC matrices to the 20 ERP roles
of Impacgo solutions Private Limited so the Flutter sidebar works.

Run:
    python -m app.fix_erp_role_matrices --tenant acme
"""

import argparse
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from . import crud, models
from .config import settings
from .database import set_tenant_context
from .rbac_columns import COLUMN_KEYS

COMPANY_ID = "caf06078-29da-4817-a26a-1f019fe083c7"

# Column order: own_profile, team_attendance, leave_approval, timesheet_approval,
#               payroll_view_own, payroll_process, benefits_admin, recruitment,
#               performance_reviews, org_structure_config, reports_analytics,
#               system_settings_rbac, audit_logs, travel_expense_approval,
#               projects_access, asset_management
ERP_ROLE_MATRICES: dict[str, str] = {
    # Full self-service for everyone + ESS
    "Employee (ESS)":       "s,s,n,s,s,n,n,n,s,n,n,n,n,n,s,s",

    # IT full admin
    "System Administrator": "s,n,n,n,n,n,n,n,n,n,v,a,a,n,n,a",

    # HR – manager level: can approve leave/attendance, process payroll, full recruitment
    "HR Manager":           "s,e,e,n,n,e,e,a,e,v,e,n,v,n,e,n",
    # HR executive: similar but no payroll process
    "HR Executive":         "s,v,v,n,n,e,e,a,e,n,e,n,n,n,e,n",

    # Finance – manager level: full payroll + view attendance
    "Finance Manager":      "s,v,v,v,s,a,e,n,e,v,e,n,v,a,n,n",
    # Accountant: process payroll, view timesheets
    "Accountant":           "s,n,n,v,n,a,e,n,n,n,e,n,n,a,n,n",
    # Payroll officer: process payroll only
    "Payroll Officer":      "s,n,n,n,n,a,e,n,n,n,v,n,n,n,n,n",

    # Auditor: view everything, process nothing
    "Auditor (Read Only)":  "s,v,v,v,v,v,v,v,v,v,v,n,v,v,v,v",

    # Sales – manager: team approvals, view recruitment, travel approval
    "Sales Manager":        "s,e,e,e,s,n,n,v,e,n,e,n,n,e,e,n",
    # Sales executive: self-service + travel
    "Sales Executive":      "s,n,n,n,s,n,n,v,s,n,n,n,n,e,n,n",

    # Procurement – manager: team approvals
    "Procurement Manager":  "s,e,e,e,s,n,n,v,e,n,e,n,n,e,e,n",
    # Buyer: self-service
    "Buyer":                "s,n,n,n,s,n,n,n,s,n,n,n,n,v,n,n",

    # Warehouse – manager: team approvals
    "Warehouse Manager":    "s,e,e,e,s,n,n,v,e,n,e,n,n,e,n,e",
    # Store keeper: self-service + asset view
    "Store Keeper":         "s,n,n,n,s,n,n,n,s,n,n,n,n,n,n,v",

    # Production – manager: team approvals
    "Production Manager":   "s,e,e,e,s,n,n,v,e,n,e,n,n,e,e,n",
    # Shop floor operator: self-service only
    "Shop Floor Operator":  "s,s,n,s,s,n,n,n,s,n,n,n,n,n,n,n",
    # Quality inspector: self-service
    "Quality Inspector":    "s,n,n,n,s,n,n,n,s,n,n,n,n,n,n,n",

    # CRM executive: self-service + travel
    "CRM Executive":        "s,n,n,n,s,n,n,n,s,n,n,n,n,e,n,n",
    # Support agent: self-service only
    "Support Agent":        "s,s,n,s,s,n,n,n,s,n,n,n,n,n,n,n",

    # Approver: general manager level
    "Approver":             "s,e,e,e,s,n,v,v,e,v,a,n,n,e,e,e",
}


def fix_matrices(db, company: models.Company) -> None:
    for role_name, row_csv in ERP_ROLE_MATRICES.items():
        role = db.scalar(
            select(models.Role).where(
                models.Role.company_id == company.id,
                models.Role.name == role_name,
            )
        )
        if role is None:
            print(f"  Role not found: {role_name!r} — skipping")
            continue

        # Clear existing role_permissions (backup ERP perms are irrelevant)
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", metavar="SLUG", default=None)
    args = parser.parse_args()
    run(tenant_slug=args.tenant)
