-- Contract employment, Phase 3 (payroll): a contract employee's current
-- rate, copied from the accepted contract offer at hire and changed only by
-- an HR contract renewal. Payroll reads it for hourly / daily contractors
-- (approved timesheet hours or days x rate) instead of the salary
-- structure's CTC / 12.
--
--   core_employees  + contract_rate_amount numeric(12,2)
--                   + contract_rate_unit   'hourly' | 'daily' | 'monthly'
--
-- NULL for every existing employee and every non-contract hire -- no
-- backfill; payroll for those employees is unchanged.
--
-- Idempotent; every tenant schema + _template. Additive only.

CREATE OR REPLACE FUNCTION pg_temp.ecr_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        ALTER TABLE %1$I.core_employees
            ADD COLUMN IF NOT EXISTS contract_rate_amount numeric(12,2),
            ADD COLUMN IF NOT EXISTS contract_rate_unit varchar(10)
    $q$, s);
    IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                   WHERE n.nspname = s AND c.conname = 'core_employees_contract_rate_unit_check') THEN
        EXECUTE format($q$ALTER TABLE %1$I.core_employees ADD CONSTRAINT core_employees_contract_rate_unit_check
            CHECK (contract_rate_unit IN ('hourly', 'daily', 'monthly'))$q$, s);
    END IF;
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.ecr_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'core_employees'
        )
    LOOP
        PERFORM pg_temp.ecr_apply(tenant.slug);
    END LOOP;
END $$;
