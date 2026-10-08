-- Payroll > Travel & Expense Reimbursements -- once a Travel Requisition
-- (hcm.travel_requests) or Expense Report/Reimbursement (hcm.expense_claims,
-- shared by both flows) is fully approved (status='approved'), HR schedules
-- its real approved amount into a specific payroll period here. Picked up
-- automatically by crud._compute_and_write_slip as a real, separate
-- reimbursement line -- never folded into gross_pay/total_deductions, so
-- it's never mistaken for salary or a deduction.
--
-- source_id is polymorphic (points at either hcm_travel_requests.id or
-- hcm_expense_claims.id depending on source_type) -- no single FK target is
-- possible, same convention already used by this app's other polymorphic
-- lookups (e.g. comments/attachments keyed by entity_type+entity_id).
--
-- The UNIQUE constraint on (source_type, source_id) is the duplicate-
-- inclusion guard: one approved travel/expense record can only ever be
-- scheduled into payroll once. applied_payroll_run_id is the separate
-- duplicate-PAYOUT guard (same pattern as hcm_asset_recovery_deductions):
-- once a payroll run has consumed this row, it's excluded from every other
-- run's eligibility query, but stays eligible for a still-draft run being
-- regenerated in place.

ALTER TABLE _template.hcm_salary_slips
    ADD COLUMN IF NOT EXISTS reimbursements_total numeric(14, 2) NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS _template.hcm_payroll_reimbursement_inclusions (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    employee_id uuid NOT NULL REFERENCES _template.core_employees(id),
    source_type varchar(20) NOT NULL,
    source_id uuid NOT NULL,
    amount numeric(14, 2) NOT NULL,
    target_period_month smallint NOT NULL,
    target_period_year smallint NOT NULL,
    applied_payroll_run_id uuid REFERENCES _template.hcm_payroll_runs(id),
    applied_at timestamptz,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_payroll_reimbursement_inclusion_source UNIQUE (source_type, source_id)
);

CREATE TABLE IF NOT EXISTS _template.hcm_salary_slip_reimbursement_lines (
    id uuid PRIMARY KEY,
    slip_id uuid NOT NULL REFERENCES _template.hcm_salary_slips(id),
    inclusion_id uuid NOT NULL REFERENCES _template.hcm_payroll_reimbursement_inclusions(id),
    label varchar(40) NOT NULL,
    amount numeric(14, 2) NOT NULL
);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        EXECUTE format(
            'ALTER TABLE %1$I.hcm_salary_slips
                ADD COLUMN IF NOT EXISTS reimbursements_total numeric(14, 2) NOT NULL DEFAULT 0',
            tenant.slug
        );
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.hcm_payroll_reimbursement_inclusions (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
                employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
                source_type varchar(20) NOT NULL,
                source_id uuid NOT NULL,
                amount numeric(14, 2) NOT NULL,
                target_period_month smallint NOT NULL,
                target_period_year smallint NOT NULL,
                applied_payroll_run_id uuid REFERENCES %1$I.hcm_payroll_runs(id),
                applied_at timestamptz,
                created_by uuid,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_by uuid,
                updated_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT uq_payroll_reimbursement_inclusion_source UNIQUE (source_type, source_id)
            )',
            tenant.slug
        );
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.hcm_salary_slip_reimbursement_lines (
                id uuid PRIMARY KEY,
                slip_id uuid NOT NULL REFERENCES %1$I.hcm_salary_slips(id),
                inclusion_id uuid NOT NULL REFERENCES %1$I.hcm_payroll_reimbursement_inclusions(id),
                label varchar(40) NOT NULL,
                amount numeric(14, 2) NOT NULL
            )',
            tenant.slug
        );
        RAISE NOTICE 'payroll reimbursement inclusions ensured for tenant %', tenant.slug;
    END LOOP;
END $$;
