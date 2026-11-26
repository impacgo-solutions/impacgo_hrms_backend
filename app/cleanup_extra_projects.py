"""Remove the 14 projects that aren't in project_report_updated_employee.xlsx
from the 'impacgo-solutions' tenant (archived by sync_projects_from_excel),
after moving their work entries to the sheet's projects.

Decisions from the review of extra_projects_not_in_excel_review.xlsx
(2 Nov 2026):
  * PROJECT_MOVES: every work entry of the project moves to that project.
  * Impacgo Solutions Internal Project: each entry moves by what it is
    about (INTERNAL_MOVES, by entry id prefix).
  * Frappe HR, Meetings Update: removed together with their work entries
    (the affected timesheets' totals are recomputed).
  * Surya (EMP117) joins App Development — Ironing Boy's team (his
    Tailscale entries move there).
  * Then all 14 projects are deleted.

Dry run by default (rolled back); --execute commits in one transaction
after writing a JSON backup of every row it moves or deletes.

Usage (from backend/):
    venv\\Scripts\\python -m app.cleanup_extra_projects
    venv\\Scripts\\python -m app.cleanup_extra_projects --execute
"""

import argparse
import collections
import datetime
import json
import re
import uuid
from pathlib import Path

from sqlalchemy import select, text

from . import crud, models
from .database import SessionLocal, set_session_tenant_slug, set_tenant_context

TENANT_SLUG = "impacgo-solutions"
COMPANY_ID = uuid.UUID("4fd9da4f-397f-4fbd-be50-e1245b5226cf")
BACKUP_DIR = Path(__file__).resolve().parent.parent / "db_backups"

PROJECT_MOVES = {
    "Work Track": "Work Task Application",
    "Rosti India Pvt Ltd (ROSTI)": "D365 F&O Support Rosti",
    "Dairy Farm Management System (DFMS)": "CALVIQ — Dairy Farm Management",
    "IMPACGO PEOPLE": "Impacgo HRMS",
    "Imax D365 - Go-Live support (Urgent)": "IMAX D365FO India Rollout",
}
REMOVE_WITH_ENTRIES = ("Frappe HR", "Meetings Update")
EMPTY = (
    "Construction Planner", "D365 Development & AMS — Perfect Model Solutions",
    "D365 Development — Daxonet", "D365 F&O Implementation — Hitachi",
    "D365 F&O Implementation — Resonac", "D365 Platform Support — ETOR",
)
INTERNAL = "Impacgo Solutions Internal Project"
WEBSITE, ERP, ROUTEMATIC, MAKE, WORKTASK, IRONING = (
    "Impacgo Website", "Impacgo ERP", "Routematic Application", "Impacgo Make",
    "Work Task Application", "App Development — Ironing Boy")
INTERNAL_MOVES = {
    # Surya -- Tailscale VPN setup (Dec 2025)
    "7241a6fa": IRONING, "850e7e39": IRONING, "f750ea36": IRONING, "8da87d1a": IRONING, "89dfc937": IRONING,
    # Bhushan -- "works on work track"
    "ab9ddb3e": WORKTASK,
    # Bhushan -- Project Handbook & repository documentation (ERP frontend)
    "b2182156": ERP, "300d8ad3": ERP, "e4154f27": ERP, "cc2b08bd": ERP, "d55327c2": ERP,
    "1251d070": ERP, "18fdff90": ERP, "1cedb597": ERP, "a02288cd": ERP,
    # Surya -- admin website bugs; Bhushan -- website, blog, GitHub, LinkedIn,
    # logo, brochure, UI Style website and its chatbot
    "ffcf655e": WEBSITE, "c2314fa5": WEBSITE, "83762fb5": WEBSITE, "9797bb32": WEBSITE, "af7c7ded": WEBSITE,
    "c66b7676": WEBSITE, "13a19699": WEBSITE, "a4d498ff": WEBSITE, "725c113b": WEBSITE, "aed23583": WEBSITE,
    "f4eb66c6": WEBSITE, "784c4de4": WEBSITE, "014df3da": WEBSITE, "f1ddd0d7": WEBSITE,
    # Manisha -- fleet management planning / Routematic catalogue
    "da3ed995": ROUTEMATIC, "d242021d": ROUTEMATIC, "aacdbbd6": ROUTEMATIC, "325b9683": ROUTEMATIC,
    # Manisha -- Production Planning / MRP and Make integration
    "73e332b0": MAKE, "bc432eaf": MAKE,
}
ADD_TO_TEAM = {IRONING: ("EMP117",)}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


class _Lookup(dict):
    """{project name: Project}, looked up by norm(name)."""

    def __init__(self, by_norm):
        super().__init__()
        self._by_norm = by_norm

    def __contains__(self, name):
        return norm(name) in self._by_norm

    def __getitem__(self, name):
        return self._by_norm[norm(name)]


def entry_row(e: models.WorkEntry) -> dict:
    return {c.name: (str(v) if v is not None else None) for c in e.__table__.columns
            for v in [getattr(e, c.key)]}


