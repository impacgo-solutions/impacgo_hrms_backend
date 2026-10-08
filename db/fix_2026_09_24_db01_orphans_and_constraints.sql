-- =============================================================================
-- DB-01 (audit 2026-09-24): orphan-row cleanup + missing FK / UNIQUE constraints
-- =============================================================================
-- NOT EXECUTED BY THE APP OR THE AUDIT. A DBA runs this with psql, per step,
-- against every tenant schema (plus _template, so newly provisioned tenants
-- inherit the constraints).
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f fix_2026_09_24_db01_orphans_and_constraints.sql
--
-- Steps 3 and 4 use psql's \gexec (a generator SELECT emits one statement per
-- tenant schema), because CREATE INDEX CONCURRENTLY cannot run inside a
-- transaction / DO block. Everything else is ordinary SQL.
--
-- Read-only orphan counts as re-verified on 2026-09-25 (before any change):
--   impacgo-solutions : core_user_roles -> missing core_users      = 4
--                       pm_time_entries -> missing pm_timesheets    = 1
--   acme              : core_users.employee_id -> missing employee  = 2
--   impacgo           : core_users.employee_id -> missing employee  = 1
--   Infyq             : duplicate pm_resource_allocations(project_id, employee_id) = 1 pair
--   everything else (dup attendance/day, dup leave allocation, dup lower(email),
--   dup employee_code, user_roles -> missing roles) = 0 in all 5 tenant schemas.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- STEP 0 -- which schemas are tenants (review this list before continuing)
-- -----------------------------------------------------------------------------
SELECT nspname AS tenant_schema
FROM pg_namespace
WHERE nspname NOT IN ('public', 'information_schema')
  AND nspname NOT LIKE 'pg\_%'
  AND to_regclass(format('%I.core_employees', nspname)) IS NOT NULL
ORDER BY 1;

-- -----------------------------------------------------------------------------
-- STEP 1 -- BACKUP the rows about to be removed / changed (keep the output!)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.db01_orphan_backup_20260924 (
    tenant_schema text        NOT NULL,
    table_name    text        NOT NULL,
    row_data      jsonb       NOT NULL,
    backed_up_at  timestamptz NOT NULL DEFAULT now()
);

DO $$
DECLARE s text;
BEGIN
  FOR s IN
    SELECT nspname FROM pg_namespace
    WHERE nspname NOT IN ('public', 'information_schema') AND nspname NOT LIKE 'pg\_%'
      AND to_regclass(format('%I.core_employees', nspname)) IS NOT NULL
  LOOP
    EXECUTE format($q$
      INSERT INTO public.db01_orphan_backup_20260924 (tenant_schema, table_name, row_data)
      SELECT %1$L, 'core_user_roles', to_jsonb(ur) FROM %1$I.core_user_roles ur
      WHERE NOT EXISTS (SELECT 1 FROM %1$I.core_users u WHERE u.id = ur.user_id)
         OR NOT EXISTS (SELECT 1 FROM %1$I.core_roles r WHERE r.id = ur.role_id)$q$, s);
    IF to_regclass(format('%I.pm_time_entries', s)) IS NOT NULL THEN
      EXECUTE format($q$
        INSERT INTO public.db01_orphan_backup_20260924 (tenant_schema, table_name, row_data)
        SELECT %1$L, 'pm_time_entries', to_jsonb(te) FROM %1$I.pm_time_entries te
        WHERE te.timesheet_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM %1$I.pm_timesheets t WHERE t.id = te.timesheet_id)$q$, s);
    END IF;
    EXECUTE format($q$
      INSERT INTO public.db01_orphan_backup_20260924 (tenant_schema, table_name, row_data)
      SELECT %1$L, 'core_users', to_jsonb(u) - 'password_hash' FROM %1$I.core_users u
      WHERE u.employee_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM %1$I.core_employees e WHERE e.id = u.employee_id)$q$, s);
  END LOOP;
END $$;

SELECT tenant_schema, table_name, count(*) FROM public.db01_orphan_backup_20260924 GROUP BY 1, 2 ORDER BY 1, 2;

