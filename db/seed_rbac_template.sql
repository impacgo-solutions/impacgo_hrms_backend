-- ============================================================================
-- Default RBAC template, stored as real data in `_template` -- REVIEW AND
-- RUN BY HAND, once, against `_template` only. Nothing here touches any
-- real tenant schema.
-- ============================================================================
--
-- Why this exists: `_template` has always been a purely STRUCTURAL clone
-- source (public.provision_tenant() does `CREATE TABLE tenant.X (LIKE
-- _template.X INCLUDING ALL)` -- columns/indexes only, zero data, zero FK
-- constraints anywhere on a tenant table). Roles & Permissions has instead
-- always been provisioned ad hoc, per company, straight from Python
-- constants (backend/app/rbac_columns.py's BUILTIN_ROLES/DEFAULT_MATRIX/
-- ROLE_BLURBS, applied by backend/app/seed_new_company.py's ensure_roles()
-- for every demo company it seeds, Vertexa included).
--
-- Investigation confirmed Vertexa's live RBAC configuration -- and every
-- other tenant's -- is 100% derivable from those same rbac_columns.py
-- constants: all 14 built-in roles, their ROLE_BLURBS descriptions, and
-- DEFAULT_MATRIX's per-column levels, with ZERO granular
-- (view/create/edit/delete/approve/export/...) action grants, ZERO
-- designation deny-list rows, and ZERO hierarchy-role-binding rows
-- anywhere (hierarchy rungs already resolve correctly by role NAME via
-- org_hierarchy.py's _RUNG_DEFAULTS with no row needed). So "use Vertexa as
-- the reference template" is exactly "make those existing constants the
-- durable, DB-stored default" -- this file is that, plus (a deliberate,
-- reviewable addition -- see backend/app/rbac_template.py) a first set of
-- granular action grants derived from each role's matrix levels, since no
-- tenant has ever populated that newer system and it would otherwise ship
-- silently empty for every future tenant too.
--
-- WHERE THE TEMPLATE DATA LIVES: a sentinel row in `_template.core_companies`
-- (fixed id below, is_active = false, obviously-not-a-real-company name)
-- anchors 14 `_template.core_roles` rows and their `_template.core_permissions`
-- / `_template.core_role_permissions` rows (both the legacy matrix and the
-- new granular grants). This is safe ONLY because these tables have zero FK
-- constraints anywhere in this schema (confirmed against the live
-- `_template`/`acme` schemas) -- company_id is an app-level scoping column,
-- never DB-enforced, so a sentinel company_id that doesn't correspond to a
-- real tenant cannot violate anything.
--
-- SYNCED, NOT JUST APPENDED (this matters if you ever re-run this file):
-- every grant below is written as DELETE-then-INSERT, keyed per (role,
-- resource) -- resource meaning a legacy matrix column (e.g.
-- leave_approval) or a granular catalog resource (e.g. employee). A
-- re-run first removes any existing _template grant for that role+resource
-- that no longer matches the CURRENT rbac_columns.py/rbac_template.py
-- output (including removing it entirely if the current value is now "no
-- access" / no actions), then inserts whatever's missing. This is required
-- for correctness: a naive "insert if this exact code doesn't already
-- exist" (what an earlier version of this file did) cannot detect that a
-- role already has, say, leave_approval set at a DIFFERENT level -- two
-- different single-character levels are two different `code` values, so a
-- pure-append script would leave a role with BOTH linked simultaneously,
-- and which one `crud.build_role_matrix` picks would be undefined. This
-- surfaced for real during this project when rbac_columns.DEFAULT_MATRIX's
-- "HR / Recruitment Staff" row changed projects_access from 'n' to 'e' --
-- re-running the old append-only script after that edit would have left a
-- stale, conflicting state in _template itself. DELETE-then-INSERT makes
-- this file safe to re-run any number of times, always converging on
-- exactly what rbac_columns.py/rbac_template.py currently say -- ONLY ever
-- touching `_template`'s own sentinel-company rows, never a real tenant
-- (see add_provision_tenant_rbac_function.sql for how a tenant's own
-- customizations are protected once provisioned from this template).
--
-- HOW A TENANT ACTUALLY GETS THIS DATA: this file only populates
-- `_template` itself -- it does not touch any tenant schema. See
-- backend/db/add_provision_tenant_rbac_function.sql for
-- public.provision_tenant_rbac(tenant_slug, company_id) and public.
-- apply_template_role_grants(tenant_slug, role_id, template_role_name), the
-- functions that copy these rows (by role NAME or an explicit role id, and
-- permission CODE, generating fresh UUIDs) into a real tenant -- used by
-- both backend/app/provision_tenant.py (brand new tenants) and
-- backend/app/migrate_apply_rbac_template.py (backfilling existing
-- tenants, additive per-resource, never overwrites a tenant's own
-- customization for a resource it has already configured).
--
-- `core_designation_permissions` (the per-designation deny-list) and
-- `core_hierarchy_role_bindings` intentionally get ZERO rows here -- that
-- faithfully matches every real tenant's current state; nothing needs
-- seeding for either mechanism to work correctly by default.
--
-- Regenerating the matrix/granular blocks below, if rbac_columns.py or
-- rbac_template.py ever change (from backend/):
--   python -m app.generate_rbac_template_actions_sql   (granular grants)
-- The matrix roles/permissions block is generated by a one-off script that
-- is not part of this repo (it's a short loop over
-- rbac_columns.BUILTIN_ROLES/DEFAULT_MATRIX/ROLE_BLURBS mirroring the same
-- DELETE-then-INSERT shape as the granular generator) -- regenerate by hand
-- from those constants if they ever change.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- STEP 0 -- Sentinel template-company row (anchors every role/permission
-- below). Not a real company: is_active = false, obviously-templated name.
-- ----------------------------------------------------------------------------

INSERT INTO _template.core_companies (id, name, legal_name, default_currency_id, code, is_active, country)
SELECT
    '11111111-1111-1111-1111-111111111111',
    '__RBAC_TEMPLATE__',
    '__RBAC_TEMPLATE__',
    (SELECT id FROM public.currencies WHERE code = 'INR'),
    '_RBAC_TEMPLATE_',
    false,
    'India'
WHERE NOT EXISTS (
    SELECT 1 FROM _template.core_companies WHERE id = '11111111-1111-1111-1111-111111111111'
);

-- If the SELECT above inserted zero rows AND the verification query at the
-- bottom of this file also shows zero rows for the sentinel company, the
-- most likely cause is public.currencies having no 'INR' row -- that makes
-- default_currency_id NULL, which violates its NOT NULL constraint and
-- ABORTS this INSERT with an error (not a silent no-op). Run
-- `SELECT id, code FROM public.currencies;` first to confirm an 'INR' row
-- exists; every company this codebase has ever seeded relies on it.


-- ----------------------------------------------------------------------------
-- STEP 1 -- 14 built-in roles + their legacy matrix permissions/grants
-- (verbatim from backend/app/rbac_columns.py)
-- ----------------------------------------------------------------------------

-- Roles (14 built-in roles, verbatim from rbac_columns.BUILTIN_ROLES / ROLE_BLURBS)
INSERT INTO _template.core_roles (id, company_id, name, description, is_system, band_id)
SELECT gen_random_uuid(), '11111111-1111-1111-1111-111111111111', v.name, v.description, true, NULL
FROM (VALUES
    ('Organization Owner / CEO', 'Full visibility and control across every module, branch and approval chain.'),
    ('C-Level Executive', 'Company-wide visibility with edit rights over org structure, benefits and platform settings.'),
    ('VP / Director', 'Company-wide visibility across your function, with edit rights on performance and org structure.'),
    ('General Manager / Sr. Manager', 'Your reporting line only — attendance, leave and timesheet approvals for your team.'),
    ('Manager', 'Your direct reports only — day-to-day approvals, no org-wide administration.'),
    ('Team Lead', 'Your squad only — first-line attendance & timesheet review, view-only on leave and org data.'),
    ('HR / Recruitment Staff', 'Full People, Recruitment and Benefits administration; no org-structure or platform settings access.'),
    ('Finance / Payroll Staff', 'Full payroll processing and benefits administration; no attendance, performance or recruitment access.'),
    ('IT / System Admin', 'Full platform administration and asset management; no HR, payroll or performance access.'),
    ('Branch Manager', 'Branch-scoped view of people, attendance and performance; no payroll, recruitment or platform access.'),
    ('Branch Head', 'Oversees every Branch Manager company-wide — cross-branch visibility and approvals, no platform administration.'),
    ('Project Manager', 'Approve leave, timesheets and travel for your project team; view-only on broader org and payroll data.'),
    ('Professional / IC Employee', 'Self-service only — your profile, attendance, leave, payslip and assigned work.'),
    ('Associate / Intern', 'Self-service only — your profile, attendance, leave, payslip and assigned work.')
) AS v(name, description)
WHERE NOT EXISTS (
    SELECT 1 FROM _template.core_roles r
    WHERE r.company_id = '11111111-1111-1111-1111-111111111111' AND r.name = v.name
);

-- Matrix permissions (one row per distinct column.level pair actually
-- used across DEFAULT_MATRIX today, code format matches crud.get_or_create_permission)
INSERT INTO _template.core_permissions (id, code, module, resource, action)
SELECT gen_random_uuid(), v.code, v.module, v.resource, v.action
FROM (VALUES
    ('asset_management.a', 'assets', 'asset_management', 'a'),
    ('asset_management.e', 'assets', 'asset_management', 'e'),
    ('asset_management.s', 'assets', 'asset_management', 's'),
    ('asset_management.v', 'assets', 'asset_management', 'v'),
    ('audit_logs.a', 'admin', 'audit_logs', 'a'),
    ('audit_logs.v', 'admin', 'audit_logs', 'v'),
    ('benefits_admin.a', 'benefits', 'benefits_admin', 'a'),
    ('benefits_admin.e', 'benefits', 'benefits_admin', 'e'),
    ('benefits_admin.v', 'benefits', 'benefits_admin', 'v'),
    ('leave_approval.e', 'leave', 'leave_approval', 'e'),
    ('leave_approval.v', 'leave', 'leave_approval', 'v'),
    ('org_structure_config.a', 'organization', 'org_structure_config', 'a'),
    ('org_structure_config.e', 'organization', 'org_structure_config', 'e'),
    ('org_structure_config.v', 'organization', 'org_structure_config', 'v'),
    ('own_profile.s', 'profile', 'own_profile', 's'),
    ('payroll_process.a', 'payroll', 'payroll_process', 'a'),
    ('payroll_process.e', 'payroll', 'payroll_process', 'e'),
    ('payroll_process.v', 'payroll', 'payroll_process', 'v'),
    ('payroll_view_own.s', 'payroll', 'payroll_view_own', 's'),
    ('performance_reviews.e', 'performance', 'performance_reviews', 'e'),
    ('performance_reviews.s', 'performance', 'performance_reviews', 's'),
    ('performance_reviews.v', 'performance', 'performance_reviews', 'v'),
    ('projects_access.a', 'projects', 'projects_access', 'a'),
    ('projects_access.e', 'projects', 'projects_access', 'e'),
    ('projects_access.s', 'projects', 'projects_access', 's'),
    ('projects_access.v', 'projects', 'projects_access', 'v'),
    ('recruitment.a', 'recruitment', 'recruitment', 'a'),
    ('recruitment.v', 'recruitment', 'recruitment', 'v'),
    ('reports_analytics.a', 'reports', 'reports_analytics', 'a'),
    ('reports_analytics.e', 'reports', 'reports_analytics', 'e'),
    ('reports_analytics.v', 'reports', 'reports_analytics', 'v'),
    ('system_settings_rbac.a', 'admin', 'system_settings_rbac', 'a'),
    ('system_settings_rbac.e', 'admin', 'system_settings_rbac', 'e'),
    ('team_attendance.e', 'attendance', 'team_attendance', 'e'),
    ('team_attendance.s', 'attendance', 'team_attendance', 's'),
    ('team_attendance.v', 'attendance', 'team_attendance', 'v'),
    ('timesheet_approval.e', 'work', 'timesheet_approval', 'e'),
    ('timesheet_approval.s', 'work', 'timesheet_approval', 's'),
    ('timesheet_approval.v', 'work', 'timesheet_approval', 'v'),
    ('travel_expense_approval.a', 'travel', 'travel_expense_approval', 'a'),
    ('travel_expense_approval.e', 'travel', 'travel_expense_approval', 'e')
) AS v(code, module, resource, action)
WHERE NOT EXISTS (
    SELECT 1 FROM _template.core_permissions p WHERE p.code = v.code
);

-- Matrix role_permission grants, per built-in role -- SYNCED (not just
-- appended): for every column, first delete any existing _template grant
-- for that role+column that no longer matches the CURRENT DEFAULT_MATRIX
-- level (including removing it entirely if the current level is 'n'),
-- then insert the current grant if it's not already there. Re-running
-- this file after rbac_columns.DEFAULT_MATRIX changes keeps _template
-- exactly in sync instead of accumulating stale/conflicting levels --
-- this ONLY ever touches _template's own sentinel-company rows, never a
-- real tenant (see add_provision_tenant_rbac_function.sql for how a
-- tenant's OWN customizations are protected once provisioned).
-- Organization Owner / CEO
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'team_attendance.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'leave_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'timesheet_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
      AND code <> 'payroll_process.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'payroll_process.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
      AND code <> 'benefits_admin.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'benefits_admin.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
      AND code <> 'recruitment.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'recruitment.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'performance_reviews.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
      AND code <> 'org_structure_config.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'org_structure_config.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'reports_analytics.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
      AND code <> 'system_settings_rbac.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'system_settings_rbac.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
      AND code <> 'audit_logs.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'audit_logs.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
      AND code <> 'travel_expense_approval.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'travel_expense_approval.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'projects_access.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'asset_management.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- C-Level Executive
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'team_attendance.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'leave_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'timesheet_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
      AND code <> 'payroll_process.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'payroll_process.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
      AND code <> 'benefits_admin.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'benefits_admin.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
      AND code <> 'recruitment.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'recruitment.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'performance_reviews.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
      AND code <> 'org_structure_config.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'org_structure_config.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'reports_analytics.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
      AND code <> 'system_settings_rbac.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'system_settings_rbac.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
      AND code <> 'audit_logs.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'audit_logs.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
      AND code <> 'travel_expense_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'travel_expense_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'projects_access.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'asset_management.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- VP / Director
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'team_attendance.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'leave_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'timesheet_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
      AND code <> 'benefits_admin.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'benefits_admin.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
      AND code <> 'recruitment.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'recruitment.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'performance_reviews.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
      AND code <> 'org_structure_config.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'org_structure_config.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'reports_analytics.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
      AND code <> 'audit_logs.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'audit_logs.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'projects_access.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'asset_management.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- General Manager / Sr. Manager
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'team_attendance.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'leave_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'timesheet_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
      AND code <> 'benefits_admin.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'benefits_admin.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
      AND code <> 'recruitment.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'recruitment.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'performance_reviews.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
      AND code <> 'org_structure_config.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'org_structure_config.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'reports_analytics.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
      AND code <> 'travel_expense_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'travel_expense_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'projects_access.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'asset_management.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Manager
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'team_attendance.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'leave_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'timesheet_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
      AND code <> 'recruitment.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'recruitment.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'performance_reviews.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'reports_analytics.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
      AND code <> 'travel_expense_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'travel_expense_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'projects_access.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'asset_management.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Team Lead
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'team_attendance.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'leave_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'timesheet_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'performance_reviews.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'reports_analytics.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'projects_access.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'asset_management.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- HR / Recruitment Staff
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'team_attendance.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'leave_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
      AND code <> 'payroll_process.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'payroll_process.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
      AND code <> 'benefits_admin.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'benefits_admin.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
      AND code <> 'recruitment.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'recruitment.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'performance_reviews.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'reports_analytics.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'projects_access.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
);

-- Finance / Payroll Staff
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'timesheet_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
      AND code <> 'payroll_process.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'payroll_process.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
      AND code <> 'benefits_admin.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'benefits_admin.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'reports_analytics.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
      AND code <> 'travel_expense_approval.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'travel_expense_approval.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
);

