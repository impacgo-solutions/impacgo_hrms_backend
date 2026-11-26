-- Contract expiry: contract start date, contract status (active / expired),
-- who/when/why it expired, and the per-company expiry-alert lead time.
-- An expired contract blocks the employee's login until it's extended
-- (app/access_lifecycle.contract_access_blocked). Idempotent; _template plus
-- every provisioned tenant. Existing contract employees are backfilled as
-- 'active' with contract_start_date = date_of_joining.

ALTER TABLE _template.core_employees
    ADD COLUMN IF NOT EXISTS contract_start_date date,
    ADD COLUMN IF NOT EXISTS contract_status varchar(10),
    ADD COLUMN IF NOT EXISTS contract_expired_at timestamptz,
    ADD COLUMN IF NOT EXISTS contract_expired_by uuid,
    ADD COLUMN IF NOT EXISTS contract_expiry_reason varchar(10);
ALTER TABLE _template.core_company_settings
    ADD COLUMN IF NOT EXISTS contract_expiry_notice_days smallint;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    -- Skip tenant rows whose schema was never provisioned (or was dropped).
    FOR tenant IN SELECT t.slug FROM public.tenants t
                  WHERE EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname = t.slug) LOOP
        EXECUTE format(
            'ALTER TABLE %I.core_employees
                ADD COLUMN IF NOT EXISTS contract_start_date date,
                ADD COLUMN IF NOT EXISTS contract_status varchar(10),
                ADD COLUMN IF NOT EXISTS contract_expired_at timestamptz,
                ADD COLUMN IF NOT EXISTS contract_expired_by uuid,
                ADD COLUMN IF NOT EXISTS contract_expiry_reason varchar(10)',
            tenant.slug
        );
        EXECUTE format(
            'ALTER TABLE %I.core_company_settings
                ADD COLUMN IF NOT EXISTS contract_expiry_notice_days smallint',
            tenant.slug
        );
        EXECUTE format(
            'UPDATE %I.core_employees
                SET contract_status = COALESCE(contract_status, ''active''),
                    contract_start_date = COALESCE(contract_start_date, date_of_joining)
              WHERE lower(replace(replace(employment_type, ''-'', '' ''), ''_'', '' '')) IN (''contract'', ''fixed term'')',
            tenant.slug
        );
        RAISE NOTICE 'contract expiry columns added for tenant %', tenant.slug;
    END LOOP;
END $$;
