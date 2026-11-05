-- Experience & Relieving Letter: one combined letter type
-- ('experience_relieving') instead of separate 'experience' / 'relieving'.
-- For databases where add_exit_letters.sql already ran with the two-type
-- constraints (impacgo_test_dev, 2026-09-28 -- no rows existed). Safe to
-- re-run. Fails (by design) if a row with an old type exists.

ALTER TABLE _template.hcm_exit_letter_html_templates DROP CONSTRAINT IF EXISTS ck_exit_letter_template_type;
ALTER TABLE _template.hcm_exit_letter_html_templates
    ADD CONSTRAINT ck_exit_letter_template_type CHECK (letter_type IN ('experience_relieving'));
ALTER TABLE _template.hcm_exit_letters DROP CONSTRAINT IF EXISTS ck_exit_letter_type;
ALTER TABLE _template.hcm_exit_letters
    ADD CONSTRAINT ck_exit_letter_type CHECK (letter_type IN ('experience_relieving'));

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_exit_letters'
        )
    LOOP
        EXECUTE format('ALTER TABLE %1$I.hcm_exit_letter_html_templates DROP CONSTRAINT IF EXISTS ck_exit_letter_template_type', tenant.slug);
        EXECUTE format('ALTER TABLE %1$I.hcm_exit_letter_html_templates ADD CONSTRAINT ck_exit_letter_template_type CHECK (letter_type IN (''experience_relieving''))', tenant.slug);
        EXECUTE format('ALTER TABLE %1$I.hcm_exit_letters DROP CONSTRAINT IF EXISTS ck_exit_letter_type', tenant.slug);
        EXECUTE format('ALTER TABLE %1$I.hcm_exit_letters ADD CONSTRAINT ck_exit_letter_type CHECK (letter_type IN (''experience_relieving''))', tenant.slug);
    END LOOP;
END $$;
