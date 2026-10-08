-- HRMS email delivery log (Microsoft Graph integration, app/email_service.py).
-- One row per email: queued in the same transaction as the HRMS change that
-- triggers it, sent only after that commit, then marked SENT / FAILED
-- (SKIPPED while email is not configured). Stores metadata only -- never
-- the body or attachment contents. Idempotent; run on _template and every
-- tenant schema.

CREATE TABLE IF NOT EXISTS _template.core_email_logs (
    id uuid PRIMARY KEY,
    company_id uuid,
    email_type varchar(40) NOT NULL,
    sender varchar(150),
    recipient text NOT NULL,
    cc text,
    bcc text,
    subject varchar(300) NOT NULL,
    attachment_names text,
    related_entity_type varchar(40),
    related_entity_id uuid,
    status varchar(12) NOT NULL DEFAULT 'QUEUED',
    transport varchar(10),
    provider_status integer,
    error_code varchar(40),
    error_message text,
    attempts integer NOT NULL DEFAULT 0,
    idempotency_key varchar(200),
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_core_email_logs_created ON _template.core_email_logs (company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_core_email_logs_idem ON _template.core_email_logs (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_core_email_logs_entity ON _template.core_email_logs (related_entity_type, related_entity_id);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (SELECT 1 FROM information_schema.schemata s WHERE s.schema_name = t.slug)
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.core_email_logs (
                id uuid PRIMARY KEY,
                company_id uuid,
                email_type varchar(40) NOT NULL,
                sender varchar(150),
                recipient text NOT NULL,
                cc text,
                bcc text,
                subject varchar(300) NOT NULL,
                attachment_names text,
                related_entity_type varchar(40),
                related_entity_id uuid,
                status varchar(12) NOT NULL DEFAULT ''QUEUED'',
                transport varchar(10),
                provider_status integer,
                error_code varchar(40),
                error_message text,
                attempts integer NOT NULL DEFAULT 0,
                idempotency_key varchar(200),
                created_by uuid,
                created_at timestamptz NOT NULL DEFAULT now(),
                sent_at timestamptz
            )', tenant.slug);
        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_core_email_logs_created ON %1$I.core_email_logs (company_id, created_at DESC)', tenant.slug);
        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_core_email_logs_idem ON %1$I.core_email_logs (idempotency_key)', tenant.slug);
        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_core_email_logs_entity ON %1$I.core_email_logs (related_entity_type, related_entity_id)', tenant.slug);
    END LOOP;
END $$;
