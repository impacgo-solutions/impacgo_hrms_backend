-- Configurable overtime compensation (Administration > Attendance & Payroll >
-- Overtime Settings): approved overtime runs as a session from the approved
-- start time for the approved duration, and on completion is compensated
-- once -- as Payable Overtime (an "Overtime Pay" earning on the next payroll
-- run) or as Compensatory Leave (credited to the comp-off leave balance).
--
--   hcm_overtime_settings        NEW: one row per company
--   hcm_overtime_requests        + start time, session window / actuals,
--                                  compensation status
--   hcm_overtime_compensations   NEW: one row per processed request (UNIQUE
--                                  overtime_request_id -- never twice)
--
-- Existing overtime requests are kept as they are (no session / no
-- compensation -- they were already added to attendance on approval).
-- Idempotent; every tenant schema + _template.

CREATE OR REPLACE FUNCTION pg_temp.ot_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_overtime_settings (
            company_id uuid PRIMARY KEY REFERENCES %1$I.core_companies(id),
            enabled boolean NOT NULL DEFAULT true,
            compensation_mode varchar(10) NOT NULL DEFAULT 'payable'
                CHECK (compensation_mode IN ('payable', 'comp_off')),
            rate_basis varchar(10) NOT NULL DEFAULT 'basic'
                CHECK (rate_basis IN ('basic', 'gross', 'fixed')),
            rate_multiplier numeric(5,2) NOT NULL DEFAULT 2.00 CHECK (rate_multiplier > 0),
            monthly_days_divisor smallint NOT NULL DEFAULT 26 CHECK (monthly_days_divisor BETWEEN 1 AND 31),
            hours_per_day numeric(4,2) NOT NULL DEFAULT 8.00 CHECK (hours_per_day > 0),
            fixed_hourly_rate numeric(12,2),
            comp_off_leave_type_id uuid,
            comp_off_full_day_hours numeric(4,2) NOT NULL DEFAULT 8.00 CHECK (comp_off_full_day_hours > 0),
            comp_off_half_day_hours numeric(4,2) NOT NULL DEFAULT 4.00 CHECK (comp_off_half_day_hours > 0),
            min_minutes smallint NOT NULL DEFAULT 30 CHECK (min_minutes >= 0),
            rounding varchar(12) NOT NULL DEFAULT 'exact'
                CHECK (rounding IN ('exact', 'nearest_15', 'nearest_30', 'floor_30')),
            updated_by uuid,
            updated_at timestamptz
        )
    $q$, s);

    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_overtime_requests
            ADD COLUMN IF NOT EXISTS start_time time,
            ADD COLUMN IF NOT EXISTS planned_start timestamptz,
            ADD COLUMN IF NOT EXISTS planned_end timestamptz,
            ADD COLUMN IF NOT EXISTS session_status varchar(12),
            ADD COLUMN IF NOT EXISTS actual_start timestamptz,
            ADD COLUMN IF NOT EXISTS actual_end timestamptz,
            ADD COLUMN IF NOT EXISTS actual_minutes integer,
            ADD COLUMN IF NOT EXISTS ended_early_by uuid,
            ADD COLUMN IF NOT EXISTS compensation_status varchar(12)
    $q$, s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_overtime_compensations (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
            overtime_request_id uuid NOT NULL UNIQUE REFERENCES %1$I.hcm_overtime_requests(id),
            employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
            mode varchar(10) NOT NULL CHECK (mode IN ('payable', 'comp_off')),
            worked_minutes integer NOT NULL,
            counted_minutes integer NOT NULL,
            hourly_rate numeric(12,2),
            rate_multiplier numeric(5,2),
            amount numeric(12,2),
            leave_type_id uuid,
            leave_days numeric(4,1),
            leave_allocation_id uuid,
            target_period_month smallint,
            target_period_year smallint,
            applied_payroll_run_id uuid,
            applied_at timestamptz,
            status varchar(16) NOT NULL,
            calculation text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_ot_comp_employee ON %1$I.hcm_overtime_compensations (employee_id, status)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_ot_session ON %1$I.hcm_overtime_requests (session_status) WHERE session_status IN (''scheduled'', ''in_progress'')', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.ot_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_overtime_requests'
        )
    LOOP
        PERFORM pg_temp.ot_apply(tenant.slug);
    END LOOP;
END $$;
