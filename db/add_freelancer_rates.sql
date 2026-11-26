-- Freelancer hourly rates: effective-dated rate history per employee (the
-- current rate stays on core_employees.contract_rate_amount/_unit, which
-- payroll already reads for rate-paid workers) and an optional company-wide
-- TDS % on freelancer fees (Payroll > Deductions Settings).
--
-- Idempotent; same convention as add_notification_settings.sql: the
-- _template schema plus every already-provisioned tenant schema.

CREATE TABLE IF NOT EXISTS _template.hcm_employee_rate_history (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    employee_id uuid NOT NULL REFERENCES _template.core_employees(id),
    rate_amount numeric(12,2) NOT NULL,
    rate_unit varchar(10) NOT NULL,
    effective_from date NOT NULL,
    notes text NULL,
    created_by uuid NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT hcm_employee_rate_history_unique UNIQUE (employee_id, effective_from)
);
CREATE INDEX IF NOT EXISTS ix_hcm_employee_rate_history_employee
    ON _template.hcm_employee_rate_history(employee_id);

ALTER TABLE _template.core_company_settings
    ADD COLUMN IF NOT EXISTS freelancer_tds_percent numeric(5,2) NULL;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    -- Skip tenant rows whose schema was never provisioned (or was dropped).
    FOR tenant IN SELECT t.slug FROM public.tenants t
                  WHERE EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname = t.slug) LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.hcm_employee_rate_history (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %I.core_companies(id),
                employee_id uuid NOT NULL REFERENCES %I.core_employees(id),
                rate_amount numeric(12,2) NOT NULL,
                rate_unit varchar(10) NOT NULL,
                effective_from date NOT NULL,
                notes text NULL,
                created_by uuid NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT hcm_employee_rate_history_unique UNIQUE (employee_id, effective_from)
            )',
            tenant.slug, tenant.slug, tenant.slug
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS ix_hcm_employee_rate_history_employee ON %I.hcm_employee_rate_history(employee_id)',
            tenant.slug
        );
        EXECUTE format(
            'ALTER TABLE %I.core_company_settings ADD COLUMN IF NOT EXISTS freelancer_tds_percent numeric(5,2) NULL',
            tenant.slug
        );
        RAISE NOTICE 'freelancer rates added for tenant %', tenant.slug;
    END LOOP;
END $$;
