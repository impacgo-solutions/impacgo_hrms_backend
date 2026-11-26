-- Documents > Templates > Intern Offer Letter: hcm_intern_offer_letter_html_templates
-- (same shape as hcm_offer_letter_html_templates; one ACTIVE per company).
-- Offers whose employment type is Intern use the active intern template;
-- with none active they keep using the standard Offer Letter Template, so
-- nothing changes until a company has one. The predefined "Standard
-- Internship Offer Letter" is added by the app the first time HR opens
-- Documents > Templates (app/intern_offer_letter.py).
--
-- Additive and idempotent; _template plus every HRMS tenant schema.
--
--     cd backend && venv/Scripts/python db/run_hrms_migration.py db/add_intern_offer_letter_templates.sql

DO $$
DECLARE
    s text;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        IF to_regclass(format('%I.core_companies', s)) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('CREATE TABLE IF NOT EXISTS %1$I.hcm_intern_offer_letter_html_templates (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
            name varchar(120) NOT NULL DEFAULT ''Standard Internship Offer Letter'',
            html_body text NOT NULL,
            css_styles text NOT NULL DEFAULT '''',
            is_active boolean NOT NULL DEFAULT true,
            version integer NOT NULL DEFAULT 1,
            updated_by uuid,
            created_at timestamptz,
            updated_at timestamptz
        )', s);
        EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_intern_offer_tpl_active
            ON %I.hcm_intern_offer_letter_html_templates (company_id) WHERE is_active', s);
        RAISE NOTICE 'intern offer letter templates ready for %', s;
    END LOOP;
END $$;
