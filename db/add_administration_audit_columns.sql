-- Adds the standard audit trail (created_at/created_by/updated_at/updated_by)
-- to the Administration-module tables that are missing some or all of it,
-- and adds the composite uniqueness core_field_rules always claimed to have
-- (crud.py's comment said so) but which was never actually created as a DB
-- constraint -- verified directly against the live DB before writing this.
--
-- NOT executed by the assistant -- review and run by hand, same convention
-- as backend/db/backfill_core_bands_existing_tenants.sql.
--
-- Current live state (checked via information_schema.columns /
-- pg_constraint against _template):
--   core_bands                     -- already has all 4 columns (untouched here)
--   core_field_rules               -- has updated_at/updated_by only
--   core_roles                     -- has none
--   core_designations              -- has none
--   core_hierarchy_role_bindings   -- has none
--   core_permissions               -- has none
--   core_role_permissions          -- has none
--   core_user_roles                -- has none
--   core_approval_workflows        -- has none
--   core_approval_workflow_steps   -- has none
--
-- Safe to re-run: every ALTER uses ADD COLUMN IF NOT EXISTS, and the new
-- constraint is wrapped so a second run is a no-op instead of an error.
--
-- Because public.provision_tenant() clones _template's CURRENT tables
-- dynamically at the moment a new tenant is created (confirmed against the
-- live function body, same as backfill_core_bands_existing_tenants.sql's
-- own analysis), running this once against _template means every tenant
-- provisioned AFTER this script runs already has these columns -- no
-- provisioning code changes needed. The loop over public.tenants below is
-- what backfills tenants that already existed before this script ran.

DO $$
DECLARE
    t record;
    schemas text[];
    s text;
BEGIN
    -- _template plus every already-provisioned tenant schema.
    SELECT array_agg(slug) INTO schemas FROM (
        SELECT '_template'::text AS slug
        UNION ALL
        SELECT slug FROM public.tenants
    ) x;

    FOREACH s IN ARRAY schemas LOOP
        -- 1. Four-column audit trail on the tables that currently have none.
        EXECUTE format('ALTER TABLE %I.core_roles
            ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS updated_by uuid', s);

        EXECUTE format('ALTER TABLE %I.core_designations
            ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS updated_by uuid', s);

        EXECUTE format('ALTER TABLE %I.core_hierarchy_role_bindings
            ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS updated_by uuid', s);

        EXECUTE format('ALTER TABLE %I.core_permissions
            ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS updated_by uuid', s);

        EXECUTE format('ALTER TABLE %I.core_role_permissions
            ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS updated_by uuid', s);

        EXECUTE format('ALTER TABLE %I.core_user_roles
            ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS updated_by uuid', s);

        EXECUTE format('ALTER TABLE %I.core_approval_workflows
            ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS updated_by uuid', s);

        EXECUTE format('ALTER TABLE %I.core_approval_workflow_steps
            ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS updated_by uuid', s);

        -- 2. core_field_rules already has updated_at/updated_by -- only
        --    created_at/created_by are missing.
        EXECUTE format('ALTER TABLE %I.core_field_rules
            ADD COLUMN IF NOT EXISTS created_at timestamp with time zone DEFAULT now() NOT NULL,
            ADD COLUMN IF NOT EXISTS created_by uuid', s);

        -- 3. The composite uniqueness crud.py's upsert_field_rules comment
        --    already claimed existed (company_id, entity_key, field_key,
        --    role_id) -- role_id is nullable (NULL = company-wide default),
        --    so this needs NULLS NOT DISTINCT (Postgres 15+; live DB is
        --    16.14) to actually dedupe the common NULL-role_id case instead
        --    of silently allowing duplicate company-wide rows.
        --
        --    The check-then-insert this replaces was racy under concurrent
        --    PUTs, so real duplicate rows may already exist -- de-dupe
        --    first (keep the most-recently-updated row of each key, which
        --    is the one that should have "won" anyway), then add the
        --    constraint.
        EXECUTE format(
            'DELETE FROM %I.core_field_rules f
             WHERE f.id IN (
                 SELECT id FROM (
                     SELECT id, row_number() OVER (
                         PARTITION BY company_id, entity_key, field_key, role_id
                         ORDER BY updated_at DESC, id DESC
                     ) AS rn
                     FROM %I.core_field_rules
                 ) ranked
                 WHERE ranked.rn > 1
             )', s, s
        );

        BEGIN
            EXECUTE format(
                'ALTER TABLE %I.core_field_rules
                 ADD CONSTRAINT core_field_rules_unique
                 UNIQUE NULLS NOT DISTINCT (company_id, entity_key, field_key, role_id)',
                s
            );
        EXCEPTION WHEN duplicate_object THEN
            NULL; -- already added by a previous run
        END;

        RAISE NOTICE 'Administration audit columns ensured for schema %', s;
    END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- Verification queries to run afterward (read-only)
-- ----------------------------------------------------------------------------
-- SELECT table_name, column_name FROM information_schema.columns
--   WHERE table_schema = '_template'
--     AND table_name IN ('core_roles','core_designations','core_bands',
--       'core_hierarchy_role_bindings','core_field_rules',
--       'core_approval_workflows','core_approval_workflow_steps',
--       'core_permissions','core_role_permissions','core_user_roles')
--     AND column_name IN ('created_at','created_by','updated_at','updated_by')
--   ORDER BY 1, 2;
--
-- SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint WHERE conrelid = '_template.core_field_rules'::regclass;
--   -- should now include core_field_rules_unique.
