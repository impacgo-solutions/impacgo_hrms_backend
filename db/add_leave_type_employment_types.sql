-- Contract employment, Phase 4: leave eligibility by employment type.
--
--   hcm_leave_types  + applicable_employment_types text[]
--                      DEFAULT '{full_time,part_time,intern}'
--
-- The DEFAULT fills every existing leave type at ADD COLUMN time, so every
-- existing type instantly excludes contract employees (who until now wrongly
-- drew each type's max_days_per_year) and still covers full-time / part-time
-- / intern employees exactly as before. HR opts a type back in for
-- contractors per type ('contract' in the array). Loss of Pay is never
-- gated (the leave engine handles LOP before any balance check).
--
-- Idempotent; every tenant schema + _template. Additive only.

CREATE OR REPLACE FUNCTION pg_temp.lte_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_leave_types
            ADD COLUMN IF NOT EXISTS applicable_employment_types text[] NOT NULL
                DEFAULT '{full_time,part_time,intern}'
    $q$, s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.lte_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_leave_types'
        )
    LOOP
        PERFORM pg_temp.lte_apply(tenant.slug);
    END LOOP;
END $$;
