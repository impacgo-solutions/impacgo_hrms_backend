"""Canonical, single-command bootstrap for a brand-new tenant + its
Organization Owner -- replaces the manual, hand-run
backend/db/provision_new_tenant_and_owner.sql runbook.

Differences from that runbook (all intentional):
  * The Owner's login (core_users, core_user_roles, public.users,
    public.admin_users) is created by crud.create_employee -- the exact
    same function POST /api/employees calls -- instead of hand-written raw
    SQL with pgcrypto crypt(). Password hashing goes through
    security.hash_password, matching every other account in this system.
  * ALL 14 built-in roles are provisioned (not just the Owner) via
    public.provision_tenant_rbac(), sourced from the RBAC default template
    stored in `_template` (see backend/db/seed_rbac_template.sql +
    backend/db/add_provision_tenant_rbac_function.sql) -- both of those
    must already have been run by hand against this database before this
    script is useful; it does not create them itself.
  * A reserved-slug guard exists here (nothing enforced this before).
  * C17: the slug must match ^[a-z][a-z0-9_-]{1,59}$ (it is interpolated
    into DDL / SET search_path) -- validated only for NEW tenants, so the
    legacy mixed-case 'Infyq' schema is unaffected.
  * C17: the whole bootstrap (public.tenants row, schema clone, company,
    RBAC template, branch/department, Owner login) runs in ONE database
    transaction on one connection -- Postgres DDL is transactional, so a
    failure at any step rolls everything back and leaves no orphan schema
    or half-registered tenant; the same slug can simply be retried.

Known limitations, same as the old runbook (not solved by this script):
  * Nothing here validates that public.currencies already has an 'INR' row
    -- it fails with a clear error if that row is missing, same requirement
    every other seed script in this codebase already has.

Usage (from backend/):
    python -m app.provision_tenant \\
        --tenant acme2 \\
        --company-name "Acme Robotics Pvt Ltd" \\
        --owner-first-name Asha --owner-last-name Rao \\
        --owner-email asha.rao@acme2.example \\
        --owner-password "<a strong password>"
"""

import argparse
import datetime
import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import crud, models, schemas
from .database import engine, set_session_tenant_slug

RESERVED_SLUGS = {"_template", "public", "information_schema"}
DEFAULT_MODULES = ["core", "hcm", "pm"]
# C17: new tenant slugs become Postgres schema names interpolated into DDL /
# SET search_path, so only a safe charset is accepted. Applied to NEW
# provisioning only (legacy slugs such as 'Infyq' pre-date this rule).
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,59}$")
SLUG_RULE = (
    "Tenant slug must be 2-60 characters: a lowercase letter first, then lowercase "
    "letters, digits, '_' or '-'."
)


class ProvisioningError(ValueError):
    """A client-fixable provisioning problem (bad/duplicate slug) -- its
    message is safe to show to the Super Admin; anything else is not."""


def _validate_slug(tenant_slug: str) -> str:
    slug = tenant_slug.strip()
    if not SLUG_PATTERN.fullmatch(slug):
        raise ProvisioningError(SLUG_RULE)
    if slug in RESERVED_SLUGS or slug.startswith("pg_") or slug.startswith("information_schema"):
        raise ProvisioningError(
            f"'{tenant_slug}' is a reserved schema name and cannot be used as a tenant slug "
            f"(reserved: {sorted(RESERVED_SLUGS)}, plus any 'pg_*' prefix)"
        )
    return slug


