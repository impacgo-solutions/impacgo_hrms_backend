-- Employee Details > Education & Experience: several education rows and
-- several previous-employment rows per employee, with a level dropdown, an
-- experience-range dropdown and per-job dates / role.
--
--   hcm_employee_education        + level            '10th' | 'Intermediate' | 'Diploma' |
--                                                    'Bachelors' | 'Masters' | 'PhD' | 'Other'
--                                 + class_percentage free text ("First Class", "8.4 CGPA", "82%")
--                                 + sort_order, created_at
--   hcm_employee_prior_experience + job_role, start_date, end_date, is_current,
--                                   sort_order, created_at
--                                   (company = employer_name, domain / industry = domain,
--                                    years of experience = years_experience: all existing)
--   hcm_employee_experience_summary (new, one row per employee)
--                                 total_experience_range, the Total Experience dropdown choice
--                                 e.g. '3-5' (kept out of core_employees so a database that
--                                 has not run this script can never break the employee queries)
--
-- Both tables already allowed many rows per employee (primary key on id only);
-- existing rows are kept untouched and simply show up as the first record.
-- No backfill, nothing is deleted or overwritten.
--
-- Idempotent; every tenant schema + _template. Additive only.

CREATE OR REPLACE FUNCTION pg_temp.eer_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_employee_education
            ADD COLUMN IF NOT EXISTS level varchar(20),
            ADD COLUMN IF NOT EXISTS class_percentage varchar(40),
            ADD COLUMN IF NOT EXISTS sort_order smallint NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now()
    $q$, s);
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_employee_prior_experience
            ADD COLUMN IF NOT EXISTS job_role varchar(150),
            ADD COLUMN IF NOT EXISTS start_date date,
            ADD COLUMN IF NOT EXISTS end_date date,
            ADD COLUMN IF NOT EXISTS is_current boolean NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS sort_order smallint NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now()
    $q$, s);
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_employee_experience_summary (
            employee_id uuid PRIMARY KEY REFERENCES %1$I.core_employees (id) ON DELETE CASCADE,
            total_experience_range varchar(20),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    $q$, s);

    IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                   WHERE n.nspname = s AND c.conname = 'hcm_employee_education_level_check') THEN
        EXECUTE format($q$ALTER TABLE %1$I.hcm_employee_education ADD CONSTRAINT hcm_employee_education_level_check
            CHECK (level IS NULL OR level IN ('10th', 'Intermediate', 'Diploma', 'Bachelors', 'Masters', 'PhD', 'Other'))$q$, s);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                   WHERE n.nspname = s AND c.conname = 'hcm_employee_prior_experience_dates_check') THEN
        EXECUTE format($q$ALTER TABLE %1$I.hcm_employee_prior_experience ADD CONSTRAINT hcm_employee_prior_experience_dates_check
            CHECK (start_date IS NULL OR end_date IS NULL OR end_date >= start_date)$q$, s);
    END IF;

    EXECUTE format('CREATE INDEX IF NOT EXISTS hcm_employee_education_employee_idx ON %1$I.hcm_employee_education (employee_id)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS hcm_employee_prior_experience_employee_idx ON %1$I.hcm_employee_prior_experience (employee_id)', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.eer_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_employee_prior_experience'
        )
    LOOP
        PERFORM pg_temp.eer_apply(tenant.slug);
    END LOOP;
END $$;
