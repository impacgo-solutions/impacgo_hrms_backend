-- Task Management review workflow: a task assigned to one employee, worked
-- on, submitted with a completion description + files, reviewed by a
-- chosen reviewer or the assignee's Reporting Manager, and only then
-- Completed.
--
--   pm_tasks              + assignee / assigned date / deadline / workflow
--                           status / current reviewer / submitted & completed
--   pm_task_submissions   one row per submission round (description, chosen
--                           reviewer, review decision + comments); files are
--                           core_attachments rows (entity_type 'task_submission')
--   pm_task_history       every workflow transition (audit trail)
--
-- workflow_status NULL = an unassigned task: the board behaves exactly as
-- before (free "Move to" between columns).
-- Additive only; idempotent; every tenant schema that has pm_tasks + _template.

CREATE OR REPLACE FUNCTION pg_temp.tw_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format('ALTER TABLE %1$I.pm_tasks '
                   'ADD COLUMN IF NOT EXISTS assignee_employee_id uuid, '
                   'ADD COLUMN IF NOT EXISTS assigned_date date, '
                   'ADD COLUMN IF NOT EXISTS deadline_at timestamptz, '
                   'ADD COLUMN IF NOT EXISTS workflow_status varchar(20), '
                   'ADD COLUMN IF NOT EXISTS reviewer_employee_id uuid, '
                   'ADD COLUMN IF NOT EXISTS submitted_at timestamptz, '
                   'ADD COLUMN IF NOT EXISTS completed_at timestamptz, '
                   'ADD COLUMN IF NOT EXISTS completed_by uuid', s);
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'ck_pm_tasks_workflow_status'
                     AND conrelid = format('%I.pm_tasks', s)::regclass) THEN
        EXECUTE format('ALTER TABLE %1$I.pm_tasks ADD CONSTRAINT ck_pm_tasks_workflow_status CHECK '
                       '(workflow_status IS NULL OR workflow_status IN '
                       '(''assigned'', ''in_progress'', ''submitted'', ''changes_requested'', ''completed''))', s);
    END IF;
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_pm_tasks_assignee ON %1$I.pm_tasks (company_id, assignee_employee_id)', s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.pm_task_submissions (
            id                   uuid PRIMARY KEY,
            company_id           uuid NOT NULL,
            task_id              uuid NOT NULL,
            round                smallint NOT NULL,
            description          text NOT NULL,
            submitted_by         uuid NOT NULL,
            submitted_at         timestamptz NOT NULL,
            reviewer_employee_id uuid,
            decision             varchar(20),
            review_comments      text,
            reviewed_by          uuid,
            reviewed_at          timestamptz,
            created_at           timestamptz,
            created_by           uuid,
            updated_at           timestamptz,
            updated_by           uuid,
            CONSTRAINT ck_pm_task_submissions_decision
                CHECK (decision IS NULL OR decision IN ('approved', 'changes_requested'))
        )$q$, s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_task_submissions_round ON %1$I.pm_task_submissions (task_id, round)', s);
    -- At most one submission awaiting review per task.
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_task_submissions_open ON %1$I.pm_task_submissions '
                   '(task_id) WHERE decision IS NULL', s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.pm_task_history (
            id                uuid PRIMARY KEY,
            company_id        uuid NOT NULL,
            task_id           uuid NOT NULL,
            action            varchar(40) NOT NULL,
            from_status       varchar(20),
            to_status         varchar(20),
            actor_user_id     uuid,
            actor_employee_id uuid,
            comments          text,
            created_at        timestamptz NOT NULL
        )$q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_pm_task_history_task ON %1$I.pm_task_history (task_id, created_at)', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = '_template' AND table_name = 'pm_tasks') THEN
        PERFORM pg_temp.tw_apply('_template');
    END IF;
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'pm_tasks'
        )
    LOOP
        PERFORM pg_temp.tw_apply(tenant.slug);
    END LOOP;
END $$;
