"""Idempotent seed script — safe to run multiple times.

Usage:
    python -m app.seed

Seeds (only inserting rows that don't already exist):
  1. A default currency (INR) and a single default company, so core.roles'
     NOT NULL company_id FK has somewhere to point — the frontend has no
     multi-company concept, so this one company row is all the API uses.
  2. The 11 built-in roles from kRoles, each with its kRoleBlurbs
     description.
  3. Their default RBAC matrix (kRbacMatrix), converted into
     core.permissions + core.role_permissions rows.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from . import crud, models, schemas, security
from .attendance_leave_seed_data import HOLIDAYS, LEAVE_TYPES, SHIFTS
from .benefits_assets_documents_seed_data import ASSET_INVENTORY, DOC_TEMPLATES, POLICY_DOCS
from .config import settings
from .database import SessionLocal
from .demo_users_seed_data import DEMO_USERS
from .designation_seed_data import DESIGNATION_BANDS
from .org_seed_data import BRANCHES, BUSINESS_UNITS, DEPARTMENTS
from .payroll_seed_data import SALARY_COMPONENTS
from .performance_learning_seed_data import COURSES, OKRS, REVIEW_CYCLES
from .projects_seed_data import PROJECTS
from .rbac_columns import BUILTIN_ROLES, COLUMN_KEYS, DEFAULT_MATRIX, ROLE_BLURBS
from .recruitment_seed_data import CANDIDATES, INTERVIEWS, JOB_OPENINGS, OFFERS


def _ensure_company(db) -> models.Company:
    company = db.scalar(select(models.Company).where(models.Company.name == settings.default_company_name))
    if company is not None:
        return company

    currency = db.scalar(select(models.Currency).where(models.Currency.code == "INR"))
    if currency is None:
        currency = models.Currency(id=uuid.uuid4(), code="INR", name="Indian Rupee", symbol="₹")
        db.add(currency)
        db.flush()

    company = models.Company(
        id=uuid.uuid4(),
        name=settings.default_company_name,
        default_currency_id=currency.id,
    )
    db.add(company)
    db.flush()
    print(f"Created company '{company.name}'")
    return company


def _ensure_role(db, company: models.Company, name: str) -> models.Role:
    role = db.scalar(
        select(models.Role).where(models.Role.company_id == company.id, models.Role.name == name)
    )
    if role is not None:
        return role

    role = models.Role(
        id=uuid.uuid4(),
        company_id=company.id,
        name=name,
        description=ROLE_BLURBS.get(name),
        is_system=True,
    )
    db.add(role)
    db.flush()
    print(f"Created role '{name}'")
    return role


def _ensure_designations(db, company: models.Company) -> None:
    for band_name, titles in DESIGNATION_BANDS:
        for title in titles:
            try:
                crud.create_designation(db, company.id, title, band_name)
            except ValueError:
                # Already exists (e.g. re-running the seed, or an admin
                # already added it via the API) -- leave it untouched.
                db.rollback()
                continue
            db.commit()
            print(f"Created designation '{title}' ({band_name})")


def _ensure_bands(db, company: models.Company) -> None:
    """Provisions this company's Band Management module rows -- idempotent
    (crud.get_or_seed_company_bands no-ops if the company already has any
    core_bands rows), safe to call on every re-run of this script."""
    crud.get_or_seed_company_bands(db, company.id)
    db.commit()
    print(f"Bands ready for '{company.name}'")


def _ensure_branches(db, company: models.Company) -> None:
    """core.branches already exists as-is in schema.sql -- no manual
    additions needed for this part."""
    for b in BRANCHES:
        try:
            crud.create_branch(
                db,
                company.id,
                schemas.BranchCreate(
                    code=b["code"],
                    name=b["name"],
                    country=b["country"],
                    state=b["state"],
                    city=b["city"],
                    tax=b["tax"],
                ),
            )
        except ValueError:
            db.rollback()
            continue
        db.commit()
        print(f"Created branch '{b['name']}'")


def _ensure_business_units_and_departments(db, company: models.Company) -> None:
    """Requires core.business_units and core.sub_departments -- both manual
    additions (backend/db/manual_schema_additions.sql section 1). Skips
    cleanly with a clear message if those tables don't exist yet, so the
    rest of the seed script still runs."""
    try:
        for u in BUSINESS_UNITS:
            try:
                crud.create_business_unit(db, company.id, u["name"], u["cost_center"])
            except ValueError:
                db.rollback()
                continue
            db.commit()
            print(f"Created business unit '{u['name']}'")

        for d in DEPARTMENTS:
            try:
                department = crud.create_department(
                    db,
                    company.id,
                    schemas.DepartmentCreate(
                        code=d["code"],
                        name=d["name"],
                        business_unit_name=d["business_unit_name"],
                    ),
                )
                db.commit()
                print(f"Created department '{d['name']}'")
            except ValueError:
                # create_department's own duplicate check is by *code*, not
                # name (core.departments' real unique constraint) -- an
                # existing company can easily have the same code under a
                # different display name, so look up the same way the
                # ValueError was raised before falling back to name.
                db.rollback()
                department = crud.get_department_by_code(
                    db, company.id, d["code"]
                ) or crud.get_department_by_name(db, company.id, d["name"])

            if department is None:
                print(
                    f"Skipped sub-departments for '{d['name']}' — no matching "
                    "department found by code or name after a duplicate error."
                )
                continue

            for sub_name in d["subs"]:
                crud.create_sub_department(db, department.id, sub_name)
            db.commit()
    except ProgrammingError:
        db.rollback()
        print(
            "Skipped Business Units/Departments seeding — core.business_units / "
            "core.sub_departments don't exist yet. Apply "
            "backend/db/manual_schema_additions.sql and re-run this seed script."
        )


def _ensure_shifts_holidays_leave_types(db, company: models.Company) -> None:
    """All three tables already exist as-is in schema.sql -- no manual
    additions needed. Shifts are seeded for API completeness even though the
    frontend doesn't read them yet this phase (see Phase 2 notes: hydrating
    the Shifts tab isn't safe until "assigned" headcounts have a real source)."""
    for s in SHIFTS:
        try:
            crud.create_shift(
                db,
                company.id,
                s["name"],
                datetime.strptime(s["start"], "%H:%M").time(),
                datetime.strptime(s["end"], "%H:%M").time(),
                s["is_night"],
            )
        except ValueError:
            db.rollback()
            continue
        db.commit()
        print(f"Created shift '{s['name']}'")

    for date_str, name, _region in HOLIDAYS:
        try:
            crud.create_holiday(db, company.id, date.fromisoformat(date_str), name)
        except ValueError:
            db.rollback()
            continue
        db.commit()
        print(f"Created holiday '{name}'")

    for name, code in LEAVE_TYPES:
        try:
            crud.create_leave_type(db, company.id, name, code)
        except ValueError:
            db.rollback()
            continue
        db.commit()
        print(f"Created leave type '{name}'")


