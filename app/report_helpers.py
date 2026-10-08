"""Shared helpers for backend/app/routers/reports.py -- date-bucketing for
trend charts, previous-period comparison windows, and the ReportChart
builder shapes every report endpoint reuses. Kept in one place so every
report's trend chart/comparison behaves identically instead of each
endpoint inventing its own bucketing logic.
"""
from __future__ import annotations

import datetime

from fastapi import HTTPException, Request

from . import schemas

# Query-parameter pairs report endpoints use for their period.
_RANGE_PARAM_PAIRS = (("from_date", "to_date"), ("date_from", "date_to"), ("start_date", "end_date"))


def check_date_range(from_date: datetime.date | None, to_date: datetime.date | None) -> None:
    """L-23: 422 for an inverted period (from after to)."""
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(status_code=422, detail="from_date must be on or before to_date.")


def reject_inverted_range(request: Request) -> None:
    """Router/route dependency (L-23): reads the period from the query
    string and rejects an inverted range with 422 before the report runs.
    Unparseable values are left to the endpoint's own validation."""
    for a, b in _RANGE_PARAM_PAIRS:
        raw_a, raw_b = request.query_params.get(a), request.query_params.get(b)
        if not raw_a or not raw_b:
            continue
        try:
            d_a = datetime.date.fromisoformat(raw_a[:10])
            d_b = datetime.date.fromisoformat(raw_b[:10])
        except ValueError:
            continue
        check_date_range(d_a, d_b)


def bucket_dates(
    from_date: datetime.date, to_date: datetime.date
) -> tuple[str, list[tuple[datetime.date, datetime.date, str]]]:
    """Splits [from_date, to_date] (inclusive) into daily/weekly/monthly
    buckets, picked automatically from the span length so a trend chart
    never renders 1 point (span too short for the naive choice) or 300
    points (span too long) -- <=31 days -> one point per day, <=120 days
    -> one point per real calendar week (Monday-Sunday, matching the exact
    same week boundary Timesheet.week_start uses -- see routers/work.py's
    `entry_date - timedelta(days=entry_date.weekday())`), otherwise one
    point per calendar month. Returns (granularity_used, buckets), each
    bucket a (start, end, label) tuple; a week/month bucket's (start, end)
    is clipped to [from_date, to_date] where it overhangs the requested
    range, but its label always names the FULL calendar week/month so it
    reads the same everywhere this range is viewed."""
    span_days = (to_date - from_date).days + 1
    buckets: list[tuple[datetime.date, datetime.date, str]] = []
    if span_days <= 31:
        d = from_date
        while d <= to_date:
            buckets.append((d, d, d.strftime("%d %b")))
            d += datetime.timedelta(days=1)
        return "daily", buckets
    if span_days <= 120:
        # Anchor to the Monday on/before from_date so every bucket is a
        # real Mon-Sun week, not an arbitrary 7-day chunk starting wherever
        # from_date happens to fall.
        d = from_date - datetime.timedelta(days=from_date.weekday())
        while d <= to_date:
            week_end = d + datetime.timedelta(days=6)
            label = (
                f"{d.strftime('%d %b')} - {week_end.strftime('%d %b')}"
                if d.month != week_end.month
                else f"{d.strftime('%d')} - {week_end.strftime('%d %b')}"
            )
            buckets.append((max(d, from_date), min(week_end, to_date), label))
            d = week_end + datetime.timedelta(days=1)
        return "weekly", buckets
    d = from_date.replace(day=1)
    while d <= to_date:
        next_month = (
            d.replace(year=d.year + 1, month=1, day=1)
            if d.month == 12
            else d.replace(month=d.month + 1, day=1)
        )
        end = min(next_month - datetime.timedelta(days=1), to_date)
        start = max(d, from_date)
        buckets.append((start, end, d.strftime("%b %Y")))
        d = next_month
    return "monthly", buckets


def bucket_daily_values(
    daily_values: dict[datetime.date, float],
    from_date: datetime.date,
    to_date: datetime.date,
) -> list[schemas.ReportChartPoint]:
    """[daily_values] is a sparse date->value map (e.g. from one GROUP BY
    date query) -- sums it into the daily/weekly/monthly buckets
    bucket_dates picks for this range, missing dates counted as 0."""
    _granularity, buckets = bucket_dates(from_date, to_date)
    points: list[schemas.ReportChartPoint] = []
    for start, end, label in buckets:
        total = 0.0
        d = start
        while d <= end:
            total += daily_values.get(d, 0.0)
            d += datetime.timedelta(days=1)
        points.append(schemas.ReportChartPoint(label=label, value=round(total, 2)))
    return points


def previous_period(
    from_date: datetime.date, to_date: datetime.date
) -> tuple[datetime.date, datetime.date]:
    """The immediately-preceding period of equal length -- e.g. for
    2026-09-01..2026-09-30 (30 days) returns 2026-08-02..2026-08-31."""
    span = (to_date - from_date).days + 1
    prev_to = from_date - datetime.timedelta(days=1)
    prev_from = prev_to - datetime.timedelta(days=span - 1)
    return prev_from, prev_to


def pct_delta_str(current: float, previous: float) -> str:
    """'+12.5%' / '-4.0%' / '—' (previous was 0 and current is also 0)."""
    if previous == 0:
        return "—" if current == 0 else "New"
    delta = (current - previous) / previous * 100
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.1f}%"


def build_comparison(
    current: dict[str, float], previous: dict[str, float]
) -> dict[str, str]:
    """Turns two same-keyed raw-numeric summaries into the
    ReportOut.comparison display dict: '<label>' -> '<prev value> (<+/-%>)'
    for every key present in [current]. Only meant for the 5-6 headline
    summary numbers each report already computes, not the full per-row
    breakdown."""
    out: dict[str, str] = {}
    for key, cur_val in current.items():
        prev_val = previous.get(key, 0.0)
        formatted_prev = f"{prev_val:.1f}" if prev_val != int(prev_val) else str(int(prev_val))
        out[key] = f"{formatted_prev} ({pct_delta_str(cur_val, prev_val)})"
    return out


def bar_chart(title: str, series_name: str, points: list[tuple[str, float]]) -> schemas.ReportChart:
    return schemas.ReportChart(
        chart_type="bar",
        title=title,
        series=[
            schemas.ReportChartSeries(
                name=series_name,
                points=[schemas.ReportChartPoint(label=label, value=value) for label, value in points],
            )
        ],
    )


def donut_chart(title: str, series_name: str, points: list[tuple[str, float]]) -> schemas.ReportChart:
    return schemas.ReportChart(
        chart_type="donut",
        title=title,
        series=[
            schemas.ReportChartSeries(
                name=series_name,
                points=[schemas.ReportChartPoint(label=label, value=value) for label, value in points],
            )
        ],
    )


def line_chart(
    title: str, series_name: str, points: list[schemas.ReportChartPoint]
) -> schemas.ReportChart:
    return schemas.ReportChart(
        chart_type="line",
        title=title,
        series=[schemas.ReportChartSeries(name=series_name, points=points)],
    )


def stacked_bar_chart(
    title: str, series: list[tuple[str, list[tuple[str, float]]]]
) -> schemas.ReportChart:
    """[series] is [(series_name, [(category_label, value), ...]), ...] --
    every series should share the same set/order of category labels."""
    return schemas.ReportChart(
        chart_type="stacked_bar",
        title=title,
        series=[
            schemas.ReportChartSeries(
                name=name,
                points=[schemas.ReportChartPoint(label=label, value=value) for label, value in pts],
            )
            for name, pts in series
        ],
    )
