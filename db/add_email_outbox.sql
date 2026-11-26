-- Durable email outbox (backend/app/email_service.py).
--
-- core_email_logs already got its QUEUED row in the same transaction as the
-- business change, but the email itself only lived in an in-memory worker
-- queue: a server restart / crash / lost DB connection between commit and
-- send dropped it for good, leaving the row QUEUED forever. These columns
-- let the row carry the email until it is delivered:
--   payload          the queued email (recipients, subject, bodies,
--                    attachments or the recipe to rebuild them). Cleared as
--                    soon as the email reaches a final state, so the log
--                    still never keeps message bodies long-term.
--   next_attempt_at  when a transient failure (Graph throttling, timeout,
--                    network) should be retried.
--   claimed_at       set when a worker atomically claims the row
--                    (QUEUED -> SENDING) so no email is ever sent twice, even
--                    with several backend processes.
-- Additive, nullable, idempotent; every tenant schema + _template.

CREATE OR REPLACE FUNCTION pg_temp.outbox_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format('ALTER TABLE %1$I.core_email_logs ADD COLUMN IF NOT EXISTS payload jsonb', s);
    EXECUTE format('ALTER TABLE %1$I.core_email_logs ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz', s);
    EXECUTE format('ALTER TABLE %1$I.core_email_logs ADD COLUMN IF NOT EXISTS claimed_at timestamptz', s);
    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS ix_email_logs_outbox ON %1$I.core_email_logs (created_at) '
        'WHERE status IN (''QUEUED'', ''SENDING'')', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = '_template' AND table_name = 'core_email_logs') THEN
        PERFORM pg_temp.outbox_apply('_template');
    END IF;
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'core_email_logs'
        )
    LOOP
        PERFORM pg_temp.outbox_apply(tenant.slug);
    END LOOP;
END $$;
