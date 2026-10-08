-- Adds core_employees.photo_url -- the stored file_url (see storage.py's
-- save_uploaded_file, entity_type="employee_photo") of an employee's
-- self-uploaded profile picture. Nullable: NULL means "no photo set yet",
-- and every avatar display falls back to initials exactly as it did before
-- this column existed.
--
-- Same convention as add_designation_band_id.sql: alter the _template
-- schema (used when provisioning brand-new tenants) plus every already-
-- provisioned tenant's own schema, since this is a schema-per-tenant
-- deployment and core_employees lives once per tenant, not once globally.

ALTER TABLE _template.core_employees
    ADD COLUMN IF NOT EXISTS photo_url text;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        EXECUTE format(
            'ALTER TABLE %I.core_employees ADD COLUMN IF NOT EXISTS photo_url text',
            tenant.slug
        );
        RAISE NOTICE 'Employee.photo_url added for tenant %', tenant.slug;
    END LOOP;
END $$;
