-- Work Entry > Project > "Other": an employee may log time on an active
-- company project they are NOT assigned to. pm_time_entries.is_other_project
-- marks such entries (true = logged via "Other", i.e. not on one of the
-- employee's assigned projects at the time) so timesheets and reports can
-- show them. Additive only: every existing entry is false (assigned).
-- Idempotent; _template + every tenant that has pm_time_entries.

CREATE OR REPLACE FUNCTION pg_temp.weo_apply(s text) RETURNS void AS $f$
BEGIN
    IF to_regclass(format('%I.pm_time_entries', s)) IS NULL THEN
        RETURN;
    END IF;
    EXECUTE format('ALTER TABLE %I.pm_time_entries ADD COLUMN IF NOT EXISTS is_other_project boolean '
                   'NOT NULL DEFAULT false', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.weo_apply('_template');
    FOR tenant IN SELECT t.slug FROM public.tenants t LOOP
        PERFORM pg_temp.weo_apply(tenant.slug);
    END LOOP;
END $$;
