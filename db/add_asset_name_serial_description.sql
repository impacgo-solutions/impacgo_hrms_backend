-- Assets > Create / Edit Asset: Asset Name, Serial Number, Description.
-- All optional (NULL = not entered), so every existing asset stays valid
-- and unchanged. Additive only; idempotent; _template + every tenant that
-- has hcm_asset_inventory.

CREATE OR REPLACE FUNCTION pg_temp.ansd_apply(s text) RETURNS void AS $f$
BEGIN
    IF to_regclass(format('%I.hcm_asset_inventory', s)) IS NULL THEN
        RETURN;
    END IF;
    EXECUTE format('ALTER TABLE %I.hcm_asset_inventory ADD COLUMN IF NOT EXISTS asset_name varchar(200)', s);
    EXECUTE format('ALTER TABLE %I.hcm_asset_inventory ADD COLUMN IF NOT EXISTS serial_number varchar(120)', s);
    EXECUTE format('ALTER TABLE %I.hcm_asset_inventory ADD COLUMN IF NOT EXISTS description text', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.ansd_apply('_template');
    FOR tenant IN SELECT t.slug FROM public.tenants t LOOP
        PERFORM pg_temp.ansd_apply(tenant.slug);
    END LOOP;
END $$;
