-- Add Employee > Employment Type: Consultant is no longer offered. Each
-- company's options are stored rows (hcm_form_field_options, seeded from
-- form_config.FIELDS), so the built-in Consultant row is hidden -- not
-- deleted: an employee already stored as Consultant keeps showing its label
-- and saves unchanged, and an admin can re-enable it in Form Configuration.
-- Idempotent; _template plus every HRMS tenant schema.
--
--     cd backend && venv/Scripts/python db/run_hrms_migration.py db/remove_consultant_employment_type.sql

DO $$
DECLARE
    s text;
    n int;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema = s AND table_name = 'hcm_form_field_options') THEN
            CONTINUE;
        END IF;
        EXECUTE format('UPDATE %1$I.hcm_form_field_options SET is_active = false
                        WHERE field_key = ''employee.employment_type''
                          AND value = ''Consultant'' AND is_active', s);
        GET DIAGNOSTICS n = ROW_COUNT;
        RAISE NOTICE 'Consultant employment type hidden for % (% rows)', s, n;
    END LOOP;
END $$;
