"""Seed script for 'impacgo people' (HCM product company).

The company and its 11 HCM roles were already inserted from backup.
This script seeds branches, departments, employees, and all HR modules.

Usage:
    python -m app.seed_impacgo_people --tenant acme
"""

import argparse
import datetime
import uuid

from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from types import SimpleNamespace

from . import crud, models, schemas
from .attendance_leave_seed_data import HOLIDAYS, LEAVE_TYPES, SHIFTS
from .config import settings
from .database import set_tenant_context
from .designation_seed_data import DESIGNATION_BANDS
from .impacgo_people_seed_data import (
    BRANCHES,
    BUSINESS_UNITS,
    COMPANY_ID,
    COMPANY_NAME,
    DEPARTMENTS,
    EMAIL_DOMAIN,
    EMPLOYEES,
    QUICK_LOGIN_BY_ROLE,
)
from .payroll_seed_data import SALARY_COMPONENTS
from .performance_learning_seed_data import COURSES, OKRS, REVIEW_CYCLES

TODAY = datetime.date(2026, 8, 4)


def _already_has_rows(db, model, **filters) -> bool:
    query = select(func.count()).select_from(model)
    for key, value in filters.items():
        query = query.where(getattr(model, key) == value)
    return (db.scalar(query) or 0) > 0


# ---------------------------------------------------------------------------
# Company (find existing)
# ---------------------------------------------------------------------------


def find_company(db) -> models.Company:
    company_uuid = uuid.UUID(COMPANY_ID)
    company = db.scalar(select(models.Company).where(models.Company.id == company_uuid))
    if company is None:
        raise RuntimeError(
            f"Company '{COMPANY_NAME}' not found (id={COMPANY_ID}). "
            "Check the company ID."
        )
    if not company.email_domain:
        company.email_domain = EMAIL_DOMAIN
        db.commit()
        print(f"Set email_domain='{EMAIL_DOMAIN}' on company")
    print(f"Found company '{company.name}'")
    return company


def ensure_company_settings(db, company: models.Company) -> None:
    crud.upsert_company_settings(
        db, company.id,
        {"default_currency": "INR", "default_timezone": "IST (UTC+5:30)", "working_days_per_week": 5},
    )
    db.commit()


# ---------------------------------------------------------------------------
# Designations / Branches / Departments
# ---------------------------------------------------------------------------


def ensure_designations(db, company: models.Company) -> None:
    for band_name, titles in DESIGNATION_BANDS:
        for title in titles:
            try:
                crud.create_designation(db, company.id, title, band_name)
            except ValueError:
                db.rollback()
                continue
            db.commit()


def ensure_bands(db, company: models.Company) -> None:
    """Provisions this company's Band Management module rows -- idempotent,
    see seed_new_company.py's ensure_bands for the full reasoning."""
    crud.get_or_seed_company_bands(db, company.id)
    db.commit()
    print(f"Bands ready for '{company.name}'")


def ensure_branches(db, company: models.Company) -> dict[str, models.Branch]:
    branches: dict[str, models.Branch] = {}
    for b in BRANCHES:
        branch = db.scalar(
            select(models.Branch).where(
                models.Branch.company_id == company.id,
                models.Branch.code == b["code"],
            )
        )
        if branch is None:
            try:
                branch = crud.create_branch(
                    db, company.id,
                    schemas.BranchCreate(
                        code=b["code"], name=b["name"], country=b["country"],
                        state=b["state"], city=b["city"], tax=b["tax"],
                    ),
                )
                branch.is_head_office = b["is_head_office"]
                db.commit()
                print(f"Created branch '{b['name']}'")
            except ValueError:
                db.rollback()
                branch = db.scalar(
                    select(models.Branch).where(
                        models.Branch.company_id == company.id,
                        models.Branch.code == b["code"],
                    )
                )
        branches[b["code"]] = branch
    return branches


def ensure_business_units_and_departments(
    db, company: models.Company, branches: dict[str, models.Branch]
) -> dict[str, models.Department]:
    for u in BUSINESS_UNITS:
        try:
            crud.create_business_unit(db, company.id, u["name"], u["cost_center"])
            db.commit()
            print(f"Created business unit '{u['name']}'")
        except ValueError:
            db.rollback()

    departments: dict[str, models.Department] = {}
    for d in DEPARTMENTS:
        department = crud.get_department_by_name(db, company.id, d["name"])
        if department is None:
            try:
                department = crud.create_department(
                    db, company.id,
                    schemas.DepartmentCreate(
                        code=d["code"],
                        name=d["name"],
                        business_unit_name=d["business_unit_name"],
                        annual_budget=d["annual_budget"],
                        branch_id=branches[d["branch_code"]].id,
                    ),
                )
                db.commit()
                print(f"Created department '{d['name']}'")
            except ValueError:
                db.rollback()
                department = crud.get_department_by_name(db, company.id, d["name"])

        for sub_name in d["subs"]:
            crud.create_sub_department(db, department.id, sub_name)
        db.commit()
        departments[d["code"]] = department
    return departments


