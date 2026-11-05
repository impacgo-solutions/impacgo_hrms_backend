-- HRMS tenants only: accounts whose stored bcrypt hash was made from a
-- password longer than 72 bytes (bcrypt silently used only the first 72 --
-- fixed in backend/app/security.py by SHA-256 pre-hashing). Such a hash can
-- never verify again, by design: the account needs an admin password reset.
--
-- A bcrypt hash doesn't record the original password's length, so these
-- accounts can't be found by scanning. Login flags one the first time its
-- owner tries the long password (it matches the old truncated hash, and is
-- still refused). Admins see flagged accounts at
-- GET /api/users/password-reset-required; any password reset clears the flag.
--
-- Scope: adds the columns to "<tenant>".core_users ONLY for tenants with the
-- HRMS module ('hcm') enabled in public.tenant_modules. It does not touch
-- public.users (shared with other applications) or any non-HRMS tenant.
-- A tenant provisioned later needs this re-run (idempotent).
--
-- Restart the API after running it (the column is detected once per
-- tenant per process, see backend/app/auth_state.py).

DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT DISTINCT tm.tenant_slug
        FROM public.tenant_modules tm
        JOIN information_schema.tables it
          ON it.table_schema = tm.tenant_slug AND it.table_name = 'core_users'
        WHERE tm.module_code = 'hcm' AND tm.is_enabled
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.core_users '
            'ADD COLUMN IF NOT EXISTS password_reset_required boolean NOT NULL DEFAULT false, '
            'ADD COLUMN IF NOT EXISTS password_reset_required_at timestamptz', t);
        RAISE NOTICE 'password_reset_required: %', t;
    END LOOP;
END $$;
