-- H-21: manager / leader references must point at a real employee.
--
-- Self-referencing foreign keys, ON DELETE SET NULL, for
--   core_employees.reporting_manager_id, core_employees.dotted_line_manager_id,
--   core_branches.branch_manager_id,
--   core_departments.head_employee_id / hr_representative_id /
--   senior_manager_id / project_manager_id
-- in _template (so provision_tenant._copy_template_foreign_keys gives every
-- new tenant the same FKs) and every HRMS tenant.
--
-- Orphans first: a reference to an employee that doesn't exist, or that
-- belongs to ANOTHER company, is set to NULL (counted in the NOTICEs).
-- The FK can't express "same company"; that part is enforced by the API
-- (employee_validation.manager_error) on create and every update path.
-- Idempotent.

DO $$
DECLARE
  s text;
  r record;
  n int;
  fk text;
BEGIN
  FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
    IF to_regclass(format('%I.core_employees', s)) IS NULL THEN CONTINUE; END IF;
    FOR r IN SELECT * FROM (VALUES
        ('core_employees', 'reporting_manager_id'),
        ('core_employees', 'dotted_line_manager_id'),
        ('core_branches', 'branch_manager_id'),
        ('core_departments', 'head_employee_id'),
        ('core_departments', 'hr_representative_id'),
        ('core_departments', 'senior_manager_id'),
        ('core_departments', 'project_manager_id')
      ) AS v(tbl, col)
    LOOP
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = s AND table_name = r.tbl AND column_name = r.col) THEN
        CONTINUE;
      END IF;
      -- 1. orphans / cross-company references -> NULL
      EXECUTE format($q$
        UPDATE %1$I.%2$I t SET %3$I = NULL
        WHERE t.%3$I IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM %1$I.core_employees m WHERE m.id = t.%3$I AND m.company_id = t.company_id)
      $q$, s, r.tbl, r.col);
      GET DIAGNOSTICS n = ROW_COUNT;
      IF n > 0 THEN RAISE NOTICE '%: %.% -- % orphan reference(s) cleared', s, r.tbl, r.col, n; END IF;
      -- 2. FK (skip if any FK already covers this column)
      fk := format('fk_%s_%s', r.tbl, r.col);
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint k
        JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY (k.conkey)
        WHERE k.contype = 'f' AND k.conrelid = format('%I.%I', s, r.tbl)::regclass
          AND a.attname = r.col AND array_length(k.conkey, 1) = 1
      ) THEN
        EXECUTE format('ALTER TABLE %1$I.%2$I ADD CONSTRAINT %3$I FOREIGN KEY (%4$I) '
                       'REFERENCES %1$I.core_employees (id) ON DELETE SET NULL',
                       s, r.tbl, left(fk, 63), r.col);
        RAISE NOTICE '%: FK added on %.%', s, r.tbl, r.col;
      END IF;
    END LOOP;
  END LOOP;
END $$;
