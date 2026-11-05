-- Backfills the fin_fiscal_years table into every tenant schema that has
-- the HCM module (hcm_leave_allocations) but is missing it.
--
-- Why this is needed: crud.get_or_create_fiscal_year (called from
-- routers/leave.py's create_leave_balance) reads/writes fin_fiscal_years --
-- a table that belongs to the Finance module, not HCM -- for every leave
-- allocation created. public.provision_tenant() only clones tables whose
-- name is prefixed with a requested module code, so a tenant provisioned
-- with modules=['core','hcm','pm'] (the exact DEFAULT_MODULES used by
-- backend/app/provision_tenant.py, and confirmed as the real set the
-- "Infyq" tenant was provisioned with) never gets fin_fiscal_years at all.
-- Confirmed directly: "Infyq" has hcm_leave_allocations but not
-- fin_fiscal_years -- POST/PATCH /api/leave-balances would 500 there today.
--
-- This is a systemic gap, not a one-off -- it would recur for every future
-- tenant provisioned the standard way. provision_tenant.py's
-- _clone_template_schema has been fixed in code to always additionally
-- clone fin_fiscal_years whenever 'hcm' is requested, so this migration
-- only needs to backfill tenants that already exist.
--
-- NOT executed by the assistant -- review and run by hand, same convention
-- as every other file in backend/db/.
--
-- Safe to re-run: CREATE TABLE IF NOT EXISTS.
--
-- No FK/data is copied -- CREATE TABLE ... LIKE _template.fin_fiscal_years
-- INCLUDING ALL clones only column structure/defaults/indexes (this schema
-- uses no FKs anywhere, confirmed against the live database), same as
-- every table public.provision_tenant() itself creates. Each tenant starts
-- with zero fiscal year rows -- get_or_create_fiscal_year creates one
-- on-demand the first time a leave allocation is made for that tenant,
-- exactly like it already does for every tenant that already has this
-- table (acme, _template).

DO $$
DECLARE
    schemas text[];
    s text;
BEGIN
    SELECT array_agg(slug) INTO schemas FROM (
        SELECT '_template'::text AS slug
        UNION ALL
        SELECT slug FROM public.tenants
    ) x;

    FOREACH s IN ARRAY schemas LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = s AND table_name = 'hcm_leave_allocations'
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = s AND table_name = 'fin_fiscal_years'
        ) THEN
            EXECUTE format(
                'CREATE TABLE %I.fin_fiscal_years (LIKE _template.fin_fiscal_years INCLUDING ALL)',
                s
            );
            RAISE NOTICE 'fin_fiscal_years created for HCM-enabled schema %', s;
        ELSIF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = s AND table_name = 'hcm_leave_allocations'
        ) THEN
            RAISE NOTICE 'Schema % already has fin_fiscal_years -- skipped', s;
        ELSE
            RAISE NOTICE 'Schema % has no hcm_leave_allocations (HCM not enabled) -- skipped', s;
        END IF;
    END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- Verification queries to run afterward (read-only)
-- ----------------------------------------------------------------------------
-- SELECT table_schema FROM information_schema.tables
--   WHERE table_name = 'fin_fiscal_years' ORDER BY table_schema;
--   -- should now include every schema that has hcm_leave_allocations
--   -- (at minimum: _template, Infyq, acme).
