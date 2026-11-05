-- Adds 'export'/'import' to the existing 'employee' resource in the
-- metadata-driven granular permission catalog (public.permission_actions,
-- see add_permission_actions_catalog.sql) -- the People Directory already
-- has a working client-side CSV Export button with no gate today; this is
-- what a company's People-access configuration will be able to grant or
-- restrict once tenant-configurable People access ships.
--
-- Global, not per-tenant (same as add_permission_actions_catalog.sql) --
-- one shared catalog every tenant's search_path already resolves into via
-- the trailing ", public".
--
-- NOT executed by the assistant -- review and run by hand, same convention
-- as every other backend/db/*.sql file.
--
-- Safe to re-run: ON CONFLICT (resource, action) DO NOTHING.

INSERT INTO public.permission_actions (module_id, resource, action, label, sort_order)
SELECT m.id, v.resource, v.action, v.label, v.sort_order
FROM public.modules m
JOIN (VALUES
    ('people', 'employee', 'export', 'Export Employees', 7),
    ('people', 'employee', 'import', 'Import Employees', 8)
) AS v(module_key, resource, action, label, sort_order)
    ON m.key = v.module_key
ON CONFLICT (resource, action) DO NOTHING;

-- ----------------------------------------------------------------------------
-- Verification query to run afterward (read-only)
-- ----------------------------------------------------------------------------
-- SELECT resource, action, label, sort_order FROM public.permission_actions
--   WHERE resource = 'employee' ORDER BY sort_order;
--   -- should now show 8 rows: view, create, edit, delete, transfer,
--   -- terminate, export, import.
