-- Adds a DIRECT tenant-configurable Designation -> Band mapping:
--   core_designations.band_id -- which core_bands row this Designation
--   belongs to (nullable), independent of core_designations.role_id (the
--   Role -> Designation hierarchy from add_band_role_designation_hierarchy.
--   sql) -- a company can map a Designation straight to a Band without
--   going through a Role at all.
--
-- Plain nullable uuid, no DB-level FK constraint -- same convention as
-- every other band_id/role_id column added so far (see add_band_role_
-- designation_hierarchy.sql's comment: provision_tenant's `LIKE ...
-- INCLUDING ALL` clone never copies FK constraints anyway, and this
-- codebase enforces referential integrity at the app layer throughout).
--
-- Backward compatible: nullable, defaults to NULL -- every existing
-- designation (including the ones already stuck at
-- core_designations.band = 'Unassigned') is unaffected until an admin
-- explicitly maps it through the Designations screen, or a future Employee
-- Creation auto-creates it under a real Band selection (see crud.py's
-- create_employee/get_or_create_designation_by_name fix, which stops
-- writing 'Unassigned' going forward).

ALTER TABLE _template.core_designations
    ADD COLUMN IF NOT EXISTS band_id uuid;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        EXECUTE format(
            'ALTER TABLE %I.core_designations ADD COLUMN IF NOT EXISTS band_id uuid',
            tenant.slug
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS core_designations_band_id_idx ON %I.core_designations (band_id)',
            tenant.slug
        );
        RAISE NOTICE 'Designation.band_id added for tenant %', tenant.slug;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS core_designations_band_id_idx ON _template.core_designations (band_id);
