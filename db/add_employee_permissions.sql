-- add_employee_permissions.sql (2026-09-29)
--
-- Administration > Roles & Permissions > Employee Permissions: an access
-- level per (employee, module) that REPLACES what the employee's role gives
-- for that module (see backend/app/employee_permissions.py). Additive only:
-- creates one new table, changes no existing table or row.
-- Idempotent; applied to every tenant schema that has core_employees and to
-- _template (new tenants are cloned from it).

CREATE OR REPLACE FUNCTION pg_temp.ep_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.core_employee_permissions (
            id           uuid PRIMARY KEY,
            company_id   uuid NOT NULL,
            employee_id  uuid NOT NULL REFERENCES %1$I.core_employees(id),
            module       varchar(40) NOT NULL,
            level        varchar(10) NOT NULL
                CHECK (level IN ('none', 'self', 'view', 'create', 'edit', 'delete', 'approve', 'admin')),
            notes        text,
            created_by   uuid,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_by   uuid,
            updated_at   timestamptz,
            CONSTRAINT ux_core_employee_permission UNIQUE (employee_id, module)
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_core_employee_permissions_company ON %1$I.core_employee_permissions (company_id)', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.ep_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'core_employees'
        )
    LOOP
        PERFORM pg_temp.ep_apply(tenant.slug);
    END LOOP;
END $$;
