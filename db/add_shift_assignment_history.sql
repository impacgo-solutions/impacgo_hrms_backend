-- Shift Management: effective-dated assignments with a complete history.
--
--   hcm_shift_assignments
--     + previous_shift_id uuid         -- the employee's shift right before this one took effect
--     + created_at        timestamptz  -- when the assignment was made
--     + created_by        uuid         -- core_users.id of who made it
--     + updated_at        timestamptz  -- last change (ended / superseded / cancelled)
--     + updated_by        uuid
--
-- from_date (the Effective Date) and to_date already exist. Existing rows
-- get NULLs for the new columns -- no backfill, nothing else changes, and
-- hcm_attendance_records is not touched.
--
-- Idempotent; every tenant schema + _template. Additive only.

CREATE OR REPLACE FUNCTION pg_temp.sah_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format('ALTER TABLE %1$I.hcm_shift_assignments ADD COLUMN IF NOT EXISTS previous_shift_id uuid', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_shift_assignments ADD COLUMN IF NOT EXISTS created_at timestamptz', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_shift_assignments ADD COLUMN IF NOT EXISTS created_by uuid', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_shift_assignments ADD COLUMN IF NOT EXISTS updated_at timestamptz', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_shift_assignments ADD COLUMN IF NOT EXISTS updated_by uuid', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_shift_assign_shift_from ON %1$I.hcm_shift_assignments (shift_id, from_date)', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.sah_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_shift_assignments'
        )
    LOOP
        PERFORM pg_temp.sah_apply(tenant.slug);
    END LOOP;
END $$;
