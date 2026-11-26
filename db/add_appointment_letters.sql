-- Appointment Letters (Documents > Templates > Appointment Letter + Employee
-- Profile > Documents > Appointment Letter). See app/appointment_letters.py.
--
-- hcm_appointment_letter_templates: the company's HTML+CSS designs, several
--   per company, at most one active (partial unique index), version bumped
--   on each save, plus the authorized signatory and seal / signature images
--   (stored under uploads/appointment_letter_asset/, never served by /media).
-- hcm_appointment_letters: every generated letter, an immutable snapshot
--   (rendered HTML, placeholder values, template name/version, stored PDF
--   under uploads/appointment_letter/). Regenerating inserts version + 1 and
--   flips is_current; old rows are otherwise never updated.
-- Additive only, no rows inserted. Idempotent; runs on _template and every
-- tenant that has core_employees.

CREATE TABLE IF NOT EXISTS _template.hcm_appointment_letter_templates (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    name varchar(120) NOT NULL,
    html_body text NOT NULL,
    css_styles text NOT NULL DEFAULT '',
    is_active boolean NOT NULL DEFAULT false,
    version integer NOT NULL DEFAULT 1,
    signatory_name varchar(120),
    signatory_designation varchar(120),
    seal_image_path text,
    signature_image_path text,
    updated_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_appointment_letter_template_active
    ON _template.hcm_appointment_letter_templates (company_id) WHERE is_active;

CREATE TABLE IF NOT EXISTS _template.hcm_appointment_letters (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    employee_id uuid NOT NULL REFERENCES _template.core_employees(id),
    version integer NOT NULL DEFAULT 1,
    letter_number varchar(60) NOT NULL,
    template_id uuid,
    template_name varchar(120) NOT NULL,
    template_version integer NOT NULL,
    rendered_html text NOT NULL,
    placeholders jsonb NOT NULL DEFAULT '{}'::jsonb,
    file_url text NOT NULL,
    file_size integer NOT NULL DEFAULT 0,
    is_current boolean NOT NULL DEFAULT true,
    notes text,
    generated_by uuid,
    generated_by_name varchar(150),
    generated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_appointment_letter_version UNIQUE (employee_id, version)
);
CREATE INDEX IF NOT EXISTS ix_appointment_letters_employee
    ON _template.hcm_appointment_letters (employee_id, version DESC);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'core_employees'
        )
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.hcm_appointment_letter_templates (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
                name varchar(120) NOT NULL,
                html_body text NOT NULL,
                css_styles text NOT NULL DEFAULT '''',
                is_active boolean NOT NULL DEFAULT false,
                version integer NOT NULL DEFAULT 1,
                signatory_name varchar(120),
                signatory_designation varchar(120),
                seal_image_path text,
                signature_image_path text,
                updated_by uuid,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )', tenant.slug);
        EXECUTE format(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_appointment_letter_template_active
                ON %1$I.hcm_appointment_letter_templates (company_id) WHERE is_active', tenant.slug);
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.hcm_appointment_letters (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
                employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
                version integer NOT NULL DEFAULT 1,
                letter_number varchar(60) NOT NULL,
                template_id uuid,
                template_name varchar(120) NOT NULL,
                template_version integer NOT NULL,
                rendered_html text NOT NULL,
                placeholders jsonb NOT NULL DEFAULT ''{}''::jsonb,
                file_url text NOT NULL,
                file_size integer NOT NULL DEFAULT 0,
                is_current boolean NOT NULL DEFAULT true,
                notes text,
                generated_by uuid,
                generated_by_name varchar(150),
                generated_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT uq_appointment_letter_version UNIQUE (employee_id, version)
            )', tenant.slug);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS ix_appointment_letters_employee
                ON %1$I.hcm_appointment_letters (employee_id, version DESC)', tenant.slug);
    END LOOP;
END $$;