-- -----------------------------------------------------------------------------
-- STEP 2 -- CLEAN the orphans (one transaction; review counts, then COMMIT)
-- -----------------------------------------------------------------------------
BEGIN;
DO $$
DECLARE s text; n bigint;
BEGIN
  FOR s IN
    SELECT nspname FROM pg_namespace
    WHERE nspname NOT IN ('public', 'information_schema') AND nspname NOT LIKE 'pg\_%'
      AND to_regclass(format('%I.core_employees', nspname)) IS NOT NULL
  LOOP
    -- role assignments of users that no longer exist (or of deleted roles)
    EXECUTE format($q$
      DELETE FROM %1$I.core_user_roles ur
      WHERE NOT EXISTS (SELECT 1 FROM %1$I.core_users u WHERE u.id = ur.user_id)
         OR NOT EXISTS (SELECT 1 FROM %1$I.core_roles r WHERE r.id = ur.role_id)$q$, s);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '% core_user_roles deleted: %', s, n;

    -- work entries pointing at a timesheet that no longer exists are NOT
    -- touched here: pm_time_entries.timesheet_id is NOT NULL, so they can't
    -- be detached, and deleting real logged work is a business decision
    -- (the rows are in the STEP 1 backup). fk_time_entries_timesheet is
    -- therefore added NOT VALID and only validated once they are resolved
    -- (see STEP 3b) -- it still enforces every new/updated row.
    IF to_regclass(format('%I.pm_time_entries', s)) IS NOT NULL THEN
      EXECUTE format($q$
        SELECT count(*) FROM %1$I.pm_time_entries te
        WHERE NOT EXISTS (SELECT 1 FROM %1$I.pm_timesheets t WHERE t.id = te.timesheet_id)$q$, s) INTO n;
      RAISE NOTICE '% pm_time_entries with a missing timesheet (left for review): %', s, n;
    END IF;

    -- login accounts linked to an employee row that no longer exists: unlink.
    -- (Deleting the account is the alternative -- decide per account; these
    -- users already cannot log in because login resolves the employee first.)
    EXECUTE format($q$
      UPDATE %1$I.core_users u SET employee_id = NULL
      WHERE u.employee_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM %1$I.core_employees e WHERE e.id = u.employee_id)$q$, s);
    GET DIAGNOSTICS n = ROW_COUNT; RAISE NOTICE '% core_users unlinked: %', s, n;
  END LOOP;
END $$;
-- Expected NOTICEs (2026-09-25): impacgo-solutions user_roles 4 (and 1 time
-- entry with a missing timesheet, left for review); acme core_users 2;
-- impacgo core_users 1; all others 0.
COMMIT;   -- or ROLLBACK if the counts differ from what you reviewed

-- -----------------------------------------------------------------------------
-- STEP 3 -- FOREIGN KEYS: add NOT VALID (instant, no full-table lock wait),
-- then VALIDATE (SHARE UPDATE EXCLUSIVE; reads/writes continue). Idempotent:
-- skips a constraint that already exists or whose tables/columns are missing.
-- -----------------------------------------------------------------------------
CREATE TEMP TABLE db01_fk_spec (child text, col text, parent text, conname text) ON COMMIT PRESERVE ROWS;
INSERT INTO db01_fk_spec VALUES
  ('core_user_roles',         'user_id',        'core_users',        'fk_user_roles_user'),
  ('core_user_roles',         'role_id',        'core_roles',        'fk_user_roles_role'),
  ('core_users',              'employee_id',    'core_employees',    'fk_users_employee'),
  ('pm_time_entries',         'timesheet_id',   'pm_timesheets',     'fk_time_entries_timesheet'),
  ('hcm_leave_requests',      'employee_id',    'core_employees',    'fk_leave_req_employee'),
  ('hcm_leave_requests',      'leave_type_id',  'hcm_leave_types',   'fk_leave_req_type'),
  ('hcm_leave_allocations',   'employee_id',    'core_employees',    'fk_leave_alloc_employee'),
  ('hcm_attendance_records',  'employee_id',    'core_employees',    'fk_att_employee'),
  ('pm_resource_allocations', 'project_id',     'pm_projects',       'fk_pra_project'),
  ('pm_resource_allocations', 'employee_id',    'core_employees',    'fk_pra_employee'),
  ('core_notifications',      'user_id',        'core_users',        'fk_notifications_user'),
  ('hcm_salary_slips',        'employee_id',    'core_employees',    'fk_salary_slips_employee'),
  ('hcm_salary_slips',        'payroll_run_id', 'hcm_payroll_runs',  'fk_salary_slips_run');

-- 3a: ADD ... NOT VALID
SELECT format('ALTER TABLE %I.%I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES %I.%I (id) NOT VALID',
              n.nspname, f.child, f.conname, f.col, n.nspname, f.parent)
