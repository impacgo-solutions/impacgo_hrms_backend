"""Read-only startup/CI check (SEC-01): compares every tenant's built-in
role matrices with rbac_columns.DEFAULT_MATRIX.

  * Prints every built-in role whose stored matrix differs from the template
    (informational -- tenants may customize most roles on purpose, e.g. the
    HR role; that is a product decision, see RBAC-02).
  * FAILS (exit 1) if a self-service role (rbac_columns.SELF_SERVICE_ROLES:
    "Professional / IC Employee", "Associate / Intern") holds 'e' or 'a' on
    any admin column (rbac_columns.ADMIN_COLUMNS), or anything above its
    template level. Authorization already caps these roles at the template
    (crud.effective_role_matrix), but the data should still be repaired --
    see backend/db/fix_2026_09_24_sec01_reset_ic_role_matrix.sql.

Never writes. Usage (from backend/):
    venv/Scripts/python -m app.check_role_matrix_drift [--tenant SLUG] [--quiet]
"""

import argparse
import sys

from sqlalchemy import text

from .database import engine
from .rbac_columns import (
    ADMIN_COLUMNS,
    COLUMN_KEYS,
    DEFAULT_MATRIX,
    LEVEL_RANK,
    SELF_SERVICE_ROLES,
    template_levels,
)


def _tenant_slugs(conn, only: str | None) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT t.slug FROM public.tenants t "
            "JOIN information_schema.tables i ON i.table_schema = t.slug AND i.table_name = 'core_roles' "
            "ORDER BY t.slug"
        )
    ).scalars().all()
    return [s for s in rows if only is None or s == only]


def check(only_tenant: str | None = None, quiet: bool = False) -> int:
    failures = 0
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        for slug in _tenant_slugs(conn, only_tenant):
            schema = '"' + slug.replace('"', '""') + '"'
            rows = conn.execute(
                text(
                    f"SELECT r.company_id, r.name, p.resource, p.action "
                    f"FROM {schema}.core_roles r "
                    f"LEFT JOIN {schema}.core_role_permissions rp ON rp.role_id = r.id "
                    f"LEFT JOIN {schema}.core_permissions p ON p.id = rp.permission_id "
                    f"WHERE r.name = ANY(:names)"
                ),
                {"names": list(DEFAULT_MATRIX)},
            ).all()
            matrices: dict[tuple, dict[str, str]] = {}
            for company_id, name, resource, action in rows:
                m = matrices.setdefault((company_id, name), {k: "n" for k in COLUMN_KEYS})
                if resource in m and action is not None and len(action) == 1:
                    m[resource] = action
            for (company_id, name), stored in sorted(matrices.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
                template = template_levels(name) or {}
                diffs = [k for k in COLUMN_KEYS if stored[k] != template.get(k, "n")]
                if not diffs:
                    continue
                violations = []
                if name in SELF_SERVICE_ROLES:
                    violations = [
                        k for k in COLUMN_KEYS
                        if (k in ADMIN_COLUMNS and stored[k] in ("e", "a"))
                        or LEVEL_RANK.get(stored[k], 0) > LEVEL_RANK.get(template.get(k, "n"), 0)
                    ]
                tag = "FAIL" if violations else "drift"
                if violations:
                    failures += 1
                if violations or not quiet:
                    detail = ", ".join(f"{k}={stored[k]} (template {template.get(k, 'n')})" for k in (violations or diffs))
                    print(f"[{tag}] {slug} company={company_id} role='{name}': {detail}")
    if failures:
        print(f"\n{failures} self-service role(s) hold admin-tier grants -- see "
              "backend/db/fix_2026_09_24_sec01_reset_ic_role_matrix.sql")
    else:
        print("OK: no self-service role holds admin-tier grants.")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args()
    sys.exit(check(args.tenant, args.quiet))
