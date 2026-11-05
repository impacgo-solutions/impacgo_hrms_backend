-- Impacgo Solutions only: Freelancer defaults for Add Employee
-- (Administration > General Settings > Freelancer Defaults shows / edits the
-- same row afterwards). Tenant DATA, not application logic -- no other
-- company is touched, and every value is resolved by name in that tenant's
-- own schema, never by a hard-coded id:
--
--   Band               10
--   Role               "Freelancer"
--   Reporting Manager  the one active employee named Srikar
--   Employment Status  Active
--   Rate               INR, per hour (changeable per freelancer)
--
-- Designation, Branch, Password and Date of Joining stay for the user to
-- choose. Run after add_freelancer_onboarding.sql; idempotent (re-running
-- refreshes the row). A value that can't be resolved is left NULL (the
-- form then simply doesn't pre-select it) and reported as a NOTICE.
--
--     cd backend && venv/Scripts/python db/run_hrms_migration.py db/impacgo_freelancer_defaults.sql

DO $$
DECLARE
    s constant text := 'impacgo-solutions';
    v_company uuid;
    v_band smallint;
    v_role text;
    v_manager uuid;
    v_count int;
BEGIN
    IF to_regclass(format('%I.hcm_freelancer_defaults', s)) IS NULL THEN
        RAISE NOTICE 'schema % has no hcm_freelancer_defaults -- run add_freelancer_onboarding.sql first; skipped', s;
        RETURN;
    END IF;

    EXECUTE format('SELECT id FROM %I.core_companies
                     WHERE name ILIKE ''Impacgo Solutions%%'' ORDER BY name LIMIT 1', s)
        INTO v_company;
    IF v_company IS NULL THEN
        RAISE NOTICE 'no Impacgo Solutions company in schema %; skipped', s;
        RETURN;
    END IF;

    EXECUTE format('SELECT band_number FROM %I.core_bands
                     WHERE company_id = $1 AND band_number = 10 AND is_active', s)
        INTO v_band USING v_company;

    EXECUTE format('SELECT name FROM %I.core_roles
                     WHERE company_id = $1 AND lower(name) = ''freelancer'' LIMIT 1', s)
        INTO v_role USING v_company;

    EXECUTE format('SELECT count(*) FROM %I.core_employees
                     WHERE company_id = $1 AND lower(status) = ''active''
                       AND (first_name ILIKE ''%%srikar%%'' OR last_name ILIKE ''%%srikar%%'')', s)
        INTO v_count USING v_company;
    IF v_count = 1 THEN
        EXECUTE format('SELECT id FROM %I.core_employees
                         WHERE company_id = $1 AND lower(status) = ''active''
                           AND (first_name ILIKE ''%%srikar%%'' OR last_name ILIKE ''%%srikar%%'')', s)
            INTO v_manager USING v_company;
    ELSE
        RAISE NOTICE '% active employees named Srikar -- Reporting Manager default left empty', v_count;
    END IF;

    IF v_band IS NULL THEN RAISE NOTICE 'Band 10 not found -- Band default left empty'; END IF;
    IF v_role IS NULL THEN RAISE NOTICE 'Role "Freelancer" not found -- Role default left empty'; END IF;

    EXECUTE format('INSERT INTO %I.hcm_freelancer_defaults
            (company_id, band_number, role_name, reporting_manager_id,
             employment_status, rate_currency, rate_unit, updated_at)
        VALUES ($1, $2, $3, $4, ''Active'', ''INR'', ''hourly'', now())
        ON CONFLICT (company_id) DO UPDATE SET
            band_number = EXCLUDED.band_number,
            role_name = EXCLUDED.role_name,
            reporting_manager_id = EXCLUDED.reporting_manager_id,
            employment_status = EXCLUDED.employment_status,
            rate_currency = EXCLUDED.rate_currency,
            rate_unit = EXCLUDED.rate_unit,
            updated_at = now()', s)
        USING v_company, v_band, v_role, v_manager;

    RAISE NOTICE 'Impacgo freelancer defaults: band=%, role=%, manager=%', v_band, v_role, v_manager;
END $$;
