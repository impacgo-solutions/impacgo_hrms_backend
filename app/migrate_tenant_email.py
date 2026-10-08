"""Creates public.tenant_email_settings (db/2026_10_08_tenant_email_settings.sql).

Additive and idempotent -- safe on dev / test / staging / production, and it
never touches existing email settings or tenant schemas.

    python -m app.migrate_tenant_email            (dry run: shows what exists)
    python -m app.migrate_tenant_email --execute
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import text

from .database import engine

SQL_FILE = Path(__file__).resolve().parents[1] / "db" / "2026_10_08_tenant_email_settings.sql"


def main(execute: bool) -> None:
    with engine.connect() as conn:
        exists = conn.execute(text("select to_regclass('public.tenant_email_settings')")).scalar()
        tenants = conn.execute(text("select count(*) from public.tenants")).scalar()
        print(f"public.tenant_email_settings exists: {bool(exists)}; tenants: {tenants}")
        if not execute:
            print("DRY RUN -- nothing changed (use --execute)")
            return
        conn.rollback()
        with conn.begin():
            conn.exec_driver_sql(SQL_FILE.read_text(encoding="utf-8"))
        print("done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    main(ap.parse_args().execute)
