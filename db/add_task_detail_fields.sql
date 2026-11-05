-- Backs the Task Management > Task Details screen (crud.get_task_board_card_detail).
--
-- description/due_date/planned_start_date/estimated_hours already exist on
-- pm_tasks for most tenants (confirmed live: Infyq, acme, impacgo-solutions)
-- -- this was a genuine ORM mapping gap (models.TaskBoardCard never mapped
-- them), not a missing-column gap, for those. But two tenants (etorgroups,
-- impacgo) are missing these columns entirely -- without this migration,
-- mapping them on models.TaskBoardCard would break those two tenants'
-- EXISTING Task Board (GET /api/task-board already SELECTs every mapped
-- column). ADD COLUMN IF NOT EXISTS makes this a no-op everywhere the
-- column is already there.
--
-- Same convention as add_employee_photo_url.sql / add_celebration_log.sql:
-- alter the _template schema (new tenants) plus every already-provisioned
-- tenant's own schema.

ALTER TABLE _template.pm_tasks
    ADD COLUMN IF NOT EXISTS description text,
    ADD COLUMN IF NOT EXISTS due_date date,
    ADD COLUMN IF NOT EXISTS planned_start_date date,
    ADD COLUMN IF NOT EXISTS estimated_hours numeric(6, 2);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        EXECUTE format(
            'ALTER TABLE %I.pm_tasks
                ADD COLUMN IF NOT EXISTS description text,
                ADD COLUMN IF NOT EXISTS due_date date,
                ADD COLUMN IF NOT EXISTS planned_start_date date,
                ADD COLUMN IF NOT EXISTS estimated_hours numeric(6, 2)',
            tenant.slug
        );
        RAISE NOTICE 'pm_tasks detail fields ensured for tenant %', tenant.slug;
    END LOOP;
END $$;
