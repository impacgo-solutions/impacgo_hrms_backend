"""Unique, sequential Travel Request IDs: 'HR-TR-<year>-<5 digits>', the same
shape as Expense Reports' 'HR-EXP-<year>-NNNNN' (travel_expense.
next_claim_series).

Assigned in one place -- just before a new hcm_travel_requests row is first
flushed -- so every way a request can be created (the Travel form, the older
create endpoint, seed / import scripts) gets one, and none can skip it. A
per-company, per-year advisory lock serializes concurrent creates, and the
database's unique index (company_id, request_no) is the last line of defence
(see backend/db/add_request_ids_and_expense_receipts.sql).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from . import models

TRAVEL_SERIES_PREFIX = "HR-TR"


def next_travel_numbers(db: Session, company_id: uuid.UUID, year: int, count: int = 1,
                        flush: bool = True) -> list[str]:
    """The next [count] Travel Request IDs of a company, in its configured
    format (Administration > General Settings > Document ID Formats)."""
    from . import document_numbers

    return document_numbers.next_ids(db, company_id, "travel_request", count, flush=flush)


@event.listens_for(Session, "before_flush")
def _number_new_travel_requests(session: Session, flush_context, instances) -> None:
    pending = [o for o in session.new if isinstance(o, models.TravelRequestModel) and not o.request_no]
    if not pending:
        return
    year = datetime.date.today().year
    with session.no_autoflush:
        groups: dict[uuid.UUID, list[models.TravelRequestModel]] = {}
        for row in pending:
            if row.company_id is None:
                employee = session.get(models.Employee, row.employee_id)
                row.company_id = employee.company_id if employee is not None else None
            if row.company_id is not None:
                groups.setdefault(row.company_id, []).append(row)
        for company_id, rows in groups.items():
            for row, number in zip(rows, next_travel_numbers(session, company_id, year, len(rows), flush=False)):
                row.request_no = number
