-- add_form_configuration.sql (2026-10-28)
--
-- Administration > Form Configuration: per-company dropdown options for the
-- configurable form fields (work-entry category, travel type, candidate
-- source, ...) and admin-defined custom fields with their per-record values.
-- Tenant tables -- run per tenant schema (and on _template so new tenants get
-- them):
--     SET search_path TO "<tenant_slug>", public;
--     \i add_form_configuration.sql
-- Additive and idempotent. A schema without these tables simply falls back to
-- the built-in option lists (the API treats the tables as optional).

CREATE TABLE IF NOT EXISTS hcm_form_field_options (
    id          uuid PRIMARY KEY,
    company_id  uuid NOT NULL REFERENCES core_companies(id),
    -- e.g. 'travel.type' (built-in field) or 'custom.<custom field id>'
    field_key   varchar(80) NOT NULL,
    -- what is stored on the record; label is what the user sees
    value       varchar(120) NOT NULL,
    label       varchar(120) NOT NULL,
    sort_order  integer NOT NULL DEFAULT 0,
    is_active   boolean NOT NULL DEFAULT true,
    -- seeded built-in option: can be hidden/renamed but never hard-deleted
    is_system   boolean NOT NULL DEFAULT false,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hcm_form_field_options_value
    ON hcm_form_field_options (company_id, field_key, lower(value));
CREATE INDEX IF NOT EXISTS ix_hcm_form_field_options_field
    ON hcm_form_field_options (company_id, field_key, sort_order);

CREATE TABLE IF NOT EXISTS hcm_custom_fields (
    id          uuid PRIMARY KEY,
    company_id  uuid NOT NULL REFERENCES core_companies(id),
    form_key    varchar(60) NOT NULL,
    field_key   varchar(60) NOT NULL,
    label       varchar(120) NOT NULL,
    field_type  varchar(20) NOT NULL
                CHECK (field_type IN ('text', 'textarea', 'number', 'date', 'dropdown', 'checkbox')),
    is_required boolean NOT NULL DEFAULT false,
    help_text   varchar(200),
    sort_order  integer NOT NULL DEFAULT 0,
    is_active   boolean NOT NULL DEFAULT true,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hcm_custom_fields_key
    ON hcm_custom_fields (company_id, form_key, field_key);

CREATE TABLE IF NOT EXISTS hcm_custom_field_values (
    id               uuid PRIMARY KEY,
    company_id       uuid NOT NULL REFERENCES core_companies(id),
    custom_field_id  uuid NOT NULL REFERENCES hcm_custom_fields(id) ON DELETE CASCADE,
    entity_id        uuid NOT NULL,
    value            text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ux_hcm_custom_field_value UNIQUE (custom_field_id, entity_id)
);
CREATE INDEX IF NOT EXISTS ix_hcm_custom_field_values_entity
    ON hcm_custom_field_values (company_id, entity_id);
