-- Asset images (Assets > Create Asset > Upload Asset Images; shown in Asset
-- Details). One row per image, many per asset. The file itself is stored by
-- storage.save_uploaded_file under uploads/asset_image/<asset_id>/<uuid>.<ext>
-- and served only through the authenticated, signed /media route
-- (media_access.authorize: same company + Asset Management access or the
-- employee the asset is assigned to). Deleting an asset deletes its rows.
-- Additive only, no rows inserted. Idempotent; runs on _template and every
-- tenant that has hcm_asset_inventory.

CREATE TABLE IF NOT EXISTS _template.hcm_asset_images (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    asset_id uuid NOT NULL REFERENCES _template.hcm_asset_inventory(id) ON DELETE CASCADE,
    file_url text NOT NULL,
    file_name varchar(255) NOT NULL,
    size_bytes integer NOT NULL DEFAULT 0,
    sort_order integer NOT NULL DEFAULT 0,
    uploaded_by uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_asset_images_asset
    ON _template.hcm_asset_images (asset_id, sort_order);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_asset_inventory'
        )
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.hcm_asset_images (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
                asset_id uuid NOT NULL REFERENCES %1$I.hcm_asset_inventory(id) ON DELETE CASCADE,
                file_url text NOT NULL,
                file_name varchar(255) NOT NULL,
                size_bytes integer NOT NULL DEFAULT 0,
                sort_order integer NOT NULL DEFAULT 0,
                uploaded_by uuid,
                created_at timestamptz NOT NULL DEFAULT now()
            )', tenant.slug);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS ix_asset_images_asset
                ON %1$I.hcm_asset_images (asset_id, sort_order)', tenant.slug);
    END LOOP;
END $$;
