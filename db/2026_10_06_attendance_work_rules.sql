-- 2026_10_06_attendance_work_rules.sql  (HRMS QA: M-12, M-13, M-26, N-03)
--
-- _template + every HRMS tenant (pg_temp.hrms_schemas(), created by
-- db/run_hrms_migration.py). Idempotent.
--   M-13  hcm_attendance_records.arrival_status: the check-in verdict
--         (present / early_in / late) kept separately from `status`, which
--         check-out may change. Backfilled from existing check-in statuses.
--   M-12  core_company_settings.regularization_backdate_days (default 30).
--   M-26  pm_resource_allocations allocation_pct may be 0 (a Team Lead tag
--         that reserves no capacity); still <= 100.
--   N-03  default office hours 09:00-17:00 for every company that has none
--         (and for the _template company new tenants are provisioned from).
DO $$
DECLARE
    s text;
    c record;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        IF to_regclass(format('%I.core_employees', s)) IS NULL THEN CONTINUE; END IF;

        IF to_regclass(format('%I.hcm_attendance_records', s)) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I.hcm_attendance_records ADD COLUMN IF NOT EXISTS arrival_status varchar(12)', s);
            EXECUTE format($q$UPDATE %I.hcm_attendance_records SET arrival_status = status
                             WHERE arrival_status IS NULL AND check_in IS NOT NULL
                               AND status IN ('present', 'early_in', 'late')$q$, s);
        END IF;

        IF to_regclass(format('%I.core_company_settings', s)) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I.core_company_settings ADD COLUMN IF NOT EXISTS regularization_backdate_days smallint DEFAULT 30', s);
            EXECUTE format('UPDATE %I.core_company_settings SET regularization_backdate_days = 30 WHERE regularization_backdate_days IS NULL', s);
            -- N-03: a settings row (with office hours) for every company.
            EXECUTE format($q$INSERT INTO %I.core_company_settings (company_id, working_hours_start, working_hours_end)
                             SELECT co.id, time '09:00', time '17:00' FROM %I.core_companies co
                             WHERE NOT EXISTS (SELECT 1 FROM %I.core_company_settings x WHERE x.company_id = co.id)$q$, s, s, s);
            EXECUTE format($q$UPDATE %I.core_company_settings
                             SET working_hours_start = time '09:00', working_hours_end = time '17:00'
                             WHERE working_hours_start IS NULL AND working_hours_end IS NULL$q$, s);
        END IF;

        IF to_regclass(format('%I.pm_resource_allocations', s)) IS NOT NULL THEN
            FOR c IN SELECT k.conname FROM pg_constraint k
                     WHERE k.conrelid = format('%I.pm_resource_allocations', s)::regclass
                       AND k.contype = 'c' AND pg_get_constraintdef(k.oid) LIKE '%allocation_pct%' LOOP
                EXECUTE format('ALTER TABLE %I.pm_resource_allocations DROP CONSTRAINT %I', s, c.conname);
            END LOOP;
            EXECUTE format($q$ALTER TABLE %I.pm_resource_allocations ADD CONSTRAINT pm_resource_allocations_alloc_chk
                             CHECK (allocation_pct >= 0 AND allocation_pct <= 100)$q$, s);
        END IF;
        RAISE NOTICE 'attendance/work rules: %', s;
    END LOOP;
END $$;
