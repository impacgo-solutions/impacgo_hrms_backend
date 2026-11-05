-- Payroll Settings > Deductions Configuration: two per-company toggles
-- gating crud._compute_and_write_slip's new LOP / Asset-recovery deduction
-- lines. Both default false so no existing tenant's payslip figures change
-- until an HR/Organization Owner explicitly turns either on.

ALTER TABLE _template.core_company_settings
    ADD COLUMN IF NOT EXISTS lop_deduction_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS asset_deduction_enabled boolean NOT NULL DEFAULT false;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        EXECUTE format(
            'ALTER TABLE %I.core_company_settings
                ADD COLUMN IF NOT EXISTS lop_deduction_enabled boolean NOT NULL DEFAULT false,
                ADD COLUMN IF NOT EXISTS asset_deduction_enabled boolean NOT NULL DEFAULT false',
            tenant.slug
        );
        RAISE NOTICE 'payroll deduction settings ensured for tenant %', tenant.slug;
    END LOOP;
END $$;
