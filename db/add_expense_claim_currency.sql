-- Travel & Expense > Add Expense: an expense report is entered in the currency
-- the employee picks (ISO 4217, any of app/currencies.py). The code is stored
-- next to the amounts -- on the report (hcm_expense_claims.currency_code, the
-- currency of every line, tax and the net total) and on each expense row
-- (hcm_expense_claim_lines.currency_code). Nothing is ever converted.
-- Existing reports were entered and shown in rupees, so they are backfilled
-- as INR (that records what was entered; it is not a conversion).
-- Idempotent; _template plus every HRMS tenant schema.
--
--     cd backend && python db/run_hrms_migration.py db/add_expense_claim_currency.sql [--dry-run]

DO $$
DECLARE
    s text;
    t text;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        FOREACH t IN ARRAY ARRAY['hcm_expense_claims', 'hcm_expense_claim_lines'] LOOP
            IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                           WHERE table_schema = s AND table_name = t) THEN
                CONTINUE;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_schema = s AND table_name = t AND column_name = 'currency_code') THEN
                EXECUTE format('ALTER TABLE %1$I.%2$I ADD COLUMN currency_code varchar(3) NOT NULL DEFAULT ''INR''', s, t);
            END IF;
            RAISE NOTICE '%.currency_code ready for %', t, s;
        END LOOP;
    END LOOP;
END $$;
