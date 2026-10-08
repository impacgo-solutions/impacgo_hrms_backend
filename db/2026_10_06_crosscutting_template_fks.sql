-- N-09: HRMS tenants provisioned before provision_tenant copied foreign keys
-- (impacgo-solutions: 16 missing -- exchange rates, HSN codes, item tables,
-- salary-structure assignment overrides and salary structures) get every
-- _template FK whose table and referenced table both exist in the tenant.
--
-- Idempotent: an FK already present (same constraint name on the same table)
-- is skipped. Orphan check first: for each missing single-column FK the rows
-- whose value has no parent are counted; a non-zero count is REPORTED via
-- NOTICE and that FK is skipped (never silently nulls business data).
-- Only HRMS schemas (pg_temp.hrms_schemas()), never _template itself.
DO $$
DECLARE
  s text;
  fk record;
  orphans bigint;
  col text;
  ref_col text;
  ref_schema text;
  def text;
  added int;
  skipped int;
BEGIN
  FOR s IN SELECT * FROM pg_temp.hrms_schemas() WHERE 1 = 1 LOOP
    CONTINUE WHEN s = '_template';
    IF to_regclass(format('%I.core_employees', s)) IS NULL THEN CONTINUE; END IF;
    added := 0; skipped := 0;
    FOR fk IN
      SELECT cl.relname AS tbl, k.conname, k.oid AS con_oid, k.conkey, k.confkey,
             rn.nspname AS ref_ns, rc.relname AS ref_table
      FROM pg_constraint k
      JOIN pg_class cl ON cl.oid = k.conrelid
      JOIN pg_namespace n ON n.oid = cl.relnamespace
      JOIN pg_class rc ON rc.oid = k.confrelid
      JOIN pg_namespace rn ON rn.oid = rc.relnamespace
      WHERE n.nspname = '_template' AND k.contype = 'f'
      ORDER BY cl.relname, k.conname
    LOOP
      -- table / referenced table must exist in the tenant
      IF to_regclass(format('%I.%I', s, fk.tbl)) IS NULL THEN CONTINUE; END IF;
      ref_schema := CASE WHEN fk.ref_ns = '_template' THEN s ELSE fk.ref_ns END;
      IF to_regclass(format('%I.%I', ref_schema, fk.ref_table)) IS NULL THEN CONTINUE; END IF;
      -- already there?
      IF EXISTS (
        SELECT 1 FROM pg_constraint k2
        JOIN pg_class c2 ON c2.oid = k2.conrelid
        JOIN pg_namespace n2 ON n2.oid = c2.relnamespace
        WHERE n2.nspname = s AND c2.relname = fk.tbl AND k2.conname = fk.conname
      ) THEN CONTINUE; END IF;
      IF array_length(fk.conkey, 1) <> 1 THEN
        RAISE NOTICE '%: skipped multi-column FK %.% (add manually)', s, fk.tbl, fk.conname;
        skipped := skipped + 1;
        CONTINUE;
      END IF;
      -- the FK column by NAME (attnums can differ between tenant and template)
      SELECT a.attname INTO col FROM pg_attribute a
        WHERE a.attrelid = format('%I.%I', s, fk.tbl)::regclass AND NOT a.attisdropped
          AND a.attname = (
            SELECT a0.attname FROM pg_attribute a0
            JOIN pg_class c0 ON c0.oid = a0.attrelid JOIN pg_namespace n0 ON n0.oid = c0.relnamespace
            WHERE n0.nspname = '_template' AND c0.relname = fk.tbl AND a0.attnum = fk.conkey[1]
          );
      SELECT a0.attname INTO ref_col FROM pg_attribute a0
        JOIN pg_class c0 ON c0.oid = a0.attrelid JOIN pg_namespace n0 ON n0.oid = c0.relnamespace
        WHERE n0.nspname = fk.ref_ns AND c0.relname = fk.ref_table AND a0.attnum = fk.confkey[1];
      IF col IS NULL OR ref_col IS NULL THEN
        RAISE NOTICE '%: skipped %.% (column missing in tenant)', s, fk.tbl, fk.conname;
        skipped := skipped + 1;
        CONTINUE;
      END IF;
      EXECUTE format(
        'SELECT count(*) FROM %I.%I t WHERE t.%I IS NOT NULL AND NOT EXISTS (SELECT 1 FROM %I.%I p WHERE p.%I = t.%I)',
        s, fk.tbl, col, ref_schema, fk.ref_table, ref_col, col
      ) INTO orphans;
      IF orphans > 0 THEN
        RAISE NOTICE '%: NOT adding %.% -- % orphan row(s) in %.% reference missing %.% rows',
          s, fk.tbl, fk.conname, orphans, fk.tbl, col, fk.ref_table, ref_col;
        skipped := skipped + 1;
        CONTINUE;
      END IF;
      def := pg_get_constraintdef(fk.con_oid);
      -- pg_get_constraintdef qualifies the reference only when it is not on
      -- the search_path; force the tenant schema either way.
      def := regexp_replace(def, 'REFERENCES\s+("?_template"?\.)?("?[A-Za-z0-9_]+"?)\(',
                            'REFERENCES ' || quote_ident(ref_schema) || '.' || quote_ident(fk.ref_table) || '(');
      EXECUTE format('ALTER TABLE %I.%I ADD CONSTRAINT %I %s', s, fk.tbl, fk.conname, def);
      added := added + 1;
      RAISE NOTICE '%: added %.%', s, fk.tbl, fk.conname;
    END LOOP;
    RAISE NOTICE 'done: % (added %, skipped %)', s, added, skipped;
  END LOOP;
END $$;