def _ensure_salary_components(db, company: models.Company) -> None:
    """core.salary_components already exists as-is in schema.sql -- no
    manual additions needed. This is just a reference catalog (see Phase 3
    notes: no per-employee salary structures/slips are seeded, since nothing
    in the frontend reads them yet)."""
    for code, name, component_type, calc_type, is_taxable in SALARY_COMPONENTS:
        try:
            crud.create_salary_component(
                db, company.id, name, code, component_type, calc_type, is_taxable
            )
        except ValueError:
            db.rollback()
            continue
        db.commit()
        print(f"Created salary component '{name}'")


def _ensure_recruitment(db, company: models.Company) -> None:
    """Requires hcm.job_openings.employment_type/posted_date and
    hcm.candidates.years_experience -- manual additions (section 11). Skips
    cleanly if they're missing, or if departments haven't been seeded yet
    (job openings need a real department_id)."""
    try:
        for j in JOB_OPENINGS:
            try:
                crud.create_job_opening(
                    db,
                    company.id,
                    j["title"],
                    j["department"],
                    j["employment_type"],
                    j["vacancies"],
                    status=j["status"],
                    posted_date=date.fromisoformat(j["posted_date"]),
                )
            except ValueError:
                db.rollback()
                continue
            db.commit()
            print(f"Created job opening '{j['title']}'")

        for name, job_title, years_experience, stage in CANDIDATES:
            existing = db.scalar(
                select(models.Candidate).where(
                    models.Candidate.company_id == company.id, models.Candidate.name == name
                )
            )
            if existing is not None:
                continue
            opening = crud.find_job_opening_by_title(db, company.id, job_title)
            if opening is None:
                continue
            candidate = crud.create_candidate(db, company.id, name, years_experience)
            crud.create_job_application(db, opening.id, candidate.id, stage)
            db.commit()
            print(f"Created candidate '{name}' ({stage})")

        for i in INTERVIEWS:
            application = crud.find_application_by_candidate_and_opening(
                db, i["candidate"], i["job_title"]
            )
            if application is None:
                continue
            existing = db.scalar(
                select(models.Interview).where(models.Interview.application_id == application.id)
            )
            if existing is not None:
                continue
            crud.create_interview(
                db, application.id, i["round_no"], datetime.fromisoformat(i["scheduled_at"])
            )
            db.commit()
            print(f"Created interview for '{i['candidate']}'")

        for o in OFFERS:
            application = crud.find_application_by_candidate_and_opening(
                db, o["candidate"], o["job_title"]
            )
            if application is None:
                continue
            existing = db.scalar(
                select(models.Offer).where(models.Offer.application_id == application.id)
            )
            if existing is not None:
                continue
            crud.create_offer(
                db,
                application.id,
                o["offered_ctc"],
                date.fromisoformat(o["offer_date"]),
                date.fromisoformat(o["proposed_joining_date"]),
                o["status"],
            )
            db.commit()
            print(f"Created offer for '{o['candidate']}'")
    except ProgrammingError:
        db.rollback()
        print(
            "Skipped Recruitment seeding — hcm.job_openings.employment_type/"
            "posted_date or hcm.candidates.years_experience don't exist yet. "
            "Apply backend/db/manual_schema_additions.sql and re-run this seed script."
        )


