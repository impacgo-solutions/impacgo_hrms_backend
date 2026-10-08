-- ============================================================================
-- Provision a new tenant + its Organization Owner -- REVIEW AND RUN BY HAND
-- ============================================================================
-- Nothing in this file has been executed against your database. Read each
-- step's comment, replace every {{PLACEHOLDER}}, and run the statements
-- yourself, in order, in ONE session (step 3's `SET search_path` must stay
-- in effect through step 10 -- see that step's note if your SQL client
-- opens a new connection per statement).
--
-- Placeholders to fill in before running anything:
--   {{TENANT_SLUG}}            lowercase, must be a valid Postgres identifier
--                               (letters/digits/underscore, starts with a
--                               letter) -- it becomes the actual schema name.
--   {{TENANT_DISPLAY_NAME}}    human-readable tenant name
--   {{COMPANY_NAME}}           the company's display name
--   {{COMPANY_LEGAL_NAME}}     registered legal name (can equal COMPANY_NAME)
--   {{OWNER_FIRST_NAME}} / {{OWNER_LAST_NAME}}
--   {{OWNER_WORK_EMAIL}}       becomes the login email -- must not already
--                               exist in public.users / public.admin_users
--   {{OWNER_PASSWORD_HASH}}    output of `venv/Scripts/python -m app.password_admin hash`
--                              (prompts without echo, checks the password policy, hashes
--                              exactly like the app -- never put the plaintext in this file)
--
--   {{COMPANY_ID}}, {{ROLE_ID}}, {{EMPLOYEE_ID}}, {{USER_ID}}
--       Four UUIDs YOU generate before starting (e.g. run
--       `SELECT gen_random_uuid();` four times, or use any UUID generator),
--       then substitute consistently everywhere that placeholder appears.
--       They are not generated inline with gen_random_uuid() because several
--       rows below must share the SAME id, and a value typed by
--       gen_random_uuid() in one manually-run statement can't be carried
--       into the next one without a session variable your SQL client may or
--       may not support -- fixed literals sidestep that entirely.
--
-- Assumption this script relies on: public.currencies already has a row
-- with code = 'INR' (true for every company this codebase has ever seeded).
-- If you're not sure, run `SELECT id, code FROM public.currencies;`
-- (read-only) yourself first to confirm, or swap the currency code in step 4.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- STEP 1 -- Create the tenant's schema and clone every table it needs
-- ----------------------------------------------------------------------------
-- public.provision_tenant(tenant_slug, enabled_modules) already exists in
-- your database (see db/full_db.sql, "Name: provision_tenant; Type:
-- FUNCTION"). It does three things:
--   1. CREATE SCHEMA {{TENANT_SLUG}}
--   2. For every table in the `_template` schema whose name is prefixed
--      with one of the module codes you pass in, runs:
--        CREATE TABLE {{TENANT_SLUG}}.<table> (LIKE _template.<table> INCLUDING ALL)
--      -- this copies column structure/defaults/indexes, but no data and no
--      foreign keys (this schema doesn't use any anywhere -- every table in
--      it only has a PRIMARY KEY constraint, confirmed against the live
--      `acme` schema).
--   3. Inserts one row per module into public.tenant_modules.
--
-- 'core' and 'hcm' are REQUIRED -- without them, core_companies /
-- core_employees / core_users / core_roles / everything this People/HR app
-- reads won't exist in the new schema at all. 'pm' is included too because
-- the Employee Profile "Projects" tab and the Projects screen read
-- pm_projects / pm_resource_allocations (backend/app/models.py Project /
-- ProjectAllocation). fin_ / scm_ / mfg_ / plan_ / retail_ tables belong to
-- other ERP modules this specific People app's routers never query, so
-- they're deliberately left out.
--
-- CAUTION: this function has no "if not exists" guard on the CREATE SCHEMA
-- step -- it can only be called ONCE per slug. If you later need one of the
-- omitted modules, add its tables individually with the same
-- `CREATE TABLE {{TENANT_SLUG}}.<table> (LIKE _template.<table> INCLUDING ALL)`
-- pattern rather than re-running this function.

SELECT public.provision_tenant('{{TENANT_SLUG}}', ARRAY['core', 'hcm', 'pm']);


-- ----------------------------------------------------------------------------
-- STEP 2 -- Register the tenant (platform-level login routing)
-- ----------------------------------------------------------------------------
-- public.tenants is the platform's registry of tenant slugs. public.users
-- (step 9) has a foreign key to this table's `slug` column
-- (users_tenant_fkey) -- without this row, step 9's insert will fail.
-- slug must be UNIQUE and must exactly match what you passed to
-- provision_tenant above (it's also literally the Postgres schema name).

INSERT INTO public.tenants (id, slug, name, country, is_active)
VALUES (
    gen_random_uuid(),
    '{{bhubhatech}}',
    '{{bhubhatech}}',
    'India',
    true
);


-- ----------------------------------------------------------------------------
-- STEP 3 -- Point this session at the new tenant schema
-- ----------------------------------------------------------------------------
-- Every statement from here on uses unqualified table names (core_companies,
-- core_employees, ...) -- this mirrors exactly what the running app does
-- per request (backend/app/deps.py: `SET search_path TO "{tenant_slug}",
-- public`). Run this in the SAME session as steps 4-10. If your SQL client
-- opens a fresh connection per statement (some GUI "run" buttons do),
-- re-issue this line before each remaining step, or prefix every table name
-- below with `{{TENANT_SLUG}}.` instead of relying on search_path.

SET search_path TO {{bhubhatech}}, public;


-- ----------------------------------------------------------------------------
-- STEP 4 -- Create the company row
-- ----------------------------------------------------------------------------
-- core_companies.default_currency_id is NOT NULL with no default -- it must
-- point at a real row in the shared (public-schema) currencies table; see
-- the assumption noted at the top of this file. {{COMPANY_ID}} is one of
-- the 4 UUIDs you generated up front -- every row below that belongs to
-- this company reuses it.
--
-- Optional columns you can add here if you want them set immediately
-- instead of later through Company Settings: gstin, pan, cin, industry,
-- address_line1/2, city, state, pincode, email_domain (drives the domain
-- half of every future hire's auto-generated work email -- see
-- backend/app/crud.py derive_company_email_domain; if left NULL, the app
-- derives one from the company name instead).

INSERT INTO core_companies (id, name, legal_name, default_currency_id, country)
VALUES (
    '{{COMPANY_ID}}',
    '{{COMPANY_NAME}}',
    '{{COMPANY_LEGAL_NAME}}',
    (SELECT id FROM public.currencies WHERE code = 'INR'),
    'India'
);


-- ----------------------------------------------------------------------------
-- STEP 5 -- Create the "Organization Owner / CEO" role
-- ----------------------------------------------------------------------------
-- The name must be EXACTLY "Organization Owner / CEO" (case, spacing and
-- slash included) -- both the backend (backend/app/deps.py,
-- BUILTIN_ROLES[0] via backend/app/rbac_columns.py) and the Flutter
-- frontend's RbacEngine special-case this exact string to bypass every
-- permission check entirely, rather than reading a role_permissions row.
-- That means STEP 6 below is NOT required for this Owner to be able to use
-- the app -- it exists only so the Administration > Roles & Permissions
-- screen shows a populated matrix for this role instead of a blank one,
-- matching what backend/app/seed_new_company.py's ensure_roles() generates
-- for every company it seeds. Skip step 6 if you don't need that screen to
-- look complete right away.

INSERT INTO core_roles (id, company_id, name, description, is_system)
VALUES (
    '{{ROLE_ID}}',
    '{{COMPANY_ID}}',
    'Organization Owner / CEO',
    'Full visibility and control across every module, branch and approval chain.',
    true
);


-- ----------------------------------------------------------------------------
-- STEP 6 (OPTIONAL -- cosmetic only, see step 5) -- Grant the RBAC matrix
-- ----------------------------------------------------------------------------
-- Mirrors backend/app/rbac_columns.py DEFAULT_MATRIX["Organization Owner /
-- CEO"] exactly, one row per RBAC column. Each permission's `code` is
-- "{column}.{level}" (backend/app/crud.py get_or_create_permission) --
-- columns at level 'n' (no access) get no row at all; this role has none.

INSERT INTO core_permissions (id, code, module, resource, action) VALUES
    (gen_random_uuid(), 'own_profile.s',             'profile',      'own_profile',             's'),
    (gen_random_uuid(), 'team_attendance.v',         'attendance',   'team_attendance',         'v'),
    (gen_random_uuid(), 'leave_approval.v',          'leave',        'leave_approval',          'v'),
    (gen_random_uuid(), 'timesheet_approval.v',      'work',         'timesheet_approval',      'v'),
    (gen_random_uuid(), 'payroll_view_own.s',        'payroll',      'payroll_view_own',        's'),
    (gen_random_uuid(), 'payroll_process.v',         'payroll',      'payroll_process',         'v'),
    (gen_random_uuid(), 'benefits_admin.a',          'benefits',     'benefits_admin',          'a'),
    (gen_random_uuid(), 'recruitment.v',             'recruitment',  'recruitment',             'v'),
    (gen_random_uuid(), 'performance_reviews.v',     'performance',  'performance_reviews',     'v'),
    (gen_random_uuid(), 'org_structure_config.a',    'organization', 'org_structure_config',    'a'),
    (gen_random_uuid(), 'reports_analytics.a',       'reports',      'reports_analytics',       'a'),
    (gen_random_uuid(), 'system_settings_rbac.a',    'admin',        'system_settings_rbac',    'a'),
    (gen_random_uuid(), 'audit_logs.a',              'admin',        'audit_logs',              'a'),
    (gen_random_uuid(), 'travel_expense_approval.a', 'travel',       'travel_expense_approval', 'a'),
    (gen_random_uuid(), 'projects_access.a',         'projects',     'projects_access',         'a'),
    (gen_random_uuid(), 'asset_management.a',        'assets',       'asset_management',        'a');

INSERT INTO core_role_permissions (role_id, permission_id)
SELECT '{{ROLE_ID}}', id FROM core_permissions
WHERE code IN (
    'own_profile.s', 'team_attendance.v', 'leave_approval.v', 'timesheet_approval.v',
    'payroll_view_own.s', 'payroll_process.v', 'benefits_admin.a', 'recruitment.v',
    'performance_reviews.v', 'org_structure_config.a', 'reports_analytics.a',
    'system_settings_rbac.a', 'audit_logs.a', 'travel_expense_approval.a',
    'projects_access.a', 'asset_management.a'
);


-- ----------------------------------------------------------------------------
-- STEP 7 -- Create the Owner's employee record
-- ----------------------------------------------------------------------------
-- branch_id / department_id / designation_id are all nullable and left NULL
-- here -- a brand-new company has none of those set up yet; the Owner
-- creates them after first login via Organization > Branches/Departments.
-- employee_code is free text with no uniqueness constraint at the DB level
-- (uniqueness is enforced by the app, not Postgres) -- 'EMP-0001' is just a
-- clean starting point. The app's own generator (backend/app/crud.py
-- generate_employee_code) auto-creates a number-series row the first time
-- anyone hires through the UI afterwards, and its retry logic already skips
-- forward past any code that's already taken -- so the very next hire made
-- through the app will correctly become EMP-0002 with no further setup here.
--
-- band = 1 assigns the Owner to this company's "Ownership / Founding" Band
-- (core_bands.band_number 1 -- see backend/app/designation_seed_data.py's
-- DESIGNATION_BANDS, position 0). core_bands itself doesn't need seeding
-- here: crud.get_or_seed_company_bands lazily creates all 10 default rows
-- the first time anything reads GET /api/bands (or the Owner's own Profile
-- > Professional tab, via crud._resolve_band_display) for this company, so
-- band=1 already resolves correctly the moment the Owner first logs in.

INSERT INTO core_employees (
    id, company_id, employee_code, first_name, last_name,
    work_email, date_of_joining, employment_type, status, band
)
VALUES (
    '{{EMPLOYEE_ID}}',
    '{{COMPANY_ID}}',
    'EMP-0001',
    '{{OWNER_FIRST_NAME}}',
    '{{OWNER_LAST_NAME}}',
    '{{OWNER_WORK_EMAIL}}',
    CURRENT_DATE,
    'Full-time',
    'Active',
    1
);


-- ----------------------------------------------------------------------------
-- STEP 8 -- Create the login account (core_users + core_user_roles)
-- ----------------------------------------------------------------------------
-- password_hash: paste the output of `python -m app.password_admin hash`.
-- Don't use pgcrypto crypt()/gen_salt('bf') here: it truncates passwords
-- at 72 bytes, defaults to bcrypt cost 6 (the app uses 12) and skips the
-- SHA-256 pre-hashing backend/app/security.py applies to long passwords.
--
-- core_users.status must be exactly the lowercase string 'active' -- unlike
-- core_employees.status above, this one IS read literally by
-- backend/app/deps.py's get_current_user (`if user.status != "active"`), so
-- any other casing would silently lock this account out.

INSERT INTO core_users (id, company_id, email, password_hash, employee_id, status, full_name)
VALUES (
    '{{USER_ID}}',
    '{{COMPANY_ID}}',
    '{{OWNER_WORK_EMAIL}}',
    '{{OWNER_PASSWORD_HASH}}',
    '{{EMPLOYEE_ID}}',
    'active',
    '{{OWNER_FIRST_NAME}} {{OWNER_LAST_NAME}}'
);

INSERT INTO core_user_roles (user_id, role_id)
VALUES ('{{USER_ID}}', '{{ROLE_ID}}');


-- ----------------------------------------------------------------------------
-- STEP 9 -- Register the login in the platform-level tables
-- ----------------------------------------------------------------------------
-- public.users is what backend/app/routers/auth.py's login() actually
-- queries FIRST (by email, via crud.find_public_user_by_email) to resolve
-- which tenant/company/employee a login belongs to, before it ever switches
-- search_path to read core_users -- without this row, the Owner cannot log
-- in even though core_users/core_employees above are already correct.
--
-- {{USER_ID}} is reused as-is here for both admin_users.id and
-- public.users.id -- this project always keeps those two ids identical to
-- core_users.id (see backend/app/crud.py create_admin_user_login and
-- backend/app/models.py AdminUser's own docstring: "Same UUID as the
-- corresponding core.users row for FK compatibility").
--
-- public.admin_users is written for consistency with how every employee
-- account this app creates gets written (create_admin_user_login always
-- writes both tables) but is NOT read by the current /api/auth/login
-- endpoint -- safe to skip if you want the bare minimum for login to work,
-- but keeping it avoids the two tables silently drifting out of sync if
-- some other code path ever starts relying on it.

INSERT INTO public.admin_users (id, email, password_hash, full_name, role)
VALUES (
    '{{USER_ID}}',
    '{{OWNER_WORK_EMAIL}}',
    '{{OWNER_PASSWORD_HASH}}',
    '{{OWNER_FIRST_NAME}} {{OWNER_LAST_NAME}}',
    'Organization Owner / CEO'
);

INSERT INTO public.users (
    id, email, password_hash, tenant_slug, company_id, role, full_name, employee_id
)
VALUES (
    '{{USER_ID}}',
    '{{OWNER_WORK_EMAIL}}',
    '{{OWNER_PASSWORD_HASH}}',
    '{{TENANT_SLUG}}',
    '{{COMPANY_ID}}',
    'Organization Owner / CEO',
    '{{OWNER_FIRST_NAME}} {{OWNER_LAST_NAME}}',
    '{{EMPLOYEE_ID}}'
);


-- ----------------------------------------------------------------------------
-- STEP 10 (RECOMMENDED, NOT REQUIRED) -- Company settings defaults
-- ----------------------------------------------------------------------------
-- Every other column on this table has a default, and the app already
-- handles a missing row gracefully (backend/app/crud.py
-- get_company_settings returns None; callers fall back to defaults) -- but
-- backend/app/seed_new_company.py always creates one for every company it
-- seeds, so this keeps the new company consistent with that convention.

INSERT INTO core_company_settings (company_id, default_currency, default_timezone, working_days_per_week)
VALUES ('{{COMPANY_ID}}', 'INR', 'IST (UTC+5:30)', 5);


-- ============================================================================
-- STEP 11 -- Verify (read-only) before trying to log in through the app
-- ============================================================================
-- Run with search_path still set from step 3.

SELECT * FROM core_companies WHERE id = '{{COMPANY_ID}}';
SELECT * FROM core_roles WHERE id = '{{ROLE_ID}}';
SELECT * FROM core_employees WHERE id = '{{EMPLOYEE_ID}}';
SELECT * FROM core_users WHERE id = '{{USER_ID}}';
SELECT * FROM core_user_roles WHERE user_id = '{{USER_ID}}';
SELECT * FROM public.tenants WHERE slug = '{{TENANT_SLUG}}';
SELECT * FROM public.users WHERE id = '{{USER_ID}}';

-- Then functionally confirm by actually logging in:
--   POST /api/auth/login  { "email": "{{OWNER_WORK_EMAIL}}", "password": "<the owner password you hashed>" }
-- A successful response returns a JWT whose payload carries tenant_slug =
-- '{{TENANT_SLUG}}' and company_id = '{{COMPANY_ID}}' -- decode it (it's
-- just base64 JSON, e.g. at jwt.io) to confirm before trying the full UI.
