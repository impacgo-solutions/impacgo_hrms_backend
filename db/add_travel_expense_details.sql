-- Travel Requests and Expense Claims, full forms (ERP-style):
--
-- TRAVEL REQUEST
--   hcm_travel_requests        + travel_type ('Domestic' | 'International'), travel_funding,
--                                sponsor_details, contact_number, contact_email,
--                                id_document_type, id_document_number, passport_number,
--                                description, cost_center, project_id,
--                                organizer_name, organizer_address, event_details,
--                                submitted_at, created_at, updated_at
--   hcm_travel_itinerary       (new) one row per leg: from, to, mode, departure, arrival
--   hcm_travel_costing         (new) one row per expense type: sponsored / funded / total
--   Existing columns keep their meaning: purpose = Purpose of Travel; destination,
--   from_date, to_date, travel_mode and estimated_cost are derived from the itinerary
--   and costing so payroll reimbursement and the approvals inbox read them unchanged.
--
-- EXPENSE CLAIM
--   hcm_expense_claims         + expense_approver_id, posting_date, clearance_date, is_paid,
--                                payable_account, remark, project_id, cost_center, task_id,
--                                travel_request_id, total_claimed, total_taxes,
--                                advance_amount, submitted_at
--                                total_amount stays what payroll reimburses = the NET payable
--                                (claimed + taxes - advances applied).
--   hcm_expense_claim_lines    + claim_type_id, sort_order
--   hcm_expense_claim_taxes    (new) taxes and charges
--   hcm_employee_advances      (new) advance payments made to an employee
--   hcm_expense_claim_advances (new) advances applied against a claim
--   hcm_expense_claim_types    (new) the configurable Expense Claim Types (seeded with
--                                Calls, Food, Medical, Others, Travel)
--
-- 'draft' needs no schema change: status is a plain varchar with no CHECK.
-- Idempotent; every tenant schema + _template. Additive only: no existing row is
-- changed or removed.

