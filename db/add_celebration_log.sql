-- Backs the Birthday / Work Anniversary celebration reminders
-- (crud.check_daily_celebrations, called from GET /notifications --
-- same check-on-read pattern as check_overdue_milestones, since this app
-- has no background scheduler).
--
-- One row per (employee, celebration_type, year) that's already been
-- broadcast -- the UNIQUE constraint is the actual duplicate-prevention
-- mechanism: `INSERT ... ON CONFLICT (employee_id, celebration_type, year)
-- DO NOTHING RETURNING id` is atomic, so repeated or concurrent calls the
-- same day (every login hits GET /notifications) never fire the
-- notification/email broadcast twice. Keying on `year` (not just a date)
-- means the same employee's birthday naturally fires again next year
-- without any cleanup job.
--
-- Same convention as add_designation_band_id.sql / add_employee_photo_url.sql:
-- alter the _template schema (new tenants) plus every already-provisioned
-- tenant's own schema.

CREATE TABLE IF NOT EXISTS _template.hcm_celebration_log (
    id uuid PRIMARY KEY,
    employee_id uuid NOT NULL,
    celebration_type varchar(20) NOT NULL,
    year integer NOT NULL,
    notified_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT hcm_celebration_log_unique UNIQUE (employee_id, celebration_type, year)
);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.hcm_celebration_log (
                id uuid PRIMARY KEY,
                employee_id uuid NOT NULL,
                celebration_type varchar(20) NOT NULL,
                year integer NOT NULL,
                notified_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT hcm_celebration_log_unique UNIQUE (employee_id, celebration_type, year)
            )',
            tenant.slug
        );
        RAISE NOTICE 'hcm_celebration_log created for tenant %', tenant.slug;
    END LOOP;
END $$;