def _ensure_performance_and_learning(db, company: models.Company) -> None:
    """OKRs and Review Cycles need hcm.company_okrs and
    hcm.appraisal_cycles.participant_count -- manual additions (section 7).
    Courses need hcm.training_courses.category/duration_label (same
    section). Skips cleanly if not applied yet."""
    try:
        for level, title, owner_name, progress in OKRS:
            existing = db.scalar(
                select(models.CompanyOkr).where(
                    models.CompanyOkr.company_id == company.id, models.CompanyOkr.title == title
                )
            )
            if existing is not None:
                continue
            okr = crud.create_okr(db, company.id, level, title, owner_name)
            okr.progress_pct = progress
            db.commit()
            print(f"Created OKR '{title}'")

        for r in REVIEW_CYCLES:
            try:
                crud.create_review_cycle(
                    db,
                    company.id,
                    r["name"],
                    date.fromisoformat(r["from_date"]),
                    date.fromisoformat(r["to_date"]),
                    r["status"],
                    r["participant_count"],
                )
            except ValueError:
                db.rollback()
                continue
            db.commit()
            print(f"Created review cycle '{r['name']}'")

        for title, course_type, category, duration_hours, duration_label in COURSES:
            existing = db.scalar(
                select(models.TrainingCourse).where(
                    models.TrainingCourse.company_id == company.id,
                    models.TrainingCourse.title == title,
                )
            )
            if existing is not None:
                continue
            crud.create_training_course(
                db, company.id, title, course_type, category, duration_hours, duration_label
            )
            db.commit()
            print(f"Created course '{title}'")
    except ProgrammingError:
        db.rollback()
        print(
            "Skipped Performance/Learning seeding — hcm.company_okrs, "
            "hcm.appraisal_cycles.participant_count, or hcm.training_courses."
            "category/duration_label don't exist yet. Apply "
            "backend/db/manual_schema_additions.sql and re-run this seed script."
        )


