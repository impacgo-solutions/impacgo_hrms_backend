"""One-off script to bootstrap a working login for the existing, currently
EMPTY company row 'manisha' (id f64b49ad-125e-42bd-b268-8f8ae3bbcafa) so it
can be used for manual end-to-end testing.

Diagnosis this script fixes: that company has 0 users, 0 roles, 0
employees, 0 branches/departments -- login is impossible not because of a
code bug, but because there is nothing to log in as.

Uses ONLY existing FastAPI CRUD/service functions (crud.create_branch,
crud.create_department, crud.apply_matrix_update, crud.create_employee) --
the exact same functions the real /api/branches, /api/departments,
/api/roles/{id}/matrix and /api/employees endpoints call. No new
architecture, no bypass of the RBAC tables: the Owner role's access comes
entirely from real core.role_permissions rows, same as every other role.

This does NOT create a new company (unlike seed_new_company.py, which
always resolves/creates by name) -- it targets this one existing company_id
directly and is safe to re-run (every step checks-then-inserts).

Creates the MINIMUM required, all scoped to company_id = MANISHA_COMPANY_ID:
  - Branch "Head Office" (code HQ001)
  - Department "Administration" (code ADMIN01)
  - Role "Organization Owner / CEO" (BUILTIN_ROLES[0]) with its real
    DEFAULT_MATRIX from rbac_columns.py -- the same matrix every other
    company's Owner role gets, applied via crud.apply_matrix_update exactly
    like backend/app/seed.py does for every built-in role. Naming it
    exactly this lets it hit BOTH the backend's owner bypass
    (deps._OWNER_ROLE_NAME) and the frontend's (RbacEngine's kRoles[0]
    check) -- unrestricted access on both sides, all through real
    role_permissions rows, not a hardcoded shortcut.
  - One Employee + User account: admin@manisha.com / Test@1234 (CHANGE
    THIS PASSWORD after your first login -- it's a placeholder)

Usage (from backend/):
    python -m app.seed_manisha_admin
"""

import datetime
import uuid

from sqlalchemy import select

from . import crud, models, schemas
from .database import SessionLocal
from .rbac_columns import BUILTIN_ROLES, COLUMN_KEYS, DEFAULT_MATRIX

MANISHA_COMPANY_ID = uuid.UUID("f64b49ad-125e-42bd-b268-8f8ae3bbcafa")
OWNER_ROLE_NAME = BUILTIN_ROLES[0]  # "Organization Owner / CEO"
ADMIN_EMAIL = "admin@manisha.com"
ADMIN_PASSWORD = "Test@1234"


def ensure_branch(db, company_id: uuid.UUID) -> models.Branch:
    existing = db.scalar(
        select(models.Branch).where(
            models.Branch.company_id == company_id, models.Branch.code == "HQ001"
        )
    )
    if existing is not None:
        return existing
    payload = schemas.BranchCreate(code="HQ001", name="Head Office", country="India")
    branch = crud.create_branch(db, company_id, payload)
    print(f"Created branch '{branch.name}'")
    return branch


def ensure_department(db, company_id: uuid.UUID) -> models.Department:
    existing = db.scalar(
        select(models.Department).where(
            models.Department.company_id == company_id, models.Department.code == "ADMIN01"
        )
    )
    if existing is not None:
        return existing
    payload = schemas.DepartmentCreate(code="ADMIN01", name="Administration")
    department = crud.create_department(db, company_id, payload)
    print(f"Created department '{department.name}'")
    return department


def ensure_owner_role(db, company_id: uuid.UUID) -> models.Role:
    role = crud.get_role_by_name(db, company_id, OWNER_ROLE_NAME)
    if role is not None:
        return role
    role = models.Role(
        id=uuid.uuid4(), company_id=company_id, name=OWNER_ROLE_NAME,
        description="Full visibility and control across every module, branch and approval chain.",
        is_system=True,
    )
    db.add(role)
    db.flush()
    # Same pattern backend/app/seed.py uses for every built-in role: the
    # role's real DEFAULT_MATRIX, applied via the existing
    # crud.apply_matrix_update -- genuine core.role_permissions rows, not a
    # hardcoded bypass.
    levels = DEFAULT_MATRIX[OWNER_ROLE_NAME].split(",")
    updates = dict(zip(COLUMN_KEYS, levels))
    crud.apply_matrix_update(db, role, updates)
    print(f"Created role '{role.name}' with its standard Owner permission matrix")
    return role


def ensure_admin_employee_and_user(
    db, company_id: uuid.UUID, branch: models.Branch, department: models.Department, role: models.Role
) -> None:
    existing_user = crud.find_user_by_email(db, ADMIN_EMAIL)
    if existing_user is not None:
        print(f"User '{ADMIN_EMAIL}' already exists -- nothing to do")
        return

    payload = schemas.EmployeeCreate(
        create_designation=True,
        employee_code="ADMIN001",
        first_name="Test",
        last_name="Owner",
        work_email=ADMIN_EMAIL,
        role_name=role.name,
        password=ADMIN_PASSWORD,
        date_of_joining=datetime.date.today(),
        employment_type="full_time",
        status="active",
        branch_name=branch.name,
        department_name=department.name,
        designation_name="Chief Executive Officer",
    )
    employee = crud.create_employee(db, company_id, payload)
    print(f"Created employee '{employee.first_name} {employee.last_name}' ({ADMIN_EMAIL})")


def main() -> None:
    db = SessionLocal()
    try:
        company = db.get(models.Company, MANISHA_COMPANY_ID)
        if company is None:
            raise SystemExit(f"Company {MANISHA_COMPANY_ID} not found -- aborting")

        crud.upsert_company_settings(
            db, company.id,
            {"default_currency": "INR", "default_timezone": "IST (UTC+5:30)", "working_days_per_week": 5},
        )
        branch = ensure_branch(db, company.id)
        department = ensure_department(db, company.id)
        role = ensure_owner_role(db, company.id)
        ensure_admin_employee_and_user(db, company.id, branch, department, role)

        db.commit()
        print()
        print("Done. Log in with:")
        print(f"  email:    {ADMIN_EMAIL}")
        print(f"  password: {ADMIN_PASSWORD}")
        print("Change this password after your first login.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
