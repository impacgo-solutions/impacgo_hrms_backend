-- Payroll QA fixes (H-11..H-16, M-28, M-30, M-31, N-06). Idempotent.
-- Run: venv/Scripts/python db/run_hrms_migration.py db/2026_10_06_payroll_period_control.sql [--dry-run]
--
--  * hcm_payroll_runs: maker-checker / period-control columns
--    (draft -> processed(generated) -> approved -> locked -> paid).
--  * hcm_salary_slips: payable days (proration), flags and the deduction
--    shortfall carried to the next run (net pay never < 0).
--  * hcm_payroll_arrears: back-dated differences of locked runs, posted in a
--    later run.
--  * hcm_loan_recoveries: EMI recovered per slip; applied to the loan's
--    outstanding balance when the run is locked.
--  * hcm_tax_declarations: more statutory deductions (80D, 80CCD(1B), 24(b))
--    + CHECK >= 0.
--  * Statutory PF / ESI salary components for every company that lacks them
--    (incl. the _template company, which provisioning copies).
--  * TDS-EST component renamed: it is now the statutory TDS line.
--  * generated_by back-filled from the audit log for already-generated runs.

DO $$ DECLARE s text; neg int; BEGIN
  FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
    IF to_regclass(format('%I.core_employees', s)) IS NULL
       OR to_regclass(format('%I.hcm_payroll_runs', s)) IS NULL THEN CONTINUE; END IF;

    EXECUTE format($q$ALTER TABLE %I.hcm_payroll_runs
        ADD COLUMN IF NOT EXISTS generated_by uuid,
        ADD COLUMN IF NOT EXISTS generated_at timestamptz,
        ADD COLUMN IF NOT EXISTS approved_by uuid,
        ADD COLUMN IF NOT EXISTS approved_at timestamptz,
        ADD COLUMN IF NOT EXISTS locked_by uuid,
        ADD COLUMN IF NOT EXISTS locked_at timestamptz,
        ADD COLUMN IF NOT EXISTS paid_by uuid,
        ADD COLUMN IF NOT EXISTS paid_at timestamptz,
        ADD COLUMN IF NOT EXISTS flagged_slips integer NOT NULL DEFAULT 0$q$, s);

    EXECUTE format($q$ALTER TABLE %I.hcm_salary_slips
        ADD COLUMN IF NOT EXISTS payable_days numeric(5,1),
        ADD COLUMN IF NOT EXISTS flags varchar(200),
        ADD COLUMN IF NOT EXISTS deduction_carry_forward numeric(14,2) NOT NULL DEFAULT 0$q$, s);

    EXECUTE format($q$CREATE TABLE IF NOT EXISTS %I.hcm_payroll_arrears (
        id uuid PRIMARY KEY,
        company_id uuid NOT NULL,
        employee_id uuid NOT NULL REFERENCES %I.core_employees(id),
        source_run_id uuid NOT NULL REFERENCES %I.hcm_payroll_runs(id),
        target_run_id uuid NOT NULL REFERENCES %I.hcm_payroll_runs(id),
        amount numeric(14,2) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (employee_id, source_run_id, target_run_id))$q$, s, s, s, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_payroll_arrears_target ON %I.hcm_payroll_arrears (target_run_id)', s);

    IF to_regclass(format('%I.hcm_loans', s)) IS NOT NULL THEN
      EXECUTE format($q$CREATE TABLE IF NOT EXISTS %I.hcm_loan_recoveries (
          id uuid PRIMARY KEY,
          loan_id uuid NOT NULL REFERENCES %I.hcm_loans(id),
          employee_id uuid NOT NULL REFERENCES %I.core_employees(id),
          payroll_run_id uuid NOT NULL REFERENCES %I.hcm_payroll_runs(id),
          slip_id uuid NOT NULL REFERENCES %I.hcm_salary_slips(id),
          amount numeric(14,2) NOT NULL,
          applied_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (loan_id, payroll_run_id))$q$, s, s, s, s, s);
      EXECUTE format('CREATE INDEX IF NOT EXISTS ix_loan_recoveries_run ON %I.hcm_loan_recoveries (payroll_run_id)', s);
    END IF;

    IF to_regclass(format('%I.hcm_tax_declarations', s)) IS NOT NULL THEN
      EXECUTE format($q$ALTER TABLE %I.hcm_tax_declarations
          ADD COLUMN IF NOT EXISTS section_80d numeric(12,2) NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS section_80ccd_1b numeric(12,2) NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS home_loan_interest numeric(12,2) NOT NULL DEFAULT 0$q$, s);
      EXECUTE format('SELECT count(*) FROM %I.hcm_tax_declarations WHERE hra_claimed < 0 OR section_80c < 0', s) INTO neg;
      IF neg > 0 THEN
        RAISE NOTICE '%: % tax declarations with negative amounts -- CHECK not added', s, neg;
      ELSIF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tax_declarations_non_negative'
                        AND connamespace = quote_ident(s)::regnamespace) THEN
        EXECUTE format($q$ALTER TABLE %I.hcm_tax_declarations ADD CONSTRAINT ck_tax_declarations_non_negative
            CHECK (coalesce(hra_claimed,0) >= 0 AND coalesce(section_80c,0) >= 0 AND section_80d >= 0
                   AND section_80ccd_1b >= 0 AND home_loan_interest >= 0)$q$, s);
      END IF;
    END IF;

    -- N-06: statutory PF / ESI employee-deduction components, per company,
    -- only where the company has no component with that code yet.
    IF to_regclass(format('%I.hcm_salary_components', s)) IS NOT NULL THEN
      EXECUTE format($q$INSERT INTO %I.hcm_salary_components (id, company_id, name, code, component_type, calc_type, is_taxable)
          SELECT gen_random_uuid(), c.id, 'Provident Fund (Employee)', 'PF', 'deduction', 'stat_pf', false
          FROM %I.core_companies c
          WHERE NOT EXISTS (SELECT 1 FROM %I.hcm_salary_components x WHERE x.company_id = c.id AND upper(x.code) = 'PF')$q$, s, s, s);
      EXECUTE format($q$INSERT INTO %I.hcm_salary_components (id, company_id, name, code, component_type, calc_type, is_taxable)
          SELECT gen_random_uuid(), c.id, 'ESI (Employee)', 'ESI', 'deduction', 'stat_esi', false
          FROM %I.core_companies c
          WHERE NOT EXISTS (SELECT 1 FROM %I.hcm_salary_components x WHERE x.company_id = c.id AND upper(x.code) IN ('ESI','ESIC'))$q$, s, s, s);
      EXECUTE format($q$UPDATE %I.hcm_salary_components SET name = 'Income Tax (TDS)'
          WHERE code = 'TDS-EST' AND name = 'TDS (Estimated)'$q$, s);
    END IF;

    IF to_regclass(format('%I.core_audit_logs', s)) IS NOT NULL THEN
      EXECUTE format($q$UPDATE %I.hcm_payroll_runs r SET generated_by = a.user_id, generated_at = a.created_at
          FROM (SELECT DISTINCT ON (document_id) document_id, user_id, created_at
                FROM %I.core_audit_logs WHERE doctype = 'payroll_run' AND action = 'generate'
                ORDER BY document_id, created_at DESC) a
          WHERE a.document_id = r.id AND r.generated_by IS NULL$q$, s, s);
    END IF;

    RAISE NOTICE 'payroll period control: %', s;
  END LOOP;
END $$;
