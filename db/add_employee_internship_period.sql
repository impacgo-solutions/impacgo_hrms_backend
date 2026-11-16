-- Add Employee > Internship Period (Intern employees only).
--
--   core_employees  + internship_duration       integer   (1..), e.g. 3
--                   + internship_duration_unit  'days' | 'months'
--                   + internship_end_date       date      -- computed from
--                     date_of_joining + duration (inclusive: 3 months from
--                     1 Jun 2026 ends 31 Aug 2026; 30 days ends 30 Jun 2026)
--
-- NULL for every existing employee and every non-intern hire -- no backfill.
-- Idempotent; _template + every HRMS tenant (pg_temp.hrms_schemas(), created
-- by db/run_hrms_migration.py). Additive only.

DO $$
DECLARE
    s text;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema = s AND table_name = 'core_employees') THEN
            CONTINUE;
        END IF;
        EXECUTE format($q$
            ALTER TABLE %1$I.core_employees
                ADD COLUMN IF NOT EXISTS internship_duration integer,
                ADD COLUMN IF NOT EXISTS internship_duration_unit varchar(6),
                ADD COLUMN IF NOT EXISTS internship_end_date date
        $q$, s);
        IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                       WHERE n.nspname = s AND c.conname = 'core_employees_internship_unit_check') THEN
            EXECUTE format($q$ALTER TABLE %1$I.core_employees ADD CONSTRAINT core_employees_internship_unit_check
                CHECK (internship_duration_unit IN ('days', 'months'))$q$, s);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                       WHERE n.nspname = s AND c.conname = 'core_employees_internship_duration_check') THEN
            EXECUTE format($q$ALTER TABLE %1$I.core_employees ADD CONSTRAINT core_employees_internship_duration_check
                CHECK (internship_duration IS NULL OR internship_duration > 0)$q$, s);
        END IF;
        RAISE NOTICE 'internship period columns ready in %', s;
    END LOOP;
END
$$;
