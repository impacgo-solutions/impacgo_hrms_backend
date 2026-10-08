-- Leave Withdrawal: an employee's request to revoke a pending / approved
-- leave, decided through the same Reporting Manager approval workflow.
-- The original leave stays active until a withdrawal is approved; then it
-- is cancelled and its balance restored exactly once (restored_days).
--
--   hcm_leave_withdrawals  (one pending withdrawal per leave at a time)
--
-- New table only; hcm_leave_requests / allocations are not altered.
-- Idempotent; every tenant schema + _template.

CREATE OR REPLACE FUNCTION pg_temp.lw_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_leave_withdrawals (
            id                 uuid PRIMARY KEY,
            company_id         uuid NOT NULL,
            employee_id        uuid NOT NULL,
            leave_request_id   uuid NOT NULL,
            leave_status_before varchar(12) NOT NULL,
            reason             text NOT NULL,
            status             varchar(12) NOT NULL DEFAULT 'pending',
            requested_at       timestamptz NOT NULL,
            approver_id        uuid,
            decision_notes     text,
            decided_at         timestamptz,
            restored_days      numeric(5,1),
            restored_detail    text,
            created_at         timestamptz,
            created_by         uuid,
            updated_at         timestamptz,
            updated_by         uuid
        )$q$, s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_leave_withdrawal_pending ON %1$I.hcm_leave_withdrawals '
                   '(leave_request_id) WHERE status = ''pending''', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_leave_withdrawal_company ON %1$I.hcm_leave_withdrawals '
                   '(company_id, requested_at)', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.lw_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_leave_requests'
        )
    LOOP
        PERFORM pg_temp.lw_apply(tenant.slug);
    END LOOP;
END $$;
