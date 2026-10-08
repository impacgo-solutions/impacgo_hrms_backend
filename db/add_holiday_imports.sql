-- Leave > Holiday Calendar > Upload Document: an uploaded holiday-calendar
-- document (PDF / DOCX / XLSX / CSV), what was extracted from it, the HR
-- review and the import result.
--
--   hcm_holiday_imports          NEW: one row per uploaded document
--   hcm_holidays.source_import_id  which import created the holiday (NULL =
--                                  added by hand)
--
-- Idempotent; every tenant schema + _template. Existing holidays unchanged.

CREATE OR REPLACE FUNCTION pg_temp.hi_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_holiday_imports (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
            original_filename varchar(255) NOT NULL,
            file_url text NOT NULL,
            content_type varchar(100),
            file_size integer,
            status varchar(12) NOT NULL
                CHECK (status IN ('extracted', 'imported', 'failed', 'cancelled')),
            extracted_rows jsonb NOT NULL DEFAULT '[]'::jsonb,
            extracted_count integer NOT NULL DEFAULT 0,
            imported_count integer NOT NULL DEFAULT 0,
            skipped_count integer NOT NULL DEFAULT 0,
            result jsonb,
            error text,
            uploaded_by uuid,
            uploaded_by_name varchar(150),
            uploaded_at timestamptz NOT NULL,
            imported_by uuid,
            imported_by_name varchar(150),
            imported_at timestamptz
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_holiday_imports_company ON %1$I.hcm_holiday_imports (company_id, uploaded_at DESC)', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_holidays ADD COLUMN IF NOT EXISTS source_import_id uuid', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.hi_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_holidays'
        )
    LOOP
        PERFORM pg_temp.hi_apply(tenant.slug);
    END LOOP;
END $$;
