-- ============================================================================
-- Remove core_employees.team -- REVIEW AND RUN BY HAND. Not executed by the
-- assistant. Optional: the application no longer reads or writes this
-- column at all (models.Employee has no `team` mapped_column, and every
-- schema/API that referenced it has been removed) -- this script just lets
-- you drop the now-unused column from the database too, if/when you want a
-- fully clean schema. Nothing breaks if you leave the column in place
-- indefinitely; it's simply orphaned data no code path touches anymore.
-- ============================================================================
--
-- Removed as part of: dropping the "Team" field from Employee Profile >
-- Edit Professional Info (frontend + backend). See:
--   backend/app/models.py        -- Employee.team mapped_column removed
--   backend/app/schemas.py       -- EmployeeListItem/EmployeeOut/EmployeeCreate/
--                                    EmployeeOrgUpdate.team fields removed
--   backend/app/crud.py          -- every "team": _dash(e.team) site removed
--   backend/app/routers/employees.py -- _to_out + update_employee_org removed
--   lib/models/employee.dart, lib/widgets/employee_profile/
--     professional_info_section.dart, lib/data/repository/
--     employee_api_repository.dart -- frontend field/UI/repository removed
--
-- Loops _template + every schema registered in public.tenants, same
-- convention as backend/db/backfill_core_bands_existing_tenants.sql and
-- backend/db/add_administration_audit_columns.sql. DROP COLUMN IF EXISTS is
-- idempotent -- safe to re-run, and safe to run against a schema that
-- somehow never had the column (e.g. one provisioned from a future
-- `_template` that has already had this column removed).
--
-- Not destructive in the sense of dropping ROWS -- this only ever discards
-- the `team` value that was stored for each employee, which the app has
-- already stopped reading/writing/exposing everywhere. If you want to keep
-- that historical data for any reason, don't run this file -- everything
-- else in this removal works identically whether or not this last step is
-- ever taken.

DO $$
DECLARE
    s text;
BEGIN
    FOR s IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name = '_template'
           OR schema_name IN (SELECT slug FROM public.tenants)
        ORDER BY schema_name
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = s AND table_name = 'core_employees' AND column_name = 'team'
        ) THEN
            EXECUTE format('ALTER TABLE %I.core_employees DROP COLUMN team', s);
            RAISE NOTICE '  [%] DROPPED %.core_employees.team', s, s;
        ELSE
            RAISE NOTICE '  [%] SKIP -- %.core_employees has no team column', s, s;
        END IF;
    END LOOP;
END $$;


-- ----------------------------------------------------------------------------
-- Verification (read-only) -- expect zero rows.
-- ----------------------------------------------------------------------------
SELECT table_schema, table_name, column_name
FROM information_schema.columns
WHERE table_name = 'core_employees' AND column_name = 'team';
