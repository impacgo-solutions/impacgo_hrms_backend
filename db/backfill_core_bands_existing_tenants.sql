-- Backfills core_bands into every ALREADY-PROVISIONED tenant schema that is
-- missing it. NOT executed by the assistant -- review and run by hand.
--
-- Why this is needed: public.provision_tenant() only clones _template's
-- CURRENT tables at the moment a NEW tenant is created (it loops
-- `pg_tables WHERE schemaname = '_template'` live, so any table added to
-- _template *before* a future provision_tenant() call is picked up
-- automatically -- confirmed against the live function body). It does
-- NOT retroactively touch tenants that were already provisioned before
-- core_bands existed. As of this analysis, that is:
--   - acme      -- already has core_bands (covered by
--                  backend/db/add_core_bands_table.sql's own migration)
--   - Infyq     -- already has core_bands (added by hand in the session
--                  that built the Band feature)
--   - impacgo   -- MISSING core_bands (registered in public.tenants,
--                  'core' module enabled, 1 real company / 1 employee /
--                  1 login -- GET/POST/PATCH/DELETE /api/bands and the Add
--                  Employee "Grade / Band" dropdown will 500 for this
--                  tenant today with "relation core_bands does not exist")
--
-- This script is written to be safe to run against ALL of them anyway
-- (IF NOT EXISTS + a per-tenant "already has rows" guard on the seed
-- INSERT), so it doesn't need to be hand-edited if another already-
-- provisioned tenant is found later, or if this is run again after a
-- future tenant is added.
--
-- Uses a DO block to loop every schema registered in public.tenants,
-- mirroring provision_tenant()'s own "discover dynamically, don't
-- hardcode names" style rather than listing tenant slugs by hand.

DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT slug FROM public.tenants LOOP
        -- 1. Create the table if this tenant doesn't have it yet.
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.core_bands (
                id           uuid DEFAULT gen_random_uuid() NOT NULL,
                company_id   uuid NOT NULL,
                name         character varying(80) NOT NULL,
                code         character varying(20) NOT NULL,
                band_number  integer NOT NULL,
                description  text,
                is_active    boolean DEFAULT true NOT NULL,
                created_by   uuid,
                created_at   timestamp with time zone DEFAULT now() NOT NULL,
                updated_by   uuid,
                updated_at   timestamp with time zone DEFAULT now() NOT NULL,
                CONSTRAINT core_bands_pkey PRIMARY KEY (id)
            )', t.slug
        );

        -- 2. Backfill default bands for every company in this tenant schema
        --    that has zero rows in core_bands -- same 10 bands, same
        --    numbering/order as backend/app/designation_seed_data.py
        --    DESIGNATION_BANDS, matching what add_core_bands_table.sql
        --    already did for acme. (crud.get_or_seed_company_bands would
        --    also lazily do this on first API call for any company still
        --    missing rows, so this step is a convenience, not the only
        --    safety net -- but running it means the Organization > Bands
        --    tab and Add Employee dropdown show real data on first load
        --    instead of triggering that lazy path.)
        EXECUTE format(
            'INSERT INTO %I.core_bands (company_id, name, code, band_number, description)
             SELECT c.id, band.name, band.code, band.band_number, NULL
             FROM %I.core_companies c
             CROSS JOIN (VALUES
                 (1, ''Ownership / Founding'', ''BAND-01''),
                 (2, ''C-Level Executive'', ''BAND-02''),
                 (3, ''VP Level'', ''BAND-03''),
                 (4, ''Director Level'', ''BAND-04''),
                 (5, ''General Management'', ''BAND-05''),
                 (6, ''Management'', ''BAND-06''),
                 (7, ''Team Lead'', ''BAND-07''),
                 (8, ''Professional / IC (Senior)'', ''BAND-08''),
                 (9, ''Professional / IC'', ''BAND-09''),
                 (10, ''Associate / Entry Level'', ''BAND-10'')
             ) AS band(band_number, name, code)
             WHERE NOT EXISTS (
                 SELECT 1 FROM %I.core_bands b WHERE b.company_id = c.id
             )', t.slug, t.slug, t.slug
        );

        RAISE NOTICE 'core_bands ensured for tenant %', t.slug;
    END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- Verification queries to run afterward (read-only)
-- ----------------------------------------------------------------------------
-- SELECT table_schema, count(*) FROM information_schema.tables
--   WHERE table_name = 'core_bands' GROUP BY table_schema;
--   -- should include every slug from `SELECT slug FROM public.tenants`,
--   -- plus _template.
--
-- SELECT 'impacgo' AS schema, count(*) FROM impacgo.core_bands;
--   -- should be 10 once this script has run.
