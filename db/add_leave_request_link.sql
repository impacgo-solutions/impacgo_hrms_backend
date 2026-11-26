-- Adds a self-referencing link column to hcm_leave_requests, so a leave
-- request that had to be auto-split across two leave types (e.g. an
-- employee requests 8 days of Sick Leave with only 3 remaining -> 3 days
-- Sick Leave + 5 days Loss of Pay, submitted together) can be decided as
-- one unit: approving/rejecting either half applies the same decision to
-- its linked sibling.
--
-- NOT executed by the assistant -- review and run by hand, same convention
-- as every other file in backend/db/.
--
-- hcm_leave_requests lives per-tenant-schema (same as core_roles), so this
-- follows the same DO $$ ... FOREACH s IN ARRAY schemas LOOP EXECUTE
-- format(...) pattern as backend/db/add_role_scope_columns.sql --
-- _template plus every schema already in public.tenants.
--
-- No FK constraint is added -- this table's existing leave_type_id column
-- already isn't enforced as a live FK at the DB level either (confirmed
-- directly: deleting an in-use leave type previously succeeded and
-- orphaned referencing rows, see the delete_leave_type application-level
-- guard added earlier today), so a self-referencing FK here would be the
-- only enforced constraint on this table and inconsistent with it.
-- Application code (backend/app/routers/leave.py) is responsible for
-- keeping both sides of the link consistent, same as everywhere else in
-- this table.
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS.
--
-- Not every tenant schema actually has hcm_leave_requests -- provision_tenant()
-- only creates tables for a tenant's enabled_modules, and confirmed directly
-- against this database that "impacgo" (and possibly others) were never
-- provisioned with the leave/HCM module. A single DO block is one
-- transaction, so hitting a missing table partway through would otherwise
-- roll back every schema already altered in the same run (confirmed this
-- happened on the first attempt at this script) -- each schema is checked
-- against information_schema.tables first and skipped, not aborted, if the
-- table doesn't exist there.

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
            WHERE table_schema = s AND table_name = 'hcm_leave_requests'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.hcm_leave_requests
                    ADD COLUMN IF NOT EXISTS linked_leave_request_id uuid',
                s
            );
            RAISE NOTICE 'linked_leave_request_id ensured for schema %', s;
        ELSE
            RAISE NOTICE 'Schema % has no hcm_leave_requests table -- skipped', s;
        END IF;
    END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- Verification queries to run afterward (read-only)
-- ----------------------------------------------------------------------------
-- SELECT column_name FROM information_schema.columns
--   WHERE table_schema = 'Infyq' AND table_name = 'hcm_leave_requests'
--     AND column_name = 'linked_leave_request_id';
--   -- should return exactly one row.
