"""L-24: server-side data for POST /api/reports/export/pdf.

The PDF renders only what the server itself produces for the caller: the
request names a report (`report_type`) and the query parameters the screen
used (`params`); the report's own handler is re-run here with the caller's
identity -- so its permission checks, reporting scope (team / org), payroll
gate (H-15) and row visibility apply unchanged -- and its columns / rows /
summary / charts are what gets printed. Client-supplied rows are ignored.
An optional `drill_down` (column + exact value) narrows the server rows the
same way the screen's chart drill-down does.
"""

from __future__ import annotations

import datetime
import inspect
import typing

from fastapi import HTTPException
from fastapi.params import Depends as DependsParam
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from . import crud, models, report_helpers as rh

# report_type -> GET handler in routers/reports.py returning schemas.ReportOut
_REPORT_OUT_TYPES = {
    "attendance": "attendance_report",
    "timesheet": "timesheet_report",
    "payroll": "payroll_report",
    "project": "project_report",
    "performance": "performance_report",
    "utilization": "utilization_report",
    "recruitment": "recruitment_report",
    "leave": "leave_report",
    "regularization": "regularization_report",
    "work-entry": "work_entry_report",
    "work_entry": "work_entry_report",
    "people": "people_report",
}
JSON_REPORT_TYPES = ("compliance", "overtime", "leave-withdrawals")
REPORT_TYPES = tuple(sorted(set(_REPORT_OUT_TYPES) | set(JSON_REPORT_TYPES)))


def _call_handler(fn, db: Session, user: models.User, params: dict[str, str]):
    """Calls a FastAPI GET handler directly: query params are parsed with the
    handler's own annotations (bad value -> 422), dependencies the reports
    use are resolved for [user]."""
    from . import deps

    hints = typing.get_type_hints(fn)
    kwargs = {}
    for name, p in inspect.signature(fn).parameters.items():
        if name == "db":
            kwargs[name] = db
        elif name == "current_user":
            kwargs[name] = user
        elif name == "scope":
            kwargs[name] = deps.get_reports_scope(user=user, db=db)
        elif name == "payroll_scope":
            kwargs[name] = deps.get_payroll_scope(user=user, db=db)
        elif isinstance(p.default, DependsParam):
            raise HTTPException(status_code=500, detail="Report export is not available for this report.")
        else:
            default = p.default
            if hasattr(default, "default"):  # Query(...) FieldInfo
                default = default.default
            raw = params.get(name)
            if raw is None or raw == "":
                if default is inspect.Parameter.empty or default is ...:
                    raise HTTPException(status_code=422, detail=f"Missing report parameter: {name}")
                kwargs[name] = default
                continue
            try:
                kwargs[name] = TypeAdapter(hints.get(name, str)).validate_python(raw)
            except ValidationError:
                raise HTTPException(status_code=422, detail=f"Invalid report parameter: {name}")
            pattern = None
            for meta in getattr(p.default, "metadata", []) or []:
                pattern = getattr(meta, "pattern", None) or pattern
            if pattern:
                import re

                if not re.match(pattern, str(raw)):
                    raise HTTPException(status_code=422, detail=f"Invalid report parameter: {name}")
    return fn(**kwargs)


# ── formatting for the three JSON (non-ReportOut) reports ───────────────────

def _cap_words(s: str | None) -> str:
    if not s:
        return "—"
    return " ".join(w[:1].upper() + w[1:] for w in s.replace("_", " ").split(" "))


