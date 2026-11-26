-- Contract employment, Phase 2: the employee record carries the contract's
-- end date (copied from the accepted contract offer when the joiner is
-- created as an employee; renewed / cleared later by HR).
--
--   core_employees  + contract_end_date date (nullable)
--
-- NULL for every existing employee and every non-contract hire -- no
-- backfill, nothing else changes.
--
-- Idempotent; every tenant schema + _template. Additive only.

CREATE OR REPLACE FUNCTION pg_temp.ecd_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format('ALTER TABLE %1$I.core_employees ADD COLUMN IF NOT EXISTS contract_end_date date', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_employees_contract_end ON %1$I.core_employees (contract_end_date) '
                   'WHERE contract_end_date IS NOT NULL', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.ecd_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'core_employees'
        )
    LOOP
        PERFORM pg_temp.ecd_apply(tenant.slug);
    END LOOP;
END $$;
