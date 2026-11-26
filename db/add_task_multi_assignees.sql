-- Several assignees per task (one shared task workflow, see app/tasks.py).
--
--   pm_task_assignments   one row per (task, employee) with role 'assignee';
--                         the table already existed (unused) in most schemas
--                         -- created here where missing, plus company_id,
--                         sort_order (the first assignee is the primary) and
--                         indexes.
--   pm_tasks.assignee_employee_id stays = the primary (first) assignee, so
--   every existing reader keeps working.
--
-- Backfills one 'assignee' row per already-assigned task.
-- Additive only; idempotent; every tenant schema that has pm_tasks + _template.

CREATE OR REPLACE FUNCTION pg_temp.tma_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.pm_task_assignments (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id         uuid NOT NULL,
            employee_id     uuid NOT NULL,
            assignment_role varchar(15) NOT NULL DEFAULT 'assignee',
            assigned_by     uuid,
            assigned_at     timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pm_task_assignments_assignment_role_check
                CHECK (assignment_role IN ('assignee', 'reviewer', 'collaborator')),
            CONSTRAINT pm_task_assignments_task_id_employee_id_assignment_role_key
                UNIQUE (task_id, employee_id, assignment_role)
        )$q$, s);
    EXECUTE format('ALTER TABLE %1$I.pm_task_assignments '
                   'ADD COLUMN IF NOT EXISTS company_id uuid, '
                   'ADD COLUMN IF NOT EXISTS sort_order smallint NOT NULL DEFAULT 0', s);
    EXECUTE format('UPDATE %1$I.pm_task_assignments a SET company_id = t.company_id '
                   'FROM %1$I.pm_tasks t WHERE a.task_id = t.id AND a.company_id IS NULL', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_pm_task_assignments_task ON %1$I.pm_task_assignments (task_id)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_pm_task_assignments_employee '
                   'ON %1$I.pm_task_assignments (company_id, employee_id)', s);
    -- Backfill: the existing single assignee becomes the primary assignee row.
    EXECUTE format('INSERT INTO %1$I.pm_task_assignments '
                   '(id, task_id, employee_id, assignment_role, assigned_at, company_id, sort_order) '
                   'SELECT gen_random_uuid(), t.id, t.assignee_employee_id, ''assignee'', '
                   'COALESCE(t.updated_at, now()), t.company_id, 0 '
                   'FROM %1$I.pm_tasks t WHERE t.assignee_employee_id IS NOT NULL '
                   'ON CONFLICT (task_id, employee_id, assignment_role) DO NOTHING', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = '_template' AND table_name = 'pm_tasks') THEN
        PERFORM pg_temp.tma_apply('_template');
    END IF;
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'pm_tasks'
        )
    LOOP
        PERFORM pg_temp.tma_apply(tenant.slug);
    END LOOP;
END $$;
