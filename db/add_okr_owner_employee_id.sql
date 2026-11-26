-- =============================================================================
-- FE4 (audit 2026-09-24): OKR owners were stored only as free text
-- (hcm_company_okrs.owner_name), so the Performance screen could match goals
-- to people only by display name (two people with the same name merged; a
-- rename broke the link). Adds a real employee reference.
-- =============================================================================
-- Additive and idempotent: a NULLABLE owner_employee_id (FK -> core_employees)
-- on every tenant schema that has the table (plus _template, so new tenants
-- get it). owner_name is kept -- it stays the display text and the fallback
-- for rows that can't be linked.
--
-- Backfill links a row ONLY when its owner_name matches exactly one employee
-- of the same company (case/space-insensitive "first last"); ambiguous or
-- unknown names stay NULL -- nothing is guessed.
--
-- Applied to impacgo_test_dev on 2026-09-25 (user-approved).
-- =============================================================================

DO $$
DECLARE s text; n bigint;
BEGIN
  FOR s IN
    SELECT nspname FROM pg_namespace
    WHERE (nspname = '_template' OR (nspname NOT IN ('public', 'information_schema') AND nspname NOT LIKE 'pg\_%'))
      AND to_regclass(format('%I.hcm_company_okrs', nspname)) IS NOT NULL
      AND to_regclass(format('%I.core_employees', nspname)) IS NOT NULL
  LOOP
    EXECUTE format('ALTER TABLE %I.hcm_company_okrs ADD COLUMN IF NOT EXISTS owner_employee_id uuid', s);
    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint k JOIN pg_class t ON t.oid = k.conrelid JOIN pg_namespace ns ON ns.oid = t.relnamespace
      WHERE ns.nspname = s AND t.relname = 'hcm_company_okrs' AND k.conname = 'fk_okrs_owner_employee'
    ) THEN
      EXECUTE format('ALTER TABLE %1$I.hcm_company_okrs ADD CONSTRAINT fk_okrs_owner_employee
                      FOREIGN KEY (owner_employee_id) REFERENCES %1$I.core_employees (id)', s);
    END IF;
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_okrs_owner_employee ON %I.hcm_company_okrs (owner_employee_id)', s);
    EXECUTE format($q$
      UPDATE %1$I.hcm_company_okrs o SET owner_employee_id = m.employee_id
      FROM (
        SELECT o2.id AS okr_id, min(e.id::text)::uuid AS employee_id
        FROM %1$I.hcm_company_okrs o2
        JOIN %1$I.core_employees e
          ON e.company_id = o2.company_id
         AND lower(regexp_replace(trim(coalesce(e.first_name, '') || ' ' || coalesce(e.last_name, '')), '\s+', ' ', 'g'))
           = lower(regexp_replace(trim(o2.owner_name), '\s+', ' ', 'g'))
        WHERE o2.owner_employee_id IS NULL
        GROUP BY o2.id
        HAVING count(*) = 1
      ) m
      WHERE o.id = m.okr_id$q$, s);
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '% hcm_company_okrs linked to an employee: %', s, n;
  END LOOP;
END $$;

-- Verification:
-- SELECT owner_name, owner_employee_id FROM "<schema>".hcm_company_okrs;
