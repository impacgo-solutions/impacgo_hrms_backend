"""Runs one HRMS migration file against the configured database.

    cd backend
    venv/Scripts/python db/run_hrms_migration.py db/<file>.sql [--dry-run]

Conventions for HRMS migration files (see db/README_HRMS_MIGRATIONS.md):
  * Idempotent (IF NOT EXISTS / ON CONFLICT / guarded UPDATEs).
  * Touch ONLY the `_template` schema and HRMS tenants -- schemas whose
    public.tenant_modules row has module_code = 'hcm' AND is_enabled. Use
    the session-only helper pg_temp.hrms_schemas() this runner creates (or the
    same SELECT). Never public.users or any non-HRMS tenant's schema.
  * Plain SQL / DO blocks; `%` is passed through untouched (raw cursor).

The whole file runs in ONE transaction with lock_timeout = 5s, so a
failure or lock wait leaves nothing half-applied. --dry-run executes it and
rolls back (shows NOTICEs, changes nothing).
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import database  # noqa: E402

HELPER = """
CREATE OR REPLACE FUNCTION pg_temp.hrms_schemas() RETURNS SETOF text
LANGUAGE sql STABLE AS $f$
    SELECT '_template'::text
    UNION
    SELECT DISTINCT tm.tenant_slug
    FROM public.tenant_modules tm
    JOIN information_schema.schemata s ON s.schema_name = tm.tenant_slug
    WHERE tm.module_code = 'hcm' AND tm.is_enabled
$f$;
"""


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    path = pathlib.Path(argv[0])
    dry = "--dry-run" in argv
    sql = path.read_text(encoding="utf-8")
    raw = database.engine.raw_connection()
    try:
        raw.autocommit = False
        cur = raw.cursor()
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute(HELPER)
        cur.execute(sql)
        for notice in getattr(raw, "notices", [])[-200:]:
            print(notice.strip())
        if dry:
            raw.rollback()
            print(f"DRY RUN -- rolled back: {path.name}")
        else:
            raw.commit()
            print(f"Applied: {path.name} on {database.engine.url.database}")
        return 0
    except Exception as exc:  # noqa: BLE001
        raw.rollback()
        print(f"FAILED (rolled back): {path.name}: {exc}", file=sys.stderr)
        return 1
    finally:
        raw.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
