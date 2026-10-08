-- SEC-01 (report/critical-issues.md): the "Professional / IC Employee" role in
-- tenant impacgo-solutions carried HR's full Admin ('a') grants on every legacy
-- RBAC matrix column (system_settings_rbac.a, payroll_process.a, audit_logs.a,
-- ...), letting any IC reset passwords, grant roles and read all payslips.
--
-- This resets ONLY that role's legacy matrix rows (single-char actions
-- n/v/s/e/a on the 16 rbac_columns.COLUMN_KEYS) to rbac_columns.DEFAULT_MATRIX:
--   s,s,n,s,s,n,n,n,s,n,n,n,n,n,s,s
-- Granular multi-char action grants (e.g. leave.view, assets.view) are kept.
-- Idempotent: re-running leaves the same end state.
--
-- Backup before running:
--   SELECT r.name, p.code FROM "impacgo-solutions".core_role_permissions rp
--   JOIN "impacgo-solutions".core_roles r ON r.id = rp.role_id
--   JOIN "impacgo-solutions".core_permissions p ON p.id = rp.permission_id
--   WHERE r.name = 'Professional / IC Employee';

BEGIN;

SET LOCAL search_path TO "impacgo-solutions", public;

DELETE FROM core_role_permissions rp
USING core_permissions p, core_roles r
WHERE rp.permission_id = p.id
  AND rp.role_id = r.id
  AND r.name = 'Professional / IC Employee'
  AND length(p.action) = 1
  AND p.resource IN (
    'own_profile', 'team_attendance', 'leave_approval', 'timesheet_approval',
    'payroll_view_own', 'payroll_process', 'benefits_admin', 'recruitment',
    'performance_reviews', 'org_structure_config', 'reports_analytics',
    'system_settings_rbac', 'audit_logs', 'travel_expense_approval',
    'projects_access', 'asset_management'
  );

WITH template(resource, action, module) AS (
  VALUES
    ('own_profile', 's', 'profile'),
    ('team_attendance', 's', 'attendance'),
    ('timesheet_approval', 's', 'work'),
    ('payroll_view_own', 's', 'payroll'),
    ('performance_reviews', 's', 'performance'),
    ('projects_access', 's', 'projects'),
    ('asset_management', 's', 'assets')
)
INSERT INTO core_permissions (code, module, resource, action)
SELECT t.resource || '.' || t.action, t.module, t.resource, t.action
FROM template t
WHERE NOT EXISTS (
  SELECT 1 FROM core_permissions p WHERE p.resource = t.resource AND p.action = t.action
);

WITH template(resource, action) AS (
  VALUES
    ('own_profile', 's'),
    ('team_attendance', 's'),
    ('timesheet_approval', 's'),
    ('payroll_view_own', 's'),
    ('performance_reviews', 's'),
    ('projects_access', 's'),
    ('asset_management', 's')
)
INSERT INTO core_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM core_roles r
CROSS JOIN template t
JOIN core_permissions p ON p.resource = t.resource AND p.action = t.action
WHERE r.name = 'Professional / IC Employee'
  AND NOT EXISTS (
    SELECT 1 FROM core_role_permissions x WHERE x.role_id = r.id AND x.permission_id = p.id
  );

COMMIT;