FROM pg_namespace n CROSS JOIN db01_fk_spec f
WHERE (n.nspname = '_template' OR (n.nspname NOT IN ('public', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'))
  AND to_regclass(format('%I.core_employees', n.nspname)) IS NOT NULL
  AND to_regclass(format('%I.%I', n.nspname, f.child)) IS NOT NULL
  AND to_regclass(format('%I.%I', n.nspname, f.parent)) IS NOT NULL
  AND EXISTS (SELECT 1 FROM information_schema.columns c
              WHERE c.table_schema = n.nspname AND c.table_name = f.child AND c.column_name = f.col)
  AND NOT EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_class t ON t.oid = k.conrelid
                  WHERE t.relnamespace = n.oid AND t.relname = f.child AND k.contype = 'f'
                    AND (k.conname = f.conname
                         OR k.conkey = ARRAY[(SELECT attnum FROM pg_attribute
                                               WHERE attrelid = t.oid AND attname = f.col)]::int2[]))
ORDER BY n.nspname, f.child \gexec

-- 3b: VALIDATE (fails loudly, naming the constraint, if any orphan remains)
SELECT format('ALTER TABLE %I.%I VALIDATE CONSTRAINT %I', n.nspname, t.relname, k.conname)
FROM pg_constraint k
JOIN pg_class t ON t.oid = k.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE k.contype = 'f' AND NOT k.convalidated
  AND k.conname IN (SELECT conname FROM db01_fk_spec)
  AND k.conname <> 'fk_time_entries_timesheet'   -- see 3c
ORDER BY n.nspname, t.relname \gexec

-- 3c: fk_time_entries_timesheet is validated only in schemas with no time
-- entry whose timesheet is gone (those rows need a business decision first,
-- see STEP 2). Until then it stays NOT VALID -- still enforced for new rows.
DO $$
DECLARE r record; orphans bigint;
BEGIN
  FOR r IN
    SELECT n.nspname FROM pg_constraint k
    JOIN pg_class t ON t.oid = k.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE k.conname = 'fk_time_entries_timesheet' AND NOT k.convalidated
  LOOP
    EXECUTE format('SELECT count(*) FROM %1$I.pm_time_entries te WHERE NOT EXISTS
                    (SELECT 1 FROM %1$I.pm_timesheets t WHERE t.id = te.timesheet_id)', r.nspname) INTO orphans;
    IF orphans = 0 THEN
      EXECUTE format('ALTER TABLE %I.pm_time_entries VALIDATE CONSTRAINT fk_time_entries_timesheet', r.nspname);
    ELSE
      RAISE NOTICE '% fk_time_entries_timesheet left NOT VALID: % orphan time entr(ies)', r.nspname, orphans;
    END IF;
  END LOOP;
END $$;

-- -----------------------------------------------------------------------------
-- STEP 4 -- UNIQUE indexes (CONCURRENTLY; run outside a transaction).
-- Pre-check: every duplicate count above must be 0 for that schema, else the
-- index build fails and leaves an INVALID index (DROP INDEX CONCURRENTLY it).
-- -----------------------------------------------------------------------------
SELECT format(ddl, n.nspname)
FROM pg_namespace n
CROSS JOIN (VALUES
  ('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_att_emp_date   ON %I.hcm_attendance_records (employee_id, attendance_date)', 'hcm_attendance_records'),
  ('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_leave_alloc    ON %I.hcm_leave_allocations (employee_id, leave_type_id, fiscal_year_id)', 'hcm_leave_allocations'),
  ('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_users_email    ON %I.core_users (lower(email))', 'core_users'),
  ('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_emp_code       ON %I.core_employees (company_id, employee_code)', 'core_employees')
) AS d(ddl, tbl)
WHERE (n.nspname = '_template' OR (n.nspname NOT IN ('public', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'))
  AND to_regclass(format('%I.core_employees', n.nspname)) IS NOT NULL
  AND to_regclass(format('%I.%I', n.nspname, d.tbl)) IS NOT NULL
ORDER BY n.nspname \gexec

-- ONLY if "one allocation per project+employee" is the business rule.
-- Infyq currently has 1 duplicate pair -- resolve it first:
--   SELECT project_id, employee_id, array_agg(id) FROM "Infyq".pm_resource_allocations
--   GROUP BY 1, 2 HAVING count(*) > 1;
-- SELECT format('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_pra_proj_emp ON %I.pm_resource_allocations (project_id, employee_id)', nspname)
-- FROM pg_namespace WHERE to_regclass(format('%I.pm_resource_allocations', nspname)) IS NOT NULL \gexec

-- -----------------------------------------------------------------------------
-- STEP 5 -- verify: no INVALID indexes left behind, all FKs validated
-- -----------------------------------------------------------------------------
SELECT n.nspname, c.relname AS invalid_index
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT i.indisvalid;

SELECT n.nspname, t.relname, k.conname, k.convalidated
FROM pg_constraint k JOIN pg_class t ON t.oid = k.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE k.conname IN (SELECT conname FROM db01_fk_spec)
ORDER BY 1, 2, 3;
