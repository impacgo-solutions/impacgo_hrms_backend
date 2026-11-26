-- add_form_field_settings.sql (2026-10-29)
--
-- Administration > Form Configuration: per-list switch "Allow users to add
-- their own option" (the "Add custom..." row at the end of a dropdown).
-- Tenant table -- run per tenant schema and on _template:
--     SET search_path TO "<tenant_slug>", public;
--     \i add_form_field_settings.sql
-- Additive and idempotent. No row = the default (users may add).

CREATE TABLE IF NOT EXISTS hcm_form_field_settings (
    company_id      uuid NOT NULL REFERENCES core_companies(id),
    field_key       varchar(80) NOT NULL,
    allow_user_add  boolean NOT NULL DEFAULT true,
    updated_by      uuid,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, field_key)
);
