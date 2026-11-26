"""Sync the 'impacgo-solutions' tenant's projects with HRMS/
project_report_updated_employee.xlsx (the Projects report export, edited).

Decisions agreed with the team (2 Nov 2026):
  * Keep history: a project in the sheet that already exists is UPDATED in
    place (client, status, project manager, start / end dates, team) so its
    logged work entries, timesheets, budgets and status history stay
    attached. Four sheet rows are renames of existing projects (RENAMES).
  * Projects not in the sheet are archived (is_active = false, team
    removed) -- never deleted, so their logged hours stay intact. Archived
    projects are hidden from the app's project lists.
  * New sheet projects are created through crud.create_project.
  * Team Members REPLACE each project's team: listed people get an
    allocation (crud.create_project_allocation, 100 %), everyone else's
    allocation on that project is removed (as Edit Project > remove does).
  * Team names map by MEMBER_MAP; "Naga Sai Paluchuri" is EMP114 Naga Sai
    Kumar Palachuri, a plain "Naga Sai" is EMP116 Venkata Naga Sai.
  * Dates: a "—" cell clears the date, an empty cell keeps the current one.
    Budget / Logged Hrs / Progress % are report figures -- not written
    (the sheet's two budgets already match).

Idempotent. Dry run by default (rolled back); --execute commits in one
transaction after writing a JSON backup of every row it changes.

Usage (from backend/):
    venv\\Scripts\\python -m app.sync_projects_from_excel
    venv\\Scripts\\python -m app.sync_projects_from_excel --execute
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

from . import crud, models
from .database import SessionLocal, set_session_tenant_slug, set_tenant_context

TENANT_SLUG = "impacgo-solutions"
COMPANY_ID = uuid.UUID("4fd9da4f-397f-4fbd-be50-e1245b5226cf")
SHEET = Path(__file__).resolve().parents[2] / "project_report_updated_employee.xlsx"
BACKUP_DIR = Path(__file__).resolve().parent.parent / "db_backups"
SOURCE_TAG = "sync_projects_from_excel"

# sheet name -> existing project name it renames
RENAMES = {
    "RSGS - REAP System Integration Implementation": "RSGS - REAP System Integration",
    "REC PMO GLOBAL SUPPORT": "RESONAC PMO GLOBAL SUPPORT",
    "RMM D365FO Customizations": "RMM D365FO Implementation",
    "D365 F&O Support Rosti": "D365 F&O Implementation — Rosti",
}
# Team Members token (normalized) -> employee code
MEMBER_MAP = {
    "aditya": "EMP112", "bhushan": "EMP113", "bhargava": "EMP119", "swetha": "EMP111",
    "vajj": "EMP389", "manisha": "EMP115", "naga sai paluchuri": "EMP114", "naga sai": "EMP116",
    "surya": "EMP117", "srikar": "EMP369", "mayank": "EMP118", "mounika": "F003", "naveen": "F002",
    "nga": "F001", "chandra": "F005", "jai": "F006", "ramesh": "EMP309",
}
MANAGER_MAP = {"venkata srikar tantravahi": "EMP369"}
STATUSES = {"active", "planning", "completed", "on_hold", "cancelled"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def blank_dash(v) -> bool:
    return isinstance(v, str) and v.strip() in ("—", "-", "–", "�")


def cell_date(v):
    """date | None (clear) | ... (keep: empty cell)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return ...
    if blank_dash(v):
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    return datetime.date.fromisoformat(str(v)[:10])


