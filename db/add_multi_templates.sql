-- Multi-template library (Documents > Templates): Payslip / Offer Letter /
-- Experience & Relieving Letter templates move from exactly one row per
-- company(+letter_type) to many named rows, with exactly one marked
-- is_active at a time -- enforced by a partial unique index, not just
-- application logic. Plus a brand-new hcm_email_templates table giving the
-- same multi-template/one-active treatment to every transactional email
-- (approvals, leave decisions, interview notices, candidate messages, ...),
-- which previously had zero DB-backed customization at all.
--
-- Fully additive and backward compatible: every existing single row already
-- has is_active = true and is the only row for its key, so it trivially
-- satisfies the new partial unique index the instant it exists -- it just
-- becomes that company's first saved template, already active. Nothing
-- about currently-generated payslips/offer letters/exit letters/emails
-- changes until a company explicitly saves a second template and switches
-- which one is active. Idempotent; every tenant schema + _template.
--
-- NOTE: the email_kind CHECK constraint below lists only the 8 kinds known
-- when this file was first written. If you're applying this to a fresh
-- database, also run add_email_kinds_leave_withdrawal_promotion.sql right
-- after it so hcm_email_templates accepts the full current set of kinds
-- from day one.

CREATE OR REPLACE FUNCTION pg_temp.mt_apply(s text) RETURNS void AS $f$
DECLARE
    con record;
BEGIN
    -- hcm_payslip_html_templates / hcm_offer_letter_html_templates: the
    -- UNIQUE(company_id) constraint was declared inline (unique=True) with
    -- no fixed name, so find and drop whatever Postgres auto-named it.
    FOR con IN
        SELECT c.conname, t.relname AS tbl
        FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = s AND c.contype = 'u'
          AND t.relname IN ('hcm_payslip_html_templates', 'hcm_offer_letter_html_templates')
    LOOP
        EXECUTE format('ALTER TABLE %1$I.%2$I DROP CONSTRAINT %3$I', s, con.tbl, con.conname);
    END LOOP;
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_payslip_tpl_active ON %1$I.hcm_payslip_html_templates (company_id) WHERE is_active', s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_offer_tpl_active ON %1$I.hcm_offer_letter_html_templates (company_id) WHERE is_active', s);

    -- hcm_exit_letter_html_templates: named constraint, drop directly.
    EXECUTE format('ALTER TABLE %1$I.hcm_exit_letter_html_templates DROP CONSTRAINT IF EXISTS uq_exit_letter_template_type', s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_exit_letter_tpl_active ON %1$I.hcm_exit_letter_html_templates (company_id, letter_type) WHERE is_active', s);

    -- New: email templates, same shape, keyed by email_kind instead of
    -- letter_type. No seed/default rows -- "no row" means "use the built-in
    -- app/email_templates/<kind>.html file", forever, until a company saves
    -- its own first template for that kind (see email_service.render_email).
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_email_templates (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
            email_kind varchar(40) NOT NULL CHECK (email_kind IN (
                'request_notification', 'celebration', 'leave_applied', 'leave_approved',
                'leave_rejected', 'hr_document', 'test_email', 'candidate_message'
            )),
            name varchar(120) NOT NULL,
            html_body text NOT NULL,
            css_styles text NOT NULL DEFAULT '',
            is_active boolean NOT NULL DEFAULT true,
            version integer NOT NULL DEFAULT 1,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_by uuid,
            updated_at timestamptz
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_email_tpl_company_kind ON %1$I.hcm_email_templates (company_id, email_kind)', s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_email_tpl_active ON %1$I.hcm_email_templates (company_id, email_kind) WHERE is_active', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.mt_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_payslip_html_templates'
        )
    LOOP
        PERFORM pg_temp.mt_apply(tenant.slug);
    END LOOP;
END $$;
