-- Missing Attendance & Work Compliance: tenant settings, an EOD run log
-- (one row per company per date -> no duplicate alerts) and the exceptions.
--
--   hcm_compliance_settings   one row per company (defaults when absent)
--   hcm_compliance_runs       (company_id, run_date) processed once
--   hcm_compliance_exceptions one row per employee / date / type / reference
--
-- New tables only; nothing existing is altered.
-- Idempotent; every tenant schema + _template.

CREATE OR REPLACE FUNCTION pg_temp.cmp_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_compliance_settings (
            company_id          uuid PRIMARY KEY,
            enabled             boolean NOT NULL DEFAULT true,
            eod_cutoff          time    NOT NULL DEFAULT '23:00',
            check_attendance    boolean NOT NULL DEFAULT true,
            check_overtime      boolean NOT NULL DEFAULT true,
            check_work_entry    boolean NOT NULL DEFAULT true,
            check_timesheet     boolean NOT NULL DEFAULT true,
            notify_employee     boolean NOT NULL DEFAULT true,
            updated_at          timestamptz,
            updated_by          uuid
        )$q$, s);
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_compliance_runs (
            company_id   uuid NOT NULL,
            run_date     date NOT NULL,
            processed_at timestamptz NOT NULL,
            exceptions   integer NOT NULL DEFAULT 0,
            PRIMARY KEY (company_id, run_date)
        )$q$, s);
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_compliance_exceptions (
            id                    uuid PRIMARY KEY,
            company_id            uuid NOT NULL,
            employee_id           uuid NOT NULL,
            exception_date        date NOT NULL,
            exception_type        varchar(30) NOT NULL,
            reference_id          uuid,
            reference_key         varchar(40) NOT NULL DEFAULT '',
            expected_label        text,
            expected_at           timestamptz,
            actual_at             timestamptz,
            details               text,
            regularization_id     uuid,
            regularization_status varchar(20),
            manager_notified_at   timestamptz,
            employee_notified_at  timestamptz,
            notification_status   text,
            status                varchar(12) NOT NULL DEFAULT 'open',
            resolved_at           timestamptz,
            resolution            text,
            created_at            timestamptz NOT NULL,
            updated_at            timestamptz
        )$q$, s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_compliance_exception ON %1$I.hcm_compliance_exceptions '
                   '(employee_id, exception_date, exception_type, reference_key)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_compliance_exception_company_date ON %1$I.hcm_compliance_exceptions '
                   '(company_id, exception_date)', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.cmp_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'core_employees'
        )
    LOOP
        PERFORM pg_temp.cmp_apply(tenant.slug);
    END LOOP;
END $$;
