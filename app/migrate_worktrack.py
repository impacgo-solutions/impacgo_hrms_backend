"""Adds WorkTrack's own tables (pm_wt_*) to _template and every tenant schema.

Additive only: CREATE TABLE / INDEX IF NOT EXISTS -- no existing table,
column or row is touched. New tenants get the tables by cloning _template
(the "pm" prefix), and routers/worktrack.ensure_tables() creates them on
first use as a safety net.

    python -m app.migrate_worktrack            (dry run: lists schemas)
    python -m app.migrate_worktrack --execute
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from .database import engine
from .routers.worktrack import WT_DDL


def main(execute: bool) -> None:
    with engine.connect() as conn:
        tenants = [r[0] for r in conn.execute(text("select slug from public.tenants order by slug"))]
        existing = {r[0] for r in conn.execute(text("select schema_name from information_schema.schemata"))}
        schemas = [s for s in ["_template", *tenants] if s in existing]
        print("schemas:", schemas)
        if not execute:
            print("DRY RUN -- nothing changed")
            return
        conn.rollback()  # end the read-only transaction opened above
        tx = conn.begin()
        for schema in schemas:
            conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            for ddl in WT_DDL:
                conn.execute(text(ddl))
            print(f"  {schema}: ok")
        tx.commit()
        print("done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    main(ap.parse_args().execute)
