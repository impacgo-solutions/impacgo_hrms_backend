-- Assets > Asset Recovery Deduction -- a brand-new concept: an approved
-- rupee amount to recover from one employee (e.g. an unreturned/damaged
-- asset), targeted at a specific payroll period, picked up automatically
-- by crud._compute_and_write_slip as a real payslip deduction line when
-- Payroll Settings > Deductions Configuration has asset_deduction_enabled
-- turned on. No prior table answered "how much to deduct this month for
-- employee X's asset" -- see models.AssetRecoveryDeduction.
--
-- applied_payroll_run_id is the duplicate-prevention guard: once a
-- payroll run has consumed this row, it's excluded from every other run's
-- eligibility query (crud.get_approved_asset_recovery_amount), so the same
-- approved amount is never deducted twice across two different months'
-- payroll runs. It stays NULL (and re-eligible) for a still-draft run
-- regenerated in place, since that query also matches the run that
-- already consumed it.

CREATE TABLE IF NOT EXISTS _template.hcm_asset_recovery_deductions (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    employee_id uuid NOT NULL REFERENCES _template.core_employees(id),
    asset_assignment_id uuid REFERENCES _template.hcm_asset_assignments(id),
    amount numeric(12, 2) NOT NULL,
    reason text,
    status varchar(12) NOT NULL DEFAULT 'pending',
    target_period_month smallint NOT NULL,
    target_period_year smallint NOT NULL,
    approver_id uuid REFERENCES _template.core_employees(id),
    decision_notes text,
    decided_at timestamptz,
    applied_payroll_run_id uuid REFERENCES _template.hcm_payroll_runs(id),
    applied_at timestamptz,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid,
    updated_at timestamptz NOT NULL DEFAULT now()
);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.hcm_asset_recovery_deductions (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
                employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
                asset_assignment_id uuid REFERENCES %1$I.hcm_asset_assignments(id),
                amount numeric(12, 2) NOT NULL,
                reason text,
                status varchar(12) NOT NULL DEFAULT ''pending'',
                target_period_month smallint NOT NULL,
                target_period_year smallint NOT NULL,
                approver_id uuid REFERENCES %1$I.core_employees(id),
                decision_notes text,
                decided_at timestamptz,
                applied_payroll_run_id uuid REFERENCES %1$I.hcm_payroll_runs(id),
                applied_at timestamptz,
                created_by uuid,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_by uuid,
                updated_at timestamptz NOT NULL DEFAULT now()
            )',
            tenant.slug
        );
        RAISE NOTICE 'hcm_asset_recovery_deductions ensured for tenant %', tenant.slug;
    END LOOP;
END $$;
