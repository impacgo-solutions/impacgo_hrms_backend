-- Adds the tenant-configurable Designation-level permission layer for the
-- granular (resource, action) catalog (backend/app/permission_actions.py) --
-- additive to, and independent of, core_role_permissions.
--
-- core_designation_permissions is a DENY-LIST, not a grant list: presence of
-- a (designation_id, permission_id) row means that Designation is EXPLICITLY
-- RESTRICTED from that action, overriding whatever the user's Role(s) grant.
-- Absence of a row means "not restricted by Designation -- deferred entirely
-- to Role". This encodes "Designation can only narrow, never widen" as the
-- simplest possible model: effective = role_grants AND NOT designation_denies.
--
-- Backward compatible: the table starts empty for every existing designation
-- in every tenant, so nothing is restricted until an admin explicitly opts a
-- Designation into a restriction via the new /api/designations/{id}/actions
-- endpoint. No existing employee's effective access changes as a result of
-- this migration alone.
--
-- Plain nullable-free uuid columns, no DB-level FK constraints -- same
-- convention as every other table added this session (provision_tenant's
-- `LIKE ... INCLUDING ALL` clone never copies FK constraints anyway; this
-- codebase enforces referential integrity at the app layer throughout).

CREATE TABLE IF NOT EXISTS _template.core_designation_permissions (
    id             uuid DEFAULT gen_random_uuid() NOT NULL PRIMARY KEY,
    designation_id uuid NOT NULL,
    permission_id  uuid NOT NULL,
    created_by     uuid,
    created_at     timestamp with time zone DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS core_designation_permissions_designation_id_idx
    ON _template.core_designation_permissions (designation_id);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.core_designation_permissions (
                id             uuid DEFAULT gen_random_uuid() NOT NULL PRIMARY KEY,
                designation_id uuid NOT NULL,
                permission_id  uuid NOT NULL,
                created_by     uuid,
                created_at     timestamp with time zone DEFAULT now() NOT NULL
            )',
            tenant.slug
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS core_designation_permissions_designation_id_idx ON %I.core_designation_permissions (designation_id)',
            tenant.slug
        );
        RAISE NOTICE 'core_designation_permissions provisioned for tenant %', tenant.slug;
    END LOOP;
END $$;
