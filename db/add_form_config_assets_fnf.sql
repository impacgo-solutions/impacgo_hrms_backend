-- add_form_config_assets_fnf.sql (2026-10-29)
--
-- "Add custom..." on the asset Return Condition and the F&F Payment Mode
-- dropdowns (Form Configuration keys asset.return_condition /
-- fnf.payment_mode). Admin/user-added labels are stored verbatim, so the
-- columns must be as wide as an option value (120). Run per tenant schema
-- and on _template. Additive and idempotent (widening only).
ALTER TABLE hcm_asset_assignments ALTER COLUMN return_condition TYPE varchar(120);
ALTER TABLE hcm_final_settlements ALTER COLUMN payment_mode     TYPE varchar(120);