def _hours(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    return f"{int(v) if v == int(v) else v} h"


def _inr(v) -> str:
    from .routers.reports import _fmt_inr

    return _fmt_inr(float(v or 0))


class _Fmt:
    def __init__(self, tz: datetime.tzinfo):
        self.tz = tz

    def dt(self, v) -> str:
        if not v:
            return "—"
        if isinstance(v, str):
            v = datetime.datetime.fromisoformat(v)
        if v.tzinfo is not None:
            v = v.astimezone(self.tz)
        return v.strftime("%d %b %Y, %I:%M %p")

    def time(self, v) -> str:
        if not v:
            return "—"
        if v.tzinfo is not None:
            v = v.astimezone(self.tz)
        return v.strftime("%I:%M %p")


def _compliance(out, f: _Fmt):
    cols = ["Employee", "Employee ID", "Department", "Reporting Manager", "Date", "Exception",
            "Expected", "Actual", "Regularization", "Notification", "Status", "Resolution", "Details"]
    rows = []
    for r in out.rows:
        expected = f"{r.expected} ({f.dt(r.expected_at)})" if r.expected_at else (r.expected or "—")
        at = r.manager_notified_at or r.employee_notified_at
        notified = (r.notification_status or "—") + (f" · {f.dt(at)}" if at else "")
        resolution = f"{r.resolution or 'Resolved'} · {f.dt(r.resolved_at)}" if r.status == "resolved" else "Open"
        rows.append([r.employee_name, r.employee_code or "", r.department or "", r.reporting_manager_name or "—",
                     r.exception_date.isoformat(), r.exception_label, expected, f.dt(r.actual_at),
                     _cap_words(r.regularization_status), notified, _cap_words(r.status), resolution, r.details or ""])
    summary = {"Exceptions": str(out.total), "Open": str(out.open), "Resolved": str(out.resolved)}
    return cols, rows, summary


def _leave_withdrawals(out: dict, f: _Fmt):
    cols = ["Employee", "Employee ID", "Department", "Reporting Manager", "Leave Type", "Leave Dates",
            "Days", "Requested On", "Reason", "Status", "Approver", "Decided On", "Decision Notes",
            "Restored Balance", "Leave Now", "Withdrawal Attempts"]
    rows = []
    for r in out["rows"]:
        dates = r["from_date"] if r["from_date"] == r["to_date"] else f"{r['from_date']} – {r['to_date']}"
        rows.append([r["employee_name"], r.get("employee_code") or "", r.get("department") or "",
                     r.get("reporting_manager_name") or "—", r["leave_type"], dates, f"{r['days']}",
                     f.dt(r.get("requested_at")), r.get("reason") or "", _cap_words(r.get("status")),
                     r.get("approver_name") or "—", f.dt(r.get("decided_at")), r.get("decision_notes") or "—",
                     "—" if r.get("restored_days") is None else f"{r['restored_days']} day(s)",
                     _cap_words(r.get("leave_status_now")), f"{r.get('attempt_count', '')}"])
    summary = {"Requests": str(out["total"]), "Pending": str(out["pending"]), "Approved": str(out["approved"]),
               "Rejected": str(out["rejected"]), "Restored": f"{out['restored_days']} day(s)"}
    return cols, rows, summary


def _overtime(out, f: _Fmt):
    cols = ["Employee", "Employee ID", "Department", "Reporting Manager", "Date", "Day Type",
            "Requested Hours", "Approved Hours", "Scheduled Start–End", "Actual Clock In–Out", "Worked",
            "Missed Punches", "Session", "Status", "Approval", "Compensation"]
    day_types = {"holiday": "Holiday", "weekly_off": "Weekly off", "working": "Working day"}
    rows = []
    for r in out.rows:
        if r.status == "pending" or r.decided_at is None:
            approval = "—"
        else:
            notes = (r.decision_notes or "").strip()
            approval = f"{r.approver_name} · {f.dt(r.decided_at)}" + (f' · "{notes}"' if notes else "")
        if r.compensation_status is None:
            comp = "—"
        elif r.compensation_status == "skipped":
            comp = "Not compensated"
        elif r.compensation_mode == "comp_off":
            d = float(r.leave_days or 0)
            comp = f"{int(d) if d == int(d) else d} day(s) comp-off"
        else:
            comp = f"{_inr(r.amount)} · " + (
                (r.payroll_period or "payroll") if r.compensation_status == "in_payroll" else "next payroll")
        rows.append([
            r.employee_name, r.employee_code or "", r.department or "", r.reporting_manager_name or "—",
            r.work_date.isoformat(), day_types.get(r.day_type or "", "—"), _hours(r.requested_hours),
            _hours(r.approved_hours),
            "—" if r.planned_start is None else f"{f.time(r.planned_start)} – {f.time(r.planned_end)}",
            "—" if r.actual_start is None else f"{f.time(r.actual_start)} – {f.time(r.actual_end)}",
            r.actual_duration or "—", r.missed_punches or "—", _cap_words(r.session_status),
            _cap_words(r.status), approval, comp,
        ])
    summary = {
        "Requests": str(out.total_requests), "Approved": str(out.approved_requests),
        "Requested hours": _hours(out.total_requested_hours), "Approved hours": _hours(out.total_approved_hours),
        "Hours worked": out.total_worked, "Missed clock-ins": str(out.missed_clock_ins),
        "Missed clock-outs": str(out.missed_clock_outs), "Overtime pay": _inr(out.total_amount),
        "Comp-off credited": f"{out.total_leave_days} day(s)",
    }
    return cols, rows, summary


def build_report_data(
    db: Session, user: models.User, report_type: str, params: dict[str, str],
    drill_down: tuple[str, str] | None = None,
) -> dict:
    """{columns, rows, summary: [{label, value}], charts} -- server data only."""
    params = {k: str(v) for k, v in (params or {}).items() if v is not None}
    rh.check_date_range(_date(params.get("from_date")), _date(params.get("to_date")))
    charts: list[dict] = []
    if report_type in _REPORT_OUT_TYPES:
        from .routers import reports as reports_router

        out = _call_handler(getattr(reports_router, _REPORT_OUT_TYPES[report_type]), db, user, params)
        columns, rows = list(out.columns), [list(r) for r in out.rows]
        summary = dict(out.summary or {})
        charts = [c.model_dump(mode="json") for c in (out.charts or [])]
    else:
        f = _Fmt(crud.company_tzinfo(db, user.company_id))
        if report_type == "compliance":
            from .routers import compliance as compliance_router

            columns, rows, summary = _compliance(
                _call_handler(compliance_router.compliance_report, db, user, params), f)
        elif report_type == "overtime":
            from .routers import overtime_settings

            columns, rows, summary = _overtime(
                _call_handler(overtime_settings.overtime_report, db, user, params), f)
        elif report_type == "leave-withdrawals":
            from .routers import leave_withdrawals

            columns, rows, summary = _leave_withdrawals(
                _call_handler(leave_withdrawals.leave_withdrawal_report, db, user, params), f)
        else:
            raise HTTPException(status_code=422, detail="Unknown report type.")
    if drill_down is not None:
        column, value = drill_down
        if column in columns:  # same as the screen: unknown column = no drill-down
            idx = columns.index(column)
            rows = [r for r in rows if idx < len(r) and r[idx] == value]
    return {
        "columns": columns,
        "rows": rows,
        "summary": [{"label": k, "value": str(v)} for k, v in summary.items()],
        "charts": charts,
    }


def _date(v: str | None) -> datetime.date | None:
    if not v:
        return None
    try:
        return datetime.date.fromisoformat(v[:10])
    except ValueError:
        return None

