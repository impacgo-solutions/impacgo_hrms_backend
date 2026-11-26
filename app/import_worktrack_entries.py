"""Replace every work entry in the 'impacgo-solutions' tenant with the
WorkTrack history exported to HRMS/LISTS/<Name>_Work_Entries.xlsx (one
file per employee; All_Work_Entries.xlsx is only used as a cross-check).

Decisions agreed with the team (15 Oct 2026):
  * ALL existing work entries are removed first (Soumya's included), after a
    JSON backup of the entries, their WorkTrack meta and their timesheets.
  * Imported as history: the "allocated to the project on that day" rule
    (work_rules.allocation_error) is NOT applied -- the entries were logged
    in WorkTrack long before HRMS allocations existed -- and no allocation
    is created or changed. Every other HRMS rule still applies: hours > 0
    and <= 24, at most 24 h a day, no identical entry, no future date
    (this import's own rule for historical data).
  * Entries are stored exactly as WorkTrack's own insert does
    (routers/worktrack.py::_insert_entry): category "General", no start /
    end time (WorkTrack's fixed 09:00-18:00 is a placeholder), task_name =
    description or project name, HRMS status "approved", and the WorkTrack
    status / location / remarks / submitted-at in pm_wt_entry_meta. The
    original created-at time is kept.
  * Spreadsheet project names are mapped to the project master (PROJECT_MAP);
    names with no clear match become new projects, as WorkTrack's
    "Other (add new project)" does -- existing projects are never modified.

Idempotent: re-running deletes what the previous run imported and imports
again. Dry run by default (rolled back); --execute commits in ONE
transaction.

Usage (from backend/):
    venv\\Scripts\\python -m app.import_worktrack_entries                 # dry run
    venv\\Scripts\\python -m app.import_worktrack_entries --execute
"""

import argparse
import collections
import datetime
import json
import re
import uuid
from pathlib import Path

import openpyxl
from sqlalchemy import func, select, text
from sqlalchemy import inspect as sa_inspect

from . import crud, models, work_rules
from .database import SessionLocal, set_session_tenant_slug, set_tenant_context

TENANT_SLUG = "impacgo-solutions"
COMPANY_ID = uuid.UUID("4fd9da4f-397f-4fbd-be50-e1245b5226cf")
DEFAULT_SOURCE = Path(__file__).resolve().parents[2] / "LISTS"
BACKUP_DIR = Path(__file__).resolve().parent.parent / "db_backups"
SOURCE_TAG = "import_worktrack_entries"

# Spreadsheet "Employee" -> HRMS login / work email.
EMPLOYEE_EMAIL = {
    "aditya": "adityaram@impacgo.com", "bhushan": "bhushan@impacgo.com", "chandra": "chandra@impacgo.com",
    "manisha": "manisha@impacgo.com", "mayank": "mayank@impacgo.com", "mounika": "mounika@impacgo.com",
    "naga sai": "nagasai@impacgo.com", "naveen": "naveen@impacgo.com", "srikar": "srikar@impacgo.com",
    "surya": "surya@impacgo.com",
}

# Spreadsheet project name -> existing project name (exact), or None = create
# a new project with the spreadsheet name. Names not listed must match an
# existing project exactly (case-insensitive) or the run stops.
PROJECT_MAP = {
    "lamax Precision India Pvt Ltd (lMAX)": "Imax Precision India Pvt Ltd (IMAX)",
    "Iamax D365 - Go-Live support (Urgent)": "Imax D365 - Go-Live support (Urgent)",
    "Resonac Materials Malaysia Sdn Bhd (RMM)": "Resonac Materials Malaysia Sdn Bhd",
    "RSGS - REAP SYSTEM INTEGRATION PROJECT": "RSGS - REAP System Integration",
    "REAP SYSTEM INTEGRATION PROJECT": "RSGS - REAP System Integration",
    "Impacgo Solutions Internal Project Pvt Ltd (IPG)": "Impacgo Solutions Internal Project",
    "Inventory Application(StockLyte)": "StockLyte – Inventory Management",
    "CalvIQ": "CALVIQ – Dairy Farm Management",
    "FarmIQ": "FarmYieldIQ",
    "Impacgo Chain ERP": "Impacgo Chain",
    # No clear match -> new projects.
    "Rosti India Pvt Ltd (ROSTI)": None,
    "Rosti Einvoice integration": None,
    "REC Global reports": None,
    "RESONAC PMO GLOBAL SUPPORT": None,
    "Dairy Farm Management System (DFMS)": None,
    "IMPACGO PEOPLE": None,
}

COLUMNS = ("Employee", "Entry Date", "Project Name", "Description", "Status", "Location", "Start Time",
           "End Time", "Hours Worked", "Billable", "Remarks", "Submitted At (UTC)", "Created At (UTC)",
           "Updated At (UTC)")


def read_rows(path: Path) -> list[dict]:
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    header = tuple(rows[0][:len(COLUMNS)])
    if header != COLUMNS:
        raise RuntimeError(f"{path.name}: unexpected columns {header}")
    return [dict(zip(COLUMNS, r)) for r in rows[1:] if any(v is not None for v in r)]


