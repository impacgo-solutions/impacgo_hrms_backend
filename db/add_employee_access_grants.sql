-- add_employee_access_grants.sql (2026-09-26)
--
-- Administration > Roles & Permissions > Employee Module Access: extra
-- access to a BUILT-IN module granted to individual employees, on top of
-- whatever their role gives them (additive -- roles are unchanged). One row
-- per (employee, catalog resource) with the granted catalog actions
-- (public.permission_actions). Tenant table: run per tenant schema and on
-- _template. The API treats a schema without it as "no employee grants".
CREATE TABLE IF NOT EXISTS core_employee_access_grants (
    id           uuid PRIMARY KEY,
    employee_id  uuid NOT NULL REFERENCES core_employees(id),
    resource     varchar(40) NOT NULL,
    actions      text[] NOT NULL,
    granted_by   uuid,
    granted_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz,
    CONSTRAINT ux_core_employee_access_grant UNIQUE (employee_id, resource)
);
CREATE INDEX IF NOT EXISTS ix_core_employee_access_grants_resource
    ON core_employee_access_grants (resource);
