-- Administration > General Settings > Employee ID Format: per employment
-- type, whether the Employee ID is auto-generated from the number series
-- ('auto', the default -- exactly today's behavior for every existing row)
-- or typed in by hand on Add Employee ('manual').
--
-- Lives on core_number_series because that's already where each type's
-- format lives (doctype 'employee' = the shared default, 'employee:<type>'
-- = a type-specific override). A type with no override row inherits the
-- default row's mode as well as its format.
--
-- Idempotent; same convention as add_employee_number_series_suffix.sql:
-- the _template schema plus every already-provisioned tenant schema.

ALTER TABLE _template.core_number_series
    ADD COLUMN IF NOT EXISTS generation_mode varchar(10) NOT NULL DEFAULT 'auto';

DO $$
DECLARE
    tenant RECORD;
BEGIN
    -- Skip tenant rows whose schema was never provisioned (or was dropped).
    FOR tenant IN SELECT t.slug FROM public.tenants t
                  WHERE EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname = t.slug) LOOP
        EXECUTE format(
            'ALTER TABLE %I.core_number_series
                ADD COLUMN IF NOT EXISTS generation_mode varchar(10) NOT NULL DEFAULT ''auto''',
            tenant.slug
        );
        RAISE NOTICE 'core_number_series.generation_mode added for tenant %', tenant.slug;
    END LOOP;
END $$;