def _ensure_benefits_assets_documents(db, company: models.Company) -> None:
    """Requires hcm.asset_inventory, hcm.policy_documents.acknowledgement_pct
    -- manual additions (sections 4 and 5). No custom benefit categories are
    seeded (WorkforceStore.customBenefitCategories starts empty too)."""
    try:
        for tag, asset_type, model, status, value, purchased in ASSET_INVENTORY:
            try:
                crud.create_asset_inventory_item(
                    db,
                    company.id,
                    tag,
                    asset_type,
                    model,
                    status,
                    crud.parse_inr_amount(value),
                    crud.parse_date_safe(purchased),
                )
            except ValueError:
                db.rollback()
                continue
            db.commit()
            print(f"Created asset '{tag}'")

        for name, version, effective_date, ack in POLICY_DOCS:
            try:
                crud.create_policy_document(
                    db,
                    company.id,
                    "policy",
                    name,
                    version,
                    date.fromisoformat(effective_date),
                    ack,
                )
            except ValueError:
                db.rollback()
                continue
            db.commit()
            print(f"Created policy document '{name}'")

        for name in DOC_TEMPLATES:
            try:
                crud.create_policy_document(db, company.id, "template", name, None, None, None)
            except ValueError:
                db.rollback()
                continue
            db.commit()
            print(f"Created document template '{name}'")
    except ProgrammingError:
        db.rollback()
        print(
            "Skipped Benefits/Assets/Documents seeding — hcm.asset_inventory or "
            "hcm.policy_documents.acknowledgement_pct don't exist yet. Apply "
            "backend/db/manual_schema_additions.sql and re-run this seed script."
        )


def _ensure_projects(db, company: models.Company) -> None:
    """hcm.projects.pm_name is a manual addition (section 6) -- skips
    cleanly if not applied yet."""
    try:
        for p in PROJECTS:
            try:
                crud.create_project(
                    db,
                    company.id,
                    p["name"],
                    p["client_name"],
                    p["project_type"],
                    p["pm_name"],
                    p["status"],
                    date.fromisoformat(p["start_date"]),
                    date.fromisoformat(p["end_date"]),
                    crud.parse_inr_abbreviated(p["budget"]),
                    p["progress_pct"],
                )
            except ValueError:
                db.rollback()
                continue
            db.commit()
            print(f"Created project '{p['name']}'")
    except ProgrammingError:
        db.rollback()
        print(
            "Skipped Projects seeding — hcm.projects.pm_name doesn't exist yet. "
            "Apply backend/db/manual_schema_additions.sql and re-run this seed script."
        )


def _ensure_tenant(db, company: models.Company) -> models.Tenant:
    """Ensures a public.tenants row exists for the default tenant so
    public.users rows can reference it. Safe to call when public.tenants
    doesn't exist yet (skips gracefully)."""
    try:
        return crud.get_or_create_tenant(db, settings.default_tenant_slug, company.name)
    except Exception:
        db.rollback()
        print(
            f"Skipped tenant seeding — public.tenants may not exist yet. "
            "Apply multitenant_schema.sql and re-run this seed script."
        )
        return None


