-- Adds hcm_asset_inventory.asset_category -- who the asset is for, picked
-- in Create Asset: 'personal', 'office' or 'client' (labels Personal /
-- Office / Client). Required for every NEW asset (the API rejects a create
-- without it); nullable in the table so assets created before this column
-- existed stay valid and simply show "—" until recategorised.
--
-- Same convention as add_employee_photo_url.sql: the _template schema
-- (new tenants) plus every already-provisioned tenant schema. Idempotent.

ALTER TABLE _template.hcm_asset_inventory
    ADD COLUMN IF NOT EXISTS asset_category varchar(15);
ALTER TABLE _template.hcm_asset_inventory
    DROP CONSTRAINT IF EXISTS hcm_asset_inventory_asset_category_check;
ALTER TABLE _template.hcm_asset_inventory
    ADD CONSTRAINT hcm_asset_inventory_asset_category_check
    CHECK (asset_category IS NULL OR asset_category IN ('personal', 'office', 'client'));

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN SELECT slug FROM public.tenants LOOP
        IF to_regclass(format('%I.hcm_asset_inventory', tenant.slug)) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('ALTER TABLE %I.hcm_asset_inventory ADD COLUMN IF NOT EXISTS asset_category varchar(15)',
                       tenant.slug);
        EXECUTE format('ALTER TABLE %I.hcm_asset_inventory DROP CONSTRAINT IF EXISTS '
                       'hcm_asset_inventory_asset_category_check', tenant.slug);
        EXECUTE format('ALTER TABLE %I.hcm_asset_inventory ADD CONSTRAINT hcm_asset_inventory_asset_category_check '
                       'CHECK (asset_category IS NULL OR asset_category IN (''personal'', ''office'', ''client''))',
                       tenant.slug);
        RAISE NOTICE 'hcm_asset_inventory.asset_category added for tenant %', tenant.slug;
    END LOOP;
END $$;
