-- Contract / contractor offers (Recruitment > Offer, Phase 1 of contract
-- employment support): an offer for a contract role is made on a rate
-- (hourly / daily / monthly) for a fixed term instead of an annual CTC with
-- probation and notice period.
--
--   hcm_offers  + compensation_type  'ctc' (default) | 'rate'
--               + rate_amount        the contract rate
--               + rate_unit          'hourly' | 'daily' | 'monthly'
--               + contract_duration_months
--               + contract_end_date
--
-- compensation_type DEFAULT 'ctc' tags every existing offer row correctly
-- at ADD COLUMN time (no backfill); the other columns are nullable and stay
-- NULL for every existing / non-contract offer, so nothing about a
-- full-time / part-time / intern offer changes.
--
-- Idempotent; every tenant schema + _template. No existing row is deleted
-- and no existing column/value is altered -- additive only.

CREATE OR REPLACE FUNCTION pg_temp.oct_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_offers
            ADD COLUMN IF NOT EXISTS compensation_type varchar(10) NOT NULL DEFAULT 'ctc',
            ADD COLUMN IF NOT EXISTS rate_amount numeric(12,2),
            ADD COLUMN IF NOT EXISTS rate_unit varchar(10),
            ADD COLUMN IF NOT EXISTS contract_duration_months smallint,
            ADD COLUMN IF NOT EXISTS contract_end_date date
    $q$, s);
    -- CHECK constraints added separately so a re-run is a no-op.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                   WHERE n.nspname = s AND c.conname = 'hcm_offers_compensation_type_check') THEN
        EXECUTE format($q$ALTER TABLE %1$I.hcm_offers ADD CONSTRAINT hcm_offers_compensation_type_check
            CHECK (compensation_type IN ('ctc', 'rate'))$q$, s);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                   WHERE n.nspname = s AND c.conname = 'hcm_offers_rate_unit_check') THEN
        EXECUTE format($q$ALTER TABLE %1$I.hcm_offers ADD CONSTRAINT hcm_offers_rate_unit_check
            CHECK (rate_unit IN ('hourly', 'daily', 'monthly'))$q$, s);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                   WHERE n.nspname = s AND c.conname = 'hcm_offers_contract_duration_check') THEN
        EXECUTE format($q$ALTER TABLE %1$I.hcm_offers ADD CONSTRAINT hcm_offers_contract_duration_check
            CHECK (contract_duration_months IS NULL OR contract_duration_months BETWEEN 1 AND 120)$q$, s);
    END IF;
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.oct_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_offers'
        )
    LOOP
        PERFORM pg_temp.oct_apply(tenant.slug);
    END LOOP;
END $$;
