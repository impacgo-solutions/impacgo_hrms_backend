"""One-off seeder for the ACTUAL 'impacgo-solutions' tenant (schema
"impacgo-solutions", company_id 4fd9da4f-397f-4fbd-be50-e1245b5226cf,
"Impacgo Solutions Pvt Ltd") -- the tenant real users (e.g.
srikar@impacgo.com, soumya@impacgo.com) log into.

This is a *different* company from "Impacgo solutions Private Limited"
(company_id caf06078-...) which lives in the shared 'acme' schema and is
seeded by seed_impacgo_company.py -- that one is an unrelated demo company
that happens to have a near-identical name. Don't confuse the two.

Adds, from impacgo-website content: 2 business units (Dynamics 365
Services, Product Engineering), 5 departments with their sub-departments,
and the 20-project/product catalogue (real named clients + in-house
products) reused as-is from impacgo_company_seed_data.IMPACGO_PROJECTS
(that list's content isn't tied to any specific company id).

Leaves the existing "Visakhapatnam Branch" (code VISAKHAPAT9934) and
"Administration" (ADMIN01) department untouched -- both are only read,
never modified. Idempotent: safe to re-run.

Usage:
    python -m app.seed_impacgo_solutions_projects
"""

import datetime
import uuid

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from . import crud, models, schemas
from .config import settings
from .database import set_tenant_context
from .impacgo_company_seed_data import IMPACGO_PROJECTS

TENANT_SLUG = "impacgo-solutions"
COMPANY_ID = "4fd9da4f-397f-4fbd-be50-e1245b5226cf"
BRANCH_CODE = "VISAKHAPAT9934"

BUSINESS_UNITS = [
    {"name": "Dynamics 365 Services", "cost_center": "CC-IPG-500"},
    {"name": "Product Engineering", "cost_center": "CC-IPG-600"},
]

DEPARTMENTS = [
    {"code": "D365", "name": "Dynamics 365 Practice", "business_unit_name": "Dynamics 365 Services",
     "subs": ["D365 Implementation", "D365 Development & Enhancement", "Application Management Services (AMS)", "D365 Consulting"],
     "annual_budget": 25_000_000},
    {"code": "PPAI", "name": "Power Platform & AI", "business_unit_name": "Dynamics 365 Services",
     "subs": ["Power BI", "Power Apps", "Power Automate", "AI Automation & Copilot"],
     "annual_budget": 12_000_000},
    {"code": "MIGINT", "name": "Migration & Integrations", "business_unit_name": "Dynamics 365 Services",
     "subs": ["AX to D365 Migration", "Third-Party Integrations", "MES Integration"],
     "annual_budget": 10_000_000},
    {"code": "PRDENG", "name": "Product Engineering", "business_unit_name": "Product Engineering",
     "subs": ["Impacgo ERP Suite Engineering", "Platform & QA"],
     "annual_budget": 35_000_000},
    {"code": "SAASPD", "name": "SaaS Products", "business_unit_name": "Product Engineering",
     "subs": ["CALVIQ – Dairy Farm Management", "StockLyte – Inventory Management", "FarmYieldIQ", "HealthVault – Family Health Records", "Work Task & Construction Planner"],
     "annual_budget": 20_000_000},
]


def _already_has_rows(db, model, **filters) -> bool:
    query = select(model)
    for key, value in filters.items():
        query = query.where(getattr(model, key) == value)
    return db.scalar(query.limit(1)) is not None


def run() -> None:
    set_tenant_context(TENANT_SLUG)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=True)
    db = Session()
    db.execute(text(f'SET search_path TO "{TENANT_SLUG}", public'))

    try:
        company = db.get(models.Company, uuid.UUID(COMPANY_ID))
        if company is None:
            raise RuntimeError(f"Company {COMPANY_ID} not found in tenant '{TENANT_SLUG}'")
        print(f"Found company '{company.name}' in tenant '{TENANT_SLUG}'")

        branch = db.scalar(
            select(models.Branch).where(
                models.Branch.company_id == company.id, models.Branch.code == BRANCH_CODE
            )
        )
        if branch is None:
            raise RuntimeError(f"Branch '{BRANCH_CODE}' not found — refusing to guess a branch")
        print(f"Using existing branch '{branch.name}' ({branch.code}) — left untouched")

        # -- Business units --
        for u in BUSINESS_UNITS:
            try:
                crud.create_business_unit(db, company.id, u["name"], u["cost_center"])
                db.commit()
                print(f"Created business unit '{u['name']}'")
            except ValueError:
                db.rollback()

        # -- Departments + sub-departments --
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
                            branch_id=branch.id,
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

        # -- Projects / products --
        _PRJ_STATUS = {"draft", "planning", "active", "on_hold", "completed", "cancelled"}
        created = 0
        skipped = 0
        for p in IMPACGO_PROJECTS:
            if _already_has_rows(db, models.Project, company_id=company.id, code=p["code"]):
                skipped += 1
                continue
            department = departments.get(p["department_code"])
            customer = crud.get_or_create_customer(db, company.id, p["client_name"])
            category = crud.get_or_create_project_category(db, company.id, p["category_name"])
            db.commit()
            try:
                crud.create_project(
                    db, company.id, p["name"],
                    p["status"] if p["status"] in _PRJ_STATUS else "planning",
                    planned_start_date=datetime.date.fromisoformat(p["planned_start_date"]),
                    planned_end_date=(
                        datetime.date.fromisoformat(p["planned_end_date"])
                        if p["planned_end_date"] else None
                    ),
                    is_billable=p["is_billable"],
                    code=p["code"],
                    customer_id=customer.id if customer else None,
                    category_id=category.id if category else None,
                    business_unit_id=department.business_unit_id if department else None,
                    department_id=department.id if department else None,
                )
                db.commit()
                created += 1
            except (ValueError, IntegrityError) as exc:
                db.rollback()
                skipped += 1
                print(f"Skipped project '{p['name']}': {exc}")
        print(f"Seeded {created} projects/products ({skipped} already present/skipped)")

        print("\n" + "=" * 70)
        print(f"Done — tenant '{TENANT_SLUG}', company '{company.name}'.")
        print("=" * 70)
    finally:
        db.close()


if __name__ == "__main__":
    run()
