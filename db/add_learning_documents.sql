-- Learning & Development > Learning Documents: training / learning /
-- development material any employee with Learning access uploads and every
-- employee of the same company can find, preview and download.
--
--   hcm_learning_documents         metadata; the file itself lives in the
--                                  existing upload storage under
--                                  uploads/learning_document/<id>/ and is
--                                  served through /media (media_access)
--   hcm_learning_document_history  upload history / audit trail (uploaded,
--                                  updated, file_replaced, deleted)
--
-- Duplicates: content_sha256 is unique among a company's live documents.
-- Delete is soft (is_deleted) so the history keeps its document; the file
-- is removed from storage.
-- New tables only; idempotent; every tenant schema + _template.

CREATE OR REPLACE FUNCTION pg_temp.ld_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_learning_documents (
            id                      uuid PRIMARY KEY,
            company_id              uuid NOT NULL,
            title                   varchar(200) NOT NULL,
            description             text,
            category                varchar(40) NOT NULL DEFAULT 'Learning',
            file_name               varchar(255) NOT NULL,
            file_url                text NOT NULL,
            file_ext                varchar(10) NOT NULL,
            mime_type               varchar(120),
            size_bytes              bigint NOT NULL,
            content_sha256          char(64) NOT NULL,
            version                 smallint NOT NULL DEFAULT 1,
            uploaded_by_employee_id uuid,
            uploaded_by_user_id     uuid NOT NULL,
            uploaded_at             timestamptz NOT NULL,
            is_deleted              boolean NOT NULL DEFAULT false,
            deleted_at              timestamptz,
            deleted_by_user_id      uuid,
            created_at              timestamptz,
            created_by              uuid,
            updated_at              timestamptz,
            updated_by              uuid
        )$q$, s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_learning_documents_sha ON %1$I.hcm_learning_documents '
                   '(company_id, content_sha256) WHERE NOT is_deleted', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_learning_documents_company ON %1$I.hcm_learning_documents '
                   '(company_id, is_deleted, uploaded_at DESC)', s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_learning_document_history (
            id                uuid PRIMARY KEY,
            company_id        uuid NOT NULL,
            document_id       uuid NOT NULL,
            action            varchar(20) NOT NULL,
            title             varchar(200) NOT NULL,
            file_name         varchar(255),
            size_bytes        bigint,
            details           text,
            actor_user_id     uuid,
            actor_employee_id uuid,
            created_at        timestamptz NOT NULL
        )$q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_learning_doc_history ON %1$I.hcm_learning_document_history '
                   '(company_id, created_at DESC)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_learning_doc_history_doc ON %1$I.hcm_learning_document_history '
                   '(document_id, created_at DESC)', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.ld_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'core_employees'
        )
    LOOP
        PERFORM pg_temp.ld_apply(tenant.slug);
    END LOOP;
END $$;
