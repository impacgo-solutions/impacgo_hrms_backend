-- Adds token revocation and login-lockout support to public.users:
--   token_version          -- embedded in every JWT issued at login; bumped
--                             by POST /api/auth/logout, so a token issued
--                             before that bump gets rejected on its next
--                             use instead of riding out its full 8h expiry.
--                             Today there is no revocation mechanism at all
--                             -- logout is 100% client-side, no server call.
--   failed_login_attempts  -- incremented on each wrong password, reset to
--                             0 on success.
--   locked_until           -- set once failed_login_attempts hits the
--                             configured threshold (backend/app/config.py's
--                             login_max_failed_attempts); login is refused
--                             with 429 until this passes, even with the
--                             correct password.
-- Today there is no rate limiting/lockout on login at all -- unlimited
-- password guesses against any known email.
--
-- NOT executed by the assistant -- review and run by hand, same convention
-- as every other file in backend/db/.
--
-- public.users is a global table (not per-tenant-schema, unlike most of
-- this session's other migrations), so this is a single plain ALTER --
-- no per-tenant loop needed.
--
-- Safe to re-run: every ALTER uses ADD COLUMN IF NOT EXISTS.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS token_version integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS failed_login_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS locked_until timestamptz;

-- ----------------------------------------------------------------------------
-- Verification queries to run afterward (read-only)
-- ----------------------------------------------------------------------------
-- SELECT column_name, column_default, is_nullable
--   FROM information_schema.columns
--   WHERE table_schema = 'public' AND table_name = 'users'
--     AND column_name IN ('token_version', 'failed_login_attempts', 'locked_until');
--   -- should return all 3 columns.
