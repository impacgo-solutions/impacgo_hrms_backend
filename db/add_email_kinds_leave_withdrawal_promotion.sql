-- Extends hcm_email_templates.email_kind's CHECK constraint (added by
-- add_multi_templates.sql) with two kinds that didn't exist when that file
-- was first written:
--   * leave_withdrawal   -- this codebase's leave-withdrawal notifications
--                           (send_leave_withdrawal_email), which the first
--                           migration pass didn't know about.
--   * promotion_announcement -- new: a congratulations email sent when an
--                           employee's promotion/designation change is
--                           recorded (see routers/employees.py), previously
--                           not an email at all.
-- Purely additive (widens an allow-list, touches no existing rows).
-- Idempotent; every tenant schema + _template.

CREATE OR REPLACE FUNCTION pg_temp.mt_apply_kinds(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format('ALTER TABLE %1$I.hcm_email_templates DROP CONSTRAINT IF EXISTS hcm_email_templates_email_kind_check', s);
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_email_templates ADD CONSTRAINT hcm_email_templates_email_kind_check
        CHECK (email_kind IN (
            'request_notification', 'celebration', 'leave_applied', 'leave_approved',
            'leave_rejected', 'hr_document', 'test_email', 'candidate_message',
            'leave_withdrawal', 'promotion_announcement'
        ))
    $q$, s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.mt_apply_kinds('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_email_templates'
        )
    LOOP
        PERFORM pg_temp.mt_apply_kinds(tenant.slug);
    END LOOP;
END $$;
