-- Learning & Development > Learning Documents: folders (multi-file and
-- folder upload, preserving the uploaded folder structure).
--
--   hcm_learning_folders            a folder; parent_id = the folder it is in
--                                   (NULL = top level). Names are unique per
--                                   parent among live folders (case-insensitive).
--                                   Delete is soft, like documents.
--   hcm_learning_documents.folder_id the folder a document is in (NULL = top
--                                   level -- every document uploaded before
--                                   folders existed stays there, unchanged).
--
-- Additive only, no rows changed. Idempotent; _template + every tenant that
-- has hcm_learning_documents.

CREATE OR REPLACE FUNCTION pg_temp.lf_apply(s text) RETURNS void AS $f$
BEGIN
    IF to_regclass(format('%I.hcm_learning_documents', s)) IS NULL THEN
        RETURN;
    END IF;
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_learning_folders (
            id                      uuid PRIMARY KEY,
            company_id              uuid NOT NULL,
            parent_id               uuid REFERENCES %1$I.hcm_learning_folders(id),
            name                    varchar(255) NOT NULL,
            created_by_user_id      uuid NOT NULL,
            created_by_employee_id  uuid,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz,
            is_deleted              boolean NOT NULL DEFAULT false,
            deleted_at              timestamptz,
            deleted_by_user_id      uuid
        )$q$, s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_learning_folders_name ON %1$I.hcm_learning_folders '
                   '(company_id, COALESCE(parent_id, ''00000000-0000-0000-0000-000000000000''::uuid), lower(name)) '
                   'WHERE NOT is_deleted', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_learning_folders_parent ON %1$I.hcm_learning_folders '
                   '(company_id, parent_id) WHERE NOT is_deleted', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_learning_documents ADD COLUMN IF NOT EXISTS folder_id uuid '
                   'REFERENCES %1$I.hcm_learning_folders(id)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_learning_documents_folder ON %1$I.hcm_learning_documents '
                   '(company_id, folder_id) WHERE NOT is_deleted', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.lf_apply('_template');
    FOR tenant IN SELECT t.slug FROM public.tenants t LOOP
        PERFORM pg_temp.lf_apply(tenant.slug);
    END LOOP;
END $$;
