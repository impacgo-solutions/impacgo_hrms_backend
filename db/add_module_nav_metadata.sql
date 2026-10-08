-- Adds the two pieces of metadata the sidebar needs to become
-- backend-driven instead of a hardcoded Dart constant (kNav in
-- lib/data/seed/nav_seed.dart) with a hardcoded RBAC-gating switch
-- statement (isNavVisible in lib/rbac/rbac_engine.dart):
--   nav_group          -- which sidebar section this module renders under
--                          (NULL = ungrouped, e.g. Dashboard)
--   permission_columns -- which RBAC permission column(s) gate visibility
--                          (NULL/empty = always visible once the module
--                          itself is enabled; more than one column means
--                          "visible if ANY of these is above 'none'", used
--                          today by Payroll, Admin, and Approvals)
--
-- NOT executed by the assistant -- review and run by hand, same convention
-- as every other file in backend/db/.
--
-- public.modules is a global catalog (not per-tenant-schema), so this is a
-- single ALTER + a set of UPDATEs -- no per-tenant loop needed.
--
-- Every value below is read directly from the CURRENTLY-RUNNING app's own
-- lib/data/seed/nav_seed.dart (kNav's 6 groups) and
-- lib/rbac/rbac_engine.dart (isNavVisible's switch statement) -- this is a
-- relocation of already-live behavior into the DB, not new/invented data.
-- The one deliberate exception NOT modeled here: isNavVisible's hardcoded
-- "IT / System Admin always sees Approvals regardless of column grants"
-- override stays as a small explicit exception in backend code, not data --
-- see PRODUCTION_READINESS_AUDIT.md Phase 2 notes.
--
-- The 19th row already in public.modules, 'retail', belongs to a different
-- product on this shared database and is intentionally left untouched
-- (NULL/NULL) -- the backend endpoint this migration supports filters it
-- out by an explicit allowlist of this app's 18 keys, so its metadata here
-- is moot either way.
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS, and every UPDATE is idempotent
-- (sets the same value every time).

ALTER TABLE public.modules
    ADD COLUMN IF NOT EXISTS nav_group varchar(40),
    ADD COLUMN IF NOT EXISTS permission_columns text[];

-- Dashboard group (no group header in the sidebar today)
UPDATE public.modules SET nav_group = NULL, permission_columns = NULL
    WHERE key = 'dashboard';

-- "Organization" group
UPDATE public.modules SET nav_group = 'Organization',
    permission_columns = ARRAY['Org Structure Config']
    WHERE key = 'organization';

-- "Workforce" group
UPDATE public.modules SET nav_group = 'Workforce', permission_columns = NULL
    WHERE key = 'people';
UPDATE public.modules SET nav_group = 'Workforce',
    permission_columns = ARRAY['Team Attendance']
    WHERE key = 'attendance';
UPDATE public.modules SET nav_group = 'Workforce', permission_columns = NULL
    WHERE key = 'work';
UPDATE public.modules SET nav_group = 'Workforce',
    permission_columns = ARRAY['Projects']
    WHERE key = 'projects';
UPDATE public.modules SET nav_group = 'Workforce', permission_columns = NULL
    WHERE key = 'leave';

-- "Money & Growth" group
UPDATE public.modules SET nav_group = 'Money & Growth',
    permission_columns = ARRAY['Payroll (View Own)', 'Payroll (Process)']
    WHERE key = 'payroll';
UPDATE public.modules SET nav_group = 'Money & Growth', permission_columns = NULL
    WHERE key = 'travel';
UPDATE public.modules SET nav_group = 'Money & Growth', permission_columns = NULL
    WHERE key = 'benefits';
UPDATE public.modules SET nav_group = 'Money & Growth',
    permission_columns = ARRAY['Performance Reviews']
    WHERE key = 'performance';
UPDATE public.modules SET nav_group = 'Money & Growth',
    permission_columns = ARRAY['Recruitment']
    WHERE key = 'recruitment';
UPDATE public.modules SET nav_group = 'Money & Growth', permission_columns = NULL
    WHERE key = 'learning';
UPDATE public.modules SET nav_group = 'Money & Growth',
    permission_columns = ARRAY['Asset Management']
    WHERE key = 'assets';

-- "Insights" group
UPDATE public.modules SET nav_group = 'Insights', permission_columns = NULL
    WHERE key = 'documents';
UPDATE public.modules SET nav_group = 'Insights',
    permission_columns = ARRAY['Reports & Analytics']
    WHERE key = 'reports';
UPDATE public.modules SET nav_group = 'Insights',
    permission_columns = ARRAY[
        'Team Attendance', 'Leave Approval', 'Timesheet Approval',
        'Recruitment', 'Benefits Admin', 'Performance Reviews',
        'Travel & Expense Approval', 'Payroll (Process)'
    ]
    WHERE key = 'approvals';

-- "System" group
UPDATE public.modules SET nav_group = 'System',
    permission_columns = ARRAY['System Settings / RBAC', 'Audit Logs']
    WHERE key = 'admin';

-- ----------------------------------------------------------------------------
-- Verification queries to run afterward (read-only)
-- ----------------------------------------------------------------------------
-- SELECT key, name, nav_group, permission_columns FROM public.modules
--   ORDER BY sort_order;
--   -- every one of the 18 HRMS keys should have a non-null nav_group
--   -- except 'dashboard'; 'retail' should be untouched (both NULL).