def as_date(v) -> datetime.date | None:
    return v.date() if isinstance(v, datetime.datetime) else v if isinstance(v, datetime.date) else None


def as_utc(v) -> datetime.datetime | None:
    return v.replace(tzinfo=datetime.timezone.utc) if isinstance(v, datetime.datetime) else None


def text_or_none(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def unique_project_code(db, name: str) -> str:
    """Same rule as routers/worktrack.py::_unique_project_code."""
    base = re.sub(r"[^A-Z0-9]", "", name.upper())[:10] or "PRJ"
    taken = set(db.scalars(select(models.Project.code).where(
        models.Project.company_id == COMPANY_ID, models.Project.code.like(f"{base}%"))))
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def row_dict(obj) -> dict:
    return {a.columns[0].name: (v.isoformat() if hasattr(v, "isoformat") else str(v) if v is not None else None)
            for a in sa_inspect(obj).mapper.column_attrs for v in [getattr(obj, a.key)]}


def run(source: Path, execute: bool) -> None:
    # ── read + cross-check the spreadsheets ──
    files = sorted(p for p in source.glob("*_Work_Entries.xlsx") if not p.name.startswith("All_"))
    rows = [dict(r, _file=p.name) for p in files for r in read_rows(p)]
    all_file = source / "All_Work_Entries.xlsx"
    if all_file.exists():
        key = lambda r: tuple(str(r[c]) for c in COLUMNS)
        if collections.Counter(map(key, rows)) != collections.Counter(map(key, read_rows(all_file))):
            raise RuntimeError("Per-employee files and All_Work_Entries.xlsx disagree -- refusing to guess.")

    set_tenant_context(TENANT_SLUG)
    db = SessionLocal()
    db.execute(text(f'SET search_path TO "{TENANT_SLUG}", public'))
    set_session_tenant_slug(db, TENANT_SLUG)
    report = collections.defaultdict(list)
    try:
        today = crud.company_today(db, COMPANY_ID)

        # ── employees ──
        employees = {}
        for name in sorted({str(r["Employee"]).strip() for r in rows}):
            email = EMPLOYEE_EMAIL.get(name.lower())
            emp = email and db.scalar(select(models.Employee).where(
                models.Employee.company_id == COMPANY_ID, func.lower(models.Employee.work_email) == email))
            if not emp:
                raise RuntimeError(f"No HRMS employee for spreadsheet name '{name}'")
            employees[name.lower()] = emp

        # ── projects ──
        by_name = {p.name.casefold(): p for p in db.scalars(
            select(models.Project).where(models.Project.company_id == COMPANY_ID))}
        first_day = collections.defaultdict(lambda: datetime.date.max)
        billable = collections.defaultdict(bool)
        for r in rows:
            src = str(r["Project Name"]).strip()
            first_day[src] = min(first_day[src], as_date(r["Entry Date"]))
            billable[src] |= str(r["Billable"]).strip().lower() == "yes"
        projects = {}
        for src in sorted(first_day):
            target = PROJECT_MAP.get(src, src)
            if target is None:
                project = by_name.get(src.casefold())
                if project is None:
                    project = crud.create_project(
                        db, COMPANY_ID, src, "active", planned_start_date=first_day[src],
                        is_billable=billable[src], code=unique_project_code(db, src))
                    crud.create_audit_log(db, COMPANY_ID, None, "create", "project", project.id,
                                          changes={"source": SOURCE_TAG})
                    by_name[src.casefold()] = project
                    report["projects created"].append(f"{src} ({project.code}), start {first_day[src]}")
            else:
                project = by_name.get(target.casefold())
                if project is None:
                    raise RuntimeError(f"Project '{target}' (for '{src}') is not in the project master")
                if src != target:
                    report["projects mapped"].append(f"{src} -> {project.name} ({project.code})")
            projects[src] = project

        # ── backup + delete every existing work entry ──
        entries = list(db.scalars(select(models.WorkEntry).join(
            models.Timesheet, models.Timesheet.id == models.WorkEntry.timesheet_id)
            .where(models.Timesheet.company_id == COMPANY_ID)))
        sheets = list(db.scalars(select(models.Timesheet).where(models.Timesheet.company_id == COMPANY_ID)))
        ids = [str(e.id) for e in entries]
        meta = [dict(m) for m in db.execute(text("select * from pm_wt_entry_meta where entry_id = any(cast(:ids as uuid[]))"),
                                            {"ids": ids}).mappings()]
        backup = {"taken_at": datetime.datetime.now().isoformat(), "tenant": TENANT_SLUG,
                  "pm_time_entries": [row_dict(e) for e in entries], "pm_wt_entry_meta":
                  [{k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if v is not None else None)
                    for k, v in m.items()} for m in meta],
                  "pm_timesheets": [row_dict(t) for t in sheets]}
        emp_names = {e.id: e.first_name for e in db.scalars(select(models.Employee).where(
            models.Employee.company_id == COMPANY_ID))}
        week_emp = {t.id: t.employee_id for t in sheets}
        for name, n in sorted(collections.Counter(emp_names.get(week_emp[e.timesheet_id]) for e in entries).items()):
            report["existing entries deleted"].append(f"{name}: {n}")
        db.execute(text("delete from pm_wt_entry_meta where entry_id = any(cast(:ids as uuid[]))"), {"ids": ids})
        for e in entries:
            crud.create_audit_log(db, COMPANY_ID, None, "delete", "work_entry", e.id, changes={"source": SOURCE_TAG})
            db.delete(e)
        db.flush()

        # ── insert ──
        week_cache: dict[tuple, models.Timesheet] = {}
        touched: set[tuple] = set()
        inserted = collections.Counter()
        rows.sort(key=lambda r: (str(r["Employee"]).lower(), as_date(r["Entry Date"]), r["Created At (UTC)"] or datetime.datetime.min))
        for r in rows:
            emp = employees[str(r["Employee"]).strip().lower()]
            day = as_date(r["Entry Date"])
            src = str(r["Project Name"]).strip()
            project = projects[src]
            label = f"{emp.first_name} {day} {src} ({r['Hours Worked']} h)"
            try:
                hours = float(r["Hours Worked"])
            except (TypeError, ValueError):
                hours = 0.0
            description = text_or_none(r["Description"])
            task_name = (description or project.name)[:200]
            problem = None
            if day is None:
                problem = "no valid entry date"
            elif day > today:
                problem = "future date"
            elif hours <= 0 or hours > 24:
                problem = "hours must be more than 0 and at most 24"
            else:
                problem = work_rules.entry_error(db, emp.id, day, project_id=project.id, task_id=None,
                                                 task_name=task_name, start_time=None, end_time=None,
                                                 hours=hours)
            if problem:
                report["entries skipped"].append(f"{label}: {problem}")
                continue
            week = day - datetime.timedelta(days=day.weekday())
            ts = week_cache.get((emp.id, week))
            if ts is None:
                ts = db.scalar(select(models.Timesheet).where(models.Timesheet.employee_id == emp.id,
                                                              models.Timesheet.week_start == week))
                if ts is None:
                    ts = crud.create_timesheet(db, COMPANY_ID, emp.id, week, 0.0, 0.0, status="draft")
                week_cache[(emp.id, week)] = ts
            entry = crud.create_work_entry(
                db, ts.id, project.id, day, category="General", start_time=None, end_time=None, hours=hours,
                description=description, is_billable=str(r["Billable"]).strip().lower() == "yes",
                task_name=task_name)
            entry.status = "rejected" if str(r["Status"]).strip().lower() == "rejected" else "approved"
            if as_utc(r["Created At (UTC)"]):
                entry.created_at = as_utc(r["Created At (UTC)"])
            db.flush()
            db.execute(text(
                "insert into pm_wt_entry_meta (entry_id, location, remarks, wt_status, submitted_at) "
                "values (:eid, :loc, :rem, :st, :sub) on conflict (entry_id) do update set "
                "location = excluded.location, remarks = excluded.remarks, wt_status = excluded.wt_status, "
                "submitted_at = excluded.submitted_at, updated_at = now()"),
                {"eid": str(entry.id), "loc": text_or_none(r["Location"]), "rem": text_or_none(r["Remarks"]),
                 "st": text_or_none(r["Status"]), "sub": as_utc(r["Submitted At (UTC)"])})
            crud.create_audit_log(db, COMPANY_ID, None, "create", "work_entry", entry.id,
                                  changes={"source": SOURCE_TAG, "file": r["_file"]})
            touched.add((emp.id, week))
            inserted[emp.first_name] += 1

        # ── timesheet totals; drop draft weeks left with no entries ──
        for emp_id, week in touched:
            crud.resync_timesheet_hours(db, emp_id, week)
        emptied = 0
        for t in db.scalars(select(models.Timesheet).where(models.Timesheet.company_id == COMPANY_ID,
                                                           models.Timesheet.status == "draft")):
            if not db.scalar(select(func.count()).select_from(models.WorkEntry)
                             .where(models.WorkEntry.timesheet_id == t.id)):
                db.delete(t)
                emptied += 1
        db.flush()
        report["entries inserted"] = [f"{k}: {v}" for k, v in sorted(inserted.items())] + [f"TOTAL {sum(inserted.values())} of {len(rows)} rows"]
        report["empty draft timesheets removed"] = [str(emptied)]

        if execute:
            BACKUP_DIR.mkdir(exist_ok=True)
            out = BACKUP_DIR / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_work_entries_backup.json"
            out.write_text(json.dumps(backup, indent=2), encoding="utf-8")
            db.commit()
            report["backup"] = [str(out)]
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("=" * 78)
    print(("EXECUTED (committed)" if execute else "DRY RUN (rolled back)") + f" -- tenant '{TENANT_SLUG}'")
    print("=" * 78)
    for section, items in report.items():
        print(f"\n[{section}] {len(items)}")
        for item in items:
            print(f"  - {item}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="folder with the *_Work_Entries.xlsx files")
    parser.add_argument("--execute", action="store_true", help="commit (default: dry run, rolled back)")
    args = parser.parse_args()
    run(args.source, args.execute)
