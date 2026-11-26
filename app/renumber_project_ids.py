"""Renumber every existing project's Project ID in each company's configured
format (Administration > General Settings > Project ID Format), oldest
project first, and make new projects continue the same sequence.

    # dry run (default) -- prints the plan, writes nothing
    cd backend && venv/Scripts/python -m app.renumber_project_ids --tenant impacgo-solutions
    # apply (one transaction per company; rolls back if any check fails)
    cd backend && venv/Scripts/python -m app.renumber_project_ids --tenant impacgo-solutions --apply
    # undo with the mapping file an --apply run saved
    cd backend && venv/Scripts/python -m app.renumber_project_ids --rollback db_backups/project_ids_<...>.json --apply

Targets the database in DATABASE_URL (backend/.env unless overridden in the
environment).

Order: actual start date, else planned start date, else the creation date;
ties by creation time, then id -- deterministic, so a re-run gives the same
result (and is a no-op once applied). Only pm_projects.code and the
company's core_number_series floor change. Every other table links to a
project by pm_projects.id (UUID), so tasks, time entries, members,
allocations, budgets, expenses, travel requests etc. are untouched; this is
verified per company (row counts per project in every table with a
project_id column, before vs after) before committing.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import database, document_numbers, models

BACKUP_DIR = Path(__file__).resolve().parents[1] / "db_backups"


def _order_key(p: models.Project):
    created = p.created_at or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    start = p.actual_start_date or p.planned_start_date or created.date()
    return (start, created, str(p.id))


def _basis(p: models.Project) -> str:
    if p.actual_start_date:
        return f"actual start {p.actual_start_date}"
    if p.planned_start_date:
        return f"planned start {p.planned_start_date} (no actual start date)"
    return f"created {p.created_at.date() if p.created_at else '?'} (no start dates)"


def _reference_tables(db: Session) -> list[str]:
    return list(db.scalars(text(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND column_name = 'project_id' ORDER BY table_name")).all())


def _reference_counts(db: Session, tables: list[str], ids: list[uuid.UUID]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for t in tables:
        rows = db.execute(text(
            f'SELECT project_id, count(*) FROM "{t}" WHERE project_id = ANY(:ids) GROUP BY project_id'),
            {"ids": ids}).all()
        out[t] = {str(pid): n for pid, n in rows}
    return out


def plan_company(db: Session, company_id: uuid.UUID, today: datetime.date) -> dict:
    series = document_numbers.get_series(db, company_id, "project")
    projects = sorted(db.scalars(select(models.Project).where(models.Project.company_id == company_id)).all(),
                      key=_order_key)
    rows = [{"id": str(p.id), "name": p.name, "old_code": p.code,
             "new_code": document_numbers.format_id(series, i, today), "basis": _basis(p)}
            for i, p in enumerate(projects, start=1)]
    return {"company_id": str(company_id), "format": {
        "prefix": series.prefix, "suffix": series.suffix or "", "padding": series.padding,
        "include_year": bool(series.fiscal_infix), "floor_before": series.current_no},
        "projects": rows, "changed": sum(r["old_code"] != r["new_code"] for r in rows),
        "next_new_project": document_numbers.format_id(series, len(rows) + 1, today)}


def apply_company(db: Session, company_id: uuid.UUID, plan: dict) -> None:
    """Applies [plan] inside the caller's transaction; raises on any failed check."""
    document_numbers.lock(db, company_id, "project")  # no project can be created meanwhile
    ids = [uuid.UUID(r["id"]) for r in plan["projects"]]
    current = {str(p.id): p.code for p in db.scalars(select(models.Project).where(
        models.Project.company_id == company_id))}
    if set(current) != {r["id"] for r in plan["projects"]}:
        raise RuntimeError("Projects changed since the plan was made -- re-run the dry run.")
    tables = _reference_tables(db)
    before = _reference_counts(db, tables, ids)
    # Two phases: unique temporary codes first, so swapping codes between
    # projects never trips the (company_id, code) unique index.
    for r in plan["projects"]:
        db.execute(text("UPDATE pm_projects SET code = :c WHERE id = :id"),
                   {"c": f"~{uuid.UUID(r['id']).hex[:28]}", "id": r["id"]})
    for r in plan["projects"]:
        db.execute(text("UPDATE pm_projects SET code = :c WHERE id = :id"), {"c": r["new_code"], "id": r["id"]})
    series = document_numbers.get_series(db, company_id, "project")
    if not series.fiscal_infix:
        # New projects continue right after the renumbered ones.
        series.current_no = len(plan["projects"])
    db.flush()

    # ── checks (any failure -> exception -> the caller rolls back) ──
    after_codes = {str(p.id): p.code for p in db.scalars(select(models.Project).where(
        models.Project.company_id == company_id).execution_options(populate_existing=True))}
    expected = {r["id"]: r["new_code"] for r in plan["projects"]}
    if after_codes != expected:
        raise RuntimeError("Project IDs after the update don't match the plan.")
    if len(set(after_codes.values())) != len(after_codes):
        raise RuntimeError("Duplicate Project IDs after the update.")
    if _reference_counts(db, tables, ids) != before:
        raise RuntimeError("Related records changed during the update.")
    if document_numbers.preview(db, company_id, "project") != plan["next_new_project"]:
        raise RuntimeError("The next new Project ID would not continue the sequence.")


