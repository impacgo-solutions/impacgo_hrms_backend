"""CLI to backfill the RBAC default template (see backend/db/
seed_rbac_template.sql) into EXISTING tenants.

Additive, per-resource, by construction: this script's own logic never
writes anything -- all it does is call public.provision_tenant_rbac(tenant_
slug, company_id) and, optionally, public.apply_template_role_grants(...)
(both in backend/db/add_provision_tenant_rbac_function.sql). Those functions
apply the template one RESOURCE at a time (a legacy matrix column like
leave_approval, or a granular catalog resource like employee): a resource
the tenant's role already has ANY grant for -- whatever level/actions it
happens to be -- is left completely untouched; only a resource the role has
never configured at all picks up the template default. A role a tenant
already customized keeps every bit of that customization, even for a
built-in role whose name matches the template exactly.

Prerequisites (run these once, by hand, before this script can do anything
useful -- see each file's own header):
    backend/db/seed_rbac_template.sql               (populates _template)
    backend/db/add_provision_tenant_rbac_function.sql (installs the functions)

Usage (from backend/):
    python -m app.migrate_apply_rbac_template                    # dry-run, every tenant
    python -m app.migrate_apply_rbac_template --tenant acme      # dry-run, one tenant
    python -m app.migrate_apply_rbac_template --apply --tenant acme
    python -m app.migrate_apply_rbac_template --apply            # every active tenant

Dry-run (the default -- no flag needed) makes zero writes. It only reports,
per tenant and per company within that tenant's schema, which of the 14
built-in roles already exist by name vs. are missing. Review that report
before ever passing --apply against a real database.

--role-map "Existing Role Name=Template Role Name" (repeatable, requires
--tenant): for a tenant whose equivalent of a built-in role has a CUSTOM
name -- e.g. INFYQ's real HR role is called "HR Manager", not "HR /
Recruitment Staff" -- provision_tenant_rbac's exact-name matching will never
touch that role; it would instead create a brand-new, unused "HR /
Recruitment Staff" role alongside it. --role-map calls public.
apply_template_role_grants(tenant, existing_role_id, template_role_name)
directly on the role YOU name, applying the same per-resource,
skip-if-already-configured logic. This is opt-in and explicit on purpose --
the script never guesses that a custom-named role "is probably" a built-in
one from name similarity alone, since that's exactly the kind of silent
misapplication of broad permissions that must never happen automatically.

Example:
    python -m app.migrate_apply_rbac_template --apply --tenant infyq \\
        --role-map "HR Manager=HR / Recruitment Staff"
"""

import argparse

from sqlalchemy import text

from .database import engine
from .rbac_columns import BUILTIN_ROLES


def _tenant_slugs(conn, only_slug: str | None) -> list[str]:
    if only_slug:
        return [only_slug]
    rows = conn.execute(
        text("SELECT slug FROM public.tenants WHERE is_active = true ORDER BY slug")
    ).all()
    return [row[0] for row in rows]


def _companies_for_tenant(conn, slug: str) -> list[tuple[str, str]]:
    conn.execute(text(f'SET search_path TO "{slug}", public'))
    rows = conn.execute(text("SELECT id::text, name FROM core_companies ORDER BY name")).all()
    return [(row[0], row[1]) for row in rows]


def _existing_role_names(conn, slug: str, company_id: str) -> set[str]:
    conn.execute(text(f'SET search_path TO "{slug}", public'))
    rows = conn.execute(
        text("SELECT name FROM core_roles WHERE company_id = :cid"), {"cid": company_id}
    ).all()
    return {row[0] for row in rows}


def _find_role_id_by_name(conn, slug: str, company_id: str, role_name: str) -> str | None:
    conn.execute(text(f'SET search_path TO "{slug}", public'))
    row = conn.execute(
        text("SELECT id::text FROM core_roles WHERE company_id = :cid AND name = :name"),
        {"cid": company_id, "name": role_name},
    ).first()
    return row[0] if row else None


