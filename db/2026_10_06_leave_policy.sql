-- Leave QA fixes (N-05, M-07 / N-04) -- idempotent, HRMS schemas only.
--
-- N-05: Casual Leave was seeded with code 'CASUALLEAV' while the profile
--       reads 'CL'. Rename it to 'CL' wherever the company has no other
--       'CL' type yet (_template included, so new tenants get 'CL').
--       (Code no longer depends on the code anyway -- leave_policy.leave_slot.)
-- M-07: one allocation per employee / leave type / fiscal year -- unique
--       index, added only after a duplicate check (duplicates are reported,
--       never deleted, and the index is then skipped for that schema).
DO $$
DECLARE
    s text;
    n int;
BEGIN
    FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
        IF to_regclass(format('%I.hcm_leave_types', s)) IS NOT NULL THEN
            EXECUTE format(
                'UPDATE %I.hcm_leave_types t SET code = ''CL''
                  WHERE upper(t.code) = ''CASUALLEAV''
                    AND NOT EXISTS (SELECT 1 FROM %I.hcm_leave_types o
                                     WHERE o.company_id = t.company_id AND upper(o.code) = ''CL'')', s, s);
            GET DIAGNOSTICS n = ROW_COUNT;
            RAISE NOTICE '%: Casual Leave code -> CL on % row(s)', s, n;
        END IF;

        IF to_regclass(format('%I.hcm_leave_allocations', s)) IS NOT NULL THEN
            EXECUTE format(
                'SELECT count(*) FROM (SELECT 1 FROM %I.hcm_leave_allocations
                   GROUP BY employee_id, leave_type_id, fiscal_year_id HAVING count(*) > 1) d', s) INTO n;
            IF n > 0 THEN
                RAISE NOTICE '%: % duplicate allocation group(s) -- unique index NOT added, clean up first', s, n;
            ELSE
                EXECUTE format(
                    'CREATE UNIQUE INDEX IF NOT EXISTS uq_leave_alloc_emp_type_fy
                       ON %I.hcm_leave_allocations (employee_id, leave_type_id, fiscal_year_id)', s);
                RAISE NOTICE '%: unique allocation index ok', s;
            END IF;
        END IF;
    END LOOP;
END $$;
