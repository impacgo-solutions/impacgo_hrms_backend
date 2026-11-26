"""The company's weekly off days -- one rule for attendance, leave, payroll,
overtime / shifts and LOP.

core_company_settings.weekly_off_days holds the days chosen under
Administration > General Settings > Weekly Off Days (comma-separated
lowercase weekday names; '' = none). NULL means the company never chose:
they are derived from working_days_per_week exactly as before -- 6 or 7
working days = Sunday off, otherwise Saturday + Sunday.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy.orm import Session

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def legacy_off_days(working_days_per_week: int | None) -> list[str]:
    return ["sunday"] if (working_days_per_week or 5) >= 6 else ["saturday", "sunday"]


def parse(raw: str | None) -> list[str] | None:
    """Stored text -> weekday names in Monday..Sunday order; None if unset."""
    if raw is None:
        return None
    chosen = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return [d for d in WEEKDAYS if d in chosen]


def normalize(days) -> list[str]:
    """Validates a submitted list (case-insensitive, duplicates ignored);
    ValueError for an unknown day or all seven."""
    chosen = set()
    for day in days or []:
        name = str(day).strip().lower()
        if name not in WEEKDAYS:
            raise ValueError(f"'{day}' is not a day of the week.")
        chosen.add(name)
    if len(chosen) == len(WEEKDAYS):
        raise ValueError("At least one day of the week must be a working day.")
    return [d for d in WEEKDAYS if d in chosen]


def off_day_names(settings_row) -> list[str]:
    """The effective weekly off days for a CompanySettings row (or None)."""
    if settings_row is None:
        return legacy_off_days(None)
    chosen = parse(getattr(settings_row, "weekly_off_days", None))
    return chosen if chosen is not None else legacy_off_days(settings_row.working_days_per_week)


def off_weekdays(settings_row) -> frozenset[int]:
    """date.weekday() numbers (Monday = 0) of the weekly off days."""
    return frozenset(WEEKDAYS.index(d) for d in off_day_names(settings_row))


def company_off_weekdays(db: Session, company_id: uuid.UUID) -> frozenset[int]:
    from . import crud

    return off_weekdays(crud.get_company_settings(db, company_id))


def is_off(day: datetime.date, off: frozenset[int]) -> bool:
    return day.weekday() in off
