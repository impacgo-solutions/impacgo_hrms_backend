"""M-21: claim numbers and double-submit protection for hcm_expense_claims
(Travel & Expenses expense reports "EXP-..." and Payroll reimbursements
"REIM-...").

claim_no used to be "<prefix>-<employee>-<date>", so an employee could file
only one expense report per submission date (and one reimbursement per
expense date) -- the second got 409. Now:

* claim_no = "<prefix>-<YYYYMMDD>-<6 random hex>" (unique per company; a
  collision is re-drawn), and
* a double-click / retried request is caught separately: an identical
  pending claim (same employee, kind, date, purpose and total) created in
  the last DUPLICATE_WINDOW_SECONDS is returned instead of a second row.
  Callers serialize on the employee with an advisory lock first.
"""

from __future__ import annotations

import datetime
import secrets
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models

DUPLICATE_WINDOW_SECONDS = 120


def lock_employee(db: Session, kind: str, company_id: uuid.UUID, employee_id: uuid.UUID) -> None:
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"{kind}:{company_id}:{employee_id}"))))


def generate_claim_no(db: Session, company_id: uuid.UUID, prefix: str, on: datetime.date) -> str:
    for _ in range(10):
        claim_no = f"{prefix}-{on.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
        taken = db.scalar(
            select(models.ExpenseClaim.id).where(
                models.ExpenseClaim.company_id == company_id, models.ExpenseClaim.claim_no == claim_no
            ).limit(1)
        )
        if taken is None:
            return claim_no
    raise RuntimeError("Could not allocate a unique claim number")


def recent_duplicate(
    db: Session,
    company_id: uuid.UUID,
    employee_id: uuid.UUID,
    prefix: str,
    claim_date: datetime.date,
    purpose: str | None,
    total: float,
) -> models.ExpenseClaim | None:
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
    return db.scalar(
        select(models.ExpenseClaim).where(
            models.ExpenseClaim.company_id == company_id,
            models.ExpenseClaim.employee_id == employee_id,
            models.ExpenseClaim.claim_no.like(f"{prefix}-%"),
            models.ExpenseClaim.claim_date == claim_date,
            models.ExpenseClaim.purpose == purpose,
            models.ExpenseClaim.total_amount == round(float(total), 2),
            models.ExpenseClaim.status == "pending",
            models.ExpenseClaim.created_at >= since,
        ).order_by(models.ExpenseClaim.created_at.desc()).limit(1)
    )


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