# ---------------------------------------------------------------------------
# Shifts / Holidays / Leave types / Salary components
# ---------------------------------------------------------------------------


def ensure_shifts(db, company: models.Company) -> dict[str, models.Shift]:
    shifts: dict[str, models.Shift] = {}
    for s in SHIFTS:
        shift = db.scalar(
            select(models.Shift).where(
                models.Shift.company_id == company.id,
                models.Shift.name == s["name"],
            )
        )
        if shift is None:
            shift = crud.create_shift(
                db, company.id, s["name"],
                datetime.datetime.strptime(s["start"], "%H:%M").time(),
                datetime.datetime.strptime(s["end"], "%H:%M").time(),
                s["is_night"],
            )
            db.commit()
            print(f"Created shift '{s['name']}'")
        shifts[s["name"]] = shift
    return shifts


def ensure_holidays(db, company: models.Company) -> None:
    for date_str, name, _region in HOLIDAYS:
        try:
            crud.create_holiday(db, company.id, datetime.date.fromisoformat(date_str), name)
            db.commit()
        except ValueError:
            db.rollback()


def ensure_leave_types(db, company: models.Company) -> dict[str, models.LeaveType]:
    leave_types: dict[str, models.LeaveType] = {}
    for name, code in LEAVE_TYPES:
        lt = db.scalar(
            select(models.LeaveType).where(
                models.LeaveType.company_id == company.id,
                models.LeaveType.code == code,
            )
        )
        if lt is None:
            lt = crud.create_leave_type(
                db, company.id, name, code,
                max_days_per_year=(
                    18 if code == "CL" else 12 if code == "SL" else 24 if code == "EL" else None
                ),
            )
            db.commit()
            print(f"Created leave type '{name}'")
        leave_types[code] = lt
    return leave_types


def ensure_salary_components(db, company: models.Company) -> dict[str, models.SalaryComponent]:
    components: dict[str, models.SalaryComponent] = {}
    for code, name, component_type, calc_type, is_taxable in SALARY_COMPONENTS:
        comp = db.scalar(
            select(models.SalaryComponent).where(
                models.SalaryComponent.company_id == company.id,
                models.SalaryComponent.code == code,
            )
        )
        if comp is None:
            comp = crud.create_salary_component(
                db, company.id, name, code, component_type, calc_type, is_taxable
            )
            db.commit()
            print(f"Created salary component '{name}'")
        components[code] = comp
    return components


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------


def ensure_employees(
    db, company: models.Company, branches: dict[str, models.Branch], departments: dict[str, models.Department]
) -> dict[str, models.Employee]:
    branch_name_by_code = {b["code"]: b["name"] for b in BRANCHES}
    department_name_by_code = {d["code"]: d["name"] for d in DEPARTMENTS}
    code_to_id: dict[str, uuid.UUID] = {}
    employees_by_code: dict[str, models.Employee] = {}

    for e in EMPLOYEES:
        existing = db.scalar(
            select(models.Employee).where(
                models.Employee.company_id == company.id,
                models.Employee.employee_code == e["employee_code"],
            )
        )
        if existing is not None:
            code_to_id[e["employee_code"]] = existing.id
            employees_by_code[e["employee_code"]] = existing
            continue

        manager_id = code_to_id.get(e["reporting_manager_code"]) if e["reporting_manager_code"] else None
        dob = e["date_of_birth"]
        doj = e["date_of_joining"]
        payload = SimpleNamespace(
            employee_code=e["employee_code"],
            first_name=e["first_name"],
            last_name=e["last_name"],
            work_email=e["work_email"],
            role_name=e["role_name"],
            password=e["password"],
            phone=e["phone"],
            gender=e["gender"],
            date_of_birth=dob.isoformat() if hasattr(dob, "isoformat") else dob,
            date_of_joining=doj.isoformat() if hasattr(doj, "isoformat") else doj,
            employment_type=e["employment_type"],
            status=e["status"],
            branch_name=branch_name_by_code[e["branch_code"]],
            department_name=department_name_by_code[e["department_code"]],
            designation_name=e["designation_name"],
            work_mode=e["work_mode"],
            band=e["numeric_band"],
            annual_ctc=e["annual_ctc"],
            reporting_manager_id=manager_id,
            pan=e["pan"],
            bank_name=e["bank_name"],
            bank_account_no=e["bank_account_no"],
            bank_ifsc=e["bank_ifsc"],
            qualification=e["qualification"],
            institute=e["institute"],
            specialization=e["specialization"],
            year_of_passing=str(e["year_of_passing"]) if e["year_of_passing"] else None,
            previous_employer=e["previous_employer"],
            experience_years=str(e["experience_years"]) if e["experience_years"] is not None else "0",
            domain=e["domain"],
            skills=e["skills"],
            certifications=e["certifications"],
            emergency_contact_name=e["emergency_contact_name"],
            emergency_contact_relation=e["emergency_contact_relation"],
            emergency_contact_phone=e["emergency_contact_phone"],
            blood_group=e["blood_group"],
            nationality=e["nationality"],
            marital_status=e["marital_status"],
            personal_email=e["personal_email"],
            personal_phone=e["personal_phone"],
            current_address=e["current_address"],
            permanent_address=e["permanent_address"],
        )
        try:
            employee = crud.create_employee(db, company.id, payload)
            db.commit()
            print(f"Created employee {e['employee_code']} {e['first_name']} {e['last_name']}")
        except (ValueError, IntegrityError) as exc:
            db.rollback()
            print(f"Skipped {e['employee_code']} ({e['first_name']} {e['last_name']}): {exc}")
            existing = db.scalar(
                select(models.Employee).where(
                    models.Employee.company_id == company.id,
                    models.Employee.employee_code == e["employee_code"],
                )
            )
            if existing is None:
                continue
            employee = existing

        # patch sub_department
        dept = departments.get(e["department_code"])
        if dept and e.get("sub_department"):
            sub = db.scalar(
                select(models.SubDepartment).where(
                    models.SubDepartment.department_id == dept.id,
                    models.SubDepartment.name == e["sub_department"],
                )
            )
            if sub is not None:
                employee.sub_department_id = sub.id
                db.commit()

        code_to_id[e["employee_code"]] = employee.id
        employees_by_code[e["employee_code"]] = employee

    print(f"Seeded {len(employees_by_code)} employees")
    return employees_by_code