def run(execute: bool) -> None:
    set_tenant_context(TENANT_SLUG)
    db = SessionLocal()
    db.execute(text(f'SET search_path TO "{TENANT_SLUG}", public'))
    set_session_tenant_slug(db, TENANT_SLUG)
    report = collections.defaultdict(list)
    backup = {"moved_entries": [], "deleted_entries": [], "deleted_projects": [], "team_added": []}
    try:
        # Matched ignoring punctuation (names use different dash characters).
        by_norm = {norm(p.name): p for p in db.scalars(
            select(models.Project).where(models.Project.company_id == COMPANY_ID))}
        projects = _Lookup(by_norm)
        extra = [*PROJECT_MOVES, *REMOVE_WITH_ENTRIES, *EMPTY, INTERNAL]
        missing = [n for n in [*extra, *PROJECT_MOVES.values(), *INTERNAL_MOVES.values()] if n not in projects]
        if missing:
            raise RuntimeError(f"Projects not found: {sorted(set(missing))}")
        for n in extra:
            if projects[n].is_active:
                raise RuntimeError(f"{n} is still active -- expected archived")
        targets = set(PROJECT_MOVES.values()) | set(INTERNAL_MOVES.values())
        for n in targets:
            if not projects[n].is_active:
                raise RuntimeError(f"target {n} is archived")

        def entries_of(name):
            return list(db.scalars(select(models.WorkEntry).where(models.WorkEntry.project_id == projects[name].id)))

        def move(e, target):
            backup["moved_entries"].append({**entry_row(e), "moved_to_project_id": str(projects[target].id)})
            e.project_id = projects[target].id
            e.task_id = None  # a task of the old project doesn't belong to the new one

        moved = collections.Counter()
        for src, dst in PROJECT_MOVES.items():
            for e in entries_of(src):
                move(e, dst)
                moved[(src, dst)] += float(e.hours)
        internal = entries_of(INTERNAL)
        prefixes = {str(e.id)[:8]: e for e in internal}
        if set(prefixes) != set(INTERNAL_MOVES) or len(prefixes) != len(internal):
            raise RuntimeError(f"{INTERNAL} entries changed since review: "
                               f"unmapped {sorted(set(prefixes) - set(INTERNAL_MOVES))}, "
                               f"gone {sorted(set(INTERNAL_MOVES) - set(prefixes))}")
        for pfx, e in prefixes.items():
            move(e, INTERNAL_MOVES[pfx])
            moved[(INTERNAL, INTERNAL_MOVES[pfx])] += float(e.hours)
        for (src, dst), h in moved.items():
            report["work entries moved"].append(f"{src} -> {dst}: {h:g} h")
        db.flush()

        # Removed together with their entries; resync those timesheets.
        weeks = set()
        for name in REMOVE_WITH_ENTRIES:
            hours = 0.0
            for e in entries_of(name):
                ts = db.get(models.Timesheet, e.timesheet_id) if e.timesheet_id else None
                if ts is not None:
                    weeks.add((ts.employee_id, ts.week_start))
                backup["deleted_entries"].append(entry_row(e))
                hours += float(e.hours)
                db.delete(e)
            report["work entries deleted"].append(f"{name}: {hours:g} h")
        db.flush()
        for emp_id, week in weeks:
            crud.resync_timesheet_hours(db, emp_id, week)
            report["timesheets recomputed"].append(f"{emp_id} week {week}")

        # Team additions.
        today = crud.company_today(db, COMPANY_ID)
        for proj, codes in ADD_TO_TEAM.items():
            for code in codes:
                emp = db.scalar(select(models.Employee).where(models.Employee.company_id == COMPANY_ID,
                                                              models.Employee.employee_code == code))
                had = db.scalar(select(models.ProjectAllocation.id).where(
                    models.ProjectAllocation.project_id == projects[proj].id,
                    models.ProjectAllocation.employee_id == emp.id,
                    models.ProjectAllocation.is_active.is_(True)))
                if had is None:
                    a = crud.create_project_allocation(db, projects[proj].id, emp.id, 100, start_date=today)
                    backup["team_added"].append(str(a.id))
                    report["team added"].append(f"{proj}: {emp.first_name} {emp.last_name or ''}".strip())

        # Delete the 14 projects (nothing may still point at them).
        for name in extra:
            p = projects[name]
            left = db.scalar(select(models.WorkEntry.id).where(models.WorkEntry.project_id == p.id).limit(1))
            if left is not None:
                raise RuntimeError(f"{name} still has work entries")
            for table in ("pm_resource_allocations", "pm_project_budgets", "pm_project_status_history",
                          "pm_tasks", "pm_project_documents"):
                exists = db.execute(text("select to_regclass(:t)"), {"t": table}).scalar()
                if exists:
                    n = db.execute(text(f"select count(*) from {table} where project_id = :p"), {"p": p.id}).scalar()
                    if n:
                        rows = [dict(r._mapping) for r in db.execute(
                            text(f"select * from {table} where project_id = :p"), {"p": p.id})]
                        backup.setdefault(table, []).extend({k: str(v) if v is not None else None for k, v in r.items()}
                                                            for r in rows)
                        db.execute(text(f"delete from {table} where project_id = :p"), {"p": p.id})
            backup["deleted_projects"].append({c.name: (str(getattr(p, c.key)) if getattr(p, c.key) is not None else None)
                                               for c in p.__table__.columns})
            db.delete(p)
            report["projects deleted"].append(name)
        db.flush()
        crud.create_audit_log(db, COMPANY_ID, None, "delete", "project", None, changes={
            "source": "cleanup_extra_projects", "projects": len(extra),
            "entries_moved": len(backup["moved_entries"]), "entries_deleted": len(backup["deleted_entries"])})

        if execute:
            BACKUP_DIR.mkdir(exist_ok=True)
            out = BACKUP_DIR / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_extra_projects_cleanup_backup.json"
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
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--execute", action="store_true")
    run(p.parse_args().execute)
