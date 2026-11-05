-- Freelancer onboarding (Add Employee > Freelancer, Documents > Templates >
-- NDA / Freelancer Agreement, first-login acceptance):
--
--   core_employees.contract_rate_currency   ISO 4217 code of the current rate
--   hcm_employee_rate_history.currency_code ISO 4217 code of each rate
--       (NULL on both = the company's default currency -- every rate set
--       before this script, so nothing changes for them)
--   core_employees.bank_account_holder / bank_branch
--   hcm_freelancer_defaults    per-company Add Employee defaults for Freelancer
--   hcm_agreement_templates    NDA / Agreement templates (one active per type;
--                              the predefined ones are seeded by the app the
--                              first time a company needs them)
--   hcm_agreement_acceptances  who accepted which template version, when
--
-- Additive and idempotent; _template plus every HRMS tenant schema.
--
--     cd backend && venv/Scripts/python db/run_hrms_migration.py db/add_freelancer_onboarding.sql

DO $$
DECLARE
    s text;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        IF to_regclass(format('%I.core_employees', s)) IS NULL THEN
            CONTINUE;
        END IF;

        EXECUTE format('ALTER TABLE %I.core_employees
            ADD COLUMN IF NOT EXISTS contract_rate_currency varchar(3),
            ADD COLUMN IF NOT EXISTS bank_account_holder varchar(120),
            ADD COLUMN IF NOT EXISTS bank_branch varchar(120)', s);

        IF to_regclass(format('%I.hcm_employee_rate_history', s)) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I.hcm_employee_rate_history
                ADD COLUMN IF NOT EXISTS currency_code varchar(3)', s);
        END IF;

        EXECUTE format('CREATE TABLE IF NOT EXISTS %1$I.hcm_freelancer_defaults (
            company_id uuid PRIMARY KEY REFERENCES %1$I.core_companies(id),
            band_number smallint,
            role_name varchar(80),
            reporting_manager_id uuid REFERENCES %1$I.core_employees(id) ON DELETE SET NULL,
            designation_name varchar(120),
            branch_name varchar(120),
            employment_status varchar(20),
            rate_currency varchar(3),
            rate_unit varchar(10) CHECK (rate_unit IS NULL OR rate_unit IN (''hourly'', ''daily'')),
            updated_by uuid,
            updated_at timestamptz
        )', s);

        EXECUTE format('CREATE TABLE IF NOT EXISTS %1$I.hcm_agreement_templates (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
            doc_type varchar(20) NOT NULL CHECK (doc_type IN (''nda'', ''agreement'')),
            name varchar(120) NOT NULL,
            description varchar(300),
            html_body text NOT NULL,
            css_styles text NOT NULL DEFAULT '''',
            is_active boolean NOT NULL DEFAULT false,
            version integer NOT NULL DEFAULT 1,
            builtin_key varchar(40),
            created_by uuid,
            created_at timestamptz,
            updated_by uuid,
            updated_at timestamptz
        )', s);
        EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_agreement_tpl_active
            ON %I.hcm_agreement_templates (company_id, doc_type) WHERE is_active', s);
        EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_agreement_tpl_builtin
            ON %I.hcm_agreement_templates (company_id, builtin_key) WHERE builtin_key IS NOT NULL', s);

        EXECUTE format('CREATE TABLE IF NOT EXISTS %1$I.hcm_agreement_acceptances (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
            employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
            user_id uuid,
            template_id uuid NOT NULL REFERENCES %1$I.hcm_agreement_templates(id),
            doc_type varchar(20) NOT NULL,
            template_name varchar(120) NOT NULL,
            template_version integer NOT NULL,
            rendered_html text,
            accepted_at timestamptz NOT NULL DEFAULT now(),
            ip_address varchar(64),
            user_agent varchar(300),
            CONSTRAINT uq_agreement_acceptance UNIQUE (employee_id, template_id, template_version)
        )', s);
        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_agreement_acceptance_emp
            ON %I.hcm_agreement_acceptances (employee_id)', s);

        RAISE NOTICE 'freelancer onboarding ready for %', s;
    END LOOP;
END $$;
