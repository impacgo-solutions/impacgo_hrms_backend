-- Unlimited Clock In / Clock Out per day:
--
--   hcm_attendance_sessions   one row per Clock In -> Clock Out pair
--                             (session_end NULL while still clocked in)
--
-- hcm_attendance_records stays one row per employee per day: check_in is
-- the day's first Clock In, check_out the last Clock Out (NULL while a
-- session is open) and work_hours the SUM of the closed sessions -- so
-- payroll, reports, regularization and exports keep reading the same
-- fields. Existing days are backfilled as one session each.
--
-- Additive and idempotent; _template plus every HRMS tenant schema.
--
--     cd backend && venv/Scripts/python db/run_hrms_migration.py db/add_attendance_sessions.sql

DO $$
DECLARE
    s text;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        IF to_regclass(format('%I.hcm_attendance_records', s)) IS NULL THEN
            CONTINUE;
        END IF;

        EXECUTE format('CREATE TABLE IF NOT EXISTS %1$I.hcm_attendance_sessions (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
            employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
            attendance_record_id uuid NOT NULL
                REFERENCES %1$I.hcm_attendance_records(id) ON DELETE CASCADE,
            session_start timestamptz NOT NULL,
            session_end timestamptz,
            work_mode varchar(20),
            -- Closed by the system because the employee never clocked out
            -- (open longer than the allowed session); counts 0 h and the day
            -- shows "Missing clock-out" until regularized.
            missing_out boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (session_end IS NULL OR session_end >= session_start)
        )', s);

        EXECUTE format('ALTER TABLE %I.hcm_attendance_sessions
            ADD COLUMN IF NOT EXISTS missing_out boolean NOT NULL DEFAULT false', s);

        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_att_sessions_record
            ON %I.hcm_attendance_sessions (attendance_record_id, session_start)', s);
        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_att_sessions_employee_start
            ON %I.hcm_attendance_sessions (employee_id, session_start DESC)', s);
        -- At most one open session per employee: a double tap can never
        -- leave two sessions running.
        EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_att_sessions_one_open
            ON %I.hcm_attendance_sessions (employee_id) WHERE session_end IS NULL', s);

        -- Backfill: each existing punched day becomes one session. An
        -- employee with several open (never clocked-out) days keeps only the
        -- newest open; older ones are closed at their check-in (0 h, which
        -- is what they already counted as) and flagged missing_out.
        EXECUTE format('INSERT INTO %1$I.hcm_attendance_sessions
                (id, company_id, employee_id, attendance_record_id, session_start, session_end, work_mode, missing_out)
            SELECT gen_random_uuid(), r.company_id, r.employee_id, r.id, r.check_in,
                   CASE
                       WHEN r.check_out IS NOT NULL AND r.check_out >= r.check_in THEN r.check_out
                       WHEN r.check_out IS NOT NULL THEN r.check_in
                       WHEN r.id = (SELECT r2.id FROM %1$I.hcm_attendance_records r2
                                    WHERE r2.employee_id = r.employee_id
                                      AND r2.check_in IS NOT NULL AND r2.check_out IS NULL
                                    ORDER BY r2.check_in DESC LIMIT 1) THEN NULL
                       ELSE r.check_in
                   END,
                   r.work_mode,
                   (r.check_out IS NULL AND r.id <> (SELECT r3.id FROM %1$I.hcm_attendance_records r3
                                    WHERE r3.employee_id = r.employee_id
                                      AND r3.check_in IS NOT NULL AND r3.check_out IS NULL
                                    ORDER BY r3.check_in DESC LIMIT 1))
            FROM %1$I.hcm_attendance_records r
            WHERE r.check_in IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM %1$I.hcm_attendance_sessions x
                              WHERE x.attendance_record_id = r.id)', s);
    END LOOP;
END $$;
