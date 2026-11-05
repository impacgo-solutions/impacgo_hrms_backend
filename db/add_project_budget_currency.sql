-- Create / Edit Project > Currency: a project budget is its amount plus an
-- ISO 4217 code (INR, USD, EUR, GBP, ...), stored separately; nothing is
-- converted. Existing budgets were entered and shown in rupees (the old field
-- displayed "₹"), so they are backfilled as INR -- that records what was
-- entered, it is not a conversion. New budgets carry whatever currency the
-- user picks.
-- Idempotent; _template plus every HRMS tenant schema.
--
--     cd backend && venv/Scripts/python db/run_hrms_migration.py db/add_project_budget_currency.sql

DO $$
DECLARE
    s text;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema = s AND table_name = 'pm_project_budgets') THEN
            CONTINUE;
        END IF;
        -- Backfill only when the column is first added, so a re-run never
        -- labels a later currency-less budget as INR.
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema = s AND table_name = 'pm_project_budgets'
                         AND column_name = 'currency_code') THEN
            EXECUTE format('ALTER TABLE %1$I.pm_project_budgets ADD COLUMN currency_code varchar(3)', s);
            EXECUTE format('UPDATE %1$I.pm_project_budgets SET currency_code = ''INR''', s);
        END IF;
        RAISE NOTICE 'pm_project_budgets.currency_code ready for %', s;
    END LOOP;
END $$;
