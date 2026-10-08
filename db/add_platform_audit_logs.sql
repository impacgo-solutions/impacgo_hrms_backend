-- SA-06 (2026-09-25): audit trail for HRMS Super Admin (platform) actions.
-- Additive and idempotent; the API tolerates the table being absent (actions
-- are then simply not recorded), so it can be applied before or after deploy.
CREATE TABLE IF NOT EXISTS public.platform_audit_logs (
    id              uuid PRIMARY KEY,
    actor_admin_id  uuid NULL,
    actor_email     varchar(150) NULL,
    action          varchar(60) NOT NULL,
    tenant_id       uuid NULL,
    tenant_slug     varchar(60) NULL,
    changes         jsonb NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_platform_audit_logs_created_at
    ON public.platform_audit_logs (created_at DESC);
