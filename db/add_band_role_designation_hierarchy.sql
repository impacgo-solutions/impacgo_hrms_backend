-- Adds the tenant-configurable Band -> Role -> Designation hierarchy:
--   core_roles.band_id        -- which Band this Role belongs to (nullable)
--   core_designations.role_id -- which Role this Designation belongs to (nullable)
--
-- Plain nullable uuid columns, no DB-level FK constraint -- matches this
-- schema's existing convention (see core_employees.band, core_bands itself):
-- referential integrity is enforced at the app layer via crud.py, not via
-- Postgres constraints, throughout this codebase. This also sidesteps a real
-- gotcha: public.provision_tenant() clones new tenants via
-- `CREATE TABLE ... (LIKE _template.tbl INCLUDING ALL)`, and Postgres's LIKE
-- clause never copies FOREIGN KEY constraints regardless of INCLUDING ALL --
-- so a real FK here would silently not exist on any tenant provisioned after
-- this migration anyway.
--
-- Backward compatible: both columns are nullable and default to NULL, so
-- every existing Role/Designation row is unaffected (unconfigured) until an
-- admin explicitly maps it through the Roles/Designations screens.
--
-- Run once against _template (so all future tenants inherit these columns
-- automatically via provision_tenant's LIKE-based clone) and against every
-- existing tenant schema (which provision_tenant already created before this
-- migration existed, so they need the ALTER applied directly).

ALTER TABLE _template.core_roles
    ADD COLUMN IF NOT EXISTS band_id uuid;

ALTER TABLE _template.core_designations
    ADD COLUMN IF NOT EXISTS role_id uuid;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        EXECUTE format(
            'ALTER TABLE %I.core_roles ADD COLUMN IF NOT EXISTS band_id uuid',
            tenant.slug
        );
        EXECUTE format(
            'ALTER TABLE %I.core_designations ADD COLUMN IF NOT EXISTS role_id uuid',
            tenant.slug
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS core_roles_band_id_idx ON %I.core_roles (band_id)',
            tenant.slug
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS core_designations_role_id_idx ON %I.core_designations (role_id)',
            tenant.slug
        );
        RAISE NOTICE 'Band/Role/Designation hierarchy columns added for tenant %', tenant.slug;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS core_roles_band_id_idx ON _template.core_roles (band_id);
CREATE INDEX IF NOT EXISTS core_designations_role_id_idx ON _template.core_designations (role_id);