def _parse_role_map(pairs: list[str]) -> dict[str, str]:
    role_map: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--role-map value {pair!r} must be 'Existing Name=Template Name'")
        existing_name, template_name = pair.split("=", 1)
        existing_name, template_name = existing_name.strip(), template_name.strip()
        if template_name not in BUILTIN_ROLES:
            raise SystemExit(
                f"--role-map: {template_name!r} is not one of the built-in template roles: {BUILTIN_ROLES}"
            )
        role_map[existing_name] = template_name
    return role_map


def run(only_slug: str | None, apply: bool, role_map: dict[str, str] | None = None) -> None:
    role_map = role_map or {}
    if role_map and not only_slug:
        raise SystemExit("--role-map requires --tenant (a role-name mapping is specific to one tenant).")

    with engine.connect() as conn:
        slugs = _tenant_slugs(conn, only_slug)

    if not slugs:
        print("No tenants found (check --tenant spelling, or public.tenants.is_active).")
        return

    print(f"{'APPLY' if apply else 'DRY-RUN'} mode -- {len(slugs)} tenant(s): {', '.join(slugs)}")
    if role_map:
        print(f"Role map for this run: {role_map}")
    print()

    for slug in slugs:
        try:
            with engine.connect() as conn:
                companies = _companies_for_tenant(conn, slug)
        except Exception as exc:
            print(f"[{slug}] SKIP -- could not read core_companies ({exc})")
            continue

        if not companies:
            print(f"[{slug}] no rows in core_companies -- skipping")
            continue

        for company_id, company_name in companies:
            with engine.connect() as conn:
                existing = _existing_role_names(conn, slug, company_id)

            missing = [r for r in BUILTIN_ROLES if r not in existing]
            present = [r for r in BUILTIN_ROLES if r in existing]

            print(f"[{slug}] company '{company_name}' ({company_id})")
            print(f"    present ({len(present)}/{len(BUILTIN_ROLES)}): {', '.join(present) if present else '(none)'}")
            print(f"    missing ({len(missing)}/{len(BUILTIN_ROLES)}): {', '.join(missing) if missing else '(none)'}")

            if apply:
                with engine.begin() as conn:
                    conn.execute(
                        text("SELECT public.provision_tenant_rbac(:slug, :cid)"),
                        {"slug": slug, "cid": company_id},
                    )
                print(f"    -> applied public.provision_tenant_rbac('{slug}', '{company_id}')")

            for existing_name, template_name in role_map.items():
                with engine.connect() as conn:
                    role_id = _find_role_id_by_name(conn, slug, company_id, existing_name)

                if role_id is None:
                    print(f"    role-map: '{existing_name}' -> '{template_name}': "
                          f"NOT FOUND in this company -- skipped")
                    continue

                print(f"    role-map: '{existing_name}' (id={role_id}) -> template '{template_name}'"
                      f"{'' if apply else ' [dry-run, not applied]'}")

                if apply:
                    with engine.begin() as conn:
                        conn.execute(
                            text("SELECT public.apply_template_role_grants(:slug, :role_id, :template_name)"),
                            {"slug": slug, "role_id": role_id, "template_name": template_name},
                        )
                    print(f"    -> applied public.apply_template_role_grants('{slug}', '{role_id}', '{template_name}')")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill the RBAC default template into existing tenants (additive, per-resource, never overwrites)."
    )
    parser.add_argument(
        "--tenant", metavar="SLUG", default=None,
        help="Only process this tenant slug (default: every row in public.tenants with is_active = true). "
        "Required when --role-map is used.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only, no writes (default behavior even without this flag).")
    mode.add_argument("--apply", action="store_true", help="Actually call the provisioning functions for each tenant/company found.")
    parser.add_argument(
        "--role-map", metavar="EXISTING=TEMPLATE", action="append", default=[],
        help="Repeatable. Map an existing, custom-named role (e.g. 'HR Manager') to a built-in "
        "template role name (e.g. 'HR / Recruitment Staff') so it receives that template role's "
        "default grants for any resource it hasn't already configured. Requires --tenant.",
    )
    args = parser.parse_args()

    run(args.tenant, apply=args.apply, role_map=_parse_role_map(args.role_map))
