-- Administration > General Settings > Weekly Off Days: the company's chosen
-- weekly holidays, as comma-separated lowercase weekday names
-- ('saturday,sunday', 'sunday', 'friday', ...; '' = no weekly off).
-- NULL = not chosen yet: the app keeps deriving them from
-- working_days_per_week exactly as before (6 or 7 = Sunday off, otherwise
-- Saturday + Sunday) -- see app/weekly_off.py. No backfill, so nothing
-- changes for any company until it saves a choice.
-- Idempotent; _template plus every HRMS tenant schema.
--
--     cd backend && venv/Scripts/python db/run_hrms_migration.py db/add_weekly_off_days.sql

DO $$
DECLARE
    s text;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema = s AND table_name = 'core_company_settings') THEN
            CONTINUE;
        END IF;
        EXECUTE format(
            'ALTER TABLE %1$I.core_company_settings ADD COLUMN IF NOT EXISTS weekly_off_days varchar(80)', s);
        RAISE NOTICE 'core_company_settings.weekly_off_days ready for %', s;
    END LOOP;
END $$;
