-- Payroll > Salary Structure > Reimbursement Components -- Travel
-- Reimbursement / Expense Reimbursement as configurable payroll components,
-- plus the review state HR needs before a payroll run is paid out.
--
-- hcm_payroll_reimbursement_components: one optional row per company per
-- source type ('travel_request' | 'expense_claim'). No row = the built-in
-- default (enabled, NOT auto-included -- exactly the behaviour before this
-- table existed: only records HR schedules by hand are paid). No rows are
-- inserted here; HR creates them from the Salary Structure screen.
--
-- hcm_payroll_reimbursement_inclusions gains:
--   status          'included' | 'excluded' -- an excluded record keeps its
--                   row (so the UNIQUE (source_type, source_id) guard still
--                   stops it being re-added) but is never paid.
--   approved_amount the source record's approved amount when it was
--                   scheduled; `amount` is what payroll pays (HR may lower
--                   it, never raise it above approved_amount).
--   auto_included   true when a payroll run's automatic identification
--                   (component auto_include) created the row.
--   notes           HR's review note (edit / exclude / defer reason).
-- Deferring = moving target_period_month/year; the change history is kept
-- in core_audit_logs (before/after values).

CREATE TABLE IF NOT EXISTS _template.hcm_payroll_reimbursement_components (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    source_type varchar(20) NOT NULL,
    display_name varchar(40) NOT NULL,
    is_enabled boolean NOT NULL DEFAULT true,
    auto_include boolean NOT NULL DEFAULT false,
    max_amount_per_request numeric(14, 2),
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_payroll_reimbursement_component UNIQUE (company_id, source_type),
    CONSTRAINT ck_payroll_reimbursement_component_type
        CHECK (source_type IN ('travel_request', 'expense_claim'))
);

ALTER TABLE _template.hcm_payroll_reimbursement_inclusions
    ADD COLUMN IF NOT EXISTS status varchar(12) NOT NULL DEFAULT 'included',
    ADD COLUMN IF NOT EXISTS approved_amount numeric(14, 2),
    ADD COLUMN IF NOT EXISTS auto_included boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS notes text;
UPDATE _template.hcm_payroll_reimbursement_inclusions
    SET approved_amount = amount WHERE approved_amount IS NULL;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_payroll_reimbursement_inclusions'
        )
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.hcm_payroll_reimbursement_components (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
                source_type varchar(20) NOT NULL,
                display_name varchar(40) NOT NULL,
                is_enabled boolean NOT NULL DEFAULT true,
                auto_include boolean NOT NULL DEFAULT false,
                max_amount_per_request numeric(14, 2),
                created_by uuid,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_by uuid,
                updated_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT uq_payroll_reimbursement_component UNIQUE (company_id, source_type),
                CONSTRAINT ck_payroll_reimbursement_component_type
                    CHECK (source_type IN (''travel_request'', ''expense_claim''))
            )',
            tenant.slug
        );
        EXECUTE format(
            'ALTER TABLE %1$I.hcm_payroll_reimbursement_inclusions
                ADD COLUMN IF NOT EXISTS status varchar(12) NOT NULL DEFAULT ''included'',
                ADD COLUMN IF NOT EXISTS approved_amount numeric(14, 2),
                ADD COLUMN IF NOT EXISTS auto_included boolean NOT NULL DEFAULT false,
                ADD COLUMN IF NOT EXISTS notes text',
            tenant.slug
        );
        EXECUTE format(
            'UPDATE %1$I.hcm_payroll_reimbursement_inclusions
                SET approved_amount = amount WHERE approved_amount IS NULL',
            tenant.slug
        );
    END LOOP;
END $$;
