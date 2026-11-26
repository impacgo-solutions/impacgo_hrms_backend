-- add_custom_modules.sql (2026-09-26)
--
-- Administration > Roles & Permissions > Custom Modules: admin-defined
-- modules (a records workspace each), per-employee access grants, and the
-- module records. Tenant tables -- run per tenant schema (and on _template so
-- new tenants get them):
--     SET search_path TO "<tenant_slug>", public;
--     \i add_custom_modules.sql
-- Additive and idempotent; the API treats a schema without these tables as
-- "no custom modules" (nothing else changes).

CREATE TABLE IF NOT EXISTS core_custom_modules (
    id           uuid PRIMARY KEY,
    company_id   uuid NOT NULL REFERENCES core_companies(id),
    name         varchar(80) NOT NULL,
    description  text,
    icon         varchar(40),
    -- the actions this module supports (subset of view/create/edit/delete/approve/export)
    actions      text[] NOT NULL DEFAULT ARRAY['view']::text[],
    is_active    boolean NOT NULL DEFAULT true,
    created_by   uuid,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   uuid,
    updated_at   timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_core_custom_modules_company_name
    ON core_custom_modules (company_id, lower(name));

CREATE TABLE IF NOT EXISTS core_custom_module_assignments (
    id           uuid PRIMARY KEY,
    module_id    uuid NOT NULL REFERENCES core_custom_modules(id) ON DELETE CASCADE,
    employee_id  uuid NOT NULL REFERENCES core_employees(id),
    -- the actions this employee holds on the module (always includes 'view')
    actions      text[] NOT NULL,
    granted_by   uuid,
    granted_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz,
    CONSTRAINT ux_core_custom_module_assignment UNIQUE (module_id, employee_id)
);
CREATE INDEX IF NOT EXISTS ix_core_custom_module_assignments_employee
    ON core_custom_module_assignments (employee_id);

CREATE TABLE IF NOT EXISTS core_custom_module_records (
    id                   uuid PRIMARY KEY,
    module_id            uuid NOT NULL REFERENCES core_custom_modules(id) ON DELETE CASCADE,
    company_id           uuid NOT NULL REFERENCES core_companies(id),
    title                varchar(200) NOT NULL,
    details              text,
    status               varchar(20) NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'approved', 'rejected')),
    created_by_employee  uuid REFERENCES core_employees(id),
    updated_by_employee  uuid REFERENCES core_employees(id),
    decided_by_employee  uuid REFERENCES core_employees(id),
    decided_at           timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz
);
CREATE INDEX IF NOT EXISTS ix_core_custom_module_records_module
    ON core_custom_module_records (module_id, created_at DESC);
