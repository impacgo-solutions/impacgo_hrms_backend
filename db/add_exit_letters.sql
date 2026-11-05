-- Experience & Relieving Letters (Documents > Templates + People >
-- Offboarding > Experience & Relieving Documents).
--
-- hcm_exit_letter_html_templates: the company's HTML+CSS design per letter
--   type ('experience_relieving' -- one combined Experience & Relieving
--   Letter), one row per company and type,
--   version bumped on each save -- same semantics as
--   hcm_offer_letter_html_templates -- plus the authorized signatory.
-- hcm_exit_letters: every generated letter, an immutable snapshot (rendered
--   HTML, placeholder values, template name/version, stored PDF path).
--   Regenerating inserts version + 1 and flips is_current; old rows are
--   never updated otherwise.
-- No rows are inserted. Idempotent; runs on _template and every tenant.

CREATE TABLE IF NOT EXISTS _template.hcm_exit_letter_html_templates (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    letter_type varchar(20) NOT NULL,
    name varchar(120) NOT NULL,
    html_body text NOT NULL,
    css_styles text NOT NULL DEFAULT '',
    is_active boolean NOT NULL DEFAULT true,
    version integer NOT NULL DEFAULT 1,
    signatory_name varchar(120),
    signatory_designation varchar(120),
    updated_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_exit_letter_template_type UNIQUE (company_id, letter_type),
    CONSTRAINT ck_exit_letter_template_type CHECK (letter_type IN ('experience_relieving'))
);

CREATE TABLE IF NOT EXISTS _template.hcm_exit_letters (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    employee_id uuid NOT NULL REFERENCES _template.core_employees(id),
    exit_request_id uuid NOT NULL REFERENCES _template.hcm_exit_requests(id),
    letter_type varchar(20) NOT NULL,
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
    CONSTRAINT uq_exit_letter_version UNIQUE (employee_id, exit_request_id, letter_type, version),
    CONSTRAINT ck_exit_letter_type CHECK (letter_type IN ('experience_relieving'))
);
CREATE INDEX IF NOT EXISTS ix_exit_letters_employee ON _template.hcm_exit_letters (employee_id, letter_type, version DESC);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_exit_requests'
        )
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.hcm_exit_letter_html_templates (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
                letter_type varchar(20) NOT NULL,
                name varchar(120) NOT NULL,
                html_body text NOT NULL,
                css_styles text NOT NULL DEFAULT '''',
                is_active boolean NOT NULL DEFAULT true,
                version integer NOT NULL DEFAULT 1,
                signatory_name varchar(120),
                signatory_designation varchar(120),
                updated_by uuid,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT uq_exit_letter_template_type UNIQUE (company_id, letter_type),
                CONSTRAINT ck_exit_letter_template_type CHECK (letter_type IN (''experience_relieving''))
            )', tenant.slug);
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.hcm_exit_letters (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
                employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
                exit_request_id uuid NOT NULL REFERENCES %1$I.hcm_exit_requests(id),
                letter_type varchar(20) NOT NULL,
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
                CONSTRAINT uq_exit_letter_version UNIQUE (employee_id, exit_request_id, letter_type, version),
                CONSTRAINT ck_exit_letter_type CHECK (letter_type IN (''experience_relieving''))
            )', tenant.slug);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS ix_exit_letters_employee ON %1$I.hcm_exit_letters (employee_id, letter_type, version DESC)',
            tenant.slug);
    END LOOP;
END $$;
