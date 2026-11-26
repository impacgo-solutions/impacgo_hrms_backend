-- Overtime: requested End Time, manual Overtime Clock In / Clock Out and
-- missed-punch reminders (no automatic clock-in/out).
--
--   hcm_overtime_requests
--     session_status  varchar(12) -> varchar(20)   (fits missed_clock_out)
--     + end_time               time         -- requested end (company local time)
--     + missed_in_notified_at  timestamptz  -- Missed Clock In reminder sent
--     + missed_out_notified_at timestamptz  -- Missed Clock Out reminder sent
--
-- Existing rows keep their values (all current sessions are NULL / legacy);
-- the new columns are NULL. Payroll / compensation tables are untouched.
--
-- Idempotent; every tenant schema + _template. Additive only.

CREATE OR REPLACE FUNCTION pg_temp.ocp_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format('ALTER TABLE %1$I.hcm_overtime_requests ALTER COLUMN session_status TYPE varchar(20)', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_overtime_requests ADD COLUMN IF NOT EXISTS end_time time', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_overtime_requests ADD COLUMN IF NOT EXISTS missed_in_notified_at timestamptz', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_overtime_requests ADD COLUMN IF NOT EXISTS missed_out_notified_at timestamptz', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.ocp_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_overtime_requests'
        )
    LOOP
        PERFORM pg_temp.ocp_apply(tenant.slug);
    END LOOP;
END $$;
