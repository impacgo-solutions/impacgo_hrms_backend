-- =============================================================================
-- DB-02 / P9 (audit 2026-09-24): missing indexes on hot filter / FK columns
-- + DB-03 server-level notes + DB-04 ANALYZE
-- =============================================================================
-- NOT EXECUTED BY THE APP OR THE AUDIT. Run with psql OUTSIDE a transaction
-- (CREATE INDEX CONCURRENTLY cannot run in a transaction or DO block), so each
-- generated statement runs on its own via \gexec:
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f fix_2026_09_24_db02_indexes.sql
--
-- Every statement is IF NOT EXISTS and schema-qualified, generated once per
-- tenant schema (plus _template, so new tenants inherit them) and skipped
-- where the table doesn't exist in that schema.
-- If a CONCURRENTLY build fails it leaves an INVALID index: find it with the
-- check at the bottom, DROP INDEX CONCURRENTLY it, fix the cause, re-run.
-- =============================================================================

SELECT format(d.ddl, n.nspname)
FROM pg_namespace n
CROSS JOIN (VALUES
  -- PERF-01/02/03/07: per-project lookups (were 57k seq scans on 198 rows)
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pra_project_id ON %I.pm_resource_allocations (project_id)', 'pm_resource_allocations'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pra_employee_id ON %I.pm_resource_allocations (employee_id)', 'pm_resource_allocations'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pm_tasks_project_active ON %I.pm_tasks (project_id) WHERE is_active', 'pm_tasks'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pm_project_budgets_project ON %I.pm_project_budgets (project_id) WHERE is_active', 'pm_project_budgets'),
  -- attendance page (Seq Scan + top-N sort on acme)
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_att_company_date ON %I.hcm_attendance_records (company_id, attendance_date DESC, id DESC)', 'hcm_attendance_records'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_att_emp_date ON %I.hcm_attendance_records (employee_id, attendance_date)', 'hcm_attendance_records'),
  -- leave
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_leave_req_emp_dates ON %I.hcm_leave_requests (employee_id, from_date, to_date)', 'hcm_leave_requests'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_leave_req_company_from ON %I.hcm_leave_requests (company_id, from_date DESC)', 'hcm_leave_requests'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_leave_alloc_emp_type ON %I.hcm_leave_allocations (employee_id, leave_type_id)', 'hcm_leave_allocations'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_shift_assign_emp_from ON %I.hcm_shift_assignments (employee_id, from_date)', 'hcm_shift_assignments'),
  -- notifications (every unread-count poll) and audit log
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_notif_user_created ON %I.core_notifications (user_id, created_at DESC)', 'core_notifications'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_notif_user_unread ON %I.core_notifications (user_id) WHERE read_at IS NULL', 'core_notifications'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_company_created ON %I.core_audit_logs (company_id, created_at DESC)', 'core_audit_logs'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_document_id ON %I.core_audit_logs (document_id)', 'core_audit_logs'),
  -- payroll
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_salary_slips_emp ON %I.hcm_salary_slips (employee_id)', 'hcm_salary_slips'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_salary_slips_run ON %I.hcm_salary_slips (payroll_run_id)', 'hcm_salary_slips'),
  -- RBAC / attachments / documents
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_user_roles_role ON %I.core_user_roles (role_id)', 'core_user_roles'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_attach_entity ON %I.core_attachments (entity_type, entity_id)', 'core_attachments'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_doc_records_emp ON %I.hcm_document_records (employee_id)', 'hcm_document_records'),
  -- reporting-chain recursive CTE (crud._reporting_subtree_ids now joins a
  -- UNION ALL of the two manager columns, so each branch can use one of these)
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_emp_reporting_manager ON %I.core_employees (reporting_manager_id)', 'core_employees'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_emp_dotted_line_manager ON %I.core_employees (dotted_line_manager_id)', 'core_employees'),
  ('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_users_employee ON %I.core_users (employee_id)', 'core_users')
) AS d(ddl, tbl)
WHERE (n.nspname = '_template' OR (n.nspname NOT IN ('public', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'))
  AND to_regclass(format('%I.core_employees', n.nspname)) IS NOT NULL
  AND to_regclass(format('%I.%I', n.nspname, d.tbl)) IS NOT NULL
ORDER BY n.nspname \gexec

-- UNIQUE indexes (ux_att_emp_date, ux_leave_alloc, ux_users_email, ux_emp_code,
-- optional ux_pra_proj_emp) live in fix_2026_09_24_db01_orphans_and_constraints.sql
-- STEP 4, because they need the duplicate/orphan cleanup first.

-- -----------------------------------------------------------------------------
-- DB-04: refresh planner statistics (acme had never been ANALYZEd:
-- n_live_tup 1-7 vs 208 employees / 4,514 attendance rows). Cheap; safe online.
-- provision_tenant.py now runs ANALYZE on a new tenant schema automatically.
-- -----------------------------------------------------------------------------
SELECT format('ANALYZE %I.%I', schemaname, relname)
FROM pg_stat_user_tables
WHERE schemaname NOT IN ('public', 'information_schema')
ORDER BY schemaname, relname \gexec

-- -----------------------------------------------------------------------------
-- Verification
-- -----------------------------------------------------------------------------
SELECT n.nspname, c.relname AS invalid_index
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT i.indisvalid;

SELECT schemaname, relname, last_analyze, last_autoanalyze, n_live_tup
FROM pg_stat_user_tables
WHERE schemaname = 'acme' AND relname IN ('core_employees', 'hcm_attendance_records');

-- =============================================================================
-- DB-03: server-side safety timeouts + query visibility (DBA / superuser)
-- =============================================================================
-- The app now sets these PER CONNECTION itself (libpq startup options in
-- app/database.py; env DB_STATEMENT_TIMEOUT_MS=30000, DB_LOCK_TIMEOUT_MS=5000,
-- DB_IDLE_IN_TRANSACTION_TIMEOUT_MS=60000; 0 disables one). Role-level
-- defaults additionally protect every other client (scripts, psql):
--
--   ALTER ROLE <app_role> SET statement_timeout = '30s';
--   ALTER ROLE <app_role> SET lock_timeout = '5s';
--   ALTER ROLE <app_role> SET idle_in_transaction_session_timeout = '60s';
--
-- Query visibility (needs superuser + a server restart for the preload):
--   ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';
--   ALTER SYSTEM SET log_min_duration_statement = '500ms';
--   ALTER SYSTEM SET track_io_timing = on;
--   -- restart PostgreSQL, then in the app database:
--   CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
--
-- Capacity note: app pool = DB_POOL_SIZE (20) + DB_MAX_OVERFLOW (10) = 30
-- connections per worker process; keep workers * 30 <= max_connections (100)
-- minus headroom for other clients.