def _ensure_demo_users(db, company: models.Company) -> None:
    """One core.users + minimal core.employees row per assets/data/
    dummy_credentials.json entry, so the frontend's login screen can
    authenticate against the real backend with the exact same demo
    credentials it already documents. Employee rows created here are
    placeholders (Phase 1 fleshes out real employee data).
    Also writes public.admin_users + public.users rows (same UUID) for
    the multi-tenant global login system."""
    tenant = _ensure_tenant(db, company)
    if tenant is not None:
        db.commit()

    for email, password, role_name in DEMO_USERS:
        existing = db.scalar(select(models.User).where(models.User.email == email))
        if existing is not None:
            # Back-fill public.admin_users / public.users for existing core.users rows
            # that were created before this migration.
            _backfill_public_login(db, existing, email, password, role_name, company)
            db.commit()
            continue

        role = db.scalar(
            select(models.Role).where(
                models.Role.company_id == company.id, models.Role.name == role_name
            )
        )
        if role is None:
            print(f"Skipping demo user '{email}' — role '{role_name}' not seeded yet")
            continue

        # Raw INSERT with only the guaranteed-to-exist core.employees
        # columns -- the models.Employee ORM class also maps columns that
        # are manual additions (sub_department_id, team, work_mode, band,
        # annual_ctc). Going through the ORM here would include those in
        # the INSERT and fail on a database that hasn't applied
        # manual_schema_additions.sql yet, which Phase 0 (login) is
        # specifically designed not to require.
        employee_id = uuid.uuid4()
        db.execute(
            text(
                """
                INSERT INTO core_employees
                    (id, company_id, employee_code, first_name, date_of_joining, status, is_active)
                VALUES
                    (:id, :company_id, :employee_code, :first_name, :date_of_joining, :status, :is_active)
                """
            ),
            {
                "id": employee_id,
                "company_id": company.id,
                "employee_code": email.split("@")[0].upper(),
                "first_name": role_name,
                "date_of_joining": date(2020, 1, 1),
                "status": "active",
                "is_active": True,
            },
        )

        password_hash = security.hash_password(password)
        user = models.User(
            id=uuid.uuid4(),
            company_id=company.id,
            email=email,
            password_hash=password_hash,
            employee_id=employee_id,
            status="active",
        )
        db.add(user)
        db.flush()

        db.add(models.UserRole(user_id=user.id, role_id=role.id))

        _backfill_public_login(db, user, email, password, role_name, company)
        db.commit()
        print(f"Created demo user '{email}' ({role_name})")


def _backfill_public_login(db, user: models.User, email: str, password: str, role_name: str, company: models.Company) -> None:
    """Writes public.admin_users + public.users rows for a core.users record
    that exists but wasn't created via the new dual-write path. Skips
    gracefully if the public schema tables don't exist yet."""
    try:
        crud.create_admin_user_login(
            db,
            user_id=user.id,
            email=email,
            password_hash=user.password_hash,
            full_name=None,
            role_name=role_name,
            tenant_slug=settings.default_tenant_slug,
            company_id=company.id,
        )
        db.flush()
    except Exception:
        db.rollback()
        print(f"Skipped public login backfill for '{email}' — public schema tables may not exist yet.")


def seed_master_data(db, company: models.Company) -> None:
    """Every org-structure master data concept a company needs: the built-in
    roles + their default RBAC matrix, designations/bands, branches,
    business units, departments/sub-departments, shifts, holidays, leave
    types, and salary components. Generalized from run()'s original
    single-default-company bootstrap so it can seed ANY company --
    including ones that already exist and were never seeded (e.g. created
    by a direct SQL insert). Idempotent and non-destructive: every
    underlying _ensure_*/create_* call only adds rows that don't already
    exist for that company, and a role's RBAC matrix is only seeded the
    first time that role is created -- existing custom roles/data (e.g. a
    company with extra roles beyond the 14 built-ins) are left untouched."""
    for role_name in BUILTIN_ROLES:
        role = _ensure_role(db, company, role_name)
        db.commit()

        # Only seed the matrix the first time a role is created — if an
        # admin has since edited it via the API, re-running the seed
        # must not clobber their changes.
        if role.role_permissions:
            continue

        levels = DEFAULT_MATRIX[role_name].split(",")
        updates = dict(zip(COLUMN_KEYS, levels))
        crud.apply_matrix_update(db, role, updates)
        db.commit()
        print(f"Seeded RBAC matrix for '{role_name}' ({company.name})")

    _ensure_designations(db, company)
    _ensure_bands(db, company)
    _ensure_branches(db, company)
    _ensure_business_units_and_departments(db, company)
    _ensure_shifts_holidays_leave_types(db, company)
    _ensure_salary_components(db, company)


def run():
    db = SessionLocal()
    try:
        company = _ensure_company(db)
        db.commit()

        seed_master_data(db, company)
        _ensure_recruitment(db, company)
        _ensure_performance_and_learning(db, company)
        _ensure_benefits_assets_documents(db, company)
        _ensure_projects(db, company)
        _ensure_demo_users(db, company)

        print("Seed complete.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
