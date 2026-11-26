-- Backs Administration > Notifications -- an admin-controlled, per-company,
-- per-event, per-channel (email / inapp) on/off matrix. This is a NEW,
-- separate table from the older, unused core_notification_preferences
-- (per-user; never wired to any send path -- left untouched).
--
-- Only overrides are stored: a missing row for (company_id, event_key,
-- channel) means "use the code's built-in default for that event", so this
-- table starts empty and changes nothing until an admin flips a checkbox.
-- backend/app/notification_settings.py holds the catalog of valid
-- event_key/channel pairs and defaults; this table just holds the overrides.
--
-- Same convention as add_celebration_log.sql: alter the _template schema
-- (new tenants) plus every already-provisioned tenant's own schema.

CREATE TABLE IF NOT EXISTS _template.core_notification_settings (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    event_key varchar(60) NOT NULL,
    channel varchar(10) NOT NULL CHECK (channel IN ('email','inapp')),
    enabled boolean NOT NULL DEFAULT true,
    updated_by uuid NULL REFERENCES _template.core_users(id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT core_notification_settings_unique UNIQUE (company_id, event_key, channel)
);

CREATE INDEX IF NOT EXISTS ix_core_notification_settings_company
    ON _template.core_notification_settings(company_id);

-- Channel-independent "has this (company, event, entity) already been
-- announced" marker, used by scheduled reminder jobs whose own idempotency
-- used to piggyback on a Notification row existing (app/reminders.py
-- holiday_reminders / contract_end_reminders) -- that broke once the new
-- in-app channel could be turned off independently of email (the marker
-- would never get created, so the same reminder would re-fire on every poll
-- for the rest of the day). This table is the real, channel-independent
-- marker instead; see notification_settings.mark_fired.
CREATE TABLE IF NOT EXISTS _template.core_notification_fired (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES _template.core_companies(id),
    event_key varchar(60) NOT NULL,
    entity_id uuid NOT NULL,
    fired_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT core_notification_fired_unique UNIQUE (company_id, event_key, entity_id)
);

DO $$
DECLARE
    tenant RECORD;
BEGIN
    -- Skip tenant rows whose schema was never provisioned (or was dropped).
    FOR tenant IN SELECT t.slug FROM public.tenants t
                  WHERE EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname = t.slug) LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.core_notification_settings (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %I.core_companies(id),
                event_key varchar(60) NOT NULL,
                channel varchar(10) NOT NULL CHECK (channel IN (''email'',''inapp'')),
                enabled boolean NOT NULL DEFAULT true,
                updated_by uuid NULL REFERENCES %I.core_users(id),
                updated_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT core_notification_settings_unique UNIQUE (company_id, event_key, channel)
            )',
            tenant.slug, tenant.slug, tenant.slug
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS ix_core_notification_settings_company ON %I.core_notification_settings(company_id)',
            tenant.slug
        );
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.core_notification_fired (
                id uuid PRIMARY KEY,
                company_id uuid NOT NULL REFERENCES %I.core_companies(id),
                event_key varchar(60) NOT NULL,
                entity_id uuid NOT NULL,
                fired_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT core_notification_fired_unique UNIQUE (company_id, event_key, entity_id)
            )',
            tenant.slug, tenant.slug
        );
        RAISE NOTICE 'core_notification_settings created for tenant %', tenant.slug;
    END LOOP;
END $$;
