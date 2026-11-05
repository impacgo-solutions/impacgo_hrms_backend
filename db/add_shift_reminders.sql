-- Shift-based Attendance / Work Entry / Timesheet reminders.
--
--   hcm_shift_reminders     one row per employee / date / kind -> never twice;
--                           acknowledged_at = the popup was dismissed (any device)
--   hcm_compliance_settings + reminders_enabled, clock_in_reminder_minutes,
--                             end_reminder_minutes (tenant configuration)
--
-- Additive only. Idempotent; every tenant schema + _template.

CREATE OR REPLACE FUNCTION pg_temp.sr_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_shift_reminders (
            id              uuid PRIMARY KEY,
            company_id      uuid NOT NULL,
            employee_id     uuid NOT NULL,
            reminder_date   date NOT NULL,
            kind            varchar(20) NOT NULL,
            title           text NOT NULL,
            message         text NOT NULL,
            due_at          timestamptz NOT NULL,
            shift_label     text,
            notification_id uuid,
            created_at      timestamptz NOT NULL,
            acknowledged_at timestamptz
        )$q$, s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_shift_reminder ON %1$I.hcm_shift_reminders '
                   '(employee_id, reminder_date, kind)', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_compliance_settings ADD COLUMN IF NOT EXISTS reminders_enabled boolean NOT NULL DEFAULT true', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_compliance_settings ADD COLUMN IF NOT EXISTS clock_in_reminder_minutes smallint NOT NULL DEFAULT 15', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_compliance_settings ADD COLUMN IF NOT EXISTS end_reminder_minutes smallint NOT NULL DEFAULT 15', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.sr_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_compliance_settings'
        )
    LOOP
        PERFORM pg_temp.sr_apply(tenant.slug);
    END LOOP;
END $$;