-- IT / System Admin
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'reports_analytics.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
      AND code <> 'system_settings_rbac.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'system_settings_rbac.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
      AND code <> 'audit_logs.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'audit_logs.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'asset_management.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Branch Manager
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'team_attendance.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'leave_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'performance_reviews.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'reports_analytics.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'projects_access.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'asset_management.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Branch Head
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'team_attendance.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'leave_approval.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'timesheet_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'performance_reviews.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
      AND code <> 'org_structure_config.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'org_structure_config.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'reports_analytics.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
      AND code <> 'travel_expense_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'travel_expense_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'projects_access.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'asset_management.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Project Manager
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'team_attendance.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
      AND code <> 'leave_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'leave_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'timesheet_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'performance_reviews.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
      AND code <> 'reports_analytics.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'reports_analytics.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
      AND code <> 'travel_expense_approval.e'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'travel_expense_approval.e'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.a'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'projects_access.a'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.v'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'asset_management.v'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Professional / IC Employee
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'team_attendance.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'timesheet_approval.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'performance_reviews.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'projects_access.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'asset_management.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Associate / Intern
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'own_profile' AND length(action) = 1
      AND code <> 'own_profile.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'own_profile.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'team_attendance' AND length(action) = 1
      AND code <> 'team_attendance.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'team_attendance.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'timesheet_approval' AND length(action) = 1
      AND code <> 'timesheet_approval.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'timesheet_approval.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_view_own' AND length(action) = 1
      AND code <> 'payroll_view_own.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'payroll_view_own.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll_process' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits_admin' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance_reviews' AND length(action) = 1
      AND code <> 'performance_reviews.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'performance_reviews.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'org_structure_config' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports_analytics' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'system_settings_rbac' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'audit_logs' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel_expense_approval' AND length(action) = 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects_access' AND length(action) = 1
      AND code <> 'projects_access.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'projects_access.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'asset_management' AND length(action) = 1
      AND code <> 'asset_management.s'
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'asset_management.s'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );


