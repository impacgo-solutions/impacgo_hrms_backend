"""Runs ONE-company migration files (plain SQL with unqualified table names,
written to be run after `SET search_path TO "<tenant>", public`) against
several company schemas -- each company in its own transaction, so a failure
in one leaves that company untouched.

    cd backend
    venv/Scripts/python db/run_for_companies.py <schema>[,<schema>...] db/<a>.sql [db/<b>.sql ...] [--dry-run]

Files run in the order given, per company. --dry-run executes everything and
rolls each company back (shows whether it would succeed; changes nothing).
Only schemas that are HRMS tenants (or _template) are accepted.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import database  # noqa: E402


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    dry = "--dry-run" in argv
    if len(args) < 2:
        print(__doc__)
        return 2
    schemas = [s.strip() for s in args[0].split(",") if s.strip()]
    files = [pathlib.Path(p) for p in args[1:]]
    for f in files:
        if not f.is_file():
            print(f"No such file: {f}", file=sys.stderr)
            return 2
    raw = database.engine.raw_connection()
    failures = 0
    try:
        raw.autocommit = False
        cur = raw.cursor()
        cur.execute(
            "SELECT '_template' UNION SELECT tm.tenant_slug FROM public.tenant_modules tm "
            "JOIN information_schema.schemata s ON s.schema_name = tm.tenant_slug "
            "WHERE tm.module_code = 'hcm' AND tm.is_enabled"
        )
        allowed = {r[0] for r in cur.fetchall()}
        raw.rollback()
        for schema in schemas:
            if schema not in allowed:
                print(f"SKIPPED {schema}: not an HRMS company schema", file=sys.stderr)
                failures += 1
                continue
            try:
                cur.execute("SET LOCAL lock_timeout = '5s'")
                cur.execute('SET LOCAL search_path TO "%s", public' % schema.replace('"', '""'))
                for f in files:
                    cur.execute(f.read_text(encoding="utf-8"))
                    print(f"  ok  {schema}: {f.name}")
                if dry:
                    raw.rollback()
                    print(f"DRY RUN -- rolled back: {schema}")
                else:
                    raw.commit()
                    print(f"Applied on {database.engine.url.database} / {schema}")
            except Exception as exc:  # noqa: BLE001
                raw.rollback()
                failures += 1
                print(f"FAILED (rolled back) {schema}: {exc}", file=sys.stderr)
    finally:
        raw.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