def assign_shifts(db, employees: dict[str, models.Employee], shifts: dict[str, models.Shift]) -> None:
    if not employees:
        return
    first_emp_id = next(iter(employees.values())).id
    if _already_has_rows(db, models.ShiftAssignment, employee_id=first_emp_id):
        return
    general = shifts.get("General Shift")
    early = shifts.get("Early Shift")
    us_overlap = shifts.get("US Overlap Shift")
    from_date = TODAY - datetime.timedelta(days=180)
    for i, employee in enumerate(employees.values()):
        shift = (us_overlap or general) if (us_overlap and i % 11 == 0) else (early or general if (early and i % 7 == 0) else general)
        if shift is None:
            continue
        try:
            crud.create_shift_assignment(db, employee.id, shift.id, from_date)
        except Exception:
            db.rollback()
            continue
    db.commit()
    print("Assigned shifts to employees")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(tenant_slug: str | None = None) -> None:
    if tenant_slug:
        print(f"Tenant schema: {tenant_slug}")
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
        company = find_company(db)
        ensure_company_settings(db, company)

        # Roles were seeded from backup — skip ensure_roles.
        ensure_designations(db, company)
        ensure_bands(db, company)
        branches = ensure_branches(db, company)
        departments = ensure_business_units_and_departments(db, company, branches)
        shifts = ensure_shifts(db, company)
        ensure_holidays(db, company)
        leave_types = ensure_leave_types(db, company)
        salary_components = ensure_salary_components(db, company)

        employees = ensure_employees(db, company, branches, departments)
        assign_shifts(db, employees, shifts)

        from . import seed_new_company_modules as modules

        modules.ensure_leave_allocations_and_requests(db, company, employees, leave_types)
        modules.ensure_attendance(db, company, employees)
        modules.ensure_payroll(db, company, employees, salary_components)
        modules.ensure_recruitment(db, company, departments)
        modules.ensure_performance_and_learning(db, company, employees)
        modules.ensure_benefits_assets_documents(db, company, employees)
        modules.ensure_projects_work_travel(db, company, employees, departments, branches)
        modules.ensure_exit_and_lifecycle(db, company, employees, departments)
        modules.ensure_recognitions_and_skills(db, company, employees)
        modules.ensure_hiring_and_salary_revisions(db, company, employees, departments)
        modules.ensure_notifications_and_integrations(db, company, employees)
        modules.ensure_audit_logs(db, company, employees)

        print("\n" + "=" * 70)
        print(f"Seed complete — tenant '{tenant_slug}', company '{COMPANY_NAME}'.")
        print("Quick-login accounts (one per role):")
        for role_name, e in QUICK_LOGIN_BY_ROLE.items():
            print(f"  {role_name:<35} {e['work_email']:<48} {e['password']}")
        print("=" * 70)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Seed impacgo people into a tenant schema."
    )
    parser.add_argument("--tenant", metavar="SLUG", default=None)
    args = parser.parse_args()
    run(tenant_slug=args.tenant)
