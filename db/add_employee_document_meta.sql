-- Employee Details > Educational Documents / Professional Documents: every
-- uploaded file keeps its own metadata next to the hcm_document_records row
-- (which stays exactly as it was: employee_id, document_type, status,
-- uploaded_on, file_url).
--
--   hcm_employee_document_meta
--       document_id        PK, FK hcm_document_records(id) ON DELETE CASCADE
--       category           'education' | 'professional' | 'other'
--       file_name          the name the file had when it was uploaded
--       file_size_bytes
--       content_type
--       uploaded_by        core_users.id of whoever uploaded it (the employee,
--                          HR, a reporting manager, a department head ...)
--       uploaded_at        exact time of the upload
--
-- A separate table (not new columns on hcm_document_records) so a database
-- that has not run this script can never break the other places that read
-- document records. Existing documents simply have no metadata row and are
-- shown with what the record itself holds. Nothing is deleted or rewritten.
--
-- Idempotent; every tenant schema + _template. Additive only.

CREATE OR REPLACE FUNCTION pg_temp.edm_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_employee_document_meta (
            document_id uuid PRIMARY KEY REFERENCES %1$I.hcm_document_records (id) ON DELETE CASCADE,
            category varchar(20) NOT NULL DEFAULT 'other',
            file_name varchar(255),
            file_size_bytes bigint,
            content_type varchar(120),
            uploaded_by uuid,
            uploaded_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT hcm_employee_document_meta_category_check
                CHECK (category IN ('education', 'professional', 'other'))
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS hcm_document_records_employee_idx ON %1$I.hcm_document_records (employee_id)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS hcm_employee_document_meta_category_idx ON %1$I.hcm_employee_document_meta (category)', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.edm_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_document_records'
        )
    LOOP
        PERFORM pg_temp.edm_apply(tenant.slug);
    END LOOP;
END $$;
