-- Contract employment, Phase 5: contract renewals and conversions are
-- written to the employee's history (People > Transfers & Promotions /
-- Employee Profile > Lifecycle) with the before / after contract terms.
--
--   hcm_employee_lifecycle_events  + from_contract_end_date / to_contract_end_date
--                                  + from_contract_rate / to_contract_rate
--                                  + contract_rate_unit
--                                  + notes
--
-- All nullable and NULL for every existing transfer / promotion row, which
-- read and display exactly as before.
--
-- Idempotent; every tenant schema + _template. Additive only.

CREATE OR REPLACE FUNCTION pg_temp.clf_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_employee_lifecycle_events
            ADD COLUMN IF NOT EXISTS from_contract_end_date date,
            ADD COLUMN IF NOT EXISTS to_contract_end_date date,
            ADD COLUMN IF NOT EXISTS from_contract_rate numeric(12,2),
            ADD COLUMN IF NOT EXISTS to_contract_rate numeric(12,2),
            ADD COLUMN IF NOT EXISTS contract_rate_unit varchar(10),
            ADD COLUMN IF NOT EXISTS notes text
    $q$, s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.clf_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_employee_lifecycle_events'
        )
    LOOP
        PERFORM pg_temp.clf_apply(tenant.slug);
    END LOOP;
END $$;
