-- A department can have several HR Representatives (one row per person).
-- core_departments.hr_representative_id stays as the primary (first) rep and
-- is kept in sync by the app (crud.set_department_hr_reps).
-- Idempotent; _template plus every provisioned tenant schema. Existing single
-- reps are backfilled as one row each.

DO $$
DECLARE
    s text;
BEGIN
    FOR s IN
        SELECT '_template'
        UNION ALL
        SELECT slug FROM public.tenants
    LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_schema = s AND table_name = 'core_departments') THEN
            CONTINUE;
        END IF;
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %1$I.core_department_hr_representatives (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
                department_id uuid NOT NULL REFERENCES %1$I.core_departments(id) ON DELETE CASCADE,
                employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
                created_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT core_department_hr_reps_unique UNIQUE (department_id, employee_id)
            )', s);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS ix_core_department_hr_reps_employee
                ON %1$I.core_department_hr_representatives (company_id, employee_id)', s);
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS ix_core_department_hr_reps_department
                ON %1$I.core_department_hr_representatives (department_id)', s);
        EXECUTE format(
            'INSERT INTO %1$I.core_department_hr_representatives (company_id, department_id, employee_id)
             SELECT d.company_id, d.id, d.hr_representative_id
               FROM %1$I.core_departments d
              WHERE d.hr_representative_id IS NOT NULL
             ON CONFLICT (department_id, employee_id) DO NOTHING', s);
        RAISE NOTICE 'core_department_hr_representatives ready for %', s;
    END LOOP;
END $$;