def sheet_rows() -> list[dict]:
    ws = openpyxl.load_workbook(SHEET, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [norm(h) for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        d = dict(zip(head, r))
        team = [norm(t) for t in str(d.get("team members") or "").split(",")]
        out.append({
            "name": " ".join(str(d["project"]).split()),
            "client": " ".join(str(d.get("client") or "").split()),
            "status": str(d.get("status") or "").strip().lower(),
            "manager": norm(str(d.get("project manager") or "")),
            "budget": d.get("budget"),
            "start": cell_date(d.get("start")),
            "end": cell_date(d.get("end")),
            "team": [t for t in team if t],
        })
    return out


def run(execute: bool) -> None:
    rows = sheet_rows()
    problems = []
    for r in rows:
        if r["status"] not in STATUSES:
            problems.append(f"{r['name']}: unknown status {r['status']!r}")
        if r["manager"] not in MANAGER_MAP:
            problems.append(f"{r['name']}: unknown project manager {r['manager']!r}")
        for t in r["team"]:
            if t not in MEMBER_MAP:
                problems.append(f"{r['name']}: unknown team member {t!r}")
    if len({norm(r["name"]) for r in rows}) != len(rows):
        problems.append("duplicate project names in the sheet")
    if problems:
        raise RuntimeError("Sheet problems:\n  " + "\n  ".join(problems))

    set_tenant_context(TENANT_SLUG)
    db = SessionLocal()
    db.execute(text(f'SET search_path TO "{TENANT_SLUG}", public'))
    set_session_tenant_slug(db, TENANT_SLUG)
    report = collections.defaultdict(list)
    backup = {"projects": [], "allocations_removed": [], "allocations_added": [], "projects_created": [],
              "customers_created": []}
    try:
        emp_by_code = {e.employee_code: e for e in db.scalars(
            select(models.Employee).where(models.Employee.company_id == COMPANY_ID))}
        missing = sorted({c for c in [*MEMBER_MAP.values(), *MANAGER_MAP.values()] if c not in emp_by_code})
        if missing:
            raise RuntimeError(f"Employees not found: {missing}")
        projects = list(db.scalars(select(models.Project).where(models.Project.company_id == COMPANY_ID)))
        by_norm = {norm(p.name): p for p in projects}
        today = crud.company_today(db, COMPANY_ID)
        budgets = crud.get_project_budgets_bulk(db, [p.id for p in projects])

        def snapshot(p):
            return {"id": str(p.id), "name": p.name, "customer_id": str(p.customer_id) if p.customer_id else None,
                    "status": p.status, "project_manager_id": str(p.project_manager_id) if p.project_manager_id else None,
                    "planned_start_date": str(p.planned_start_date) if p.planned_start_date else None,
                    "planned_end_date": str(p.planned_end_date) if p.planned_end_date else None,
                    "is_active": p.is_active}

        kept: set[uuid.UUID] = set()
        for r in rows:
            existing_name = RENAMES.get(r["name"], r["name"])
            p = by_norm.get(norm(existing_name))
            manager = emp_by_code[MANAGER_MAP[r["manager"]]]
            customer = None
            if r["client"]:
                had = db.scalar(select(models.Customer.id).where(
                    models.Customer.company_id == COMPANY_ID,
                    func.lower(models.Customer.name) == r["client"].lower()))
                customer = crud.get_or_create_customer(db, COMPANY_ID, r["client"])
                if had is None and customer is not None:
                    backup["customers_created"].append({"id": str(customer.id), "name": customer.name})
                    report["clients created"].append(r["client"])
            if p is None:
                p = crud.create_project(
                    db, COMPANY_ID, r["name"], r["status"],
                    planned_start_date=r["start"] if r["start"] is not ... else None,
                    planned_end_date=r["end"] if r["end"] is not ... else None,
                    project_manager_id=manager.id,
                    customer_id=customer.id if customer else None,
                )
                crud.record_project_status_change(db, p.id, None, r["status"], None, remarks=SOURCE_TAG)
                backup["projects_created"].append(str(p.id))
                report["projects created"].append(f"{r['name']} [{r['client']}, {r['status']}]")
            else:
                before = snapshot(p)
                changes = []
                if p.name != r["name"]:
                    changes.append(f"name {p.name!r} -> {r['name']!r}")
                    p.name = r["name"]
                if customer is not None and p.customer_id != customer.id:
                    changes.append(f"client -> {customer.name}")
                    p.customer_id = customer.id
                if p.status != r["status"]:
                    changes.append(f"status {p.status} -> {r['status']}")
                    crud.record_project_status_change(db, p.id, p.status, r["status"], None, remarks=SOURCE_TAG)
                    p.status = r["status"]
                if p.project_manager_id != manager.id:
                    changes.append("project manager -> Venkata Srikar Tantravahi")
                    p.project_manager_id = manager.id
                for field, value in (("planned_start_date", r["start"]), ("planned_end_date", r["end"])):
                    if value is not ... and getattr(p, field) != value:
                        changes.append(f"{field.split('_')[1]} {getattr(p, field)} -> {value}")
                        setattr(p, field, value)
                if not p.is_active:
                    changes.append("re-activated")
                    p.is_active = True
                if changes:
                    backup["projects"].append(before)
                    report["projects updated"].append(f"{r['name']}: " + "; ".join(changes))
                else:
                    report["projects unchanged"].append(r["name"])
            kept.add(p.id)
            db.flush()

            # Budget check only (report figure; the sheet's budgets already match).
            if isinstance(r["budget"], (int, float)):
                b = budgets.get(p.id)
                if b is None or float(b.planned_amount) != float(r["budget"]):
                    report["budget differs (not changed)"].append(
                        f"{r['name']}: sheet {r['budget']} vs app {b.planned_amount if b else None}")

            # Team: exactly the listed people.
            wanted = {emp_by_code[MEMBER_MAP[t]].id for t in r["team"]}
            current = list(db.scalars(select(models.ProjectAllocation).where(
                models.ProjectAllocation.project_id == p.id)))
            for a in current:
                if a.employee_id not in wanted:
                    backup["allocations_removed"].append({
                        "id": str(a.id), "project_id": str(a.project_id), "employee_id": str(a.employee_id),
                        "project_role_id": str(a.project_role_id) if a.project_role_id else None,
                        "allocation_pct": float(a.allocation_pct), "start_date": str(a.start_date),
                        "end_date": str(a.end_date) if a.end_date else None, "is_active": a.is_active})
                    db.delete(a)
                    report["team removed"].append(f"{r['name']}: {emp_by_code_name(emp_by_code, a.employee_id)}")
            db.flush()
            have = {a.employee_id for a in current if a.employee_id in wanted and a.is_active}
            for emp_id in sorted(wanted - have, key=str):
                a = crud.create_project_allocation(db, p.id, emp_id, 100, start_date=today)
                backup["allocations_added"].append(str(a.id))
                report["team added"].append(f"{r['name']}: {emp_by_code_name(emp_by_code, emp_id)}")
            db.flush()

        # Archive everything not in the sheet (history kept).
        for p in projects:
            if p.id in kept or not p.is_active:
                continue
            backup["projects"].append(snapshot(p))
            p.is_active = False
            for a in db.scalars(select(models.ProjectAllocation).where(models.ProjectAllocation.project_id == p.id)):
                backup["allocations_removed"].append({
                    "id": str(a.id), "project_id": str(a.project_id), "employee_id": str(a.employee_id),
                    "project_role_id": str(a.project_role_id) if a.project_role_id else None,
                    "allocation_pct": float(a.allocation_pct), "start_date": str(a.start_date),
                    "end_date": str(a.end_date) if a.end_date else None, "is_active": a.is_active})
                db.delete(a)
            hours = db.scalar(select(func.coalesce(func.sum(models.WorkEntry.hours), 0))
                              .where(models.WorkEntry.project_id == p.id))
            report["projects archived (history kept)"].append(
                f"{p.name}" + (f" -- {float(hours):g} h logged, kept" if hours else ""))
        db.flush()
        crud.create_audit_log(db, COMPANY_ID, None, "import", "project", None, changes={
            "source": SOURCE_TAG, "created": len(backup["projects_created"]),
            "updated_or_archived": len(backup["projects"]),
            "team_added": len(backup["allocations_added"]), "team_removed": len(backup["allocations_removed"])})

        if execute:
            BACKUP_DIR.mkdir(exist_ok=True)
            out = BACKUP_DIR / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_project_sync_backup.json"
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


def emp_by_code_name(emp_by_code: dict, emp_id) -> str:
    for e in emp_by_code.values():
        if e.id == emp_id:
            return f"{e.first_name} {e.last_name or ''}".strip()
    return str(emp_id)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--execute", action="store_true")
    run(p.parse_args().execute)
