-- Unique request IDs + expense-wise receipts:
--
--   hcm_travel_requests.company_id   the request's company (from its employee)
--   hcm_travel_requests.request_no   'HR-TR-<year>-<5 digits>', sequential per
--                                    company and year -- the same shape as
--                                    Expense Reports' 'HR-EXP-<year>-NNNNN'
--   ux_travel_requests_request_no    no two requests of a company share one
--   ux_expense_claims_claim_no       no two expense reports of a company share one
--   hcm_expense_claim_lines.receipt_attachment_id
--                                    the receipt / invoice uploaded for that
--                                    one expense row (a core_attachments row
--                                    of the same claim); NULL = none
--
-- Existing travel requests are numbered once, oldest first. Existing expense
-- report numbers are kept as they are. Project IDs need no schema change:
-- they use core_number_series (doctype 'project'), created on first use.
--
-- Additive and idempotent; _template plus every HRMS tenant schema.
--
--     cd backend && venv/Scripts/python db/run_hrms_migration.py db/add_request_ids_and_expense_receipts.sql

DO $$
DECLARE
    s text;
    dups int;
    filed text;    -- when a request was filed: created_at, if the table has it
    filed_t text;  -- the same, qualified with the alias t
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        -- ── travel requests ───────────────────────────────────────────────
        IF to_regclass(format('%I.hcm_travel_requests', s)) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I.hcm_travel_requests
                ADD COLUMN IF NOT EXISTS company_id uuid,
                ADD COLUMN IF NOT EXISTS request_no varchar(30)', s);

            -- Older schemas may lack created_at: number those by id order.
            filed := CASE WHEN EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = s AND table_name = 'hcm_travel_requests' AND column_name = 'created_at')
                THEN 'created_at' ELSE 'NULL::timestamptz' END;
            filed_t := CASE WHEN filed = 'created_at' THEN 't.created_at' ELSE filed END;

            EXECUTE format('UPDATE %1$I.hcm_travel_requests t
                SET company_id = e.company_id
                FROM %1$I.core_employees e
                WHERE e.id = t.employee_id AND t.company_id IS NULL', s);

            -- Number the existing requests once: per company and year of
            -- filing, oldest first, continuing after any number already set.
            EXECUTE format($q$
                WITH base AS (
                    SELECT company_id, yr, COALESCE(MAX(NULLIF(split_part(request_no, '-', 4), '')::int), 0) AS top
                    FROM (
                        SELECT company_id,
                               EXTRACT(YEAR FROM COALESCE(%2$s, now()))::int AS yr,
                               request_no
                        FROM %1$I.hcm_travel_requests
                        WHERE company_id IS NOT NULL
                    ) x
                    WHERE request_no ~ '^HR-TR-[0-9]{4}-[0-9]+$' OR request_no IS NULL
                    GROUP BY company_id, yr
                ),
                todo AS (
                    SELECT t.id, t.company_id,
                           EXTRACT(YEAR FROM COALESCE(%3$s, now()))::int AS yr,
                           ROW_NUMBER() OVER (
                               PARTITION BY t.company_id, EXTRACT(YEAR FROM COALESCE(%3$s, now()))
                               ORDER BY %3$s NULLS FIRST, t.id) AS rn
                    FROM %1$I.hcm_travel_requests t
                    WHERE t.request_no IS NULL AND t.company_id IS NOT NULL
                )
                UPDATE %1$I.hcm_travel_requests t
                SET request_no = 'HR-TR-' || todo.yr || '-' || lpad((b.top + todo.rn)::text, 5, '0')
                FROM todo JOIN base b ON b.company_id = todo.company_id AND b.yr = todo.yr
                WHERE t.id = todo.id
            $q$, s, filed, filed_t);

            EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_travel_requests_request_no
                ON %I.hcm_travel_requests (company_id, request_no)
                WHERE request_no IS NOT NULL', s);
        END IF;

        -- ── expense reports ───────────────────────────────────────────────
        IF to_regclass(format('%I.hcm_expense_claims', s)) IS NOT NULL THEN
            EXECUTE format('SELECT count(*) FROM (
                SELECT 1 FROM %I.hcm_expense_claims GROUP BY company_id, claim_no HAVING count(*) > 1) d', s)
                INTO dups;
            IF dups = 0 THEN
                EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_expense_claims_claim_no
                    ON %I.hcm_expense_claims (company_id, claim_no)', s);
            ELSE
                RAISE NOTICE 'schema %: % duplicated expense report numbers -- unique index skipped', s, dups;
            END IF;
        END IF;

        IF to_regclass(format('%I.hcm_expense_claim_lines', s)) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %1$I.hcm_expense_claim_lines
                ADD COLUMN IF NOT EXISTS receipt_attachment_id uuid
                REFERENCES %1$I.core_attachments(id) ON DELETE SET NULL', s);
            EXECUTE format('CREATE INDEX IF NOT EXISTS ix_expense_claim_lines_receipt
                ON %I.hcm_expense_claim_lines (receipt_attachment_id)
                WHERE receipt_attachment_id IS NOT NULL', s);
        END IF;
    END LOOP;
END $$;
