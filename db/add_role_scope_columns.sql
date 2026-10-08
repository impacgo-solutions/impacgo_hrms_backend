-- Adds dashboard_scope, people_scope, and blurb columns to core_roles, and
-- backfills every role name currently live in this database.
--
-- Why: routers/roles.py's _DASHBOARD_SCOPE / _PEOPLE_SCOPE / _ROLE_BLURBS
-- dicts are keyed by exact role name string. Checked directly against the
-- live database: every one of the 4 provisioned tenants (Infyq, acme,
-- impacgo, etorgroups) has 14 roles, but only 5 of those 14 names
-- (Branch Head, Branch Manager, Organization Owner / CEO, Project Manager,
-- Team Lead) match a dict key -- the other 9 real role names (CHO, Vice
-- President, Managing director, General Manager, IT Manager, HR Manager,
-- Recruitment Staff, Recruitment, HR EXECUTIVE) fall through to the
-- generic default today, on every login, for every user holding one of
-- those 9 roles. This is a live bug, not a future risk.
--
-- NOT executed by the assistant -- review and run by hand, same convention
-- as every other file in backend/db/.
--
-- core_roles lives per-tenant-schema, so this follows the same
-- DO $$ ... FOREACH s IN ARRAY schemas LOOP EXECUTE format(...) pattern as
-- backend/db/add_administration_audit_columns.sql -- _template plus every
-- schema already in public.tenants.
--
-- Values for the 5 directly-matching role names are copied verbatim from
-- routers/roles.py's three dicts (unchanged behavior, just relocated).
-- Values for the 9 non-matching real role names are copied from whichever
-- built-in role they were confirmed (with the user) to correspond to:
--   CHO                -> C-Level Executive
--   Vice President     -> VP / Director
--   Managing director  -> Organization Owner / CEO
--   General Manager    -> General Manager / Sr. Manager
--   IT Manager         -> IT / System Admin
--   HR Manager, Recruitment Staff, Recruitment, HR EXECUTIVE
--                       -> HR / Recruitment Staff
-- Any role name neither live today nor in this list (e.g. one created
-- after this migration runs) stays NULL and falls back to
-- routers/roles.py's hardcoded dicts, matching today's exact behavior --
-- see the GET /api/roles/scopes change in the same phase.
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS, and every UPDATE is idempotent.

DO $$
DECLARE
    schemas text[];
    s text;
BEGIN
    SELECT array_agg(slug) INTO schemas FROM (
        SELECT '_template'::text AS slug
        UNION ALL
        SELECT slug FROM public.tenants
    ) x;

    FOREACH s IN ARRAY schemas LOOP
        EXECUTE format('ALTER TABLE %I.core_roles
            ADD COLUMN IF NOT EXISTS dashboard_scope varchar(20),
            ADD COLUMN IF NOT EXISTS people_scope varchar(20),
            ADD COLUMN IF NOT EXISTS blurb text', s);

        -- Organization Owner / CEO
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'org', people_scope = 'org-full',
            blurb = 'Full visibility and control across every module, branch and approval chain.'
            WHERE name IN ('Organization Owner / CEO', 'Managing director')$f$, s);

        -- C-Level Executive
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'org', people_scope = 'org-view',
            blurb = 'Company-wide visibility with edit rights over org structure, benefits and platform settings.'
            WHERE name IN ('C-Level Executive', 'CHO')$f$, s);

        -- VP / Director
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'org', people_scope = 'org-view',
            blurb = 'Company-wide visibility across your function, with edit rights on performance and org structure.'
            WHERE name IN ('VP / Director', 'Vice President')$f$, s);

        -- General Manager / Sr. Manager
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'team', people_scope = 'team',
            blurb = 'Your reporting line only -- attendance, leave and timesheet approvals for your team.'
            WHERE name IN ('General Manager / Sr. Manager', 'General Manager')$f$, s);

        -- Manager
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'team', people_scope = 'team',
            blurb = 'Your direct reports only -- day-to-day approvals, no org-wide administration.'
            WHERE name = 'Manager'$f$, s);

        -- Team Lead
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'team', people_scope = 'team',
            blurb = 'Your squad only -- first-line attendance & timesheet review, view-only on leave and org data.'
            WHERE name = 'Team Lead'$f$, s);

        -- HR / Recruitment Staff (+ the 4 real-but-unmatched HR/recruitment
        -- role names mapped to it)
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'org', people_scope = 'branch',
            blurb = 'Full People, Recruitment and Benefits administration; no org-structure or platform settings access.'
            WHERE name IN ('HR / Recruitment Staff', 'HR Manager', 'Recruitment Staff', 'Recruitment', 'HR EXECUTIVE')$f$, s);

        -- Finance / Payroll Staff
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'org', people_scope = 'org-view',
            blurb = 'Full payroll processing and benefits administration; no attendance, performance or recruitment access.'
            WHERE name = 'Finance / Payroll Staff'$f$, s);

        -- IT / System Admin
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'org', people_scope = 'org-view',
            blurb = 'Full platform administration and asset management; no HR, payroll or performance access.'
            WHERE name IN ('IT / System Admin', 'IT Manager')$f$, s);

        -- Branch Manager
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'branch', people_scope = 'branch',
            blurb = 'Branch-scoped view of people, attendance and performance; no payroll, recruitment or platform access.'
            WHERE name = 'Branch Manager'$f$, s);

        -- Branch Head
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'org', people_scope = 'org-view',
            blurb = 'Oversees every Branch Manager company-wide -- cross-branch visibility and approvals, no platform administration.'
            WHERE name = 'Branch Head'$f$, s);

        -- Project Manager
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'project', people_scope = 'project',
            blurb = 'Approve leave, timesheets and travel for your project team; view-only on broader org and payroll data.'
            WHERE name = 'Project Manager'$f$, s);

        -- Professional / IC Employee / Associate / Intern
        EXECUTE format($f$UPDATE %I.core_roles SET
            dashboard_scope = 'self', people_scope = 'self',
            blurb = 'Self-service only -- your profile, attendance, leave, payslip and assigned work.'
            WHERE name IN ('Professional / IC Employee', 'Associate / Intern')$f$, s);

        RAISE NOTICE 'Role scope columns ensured/backfilled for schema %', s;
    END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- Verification queries to run afterward (read-only)
-- ----------------------------------------------------------------------------
-- SELECT name, dashboard_scope, people_scope, blurb FROM "Infyq".core_roles
--   ORDER BY name;
--   -- every one of the 14 role names should now have all 3 columns filled.
--
-- SELECT name FROM "Infyq".core_roles
--   WHERE dashboard_scope IS NULL OR people_scope IS NULL OR blurb IS NULL;
--   -- should return zero rows for any tenant whose role names are all
--   -- accounted for above; a non-empty result means a role name exists
--   -- that wasn't in this backfill list (e.g. a genuinely new custom role
--   -- created since) -- it will still work correctly via the
--   -- routers/roles.py hardcoded-dict fallback, just not from this table.
