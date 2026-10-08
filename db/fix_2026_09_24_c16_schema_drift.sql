-- fix_2026_09_24_c16_schema_drift.sql  (C16 -- models vs repo DDL drift)
--
-- NOT EXECUTED. Generated 2026-09-24 by read-only introspection of the LIVE
-- database (pg_attribute / pg_constraint / pg_indexes) -- tenant schema
-- "impacgo-solutions" and schema "public" -- for every table/column that
-- app/models.py maps but no backend/db/*.sql file defines. Running it on a
-- DB that already has these objects is a no-op (IF NOT EXISTS / duplicate
-- guards), so it is safe to run against every tenant.
--
-- How to run (per tenant schema; the public.* statements are idempotent):
--     SET search_path TO "<tenant_slug>", public;
--     \i fix_2026_09_24_c16_schema_drift.sql
--
-- Notes
--  * backend/db/updatedtable.sql and backend/db/add_project_allocation_fields.sql
--    are staged deletions in the working tree. They describe the OLD
--    core./hcm. schema layout (hcm.projects, hcm.project_allocations.team_lead_id,
--    ...) that the ORM no longer maps (ProjectAllocation -> pm_resource_allocations),
--    so they must NOT be restored as-is; this file only covers objects the
--    current models need.
--  * hcm_celebration_log already has backend/db/add_celebration_log.sql; it is
--    repeated here (identical to live) so a rebuild from this file alone works.
--  * Tenant-table FKs are added in DO blocks so re-running doesn't fail.
BEGIN;

CREATE TABLE IF NOT EXISTS public.platform_settings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    trial_days integer DEFAULT 14 NOT NULL,
    currency character varying(10) DEFAULT 'USD'::character varying NOT NULL,
    timezone character varying(60) DEFAULT 'IST (UTC+5:30)'::character varying NOT NULL,
    monthly_price numeric(10,2) DEFAULT 99 NOT NULL,
    yearly_price numeric(10,2) DEFAULT 999 NOT NULL,
    yearly_discount_pct numeric(5,2) DEFAULT 16 NOT NULL,
    stripe_public_key text,
    stripe_secret_key text,
    auto_renew boolean DEFAULT true NOT NULL,
    alert_enabled boolean DEFAULT true NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT platform_settings_pkey PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS public.platform_notifications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    title character varying(200) NOT NULL,
    body text NOT NULL,
    target_segment character varying(20) DEFAULT 'all'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_read boolean DEFAULT false NOT NULL,
    CONSTRAINT platform_notifications_pkey PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS hcm_break_records (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    attendance_record_id uuid NOT NULL,
    break_start timestamp with time zone NOT NULL,
    break_end timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hcm_break_records_pkey PRIMARY KEY (id)
);
DO $$ BEGIN
    ALTER TABLE hcm_break_records ADD CONSTRAINT hcm_break_records_attendance_record_id_fkey FOREIGN KEY (attendance_record_id) REFERENCES hcm_attendance_records(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE hcm_break_records ADD CONSTRAINT hcm_break_records_company_id_fkey FOREIGN KEY (company_id) REFERENCES core_companies(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE hcm_break_records ADD CONSTRAINT hcm_break_records_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES core_employees(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS idx_hcm_break_records_attendance ON hcm_break_records USING btree (attendance_record_id);
CREATE INDEX IF NOT EXISTS idx_hcm_break_records_employee ON hcm_break_records USING btree (employee_id);

CREATE TABLE IF NOT EXISTS hcm_payslip_html_templates (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) DEFAULT 'Standard Payslip'::character varying NOT NULL,
    html_body text NOT NULL,
    css_styles text DEFAULT ''::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    updated_by uuid,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    CONSTRAINT hcm_payslip_html_templates_company_id_key UNIQUE (company_id),
    CONSTRAINT hcm_payslip_html_templates_pkey PRIMARY KEY (id)
);
DO $$ BEGIN
    ALTER TABLE hcm_payslip_html_templates ADD CONSTRAINT hcm_payslip_html_templates_company_id_fkey FOREIGN KEY (company_id) REFERENCES core_companies(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS hcm_offer_letter_html_templates (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) DEFAULT 'Standard Offer Letter'::character varying NOT NULL,
    html_body text NOT NULL,
    css_styles text DEFAULT ''::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    updated_by uuid,
    created_at timestamp with time zone,
    updated_at timestamp with time zone,
    CONSTRAINT hcm_offer_letter_html_templates_company_id_key UNIQUE (company_id),
    CONSTRAINT hcm_offer_letter_html_templates_pkey PRIMARY KEY (id)
);
DO $$ BEGIN
    ALTER TABLE hcm_offer_letter_html_templates ADD CONSTRAINT hcm_offer_letter_html_templates_company_id_fkey FOREIGN KEY (company_id) REFERENCES core_companies(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS hcm_variable_pay_payouts (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    structure_assignment_id uuid NOT NULL,
    component_id uuid NOT NULL,
    payroll_run_id uuid NOT NULL,
    fiscal_year_start date NOT NULL,
    amount numeric(14,2) NOT NULL,
    created_by uuid,
    created_at timestamp with time zone NOT NULL,
    notes character varying(300),
    CONSTRAINT uq_variable_pay_payout_employee_component_run UNIQUE (employee_id, component_id, payroll_run_id),
    CONSTRAINT hcm_variable_pay_payouts_pkey PRIMARY KEY (id)
);
DO $$ BEGIN
    ALTER TABLE hcm_variable_pay_payouts ADD CONSTRAINT hcm_variable_pay_payouts_company_id_fkey FOREIGN KEY (company_id) REFERENCES core_companies(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE hcm_variable_pay_payouts ADD CONSTRAINT hcm_variable_pay_payouts_component_id_fkey FOREIGN KEY (component_id) REFERENCES hcm_salary_components(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE hcm_variable_pay_payouts ADD CONSTRAINT hcm_variable_pay_payouts_created_by_fkey FOREIGN KEY (created_by) REFERENCES core_users(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE hcm_variable_pay_payouts ADD CONSTRAINT hcm_variable_pay_payouts_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES core_employees(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE hcm_variable_pay_payouts ADD CONSTRAINT hcm_variable_pay_payouts_payroll_run_id_fkey FOREIGN KEY (payroll_run_id) REFERENCES hcm_payroll_runs(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE hcm_variable_pay_payouts ADD CONSTRAINT hcm_variable_pay_payouts_structure_assignment_id_fkey FOREIGN KEY (structure_assignment_id) REFERENCES hcm_salary_structure_assignments(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS hcm_salary_structure_assignment_overrides (
    id uuid NOT NULL,
    assignment_id uuid NOT NULL,
    component_id uuid NOT NULL,
    override_type character varying(10) NOT NULL,
    value numeric(14,2) NOT NULL,
    reason character varying(200),
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hcm_salary_structure_assignment__assignment_id_component_id_key UNIQUE (assignment_id, component_id),
    CONSTRAINT hcm_salary_structure_assignment_overrides_pkey PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS hcm_benefit_category_assignments (
    id uuid NOT NULL,
    category_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    CONSTRAINT uq_benefit_category_assignment UNIQUE (category_id, employee_id),
    CONSTRAINT hcm_benefit_category_assignments_pkey PRIMARY KEY (id)
);
DO $$ BEGIN
    ALTER TABLE hcm_benefit_category_assignments ADD CONSTRAINT hcm_benefit_category_assignments_category_id_fkey FOREIGN KEY (category_id) REFERENCES hcm_benefit_categories(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE hcm_benefit_category_assignments ADD CONSTRAINT hcm_benefit_category_assignments_employee_id_fkey FOREIGN KEY (employee_id) REFERENCES core_employees(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS hcm_celebration_log (
    id uuid NOT NULL,
    employee_id uuid NOT NULL,
    celebration_type character varying(20) NOT NULL,
    year integer NOT NULL,
    notified_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT hcm_celebration_log_unique UNIQUE (employee_id, celebration_type, year),
    CONSTRAINT hcm_celebration_log_pkey PRIMARY KEY (id)
);

-- ---- Missing columns on existing tables ----
ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS plan_type character varying(10) DEFAULT 'trial'::character varying NOT NULL;
ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS trial_ends_at date;
ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS status character varying(10) DEFAULT 'trial'::character varying NOT NULL;
ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS contact_name character varying(150);
ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS contact_email character varying(150);
ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS contact_phone character varying(20);
ALTER TABLE core_company_settings ADD COLUMN IF NOT EXISTS payslip_template text;
ALTER TABLE core_company_settings ADD COLUMN IF NOT EXISTS auto_tds_estimate_enabled boolean DEFAULT false NOT NULL;
ALTER TABLE core_company_settings ADD COLUMN IF NOT EXISTS work_entry_backdate_days smallint;
ALTER TABLE core_company_settings ADD COLUMN IF NOT EXISTS pf_wage_ceiling numeric(10,2) DEFAULT 15000 NOT NULL;
ALTER TABLE hcm_attendance_records ADD COLUMN IF NOT EXISTS work_mode character varying(20);
ALTER TABLE hcm_exit_requests ADD COLUMN IF NOT EXISTS approver_id uuid;
ALTER TABLE hcm_exit_requests ADD COLUMN IF NOT EXISTS decision_notes text;
ALTER TABLE hcm_exit_requests ADD COLUMN IF NOT EXISTS decided_at timestamp with time zone;
ALTER TABLE hcm_hiring_requisitions ADD COLUMN IF NOT EXISTS approver_id uuid;
ALTER TABLE hcm_hiring_requisitions ADD COLUMN IF NOT EXISTS decision_notes text;
ALTER TABLE hcm_hiring_requisitions ADD COLUMN IF NOT EXISTS decided_at timestamp with time zone;
ALTER TABLE hcm_salary_structures ADD COLUMN IF NOT EXISTS department_id uuid;
ALTER TABLE hcm_salary_structures ADD COLUMN IF NOT EXISTS designation_id uuid;
ALTER TABLE hcm_salary_structures ADD COLUMN IF NOT EXISTS branch_id uuid;
ALTER TABLE hcm_salary_structures ADD COLUMN IF NOT EXISTS grade character varying(50);
ALTER TABLE hcm_shifts ADD COLUMN IF NOT EXISTS max_breaks smallint DEFAULT 1 NOT NULL;
ALTER TABLE hcm_leave_requests ADD COLUMN IF NOT EXISTS half_day_period character varying(10);
ALTER TABLE hcm_salary_structure_assignments ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL;
ALTER TABLE pm_time_entries ADD COLUMN IF NOT EXISTS task_name character varying(200);

COMMIT;