-- ----------------------------------------------------------------------------
-- STEP 2 -- Granular (resource, action) grants -- View/Create/Edit/Delete/
-- Approve/Export/etc. Synthesized defaults derived from each role's matrix
-- levels above (backend/app/rbac_template.py's derive_default_actions) --
-- see this file's header comment for why. No tenant, including Vertexa, has
-- ever populated this system, so there is nothing to mine here; this is new
-- policy, reviewed once as a whole rather than typed per role/resource.
-- This is the block that grants HR / Recruitment Staff (and Owner, IT
-- Admin) their `employee.*` actions -- e.g. the "HR can see the Employee
-- Directory" default -- since the legacy 16-column matrix has no column for
-- employee-record management at all.
-- ----------------------------------------------------------------------------

-- Auto-generated by: python -m app.generate_rbac_template_actions_sql
-- Do not hand-edit the VALUES lists below -- regenerate instead if the
-- derivation rules in backend/app/rbac_template.py change.

-- Granular action permissions (one row per distinct (resource, action)
-- pair used by any built-in role's current derived defaults).
INSERT INTO _template.core_permissions (id, code, module, resource, action)
SELECT gen_random_uuid(), v.code, v.resource, v.resource, v.action
FROM (VALUES
    ('admin.configure', 'admin', 'configure'),
    ('admin.manage', 'admin', 'manage'),
    ('admin.view', 'admin', 'view'),
    ('approvals.view', 'approvals', 'view'),
    ('assets.assign', 'assets', 'assign'),
    ('assets.create', 'assets', 'create'),
    ('assets.delete', 'assets', 'delete'),
    ('assets.edit', 'assets', 'edit'),
    ('assets.manage', 'assets', 'manage'),
    ('assets.view', 'assets', 'view'),
    ('attendance.approve', 'attendance', 'approve'),
    ('attendance.create', 'attendance', 'create'),
    ('attendance.edit', 'attendance', 'edit'),
    ('attendance.reject', 'attendance', 'reject'),
    ('attendance.view', 'attendance', 'view'),
    ('benefits.create', 'benefits', 'create'),
    ('benefits.delete', 'benefits', 'delete'),
    ('benefits.edit', 'benefits', 'edit'),
    ('benefits.manage', 'benefits', 'manage'),
    ('benefits.view', 'benefits', 'view'),
    ('config.edit', 'config', 'edit'),
    ('config.view', 'config', 'view'),
    ('dashboard.view', 'dashboard', 'view'),
    ('documents.create', 'documents', 'create'),
    ('documents.delete', 'documents', 'delete'),
    ('documents.manage', 'documents', 'manage'),
    ('documents.view', 'documents', 'view'),
    ('employee.create', 'employee', 'create'),
    ('employee.delete', 'employee', 'delete'),
    ('employee.edit', 'employee', 'edit'),
    ('employee.export', 'employee', 'export'),
    ('employee.import', 'employee', 'import'),
    ('employee.terminate', 'employee', 'terminate'),
    ('employee.transfer', 'employee', 'transfer'),
    ('employee.view', 'employee', 'view'),
    ('learning.create', 'learning', 'create'),
    ('learning.edit', 'learning', 'edit'),
    ('learning.view', 'learning', 'view'),
    ('leave.approve', 'leave', 'approve'),
    ('leave.reject', 'leave', 'reject'),
    ('leave.view', 'leave', 'view'),
    ('organization.configure', 'organization', 'configure'),
    ('organization.edit', 'organization', 'edit'),
    ('organization.view', 'organization', 'view'),
    ('payroll.delete', 'payroll', 'delete'),
    ('payroll.edit', 'payroll', 'edit'),
    ('payroll.export', 'payroll', 'export'),
    ('payroll.generate', 'payroll', 'generate'),
    ('payroll.view', 'payroll', 'view'),
    ('performance.approve', 'performance', 'approve'),
    ('performance.create', 'performance', 'create'),
    ('performance.edit', 'performance', 'edit'),
    ('performance.view', 'performance', 'view'),
    ('projects.assign', 'projects', 'assign'),
    ('projects.create', 'projects', 'create'),
    ('projects.delete', 'projects', 'delete'),
    ('projects.edit', 'projects', 'edit'),
    ('projects.view', 'projects', 'view'),
    ('recruitment.create', 'recruitment', 'create'),
    ('recruitment.delete', 'recruitment', 'delete'),
    ('recruitment.edit', 'recruitment', 'edit'),
    ('recruitment.manage', 'recruitment', 'manage'),
    ('recruitment.view', 'recruitment', 'view'),
    ('reports.export', 'reports', 'export'),
    ('reports.import', 'reports', 'import'),
    ('reports.view', 'reports', 'view'),
    ('travel.approve', 'travel', 'approve'),
    ('travel.create', 'travel', 'create'),
    ('travel.edit', 'travel', 'edit'),
    ('travel.reject', 'travel', 'reject'),
    ('travel.view', 'travel', 'view'),
    ('work.approve', 'work', 'approve'),
    ('work.create', 'work', 'create'),
    ('work.edit', 'work', 'edit'),
    ('work.reject', 'work', 'reject'),
    ('work.view', 'work', 'view')
) AS v(code, resource, action)
WHERE NOT EXISTS (
    SELECT 1 FROM _template.core_permissions p WHERE p.code = v.code
);

-- Granular role_permission grants, per built-in role -- SYNCED across the
-- full resource catalog (see module docstring above).
-- Organization Owner / CEO
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
      AND code NOT IN ('admin.configure', 'admin.manage', 'admin.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'admin.configure'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'admin.manage'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'admin.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
      AND code NOT IN ('approvals.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'approvals.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.assign', 'assets.create', 'assets.delete', 'assets.edit', 'assets.manage', 'assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'assets.assign'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'assets.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'assets.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'assets.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'assets.manage'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
      AND code NOT IN ('benefits.create', 'benefits.delete', 'benefits.edit', 'benefits.manage', 'benefits.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'benefits.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'benefits.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'benefits.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'benefits.manage'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'benefits.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
      AND code NOT IN ('config.edit', 'config.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'config.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'config.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.delete', 'documents.manage', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'documents.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'documents.manage'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.create', 'employee.delete', 'employee.edit', 'employee.export', 'employee.import', 'employee.terminate', 'employee.transfer', 'employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'employee.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'employee.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'employee.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'employee.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'employee.import'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'employee.terminate'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'employee.transfer'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
      AND code NOT IN ('organization.configure', 'organization.edit', 'organization.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'organization.configure'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'organization.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'organization.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
      AND code NOT IN ('payroll.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'payroll.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.assign', 'projects.create', 'projects.delete', 'projects.edit', 'projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'projects.assign'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'projects.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'projects.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'projects.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
      AND code NOT IN ('recruitment.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'recruitment.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.export', 'reports.import', 'reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'reports.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'reports.import'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
      AND code NOT IN ('travel.approve', 'travel.create', 'travel.edit', 'travel.reject', 'travel.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'travel.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'travel.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'travel.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'travel.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'travel.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Organization Owner / CEO'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Organization Owner / CEO'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- C-Level Executive
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
      AND code NOT IN ('admin.configure', 'admin.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'admin.configure'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'admin.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
      AND code NOT IN ('approvals.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'approvals.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.create', 'assets.edit', 'assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'assets.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'assets.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
      AND code NOT IN ('benefits.create', 'benefits.edit', 'benefits.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'benefits.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'benefits.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'benefits.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
      AND code NOT IN ('config.edit', 'config.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'config.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'config.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.create', 'employee.edit', 'employee.export', 'employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'employee.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'employee.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'employee.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
      AND code NOT IN ('organization.edit', 'organization.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'organization.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'organization.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
      AND code NOT IN ('payroll.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'payroll.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.create', 'projects.edit', 'projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'projects.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'projects.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
      AND code NOT IN ('recruitment.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'recruitment.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.export', 'reports.import', 'reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'reports.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'reports.import'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
      AND code NOT IN ('travel.approve', 'travel.create', 'travel.edit', 'travel.reject', 'travel.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'travel.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'travel.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'travel.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'travel.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'travel.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'C-Level Executive'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'C-Level Executive'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- VP / Director
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
      AND code NOT IN ('benefits.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'benefits.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
      AND code NOT IN ('config.edit', 'config.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'config.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'config.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.create', 'employee.edit', 'employee.export', 'employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'employee.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'employee.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'employee.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.create', 'learning.edit', 'learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'learning.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'learning.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
      AND code NOT IN ('organization.edit', 'organization.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'organization.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'organization.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.approve', 'performance.create', 'performance.edit', 'performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'performance.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'performance.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'performance.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
      AND code NOT IN ('recruitment.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'recruitment.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.export', 'reports.import', 'reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'reports.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'reports.import'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'VP / Director'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'VP / Director'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- General Manager / Sr. Manager
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
      AND code NOT IN ('approvals.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'approvals.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.create', 'assets.edit', 'assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'assets.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'assets.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.approve', 'attendance.create', 'attendance.edit', 'attendance.reject', 'attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'attendance.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'attendance.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'attendance.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'attendance.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
      AND code NOT IN ('benefits.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'benefits.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
      AND code NOT IN ('config.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'config.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.edit', 'employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'employee.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.create', 'learning.edit', 'learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'learning.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'learning.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.approve', 'leave.reject', 'leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'leave.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'leave.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
      AND code NOT IN ('organization.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'organization.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.approve', 'performance.create', 'performance.edit', 'performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'performance.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'performance.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'performance.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.create', 'projects.edit', 'projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'projects.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'projects.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
      AND code NOT IN ('recruitment.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'recruitment.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.export', 'reports.import', 'reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'reports.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'reports.import'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
      AND code NOT IN ('travel.approve', 'travel.create', 'travel.edit', 'travel.reject', 'travel.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'travel.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'travel.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'travel.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'travel.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'travel.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'General Manager / Sr. Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.approve', 'work.create', 'work.edit', 'work.reject', 'work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'work.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'work.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'work.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'work.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'General Manager / Sr. Manager'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Manager
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
      AND code NOT IN ('approvals.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'approvals.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.create', 'assets.edit', 'assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'assets.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'assets.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.approve', 'attendance.create', 'attendance.edit', 'attendance.reject', 'attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'attendance.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'attendance.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'attendance.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'attendance.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.create', 'learning.edit', 'learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'learning.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'learning.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.approve', 'leave.reject', 'leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'leave.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'leave.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.approve', 'performance.create', 'performance.edit', 'performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'performance.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'performance.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'performance.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.create', 'projects.edit', 'projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'projects.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'projects.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
      AND code NOT IN ('recruitment.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'recruitment.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.export', 'reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'reports.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
      AND code NOT IN ('travel.approve', 'travel.create', 'travel.edit', 'travel.reject', 'travel.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'travel.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'travel.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'travel.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'travel.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'travel.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.approve', 'work.create', 'work.edit', 'work.reject', 'work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'work.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'work.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'work.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'work.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Manager'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Team Lead
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
      AND code NOT IN ('approvals.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'approvals.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.approve', 'attendance.create', 'attendance.edit', 'attendance.reject', 'attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'attendance.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'attendance.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'attendance.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'attendance.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.create', 'projects.edit', 'projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'projects.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'projects.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Team Lead'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.approve', 'work.create', 'work.edit', 'work.reject', 'work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'work.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'work.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'work.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'work.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Team Lead'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- HR / Recruitment Staff
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
      AND code NOT IN ('benefits.create', 'benefits.edit', 'benefits.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'benefits.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'benefits.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'benefits.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.delete', 'documents.manage', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'documents.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'documents.manage'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.create', 'employee.delete', 'employee.edit', 'employee.export', 'employee.import', 'employee.terminate', 'employee.transfer', 'employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'employee.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'employee.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'employee.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'employee.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'employee.import'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'employee.terminate'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'employee.transfer'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.create', 'learning.edit', 'learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'learning.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'learning.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
      AND code NOT IN ('payroll.edit', 'payroll.generate', 'payroll.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'payroll.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'payroll.generate'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'payroll.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.approve', 'performance.create', 'performance.edit', 'performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'performance.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'performance.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'performance.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.create', 'projects.edit', 'projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'projects.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'projects.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
      AND code NOT IN ('recruitment.create', 'recruitment.delete', 'recruitment.edit', 'recruitment.manage', 'recruitment.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'recruitment.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'recruitment.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'recruitment.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'recruitment.manage'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'recruitment.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.export', 'reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'reports.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'HR / Recruitment Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
);

-- Finance / Payroll Staff
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
      AND code NOT IN ('approvals.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'approvals.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
      AND code NOT IN ('benefits.create', 'benefits.edit', 'benefits.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'benefits.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'benefits.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'benefits.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.export', 'employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'employee.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
      AND code NOT IN ('payroll.delete', 'payroll.edit', 'payroll.export', 'payroll.generate', 'payroll.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'payroll.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'payroll.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'payroll.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'payroll.generate'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'payroll.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.export', 'reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'reports.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
      AND code NOT IN ('travel.approve', 'travel.create', 'travel.edit', 'travel.reject', 'travel.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'travel.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'travel.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'travel.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'travel.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'travel.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Finance / Payroll Staff'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Finance / Payroll Staff'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- IT / System Admin
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
      AND code NOT IN ('admin.configure', 'admin.manage', 'admin.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'admin.configure'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'admin.manage'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'admin.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.assign', 'assets.create', 'assets.delete', 'assets.edit', 'assets.manage', 'assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'assets.assign'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'assets.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'assets.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'assets.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'assets.manage'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
      AND code NOT IN ('config.edit', 'config.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'config.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'config.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.delete', 'documents.manage', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'documents.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'documents.manage'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.export', 'employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'employee.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'IT / System Admin'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'IT / System Admin'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
);

-- Branch Manager
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Manager'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
);

-- Branch Head
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
      AND code NOT IN ('approvals.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'approvals.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
      AND code NOT IN ('config.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'config.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
      AND code NOT IN ('organization.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'organization.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.export', 'reports.import', 'reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'reports.export'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'reports.import'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
      AND code NOT IN ('travel.approve', 'travel.create', 'travel.edit', 'travel.reject', 'travel.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'travel.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'travel.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'travel.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'travel.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'travel.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Branch Head'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.approve', 'work.create', 'work.edit', 'work.reject', 'work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'work.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'work.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'work.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'work.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Branch Head'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Project Manager
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
      AND code NOT IN ('approvals.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'approvals.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.create', 'documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'documents.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
      AND code NOT IN ('employee.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'employee.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
      AND code NOT IN ('leave.approve', 'leave.reject', 'leave.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'leave.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'leave.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'leave.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.assign', 'projects.create', 'projects.delete', 'projects.edit', 'projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'projects.assign'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'projects.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'projects.delete'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'projects.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
      AND code NOT IN ('reports.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'reports.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
      AND code NOT IN ('travel.approve', 'travel.create', 'travel.edit', 'travel.reject', 'travel.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'travel.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'travel.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'travel.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'travel.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'travel.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Project Manager'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.approve', 'work.create', 'work.edit', 'work.reject', 'work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'work.approve'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'work.create'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'work.edit'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'work.reject'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Project Manager'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Professional / IC Employee
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Professional / IC Employee'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Professional / IC Employee'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );

-- Associate / Intern
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'admin' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'approvals' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'assets' AND length(action) > 1
      AND code NOT IN ('assets.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'assets.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'attendance' AND length(action) > 1
      AND code NOT IN ('attendance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'attendance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'benefits' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'config' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'dashboard' AND length(action) > 1
      AND code NOT IN ('dashboard.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'dashboard.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'documents' AND length(action) > 1
      AND code NOT IN ('documents.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'documents.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'employee' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'learning' AND length(action) > 1
      AND code NOT IN ('learning.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'learning.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'leave' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'organization' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'payroll' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'performance' AND length(action) > 1
      AND code NOT IN ('performance.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'performance.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'projects' AND length(action) > 1
      AND code NOT IN ('projects.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'projects.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'recruitment' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'reports' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'travel' AND length(action) > 1
);
DELETE FROM _template.core_role_permissions
WHERE role_id = (
    SELECT id FROM _template.core_roles
    WHERE company_id = '11111111-1111-1111-1111-111111111111' AND name = 'Associate / Intern'
)
AND permission_id IN (
    SELECT id FROM _template.core_permissions
    WHERE resource = 'work' AND length(action) > 1
      AND code NOT IN ('work.view')
);
INSERT INTO _template.core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM _template.core_roles r, _template.core_permissions p
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'Associate / Intern'
  AND p.code = 'work.view'
  AND NOT EXISTS (
      SELECT 1 FROM _template.core_role_permissions rp
      WHERE rp.role_id = r.id AND rp.permission_id = p.id
  );


-- ----------------------------------------------------------------------------
-- STEP 3 -- Designation deny-list / hierarchy role bindings: NO ROWS, BY
-- DESIGN.
-- ----------------------------------------------------------------------------
-- `_template.core_designation_permissions` (a DENY-list -- see
-- backend/db/add_designation_permissions.sql) and
-- `_template.core_hierarchy_role_bindings` (per-company rung->role overrides
-- -- see backend/app/org_hierarchy.py) are left completely empty here,
-- matching every real tenant's actual current state:
--   * No tenant has ever restricted a designation from an action its role
--     grants -- the deny-list starts empty everywhere, and an empty list
--     means "no restrictions", which is the correct default.
--   * No tenant has ever configured a hierarchy-role binding -- rungs
--     (team_lead / project_manager / senior_manager / branch_manager /
--     branch_head) already resolve correctly by matching a role's NAME
--     against org_hierarchy.py's _RUNG_DEFAULTS, with no row required.
-- A tenant (or the template, later) can add rows to either table at any
-- time through the existing Administration screens -- nothing about this
-- template requires them to be empty forever, only that a freshly
-- provisioned company starts with no extra restrictions, same as today.


-- ============================================================================
-- Verification (read-only) -- run these ONE AT A TIME if your SQL client
-- only shows the result of the LAST statement in a multi-statement script
-- (several GUI tools do this) -- a summary count of 0 on an unrelated
-- earlier check is easy to mistake for "the seed did nothing" when it's
-- actually one of the two checks below that are SUPPOSED to read 0.
-- ============================================================================

-- Expect: 0 (see STEP 3 above -- this table is deliberately empty)
SELECT count(*) AS designation_permissions_expect_0 FROM _template.core_designation_permissions;

-- Expect: 0 (see STEP 3 above -- this table is deliberately empty)
SELECT count(*) AS hierarchy_role_bindings_expect_0 FROM _template.core_hierarchy_role_bindings;

-- Expect: 1 row, is_active = false
SELECT id, name, is_active FROM _template.core_companies
WHERE id = '11111111-1111-1111-1111-111111111111';

-- Expect: 14 rows
SELECT name, is_system FROM _template.core_roles
WHERE company_id = '11111111-1111-1111-1111-111111111111'
ORDER BY name;

-- Expect: 14 rows, matrix_grants > 0 for every role
SELECT r.name, count(*) AS matrix_grants
FROM _template.core_roles r
JOIN _template.core_role_permissions rp ON rp.role_id = r.id
JOIN _template.core_permissions p ON p.id = rp.permission_id
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND length(p.action) = 1  -- matrix levels are single chars (n/v/s/e/a)
GROUP BY r.name
ORDER BY r.name;

-- Expect: most roles > 0 (Owner, HR, IT Admin especially -- these are the
-- roles most likely to hold employee/admin/assets actions)
SELECT r.name, count(*) AS granular_grants
FROM _template.core_roles r
JOIN _template.core_role_permissions rp ON rp.role_id = r.id
JOIN _template.core_permissions p ON p.id = rp.permission_id
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND length(p.action) > 1  -- granular actions are full words
GROUP BY r.name
ORDER BY r.name;

-- Spot-check the exact issue this template exists to fix: HR / Recruitment
-- Staff should have employee.view (Employee Directory access) by default.
-- Expect: at least one row, resource='employee', action IN ('view', ...)
SELECT p.resource, p.action
FROM _template.core_roles r
JOIN _template.core_role_permissions rp ON rp.role_id = r.id
JOIN _template.core_permissions p ON p.id = rp.permission_id
WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
  AND r.name = 'HR / Recruitment Staff'
  AND p.resource = 'employee'
ORDER BY p.action;

-- ----------------------------------------------------------------------------
-- FINAL SUMMARY -- run last; this is the one query whose result actually
-- tells you whether the seed worked, if your tool only shows the last
-- statement's output.
-- ----------------------------------------------------------------------------
SELECT
    (SELECT count(*) FROM _template.core_companies
        WHERE id = '11111111-1111-1111-1111-111111111111')          AS sentinel_company_rows_expect_1,
    (SELECT count(*) FROM _template.core_roles
        WHERE company_id = '11111111-1111-1111-1111-111111111111')  AS roles_expect_14,
    (SELECT count(*) FROM _template.core_roles r
        JOIN _template.core_role_permissions rp ON rp.role_id = r.id
        JOIN _template.core_permissions p ON p.id = rp.permission_id
        WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
          AND length(p.action) = 1)                                  AS total_matrix_grants_expect_gt_0,
    (SELECT count(*) FROM _template.core_roles r
        JOIN _template.core_role_permissions rp ON rp.role_id = r.id
        JOIN _template.core_permissions p ON p.id = rp.permission_id
        WHERE r.company_id = '11111111-1111-1111-1111-111111111111'
          AND length(p.action) > 1)                                  AS total_granular_grants_expect_gt_0;
