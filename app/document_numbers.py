"""Configurable document IDs -- Administration > General Settings > Document
ID Formats -- for Projects, Travel Requests and Expense Reports, the same idea
as Employee ID Format.

An ID is  {prefix}{year + "-" when include_year}{zero-padded number}{suffix},
e.g. PRJ-0001, HR-TR-2026-00001, HR-EXP-2026-00001 (the defaults, identical to
the formats used before this was configurable).

Each company keeps one core_number_series row per document type:
  prefix / suffix / padding   the format
  fiscal_infix                include the year (numbering restarts each year)
  current_no                  floor: the next number is at least current_no + 1
                              (the admin's "Starting Number" sets it)

The next number is one past the highest ID already issued in the CURRENT
format (and year), never below the floor -- so an ID is never handed out twice,
changing the format starts that format's own sequence, and IDs issued earlier
never change. A company-wide advisory lock per document type serializes
concurrent creates; the tables' unique indexes are the last line of defence.
Without a year, every issued number also raises the floor, so deleting the
newest record never frees its number for reuse.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models


@dataclass(frozen=True)
class DocType:
    key: str
    label: str
    prefix: str
    padding: int
    include_year: bool
    model: type
    column: str
    # The table is per company through this column (travel requests carry
    # company_id since add_request_ids_and_expense_receipts.sql).
    company_column: str = "company_id"


DOCTYPES: dict[str, DocType] = {
    "project": DocType("project", "Project ID", "PRJ-", 4, False, models.Project, "code"),
    "travel_request": DocType("travel_request", "Travel Request ID", "HR-TR-", 5, True,
                              models.TravelRequestModel, "request_no"),
    "expense_report": DocType("expense_report", "Expense Report ID", "HR-EXP-", 5, True,
                              models.ExpenseClaim, "claim_no"),
}


def doctype(key: str) -> DocType:
    try:
        return DOCTYPES[key]
    except KeyError:
        raise ValueError(f"Unknown document type '{key}'") from None


def get_series(db: Session, company_id: uuid.UUID, key: str, flush: bool = True) -> models.NumberSeries:
    """This company's format for [key], created with the default on first use."""
    d = doctype(key)
    with db.no_autoflush:
        series = db.scalar(select(models.NumberSeries).where(
            models.NumberSeries.company_id == company_id, models.NumberSeries.doctype == d.key))
    if series is not None:
        return series
    series = models.NumberSeries(
        id=uuid.uuid4(), company_id=company_id, doctype=d.key, prefix=d.prefix, suffix="",
        fiscal_infix=d.include_year, padding=d.padding, current_no=0,
    )
    db.add(series)
    if flush:
        db.flush()
    return series


def _head(series: models.NumberSeries, on: datetime.date) -> str:
    return f"{series.prefix}{f'{on.year}-' if series.fiscal_infix else ''}"


def format_id(series: models.NumberSeries, n: int, on: datetime.date) -> str:
    return f"{_head(series, on)}{str(n).zfill(series.padding)}{series.suffix}"


def _highest_issued(db: Session, company_id: uuid.UUID, d: DocType, series: models.NumberSeries,
                    on: datetime.date) -> int:
    """The highest number already used in the current format (and year)."""
    head, tail = _head(series, on), series.suffix or ""
    col = getattr(d.model, d.column)
    highest = 0
    with db.no_autoflush:
        rows = db.scalars(select(col).where(
            getattr(d.model, d.company_column) == company_id,
            func.lower(col).like(f"{head.lower()}%"),
        )).all()
    for code in rows:
        code = (code or "").strip()
        if not code.lower().startswith(head.lower()):
            continue
        middle = code[len(head):]
        if tail:
            if not middle.lower().endswith(tail.lower()):
                continue
            middle = middle[: len(middle) - len(tail)]
        if middle.isdigit():
            highest = max(highest, int(middle))
    return highest


def lock(db: Session, company_id: uuid.UUID, key: str) -> None:
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"document_numbers:{key}:{company_id}"))))


def next_ids(db: Session, company_id: uuid.UUID, key: str, count: int = 1,
             on: datetime.date | None = None, flush: bool = True) -> list[str]:
    """Issues the next [count] IDs of [key] for a company (takes the lock).
    [flush]=False when called while the session is already flushing."""
    d = doctype(key)
    on = on or datetime.date.today()
    lock(db, company_id, key)
    series = get_series(db, company_id, key, flush=flush)
    start = max(_highest_issued(db, company_id, d, series, on), series.current_no) + 1
    ids = [format_id(series, start + i, on) for i in range(count)]
    if not series.fiscal_infix:
        series.current_no = start + count - 1
    if flush:
        db.flush()
    return ids


def preview(db: Session, company_id: uuid.UUID, key: str, on: datetime.date | None = None) -> str:
    """The ID the next record would get now -- nothing is reserved."""
    d = doctype(key)
    on = on or datetime.date.today()
    series = get_series(db, company_id, key)
    return format_id(series, max(_highest_issued(db, company_id, d, series, on), series.current_no) + 1, on)


def update_format(db: Session, company_id: uuid.UUID, key: str, *, prefix: str | None = None,
                  suffix: str | None = None, padding: int | None = None, next_number: int | None = None,
                  include_year: bool | None = None) -> models.NumberSeries:
    """[next_number] is the lowest number the next record can get; IDs
    already in use are still skipped, so it can never create a duplicate."""
    lock(db, company_id, key)
    series = get_series(db, company_id, key)
    if prefix is not None:
        series.prefix = prefix
    if suffix is not None:
        series.suffix = suffix
    if padding is not None:
        series.padding = padding
    if include_year is not None:
        series.fiscal_infix = include_year
    if next_number is not None:
        series.current_no = max(next_number - 1, 0)
    db.flush()
    return series


def is_taken(db: Session, company_id: uuid.UUID, key: str, code: str) -> bool:
    d = doctype(key)
    normalized = (code or "").strip().lower()
    col = getattr(d.model, d.column)
    return bool(normalized) and db.scalar(select(getattr(d.model, "id")).where(
        getattr(d.model, d.company_column) == company_id,
        func.lower(func.trim(col)) == normalized).limit(1)) is not None
