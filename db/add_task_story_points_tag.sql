-- add_task_story_points_tag.sql (2026-10-28)
--
-- Task > New Task dialog carries "Story Points (optional)" and "Tag" but
-- pm_tasks had no columns for them (the create schema accepted them and the
-- router silently dropped them). Additive + idempotent; run on _template and
-- every tenant schema that has pm_tasks. REQUIRED in production before the
-- matching app code is deployed (models.TaskBoardCard maps both columns, and
-- every SELECT on pm_tasks names them).

ALTER TABLE _template.pm_tasks
    ADD COLUMN IF NOT EXISTS story_points integer,
    ADD COLUMN IF NOT EXISTS tag varchar(60);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        IF to_regclass(quote_ident(tenant.slug) || '.pm_tasks') IS NOT NULL THEN
            EXECUTE 'ALTER TABLE ' || quote_ident(tenant.slug) || '.pm_tasks '
                || 'ADD COLUMN IF NOT EXISTS story_points integer, '
                || 'ADD COLUMN IF NOT EXISTS tag varchar(60)';
        END IF;
    END LOOP;
END $$;