def run(tenants: list[str] | None, apply: bool) -> int:
    today = datetime.date.today()
    with Session(database.engine) as probe:
        all_tenants = list(probe.scalars(text(
            "SELECT t.slug FROM public.tenants t WHERE EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = t.slug AND table_name = 'pm_projects') ORDER BY t.slug")).all())
    targets = [t for t in all_tenants if not tenants or t in tenants]
    missing = set(tenants or []) - set(targets)
    if missing:
        print(f"Unknown tenant(s): {', '.join(sorted(missing))}")
        return 2
    report = {"database": database.engine.url.database, "run_at": datetime.datetime.now().isoformat(),
              "applied": apply, "tenants": {}}
    for slug in targets:
        from . import crud
        db = crud.open_tenant_session(slug)
        try:
            companies = list(db.scalars(select(models.Company.id)).all())
            plans = [plan_company(db, cid, today) for cid in companies]
            plans = [p for p in plans if p["projects"]]
            report["tenants"][slug] = plans
            for p in plans:
                f = p["format"]
                print(f"\n=== {slug} / company {p['company_id']} -- format {f['prefix']}{'YYYY-' if f['include_year'] else ''}"
                      f"{'0' * (f['padding'] - 1)}1{f['suffix']}; {len(p['projects'])} projects, {p['changed']} change; "
                      f"next new project: {p['next_new_project']}")
                for r in p["projects"]:
                    mark = "  " if r["old_code"] == r["new_code"] else "->"
                    print(f"  {r['old_code']:>14} {mark} {r['new_code']:<14} {r['name'][:44]:<44} {r['basis']}")
            if apply:
                for p in plans:
                    apply_company(db, uuid.UUID(p["company_id"]), p)
                db.commit()
                print(f"\n{slug}: applied and verified.")
            else:
                db.rollback()  # get_series may have created a default format row
        except Exception as exc:
            db.rollback()
            print(f"\n{slug}: NOT applied -- {exc}")
            return 1
        finally:
            db.close()
    if apply:
        BACKUP_DIR.mkdir(exist_ok=True)
        path = BACKUP_DIR / f"project_ids_{report['database']}_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nOld -> new mapping saved for rollback: {path}")
    else:
        print("\nDry run only -- nothing was written. Re-run with --apply to apply.")
    return 0


def rollback(path: str, apply: bool) -> int:
    """Restores the old codes (and floor) recorded by an --apply run."""
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    from . import crud
    for slug, plans in report["tenants"].items():
        db = crud.open_tenant_session(slug)
        try:
            for p in plans:
                cid = uuid.UUID(p["company_id"])
                document_numbers.lock(db, cid, "project")
                for r in p["projects"]:
                    db.execute(text("UPDATE pm_projects SET code = :c WHERE id = :id"),
                               {"c": f"~{uuid.UUID(r['id']).hex[:28]}", "id": r["id"]})
                for r in p["projects"]:
                    db.execute(text("UPDATE pm_projects SET code = :c WHERE id = :id"),
                               {"c": r["old_code"], "id": r["id"]})
                document_numbers.get_series(db, cid, "project").current_no = p["format"]["floor_before"]
                print(f"{slug} / {cid}: {len(p['projects'])} projects restored")
            db.commit() if apply else db.rollback()
        finally:
            db.close()
    print("Rolled back." if apply else "Dry run only -- re-run with --apply to roll back.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", action="append", help="tenant slug (repeatable); default: every tenant")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--rollback", metavar="MAPPING_JSON", help="restore the codes from an --apply run's mapping")
    args = ap.parse_args(argv)
    if args.rollback:
        return rollback(args.rollback, args.apply)
    return run(args.tenant, args.apply)


if __name__ == "__main__":
    sys.exit(main())
