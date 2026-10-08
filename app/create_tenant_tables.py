"""Create all tenant-schema tables for a given slug.

Run this ONCE before seeding a new tenant that has an empty schema.

Usage:
    python -m app.create_tenant_tables --tenant acme

The script sets search_path to the tenant schema, then runs
CREATE TABLE IF NOT EXISTS for every non-public model
(core_companies, core_employees, hcm_leaves, …).
public.admin_users / public.users / public.tenants are left untouched.
"""

import argparse

from sqlalchemy import text

from . import models  # noqa: F401 — registers all model classes on Base.metadata
from .database import Base, engine


def create_tables(tenant_slug: str) -> None:
    # Only tables that have no explicit schema (they rely on search_path).
    # Public-schema models (AdminUser, PublicUser, Tenant) are excluded.
    tenant_tables = [
        t for t in Base.metadata.sorted_tables
        if t.schema is None
    ]

    print(f"Creating {len(tenant_tables)} tables in schema '{tenant_slug}' …")

    with engine.connect() as conn:
        # All DDL on this connection resolves unqualified names to the tenant schema.
        conn.execute(text(f'SET search_path TO "{tenant_slug}", public'))

        # checkfirst=True → emits CREATE TABLE IF NOT EXISTS (safe to re-run).
        Base.metadata.create_all(conn, tables=tenant_tables, checkfirst=True)
        conn.commit()

    print(f"Done — schema '{tenant_slug}' is ready.")
    print(f"Next step:  python -m app.seed_new_company --tenant {tenant_slug}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create tenant schema tables (run once per new tenant)."
    )
    parser.add_argument(
        "--tenant",
        metavar="SLUG",
        required=True,
        help="Tenant slug whose schema will receive the tables (e.g. 'acme').",
    )
    args = parser.parse_args()
    create_tables(args.tenant)
