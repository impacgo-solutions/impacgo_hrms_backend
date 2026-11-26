-- Extends hcm_email_templates.email_kind's CHECK constraint with the kinds
-- that split the old generic emails into dedicated, separately-customizable
-- templates:
--   * birthday / work_anniversary      -- were one shared "celebration" kind
--   * holiday_reminder                  -- previously reused request_notification
--   * candidate_interview_scheduled / _rescheduled / _cancelled,
--     candidate_next_step, candidate_rejected
--                                       -- previously all one generic
--                                          candidate_message (which stays,
--                                          for ad-hoc candidate messages)
-- 'celebration' stays allowed so any existing row keeps validating (it is no
-- longer rendered). Purely additive. Idempotent; every tenant schema + _template.

CREATE OR REPLACE FUNCTION pg_temp.mt_apply_kinds(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format('ALTER TABLE %1$I.hcm_email_templates DROP CONSTRAINT IF EXISTS hcm_email_templates_email_kind_check', s);
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_email_templates ADD CONSTRAINT hcm_email_templates_email_kind_check
        CHECK (email_kind IN (
            'request_notification', 'celebration', 'leave_applied', 'leave_approved',
            'leave_rejected', 'hr_document', 'test_email', 'candidate_message',
            'leave_withdrawal', 'promotion_announcement',
            'birthday', 'work_anniversary', 'holiday_reminder',
            'candidate_interview_scheduled', 'candidate_interview_rescheduled',
            'candidate_interview_cancelled', 'candidate_next_step', 'candidate_rejected'
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
