"""Backfills org-structure master data for every company already in the
database -- roles + RBAC matrix, designations/bands, branches, business
units, departments/sub-departments, shifts, holidays, leave types, and
salary components (see seed.seed_master_data). Covers companies created
outside any seed script too (e.g. a direct SQL insert), which otherwise
stay stuck with whatever partial data they happened to get.

No demo/business data (sample projects, candidates, performance cycles,
demo login users) is touched, and no new company is created here -- this
only ever adds master data to companies that already exist. Idempotent and
non-destructive, same as seed.py itself: safe to re-run any time (e.g.
after adding a new company row, or after extending the master data lists),
and never overwrites a company's existing custom roles/data.

Usage:
    python -m app.seed_all_companies
"""

from sqlalchemy import select

from . import models
from .database import SessionLocal
from .seed import seed_master_data


def run():
    db = SessionLocal()
    try:
        companies = db.scalars(select(models.Company)).all()
        for company in companies:
            print(f"--- {company.name} ---")
            seed_master_data(db, company)
        print(f"Done. Backfilled {len(companies)} companies.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