CREATE OR REPLACE FUNCTION pg_temp.tre_apply(s text) RETURNS void AS $f$
BEGIN
    -- ── travel request header ───────────────────────────────────────────────
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_travel_requests
            ADD COLUMN IF NOT EXISTS travel_type varchar(20),
            ADD COLUMN IF NOT EXISTS travel_funding varchar(40),
            ADD COLUMN IF NOT EXISTS sponsor_details varchar(200),
            ADD COLUMN IF NOT EXISTS contact_number varchar(30),
            ADD COLUMN IF NOT EXISTS contact_email varchar(150),
            ADD COLUMN IF NOT EXISTS id_document_type varchar(40),
            ADD COLUMN IF NOT EXISTS id_document_number varchar(60),
            ADD COLUMN IF NOT EXISTS passport_number varchar(30),
            ADD COLUMN IF NOT EXISTS description text,
            ADD COLUMN IF NOT EXISTS cost_center varchar(100),
            ADD COLUMN IF NOT EXISTS project_id uuid,
            ADD COLUMN IF NOT EXISTS organizer_name varchar(150),
            ADD COLUMN IF NOT EXISTS organizer_address varchar(255),
            ADD COLUMN IF NOT EXISTS event_details text,
            ADD COLUMN IF NOT EXISTS submitted_at timestamptz,
            ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),
            ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()
    $q$, s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_travel_itinerary (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            travel_request_id uuid NOT NULL REFERENCES %1$I.hcm_travel_requests (id) ON DELETE CASCADE,
            sort_order smallint NOT NULL DEFAULT 0,
            travel_from varchar(150) NOT NULL,
            travel_to varchar(150) NOT NULL,
            mode_of_travel varchar(40) NOT NULL,
            departure_at timestamp NOT NULL,
            arrival_at timestamp,
            CONSTRAINT hcm_travel_itinerary_dates_check CHECK (arrival_at IS NULL OR arrival_at >= departure_at)
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS hcm_travel_itinerary_request_idx ON %1$I.hcm_travel_itinerary (travel_request_id)', s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_travel_costing (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            travel_request_id uuid NOT NULL REFERENCES %1$I.hcm_travel_requests (id) ON DELETE CASCADE,
            sort_order smallint NOT NULL DEFAULT 0,
            expense_type varchar(80) NOT NULL,
            comments varchar(255),
            sponsored_amount numeric(14,2) NOT NULL DEFAULT 0 CHECK (sponsored_amount >= 0),
            funded_amount numeric(14,2) NOT NULL DEFAULT 0 CHECK (funded_amount >= 0),
            total_amount numeric(14,2) NOT NULL DEFAULT 0 CHECK (total_amount >= 0)
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS hcm_travel_costing_request_idx ON %1$I.hcm_travel_costing (travel_request_id)', s);

    -- ── expense claim types ─────────────────────────────────────────────────
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_expense_claim_types (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL,
            name varchar(80) NOT NULL,
            description varchar(255),
            is_active boolean NOT NULL DEFAULT true,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    $q$, s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS hcm_expense_claim_types_name_uq ON %1$I.hcm_expense_claim_types (company_id, lower(name))', s);

    -- ── expense claim header / lines ────────────────────────────────────────
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_expense_claims
            ADD COLUMN IF NOT EXISTS expense_approver_id uuid,
            ADD COLUMN IF NOT EXISTS posting_date date,
            ADD COLUMN IF NOT EXISTS clearance_date date,
            ADD COLUMN IF NOT EXISTS is_paid boolean NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS payable_account varchar(120),
            ADD COLUMN IF NOT EXISTS remark text,
            ADD COLUMN IF NOT EXISTS project_id uuid,
            ADD COLUMN IF NOT EXISTS cost_center varchar(100),
            ADD COLUMN IF NOT EXISTS task_id uuid,
            ADD COLUMN IF NOT EXISTS travel_request_id uuid,
            ADD COLUMN IF NOT EXISTS total_claimed numeric(14,2) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS total_taxes numeric(14,2) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS advance_amount numeric(14,2) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS submitted_at timestamptz
    $q$, s);
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_expense_claim_lines
            ADD COLUMN IF NOT EXISTS claim_type_id uuid,
            ADD COLUMN IF NOT EXISTS sort_order smallint NOT NULL DEFAULT 0
    $q$, s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_expense_claim_taxes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            claim_id uuid NOT NULL REFERENCES %1$I.hcm_expense_claims (id) ON DELETE CASCADE,
            sort_order smallint NOT NULL DEFAULT 0,
            account_head varchar(120) NOT NULL,
            description varchar(255),
            rate numeric(7,3) CHECK (rate IS NULL OR (rate >= 0 AND rate <= 100)),
            tax_amount numeric(14,2) NOT NULL CHECK (tax_amount >= 0)
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS hcm_expense_claim_taxes_claim_idx ON %1$I.hcm_expense_claim_taxes (claim_id)', s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_employee_advances (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL,
            employee_id uuid NOT NULL REFERENCES %1$I.core_employees (id),
            posting_date date NOT NULL,
            purpose varchar(255),
            advance_amount numeric(14,2) NOT NULL CHECK (advance_amount > 0),
            claimed_amount numeric(14,2) NOT NULL DEFAULT 0 CHECK (claimed_amount >= 0),
            returned_amount numeric(14,2) NOT NULL DEFAULT 0 CHECK (returned_amount >= 0),
            status varchar(20) NOT NULL DEFAULT 'paid',
            travel_request_id uuid,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT hcm_employee_advances_balance_check CHECK (claimed_amount + returned_amount <= advance_amount)
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS hcm_employee_advances_employee_idx ON %1$I.hcm_employee_advances (employee_id)', s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_expense_claim_advances (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            claim_id uuid NOT NULL REFERENCES %1$I.hcm_expense_claims (id) ON DELETE CASCADE,
            advance_id uuid NOT NULL REFERENCES %1$I.hcm_employee_advances (id),
            allocated_amount numeric(14,2) NOT NULL CHECK (allocated_amount > 0),
            applied boolean NOT NULL DEFAULT false
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS hcm_expense_claim_advances_claim_idx ON %1$I.hcm_expense_claim_advances (claim_id)', s);

    -- Seed the default claim types for every company already in this schema.
    IF to_regclass(format('%I.core_companies', s)) IS NOT NULL THEN
        EXECUTE format($q$
            INSERT INTO %1$I.hcm_expense_claim_types (company_id, name)
            SELECT c.id, t.name
            FROM %1$I.core_companies c
            CROSS JOIN (VALUES ('Calls'), ('Food'), ('Medical'), ('Others'), ('Travel')) AS t(name)
            ON CONFLICT DO NOTHING
        $q$, s);
    END IF;
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.tre_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_expense_claims'
        )
        AND EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_travel_requests'
        )
    LOOP
        PERFORM pg_temp.tre_apply(tenant.slug);
    END LOOP;
END $$;
