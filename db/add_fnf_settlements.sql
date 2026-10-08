-- Full & Final Settlement: HR-entered line items, Prepare -> Approve -> Paid
-- workflow, and the F&F Settlement Statement document (letter_type
-- 'fnf_statement' on the Experience & Relieving letter tables).
--
--   hcm_final_settlements        + workflow / payment / audit columns
--   hcm_final_settlement_lines   NEW: one row per earning / deduction
--   hcm_exit_letter_html_templates / hcm_exit_letters: letter_type check
--                                  now allows 'fnf_statement'
--
-- Existing settlement rows keep their totals and status ('posted' is read as
-- approved). The unique index on exit_id is only created where no exit has
-- two settlement rows (a notice is raised otherwise, nothing is deleted).
-- Run after add_exit_letters.sql and add_exit_letter_seal_signature.sql.
-- Idempotent; every tenant schema + _template.

CREATE OR REPLACE FUNCTION pg_temp.fnf_apply(s text) RETURNS void AS $f$
DECLARE
    has_duplicates boolean;
BEGIN
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_final_settlements
            ADD COLUMN IF NOT EXISTS notes text,
            ADD COLUMN IF NOT EXISTS prepared_by uuid,
            ADD COLUMN IF NOT EXISTS prepared_by_name varchar(150),
            ADD COLUMN IF NOT EXISTS prepared_at timestamptz,
            ADD COLUMN IF NOT EXISTS approved_by uuid,
            ADD COLUMN IF NOT EXISTS approved_by_name varchar(150),
            ADD COLUMN IF NOT EXISTS approved_at timestamptz,
            ADD COLUMN IF NOT EXISTS approval_notes text,
            ADD COLUMN IF NOT EXISTS payment_date date,
            ADD COLUMN IF NOT EXISTS payment_mode varchar(30),
            ADD COLUMN IF NOT EXISTS payment_reference varchar(80),
            ADD COLUMN IF NOT EXISTS paid_by uuid,
            ADD COLUMN IF NOT EXISTS paid_at timestamptz,
            ADD COLUMN IF NOT EXISTS created_at timestamptz,
            ADD COLUMN IF NOT EXISTS updated_at timestamptz
    $q$, s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_final_settlement_lines (
            id uuid PRIMARY KEY,
            settlement_id uuid NOT NULL REFERENCES %1$I.hcm_final_settlements(id) ON DELETE CASCADE,
            line_type varchar(10) NOT NULL CHECK (line_type IN ('earning', 'deduction')),
            component varchar(60) NOT NULL,
            description varchar(200),
            amount numeric(14,2) NOT NULL CHECK (amount >= 0),
            sort_order integer NOT NULL DEFAULT 0
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_fnf_lines_settlement ON %1$I.hcm_final_settlement_lines (settlement_id)', s);

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE schemaname = s AND indexname = 'uq_final_settlement_exit'
    ) THEN
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %1$I.hcm_final_settlements GROUP BY exit_id HAVING count(*) > 1)', s)
            INTO has_duplicates;
        IF has_duplicates THEN
            RAISE NOTICE '%: an exit has more than one settlement row -- unique index skipped', s;
        ELSE
            EXECUTE format('CREATE UNIQUE INDEX uq_final_settlement_exit ON %1$I.hcm_final_settlements (exit_id)', s);
        END IF;
    END IF;

    EXECUTE format('ALTER TABLE %1$I.hcm_exit_letter_html_templates DROP CONSTRAINT IF EXISTS ck_exit_letter_template_type', s);
    EXECUTE format($q$ALTER TABLE %1$I.hcm_exit_letter_html_templates ADD CONSTRAINT ck_exit_letter_template_type
                     CHECK (letter_type IN ('experience_relieving', 'fnf_statement'))$q$, s);
    EXECUTE format('ALTER TABLE %1$I.hcm_exit_letters DROP CONSTRAINT IF EXISTS ck_exit_letter_type', s);
    EXECUTE format($q$ALTER TABLE %1$I.hcm_exit_letters ADD CONSTRAINT ck_exit_letter_type
                     CHECK (letter_type IN ('experience_relieving', 'fnf_statement'))$q$, s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.fnf_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_exit_letters'
        )
    LOOP
        PERFORM pg_temp.fnf_apply(tenant.slug);
    END LOOP;
END $$;