def _analyze_tenant_schema(tenant_slug: str) -> None:
    """DB-04: refresh planner statistics for a freshly provisioned tenant
    (acme was found never ANALYZEd -- row estimates of 1-7 vs thousands of
    real rows). Best-effort: never fails provisioning. Re-run
    `ANALYZE` per schema after any bulk seed (see
    backend/db/fix_2026_09_24_db02_indexes.sql)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = 0"))
            tables = conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": tenant_slug}
            ).scalars().all()
            for table in tables:
                conn.execute(text(f'ANALYZE "{tenant_slug}"."{table}"'))
            conn.execute(text("RESET statement_timeout"))
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"(warning) ANALYZE of '{tenant_slug}' skipped: {exc}")


def _clone_template_schema(conn, tenant_slug: str, modules: list[str]) -> None:
    conn.execute(
        text("SELECT public.provision_tenant(:slug, :modules)"),
        {"slug": tenant_slug, "modules": modules},
    )
    if "hcm" in modules:
        # public.provision_tenant() clones tables purely by module-name
        # prefix match, so requesting "hcm" alone never brings along
        # fin_fiscal_years -- yet crud.get_or_create_fiscal_year
        # (leave.py's create_leave_balance) reads/writes it for every
        # HCM tenant. Without this, every new HCM-enabled tenant would
        # hit the exact same "relation fin_fiscal_years does not exist"
        # gap discovered on the Infyq tenant (provisioned with only
        # ["core", "hcm", "pm"], matching DEFAULT_MODULES exactly --
        # this is a systemic gap, not a one-off). Cloning the single
        # table explicitly avoids pulling in the other ~30 fin_* tables
        # (accounting, budgets, fixed assets, etc.) that a pure-HCM
        # tenant has no use for.
        conn.execute(
            text(
                f'CREATE TABLE IF NOT EXISTS "{tenant_slug}".fin_fiscal_years '
                f"(LIKE _template.fin_fiscal_years INCLUDING ALL)"
            )
        )


TEMPLATE_COMPANY_ID = "11111111-1111-1111-1111-111111111111"

# Default configuration every new company inherits from the _template
# schema's template company (seeded by
# backend/db/template_2026_09_26_new_tenant_defaults.sql). Parents before
# children; id references between these tables are remapped.
CONFIG_TEMPLATE_TABLES = [
    "core_bands",
    "hcm_leave_types",
    "hcm_salary_components",
    "hcm_shifts",
    "core_integrations",
    "hcm_payslip_html_templates",
    "hcm_offer_letter_html_templates",
    # N-02: default Experience & Relieving / F&F Statement templates
    # (db/2026_10_06_exit_default_letter_templates.sql).
    "hcm_exit_letter_html_templates",
    "core_approval_workflows",
    "core_approval_workflow_steps",  # child of core_approval_workflows
]
_CONFIG_CHILD_KEYS = {"core_approval_workflow_steps": ("workflow_id", "core_approval_workflows")}


def _tenant_tables(conn, tenant_slug: str) -> set[str]:
    return set(conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": tenant_slug}
    ).scalars())


def _copy_template_foreign_keys(conn, tenant_slug: str) -> int:
    """CREATE TABLE ... (LIKE ... INCLUDING ALL) copies columns, defaults,
    checks and indexes but never foreign keys, so every tenant provisioned
    before this had none. Recreates each _template FK whose table and
    referenced table were both cloned (references into other schemas, e.g.
    public, are kept as-is). Constraint definitions are read with an empty
    search_path so every reference comes back schema-qualified."""
    tables = _tenant_tables(conn, tenant_slug)
    conn.execute(text("SET LOCAL search_path TO pg_catalog"))
    fks = conn.execute(text("""
        SELECT cl.relname AS tbl, k.conname, pg_get_constraintdef(k.oid) AS condef,
               rn.nspname AS ref_schema, rc.relname AS ref_table
        FROM pg_constraint k
        JOIN pg_class cl ON cl.oid = k.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        JOIN pg_class rc ON rc.oid = k.confrelid
        JOIN pg_namespace rn ON rn.oid = rc.relnamespace
        WHERE n.nspname = '_template' AND k.contype = 'f'
        ORDER BY cl.relname, k.conname
    """)).all()
    quoted = '"' + tenant_slug.replace('"', '""') + '"'
    added = 0
    for fk in fks:
        if fk.tbl not in tables or (fk.ref_schema == "_template" and fk.ref_table not in tables):
            continue
        definition = fk.condef.replace("REFERENCES _template.", f"REFERENCES {quoted}.")
        conn.execute(text(
            f'ALTER TABLE {quoted}."{fk.tbl}" ADD CONSTRAINT "{fk.conname}" {definition}'
        ))
        added += 1
    return added


def _apply_config_template(conn, tenant_slug: str, company_id: uuid.UUID) -> dict[str, int]:
    """Copies the template company's default configuration rows into the new
    company: fresh ids, company_id set to the new company, audit columns
    reset, and references between these tables (workflow -> steps)
    remapped. Tenant-agnostic by construction -- the template holds no
    people, projects or business records."""
    tables = _tenant_tables(conn, tenant_slug)
    quoted = '"' + tenant_slug.replace('"', '""') + '"'
    conn.execute(text(
        "CREATE TEMP TABLE IF NOT EXISTS _prov_idmap (old_id uuid PRIMARY KEY, new_id uuid NOT NULL) ON COMMIT DROP"
    ))
    copied: dict[str, int] = {}
    for table in CONFIG_TEMPLATE_TABLES:
        if table not in tables:
            continue  # module not enabled for this tenant
        cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"
        ), {"s": tenant_slug, "t": table}).scalars().all()
        child = _CONFIG_CHILD_KEYS.get(table)
        if child:
            where = f"s.{child[0]} IN (SELECT old_id FROM _prov_idmap)"
        else:
            where = "s.company_id = CAST(:tmpl AS uuid)"
        conn.execute(text(
            f"INSERT INTO _prov_idmap (old_id, new_id) SELECT s.id, gen_random_uuid() "
            f"FROM _template.{table} s WHERE {where} ON CONFLICT DO NOTHING"
        ), {"tmpl": TEMPLATE_COMPANY_ID})
        exprs = []
        for col in cols:
            if col == "id":
                exprs.append("m.new_id")
            elif col == "company_id":
                exprs.append("CAST(:cid AS uuid)")
            elif child and col == child[0]:
                exprs.append(f"(SELECT new_id FROM _prov_idmap WHERE old_id = s.{col})")
            elif col in ("created_at", "updated_at"):
                exprs.append("now()")
            elif col in ("created_by", "updated_by"):
                exprs.append("NULL")
            else:
                exprs.append(f"s.{col}")
        result = conn.execute(text(
            f"INSERT INTO {quoted}.{table} ({', '.join(cols)}) "
            f"SELECT {', '.join(exprs)} FROM _template.{table} s "
            f"JOIN _prov_idmap m ON m.old_id = s.id WHERE {where} ON CONFLICT DO NOTHING"
        ), {"cid": company_id, "tmpl": TEMPLATE_COMPANY_ID})
        copied[table] = result.rowcount
    return copied


def _verify_against_template(conn, tenant_slug: str) -> None:
    """Refuses to finish provisioning (-> whole transaction rolls back) if
    the new schema is missing any column, constraint or index that
    _template has for the tables it cloned."""
    conn.execute(text("SET LOCAL search_path TO pg_catalog"))
    quoted = '"' + tenant_slug.replace('"', '""') + '"'

    def shape(schema: str) -> set[str]:
        rows = conn.execute(text("""
            SELECT c.table_name || '.' || c.column_name || ':' || c.data_type || ':' || c.is_nullable
              FROM information_schema.columns c
              JOIN pg_tables t ON t.schemaname = :tenant AND t.tablename = c.table_name
             WHERE c.table_schema = :s
            UNION ALL
            SELECT cl.relname || ':' || pg_get_constraintdef(k.oid)
              FROM pg_constraint k JOIN pg_class cl ON cl.oid = k.conrelid
              JOIN pg_namespace n ON n.oid = cl.relnamespace
              JOIN pg_tables t ON t.schemaname = :tenant AND t.tablename = cl.relname
              LEFT JOIN pg_class rc ON rc.oid = k.confrelid
              LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace
             WHERE n.nspname = :s
               -- an FK into a module table this tenant didn't get is not copied
               AND (k.contype <> 'f' OR rn.nspname NOT IN ('_template', :tenant)
                    OR EXISTS (SELECT 1 FROM pg_tables x
                               WHERE x.schemaname = :tenant AND x.tablename = rc.relname))
            UNION ALL
            SELECT i.tablename || ':' || regexp_replace(i.indexdef, 'INDEX [^ ]+ ON', 'INDEX ON')
              FROM pg_indexes i
              JOIN pg_tables t ON t.schemaname = :tenant AND t.tablename = i.tablename
             WHERE i.schemaname = :s
        """), {"s": schema, "tenant": tenant_slug}).scalars().all()
        return {
            r.replace(f"{quoted}.", "").replace(f"{tenant_slug}.", "").replace("_template.", "")
            for r in rows
        }

    missing = shape("_template") - shape(tenant_slug)
    if missing:
        raise RuntimeError(
            f"Provisioned schema '{tenant_slug}' differs from _template "
            f"({len(missing)} missing, e.g. {sorted(missing)[:3]}) -- rolled back."
        )


def _copy_template_role_scopes(conn, tenant_slug: str, company_id: uuid.UUID) -> int:
    """public.provision_tenant_rbac creates the roles without their dashboard
    scope / People scope / description, which then fall back to the code's
    built-in defaults. Copy them from the template company's roles by name
    so the template is the single source (e.g. HR = People 'org-full')."""
    quoted = '"' + tenant_slug.replace('"', '""') + '"'
    result = conn.execute(text(
        f"UPDATE {quoted}.core_roles r SET dashboard_scope = t.dashboard_scope, "
        f"people_scope = t.people_scope, blurb = t.blurb "
        f"FROM _template.core_roles t "
        f"WHERE t.company_id = CAST(:tmpl AS uuid) AND t.name = r.name AND r.company_id = CAST(:cid AS uuid)"
    ), {"tmpl": TEMPLATE_COMPANY_ID, "cid": company_id})
    return result.rowcount


def _apply_rbac_template(conn, tenant_slug: str, company_id: uuid.UUID) -> None:
    conn.execute(
        text("SELECT public.provision_tenant_rbac(:slug, :cid)"),
        {"slug": tenant_slug, "cid": company_id},
    )


def _get_or_create_head_office(db, company_id: uuid.UUID) -> models.Branch:
    branch = db.scalar(
        select(models.Branch).where(models.Branch.company_id == company_id, models.Branch.code == "HQ001")
    )
    if branch is not None:
        return branch
    return crud.create_branch(
        db, company_id, schemas.BranchCreate(code="HQ001", name="Head Office", country="India")
    )


def _get_or_create_administration_dept(db, company_id: uuid.UUID) -> models.Department:
    department = db.scalar(
        select(models.Department).where(
            models.Department.company_id == company_id, models.Department.code == "ADMIN01"
        )
    )
    if department is not None:
        return department
    return crud.create_department(
        db, company_id, schemas.DepartmentCreate(code="ADMIN01", name="Administration")
    )


class _CliPlan:
    """N-08: plan chosen on the command line (resolved inside the provisioning
    transaction, where Platform Settings can be read)."""

    def __init__(self, plan: str, trial_days: int | None):
        self.plan, self.trial_days = plan, trial_days


def cli_plan_fields(db: Session, plan: str = "trial", trial_days: int | None = None) -> dict:
    """public.tenants plan fields, same rules as crud.create_hrms_admin."""
    if plan == "trial":
        days = trial_days or crud.get_or_create_platform_settings(db).trial_days
        return {"plan_type": "trial", "status": "trial",
                "trial_ends_at": datetime.date.today() + datetime.timedelta(days=days)}
    return {"plan_type": plan, "status": "active", "trial_ends_at": None}


def provision_tenant(
    tenant_slug: str,
    company_name: str,
    company_legal_name: str,
    owner_first_name: str,
    owner_last_name: str,
    owner_email: str,
    owner_password: str,
    modules: list[str] | None = None,
    tenant_fields: dict | None = None,
) -> None:
    """Atomic (C17): everything below runs inside ONE transaction on one
    connection. The Session joins it with join_transaction_mode=
    "create_savepoint", so the crud helpers' own db.commit() calls only
    release savepoints; nothing is durable until the final commit, and any
    failure rolls back the schema clone too. `tenant_fields` (plan/trial/
    contact columns) are written onto the public.tenants row in the same
    transaction (used by the Super Admin API)."""
    modules = modules or DEFAULT_MODULES
    tenant_slug = _validate_slug(tenant_slug)

    with engine.connect() as conn:
        outer = conn.begin()
        # Schema cloning is long-running DDL: lift the app engine's
        # per-connection safety timeouts (DB-03, database.py) for this
        # provisioning transaction only.
        conn.execute(text("SET LOCAL statement_timeout = 0"))
        conn.execute(text("SET LOCAL lock_timeout = 0"))
        db = Session(bind=conn, join_transaction_mode="create_savepoint", autoflush=True)
        # crud.create_employee reads the tenant slug off the Session for the
        # public.users row it writes.
        set_session_tenant_slug(db, tenant_slug)
        try:
            exists = conn.execute(
                text(
                    "SELECT 1 FROM public.tenants WHERE lower(slug) = lower(:s) "
                    "UNION ALL SELECT 1 FROM information_schema.schemata WHERE lower(schema_name) = lower(:s)"
                ),
                {"s": tenant_slug},
            ).first()
            if exists is not None:
                raise ProvisioningError("This tenant slug is already in use")

            print(f"[1/6] Registering tenant '{tenant_slug}' in public.tenants ...")
            tenant = crud.get_or_create_tenant(db, tenant_slug, company_name)
            if tenant_fields is None or isinstance(tenant_fields, _CliPlan):
                # N-08: the CLI path gets the same plan default as the Super
                # Admin API -- a trial ending after Platform Settings'
                # trial_days -- instead of an empty (unlimited) trial.
                plan = tenant_fields or _CliPlan("trial", None)
                tenant_fields = cli_plan_fields(db, plan.plan, plan.trial_days)
            for field, value in (tenant_fields or {}).items():
                setattr(tenant, field, value)
            db.flush()

            print(f"[2/6] Cloning _template tables into schema '{tenant_slug}' (modules: {modules}) ...")
            _clone_template_schema(conn, tenant_slug, modules)
            fk_count = _copy_template_foreign_keys(conn, tenant_slug)
            print(f"      {fk_count} foreign key(s) copied from _template")
            conn.execute(text(f'SET LOCAL search_path TO "{tenant_slug}", public'))

            print(f"[3/6] Creating company '{company_name}' ...")
            currency_id = db.execute(
                text("SELECT id FROM public.currencies WHERE code = 'INR' LIMIT 1")
            ).scalar()
            if currency_id is None:
                raise RuntimeError(
                    "public.currencies has no 'INR' row -- seed it before provisioning a tenant "
                    "(every company this codebase has ever created relies on this)."
                )

            company = models.Company(
                id=uuid.uuid4(),
                name=company_name,
                legal_name=company_legal_name,
                default_currency_id=currency_id,
                country="India",
            )
            db.add(company)
            db.flush()

            crud.upsert_company_settings(
                db, company.id,
                {"default_currency": "INR", "default_timezone": "IST (UTC+5:30)", "working_days_per_week": 5,
                 # N-03: default office hours, so employees without a shift
                 # get late / early flags and overtime has a window.
                 "working_hours_start": datetime.time(9, 0), "working_hours_end": datetime.time(17, 0)},
            )
            db.flush()

            print("[4/6] Applying RBAC default template from _template (all built-in roles) ...")
            _apply_rbac_template(conn, tenant_slug, company.id)
            scoped = _copy_template_role_scopes(conn, tenant_slug, company.id)
            print(f"      role scopes copied from _template for {scoped} role(s)")
            copied = _apply_config_template(conn, tenant_slug, company.id)
            print(f"      default configuration from _template: {copied}")

            print("[5/6] Bootstrapping Head Office branch + Administration department ...")
            branch = _get_or_create_head_office(db, company.id)
            department = _get_or_create_administration_dept(db, company.id)
            db.flush()

            print("[6/6] Creating the Organization Owner's employee + login ...")
            role = crud.get_role_by_name(db, company.id, "Organization Owner / CEO")
            if role is None:
                raise RuntimeError(
                    "'Organization Owner / CEO' role not found after applying the RBAC template -- "
                    "check that backend/db/seed_rbac_template.sql was run against _template."
                )

            employee = crud.create_employee(
                db, company.id,
                schemas.EmployeeCreate(
                    create_designation=True,  # L-10: the owner's title seeds the catalog
                    first_name=owner_first_name,
                    last_name=owner_last_name,
                    work_email=owner_email,
                    role_name=role.name,
                    password=owner_password,
                    date_of_joining=datetime.date.today(),
                    employment_type="full_time",
                    status="active",
                    branch_name=branch.name,
                    department_name=department.name,
                    designation_name="Chief Executive Officer",
                ),
            )
            db.flush()
            owner_login = employee.work_email
            _verify_against_template(conn, tenant_slug)
            if "hcm" in modules:
                # HRMS tenants: "needs admin password reset" flag
                # (db/add_password_reset_required.sql -- same columns).
                conn.execute(text(
                    f'ALTER TABLE "{tenant_slug}".core_users '
                    "ADD COLUMN IF NOT EXISTS password_reset_required boolean NOT NULL DEFAULT false, "
                    "ADD COLUMN IF NOT EXISTS password_reset_required_at timestamptz"
                ))
            conn.execute(text(f'SET LOCAL search_path TO "{tenant_slug}", public'))
            # Release the Session's savepoint first: with create_savepoint,
            # close() on an uncommitted Session rolls the savepoint back and
            # the tenant/company/owner rows were silently lost.
            db.commit()
            db.close()
            outer.commit()
        except Exception:
            db.close()
            outer.rollback()
            raise

    _analyze_tenant_schema(tenant_slug)

    print()
    print("=" * 70)
    print(f"Tenant '{tenant_slug}' provisioned -- company '{company_name}'.")
    print(f"Owner login: {owner_login}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Provision a brand-new tenant + Organization Owner, RBAC included, from the _template default."
    )
    parser.add_argument("--tenant", required=True, metavar="SLUG", help="New tenant slug (becomes the Postgres schema name).")
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--company-legal-name", default=None, help="Defaults to --company-name if omitted.")
    parser.add_argument("--owner-first-name", required=True)
    parser.add_argument("--owner-last-name", required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--owner-password", required=True)
    parser.add_argument(
        "--modules", default=",".join(DEFAULT_MODULES),
        help=f"Comma-separated module prefixes to clone from _template (default: {','.join(DEFAULT_MODULES)}).",
    )
    parser.add_argument("--plan", choices=("trial", "monthly", "yearly"), default="trial",
                        help="Plan written to public.tenants (default: trial, like the Super Admin API).")
    parser.add_argument("--trial-days", type=int, default=None,
                        help="Trial length in days (default: Platform Settings trial_days).")
    args = parser.parse_args()
    if args.trial_days is not None and args.trial_days < 1:
        parser.error("--trial-days must be at least 1")

    provision_tenant(
        tenant_fields=_CliPlan(args.plan, args.trial_days),
        tenant_slug=args.tenant,
        company_name=args.company_name,
        company_legal_name=args.company_legal_name or args.company_name,
        owner_first_name=args.owner_first_name,
        owner_last_name=args.owner_last_name,
        owner_email=args.owner_email,
        owner_password=args.owner_password,
        modules=[m.strip() for m in args.modules.split(",") if m.strip()],
    )
