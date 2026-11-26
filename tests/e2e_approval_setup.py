"""Creates the two QA tenants used by tests/e2e_approval_modes.py:

  qa-chain-approval     -> Company approval mode: CHAIN  (R1 then R2)
  qa-parallel-approval  -> Company approval mode: PARALLEL (R1 or R2)

Each gets, via the official provisioning (app.provision_tenant) and
crud.create_employee (same path as POST /api/employees):
  Owner  owner@<slug>.example.com
  R1     r1@<slug>.example.com   (Reporting Manager of E1)
  R2     r2@<slug>.example.com   (Second Reporting Manager of E1)
  E1     e1@<slug>.example.com   (applies for leave)
All passwords: QaTest@2026!   Idempotent: an existing tenant is reused.

    cd backend && <venv>/Scripts/python -m tests.e2e_approval_setup
"""

from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas  # noqa: E402
from app.provision_tenant import provision_tenant  # noqa: E402

PASSWORD = "QaTest@2026!"
TENANTS = {
    "qa-chain-approval": "QA Chain Approval Pvt Ltd",
    "qa-parallel-approval": "QA Parallel Approval Pvt Ltd",
}


def email(slug: str, who: str) -> str:
    return f"{who}@{slug}.example.com"


def ensure_tenant(slug: str, name: str) -> None:
    with database.engine.connect() as c:
        exists = c.execute(text("SELECT 1 FROM public.tenants WHERE slug = :s"), {"s": slug}).first()
    if exists:
        print(f"{slug}: already exists, reusing")
        return
    provision_tenant(slug, name, name, "Qa", "Owner", email(slug, "owner"), PASSWORD)


def ensure_people(slug: str) -> None:
    with database.engine.connect() as conn:
        tx = conn.begin()
        conn.execute(text(f'SET LOCAL search_path TO "{slug}", public'))
        db = Session(bind=conn, join_transaction_mode="create_savepoint")
        database.set_tenant_context(slug)
        database.set_session_tenant_slug(db, slug)
        database.ensure_tenant_search_path(db)
        company = db.scalars(select(models.Company)).first()
        branch = db.scalars(select(models.Branch)).first()
        dept = db.scalars(select(models.Department)).first()
        people = {}
        for who, first, role in [("r1", "Ravi", "Manager"), ("r2", "Priya", "Manager"),
                                 ("e1", "Esha", "Professional / IC Employee")]:
            addr = email(slug, who)
            emp = db.scalars(select(models.Employee).where(models.Employee.work_email == addr)).first()
            if emp is None:
                emp = crud.create_employee(db, company.id, schemas.EmployeeCreate(
                    first_name=first, last_name=who.upper(), work_email=addr, role_name=role, password=PASSWORD,
                    date_of_joining=datetime.date.today() - datetime.timedelta(days=400),
                    employment_type="full_time", status="active", branch_name=branch.name,
                    department_name=dept.name, designation_name="Engineer" if who == "e1" else "Engineering Manager",
                ))
                db.flush()
            people[who] = emp
        e1 = people["e1"]
        e1.reporting_manager_id = people["r1"].id
        e1.dotted_line_manager_id = people["r2"].id
        conn.execute(text(f'SET LOCAL search_path TO "{slug}", public'))
        # A leave type with enough balance for E1.
        lt = db.scalars(select(models.LeaveType).where(models.LeaveType.company_id == company.id)).first()
        lt_name = lt.name if lt else "NONE"
        db.commit()
        tx.commit()
        print(f"{slug}: owner/r1/r2/e1 ready; leave type: {lt_name}")


if __name__ == "__main__":
    for slug, name in TENANTS.items():
        ensure_tenant(slug, name)
        ensure_people(slug)
