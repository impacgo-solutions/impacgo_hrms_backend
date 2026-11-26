-- Experience & Relieving Letter: company seal and authorized signature
-- images printed on generated letters ({{company_seal}},
-- {{authorized_signature}}). Stored as upload paths under
-- uploads/exit_letter_asset/<company_id>/ -- never served by /media
-- (media_access denies the entity type); only embedded into the letter PDF.
-- Run after add_exit_letters.sql. Idempotent; tenant schemas + _template.

ALTER TABLE _template.hcm_exit_letter_html_templates ADD COLUMN IF NOT EXISTS seal_image_path text;
ALTER TABLE _template.hcm_exit_letter_html_templates ADD COLUMN IF NOT EXISTS signature_image_path text;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_exit_letter_html_templates'
        )
    LOOP
        EXECUTE format('ALTER TABLE %1$I.hcm_exit_letter_html_templates ADD COLUMN IF NOT EXISTS seal_image_path text', tenant.slug);
        EXECUTE format('ALTER TABLE %1$I.hcm_exit_letter_html_templates ADD COLUMN IF NOT EXISTS signature_image_path text', tenant.slug);
    END LOOP;
END $$;
