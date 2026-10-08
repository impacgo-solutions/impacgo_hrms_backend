-- Per-tenant outgoing email configuration (app/tenant_email/).
-- GLOBAL (public) table: one row per tenant, NOT copied into tenant schemas.
-- Every *_encrypted column holds Fernet ciphertext produced by the app
-- (TENANT_EMAIL_ENCRYPTION_KEY); the database never sees plaintext secrets.
-- Idempotent and additive only: safe on dev / test / staging / production.
-- Existing global .env email settings are untouched (see
-- EMAIL_GLOBAL_FALLBACK_TENANTS and docs/TENANT_EMAIL_SETTINGS.md for the
-- transition plan). Rollback: DROP TABLE public.tenant_email_settings;

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid() on PostgreSQL < 13

CREATE TABLE IF NOT EXISTS public.tenant_email_settings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    provider varchar(50) NOT NULL DEFAULT 'smtp',
    -- smtp
    smtp_host varchar(255),
    smtp_port integer DEFAULT 587,
    smtp_username varchar(255),
    smtp_password_encrypted text,
    smtp_use_tls boolean NOT NULL DEFAULT true,
    smtp_use_ssl boolean NOT NULL DEFAULT false,
    -- microsoft_graph
    microsoft_tenant_id varchar(100),
    microsoft_client_id varchar(100),
    microsoft_client_secret_encrypted text,
    -- sendgrid
    sendgrid_api_key_encrypted text,
    -- ses (SESv2 API; for SES SMTP credentials use provider = 'smtp')
    ses_region varchar(30),
    ses_access_key_id varchar(128),
    ses_secret_access_key_encrypted text,
    -- common
    from_email varchar(255),
    from_name varchar(255),
    reply_to varchar(255),
    is_enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid,
    CONSTRAINT uq_tenant_email_settings_tenant UNIQUE (tenant_id),
    CONSTRAINT ck_tenant_email_settings_provider
        CHECK (provider IN ('smtp', 'microsoft_graph', 'sendgrid', 'ses')),
    CONSTRAINT ck_tenant_email_settings_port
        CHECK (smtp_port IS NULL OR smtp_port BETWEEN 1 AND 65535)
);
