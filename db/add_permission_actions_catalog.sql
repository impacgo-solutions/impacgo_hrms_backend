-- Metadata-driven catalog of granular (resource, action) permissions,
-- grouped by real module -- the DB-backed replacement for the hardcoded
-- Python list in backend/app/permission_actions.py. Global/public, same
-- schema placement as public.modules/public.menu_items (NOT per-tenant --
-- every tenant shares one catalog of *available* actions; which of a
-- tenant's own roles are actually GRANTED which action is still stored per-
-- tenant in each schema's own core.role_permissions, unchanged).
--
-- Once this table exists, adding a brand-new module's actions (or a new
-- action on an existing module) is a plain INSERT here -- no code change,
-- no redeploy -- and it appears in GET /api/permission-actions and
-- Administration > Add Custom Role automatically, per
-- backend/app/crud.py's list_permission_actions/apply_actions_update now
-- reading this table instead of importing permission_actions.py's
-- GRANULAR_RESOURCES/GRANULAR_ACTION_SET constants.
--
-- NOT executed by the assistant -- review and run by hand, same convention
-- as every other backend/db/*.sql file.
--
-- Safe to re-run: CREATE TABLE IF NOT EXISTS + ON CONFLICT DO NOTHING on
-- the (resource, action) uniqueness every insert below relies on.

CREATE TABLE IF NOT EXISTS public.permission_actions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id uuid NOT NULL REFERENCES public.modules(id),
    resource varchar(60) NOT NULL,
    action varchar(30) NOT NULL,
    label varchar(120) NOT NULL,
    sort_order smallint NOT NULL DEFAULT 0,
    UNIQUE (resource, action)
);

-- Existing 6 resources, unchanged keys/actions (nothing has ever been
-- granted against them -- every live tenant's core.role_permissions has
-- zero granular rows -- but the keys are kept identical anyway on the
-- principle of least surprise), just correctly grouped under their real
-- module for the first time (employee -> people, config -> admin).
INSERT INTO public.permission_actions (module_id, resource, action, label, sort_order)
SELECT m.id, v.resource, v.action, v.label, v.sort_order
FROM public.modules m
JOIN (VALUES
    ('people', 'employee', 'view',      'View Employee',      1),
    ('people', 'employee', 'create',    'Create Employee',    2),
    ('people', 'employee', 'edit',      'Edit Employee',      3),
    ('people', 'employee', 'delete',    'Delete Employee',    4),
    ('people', 'employee', 'transfer',  'Transfer Employee',  5),
    ('people', 'employee', 'terminate', 'Terminate Employee', 6),

    ('leave', 'leave', 'view',    'View Leave',    1),
    ('leave', 'leave', 'approve', 'Approve Leave', 2),
    ('leave', 'leave', 'reject',  'Reject Leave',  3),

    ('payroll', 'payroll', 'view',     'View Payroll',     1),
    ('payroll', 'payroll', 'generate', 'Generate Payroll', 2),
    ('payroll', 'payroll', 'edit',     'Edit Payroll',     3),
    ('payroll', 'payroll', 'delete',   'Delete Payroll',   4),
    ('payroll', 'payroll', 'export',   'Export Payroll',   5),

    ('projects', 'projects', 'view',   'View Projects',   1),
    ('projects', 'projects', 'create', 'Create Projects', 2),
    ('projects', 'projects', 'edit',   'Edit Projects',   3),
    ('projects', 'projects', 'delete', 'Delete Projects', 4),
    ('projects', 'projects', 'assign', 'Assign Projects', 5),

    ('reports', 'reports', 'view',   'View Reports',   1),
    ('reports', 'reports', 'export', 'Export Reports', 2),
    ('reports', 'reports', 'import', 'Import Reports', 3),

    ('admin', 'config', 'view', 'View Configuration', 1),
    ('admin', 'config', 'edit', 'Edit Configuration', 2)
) AS v(module_key, resource, action, label, sort_order)
    ON m.key = v.module_key
ON CONFLICT (resource, action) DO NOTHING;

-- New resources for the 12 real modules the old catalog never covered.
INSERT INTO public.permission_actions (module_id, resource, action, label, sort_order)
SELECT m.id, v.resource, v.action, v.label, v.sort_order
FROM public.modules m
JOIN (VALUES
    ('dashboard', 'dashboard', 'view', 'View Dashboard', 1),

    ('organization', 'organization', 'view',      'View Organization',      1),
    ('organization', 'organization', 'edit',      'Edit Organization',      2),
    ('organization', 'organization', 'configure', 'Configure Organization', 3),

    ('attendance', 'attendance', 'view',    'View Attendance',    1),
    ('attendance', 'attendance', 'create',  'Create Attendance',  2),
    ('attendance', 'attendance', 'edit',    'Edit Attendance',    3),
    ('attendance', 'attendance', 'delete',  'Delete Attendance',  4),
    ('attendance', 'attendance', 'approve', 'Approve Attendance', 5),
    ('attendance', 'attendance', 'reject',  'Reject Attendance',  6),

    ('performance', 'performance', 'view',    'View Performance',    1),
    ('performance', 'performance', 'create',  'Create Performance',  2),
    ('performance', 'performance', 'edit',    'Edit Performance',    3),
    ('performance', 'performance', 'delete',  'Delete Performance',  4),
    ('performance', 'performance', 'approve', 'Approve Performance', 5),

    ('recruitment', 'recruitment', 'view',   'View Recruitment',   1),
    ('recruitment', 'recruitment', 'create', 'Create Recruitment', 2),
    ('recruitment', 'recruitment', 'edit',   'Edit Recruitment',   3),
    ('recruitment', 'recruitment', 'delete', 'Delete Recruitment', 4),
    ('recruitment', 'recruitment', 'manage', 'Manage Recruitment', 5),

    ('benefits', 'benefits', 'view',   'View Benefits',   1),
    ('benefits', 'benefits', 'create', 'Create Benefits', 2),
    ('benefits', 'benefits', 'edit',   'Edit Benefits',   3),
    ('benefits', 'benefits', 'delete', 'Delete Benefits', 4),
    ('benefits', 'benefits', 'manage', 'Manage Benefits', 5),

    ('learning', 'learning', 'view',   'View Learning',   1),
    ('learning', 'learning', 'create', 'Create Learning', 2),
    ('learning', 'learning', 'edit',   'Edit Learning',   3),
    ('learning', 'learning', 'delete', 'Delete Learning', 4),
    ('learning', 'learning', 'assign', 'Assign Learning', 5),

    ('work', 'work', 'view',    'View Work & Timesheets',    1),
    ('work', 'work', 'create',  'Create Work & Timesheets',  2),
    ('work', 'work', 'edit',    'Edit Work & Timesheets',    3),
    ('work', 'work', 'approve', 'Approve Work & Timesheets', 4),
    ('work', 'work', 'reject',  'Reject Work & Timesheets',  5),

    ('travel', 'travel', 'view',    'View Travel & Expenses',    1),
    ('travel', 'travel', 'create',  'Create Travel & Expenses',  2),
    ('travel', 'travel', 'edit',    'Edit Travel & Expenses',    3),
    ('travel', 'travel', 'approve', 'Approve Travel & Expenses', 4),
    ('travel', 'travel', 'reject',  'Reject Travel & Expenses',  5),

    ('assets', 'assets', 'view',   'View Assets',   1),
    ('assets', 'assets', 'create', 'Create Assets', 2),
    ('assets', 'assets', 'edit',   'Edit Assets',   3),
    ('assets', 'assets', 'delete', 'Delete Assets', 4),
    ('assets', 'assets', 'assign', 'Assign Assets', 5),
    ('assets', 'assets', 'manage', 'Manage Assets', 6),

    ('documents', 'documents', 'view',   'View Documents',   1),
    ('documents', 'documents', 'create', 'Create Documents', 2),
    ('documents', 'documents', 'delete', 'Delete Documents', 3),
    ('documents', 'documents', 'manage', 'Manage Documents', 4),

    ('approvals', 'approvals', 'view', 'View Approvals', 1),

    ('admin', 'admin', 'view',      'View Administration',      1),
    ('admin', 'admin', 'configure', 'Configure Administration', 2),
    ('admin', 'admin', 'manage',    'Manage Administration',    3)
) AS v(module_key, resource, action, label, sort_order)
    ON m.key = v.module_key
ON CONFLICT (resource, action) DO NOTHING;

-- ----------------------------------------------------------------------------
-- Verification queries to run afterward (read-only)
-- ----------------------------------------------------------------------------
-- SELECT m.key, m.name, count(*) FROM public.permission_actions pa
--   JOIN public.modules m ON m.id = pa.module_id
--   GROUP BY m.key, m.name ORDER BY m.key;
--   -- should show a row for all 18 modules.
--
-- SELECT m.key, pa.resource, pa.action, pa.label FROM public.permission_actions pa
--   JOIN public.modules m ON m.id = pa.module_id
--   ORDER BY m.sort_order, pa.sort_order;
