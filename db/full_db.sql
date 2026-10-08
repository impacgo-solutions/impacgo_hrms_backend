--
-- PostgreSQL database dump
--

-- Dumped from database version 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 17.5

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: _template; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA _template;


--
-- Name: acme; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA acme;


--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: add_module_to_tenant(text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.add_module_to_tenant(tenant_slug text, module_code text) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    tbl record;
BEGIN
    FOR tbl IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = '_template'
          AND split_part(tablename, '_', 1) = module_code
        ORDER BY tablename
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I.%I (LIKE _template.%I INCLUDING ALL)',
            tenant_slug, tbl.tablename, tbl.tablename
        );
    END LOOP;

    INSERT INTO public.tenant_modules (tenant_slug, module_code, is_enabled)
    VALUES (tenant_slug, module_code, true)
    ON CONFLICT (tenant_slug, module_code)
    DO UPDATE SET is_enabled = true, enabled_at = now();

    RAISE NOTICE 'Module % added to tenant %', module_code, tenant_slug;
END;
$$;


--
-- Name: disable_module_for_tenant(text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.disable_module_for_tenant(tenant_slug text, module_code text) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE public.tenant_modules tm
    SET is_enabled = false
    WHERE tm.tenant_slug = disable_module_for_tenant.tenant_slug
      AND tm.module_code = disable_module_for_tenant.module_code;

    RAISE NOTICE 'Module % disabled for tenant % (tables retained)', module_code, tenant_slug;
END;
$$;


--
-- Name: migrate_add_column(text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.migrate_add_column(p_table text, p_column_def text) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    s          text;
    col_name   text;
BEGIN
    -- extract column name (first word of column_def)
    col_name := split_part(trim(p_column_def), ' ', 1);

    FOR s IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('information_schema')
          AND schema_name NOT LIKE 'pg_%'
          AND (
              schema_name = '_template'
              OR schema_name IN (SELECT slug FROM public.tenants)
          )
        ORDER BY schema_name
    LOOP
        -- skip if column already exists in this schema
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = s
              AND table_name   = p_table
              AND column_name  = col_name
        ) THEN
            EXECUTE format('ALTER TABLE %I.%I ADD COLUMN %s', s, p_table, p_column_def);
            RAISE NOTICE '  [%] ALTER TABLE %.% ADD COLUMN %', s, s, p_table, col_name;
        ELSE
            RAISE NOTICE '  [%] SKIP — column %.%.% already exists', s, s, p_table, col_name;
        END IF;
    END LOOP;
END;
$$;


--
-- Name: migrate_add_table(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.migrate_add_table(p_table text) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    s text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_tables
        WHERE schemaname = '_template' AND tablename = p_table
    ) THEN
        RAISE EXCEPTION 'Table _template.% does not exist. Create it there first.', p_table;
    END IF;

    FOR s IN
        SELECT slug FROM public.tenants WHERE is_active = true
        ORDER BY slug
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_tables
            WHERE schemaname = s AND tablename = p_table
        ) THEN
            EXECUTE format(
                'CREATE TABLE %I.%I (LIKE _template.%I INCLUDING ALL)',
                s, p_table, p_table
            );
            RAISE NOTICE '  [%] CREATE TABLE %.%', s, s, p_table;
        ELSE
            RAISE NOTICE '  [%] SKIP — %.% already exists', s, s, p_table;
        END IF;
    END LOOP;
END;
$$;


--
-- Name: provision_tenant(text, text[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.provision_tenant(tenant_slug text, enabled_modules text[]) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    tbl           record;
    module_prefix text;
BEGIN
    EXECUTE format('CREATE SCHEMA %I', tenant_slug);

    FOR tbl IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = '_template'
        ORDER BY tablename
    LOOP
        module_prefix := split_part(tbl.tablename, '_', 1);
        IF module_prefix = ANY(enabled_modules) THEN
            EXECUTE format(
                'CREATE TABLE %I.%I (LIKE _template.%I INCLUDING ALL)',
                tenant_slug, tbl.tablename, tbl.tablename
            );
        END IF;
    END LOOP;

    INSERT INTO public.tenant_modules (tenant_slug, module_code, is_enabled)
    SELECT tenant_slug, m, true FROM unnest(enabled_modules) AS m;

    RAISE NOTICE 'Tenant % provisioned with modules: %', tenant_slug, enabled_modules;
END;
$$;


--
-- Name: run_migration(text, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.run_migration(p_version text, p_name text, p_sql text) RETURNS void
    LANGUAGE plpgsql
    AS $$
DECLARE
    t_start  timestamptz;
    t_ms     integer;
BEGIN
    -- idempotency check
    IF EXISTS (
        SELECT 1 FROM public.schema_migrations WHERE version = p_version
    ) THEN
        RAISE NOTICE 'Migration % already applied — skipping.', p_version;
        RETURN;
    END IF;

    RAISE NOTICE 'Running migration % : %', p_version, p_name;
    t_start := clock_timestamp();

    EXECUTE p_sql;

    t_ms := extract(epoch FROM (clock_timestamp() - t_start)) * 1000;

    INSERT INTO public.schema_migrations (version, name, execution_ms)
    VALUES (p_version, p_name, t_ms);

    RAISE NOTICE 'Migration % done in % ms.', p_version, t_ms;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: core_activity_logs; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_activity_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(60) NOT NULL,
    entity_id uuid NOT NULL,
    user_id uuid,
    activity_type character varying(40) NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_addresses; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_addresses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(30) NOT NULL,
    entity_id uuid NOT NULL,
    address_type character varying(15) DEFAULT 'billing'::character varying NOT NULL,
    line1 character varying(200) NOT NULL,
    line2 character varying(200),
    city character varying(80),
    state character varying(80),
    state_code character varying(2),
    pincode character varying(10),
    country character varying(80) DEFAULT 'India'::character varying NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    is_permanent boolean DEFAULT false NOT NULL
);


--
-- Name: core_approval_actions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_approval_actions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    request_id uuid NOT NULL,
    step_order smallint NOT NULL,
    actor_id uuid NOT NULL,
    action character varying(15) NOT NULL,
    comments text,
    acted_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_approval_requests; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_approval_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    workflow_id uuid NOT NULL,
    doctype character varying(60) NOT NULL,
    document_id uuid NOT NULL,
    status character varying(15) DEFAULT 'pending'::character varying NOT NULL,
    current_step smallint DEFAULT 1 NOT NULL,
    requested_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_approval_workflow_steps; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_approval_workflow_steps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workflow_id uuid NOT NULL,
    step_order smallint NOT NULL,
    approver_type character varying(20) NOT NULL,
    role_id uuid,
    user_id uuid,
    min_amount numeric(18,2),
    max_amount numeric(18,2)
);


--
-- Name: core_approval_workflows; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_approval_workflows (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    doctype character varying(60) NOT NULL,
    name character varying(120) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_attachments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_attachments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(60) NOT NULL,
    entity_id uuid NOT NULL,
    file_name character varying(255) NOT NULL,
    file_url text NOT NULL,
    mime_type character varying(100),
    size_bytes bigint,
    uploaded_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_audit_logs; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_audit_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    user_id uuid,
    action character varying(20) NOT NULL,
    doctype character varying(60) NOT NULL,
    document_id uuid,
    changes jsonb,
    ip_address inet,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_bands; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_bands (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    code character varying(20) NOT NULL,
    band_number integer NOT NULL,
    description text,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_branches; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_branches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    gstin character varying(15),
    is_head_office boolean DEFAULT false NOT NULL,
    address_line1 character varying(200),
    city character varying(80),
    state character varying(80),
    pincode character varying(10),
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    country character varying(80) DEFAULT 'India'::character varying NOT NULL,
    tz character varying(60) DEFAULT 'IST (UTC+5:30)'::character varying NOT NULL,
    branch_manager_id uuid
);


--
-- Name: core_business_units; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_business_units (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    head_employee_id uuid,
    cost_center character varying(20),
    is_active boolean DEFAULT true NOT NULL,
    branch_id uuid
);


--
-- Name: core_comments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_comments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(60) NOT NULL,
    entity_id uuid NOT NULL,
    user_id uuid NOT NULL,
    parent_id uuid,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_companies; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_companies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(150) NOT NULL,
    legal_name character varying(200),
    gstin character varying(15),
    pan character varying(10),
    cin character varying(25),
    default_currency_id uuid NOT NULL,
    fiscal_year_start_month smallint DEFAULT 4 NOT NULL,
    address_line1 character varying(200),
    address_line2 character varying(200),
    city character varying(80),
    state character varying(80),
    pincode character varying(10),
    country character varying(80) DEFAULT 'India'::character varying,
    logo_url text,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    industry character varying(100),
    founded_date date,
    email_domain character varying(150),
    branch_head_id uuid,
    company_group_id uuid,
    is_consolidation_only boolean DEFAULT false NOT NULL,
    gsp_provider character varying DEFAULT 'mock'::character varying NOT NULL,
    code character varying(30)
);


--
-- Name: core_company_groups; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_company_groups (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_company_menu_items; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_company_menu_items (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    menu_item_id uuid NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL
);


--
-- Name: core_company_modules; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_company_modules (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    module_id uuid NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_company_settings; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_company_settings (
    company_id uuid NOT NULL,
    default_currency character varying(10) DEFAULT 'INR'::character varying NOT NULL,
    default_timezone character varying(60) DEFAULT 'IST (UTC+5:30)'::character varying NOT NULL,
    working_days_per_week smallint DEFAULT 5 NOT NULL,
    enable_branch_level boolean DEFAULT true NOT NULL,
    enable_business_unit_level boolean DEFAULT true NOT NULL,
    enable_department_level boolean DEFAULT true NOT NULL,
    enable_sub_department_level boolean DEFAULT true NOT NULL,
    working_hours_start time without time zone,
    working_hours_end time without time zone,
    probation_period_days smallint DEFAULT 90 NOT NULL,
    notice_period_days smallint DEFAULT 30 NOT NULL
);


--
-- Name: core_contacts; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_contacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(30) NOT NULL,
    entity_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    designation character varying(80),
    email character varying(150),
    phone character varying(20),
    is_primary boolean DEFAULT false NOT NULL,
    is_emergency boolean DEFAULT false NOT NULL
);


--
-- Name: core_customers; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_customers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(200) NOT NULL,
    customer_type character varying(10) DEFAULT 'b2b'::character varying NOT NULL,
    gstin character varying(15),
    pan character varying(10),
    credit_limit numeric(18,2) DEFAULT 0 NOT NULL,
    credit_days integer DEFAULT 0 NOT NULL,
    receivable_account_id uuid,
    default_currency_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    default_tax_template_id uuid
);


--
-- Name: core_departments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_departments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    parent_id uuid,
    head_employee_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    business_unit_id uuid,
    annual_budget numeric(14,2),
    hr_representative_id uuid,
    senior_manager_id uuid,
    project_manager_id uuid
);


--
-- Name: core_designation_permissions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_designation_permissions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    designation_id uuid NOT NULL,
    permission_id uuid NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_designations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_designations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    band character varying(40),
    is_active boolean DEFAULT true NOT NULL,
    role_id uuid,
    band_id uuid
);


--
-- Name: core_employees; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_employees (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    department_id uuid,
    designation_id uuid,
    employee_code character varying(20) NOT NULL,
    first_name character varying(80) NOT NULL,
    last_name character varying(80),
    work_email character varying(150),
    date_of_birth date,
    gender character varying(15),
    date_of_joining date NOT NULL,
    employment_type character varying(20) DEFAULT 'full_time'::character varying NOT NULL,
    reporting_manager_id uuid,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    pan character varying(10),
    uan character varying(12),
    esi_number character varying(17),
    bank_name character varying(100),
    bank_account_no character varying(30),
    bank_ifsc character varying(11),
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    sub_department_id uuid,
    team character varying(150),
    work_mode character varying(20),
    confirmation_date date,
    probation_end_date date,
    dotted_line_manager_id uuid,
    band integer,
    annual_ctc bigint,
    blood_group character varying(5),
    nationality character varying(50),
    marital_status character varying(20),
    personal_email character varying(150),
    personal_phone character varying(20),
    current_address text,
    permanent_address text,
    tax_regime character varying(10)
);


--
-- Name: core_field_rules; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_field_rules (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    entity_key character varying(40) NOT NULL,
    field_key character varying(60) NOT NULL,
    state character varying(10) NOT NULL,
    role_id uuid,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT core_field_rules_state_check CHECK (((state)::text = ANY (ARRAY['mandatory'::text, 'optional'::text, 'hidden'::text, 'readonly'::text])))
);


--
-- Name: core_hierarchy_role_bindings; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_hierarchy_role_bindings (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    rung_key character varying(30) NOT NULL,
    role_id uuid NOT NULL,
    CONSTRAINT core_hrb_rung_check CHECK (((rung_key)::text = ANY (ARRAY['team_lead'::text, 'project_manager'::text, 'senior_manager'::text, 'branch_manager'::text, 'branch_head'::text])))
);


--
-- Name: core_integrations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_integrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    description character varying(200),
    is_connected boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_intercompany_relationships; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_intercompany_relationships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_group_id uuid NOT NULL,
    company_id uuid NOT NULL,
    counterparty_company_id uuid NOT NULL,
    due_from_account_id uuid,
    due_to_account_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_item_groups; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_item_groups (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    parent_id uuid,
    income_account_id uuid,
    expense_account_id uuid
);


--
-- Name: core_items; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(40) NOT NULL,
    name character varying(200) NOT NULL,
    item_group_id uuid,
    item_type character varying(15) DEFAULT 'goods'::character varying NOT NULL,
    uom_id uuid NOT NULL,
    hsn_sac_code character varying(10),
    sales_tax_template_id uuid,
    purchase_tax_template_id uuid,
    is_stock_item boolean DEFAULT true NOT NULL,
    is_sales_item boolean DEFAULT true NOT NULL,
    is_purchase_item boolean DEFAULT true NOT NULL,
    is_manufactured boolean DEFAULT false NOT NULL,
    has_batch boolean DEFAULT false NOT NULL,
    has_serial boolean DEFAULT false NOT NULL,
    valuation_method character varying(10) DEFAULT 'fifo'::character varying NOT NULL,
    standard_selling_rate numeric(18,2),
    standard_buying_rate numeric(18,2),
    reorder_level numeric(18,3) DEFAULT 0,
    reorder_qty numeric(18,3) DEFAULT 0,
    barcode character varying(60),
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_notification_preferences; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_notification_preferences (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    event_key character varying(80) NOT NULL,
    channel character varying(10) NOT NULL,
    enabled boolean DEFAULT true NOT NULL
);


--
-- Name: core_notifications; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_notifications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    body text,
    entity_type character varying(60),
    entity_id uuid,
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_number_series; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_number_series (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    doctype character varying(60) NOT NULL,
    prefix character varying(20) NOT NULL,
    fiscal_infix boolean DEFAULT true NOT NULL,
    padding smallint DEFAULT 5 NOT NULL,
    current_no bigint DEFAULT 0 NOT NULL,
    suffix character varying(20) DEFAULT ''::character varying NOT NULL
);


--
-- Name: core_permissions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_permissions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code character varying(120) NOT NULL,
    module character varying(20) NOT NULL,
    resource character varying(60) NOT NULL,
    action character varying(30) NOT NULL
);


--
-- Name: core_role_permissions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_role_permissions (
    role_id uuid NOT NULL,
    permission_id uuid NOT NULL
);


--
-- Name: core_roles; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_roles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    description text,
    is_system boolean DEFAULT false NOT NULL,
    band_id uuid
);


--
-- Name: core_sub_departments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_sub_departments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    department_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_suppliers; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_suppliers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(200) NOT NULL,
    gstin character varying(15),
    pan character varying(10),
    payment_terms_days integer DEFAULT 0 NOT NULL,
    payable_account_id uuid,
    default_currency_id uuid,
    is_msme boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    default_tax_template_id uuid,
    default_tds_section_id uuid,
    pan_verified boolean DEFAULT false NOT NULL
);


--
-- Name: core_tax_template_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_tax_template_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tax_template_id uuid NOT NULL,
    tax_id uuid NOT NULL
);


--
-- Name: core_tax_templates; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_tax_templates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(60) NOT NULL,
    applies_to character varying(10) DEFAULT 'both'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_taxes; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_taxes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(60) NOT NULL,
    tax_type character varying(15) NOT NULL,
    rate numeric(7,3) NOT NULL,
    account_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_tds_deductions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_tds_deductions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    purchase_invoice_id uuid NOT NULL,
    supplier_id uuid NOT NULL,
    tds_section_id uuid NOT NULL,
    taxable_amount numeric NOT NULL,
    tds_amount numeric NOT NULL,
    deducted_at timestamp with time zone DEFAULT now() NOT NULL,
    certificate_no character varying,
    journal_entry_id uuid,
    status character varying DEFAULT 'deducted'::character varying NOT NULL
);


--
-- Name: core_tds_sections; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_tds_sections (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    section_code character varying NOT NULL,
    description character varying NOT NULL,
    rate numeric NOT NULL,
    threshold_amount numeric,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_uom_conversions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_uom_conversions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    item_id uuid,
    from_uom_id uuid NOT NULL,
    to_uom_id uuid NOT NULL,
    factor numeric(18,6) NOT NULL
);


--
-- Name: core_uoms; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_uoms (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(15) NOT NULL,
    name character varying(60) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_user_roles; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_user_roles (
    user_id uuid NOT NULL,
    role_id uuid NOT NULL,
    branch_id uuid
);


--
-- Name: core_users; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    email character varying(150) NOT NULL,
    phone character varying(20),
    password_hash text NOT NULL,
    employee_id uuid,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    last_login_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    full_name character varying(150)
);


--
-- Name: core_warehouses; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.core_warehouses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    warehouse_type character varying(20) DEFAULT 'stores'::character varying NOT NULL,
    parent_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_accounting_periods; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_accounting_periods (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    fiscal_year_id uuid NOT NULL,
    name character varying(20) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    status character varying(10) DEFAULT 'open'::character varying NOT NULL
);


--
-- Name: fin_accounts; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(150) NOT NULL,
    parent_id uuid,
    is_group boolean DEFAULT false NOT NULL,
    root_type character varying(12) NOT NULL,
    account_type character varying(30),
    currency_id uuid,
    is_frozen boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_allocation_rule_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_allocation_rule_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    allocation_rule_id uuid NOT NULL,
    line_no smallint NOT NULL,
    destination_account_id uuid NOT NULL,
    cost_center_id uuid,
    department_id uuid,
    percentage numeric(5,2) NOT NULL,
    CONSTRAINT fin_allocation_rule_lines_pct_chk CHECK (((percentage > (0)::numeric) AND (percentage <= (100)::numeric)))
);


--
-- Name: fin_allocation_rules; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_allocation_rules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    source_account_id uuid NOT NULL,
    remarks text,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_asset_categories; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_asset_categories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    depreciation_method character varying(15) DEFAULT 'slm'::character varying NOT NULL,
    useful_life_months integer NOT NULL,
    depreciation_rate numeric(7,3),
    asset_account_id uuid,
    depreciation_expense_account_id uuid,
    accumulated_depreciation_account_id uuid,
    disposal_gain_loss_account_id uuid
);


--
-- Name: fin_asset_category_books; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_asset_category_books (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    category_id uuid NOT NULL,
    book_id uuid NOT NULL,
    depreciation_method character varying DEFAULT 'slm'::character varying NOT NULL,
    useful_life_months integer NOT NULL,
    depreciation_rate numeric,
    depreciation_expense_account_id uuid,
    accumulated_depreciation_account_id uuid,
    disposal_gain_loss_account_id uuid
);


--
-- Name: fin_asset_depreciation_schedules; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_asset_depreciation_schedules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    asset_id uuid NOT NULL,
    schedule_date date NOT NULL,
    depreciation_amount numeric(18,2) NOT NULL,
    accumulated_amount numeric(18,2) NOT NULL,
    journal_entry_id uuid,
    status character varying(10) DEFAULT 'pending'::character varying NOT NULL,
    book_id uuid NOT NULL
);


--
-- Name: fin_bank_accounts; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_bank_accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    account_id uuid NOT NULL,
    bank_name character varying(120) NOT NULL,
    account_no character varying(30) NOT NULL,
    ifsc character varying(11),
    branch_name character varying(120),
    account_type character varying(20) DEFAULT 'current'::character varying,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_bank_transactions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_bank_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bank_account_id uuid NOT NULL,
    txn_date date NOT NULL,
    description text,
    reference character varying(80),
    deposit numeric(18,2) DEFAULT 0 NOT NULL,
    withdrawal numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(15) DEFAULT 'unreconciled'::character varying NOT NULL,
    matched_journal_line_id uuid
);


--
-- Name: fin_budget_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_budget_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    budget_id uuid NOT NULL,
    account_id uuid NOT NULL,
    annual_amount numeric(18,2) NOT NULL,
    monthly_distribution jsonb
);


--
-- Name: fin_budgets; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_budgets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    fiscal_year_id uuid NOT NULL,
    cost_center_id uuid,
    name character varying(120) NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    control_level character varying DEFAULT 'warn'::character varying NOT NULL
);


--
-- Name: fin_cost_centers; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_cost_centers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    parent_id uuid,
    is_group boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_depreciation_books; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_depreciation_books (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying NOT NULL,
    name character varying NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    posts_to_gl boolean DEFAULT true NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_dimension_types; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_dimension_types (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying NOT NULL,
    name character varying NOT NULL,
    value_source character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_dimension_values; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_dimension_values (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dimension_type_id uuid NOT NULL,
    code character varying NOT NULL,
    name character varying NOT NULL,
    parent_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_elimination_entries; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_elimination_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_group_id uuid NOT NULL,
    consolidation_company_id uuid NOT NULL,
    as_of_date date NOT NULL,
    journal_entry_id uuid NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_elimination_entry_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_elimination_entry_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    elimination_entry_id uuid NOT NULL,
    intercompany_transaction_id uuid NOT NULL
);


--
-- Name: fin_fiscal_years; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_fiscal_years (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(20) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    is_closed boolean DEFAULT false NOT NULL
);


--
-- Name: fin_fixed_assets; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_fixed_assets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    asset_code character varying(30) NOT NULL,
    name character varying(200) NOT NULL,
    category_id uuid NOT NULL,
    branch_id uuid,
    custodian_employee_id uuid,
    purchase_date date,
    purchase_invoice_id uuid,
    gross_value numeric(18,2) NOT NULL,
    salvage_value numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(15) DEFAULT 'in_use'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_fx_revaluation_runs; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_fx_revaluation_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    run_date date NOT NULL,
    journal_entry_id uuid NOT NULL,
    reversal_journal_entry_id uuid,
    status character varying DEFAULT 'posted'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_intercompany_transactions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_intercompany_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_group_id uuid NOT NULL,
    source_company_id uuid NOT NULL,
    source_journal_entry_id uuid NOT NULL,
    target_company_id uuid NOT NULL,
    target_journal_entry_id uuid,
    transaction_type character varying DEFAULT 'recharge'::character varying NOT NULL,
    amount numeric NOT NULL,
    remarks text,
    status character varying DEFAULT 'pending_approval'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_journal_batches; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_journal_batches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying NOT NULL,
    description text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_journal_entries; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_journal_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entry_no character varying(30) NOT NULL,
    posting_date date NOT NULL,
    period_id uuid NOT NULL,
    entry_type character varying(25) DEFAULT 'journal'::character varying NOT NULL,
    source_module character varying(10),
    source_doctype character varying(60),
    source_id uuid,
    currency_id uuid,
    exchange_rate numeric(18,6) DEFAULT 1 NOT NULL,
    total_debit numeric(18,2) DEFAULT 0 NOT NULL,
    total_credit numeric(18,2) DEFAULT 0 NOT NULL,
    remarks text,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    reversal_of_id uuid,
    posted_by uuid,
    posted_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    batch_id uuid,
    CONSTRAINT fin_journal_entries_balanced CHECK ((((status)::text <> 'posted'::text) OR (total_debit = total_credit)))
);


--
-- Name: fin_journal_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_journal_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    journal_entry_id uuid NOT NULL,
    line_no smallint NOT NULL,
    account_id uuid NOT NULL,
    cost_center_id uuid,
    debit numeric(18,2) DEFAULT 0 NOT NULL,
    credit numeric(18,2) DEFAULT 0 NOT NULL,
    party_type character varying(15),
    party_id uuid,
    against_doctype character varying(60),
    against_id uuid,
    description text,
    department_id uuid,
    reference_1 character varying(100),
    reference_2 character varying(100),
    tax_template_id uuid,
    tax_amount numeric,
    currency_id uuid,
    exchange_rate numeric(18,6),
    business_unit_id uuid,
    CONSTRAINT fin_journal_lines_dr_cr CHECK ((NOT ((debit > (0)::numeric) AND (credit > (0)::numeric))))
);


--
-- Name: fin_journal_template_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_journal_template_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    journal_template_id uuid NOT NULL,
    line_no smallint NOT NULL,
    account_id uuid NOT NULL,
    cost_center_id uuid,
    debit numeric(18,2) DEFAULT 0 NOT NULL,
    credit numeric(18,2) DEFAULT 0 NOT NULL,
    party_type character varying(15),
    party_id uuid,
    description text,
    department_id uuid,
    CONSTRAINT fin_journal_template_lines_dr_cr CHECK ((NOT ((debit > (0)::numeric) AND (credit > (0)::numeric))))
);


--
-- Name: fin_journal_templates; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_journal_templates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    entry_type character varying(25) DEFAULT 'journal'::character varying NOT NULL,
    remarks text,
    frequency character varying(20),
    next_run_date date,
    last_generated_at timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fin_journal_templates_freq_check CHECK (((frequency IS NULL) OR ((frequency)::text = ANY (ARRAY['monthly'::text, 'quarterly'::text, 'annually'::text]))))
);


--
-- Name: fin_line_dimensions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_line_dimensions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    line_table character varying NOT NULL,
    line_id uuid NOT NULL,
    dimension_type_id uuid NOT NULL,
    dimension_value_id uuid NOT NULL
);


--
-- Name: fin_payment_allocations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_payment_allocations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    payment_entry_id uuid NOT NULL,
    against_doctype character varying(60) NOT NULL,
    against_id uuid NOT NULL,
    allocated_amount numeric(18,2) NOT NULL
);


--
-- Name: fin_payment_entries; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_payment_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    payment_no character varying(30) NOT NULL,
    payment_type character varying(10) NOT NULL,
    party_type character varying(15),
    party_id uuid,
    posting_date date NOT NULL,
    mode_of_payment character varying(20) NOT NULL,
    bank_account_id uuid,
    amount numeric(18,2) NOT NULL,
    unallocated_amount numeric(18,2) DEFAULT 0 NOT NULL,
    reference_no character varying(60),
    reference_date date,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    journal_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    cheque_status character varying(10),
    currency_id uuid,
    exchange_rate numeric(18,6) DEFAULT 1 NOT NULL
);


--
-- Name: fin_period_closings; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.fin_period_closings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    period_id uuid NOT NULL,
    closing_journal_entry_id uuid,
    closed_by uuid,
    closed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_appraisal_cycles; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_appraisal_cycles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    status character varying(12) DEFAULT 'active'::character varying NOT NULL,
    participant_count integer DEFAULT 0 NOT NULL
);


--
-- Name: hcm_appraisals; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_appraisals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    cycle_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    reviewer_id uuid,
    self_score numeric(4,2),
    manager_score numeric(4,2),
    final_rating character varying(20),
    status character varying(15) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: hcm_asset_assignments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_asset_assignments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    asset_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    assigned_on date NOT NULL,
    returned_on date,
    return_condition character varying(60),
    return_notes text
);


--
-- Name: hcm_asset_inventory; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_asset_inventory (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    asset_tag character varying(40) NOT NULL,
    asset_type character varying(60) NOT NULL,
    model character varying(120),
    status character varying(15) DEFAULT 'available'::character varying NOT NULL,
    purchase_value numeric(12,2),
    purchased_on date
);


--
-- Name: hcm_asset_requests; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_asset_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    asset_type character varying(60) NOT NULL,
    justification text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: hcm_attendance_records; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_attendance_records (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    attendance_date date NOT NULL,
    shift_id uuid,
    check_in timestamp with time zone,
    check_out timestamp with time zone,
    work_hours numeric(5,2),
    overtime_hours numeric(5,2) DEFAULT 0,
    status character varying(12) NOT NULL,
    source character varying(12) DEFAULT 'web'::character varying NOT NULL
);


--
-- Name: hcm_attendance_regularizations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_attendance_regularizations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    attendance_date date NOT NULL,
    requested_in timestamp with time zone,
    requested_out timestamp with time zone,
    reason text NOT NULL,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: hcm_benefit_categories; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_benefit_categories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: hcm_benefit_category_items; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_benefit_category_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    category_id uuid NOT NULL,
    item character varying(200) NOT NULL
);


--
-- Name: hcm_candidates; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_candidates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    email character varying(150),
    phone character varying(20),
    source character varying(40),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    years_experience numeric(4,1)
);


--
-- Name: hcm_certifications; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_certifications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    name character varying(200) NOT NULL,
    issuer character varying(150),
    issue_date date,
    expiry_date date
);


--
-- Name: hcm_company_okrs; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_company_okrs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    level character varying(20) NOT NULL,
    title character varying(255) NOT NULL,
    owner_name character varying(150) NOT NULL,
    progress_pct smallint DEFAULT 0 NOT NULL
);


--
-- Name: hcm_document_records; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_document_records (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    document_type character varying(80) NOT NULL,
    status character varying(15) DEFAULT 'pending'::character varying NOT NULL,
    uploaded_on date DEFAULT CURRENT_DATE NOT NULL,
    file_url text
);


--
-- Name: hcm_employee_benefits; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_employee_benefits (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    insurance_plan character varying(150),
    esop_units integer DEFAULT 0,
    cab_facility boolean DEFAULT false,
    meal_card boolean DEFAULT false,
    internet_reimbursement boolean DEFAULT false,
    wellness_program boolean DEFAULT false,
    learning_budget_total numeric(12,2) DEFAULT 0,
    learning_budget_used numeric(12,2) DEFAULT 0,
    dependents_covered smallint DEFAULT 0
);


--
-- Name: hcm_employee_education; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_employee_education (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    qualification character varying(150),
    institute character varying(200),
    specialization character varying(150),
    year_of_passing smallint
);


--
-- Name: hcm_employee_lifecycle_events; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_employee_lifecycle_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    event_type character varying(20) NOT NULL,
    event_date date NOT NULL,
    from_designation_id uuid,
    to_designation_id uuid,
    from_department_id uuid,
    to_department_id uuid,
    from_ctc bigint,
    to_ctc bigint,
    from_branch_id uuid,
    to_branch_id uuid,
    from_business_unit_id uuid,
    to_business_unit_id uuid,
    from_sub_department_id uuid,
    to_sub_department_id uuid,
    from_reporting_manager_id uuid,
    to_reporting_manager_id uuid,
    from_dotted_line_manager_id uuid,
    to_dotted_line_manager_id uuid
);


--
-- Name: hcm_employee_prior_experience; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_employee_prior_experience (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    employer_name character varying(200) NOT NULL,
    years_experience numeric(4,1),
    domain character varying(150)
);


--
-- Name: hcm_employee_skill_ratings; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_employee_skill_ratings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    skill character varying(100) NOT NULL,
    level character varying(20) NOT NULL
);


--
-- Name: hcm_employee_skills; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_employee_skills (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    skill character varying(100) NOT NULL
);


--
-- Name: hcm_exit_checklist_items; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_exit_checklist_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    exit_id uuid NOT NULL,
    task character varying(200) NOT NULL,
    owner_id uuid,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: hcm_exit_requests; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_exit_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    resignation_date date NOT NULL,
    last_working_day date,
    reason text,
    exit_interview_notes text,
    status character varying(15) DEFAULT 'submitted'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_expense_claim_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_expense_claim_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    claim_id uuid NOT NULL,
    expense_date date NOT NULL,
    expense_account_id uuid,
    description character varying(255),
    amount numeric(14,2) NOT NULL,
    category character varying(40)
);


--
-- Name: hcm_expense_claims; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_expense_claims (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    claim_no character varying(30) NOT NULL,
    employee_id uuid NOT NULL,
    claim_date date NOT NULL,
    purpose character varying(200),
    total_amount numeric(14,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    approver_id uuid,
    journal_entry_id uuid,
    payment_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    current_step smallint DEFAULT 1 NOT NULL,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: hcm_final_settlements; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_final_settlements (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    exit_id uuid NOT NULL,
    payable_amount numeric(14,2) DEFAULT 0 NOT NULL,
    recovery_amount numeric(14,2) DEFAULT 0 NOT NULL,
    net_amount numeric(14,2) DEFAULT 0 NOT NULL,
    journal_entry_id uuid,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: hcm_goals; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_goals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    cycle_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    weight_pct numeric(5,2) DEFAULT 100 NOT NULL,
    progress_pct numeric(5,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'active'::character varying NOT NULL
);


--
-- Name: hcm_hiring_requisitions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_hiring_requisitions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    requested_by uuid NOT NULL,
    designation_title character varying(120) NOT NULL,
    department_id uuid,
    positions_count integer DEFAULT 1 NOT NULL,
    justification text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: hcm_holidays; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_holidays (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    holiday_date date NOT NULL,
    name character varying(120) NOT NULL,
    is_optional boolean DEFAULT false NOT NULL
);


--
-- Name: hcm_interviews; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_interviews (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    application_id uuid NOT NULL,
    round_no smallint DEFAULT 1 NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    interviewer_id uuid,
    feedback text,
    result character varying(12)
);


--
-- Name: hcm_job_applications; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_job_applications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    opening_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    stage character varying(15) DEFAULT 'applied'::character varying NOT NULL,
    rating smallint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_job_openings; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_job_openings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    title character varying(150) NOT NULL,
    department_id uuid,
    designation_id uuid,
    branch_id uuid,
    vacancies smallint DEFAULT 1 NOT NULL,
    description text,
    status character varying(12) DEFAULT 'open'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    employment_type character varying(20) DEFAULT 'full_time'::character varying NOT NULL,
    posted_date date DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: hcm_leave_allocations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_leave_allocations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    leave_type_id uuid NOT NULL,
    fiscal_year_id uuid NOT NULL,
    allocated_days numeric(5,1) NOT NULL,
    carried_forward_days numeric(5,1) DEFAULT 0 NOT NULL,
    used_days numeric(5,1) DEFAULT 0 NOT NULL
);


--
-- Name: hcm_leave_requests; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_leave_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    leave_type_id uuid NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    days numeric(4,1) NOT NULL,
    is_half_day boolean DEFAULT false NOT NULL,
    reason text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    approved_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    decision_notes text
);


--
-- Name: hcm_leave_types; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_leave_types (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    code character varying(10) NOT NULL,
    is_paid boolean DEFAULT true NOT NULL,
    max_days_per_year numeric(5,1),
    carry_forward boolean DEFAULT false NOT NULL,
    is_encashable boolean DEFAULT false NOT NULL
);


--
-- Name: hcm_loans; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_loans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    loan_type character varying(60) NOT NULL,
    principal_amount numeric(14,2) NOT NULL,
    emi_amount numeric(12,2),
    outstanding_balance numeric(14,2) NOT NULL,
    status character varying(12) DEFAULT 'active'::character varying NOT NULL
);


--
-- Name: hcm_offers; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_offers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    application_id uuid NOT NULL,
    offered_ctc numeric(14,2) NOT NULL,
    offer_date date NOT NULL,
    proposed_joining_date date,
    status character varying(12) DEFAULT 'sent'::character varying NOT NULL
);


--
-- Name: hcm_onboarding_tasks; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_onboarding_tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    onboarding_id uuid NOT NULL,
    task character varying(200) NOT NULL,
    assignee_id uuid,
    due_date date,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: hcm_onboardings; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_onboardings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    start_date date NOT NULL,
    status character varying(12) DEFAULT 'in_progress'::character varying NOT NULL
);


--
-- Name: hcm_overtime_requests; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_overtime_requests (
    id uuid NOT NULL,
    employee_id uuid NOT NULL,
    work_date date NOT NULL,
    hours numeric(4,2) NOT NULL,
    reason text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_payroll_runs; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_payroll_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    run_no character varying(30) NOT NULL,
    period_month smallint NOT NULL,
    period_year smallint NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    journal_entry_id uuid,
    payment_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_policy_documents; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_policy_documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    doc_kind character varying(15) DEFAULT 'policy'::character varying NOT NULL,
    name character varying(200) NOT NULL,
    version character varying(20),
    effective_date date,
    acknowledgement_pct numeric(5,2),
    file_url text
);


--
-- Name: hcm_recognitions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_recognitions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    badge character varying(80) NOT NULL,
    reason text,
    given_on date DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: hcm_salary_components; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_salary_components (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    code character varying(15) NOT NULL,
    component_type character varying(22) NOT NULL,
    calc_type character varying(10) DEFAULT 'fixed'::character varying NOT NULL,
    formula text,
    is_taxable boolean DEFAULT true NOT NULL,
    statutory_code character varying(10),
    gl_account_id uuid
);


--
-- Name: hcm_salary_revision_requests; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_salary_revision_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    current_ctc bigint,
    proposed_ctc bigint,
    reason text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: hcm_salary_slip_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_salary_slip_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slip_id uuid NOT NULL,
    component_id uuid NOT NULL,
    amount numeric(14,2) NOT NULL
);


--
-- Name: hcm_salary_slips; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_salary_slips (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    payroll_run_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    working_days numeric(4,1) NOT NULL,
    lop_days numeric(4,1) DEFAULT 0 NOT NULL,
    gross_pay numeric(14,2) NOT NULL,
    total_deductions numeric(14,2) NOT NULL,
    net_pay numeric(14,2) NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: hcm_salary_structure_assignments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_salary_structure_assignments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    structure_id uuid NOT NULL,
    from_date date NOT NULL,
    base_amount numeric(14,2) NOT NULL,
    annual_ctc numeric(14,2)
);


--
-- Name: hcm_salary_structure_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_salary_structure_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    structure_id uuid NOT NULL,
    component_id uuid NOT NULL,
    amount numeric(14,2),
    percent_of character varying(15),
    percent numeric(7,3)
);


--
-- Name: hcm_salary_structures; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_salary_structures (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    effective_from date NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: hcm_shift_assignments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_shift_assignments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    shift_id uuid NOT NULL,
    from_date date NOT NULL,
    to_date date
);


--
-- Name: hcm_shifts; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_shifts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    start_time time without time zone NOT NULL,
    end_time time without time zone NOT NULL,
    break_minutes smallint DEFAULT 60 NOT NULL,
    grace_minutes smallint DEFAULT 10 NOT NULL,
    is_night boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: hcm_tax_declarations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_tax_declarations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    fiscal_year character varying(9) NOT NULL,
    tax_regime character varying(10) NOT NULL,
    hra_claimed numeric(12,2) DEFAULT 0,
    section_80c numeric(12,2) DEFAULT 0,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: hcm_training_courses; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_training_courses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    course_type character varying(20) DEFAULT 'internal'::character varying,
    duration_hours numeric(6,1),
    provider character varying(120),
    category character varying(60),
    duration_label character varying(30)
);


--
-- Name: hcm_training_enrollments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_training_enrollments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    course_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    status character varying(15) DEFAULT 'enrolled'::character varying NOT NULL,
    completion_date date,
    score numeric(5,2)
);


--
-- Name: hcm_training_sessions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_training_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    session_date date NOT NULL,
    trainer_name character varying(120),
    is_mandatory boolean DEFAULT false NOT NULL,
    attendee_count integer DEFAULT 0 NOT NULL
);


--
-- Name: hcm_travel_requests; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.hcm_travel_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    purpose character varying(200),
    destination character varying(150),
    from_date date NOT NULL,
    to_date date NOT NULL,
    travel_mode character varying(40),
    estimated_cost numeric(12,2),
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    current_step smallint DEFAULT 1,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: mfg_bom_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_bom_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bom_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,4) NOT NULL,
    uom_id uuid,
    rate numeric(18,4),
    scrap_pct numeric(6,3) DEFAULT 0 NOT NULL
);


--
-- Name: mfg_bom_operations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_bom_operations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bom_id uuid NOT NULL,
    seq smallint NOT NULL,
    operation_name character varying(120) NOT NULL,
    work_center_id uuid NOT NULL,
    time_minutes numeric(10,2) NOT NULL,
    operating_cost numeric(14,2)
);


--
-- Name: mfg_boms; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_boms (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    bom_no character varying(30) NOT NULL,
    item_id uuid NOT NULL,
    quantity numeric(18,3) DEFAULT 1 NOT NULL,
    uom_id uuid,
    version smallint DEFAULT 1 NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    total_cost numeric(18,2),
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: mfg_job_card_time_logs; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_job_card_time_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_card_id uuid NOT NULL,
    employee_id uuid,
    from_time timestamp with time zone NOT NULL,
    to_time timestamp with time zone,
    completed_qty numeric(18,3) DEFAULT 0 NOT NULL
);


--
-- Name: mfg_job_cards; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_job_cards (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    jc_no character varying(30) NOT NULL,
    work_order_id uuid NOT NULL,
    bom_operation_id uuid,
    work_center_id uuid NOT NULL,
    seq smallint DEFAULT 1 NOT NULL,
    planned_qty numeric(18,3) NOT NULL,
    completed_qty numeric(18,3) DEFAULT 0 NOT NULL,
    rejected_qty numeric(18,3) DEFAULT 0 NOT NULL,
    assigned_employee_id uuid,
    status character varying(15) DEFAULT 'open'::character varying NOT NULL
);


--
-- Name: mfg_machine_utilization_logs; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_machine_utilization_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    work_center_id uuid NOT NULL,
    log_date date NOT NULL,
    available_minutes numeric(8,1) NOT NULL,
    operated_minutes numeric(8,1) DEFAULT 0 NOT NULL,
    downtime_minutes numeric(8,1) DEFAULT 0 NOT NULL,
    downtime_reason character varying(200)
);


--
-- Name: mfg_production_costings; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_production_costings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    work_order_id uuid NOT NULL,
    material_cost numeric(18,2) DEFAULT 0 NOT NULL,
    labour_cost numeric(18,2) DEFAULT 0 NOT NULL,
    overhead_cost numeric(18,2) DEFAULT 0 NOT NULL,
    total_cost numeric(18,2) DEFAULT 0 NOT NULL,
    per_unit_cost numeric(18,4),
    journal_entry_id uuid
);


--
-- Name: mfg_production_plan_items; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_production_plan_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    production_plan_id uuid NOT NULL,
    item_id uuid NOT NULL,
    bom_id uuid,
    planned_qty numeric(18,3) NOT NULL,
    sales_order_id uuid,
    warehouse_id uuid
);


--
-- Name: mfg_production_plans; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_production_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    plan_no character varying(30) NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    status character varying(15) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: mfg_work_centers; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_work_centers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    branch_id uuid,
    capacity_per_hour numeric(12,3),
    hour_rate numeric(12,2) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: mfg_work_orders; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.mfg_work_orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    wo_no character varying(30) NOT NULL,
    item_id uuid NOT NULL,
    bom_id uuid NOT NULL,
    qty_to_produce numeric(18,3) NOT NULL,
    produced_qty numeric(18,3) DEFAULT 0 NOT NULL,
    production_plan_id uuid,
    sales_order_id uuid,
    wip_warehouse_id uuid,
    fg_warehouse_id uuid,
    planned_start date,
    planned_end date,
    actual_start timestamp with time zone,
    actual_end timestamp with time zone,
    status character varying(15) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: plan_capacity_plans; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_capacity_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    work_center_id uuid NOT NULL,
    period_id uuid NOT NULL,
    available_hours numeric(10,2) DEFAULT 0 NOT NULL,
    planned_hours numeric(10,2) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: plan_demand_forecasts; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_demand_forecasts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid,
    period_id uuid NOT NULL,
    forecast_qty numeric(18,3) DEFAULT 0 NOT NULL,
    forecast_method character varying(15) DEFAULT 'manual'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT plan_demand_forecasts_forecast_method_check CHECK (((forecast_method)::text = ANY ((ARRAY['manual'::character varying, 'moving_avg'::character varying, 'statistical'::character varying])::text[])))
);


--
-- Name: plan_material_requirements; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_material_requirements (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    period_id uuid NOT NULL,
    gross_requirement numeric(18,3) DEFAULT 0 NOT NULL,
    scheduled_receipts numeric(18,3) DEFAULT 0 NOT NULL,
    projected_on_hand numeric(18,3) DEFAULT 0 NOT NULL,
    net_requirement numeric(18,3) DEFAULT 0 NOT NULL,
    suggested_qty numeric(18,3) DEFAULT 0 NOT NULL,
    suggested_action character varying(10),
    source_doctype character varying(60),
    source_id uuid,
    converted_to_doctype character varying(60),
    converted_to_id uuid,
    status character varying(10) DEFAULT 'open'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT plan_material_requirements_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'actioned'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT plan_material_requirements_suggested_action_check CHECK (((suggested_action)::text = ANY ((ARRAY['make'::character varying, 'buy'::character varying, 'transfer'::character varying, 'none'::character varying])::text[])))
);


--
-- Name: plan_planning_calendars; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_planning_calendars (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    bucket_type character varying(10) DEFAULT 'monthly'::character varying NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT plan_planning_calendars_bucket_type_check CHECK (((bucket_type)::text = ANY ((ARRAY['weekly'::character varying, 'monthly'::character varying, 'quarterly'::character varying])::text[])))
);


--
-- Name: plan_planning_exceptions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_planning_exceptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    material_requirement_id uuid,
    exception_type character varying(20) NOT NULL,
    item_id uuid,
    work_center_id uuid,
    period_id uuid,
    description text,
    severity character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    status character varying(12) DEFAULT 'open'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT plan_planning_exceptions_exception_type_check CHECK (((exception_type)::text = ANY ((ARRAY['shortage'::character varying, 'excess'::character varying, 'late_supply'::character varying, 'capacity_overload'::character varying])::text[]))),
    CONSTRAINT plan_planning_exceptions_severity_check CHECK (((severity)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying])::text[]))),
    CONSTRAINT plan_planning_exceptions_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'acknowledged'::character varying, 'resolved'::character varying])::text[])))
);


--
-- Name: plan_planning_periods; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_planning_periods (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    calendar_id uuid NOT NULL,
    period_no integer NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    CONSTRAINT plan_planning_periods_dates_check CHECK ((end_date >= start_date))
);


--
-- Name: plan_planning_scenarios; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_planning_scenarios (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    fiscal_year_id uuid,
    name character varying(150) NOT NULL,
    description text,
    scenario_type character varying(15) DEFAULT 'forecast'::character varying NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT plan_planning_scenarios_scenario_type_check CHECK (((scenario_type)::text = ANY ((ARRAY['budget'::character varying, 'forecast'::character varying, 'whatif'::character varying])::text[]))),
    CONSTRAINT plan_planning_scenarios_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'active'::character varying, 'archived'::character varying])::text[])))
);


--
-- Name: plan_planning_versions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_planning_versions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    scenario_id uuid NOT NULL,
    version_no smallint NOT NULL,
    name character varying(120),
    is_baseline boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: plan_resource_plans; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_resource_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    department_id uuid NOT NULL,
    designation_id uuid,
    period_id uuid NOT NULL,
    required_headcount numeric(8,2) DEFAULT 0 NOT NULL,
    available_headcount numeric(8,2) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: plan_sales_forecasts; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.plan_sales_forecasts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    item_id uuid NOT NULL,
    customer_id uuid,
    period_id uuid NOT NULL,
    forecast_qty numeric(18,3) DEFAULT 0 NOT NULL,
    forecast_revenue numeric(18,2) DEFAULT 0 NOT NULL,
    currency_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: pm_issues; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_issues (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid,
    code character varying(30) NOT NULL,
    title character varying(200) NOT NULL,
    description text,
    issue_type character varying(20) DEFAULT 'other'::character varying NOT NULL,
    severity character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    status character varying(12) DEFAULT 'open'::character varying NOT NULL,
    reported_by uuid,
    assigned_to uuid,
    reported_date date DEFAULT CURRENT_DATE NOT NULL,
    resolved_date date,
    resolution_notes text,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_issues_issue_type_check CHECK (((issue_type)::text = ANY ((ARRAY['bug'::character varying, 'blocker'::character varying, 'change_request'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT pm_issues_severity_check CHECK (((severity)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT pm_issues_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'in_progress'::character varying, 'resolved'::character varying, 'closed'::character varying])::text[])))
);


--
-- Name: pm_meeting_participants; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_meeting_participants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    meeting_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    attendance_status character varying(12) DEFAULT 'invited'::character varying NOT NULL,
    CONSTRAINT pm_meeting_participants_attendance_status_check CHECK (((attendance_status)::text = ANY ((ARRAY['invited'::character varying, 'accepted'::character varying, 'declined'::character varying, 'attended'::character varying, 'absent'::character varying])::text[])))
);


--
-- Name: pm_meetings; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_meetings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid,
    title character varying(200) NOT NULL,
    description text,
    meeting_date date NOT NULL,
    start_time time without time zone NOT NULL,
    end_time time without time zone,
    location character varying(200),
    organizer_employee_id uuid,
    status character varying(12) DEFAULT 'scheduled'::character varying NOT NULL,
    minutes text,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_meetings_status_check CHECK (((status)::text = ANY ((ARRAY['scheduled'::character varying, 'completed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: pm_milestones; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_milestones (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    phase_id uuid,
    name character varying(150) NOT NULL,
    description text,
    due_date date NOT NULL,
    completed_date date,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_milestones_status_check CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'achieved'::character varying, 'missed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: pm_project_budgets; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_budgets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    fiscal_year_id uuid,
    account_id uuid,
    budget_type character varying(10) DEFAULT 'opex'::character varying NOT NULL,
    currency_id uuid,
    planned_amount numeric(18,2) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_project_budgets_budget_type_check CHECK (((budget_type)::text = ANY ((ARRAY['capex'::character varying, 'opex'::character varying])::text[])))
);


--
-- Name: pm_project_categories; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_categories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    parent_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: pm_project_expenses; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_expenses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid,
    employee_id uuid,
    account_id uuid,
    currency_id uuid,
    expense_date date NOT NULL,
    amount numeric(18,2) NOT NULL,
    description character varying(255),
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    approved_by uuid,
    journal_entry_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_project_expenses_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'submitted'::character varying, 'approved'::character varying, 'rejected'::character varying, 'paid'::character varying])::text[])))
);


--
-- Name: pm_project_members; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    project_role_id uuid,
    allocation_pct numeric(5,2) DEFAULT 100,
    start_date date NOT NULL,
    end_date date,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_project_members_alloc_check CHECK (((allocation_pct > (0)::numeric) AND (allocation_pct <= (100)::numeric)))
);


--
-- Name: pm_project_phases; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_phases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    description text,
    sequence smallint NOT NULL,
    planned_start_date date,
    planned_end_date date,
    actual_start_date date,
    actual_end_date date,
    status character varying(15) DEFAULT 'not_started'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_project_phases_status_check CHECK (((status)::text = ANY ((ARRAY['not_started'::character varying, 'in_progress'::character varying, 'completed'::character varying, 'skipped'::character varying])::text[])))
);


--
-- Name: pm_project_roles; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_roles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    description text,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: pm_project_status_history; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_status_history (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    from_status character varying(15),
    to_status character varying(15) NOT NULL,
    changed_by uuid,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    remarks text
);


--
-- Name: pm_project_tags; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_tags (
    project_id uuid NOT NULL,
    tag_id uuid NOT NULL
);


--
-- Name: pm_project_template_phases; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_template_phases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    template_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    sequence smallint NOT NULL,
    default_duration_days integer
);


--
-- Name: pm_project_templates; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_project_templates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    description text,
    category_id uuid,
    default_duration_days integer,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: pm_projects; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_projects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    code character varying(30) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    category_id uuid,
    template_id uuid,
    customer_id uuid,
    cost_center_id uuid,
    project_manager_id uuid,
    currency_id uuid,
    is_billable boolean DEFAULT false NOT NULL,
    priority character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    status character varying(15) DEFAULT 'draft'::character varying NOT NULL,
    planned_start_date date,
    planned_end_date date,
    actual_start_date date,
    actual_end_date date,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_projects_dates_check CHECK (((planned_end_date IS NULL) OR (planned_start_date IS NULL) OR (planned_end_date >= planned_start_date))),
    CONSTRAINT pm_projects_priority_check CHECK (((priority)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT pm_projects_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'planning'::character varying, 'active'::character varying, 'on_hold'::character varying, 'completed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: pm_resource_allocations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_resource_allocations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    project_role_id uuid,
    allocation_pct numeric(5,2) NOT NULL,
    start_date date NOT NULL,
    end_date date,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_resource_allocations_alloc_chk CHECK (((allocation_pct > (0)::numeric) AND (allocation_pct <= (100)::numeric))),
    CONSTRAINT pm_resource_allocations_dates_chk CHECK (((end_date IS NULL) OR (end_date >= start_date)))
);


--
-- Name: pm_risks; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_risks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    code character varying(30) NOT NULL,
    title character varying(200) NOT NULL,
    description text,
    category character varying(20) DEFAULT 'other'::character varying NOT NULL,
    probability character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    impact character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    mitigation_plan text,
    owner_employee_id uuid,
    status character varying(12) DEFAULT 'identified'::character varying NOT NULL,
    identified_date date DEFAULT CURRENT_DATE NOT NULL,
    closed_date date,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_risks_category_check CHECK (((category)::text = ANY ((ARRAY['schedule'::character varying, 'cost'::character varying, 'scope'::character varying, 'resource'::character varying, 'technical'::character varying, 'external'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT pm_risks_impact_check CHECK (((impact)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying])::text[]))),
    CONSTRAINT pm_risks_probability_check CHECK (((probability)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying])::text[]))),
    CONSTRAINT pm_risks_status_check CHECK (((status)::text = ANY ((ARRAY['identified'::character varying, 'mitigating'::character varying, 'occurred'::character varying, 'closed'::character varying])::text[])))
);


--
-- Name: pm_tags; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_tags (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(60) NOT NULL,
    color character varying(7),
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: pm_task_assignments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_task_assignments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    assignment_role character varying(15) DEFAULT 'assignee'::character varying NOT NULL,
    assigned_by uuid,
    assigned_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_task_assignments_assignment_role_check CHECK (((assignment_role)::text = ANY ((ARRAY['assignee'::character varying, 'reviewer'::character varying, 'collaborator'::character varying])::text[])))
);


--
-- Name: pm_task_checklist_items; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_task_checklist_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    description character varying(255) NOT NULL,
    sequence smallint DEFAULT 1 NOT NULL,
    is_checked boolean DEFAULT false NOT NULL,
    checked_by uuid,
    checked_at timestamp with time zone
);


--
-- Name: pm_task_dependencies; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_task_dependencies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    depends_on_task_id uuid NOT NULL,
    dependency_type character varying(20) DEFAULT 'finish_to_start'::character varying NOT NULL,
    CONSTRAINT pm_task_dependencies_dependency_type_check CHECK (((dependency_type)::text = ANY ((ARRAY['finish_to_start'::character varying, 'start_to_start'::character varying, 'finish_to_finish'::character varying, 'start_to_finish'::character varying])::text[]))),
    CONSTRAINT pm_task_dependencies_no_self CHECK ((task_id <> depends_on_task_id))
);


--
-- Name: pm_tasks; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    phase_id uuid,
    milestone_id uuid,
    parent_task_id uuid,
    code character varying(30) NOT NULL,
    title character varying(200) NOT NULL,
    description text,
    task_type character varying(15) DEFAULT 'task'::character varying NOT NULL,
    priority character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    status character varying(15) DEFAULT 'todo'::character varying NOT NULL,
    planned_start_date date,
    due_date date,
    actual_start_date timestamp with time zone,
    actual_end_date timestamp with time zone,
    estimated_hours numeric(8,2),
    actual_hours numeric(8,2) DEFAULT 0 NOT NULL,
    progress_pct smallint DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_tasks_no_self_ref CHECK (((parent_task_id IS NULL) OR (parent_task_id <> id))),
    CONSTRAINT pm_tasks_priority_check CHECK (((priority)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT pm_tasks_progress_chk CHECK (((progress_pct >= 0) AND (progress_pct <= 100))),
    CONSTRAINT pm_tasks_status_check CHECK (((status)::text = ANY ((ARRAY['todo'::character varying, 'in_progress'::character varying, 'in_review'::character varying, 'blocked'::character varying, 'done'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT pm_tasks_task_type_check CHECK (((task_type)::text = ANY ((ARRAY['epic'::character varying, 'task'::character varying, 'subtask'::character varying, 'bug'::character varying])::text[])))
);


--
-- Name: pm_time_entries; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_time_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    timesheet_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid,
    entry_date date NOT NULL,
    start_time time without time zone,
    end_time time without time zone,
    hours numeric(5,2) NOT NULL,
    description text,
    category character varying(40),
    is_billable boolean DEFAULT true NOT NULL,
    billing_rate numeric(12,2),
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_time_entries_hours CHECK (((hours > (0)::numeric) AND (hours <= (24)::numeric))),
    CONSTRAINT pm_time_entries_status_check CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying])::text[])))
);


--
-- Name: pm_timesheets; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.pm_timesheets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    week_start_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    total_hours numeric(6,2) DEFAULT 0 NOT NULL,
    billable_hours numeric(6,2) DEFAULT 0 NOT NULL,
    submitted_at timestamp with time zone,
    approved_by uuid,
    approved_at timestamp with time zone,
    decision_notes text,
    decided_at timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_timesheets_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'submitted'::character varying, 'approved'::character varying, 'rejected'::character varying])::text[])))
);


--
-- Name: retail_cash_movements; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_cash_movements (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid NOT NULL,
    movement_type character varying(10) NOT NULL,
    amount numeric(14,2) NOT NULL,
    reason character varying(200),
    recorded_by uuid,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT retail_cash_movements_amount_check CHECK ((amount > (0)::numeric)),
    CONSTRAINT retail_cash_movements_movement_type_check CHECK (((movement_type)::text = ANY ((ARRAY['cash_in'::character varying, 'cash_out'::character varying, 'drop'::character varying])::text[])))
);


--
-- Name: retail_cash_reconciliations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_cash_reconciliations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    daily_closing_id uuid NOT NULL,
    expected_amount numeric(14,2) NOT NULL,
    counted_amount numeric(14,2) NOT NULL,
    variance numeric(14,2) NOT NULL,
    counted_by uuid,
    counted_at timestamp with time zone DEFAULT now() NOT NULL,
    remarks character varying(255)
);


--
-- Name: retail_coupons; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_coupons (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    promotion_id uuid,
    code character varying(30) NOT NULL,
    description character varying(255),
    discount_type character varying(10) DEFAULT 'pct'::character varying NOT NULL,
    discount_value numeric(10,2) NOT NULL,
    valid_from date NOT NULL,
    valid_to date NOT NULL,
    usage_limit integer,
    used_count integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_coupons_dates_check CHECK ((valid_to >= valid_from)),
    CONSTRAINT retail_coupons_discount_type_check CHECK (((discount_type)::text = ANY ((ARRAY['pct'::character varying, 'fixed'::character varying])::text[])))
);


--
-- Name: retail_daily_closings; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_daily_closings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    store_id uuid NOT NULL,
    closing_date date NOT NULL,
    session_count integer DEFAULT 0 NOT NULL,
    total_sales numeric(14,2) DEFAULT 0 NOT NULL,
    total_returns numeric(14,2) DEFAULT 0 NOT NULL,
    total_tax_collected numeric(14,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    reconciled_by uuid,
    reconciled_at timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_daily_closings_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'reconciled'::character varying])::text[])))
);


--
-- Name: retail_gift_card_transactions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_gift_card_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    gift_card_id uuid NOT NULL,
    pos_sale_id uuid,
    txn_type character varying(10) NOT NULL,
    amount numeric(12,2) NOT NULL,
    txn_date timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT retail_gift_card_transactions_txn_type_check CHECK (((txn_type)::text = ANY ((ARRAY['issue'::character varying, 'redeem'::character varying, 'reload'::character varying, 'adjust'::character varying])::text[])))
);


--
-- Name: retail_gift_cards; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_gift_cards (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    card_no character varying(30) NOT NULL,
    issued_to_customer_id uuid,
    issued_date date DEFAULT CURRENT_DATE NOT NULL,
    expiry_date date,
    initial_balance numeric(12,2) NOT NULL,
    current_balance numeric(12,2) NOT NULL,
    status character varying(10) DEFAULT 'active'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_gift_cards_balance_chk CHECK (((current_balance >= (0)::numeric) AND (current_balance <= initial_balance))),
    CONSTRAINT retail_gift_cards_status_check CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'redeemed'::character varying, 'expired'::character varying, 'blocked'::character varying])::text[])))
);


--
-- Name: retail_loyalty_members; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_loyalty_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    loyalty_code character varying(30) NOT NULL,
    enrolled_date date DEFAULT CURRENT_DATE NOT NULL,
    tier character varying(15) DEFAULT 'standard'::character varying NOT NULL,
    points_balance numeric(12,2) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_loyalty_members_tier_check CHECK (((tier)::text = ANY ((ARRAY['standard'::character varying, 'silver'::character varying, 'gold'::character varying, 'platinum'::character varying])::text[])))
);


--
-- Name: retail_loyalty_transactions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_loyalty_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    loyalty_member_id uuid NOT NULL,
    pos_sale_id uuid,
    txn_type character varying(10) NOT NULL,
    points numeric(12,2) NOT NULL,
    txn_date timestamp with time zone DEFAULT now() NOT NULL,
    notes character varying(255),
    CONSTRAINT retail_loyalty_transactions_txn_type_check CHECK (((txn_type)::text = ANY ((ARRAY['earn'::character varying, 'redeem'::character varying, 'expire'::character varying, 'adjust'::character varying])::text[])))
);


--
-- Name: retail_payment_methods; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_payment_methods (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(60) NOT NULL,
    method_type character varying(15) NOT NULL,
    gl_account_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_payment_methods_method_type_check CHECK (((method_type)::text = ANY ((ARRAY['cash'::character varying, 'card'::character varying, 'upi'::character varying, 'wallet'::character varying, 'gift_card'::character varying, 'store_credit'::character varying, 'other'::character varying])::text[])))
);


--
-- Name: retail_pos_exchanges; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_pos_exchanges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    original_sale_id uuid NOT NULL,
    return_id uuid,
    new_sale_id uuid,
    exchange_datetime timestamp with time zone DEFAULT now() NOT NULL,
    notes character varying(255),
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_pos_exchanges_no_self_check CHECK (((new_sale_id IS NULL) OR (new_sale_id <> original_sale_id)))
);


--
-- Name: retail_pos_payments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_pos_payments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pos_sale_id uuid NOT NULL,
    payment_method_id uuid NOT NULL,
    amount numeric(14,2) NOT NULL,
    reference_no character varying(60),
    CONSTRAINT retail_pos_payments_amount_chk CHECK ((amount > (0)::numeric))
);


--
-- Name: retail_pos_profiles; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_pos_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    store_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    selling_warehouse_id uuid NOT NULL,
    cash_account_id uuid,
    default_customer_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: retail_pos_return_items; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_pos_return_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pos_return_id uuid NOT NULL,
    pos_sale_item_id uuid NOT NULL,
    qty numeric(14,3) NOT NULL,
    amount numeric(14,2) NOT NULL,
    CONSTRAINT retail_pos_return_items_qty_chk CHECK ((qty > (0)::numeric))
);


--
-- Name: retail_pos_returns; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_pos_returns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    original_sale_id uuid NOT NULL,
    return_datetime timestamp with time zone DEFAULT now() NOT NULL,
    reason character varying(255),
    total_amount numeric(14,2) NOT NULL,
    journal_entry_id uuid,
    status character varying(12) DEFAULT 'completed'::character varying NOT NULL,
    processed_by uuid,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_pos_returns_status_check CHECK (((status)::text = ANY ((ARRAY['completed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: retail_pos_sale_items; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_pos_sale_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pos_sale_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    qty numeric(14,3) NOT NULL,
    rate numeric(14,2) NOT NULL,
    discount_pct numeric(6,3) DEFAULT 0 NOT NULL,
    tax_template_id uuid,
    amount numeric(14,2) NOT NULL,
    CONSTRAINT retail_pos_sale_items_qty_chk CHECK ((qty > (0)::numeric))
);


--
-- Name: retail_pos_sales; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_pos_sales (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    store_id uuid NOT NULL,
    session_id uuid NOT NULL,
    sale_no character varying(30) NOT NULL,
    customer_id uuid,
    cashier_employee_id uuid NOT NULL,
    sale_datetime timestamp with time zone DEFAULT now() NOT NULL,
    subtotal numeric(14,2) DEFAULT 0 NOT NULL,
    discount_total numeric(14,2) DEFAULT 0 NOT NULL,
    tax_total numeric(14,2) DEFAULT 0 NOT NULL,
    grand_total numeric(14,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'completed'::character varying NOT NULL,
    journal_entry_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_pos_sales_status_check CHECK (((status)::text = ANY ((ARRAY['completed'::character varying, 'voided'::character varying, 'returned'::character varying, 'partially_returned'::character varying])::text[])))
);


--
-- Name: retail_pos_sessions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_pos_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    terminal_id uuid NOT NULL,
    cashier_employee_id uuid NOT NULL,
    opened_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    opening_cash_amount numeric(14,2) DEFAULT 0 NOT NULL,
    expected_closing_amount numeric(14,2),
    closing_cash_amount numeric(14,2),
    cash_variance numeric(14,2),
    status character varying(10) DEFAULT 'open'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_pos_sessions_dates_check CHECK (((closed_at IS NULL) OR (closed_at >= opened_at))),
    CONSTRAINT retail_pos_sessions_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'closed'::character varying])::text[])))
);


--
-- Name: retail_pos_terminals; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_pos_terminals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    store_id uuid NOT NULL,
    pos_profile_id uuid NOT NULL,
    terminal_code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: retail_price_rules; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_price_rules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    promotion_id uuid,
    item_id uuid,
    item_group_id uuid,
    min_qty numeric(10,3) DEFAULT 1 NOT NULL,
    discount_type character varying(10) DEFAULT 'pct'::character varying NOT NULL,
    discount_value numeric(10,2) NOT NULL,
    priority smallint DEFAULT 1 NOT NULL,
    valid_from date NOT NULL,
    valid_to date NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_price_rules_dates_check CHECK ((valid_to >= valid_from)),
    CONSTRAINT retail_price_rules_discount_type_check CHECK (((discount_type)::text = ANY ((ARRAY['pct'::character varying, 'fixed'::character varying])::text[]))),
    CONSTRAINT retail_price_rules_item_chk CHECK (((item_id IS NOT NULL) OR (item_group_id IS NOT NULL)))
);


--
-- Name: retail_promotions; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_promotions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    description text,
    promotion_type character varying(20) DEFAULT 'pct_off'::character varying NOT NULL,
    valid_from date NOT NULL,
    valid_to date NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_promotions_dates_check CHECK ((valid_to >= valid_from)),
    CONSTRAINT retail_promotions_promotion_type_check CHECK (((promotion_type)::text = ANY ((ARRAY['buy_x_get_y'::character varying, 'pct_off'::character varying, 'fixed_off'::character varying])::text[])))
);


--
-- Name: retail_stores; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.retail_stores (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(150) NOT NULL,
    store_format character varying(15) DEFAULT 'standard'::character varying NOT NULL,
    manager_employee_id uuid,
    opening_date date,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT retail_stores_store_format_check CHECK (((store_format)::text = ANY ((ARRAY['flagship'::character varying, 'standard'::character varying, 'kiosk'::character varying, 'outlet'::character varying, 'online'::character varying])::text[])))
);


--
-- Name: scm_batches; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_batches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    item_id uuid NOT NULL,
    batch_no character varying(40) NOT NULL,
    mfg_date date,
    expiry_date date
);


--
-- Name: scm_bins; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_bins (
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    actual_qty numeric(18,3) DEFAULT 0 NOT NULL,
    reserved_qty numeric(18,3) DEFAULT 0 NOT NULL,
    ordered_qty numeric(18,3) DEFAULT 0 NOT NULL,
    valuation_rate numeric(18,4) DEFAULT 0 NOT NULL
);


--
-- Name: scm_delivery_note_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_delivery_note_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    delivery_note_id uuid NOT NULL,
    so_line_id uuid,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    batch_id uuid
);


--
-- Name: scm_delivery_notes; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_delivery_notes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    dn_no character varying(30) NOT NULL,
    customer_id uuid NOT NULL,
    sales_order_id uuid,
    dn_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_eway_bills; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_eway_bills (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    sales_invoice_id uuid,
    purchase_invoice_id uuid,
    ewb_no character varying NOT NULL,
    ewb_date timestamp with time zone NOT NULL,
    valid_until timestamp with time zone NOT NULL,
    vehicle_no character varying,
    transporter_id character varying,
    transport_mode character varying DEFAULT 'road'::character varying NOT NULL,
    distance_km integer NOT NULL,
    status character varying DEFAULT 'active'::character varying NOT NULL,
    is_mock boolean DEFAULT true NOT NULL,
    cancelled_at timestamp with time zone,
    cancellation_reason text
);


--
-- Name: scm_leads; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_leads (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    company_name character varying(200),
    email character varying(150),
    phone character varying(20),
    source character varying(40),
    status character varying(15) DEFAULT 'new'::character varying NOT NULL,
    owner_id uuid,
    converted_customer_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_opportunities; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_opportunities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(200) NOT NULL,
    lead_id uuid,
    customer_id uuid,
    stage character varying(15) DEFAULT 'prospecting'::character varying NOT NULL,
    amount numeric(18,2),
    probability_pct smallint DEFAULT 50,
    expected_close_date date,
    owner_id uuid
);


--
-- Name: scm_purchase_invoice_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_purchase_invoice_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    purchase_invoice_id uuid NOT NULL,
    po_line_id uuid,
    item_id uuid NOT NULL,
    hsn_sac_code character varying(10),
    qty numeric(18,3) NOT NULL,
    rate numeric(18,2) NOT NULL,
    tax_template_id uuid,
    cgst_amount numeric(18,2) DEFAULT 0,
    sgst_amount numeric(18,2) DEFAULT 0,
    igst_amount numeric(18,2) DEFAULT 0,
    amount numeric(18,2) NOT NULL,
    expense_account_id uuid
);


--
-- Name: scm_purchase_invoices; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_purchase_invoices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    pi_no character varying(30) NOT NULL,
    supplier_bill_no character varying(60),
    supplier_id uuid NOT NULL,
    purchase_order_id uuid,
    purchase_receipt_id uuid,
    invoice_date date NOT NULL,
    due_date date,
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    cgst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    sgst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    igst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    outstanding_amount numeric(18,2) DEFAULT 0 NOT NULL,
    itc_eligible boolean DEFAULT true NOT NULL,
    status character varying(18) DEFAULT 'draft'::character varying NOT NULL,
    journal_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    exchange_rate numeric(18,6) DEFAULT 1 NOT NULL
);


--
-- Name: scm_purchase_order_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_purchase_order_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    purchase_order_id uuid NOT NULL,
    pr_line_id uuid,
    item_id uuid NOT NULL,
    warehouse_id uuid,
    qty numeric(18,3) NOT NULL,
    received_qty numeric(18,3) DEFAULT 0 NOT NULL,
    billed_qty numeric(18,3) DEFAULT 0 NOT NULL,
    uom_id uuid,
    rate numeric(18,2) NOT NULL,
    tax_template_id uuid,
    amount numeric(18,2) NOT NULL
);


--
-- Name: scm_purchase_orders; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_purchase_orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    po_no character varying(30) NOT NULL,
    supplier_id uuid NOT NULL,
    supplier_quotation_id uuid,
    order_date date NOT NULL,
    expected_date date,
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    tax_total numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_purchase_receipt_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_purchase_receipt_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    purchase_receipt_id uuid NOT NULL,
    po_line_id uuid,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    accepted_qty numeric(18,3) NOT NULL,
    rejected_qty numeric(18,3) DEFAULT 0 NOT NULL,
    batch_id uuid
);


--
-- Name: scm_purchase_receipts; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_purchase_receipts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    grn_no character varying(30) NOT NULL,
    supplier_id uuid NOT NULL,
    purchase_order_id uuid,
    receipt_date date NOT NULL,
    qc_status character varying(12) DEFAULT 'pending'::character varying,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_purchase_request_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_purchase_request_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    purchase_request_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    uom_id uuid,
    warehouse_id uuid
);


--
-- Name: scm_purchase_requests; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_purchase_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    pr_no character varying(30) NOT NULL,
    requested_by uuid,
    department_id uuid,
    required_by date,
    source character varying(12) DEFAULT 'manual'::character varying NOT NULL,
    production_plan_id uuid,
    status character varying(15) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_purchase_returns; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_purchase_returns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    return_no character varying(30) NOT NULL,
    purchase_invoice_id uuid NOT NULL,
    return_date date NOT NULL,
    reason text,
    total_amount numeric(18,2) NOT NULL,
    journal_entry_id uuid,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: scm_quotation_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_quotation_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    quotation_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    uom_id uuid,
    rate numeric(18,2) NOT NULL,
    discount_pct numeric(6,3) DEFAULT 0,
    tax_template_id uuid,
    amount numeric(18,2) NOT NULL
);


--
-- Name: scm_quotations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_quotations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    quotation_no character varying(30) NOT NULL,
    customer_id uuid NOT NULL,
    opportunity_id uuid,
    quotation_date date NOT NULL,
    valid_till date,
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    tax_total numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_rfq_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_rfq_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    rfq_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL
);


--
-- Name: scm_rfq_suppliers; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_rfq_suppliers (
    rfq_id uuid NOT NULL,
    supplier_id uuid NOT NULL,
    sent_at timestamp with time zone,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: scm_rfqs; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_rfqs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    rfq_no character varying(30) NOT NULL,
    rfq_date date NOT NULL,
    purchase_request_id uuid,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: scm_sales_invoice_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_sales_invoice_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sales_invoice_id uuid NOT NULL,
    so_line_id uuid,
    item_id uuid NOT NULL,
    hsn_sac_code character varying(10),
    qty numeric(18,3) NOT NULL,
    rate numeric(18,2) NOT NULL,
    discount_pct numeric(6,3) DEFAULT 0,
    tax_template_id uuid,
    cgst_amount numeric(18,2) DEFAULT 0,
    sgst_amount numeric(18,2) DEFAULT 0,
    igst_amount numeric(18,2) DEFAULT 0,
    amount numeric(18,2) NOT NULL,
    income_account_id uuid
);


--
-- Name: scm_sales_invoices; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_sales_invoices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    invoice_no character varying(30) NOT NULL,
    customer_id uuid NOT NULL,
    sales_order_id uuid,
    delivery_note_id uuid,
    invoice_date date NOT NULL,
    due_date date,
    place_of_supply character varying(2),
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    cgst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    sgst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    igst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    outstanding_amount numeric(18,2) DEFAULT 0 NOT NULL,
    einvoice_irn character varying(64),
    eway_bill_no character varying(20),
    status character varying(18) DEFAULT 'draft'::character varying NOT NULL,
    journal_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    exchange_rate numeric(18,6) DEFAULT 1 NOT NULL,
    place_of_supply_state_code character varying(2),
    supply_type character varying DEFAULT 'B2B'::character varying NOT NULL,
    buyer_gstin_snapshot character varying,
    seller_gstin_snapshot character varying,
    irn character varying(64),
    irn_ack_no character varying,
    irn_ack_date timestamp with time zone,
    signed_qr_code text,
    irn_cancelled boolean DEFAULT false NOT NULL,
    irn_cancelled_at timestamp with time zone,
    irn_cancellation_reason text,
    is_mock_irn boolean DEFAULT true NOT NULL
);


--
-- Name: scm_sales_order_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_sales_order_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sales_order_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid,
    qty numeric(18,3) NOT NULL,
    delivered_qty numeric(18,3) DEFAULT 0 NOT NULL,
    invoiced_qty numeric(18,3) DEFAULT 0 NOT NULL,
    uom_id uuid,
    rate numeric(18,2) NOT NULL,
    discount_pct numeric(6,3) DEFAULT 0,
    tax_template_id uuid,
    amount numeric(18,2) NOT NULL
);


--
-- Name: scm_sales_orders; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_sales_orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    order_no character varying(30) NOT NULL,
    customer_id uuid NOT NULL,
    quotation_id uuid,
    order_date date NOT NULL,
    delivery_date date,
    billing_address_id uuid,
    shipping_address_id uuid,
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    tax_total numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    advance_paid numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_sales_returns; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_sales_returns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    return_no character varying(30) NOT NULL,
    sales_invoice_id uuid NOT NULL,
    return_date date NOT NULL,
    reason text,
    total_amount numeric(18,2) NOT NULL,
    restock boolean DEFAULT true NOT NULL,
    journal_entry_id uuid,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: scm_serial_nos; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_serial_nos (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    item_id uuid NOT NULL,
    serial_no character varying(60) NOT NULL,
    batch_id uuid,
    warehouse_id uuid,
    status character varying(15) DEFAULT 'in_stock'::character varying NOT NULL
);


--
-- Name: scm_shipments; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_shipments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    shipment_no character varying(30) NOT NULL,
    delivery_note_id uuid,
    transporter character varying(150),
    vehicle_no character varying(20),
    lr_no character varying(40),
    driver_name character varying(100),
    driver_phone character varying(20),
    eway_bill_no character varying(20),
    dispatch_datetime timestamp with time zone,
    delivered_datetime timestamp with time zone,
    status character varying(15) DEFAULT 'planned'::character varying NOT NULL
);


--
-- Name: scm_stock_entries; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_stock_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entry_no character varying(30) NOT NULL,
    purpose character varying(20) NOT NULL,
    work_order_id uuid,
    entry_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_stock_entry_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_stock_entry_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    stock_entry_id uuid NOT NULL,
    item_id uuid NOT NULL,
    source_warehouse_id uuid,
    target_warehouse_id uuid,
    qty numeric(18,3) NOT NULL,
    rate numeric(18,4),
    batch_id uuid
);


--
-- Name: scm_stock_ledger_entries; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_stock_ledger_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    posting_datetime timestamp with time zone DEFAULT now() NOT NULL,
    qty_change numeric(18,3) NOT NULL,
    valuation_rate numeric(18,4),
    stock_value_change numeric(18,2),
    batch_id uuid,
    source_doctype character varying(60) NOT NULL,
    source_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_stock_transfer_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_stock_transfer_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    stock_transfer_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    batch_id uuid
);


--
-- Name: scm_stock_transfers; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_stock_transfers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    transfer_no character varying(30) NOT NULL,
    from_warehouse_id uuid NOT NULL,
    to_warehouse_id uuid NOT NULL,
    transfer_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_supplier_quotation_lines; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_supplier_quotation_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sq_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    rate numeric(18,2) NOT NULL
);


--
-- Name: scm_supplier_quotations; Type: TABLE; Schema: _template; Owner: -
--

CREATE TABLE _template.scm_supplier_quotations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    sq_no character varying(30) NOT NULL,
    supplier_id uuid NOT NULL,
    rfq_id uuid,
    quote_date date NOT NULL,
    valid_till date,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'received'::character varying NOT NULL
);


--
-- Name: core_activity_logs; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_activity_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(60) NOT NULL,
    entity_id uuid NOT NULL,
    user_id uuid,
    activity_type character varying(40) NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_addresses; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_addresses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(30) NOT NULL,
    entity_id uuid NOT NULL,
    address_type character varying(15) DEFAULT 'billing'::character varying NOT NULL,
    line1 character varying(200) NOT NULL,
    line2 character varying(200),
    city character varying(80),
    state character varying(80),
    state_code character varying(2),
    pincode character varying(10),
    country character varying(80) DEFAULT 'India'::character varying NOT NULL,
    is_primary boolean DEFAULT false NOT NULL,
    is_permanent boolean DEFAULT false NOT NULL
);


--
-- Name: core_approval_actions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_approval_actions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    request_id uuid NOT NULL,
    step_order smallint NOT NULL,
    actor_id uuid NOT NULL,
    action character varying(15) NOT NULL,
    comments text,
    acted_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_approval_requests; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_approval_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    workflow_id uuid NOT NULL,
    doctype character varying(60) NOT NULL,
    document_id uuid NOT NULL,
    status character varying(15) DEFAULT 'pending'::character varying NOT NULL,
    current_step smallint DEFAULT 1 NOT NULL,
    requested_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_approval_workflow_steps; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_approval_workflow_steps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workflow_id uuid NOT NULL,
    step_order smallint NOT NULL,
    approver_type character varying(20) NOT NULL,
    role_id uuid,
    user_id uuid,
    min_amount numeric(18,2),
    max_amount numeric(18,2)
);


--
-- Name: core_approval_workflows; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_approval_workflows (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    doctype character varying(60) NOT NULL,
    name character varying(120) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_attachments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_attachments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(60) NOT NULL,
    entity_id uuid NOT NULL,
    file_name character varying(255) NOT NULL,
    file_url text NOT NULL,
    mime_type character varying(100),
    size_bytes bigint,
    uploaded_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_audit_logs; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_audit_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    user_id uuid,
    action character varying(20) NOT NULL,
    doctype character varying(60) NOT NULL,
    document_id uuid,
    changes jsonb,
    ip_address inet,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_bands; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_bands (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    code character varying(20) NOT NULL,
    band_number integer NOT NULL,
    description text,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_branches; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_branches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    gstin character varying(15),
    is_head_office boolean DEFAULT false NOT NULL,
    address_line1 character varying(200),
    city character varying(80),
    state character varying(80),
    pincode character varying(10),
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    country character varying(80) DEFAULT 'India'::character varying NOT NULL,
    tz character varying(60) DEFAULT 'IST (UTC+5:30)'::character varying NOT NULL,
    branch_manager_id uuid
);


--
-- Name: core_business_units; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_business_units (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    head_employee_id uuid,
    cost_center character varying(20),
    is_active boolean DEFAULT true NOT NULL,
    branch_id uuid
);


--
-- Name: core_comments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_comments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(60) NOT NULL,
    entity_id uuid NOT NULL,
    user_id uuid NOT NULL,
    parent_id uuid,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_companies; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_companies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(150) NOT NULL,
    legal_name character varying(200),
    gstin character varying(15),
    pan character varying(10),
    cin character varying(25),
    default_currency_id uuid NOT NULL,
    fiscal_year_start_month smallint DEFAULT 4 NOT NULL,
    address_line1 character varying(200),
    address_line2 character varying(200),
    city character varying(80),
    state character varying(80),
    pincode character varying(10),
    country character varying(80) DEFAULT 'India'::character varying,
    logo_url text,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    industry character varying(100),
    founded_date date,
    email_domain character varying(150),
    branch_head_id uuid,
    company_group_id uuid,
    is_consolidation_only boolean DEFAULT false NOT NULL,
    gsp_provider character varying DEFAULT 'mock'::character varying NOT NULL,
    code character varying(30)
);


--
-- Name: core_company_groups; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_company_groups (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_company_menu_items; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_company_menu_items (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    menu_item_id uuid NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL
);


--
-- Name: core_company_modules; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_company_modules (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    module_id uuid NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_company_settings; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_company_settings (
    company_id uuid NOT NULL,
    default_currency character varying(10) DEFAULT 'INR'::character varying NOT NULL,
    default_timezone character varying(60) DEFAULT 'IST (UTC+5:30)'::character varying NOT NULL,
    working_days_per_week smallint DEFAULT 5 NOT NULL,
    enable_branch_level boolean DEFAULT true NOT NULL,
    enable_business_unit_level boolean DEFAULT true NOT NULL,
    enable_department_level boolean DEFAULT true NOT NULL,
    enable_sub_department_level boolean DEFAULT true NOT NULL,
    working_hours_start time without time zone,
    working_hours_end time without time zone,
    probation_period_days smallint DEFAULT 90 NOT NULL,
    notice_period_days smallint DEFAULT 30 NOT NULL
);


--
-- Name: core_contacts; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_contacts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entity_type character varying(30) NOT NULL,
    entity_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    designation character varying(80),
    email character varying(150),
    phone character varying(20),
    is_primary boolean DEFAULT false NOT NULL,
    is_emergency boolean DEFAULT false NOT NULL
);


--
-- Name: core_customers; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_customers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(200) NOT NULL,
    customer_type character varying(10) DEFAULT 'b2b'::character varying NOT NULL,
    gstin character varying(15),
    pan character varying(10),
    credit_limit numeric(18,2) DEFAULT 0 NOT NULL,
    credit_days integer DEFAULT 0 NOT NULL,
    receivable_account_id uuid,
    default_currency_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    default_tax_template_id uuid
);


--
-- Name: core_departments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_departments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    parent_id uuid,
    head_employee_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    business_unit_id uuid,
    annual_budget numeric(14,2),
    hr_representative_id uuid,
    senior_manager_id uuid,
    project_manager_id uuid
);


--
-- Name: core_designation_permissions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_designation_permissions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    designation_id uuid NOT NULL,
    permission_id uuid NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_designations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_designations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    band character varying(40),
    is_active boolean DEFAULT true NOT NULL,
    role_id uuid,
    band_id uuid
);


--
-- Name: core_employees; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_employees (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    department_id uuid,
    designation_id uuid,
    employee_code character varying(20) NOT NULL,
    first_name character varying(80) NOT NULL,
    last_name character varying(80),
    work_email character varying(150),
    date_of_birth date,
    gender character varying(15),
    date_of_joining date NOT NULL,
    employment_type character varying(20) DEFAULT 'full_time'::character varying NOT NULL,
    reporting_manager_id uuid,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    pan character varying(10),
    uan character varying(12),
    esi_number character varying(17),
    bank_name character varying(100),
    bank_account_no character varying(30),
    bank_ifsc character varying(11),
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    sub_department_id uuid,
    team character varying(150),
    work_mode character varying(20),
    confirmation_date date,
    probation_end_date date,
    dotted_line_manager_id uuid,
    band integer,
    annual_ctc bigint,
    blood_group character varying(5),
    nationality character varying(50),
    marital_status character varying(20),
    personal_email character varying(150),
    personal_phone character varying(20),
    current_address text,
    permanent_address text,
    tax_regime character varying(10)
);


--
-- Name: core_field_rules; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_field_rules (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    entity_key character varying(40) NOT NULL,
    field_key character varying(60) NOT NULL,
    state character varying(10) NOT NULL,
    role_id uuid,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT core_field_rules_state_check CHECK (((state)::text = ANY (ARRAY['mandatory'::text, 'optional'::text, 'hidden'::text, 'readonly'::text])))
);


--
-- Name: core_hierarchy_role_bindings; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_hierarchy_role_bindings (
    id uuid NOT NULL,
    company_id uuid NOT NULL,
    rung_key character varying(30) NOT NULL,
    role_id uuid NOT NULL,
    CONSTRAINT core_hrb_rung_check CHECK (((rung_key)::text = ANY (ARRAY['team_lead'::text, 'project_manager'::text, 'senior_manager'::text, 'branch_manager'::text, 'branch_head'::text])))
);


--
-- Name: core_integrations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_integrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    description character varying(200),
    is_connected boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_intercompany_relationships; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_intercompany_relationships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_group_id uuid NOT NULL,
    company_id uuid NOT NULL,
    counterparty_company_id uuid NOT NULL,
    due_from_account_id uuid,
    due_to_account_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_item_groups; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_item_groups (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    parent_id uuid,
    income_account_id uuid,
    expense_account_id uuid
);


--
-- Name: core_items; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(40) NOT NULL,
    name character varying(200) NOT NULL,
    item_group_id uuid,
    item_type character varying(15) DEFAULT 'goods'::character varying NOT NULL,
    uom_id uuid NOT NULL,
    hsn_sac_code character varying(10),
    sales_tax_template_id uuid,
    purchase_tax_template_id uuid,
    is_stock_item boolean DEFAULT true NOT NULL,
    is_sales_item boolean DEFAULT true NOT NULL,
    is_purchase_item boolean DEFAULT true NOT NULL,
    is_manufactured boolean DEFAULT false NOT NULL,
    has_batch boolean DEFAULT false NOT NULL,
    has_serial boolean DEFAULT false NOT NULL,
    valuation_method character varying(10) DEFAULT 'fifo'::character varying NOT NULL,
    standard_selling_rate numeric(18,2),
    standard_buying_rate numeric(18,2),
    reorder_level numeric(18,3) DEFAULT 0,
    reorder_qty numeric(18,3) DEFAULT 0,
    barcode character varying(60),
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_notification_preferences; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_notification_preferences (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    event_key character varying(80) NOT NULL,
    channel character varying(10) NOT NULL,
    enabled boolean DEFAULT true NOT NULL
);


--
-- Name: core_notifications; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_notifications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    body text,
    entity_type character varying(60),
    entity_id uuid,
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: core_number_series; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_number_series (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    doctype character varying(60) NOT NULL,
    prefix character varying(20) NOT NULL,
    fiscal_infix boolean DEFAULT true NOT NULL,
    padding smallint DEFAULT 5 NOT NULL,
    current_no bigint DEFAULT 0 NOT NULL,
    suffix character varying(20) DEFAULT ''::character varying NOT NULL
);


--
-- Name: core_permissions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_permissions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code character varying(120) NOT NULL,
    module character varying(20) NOT NULL,
    resource character varying(60) NOT NULL,
    action character varying(30) NOT NULL
);


--
-- Name: core_role_permissions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_role_permissions (
    role_id uuid NOT NULL,
    permission_id uuid NOT NULL
);


--
-- Name: core_roles; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_roles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    description text,
    is_system boolean DEFAULT false NOT NULL,
    band_id uuid
);


--
-- Name: core_sub_departments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_sub_departments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    department_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_suppliers; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_suppliers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(200) NOT NULL,
    gstin character varying(15),
    pan character varying(10),
    payment_terms_days integer DEFAULT 0 NOT NULL,
    payable_account_id uuid,
    default_currency_id uuid,
    is_msme boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    default_tax_template_id uuid,
    default_tds_section_id uuid,
    pan_verified boolean DEFAULT false NOT NULL
);


--
-- Name: core_tax_template_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_tax_template_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tax_template_id uuid NOT NULL,
    tax_id uuid NOT NULL
);


--
-- Name: core_tax_templates; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_tax_templates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(60) NOT NULL,
    applies_to character varying(10) DEFAULT 'both'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_taxes; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_taxes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(60) NOT NULL,
    tax_type character varying(15) NOT NULL,
    rate numeric(7,3) NOT NULL,
    account_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_tds_deductions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_tds_deductions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    purchase_invoice_id uuid NOT NULL,
    supplier_id uuid NOT NULL,
    tds_section_id uuid NOT NULL,
    taxable_amount numeric NOT NULL,
    tds_amount numeric NOT NULL,
    deducted_at timestamp with time zone DEFAULT now() NOT NULL,
    certificate_no character varying,
    journal_entry_id uuid,
    status character varying DEFAULT 'deducted'::character varying NOT NULL
);


--
-- Name: core_tds_sections; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_tds_sections (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    section_code character varying NOT NULL,
    description character varying NOT NULL,
    rate numeric NOT NULL,
    threshold_amount numeric,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_uom_conversions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_uom_conversions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    item_id uuid,
    from_uom_id uuid NOT NULL,
    to_uom_id uuid NOT NULL,
    factor numeric(18,6) NOT NULL
);


--
-- Name: core_uoms; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_uoms (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(15) NOT NULL,
    name character varying(60) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: core_user_roles; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_user_roles (
    user_id uuid NOT NULL,
    role_id uuid NOT NULL,
    branch_id uuid
);


--
-- Name: core_users; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    email character varying(150) NOT NULL,
    phone character varying(20),
    password_hash text NOT NULL,
    employee_id uuid,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    last_login_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    full_name character varying(150)
);


--
-- Name: core_warehouses; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.core_warehouses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    warehouse_type character varying(20) DEFAULT 'stores'::character varying NOT NULL,
    parent_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_accounting_periods; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_accounting_periods (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    fiscal_year_id uuid NOT NULL,
    name character varying(20) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    status character varying(10) DEFAULT 'open'::character varying NOT NULL
);


--
-- Name: fin_accounts; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(150) NOT NULL,
    parent_id uuid,
    is_group boolean DEFAULT false NOT NULL,
    root_type character varying(12) NOT NULL,
    account_type character varying(30),
    currency_id uuid,
    is_frozen boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_allocation_rule_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_allocation_rule_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    allocation_rule_id uuid NOT NULL,
    line_no smallint NOT NULL,
    destination_account_id uuid NOT NULL,
    cost_center_id uuid,
    department_id uuid,
    percentage numeric(5,2) NOT NULL,
    CONSTRAINT fin_allocation_rule_lines_pct_chk CHECK (((percentage > (0)::numeric) AND (percentage <= (100)::numeric)))
);


--
-- Name: fin_allocation_rules; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_allocation_rules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    source_account_id uuid NOT NULL,
    remarks text,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_asset_categories; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_asset_categories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    depreciation_method character varying(15) DEFAULT 'slm'::character varying NOT NULL,
    useful_life_months integer NOT NULL,
    depreciation_rate numeric(7,3),
    asset_account_id uuid,
    depreciation_expense_account_id uuid,
    accumulated_depreciation_account_id uuid,
    disposal_gain_loss_account_id uuid
);


--
-- Name: fin_asset_category_books; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_asset_category_books (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    category_id uuid NOT NULL,
    book_id uuid NOT NULL,
    depreciation_method character varying DEFAULT 'slm'::character varying NOT NULL,
    useful_life_months integer NOT NULL,
    depreciation_rate numeric,
    depreciation_expense_account_id uuid,
    accumulated_depreciation_account_id uuid,
    disposal_gain_loss_account_id uuid
);


--
-- Name: fin_asset_depreciation_schedules; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_asset_depreciation_schedules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    asset_id uuid NOT NULL,
    schedule_date date NOT NULL,
    depreciation_amount numeric(18,2) NOT NULL,
    accumulated_amount numeric(18,2) NOT NULL,
    journal_entry_id uuid,
    status character varying(10) DEFAULT 'pending'::character varying NOT NULL,
    book_id uuid NOT NULL
);


--
-- Name: fin_bank_accounts; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_bank_accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    account_id uuid NOT NULL,
    bank_name character varying(120) NOT NULL,
    account_no character varying(30) NOT NULL,
    ifsc character varying(11),
    branch_name character varying(120),
    account_type character varying(20) DEFAULT 'current'::character varying,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_bank_transactions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_bank_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bank_account_id uuid NOT NULL,
    txn_date date NOT NULL,
    description text,
    reference character varying(80),
    deposit numeric(18,2) DEFAULT 0 NOT NULL,
    withdrawal numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(15) DEFAULT 'unreconciled'::character varying NOT NULL,
    matched_journal_line_id uuid
);


--
-- Name: fin_budget_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_budget_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    budget_id uuid NOT NULL,
    account_id uuid NOT NULL,
    annual_amount numeric(18,2) NOT NULL,
    monthly_distribution jsonb
);


--
-- Name: fin_budgets; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_budgets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    fiscal_year_id uuid NOT NULL,
    cost_center_id uuid,
    name character varying(120) NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    control_level character varying DEFAULT 'warn'::character varying NOT NULL
);


--
-- Name: fin_cost_centers; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_cost_centers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    parent_id uuid,
    is_group boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_depreciation_books; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_depreciation_books (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying NOT NULL,
    name character varying NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    posts_to_gl boolean DEFAULT true NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_dimension_types; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_dimension_types (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying NOT NULL,
    name character varying NOT NULL,
    value_source character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_dimension_values; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_dimension_values (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dimension_type_id uuid NOT NULL,
    code character varying NOT NULL,
    name character varying NOT NULL,
    parent_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: fin_elimination_entries; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_elimination_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_group_id uuid NOT NULL,
    consolidation_company_id uuid NOT NULL,
    as_of_date date NOT NULL,
    journal_entry_id uuid NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_elimination_entry_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_elimination_entry_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    elimination_entry_id uuid NOT NULL,
    intercompany_transaction_id uuid NOT NULL
);


--
-- Name: fin_fiscal_years; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_fiscal_years (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(20) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    is_closed boolean DEFAULT false NOT NULL
);


--
-- Name: fin_fixed_assets; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_fixed_assets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    asset_code character varying(30) NOT NULL,
    name character varying(200) NOT NULL,
    category_id uuid NOT NULL,
    branch_id uuid,
    custodian_employee_id uuid,
    purchase_date date,
    purchase_invoice_id uuid,
    gross_value numeric(18,2) NOT NULL,
    salvage_value numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(15) DEFAULT 'in_use'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_fx_revaluation_runs; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_fx_revaluation_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    run_date date NOT NULL,
    journal_entry_id uuid NOT NULL,
    reversal_journal_entry_id uuid,
    status character varying DEFAULT 'posted'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_intercompany_transactions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_intercompany_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_group_id uuid NOT NULL,
    source_company_id uuid NOT NULL,
    source_journal_entry_id uuid NOT NULL,
    target_company_id uuid NOT NULL,
    target_journal_entry_id uuid,
    transaction_type character varying DEFAULT 'recharge'::character varying NOT NULL,
    amount numeric NOT NULL,
    remarks text,
    status character varying DEFAULT 'pending_approval'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_journal_batches; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_journal_batches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying NOT NULL,
    description text,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fin_journal_entries; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_journal_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entry_no character varying(30) NOT NULL,
    posting_date date NOT NULL,
    period_id uuid NOT NULL,
    entry_type character varying(25) DEFAULT 'journal'::character varying NOT NULL,
    source_module character varying(10),
    source_doctype character varying(60),
    source_id uuid,
    currency_id uuid,
    exchange_rate numeric(18,6) DEFAULT 1 NOT NULL,
    total_debit numeric(18,2) DEFAULT 0 NOT NULL,
    total_credit numeric(18,2) DEFAULT 0 NOT NULL,
    remarks text,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    reversal_of_id uuid,
    posted_by uuid,
    posted_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    batch_id uuid,
    CONSTRAINT fin_journal_entries_balanced CHECK ((((status)::text <> 'posted'::text) OR (total_debit = total_credit)))
);


--
-- Name: fin_journal_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_journal_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    journal_entry_id uuid NOT NULL,
    line_no smallint NOT NULL,
    account_id uuid NOT NULL,
    cost_center_id uuid,
    debit numeric(18,2) DEFAULT 0 NOT NULL,
    credit numeric(18,2) DEFAULT 0 NOT NULL,
    party_type character varying(15),
    party_id uuid,
    against_doctype character varying(60),
    against_id uuid,
    description text,
    department_id uuid,
    reference_1 character varying(100),
    reference_2 character varying(100),
    tax_template_id uuid,
    tax_amount numeric,
    currency_id uuid,
    exchange_rate numeric(18,6),
    business_unit_id uuid,
    CONSTRAINT fin_journal_lines_dr_cr CHECK ((NOT ((debit > (0)::numeric) AND (credit > (0)::numeric))))
);


--
-- Name: fin_journal_template_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_journal_template_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    journal_template_id uuid NOT NULL,
    line_no smallint NOT NULL,
    account_id uuid NOT NULL,
    cost_center_id uuid,
    debit numeric(18,2) DEFAULT 0 NOT NULL,
    credit numeric(18,2) DEFAULT 0 NOT NULL,
    party_type character varying(15),
    party_id uuid,
    description text,
    department_id uuid,
    CONSTRAINT fin_journal_template_lines_dr_cr CHECK ((NOT ((debit > (0)::numeric) AND (credit > (0)::numeric))))
);


--
-- Name: fin_journal_templates; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_journal_templates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    entry_type character varying(25) DEFAULT 'journal'::character varying NOT NULL,
    remarks text,
    frequency character varying(20),
    next_run_date date,
    last_generated_at timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fin_journal_templates_freq_check CHECK (((frequency IS NULL) OR ((frequency)::text = ANY (ARRAY['monthly'::text, 'quarterly'::text, 'annually'::text]))))
);


--
-- Name: fin_line_dimensions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_line_dimensions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    line_table character varying NOT NULL,
    line_id uuid NOT NULL,
    dimension_type_id uuid NOT NULL,
    dimension_value_id uuid NOT NULL
);


--
-- Name: fin_payment_allocations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_payment_allocations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    payment_entry_id uuid NOT NULL,
    against_doctype character varying(60) NOT NULL,
    against_id uuid NOT NULL,
    allocated_amount numeric(18,2) NOT NULL
);


--
-- Name: fin_payment_entries; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_payment_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    payment_no character varying(30) NOT NULL,
    payment_type character varying(10) NOT NULL,
    party_type character varying(15),
    party_id uuid,
    posting_date date NOT NULL,
    mode_of_payment character varying(20) NOT NULL,
    bank_account_id uuid,
    amount numeric(18,2) NOT NULL,
    unallocated_amount numeric(18,2) DEFAULT 0 NOT NULL,
    reference_no character varying(60),
    reference_date date,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    journal_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    cheque_status character varying(10),
    currency_id uuid,
    exchange_rate numeric(18,6) DEFAULT 1 NOT NULL
);


--
-- Name: fin_period_closings; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.fin_period_closings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    period_id uuid NOT NULL,
    closing_journal_entry_id uuid,
    closed_by uuid,
    closed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_appraisal_cycles; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_appraisal_cycles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    status character varying(12) DEFAULT 'active'::character varying NOT NULL,
    participant_count integer DEFAULT 0 NOT NULL
);


--
-- Name: hcm_appraisals; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_appraisals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    cycle_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    reviewer_id uuid,
    self_score numeric(4,2),
    manager_score numeric(4,2),
    final_rating character varying(20),
    status character varying(15) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: hcm_asset_assignments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_asset_assignments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    asset_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    assigned_on date NOT NULL,
    returned_on date,
    return_condition character varying(60),
    return_notes text
);


--
-- Name: hcm_asset_inventory; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_asset_inventory (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    asset_tag character varying(40) NOT NULL,
    asset_type character varying(60) NOT NULL,
    model character varying(120),
    status character varying(15) DEFAULT 'available'::character varying NOT NULL,
    purchase_value numeric(12,2),
    purchased_on date
);


--
-- Name: hcm_asset_requests; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_asset_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    asset_type character varying(60) NOT NULL,
    justification text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: hcm_attendance_records; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_attendance_records (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    attendance_date date NOT NULL,
    shift_id uuid,
    check_in timestamp with time zone,
    check_out timestamp with time zone,
    work_hours numeric(5,2),
    overtime_hours numeric(5,2) DEFAULT 0,
    status character varying(12) NOT NULL,
    source character varying(12) DEFAULT 'web'::character varying NOT NULL
);


--
-- Name: hcm_attendance_regularizations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_attendance_regularizations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    attendance_date date NOT NULL,
    requested_in timestamp with time zone,
    requested_out timestamp with time zone,
    reason text NOT NULL,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: hcm_benefit_categories; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_benefit_categories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: hcm_benefit_category_items; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_benefit_category_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    category_id uuid NOT NULL,
    item character varying(200) NOT NULL
);


--
-- Name: hcm_candidates; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_candidates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    email character varying(150),
    phone character varying(20),
    source character varying(40),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    years_experience numeric(4,1)
);


--
-- Name: hcm_certifications; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_certifications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    name character varying(200) NOT NULL,
    issuer character varying(150),
    issue_date date,
    expiry_date date
);


--
-- Name: hcm_company_okrs; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_company_okrs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    level character varying(20) NOT NULL,
    title character varying(255) NOT NULL,
    owner_name character varying(150) NOT NULL,
    progress_pct smallint DEFAULT 0 NOT NULL
);


--
-- Name: hcm_document_records; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_document_records (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    document_type character varying(80) NOT NULL,
    status character varying(15) DEFAULT 'pending'::character varying NOT NULL,
    uploaded_on date DEFAULT CURRENT_DATE NOT NULL,
    file_url text
);


--
-- Name: hcm_employee_benefits; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_employee_benefits (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    insurance_plan character varying(150),
    esop_units integer DEFAULT 0,
    cab_facility boolean DEFAULT false,
    meal_card boolean DEFAULT false,
    internet_reimbursement boolean DEFAULT false,
    wellness_program boolean DEFAULT false,
    learning_budget_total numeric(12,2) DEFAULT 0,
    learning_budget_used numeric(12,2) DEFAULT 0,
    dependents_covered smallint DEFAULT 0
);


--
-- Name: hcm_employee_education; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_employee_education (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    qualification character varying(150),
    institute character varying(200),
    specialization character varying(150),
    year_of_passing smallint
);


--
-- Name: hcm_employee_lifecycle_events; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_employee_lifecycle_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    event_type character varying(20) NOT NULL,
    event_date date NOT NULL,
    from_designation_id uuid,
    to_designation_id uuid,
    from_department_id uuid,
    to_department_id uuid,
    from_ctc bigint,
    to_ctc bigint,
    from_branch_id uuid,
    to_branch_id uuid,
    from_business_unit_id uuid,
    to_business_unit_id uuid,
    from_sub_department_id uuid,
    to_sub_department_id uuid,
    from_reporting_manager_id uuid,
    to_reporting_manager_id uuid,
    from_dotted_line_manager_id uuid,
    to_dotted_line_manager_id uuid
);


--
-- Name: hcm_employee_prior_experience; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_employee_prior_experience (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    employer_name character varying(200) NOT NULL,
    years_experience numeric(4,1),
    domain character varying(150)
);


--
-- Name: hcm_employee_skill_ratings; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_employee_skill_ratings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    skill character varying(100) NOT NULL,
    level character varying(20) NOT NULL
);


--
-- Name: hcm_employee_skills; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_employee_skills (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    skill character varying(100) NOT NULL
);


--
-- Name: hcm_exit_checklist_items; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_exit_checklist_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    exit_id uuid NOT NULL,
    task character varying(200) NOT NULL,
    owner_id uuid,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: hcm_exit_requests; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_exit_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    resignation_date date NOT NULL,
    last_working_day date,
    reason text,
    exit_interview_notes text,
    status character varying(15) DEFAULT 'submitted'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_expense_claim_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_expense_claim_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    claim_id uuid NOT NULL,
    expense_date date NOT NULL,
    expense_account_id uuid,
    description character varying(255),
    amount numeric(14,2) NOT NULL,
    category character varying(40)
);


--
-- Name: hcm_expense_claims; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_expense_claims (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    claim_no character varying(30) NOT NULL,
    employee_id uuid NOT NULL,
    claim_date date NOT NULL,
    purpose character varying(200),
    total_amount numeric(14,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    approver_id uuid,
    journal_entry_id uuid,
    payment_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    current_step smallint DEFAULT 1 NOT NULL,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: hcm_final_settlements; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_final_settlements (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    exit_id uuid NOT NULL,
    payable_amount numeric(14,2) DEFAULT 0 NOT NULL,
    recovery_amount numeric(14,2) DEFAULT 0 NOT NULL,
    net_amount numeric(14,2) DEFAULT 0 NOT NULL,
    journal_entry_id uuid,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: hcm_goals; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_goals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    cycle_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    weight_pct numeric(5,2) DEFAULT 100 NOT NULL,
    progress_pct numeric(5,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'active'::character varying NOT NULL
);


--
-- Name: hcm_hiring_requisitions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_hiring_requisitions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    requested_by uuid NOT NULL,
    designation_title character varying(120) NOT NULL,
    department_id uuid,
    positions_count integer DEFAULT 1 NOT NULL,
    justification text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: hcm_holidays; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_holidays (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    holiday_date date NOT NULL,
    name character varying(120) NOT NULL,
    is_optional boolean DEFAULT false NOT NULL
);


--
-- Name: hcm_interviews; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_interviews (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    application_id uuid NOT NULL,
    round_no smallint DEFAULT 1 NOT NULL,
    scheduled_at timestamp with time zone NOT NULL,
    interviewer_id uuid,
    feedback text,
    result character varying(12)
);


--
-- Name: hcm_job_applications; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_job_applications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    opening_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    stage character varying(15) DEFAULT 'applied'::character varying NOT NULL,
    rating smallint,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_job_openings; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_job_openings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    title character varying(150) NOT NULL,
    department_id uuid,
    designation_id uuid,
    branch_id uuid,
    vacancies smallint DEFAULT 1 NOT NULL,
    description text,
    status character varying(12) DEFAULT 'open'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    employment_type character varying(20) DEFAULT 'full_time'::character varying NOT NULL,
    posted_date date DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: hcm_leave_allocations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_leave_allocations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    leave_type_id uuid NOT NULL,
    fiscal_year_id uuid NOT NULL,
    allocated_days numeric(5,1) NOT NULL,
    carried_forward_days numeric(5,1) DEFAULT 0 NOT NULL,
    used_days numeric(5,1) DEFAULT 0 NOT NULL
);


--
-- Name: hcm_leave_requests; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_leave_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    leave_type_id uuid NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    days numeric(4,1) NOT NULL,
    is_half_day boolean DEFAULT false NOT NULL,
    reason text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    approved_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    decision_notes text
);


--
-- Name: hcm_leave_types; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_leave_types (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    code character varying(10) NOT NULL,
    is_paid boolean DEFAULT true NOT NULL,
    max_days_per_year numeric(5,1),
    carry_forward boolean DEFAULT false NOT NULL,
    is_encashable boolean DEFAULT false NOT NULL
);


--
-- Name: hcm_loans; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_loans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    loan_type character varying(60) NOT NULL,
    principal_amount numeric(14,2) NOT NULL,
    emi_amount numeric(12,2),
    outstanding_balance numeric(14,2) NOT NULL,
    status character varying(12) DEFAULT 'active'::character varying NOT NULL
);


--
-- Name: hcm_offers; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_offers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    application_id uuid NOT NULL,
    offered_ctc numeric(14,2) NOT NULL,
    offer_date date NOT NULL,
    proposed_joining_date date,
    status character varying(12) DEFAULT 'sent'::character varying NOT NULL
);


--
-- Name: hcm_onboarding_tasks; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_onboarding_tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    onboarding_id uuid NOT NULL,
    task character varying(200) NOT NULL,
    assignee_id uuid,
    due_date date,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: hcm_onboardings; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_onboardings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    start_date date NOT NULL,
    status character varying(12) DEFAULT 'in_progress'::character varying NOT NULL
);


--
-- Name: hcm_overtime_requests; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_overtime_requests (
    id uuid NOT NULL,
    employee_id uuid NOT NULL,
    work_date date NOT NULL,
    hours numeric(4,2) NOT NULL,
    reason text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_payroll_runs; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_payroll_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    run_no character varying(30) NOT NULL,
    period_month smallint NOT NULL,
    period_year smallint NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    journal_entry_id uuid,
    payment_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hcm_policy_documents; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_policy_documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    doc_kind character varying(15) DEFAULT 'policy'::character varying NOT NULL,
    name character varying(200) NOT NULL,
    version character varying(20),
    effective_date date,
    acknowledgement_pct numeric(5,2),
    file_url text
);


--
-- Name: hcm_recognitions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_recognitions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    badge character varying(80) NOT NULL,
    reason text,
    given_on date DEFAULT CURRENT_DATE NOT NULL
);


--
-- Name: hcm_salary_components; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_salary_components (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    code character varying(15) NOT NULL,
    component_type character varying(22) NOT NULL,
    calc_type character varying(10) DEFAULT 'fixed'::character varying NOT NULL,
    formula text,
    is_taxable boolean DEFAULT true NOT NULL,
    statutory_code character varying(10),
    gl_account_id uuid
);


--
-- Name: hcm_salary_revision_requests; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_salary_revision_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    current_ctc bigint,
    proposed_ctc bigint,
    reason text,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: hcm_salary_slip_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_salary_slip_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slip_id uuid NOT NULL,
    component_id uuid NOT NULL,
    amount numeric(14,2) NOT NULL
);


--
-- Name: hcm_salary_slips; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_salary_slips (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    payroll_run_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    working_days numeric(4,1) NOT NULL,
    lop_days numeric(4,1) DEFAULT 0 NOT NULL,
    gross_pay numeric(14,2) NOT NULL,
    total_deductions numeric(14,2) NOT NULL,
    net_pay numeric(14,2) NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: hcm_salary_structure_assignments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_salary_structure_assignments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    structure_id uuid NOT NULL,
    from_date date NOT NULL,
    base_amount numeric(14,2) NOT NULL,
    annual_ctc numeric(14,2)
);


--
-- Name: hcm_salary_structure_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_salary_structure_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    structure_id uuid NOT NULL,
    component_id uuid NOT NULL,
    amount numeric(14,2),
    percent_of character varying(15),
    percent numeric(7,3)
);


--
-- Name: hcm_salary_structures; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_salary_structures (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    effective_from date NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: hcm_shift_assignments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_shift_assignments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    shift_id uuid NOT NULL,
    from_date date NOT NULL,
    to_date date
);


--
-- Name: hcm_shifts; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_shifts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    start_time time without time zone NOT NULL,
    end_time time without time zone NOT NULL,
    break_minutes smallint DEFAULT 60 NOT NULL,
    grace_minutes smallint DEFAULT 10 NOT NULL,
    is_night boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: hcm_tax_declarations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_tax_declarations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    fiscal_year character varying(9) NOT NULL,
    tax_regime character varying(10) NOT NULL,
    hra_claimed numeric(12,2) DEFAULT 0,
    section_80c numeric(12,2) DEFAULT 0,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: hcm_training_courses; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_training_courses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    course_type character varying(20) DEFAULT 'internal'::character varying,
    duration_hours numeric(6,1),
    provider character varying(120),
    category character varying(60),
    duration_label character varying(30)
);


--
-- Name: hcm_training_enrollments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_training_enrollments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    course_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    status character varying(15) DEFAULT 'enrolled'::character varying NOT NULL,
    completion_date date,
    score numeric(5,2)
);


--
-- Name: hcm_training_sessions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_training_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    session_date date NOT NULL,
    trainer_name character varying(120),
    is_mandatory boolean DEFAULT false NOT NULL,
    attendee_count integer DEFAULT 0 NOT NULL
);


--
-- Name: hcm_travel_requests; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.hcm_travel_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    employee_id uuid NOT NULL,
    purpose character varying(200),
    destination character varying(150),
    from_date date NOT NULL,
    to_date date NOT NULL,
    travel_mode character varying(40),
    estimated_cost numeric(12,2),
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    current_step smallint DEFAULT 1,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone
);


--
-- Name: mfg_bom_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_bom_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bom_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,4) NOT NULL,
    uom_id uuid,
    rate numeric(18,4),
    scrap_pct numeric(6,3) DEFAULT 0 NOT NULL
);


--
-- Name: mfg_bom_operations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_bom_operations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bom_id uuid NOT NULL,
    seq smallint NOT NULL,
    operation_name character varying(120) NOT NULL,
    work_center_id uuid NOT NULL,
    time_minutes numeric(10,2) NOT NULL,
    operating_cost numeric(14,2)
);


--
-- Name: mfg_boms; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_boms (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    bom_no character varying(30) NOT NULL,
    item_id uuid NOT NULL,
    quantity numeric(18,3) DEFAULT 1 NOT NULL,
    uom_id uuid,
    version smallint DEFAULT 1 NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    total_cost numeric(18,2),
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: mfg_job_card_time_logs; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_job_card_time_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_card_id uuid NOT NULL,
    employee_id uuid,
    from_time timestamp with time zone NOT NULL,
    to_time timestamp with time zone,
    completed_qty numeric(18,3) DEFAULT 0 NOT NULL
);


--
-- Name: mfg_job_cards; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_job_cards (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    jc_no character varying(30) NOT NULL,
    work_order_id uuid NOT NULL,
    bom_operation_id uuid,
    work_center_id uuid NOT NULL,
    seq smallint DEFAULT 1 NOT NULL,
    planned_qty numeric(18,3) NOT NULL,
    completed_qty numeric(18,3) DEFAULT 0 NOT NULL,
    rejected_qty numeric(18,3) DEFAULT 0 NOT NULL,
    assigned_employee_id uuid,
    status character varying(15) DEFAULT 'open'::character varying NOT NULL
);


--
-- Name: mfg_machine_utilization_logs; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_machine_utilization_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    work_center_id uuid NOT NULL,
    log_date date NOT NULL,
    available_minutes numeric(8,1) NOT NULL,
    operated_minutes numeric(8,1) DEFAULT 0 NOT NULL,
    downtime_minutes numeric(8,1) DEFAULT 0 NOT NULL,
    downtime_reason character varying(200)
);


--
-- Name: mfg_production_costings; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_production_costings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    work_order_id uuid NOT NULL,
    material_cost numeric(18,2) DEFAULT 0 NOT NULL,
    labour_cost numeric(18,2) DEFAULT 0 NOT NULL,
    overhead_cost numeric(18,2) DEFAULT 0 NOT NULL,
    total_cost numeric(18,2) DEFAULT 0 NOT NULL,
    per_unit_cost numeric(18,4),
    journal_entry_id uuid
);


--
-- Name: mfg_production_plan_items; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_production_plan_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    production_plan_id uuid NOT NULL,
    item_id uuid NOT NULL,
    bom_id uuid,
    planned_qty numeric(18,3) NOT NULL,
    sales_order_id uuid,
    warehouse_id uuid
);


--
-- Name: mfg_production_plans; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_production_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    plan_no character varying(30) NOT NULL,
    from_date date NOT NULL,
    to_date date NOT NULL,
    status character varying(15) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: mfg_work_centers; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_work_centers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    branch_id uuid,
    capacity_per_hour numeric(12,3),
    hour_rate numeric(12,2) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: mfg_work_orders; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.mfg_work_orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    wo_no character varying(30) NOT NULL,
    item_id uuid NOT NULL,
    bom_id uuid NOT NULL,
    qty_to_produce numeric(18,3) NOT NULL,
    produced_qty numeric(18,3) DEFAULT 0 NOT NULL,
    production_plan_id uuid,
    sales_order_id uuid,
    wip_warehouse_id uuid,
    fg_warehouse_id uuid,
    planned_start date,
    planned_end date,
    actual_start timestamp with time zone,
    actual_end timestamp with time zone,
    status character varying(15) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: plan_capacity_plans; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_capacity_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    work_center_id uuid NOT NULL,
    period_id uuid NOT NULL,
    available_hours numeric(10,2) DEFAULT 0 NOT NULL,
    planned_hours numeric(10,2) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: plan_demand_forecasts; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_demand_forecasts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid,
    period_id uuid NOT NULL,
    forecast_qty numeric(18,3) DEFAULT 0 NOT NULL,
    forecast_method character varying(15) DEFAULT 'manual'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT plan_demand_forecasts_forecast_method_check CHECK (((forecast_method)::text = ANY ((ARRAY['manual'::character varying, 'moving_avg'::character varying, 'statistical'::character varying])::text[])))
);


--
-- Name: plan_material_requirements; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_material_requirements (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    period_id uuid NOT NULL,
    gross_requirement numeric(18,3) DEFAULT 0 NOT NULL,
    scheduled_receipts numeric(18,3) DEFAULT 0 NOT NULL,
    projected_on_hand numeric(18,3) DEFAULT 0 NOT NULL,
    net_requirement numeric(18,3) DEFAULT 0 NOT NULL,
    suggested_qty numeric(18,3) DEFAULT 0 NOT NULL,
    suggested_action character varying(10),
    source_doctype character varying(60),
    source_id uuid,
    converted_to_doctype character varying(60),
    converted_to_id uuid,
    status character varying(10) DEFAULT 'open'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT plan_material_requirements_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'actioned'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT plan_material_requirements_suggested_action_check CHECK (((suggested_action)::text = ANY ((ARRAY['make'::character varying, 'buy'::character varying, 'transfer'::character varying, 'none'::character varying])::text[])))
);


--
-- Name: plan_planning_calendars; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_planning_calendars (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    bucket_type character varying(10) DEFAULT 'monthly'::character varying NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT plan_planning_calendars_bucket_type_check CHECK (((bucket_type)::text = ANY ((ARRAY['weekly'::character varying, 'monthly'::character varying, 'quarterly'::character varying])::text[])))
);


--
-- Name: plan_planning_exceptions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_planning_exceptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    material_requirement_id uuid,
    exception_type character varying(20) NOT NULL,
    item_id uuid,
    work_center_id uuid,
    period_id uuid,
    description text,
    severity character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    status character varying(12) DEFAULT 'open'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT plan_planning_exceptions_exception_type_check CHECK (((exception_type)::text = ANY ((ARRAY['shortage'::character varying, 'excess'::character varying, 'late_supply'::character varying, 'capacity_overload'::character varying])::text[]))),
    CONSTRAINT plan_planning_exceptions_severity_check CHECK (((severity)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying])::text[]))),
    CONSTRAINT plan_planning_exceptions_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'acknowledged'::character varying, 'resolved'::character varying])::text[])))
);


--
-- Name: plan_planning_periods; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_planning_periods (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    calendar_id uuid NOT NULL,
    period_no integer NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    CONSTRAINT plan_planning_periods_dates_check CHECK ((end_date >= start_date))
);


--
-- Name: plan_planning_scenarios; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_planning_scenarios (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    fiscal_year_id uuid,
    name character varying(150) NOT NULL,
    description text,
    scenario_type character varying(15) DEFAULT 'forecast'::character varying NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT plan_planning_scenarios_scenario_type_check CHECK (((scenario_type)::text = ANY ((ARRAY['budget'::character varying, 'forecast'::character varying, 'whatif'::character varying])::text[]))),
    CONSTRAINT plan_planning_scenarios_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'active'::character varying, 'archived'::character varying])::text[])))
);


--
-- Name: plan_planning_versions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_planning_versions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    scenario_id uuid NOT NULL,
    version_no smallint NOT NULL,
    name character varying(120),
    is_baseline boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: plan_resource_plans; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_resource_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    department_id uuid NOT NULL,
    designation_id uuid,
    period_id uuid NOT NULL,
    required_headcount numeric(8,2) DEFAULT 0 NOT NULL,
    available_headcount numeric(8,2) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: plan_sales_forecasts; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.plan_sales_forecasts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    scenario_id uuid NOT NULL,
    version_id uuid NOT NULL,
    item_id uuid NOT NULL,
    customer_id uuid,
    period_id uuid NOT NULL,
    forecast_qty numeric(18,3) DEFAULT 0 NOT NULL,
    forecast_revenue numeric(18,2) DEFAULT 0 NOT NULL,
    currency_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: pm_issues; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_issues (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid,
    code character varying(30) NOT NULL,
    title character varying(200) NOT NULL,
    description text,
    issue_type character varying(20) DEFAULT 'other'::character varying NOT NULL,
    severity character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    status character varying(12) DEFAULT 'open'::character varying NOT NULL,
    reported_by uuid,
    assigned_to uuid,
    reported_date date DEFAULT CURRENT_DATE NOT NULL,
    resolved_date date,
    resolution_notes text,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_issues_issue_type_check CHECK (((issue_type)::text = ANY ((ARRAY['bug'::character varying, 'blocker'::character varying, 'change_request'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT pm_issues_severity_check CHECK (((severity)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT pm_issues_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'in_progress'::character varying, 'resolved'::character varying, 'closed'::character varying])::text[])))
);


--
-- Name: pm_meeting_participants; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_meeting_participants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    meeting_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    attendance_status character varying(12) DEFAULT 'invited'::character varying NOT NULL,
    CONSTRAINT pm_meeting_participants_attendance_status_check CHECK (((attendance_status)::text = ANY ((ARRAY['invited'::character varying, 'accepted'::character varying, 'declined'::character varying, 'attended'::character varying, 'absent'::character varying])::text[])))
);


--
-- Name: pm_meetings; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_meetings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid,
    title character varying(200) NOT NULL,
    description text,
    meeting_date date NOT NULL,
    start_time time without time zone NOT NULL,
    end_time time without time zone,
    location character varying(200),
    organizer_employee_id uuid,
    status character varying(12) DEFAULT 'scheduled'::character varying NOT NULL,
    minutes text,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_meetings_status_check CHECK (((status)::text = ANY ((ARRAY['scheduled'::character varying, 'completed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: pm_milestones; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_milestones (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    phase_id uuid,
    name character varying(150) NOT NULL,
    description text,
    due_date date NOT NULL,
    completed_date date,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_milestones_status_check CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'achieved'::character varying, 'missed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: pm_project_budgets; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_budgets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    fiscal_year_id uuid,
    account_id uuid,
    budget_type character varying(10) DEFAULT 'opex'::character varying NOT NULL,
    currency_id uuid,
    planned_amount numeric(18,2) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_project_budgets_budget_type_check CHECK (((budget_type)::text = ANY ((ARRAY['capex'::character varying, 'opex'::character varying])::text[])))
);


--
-- Name: pm_project_categories; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_categories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    parent_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: pm_project_expenses; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_expenses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid,
    employee_id uuid,
    account_id uuid,
    currency_id uuid,
    expense_date date NOT NULL,
    amount numeric(18,2) NOT NULL,
    description character varying(255),
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    approved_by uuid,
    journal_entry_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_project_expenses_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'submitted'::character varying, 'approved'::character varying, 'rejected'::character varying, 'paid'::character varying])::text[])))
);


--
-- Name: pm_project_members; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    project_role_id uuid,
    allocation_pct numeric(5,2) DEFAULT 100,
    start_date date NOT NULL,
    end_date date,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_project_members_alloc_check CHECK (((allocation_pct > (0)::numeric) AND (allocation_pct <= (100)::numeric)))
);


--
-- Name: pm_project_phases; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_phases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    description text,
    sequence smallint NOT NULL,
    planned_start_date date,
    planned_end_date date,
    actual_start_date date,
    actual_end_date date,
    status character varying(15) DEFAULT 'not_started'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_project_phases_status_check CHECK (((status)::text = ANY ((ARRAY['not_started'::character varying, 'in_progress'::character varying, 'completed'::character varying, 'skipped'::character varying])::text[])))
);


--
-- Name: pm_project_roles; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_roles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    description text,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: pm_project_status_history; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_status_history (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    from_status character varying(15),
    to_status character varying(15) NOT NULL,
    changed_by uuid,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    remarks text
);


--
-- Name: pm_project_tags; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_tags (
    project_id uuid NOT NULL,
    tag_id uuid NOT NULL
);


--
-- Name: pm_project_template_phases; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_template_phases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    template_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    sequence smallint NOT NULL,
    default_duration_days integer
);


--
-- Name: pm_project_templates; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_project_templates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    description text,
    category_id uuid,
    default_duration_days integer,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: pm_projects; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_projects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid,
    code character varying(30) NOT NULL,
    name character varying(200) NOT NULL,
    description text,
    category_id uuid,
    template_id uuid,
    customer_id uuid,
    cost_center_id uuid,
    project_manager_id uuid,
    currency_id uuid,
    is_billable boolean DEFAULT false NOT NULL,
    priority character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    status character varying(15) DEFAULT 'draft'::character varying NOT NULL,
    planned_start_date date,
    planned_end_date date,
    actual_start_date date,
    actual_end_date date,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_projects_dates_check CHECK (((planned_end_date IS NULL) OR (planned_start_date IS NULL) OR (planned_end_date >= planned_start_date))),
    CONSTRAINT pm_projects_priority_check CHECK (((priority)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT pm_projects_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'planning'::character varying, 'active'::character varying, 'on_hold'::character varying, 'completed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: pm_resource_allocations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_resource_allocations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    project_role_id uuid,
    allocation_pct numeric(5,2) NOT NULL,
    start_date date NOT NULL,
    end_date date,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_resource_allocations_alloc_chk CHECK (((allocation_pct > (0)::numeric) AND (allocation_pct <= (100)::numeric))),
    CONSTRAINT pm_resource_allocations_dates_chk CHECK (((end_date IS NULL) OR (end_date >= start_date)))
);


--
-- Name: pm_risks; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_risks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    code character varying(30) NOT NULL,
    title character varying(200) NOT NULL,
    description text,
    category character varying(20) DEFAULT 'other'::character varying NOT NULL,
    probability character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    impact character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    mitigation_plan text,
    owner_employee_id uuid,
    status character varying(12) DEFAULT 'identified'::character varying NOT NULL,
    identified_date date DEFAULT CURRENT_DATE NOT NULL,
    closed_date date,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_risks_category_check CHECK (((category)::text = ANY ((ARRAY['schedule'::character varying, 'cost'::character varying, 'scope'::character varying, 'resource'::character varying, 'technical'::character varying, 'external'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT pm_risks_impact_check CHECK (((impact)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying])::text[]))),
    CONSTRAINT pm_risks_probability_check CHECK (((probability)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying])::text[]))),
    CONSTRAINT pm_risks_status_check CHECK (((status)::text = ANY ((ARRAY['identified'::character varying, 'mitigating'::character varying, 'occurred'::character varying, 'closed'::character varying])::text[])))
);


--
-- Name: pm_tags; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_tags (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(60) NOT NULL,
    color character varying(7),
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: pm_task_assignments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_task_assignments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    assignment_role character varying(15) DEFAULT 'assignee'::character varying NOT NULL,
    assigned_by uuid,
    assigned_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_task_assignments_assignment_role_check CHECK (((assignment_role)::text = ANY ((ARRAY['assignee'::character varying, 'reviewer'::character varying, 'collaborator'::character varying])::text[])))
);


--
-- Name: pm_task_checklist_items; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_task_checklist_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    description character varying(255) NOT NULL,
    sequence smallint DEFAULT 1 NOT NULL,
    is_checked boolean DEFAULT false NOT NULL,
    checked_by uuid,
    checked_at timestamp with time zone
);


--
-- Name: pm_task_dependencies; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_task_dependencies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    depends_on_task_id uuid NOT NULL,
    dependency_type character varying(20) DEFAULT 'finish_to_start'::character varying NOT NULL,
    CONSTRAINT pm_task_dependencies_dependency_type_check CHECK (((dependency_type)::text = ANY ((ARRAY['finish_to_start'::character varying, 'start_to_start'::character varying, 'finish_to_finish'::character varying, 'start_to_finish'::character varying])::text[]))),
    CONSTRAINT pm_task_dependencies_no_self CHECK ((task_id <> depends_on_task_id))
);


--
-- Name: pm_tasks; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    phase_id uuid,
    milestone_id uuid,
    parent_task_id uuid,
    code character varying(30) NOT NULL,
    title character varying(200) NOT NULL,
    description text,
    task_type character varying(15) DEFAULT 'task'::character varying NOT NULL,
    priority character varying(10) DEFAULT 'medium'::character varying NOT NULL,
    status character varying(15) DEFAULT 'todo'::character varying NOT NULL,
    planned_start_date date,
    due_date date,
    actual_start_date timestamp with time zone,
    actual_end_date timestamp with time zone,
    estimated_hours numeric(8,2),
    actual_hours numeric(8,2) DEFAULT 0 NOT NULL,
    progress_pct smallint DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_tasks_no_self_ref CHECK (((parent_task_id IS NULL) OR (parent_task_id <> id))),
    CONSTRAINT pm_tasks_priority_check CHECK (((priority)::text = ANY ((ARRAY['low'::character varying, 'medium'::character varying, 'high'::character varying, 'critical'::character varying])::text[]))),
    CONSTRAINT pm_tasks_progress_chk CHECK (((progress_pct >= 0) AND (progress_pct <= 100))),
    CONSTRAINT pm_tasks_status_check CHECK (((status)::text = ANY ((ARRAY['todo'::character varying, 'in_progress'::character varying, 'in_review'::character varying, 'blocked'::character varying, 'done'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT pm_tasks_task_type_check CHECK (((task_type)::text = ANY ((ARRAY['epic'::character varying, 'task'::character varying, 'subtask'::character varying, 'bug'::character varying])::text[])))
);


--
-- Name: pm_time_entries; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_time_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    timesheet_id uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id uuid,
    entry_date date NOT NULL,
    start_time time without time zone,
    end_time time without time zone,
    hours numeric(5,2) NOT NULL,
    description text,
    category character varying(40),
    is_billable boolean DEFAULT true NOT NULL,
    billing_rate numeric(12,2),
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL,
    approver_id uuid,
    decision_notes text,
    decided_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT pm_time_entries_hours CHECK (((hours > (0)::numeric) AND (hours <= (24)::numeric))),
    CONSTRAINT pm_time_entries_status_check CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying])::text[])))
);


--
-- Name: pm_timesheets; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.pm_timesheets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    week_start_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    total_hours numeric(6,2) DEFAULT 0 NOT NULL,
    billable_hours numeric(6,2) DEFAULT 0 NOT NULL,
    submitted_at timestamp with time zone,
    approved_by uuid,
    approved_at timestamp with time zone,
    decision_notes text,
    decided_at timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT pm_timesheets_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'submitted'::character varying, 'approved'::character varying, 'rejected'::character varying])::text[])))
);


--
-- Name: retail_cash_movements; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_cash_movements (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid NOT NULL,
    movement_type character varying(10) NOT NULL,
    amount numeric(14,2) NOT NULL,
    reason character varying(200),
    recorded_by uuid,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT retail_cash_movements_amount_check CHECK ((amount > (0)::numeric)),
    CONSTRAINT retail_cash_movements_movement_type_check CHECK (((movement_type)::text = ANY ((ARRAY['cash_in'::character varying, 'cash_out'::character varying, 'drop'::character varying])::text[])))
);


--
-- Name: retail_cash_reconciliations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_cash_reconciliations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    daily_closing_id uuid NOT NULL,
    expected_amount numeric(14,2) NOT NULL,
    counted_amount numeric(14,2) NOT NULL,
    variance numeric(14,2) NOT NULL,
    counted_by uuid,
    counted_at timestamp with time zone DEFAULT now() NOT NULL,
    remarks character varying(255)
);


--
-- Name: retail_coupons; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_coupons (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    promotion_id uuid,
    code character varying(30) NOT NULL,
    description character varying(255),
    discount_type character varying(10) DEFAULT 'pct'::character varying NOT NULL,
    discount_value numeric(10,2) NOT NULL,
    valid_from date NOT NULL,
    valid_to date NOT NULL,
    usage_limit integer,
    used_count integer DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_coupons_dates_check CHECK ((valid_to >= valid_from)),
    CONSTRAINT retail_coupons_discount_type_check CHECK (((discount_type)::text = ANY ((ARRAY['pct'::character varying, 'fixed'::character varying])::text[])))
);


--
-- Name: retail_daily_closings; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_daily_closings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    store_id uuid NOT NULL,
    closing_date date NOT NULL,
    session_count integer DEFAULT 0 NOT NULL,
    total_sales numeric(14,2) DEFAULT 0 NOT NULL,
    total_returns numeric(14,2) DEFAULT 0 NOT NULL,
    total_tax_collected numeric(14,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    reconciled_by uuid,
    reconciled_at timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_daily_closings_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'reconciled'::character varying])::text[])))
);


--
-- Name: retail_gift_card_transactions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_gift_card_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    gift_card_id uuid NOT NULL,
    pos_sale_id uuid,
    txn_type character varying(10) NOT NULL,
    amount numeric(12,2) NOT NULL,
    txn_date timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT retail_gift_card_transactions_txn_type_check CHECK (((txn_type)::text = ANY ((ARRAY['issue'::character varying, 'redeem'::character varying, 'reload'::character varying, 'adjust'::character varying])::text[])))
);


--
-- Name: retail_gift_cards; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_gift_cards (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    card_no character varying(30) NOT NULL,
    issued_to_customer_id uuid,
    issued_date date DEFAULT CURRENT_DATE NOT NULL,
    expiry_date date,
    initial_balance numeric(12,2) NOT NULL,
    current_balance numeric(12,2) NOT NULL,
    status character varying(10) DEFAULT 'active'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_gift_cards_balance_chk CHECK (((current_balance >= (0)::numeric) AND (current_balance <= initial_balance))),
    CONSTRAINT retail_gift_cards_status_check CHECK (((status)::text = ANY ((ARRAY['active'::character varying, 'redeemed'::character varying, 'expired'::character varying, 'blocked'::character varying])::text[])))
);


--
-- Name: retail_loyalty_members; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_loyalty_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    customer_id uuid NOT NULL,
    loyalty_code character varying(30) NOT NULL,
    enrolled_date date DEFAULT CURRENT_DATE NOT NULL,
    tier character varying(15) DEFAULT 'standard'::character varying NOT NULL,
    points_balance numeric(12,2) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_loyalty_members_tier_check CHECK (((tier)::text = ANY ((ARRAY['standard'::character varying, 'silver'::character varying, 'gold'::character varying, 'platinum'::character varying])::text[])))
);


--
-- Name: retail_loyalty_transactions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_loyalty_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    loyalty_member_id uuid NOT NULL,
    pos_sale_id uuid,
    txn_type character varying(10) NOT NULL,
    points numeric(12,2) NOT NULL,
    txn_date timestamp with time zone DEFAULT now() NOT NULL,
    notes character varying(255),
    CONSTRAINT retail_loyalty_transactions_txn_type_check CHECK (((txn_type)::text = ANY ((ARRAY['earn'::character varying, 'redeem'::character varying, 'expire'::character varying, 'adjust'::character varying])::text[])))
);


--
-- Name: retail_payment_methods; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_payment_methods (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(60) NOT NULL,
    method_type character varying(15) NOT NULL,
    gl_account_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_payment_methods_method_type_check CHECK (((method_type)::text = ANY ((ARRAY['cash'::character varying, 'card'::character varying, 'upi'::character varying, 'wallet'::character varying, 'gift_card'::character varying, 'store_credit'::character varying, 'other'::character varying])::text[])))
);


--
-- Name: retail_pos_exchanges; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_pos_exchanges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    original_sale_id uuid NOT NULL,
    return_id uuid,
    new_sale_id uuid,
    exchange_datetime timestamp with time zone DEFAULT now() NOT NULL,
    notes character varying(255),
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_pos_exchanges_no_self_check CHECK (((new_sale_id IS NULL) OR (new_sale_id <> original_sale_id)))
);


--
-- Name: retail_pos_payments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_pos_payments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pos_sale_id uuid NOT NULL,
    payment_method_id uuid NOT NULL,
    amount numeric(14,2) NOT NULL,
    reference_no character varying(60),
    CONSTRAINT retail_pos_payments_amount_chk CHECK ((amount > (0)::numeric))
);


--
-- Name: retail_pos_profiles; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_pos_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    store_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    selling_warehouse_id uuid NOT NULL,
    cash_account_id uuid,
    default_customer_id uuid,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: retail_pos_return_items; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_pos_return_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pos_return_id uuid NOT NULL,
    pos_sale_item_id uuid NOT NULL,
    qty numeric(14,3) NOT NULL,
    amount numeric(14,2) NOT NULL,
    CONSTRAINT retail_pos_return_items_qty_chk CHECK ((qty > (0)::numeric))
);


--
-- Name: retail_pos_returns; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_pos_returns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    original_sale_id uuid NOT NULL,
    return_datetime timestamp with time zone DEFAULT now() NOT NULL,
    reason character varying(255),
    total_amount numeric(14,2) NOT NULL,
    journal_entry_id uuid,
    status character varying(12) DEFAULT 'completed'::character varying NOT NULL,
    processed_by uuid,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_pos_returns_status_check CHECK (((status)::text = ANY ((ARRAY['completed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: retail_pos_sale_items; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_pos_sale_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    pos_sale_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    qty numeric(14,3) NOT NULL,
    rate numeric(14,2) NOT NULL,
    discount_pct numeric(6,3) DEFAULT 0 NOT NULL,
    tax_template_id uuid,
    amount numeric(14,2) NOT NULL,
    CONSTRAINT retail_pos_sale_items_qty_chk CHECK ((qty > (0)::numeric))
);


--
-- Name: retail_pos_sales; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_pos_sales (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    store_id uuid NOT NULL,
    session_id uuid NOT NULL,
    sale_no character varying(30) NOT NULL,
    customer_id uuid,
    cashier_employee_id uuid NOT NULL,
    sale_datetime timestamp with time zone DEFAULT now() NOT NULL,
    subtotal numeric(14,2) DEFAULT 0 NOT NULL,
    discount_total numeric(14,2) DEFAULT 0 NOT NULL,
    tax_total numeric(14,2) DEFAULT 0 NOT NULL,
    grand_total numeric(14,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'completed'::character varying NOT NULL,
    journal_entry_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_pos_sales_status_check CHECK (((status)::text = ANY ((ARRAY['completed'::character varying, 'voided'::character varying, 'returned'::character varying, 'partially_returned'::character varying])::text[])))
);


--
-- Name: retail_pos_sessions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_pos_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    terminal_id uuid NOT NULL,
    cashier_employee_id uuid NOT NULL,
    opened_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    opening_cash_amount numeric(14,2) DEFAULT 0 NOT NULL,
    expected_closing_amount numeric(14,2),
    closing_cash_amount numeric(14,2),
    cash_variance numeric(14,2),
    status character varying(10) DEFAULT 'open'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_pos_sessions_dates_check CHECK (((closed_at IS NULL) OR (closed_at >= opened_at))),
    CONSTRAINT retail_pos_sessions_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'closed'::character varying])::text[])))
);


--
-- Name: retail_pos_terminals; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_pos_terminals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    store_id uuid NOT NULL,
    pos_profile_id uuid NOT NULL,
    terminal_code character varying(20) NOT NULL,
    name character varying(120) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: retail_price_rules; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_price_rules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    promotion_id uuid,
    item_id uuid,
    item_group_id uuid,
    min_qty numeric(10,3) DEFAULT 1 NOT NULL,
    discount_type character varying(10) DEFAULT 'pct'::character varying NOT NULL,
    discount_value numeric(10,2) NOT NULL,
    priority smallint DEFAULT 1 NOT NULL,
    valid_from date NOT NULL,
    valid_to date NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_price_rules_dates_check CHECK ((valid_to >= valid_from)),
    CONSTRAINT retail_price_rules_discount_type_check CHECK (((discount_type)::text = ANY ((ARRAY['pct'::character varying, 'fixed'::character varying])::text[]))),
    CONSTRAINT retail_price_rules_item_chk CHECK (((item_id IS NOT NULL) OR (item_group_id IS NOT NULL)))
);


--
-- Name: retail_promotions; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_promotions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    description text,
    promotion_type character varying(20) DEFAULT 'pct_off'::character varying NOT NULL,
    valid_from date NOT NULL,
    valid_to date NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    CONSTRAINT retail_promotions_dates_check CHECK ((valid_to >= valid_from)),
    CONSTRAINT retail_promotions_promotion_type_check CHECK (((promotion_type)::text = ANY ((ARRAY['buy_x_get_y'::character varying, 'pct_off'::character varying, 'fixed_off'::character varying])::text[])))
);


--
-- Name: retail_stores; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.retail_stores (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    branch_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    code character varying(20) NOT NULL,
    name character varying(150) NOT NULL,
    store_format character varying(15) DEFAULT 'standard'::character varying NOT NULL,
    manager_employee_id uuid,
    opening_date date,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT retail_stores_store_format_check CHECK (((store_format)::text = ANY ((ARRAY['flagship'::character varying, 'standard'::character varying, 'kiosk'::character varying, 'outlet'::character varying, 'online'::character varying])::text[])))
);


--
-- Name: scm_batches; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_batches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    item_id uuid NOT NULL,
    batch_no character varying(40) NOT NULL,
    mfg_date date,
    expiry_date date
);


--
-- Name: scm_bins; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_bins (
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    actual_qty numeric(18,3) DEFAULT 0 NOT NULL,
    reserved_qty numeric(18,3) DEFAULT 0 NOT NULL,
    ordered_qty numeric(18,3) DEFAULT 0 NOT NULL,
    valuation_rate numeric(18,4) DEFAULT 0 NOT NULL
);


--
-- Name: scm_delivery_note_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_delivery_note_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    delivery_note_id uuid NOT NULL,
    so_line_id uuid,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    batch_id uuid
);


--
-- Name: scm_delivery_notes; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_delivery_notes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    dn_no character varying(30) NOT NULL,
    customer_id uuid NOT NULL,
    sales_order_id uuid,
    dn_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_eway_bills; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_eway_bills (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    sales_invoice_id uuid,
    purchase_invoice_id uuid,
    ewb_no character varying NOT NULL,
    ewb_date timestamp with time zone NOT NULL,
    valid_until timestamp with time zone NOT NULL,
    vehicle_no character varying,
    transporter_id character varying,
    transport_mode character varying DEFAULT 'road'::character varying NOT NULL,
    distance_km integer NOT NULL,
    status character varying DEFAULT 'active'::character varying NOT NULL,
    is_mock boolean DEFAULT true NOT NULL,
    cancelled_at timestamp with time zone,
    cancellation_reason text
);


--
-- Name: scm_leads; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_leads (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(150) NOT NULL,
    company_name character varying(200),
    email character varying(150),
    phone character varying(20),
    source character varying(40),
    status character varying(15) DEFAULT 'new'::character varying NOT NULL,
    owner_id uuid,
    converted_customer_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_opportunities; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_opportunities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(200) NOT NULL,
    lead_id uuid,
    customer_id uuid,
    stage character varying(15) DEFAULT 'prospecting'::character varying NOT NULL,
    amount numeric(18,2),
    probability_pct smallint DEFAULT 50,
    expected_close_date date,
    owner_id uuid
);


--
-- Name: scm_purchase_invoice_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_purchase_invoice_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    purchase_invoice_id uuid NOT NULL,
    po_line_id uuid,
    item_id uuid NOT NULL,
    hsn_sac_code character varying(10),
    qty numeric(18,3) NOT NULL,
    rate numeric(18,2) NOT NULL,
    tax_template_id uuid,
    cgst_amount numeric(18,2) DEFAULT 0,
    sgst_amount numeric(18,2) DEFAULT 0,
    igst_amount numeric(18,2) DEFAULT 0,
    amount numeric(18,2) NOT NULL,
    expense_account_id uuid
);


--
-- Name: scm_purchase_invoices; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_purchase_invoices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    pi_no character varying(30) NOT NULL,
    supplier_bill_no character varying(60),
    supplier_id uuid NOT NULL,
    purchase_order_id uuid,
    purchase_receipt_id uuid,
    invoice_date date NOT NULL,
    due_date date,
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    cgst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    sgst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    igst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    outstanding_amount numeric(18,2) DEFAULT 0 NOT NULL,
    itc_eligible boolean DEFAULT true NOT NULL,
    status character varying(18) DEFAULT 'draft'::character varying NOT NULL,
    journal_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    exchange_rate numeric(18,6) DEFAULT 1 NOT NULL
);


--
-- Name: scm_purchase_order_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_purchase_order_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    purchase_order_id uuid NOT NULL,
    pr_line_id uuid,
    item_id uuid NOT NULL,
    warehouse_id uuid,
    qty numeric(18,3) NOT NULL,
    received_qty numeric(18,3) DEFAULT 0 NOT NULL,
    billed_qty numeric(18,3) DEFAULT 0 NOT NULL,
    uom_id uuid,
    rate numeric(18,2) NOT NULL,
    tax_template_id uuid,
    amount numeric(18,2) NOT NULL
);


--
-- Name: scm_purchase_orders; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_purchase_orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    po_no character varying(30) NOT NULL,
    supplier_id uuid NOT NULL,
    supplier_quotation_id uuid,
    order_date date NOT NULL,
    expected_date date,
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    tax_total numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_purchase_receipt_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_purchase_receipt_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    purchase_receipt_id uuid NOT NULL,
    po_line_id uuid,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    accepted_qty numeric(18,3) NOT NULL,
    rejected_qty numeric(18,3) DEFAULT 0 NOT NULL,
    batch_id uuid
);


--
-- Name: scm_purchase_receipts; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_purchase_receipts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    grn_no character varying(30) NOT NULL,
    supplier_id uuid NOT NULL,
    purchase_order_id uuid,
    receipt_date date NOT NULL,
    qc_status character varying(12) DEFAULT 'pending'::character varying,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_purchase_request_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_purchase_request_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    purchase_request_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    uom_id uuid,
    warehouse_id uuid
);


--
-- Name: scm_purchase_requests; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_purchase_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    pr_no character varying(30) NOT NULL,
    requested_by uuid,
    department_id uuid,
    required_by date,
    source character varying(12) DEFAULT 'manual'::character varying NOT NULL,
    production_plan_id uuid,
    status character varying(15) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_purchase_returns; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_purchase_returns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    return_no character varying(30) NOT NULL,
    purchase_invoice_id uuid NOT NULL,
    return_date date NOT NULL,
    reason text,
    total_amount numeric(18,2) NOT NULL,
    journal_entry_id uuid,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: scm_quotation_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_quotation_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    quotation_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    uom_id uuid,
    rate numeric(18,2) NOT NULL,
    discount_pct numeric(6,3) DEFAULT 0,
    tax_template_id uuid,
    amount numeric(18,2) NOT NULL
);


--
-- Name: scm_quotations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_quotations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    quotation_no character varying(30) NOT NULL,
    customer_id uuid NOT NULL,
    opportunity_id uuid,
    quotation_date date NOT NULL,
    valid_till date,
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    tax_total numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_rfq_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_rfq_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    rfq_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL
);


--
-- Name: scm_rfq_suppliers; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_rfq_suppliers (
    rfq_id uuid NOT NULL,
    supplier_id uuid NOT NULL,
    sent_at timestamp with time zone,
    status character varying(12) DEFAULT 'pending'::character varying NOT NULL
);


--
-- Name: scm_rfqs; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_rfqs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    rfq_no character varying(30) NOT NULL,
    rfq_date date NOT NULL,
    purchase_request_id uuid,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: scm_sales_invoice_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_sales_invoice_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sales_invoice_id uuid NOT NULL,
    so_line_id uuid,
    item_id uuid NOT NULL,
    hsn_sac_code character varying(10),
    qty numeric(18,3) NOT NULL,
    rate numeric(18,2) NOT NULL,
    discount_pct numeric(6,3) DEFAULT 0,
    tax_template_id uuid,
    cgst_amount numeric(18,2) DEFAULT 0,
    sgst_amount numeric(18,2) DEFAULT 0,
    igst_amount numeric(18,2) DEFAULT 0,
    amount numeric(18,2) NOT NULL,
    income_account_id uuid
);


--
-- Name: scm_sales_invoices; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_sales_invoices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    invoice_no character varying(30) NOT NULL,
    customer_id uuid NOT NULL,
    sales_order_id uuid,
    delivery_note_id uuid,
    invoice_date date NOT NULL,
    due_date date,
    place_of_supply character varying(2),
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    cgst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    sgst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    igst_amount numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    outstanding_amount numeric(18,2) DEFAULT 0 NOT NULL,
    einvoice_irn character varying(64),
    eway_bill_no character varying(20),
    status character varying(18) DEFAULT 'draft'::character varying NOT NULL,
    journal_entry_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    exchange_rate numeric(18,6) DEFAULT 1 NOT NULL,
    place_of_supply_state_code character varying(2),
    supply_type character varying DEFAULT 'B2B'::character varying NOT NULL,
    buyer_gstin_snapshot character varying,
    seller_gstin_snapshot character varying,
    irn character varying(64),
    irn_ack_no character varying,
    irn_ack_date timestamp with time zone,
    signed_qr_code text,
    irn_cancelled boolean DEFAULT false NOT NULL,
    irn_cancelled_at timestamp with time zone,
    irn_cancellation_reason text,
    is_mock_irn boolean DEFAULT true NOT NULL
);


--
-- Name: scm_sales_order_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_sales_order_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sales_order_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid,
    qty numeric(18,3) NOT NULL,
    delivered_qty numeric(18,3) DEFAULT 0 NOT NULL,
    invoiced_qty numeric(18,3) DEFAULT 0 NOT NULL,
    uom_id uuid,
    rate numeric(18,2) NOT NULL,
    discount_pct numeric(6,3) DEFAULT 0,
    tax_template_id uuid,
    amount numeric(18,2) NOT NULL
);


--
-- Name: scm_sales_orders; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_sales_orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    order_no character varying(30) NOT NULL,
    customer_id uuid NOT NULL,
    quotation_id uuid,
    order_date date NOT NULL,
    delivery_date date,
    billing_address_id uuid,
    shipping_address_id uuid,
    currency_id uuid,
    subtotal numeric(18,2) DEFAULT 0 NOT NULL,
    tax_total numeric(18,2) DEFAULT 0 NOT NULL,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    advance_paid numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(20) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_sales_returns; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_sales_returns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    return_no character varying(30) NOT NULL,
    sales_invoice_id uuid NOT NULL,
    return_date date NOT NULL,
    reason text,
    total_amount numeric(18,2) NOT NULL,
    restock boolean DEFAULT true NOT NULL,
    journal_entry_id uuid,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL
);


--
-- Name: scm_serial_nos; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_serial_nos (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    item_id uuid NOT NULL,
    serial_no character varying(60) NOT NULL,
    batch_id uuid,
    warehouse_id uuid,
    status character varying(15) DEFAULT 'in_stock'::character varying NOT NULL
);


--
-- Name: scm_shipments; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_shipments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    shipment_no character varying(30) NOT NULL,
    delivery_note_id uuid,
    transporter character varying(150),
    vehicle_no character varying(20),
    lr_no character varying(40),
    driver_name character varying(100),
    driver_phone character varying(20),
    eway_bill_no character varying(20),
    dispatch_datetime timestamp with time zone,
    delivered_datetime timestamp with time zone,
    status character varying(15) DEFAULT 'planned'::character varying NOT NULL
);


--
-- Name: scm_stock_entries; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_stock_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    entry_no character varying(30) NOT NULL,
    purpose character varying(20) NOT NULL,
    work_order_id uuid,
    entry_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_stock_entry_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_stock_entry_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    stock_entry_id uuid NOT NULL,
    item_id uuid NOT NULL,
    source_warehouse_id uuid,
    target_warehouse_id uuid,
    qty numeric(18,3) NOT NULL,
    rate numeric(18,4),
    batch_id uuid
);


--
-- Name: scm_stock_ledger_entries; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_stock_ledger_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    item_id uuid NOT NULL,
    warehouse_id uuid NOT NULL,
    posting_datetime timestamp with time zone DEFAULT now() NOT NULL,
    qty_change numeric(18,3) NOT NULL,
    valuation_rate numeric(18,4),
    stock_value_change numeric(18,2),
    batch_id uuid,
    source_doctype character varying(60) NOT NULL,
    source_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_stock_transfer_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_stock_transfer_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    stock_transfer_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    batch_id uuid
);


--
-- Name: scm_stock_transfers; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_stock_transfers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    transfer_no character varying(30) NOT NULL,
    from_warehouse_id uuid NOT NULL,
    to_warehouse_id uuid NOT NULL,
    transfer_date date NOT NULL,
    status character varying(12) DEFAULT 'draft'::character varying NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: scm_supplier_quotation_lines; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_supplier_quotation_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sq_id uuid NOT NULL,
    item_id uuid NOT NULL,
    qty numeric(18,3) NOT NULL,
    rate numeric(18,2) NOT NULL
);


--
-- Name: scm_supplier_quotations; Type: TABLE; Schema: acme; Owner: -
--

CREATE TABLE acme.scm_supplier_quotations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    sq_no character varying(30) NOT NULL,
    supplier_id uuid NOT NULL,
    rfq_id uuid,
    quote_date date NOT NULL,
    valid_till date,
    grand_total numeric(18,2) DEFAULT 0 NOT NULL,
    status character varying(12) DEFAULT 'received'::character varying NOT NULL
);


--
-- Name: admin_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying(150) NOT NULL,
    password_hash text NOT NULL,
    full_name character varying(150),
    role character varying(30) DEFAULT 'superadmin'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: currencies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.currencies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    code character varying(3) NOT NULL,
    name character varying(50) NOT NULL,
    symbol character varying(5) NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: exchange_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.exchange_rates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    from_currency_id uuid NOT NULL,
    to_currency_id uuid NOT NULL,
    rate numeric(18,6) NOT NULL,
    effective_date date NOT NULL
);


--
-- Name: menu_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.menu_items (
    id uuid NOT NULL,
    module_id uuid NOT NULL,
    key character varying(40) NOT NULL,
    label character varying(80) NOT NULL,
    sort_order smallint DEFAULT 0 NOT NULL
);


--
-- Name: modules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.modules (
    id uuid NOT NULL,
    key character varying(40) NOT NULL,
    name character varying(80) NOT NULL,
    icon character varying(40),
    sort_order smallint DEFAULT 0 NOT NULL,
    is_core boolean DEFAULT false NOT NULL
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    version character varying(60) NOT NULL,
    name character varying(200) NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    applied_by character varying(100) DEFAULT CURRENT_USER NOT NULL,
    execution_ms integer
);


--
-- Name: subscription_plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscription_plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(80) NOT NULL,
    code character varying(20) NOT NULL,
    max_users integer,
    max_companies integer,
    modules text[] DEFAULT '{}'::text[],
    price_monthly numeric(10,2),
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: tenant_modules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_modules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_slug character varying(60) NOT NULL,
    module_code character varying(20) NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    enabled_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tenants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    slug character varying(60) NOT NULL,
    name character varying(150) NOT NULL,
    legal_name character varying(200),
    gstin character varying(15),
    pan character varying(10),
    cin character varying(25),
    logo_url text,
    email_domain character varying(150),
    industry character varying(100),
    country character varying(80) DEFAULT 'India'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying(150) NOT NULL,
    password_hash text NOT NULL,
    tenant_slug character varying(60) NOT NULL,
    company_id uuid NOT NULL,
    role character varying(60) NOT NULL,
    full_name character varying(150),
    is_active boolean DEFAULT true NOT NULL,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    employee_id uuid
);


--
-- Name: core_activity_logs core_activity_logs_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_activity_logs
    ADD CONSTRAINT core_activity_logs_pkey PRIMARY KEY (id);


--
-- Name: core_addresses core_addresses_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_addresses
    ADD CONSTRAINT core_addresses_pkey PRIMARY KEY (id);


--
-- Name: core_approval_actions core_approval_actions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_approval_actions
    ADD CONSTRAINT core_approval_actions_pkey PRIMARY KEY (id);


--
-- Name: core_approval_requests core_approval_requests_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_approval_requests
    ADD CONSTRAINT core_approval_requests_pkey PRIMARY KEY (id);


--
-- Name: core_approval_workflow_steps core_approval_workflow_steps_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_approval_workflow_steps
    ADD CONSTRAINT core_approval_workflow_steps_pkey PRIMARY KEY (id);


--
-- Name: core_approval_workflows core_approval_workflows_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_approval_workflows
    ADD CONSTRAINT core_approval_workflows_pkey PRIMARY KEY (id);


--
-- Name: core_attachments core_attachments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_attachments
    ADD CONSTRAINT core_attachments_pkey PRIMARY KEY (id);


--
-- Name: core_audit_logs core_audit_logs_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_audit_logs
    ADD CONSTRAINT core_audit_logs_pkey PRIMARY KEY (id);


--
-- Name: core_bands core_bands_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_bands
    ADD CONSTRAINT core_bands_pkey PRIMARY KEY (id);


--
-- Name: core_branches core_branches_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_branches
    ADD CONSTRAINT core_branches_pkey PRIMARY KEY (id);


--
-- Name: core_business_units core_business_units_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_business_units
    ADD CONSTRAINT core_business_units_pkey PRIMARY KEY (id);


--
-- Name: core_comments core_comments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_comments
    ADD CONSTRAINT core_comments_pkey PRIMARY KEY (id);


--
-- Name: core_companies core_companies_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_companies
    ADD CONSTRAINT core_companies_pkey PRIMARY KEY (id);


--
-- Name: core_company_groups core_company_groups_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_company_groups
    ADD CONSTRAINT core_company_groups_pkey PRIMARY KEY (id);


--
-- Name: core_company_menu_items core_company_menu_items_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_company_menu_items
    ADD CONSTRAINT core_company_menu_items_pkey PRIMARY KEY (id);


--
-- Name: core_company_modules core_company_modules_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_company_modules
    ADD CONSTRAINT core_company_modules_pkey PRIMARY KEY (id);


--
-- Name: core_company_settings core_company_settings_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_company_settings
    ADD CONSTRAINT core_company_settings_pkey PRIMARY KEY (company_id);


--
-- Name: core_contacts core_contacts_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_contacts
    ADD CONSTRAINT core_contacts_pkey PRIMARY KEY (id);


--
-- Name: core_customers core_customers_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_customers
    ADD CONSTRAINT core_customers_pkey PRIMARY KEY (id);


--
-- Name: core_departments core_departments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_departments
    ADD CONSTRAINT core_departments_pkey PRIMARY KEY (id);


--
-- Name: core_designation_permissions core_designation_permissions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_designation_permissions
    ADD CONSTRAINT core_designation_permissions_pkey PRIMARY KEY (id);


--
-- Name: core_designations core_designations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_designations
    ADD CONSTRAINT core_designations_pkey PRIMARY KEY (id);


--
-- Name: core_employees core_employees_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_employees
    ADD CONSTRAINT core_employees_pkey PRIMARY KEY (id);


--
-- Name: core_field_rules core_field_rules_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_field_rules
    ADD CONSTRAINT core_field_rules_pkey PRIMARY KEY (id);


--
-- Name: core_hierarchy_role_bindings core_hierarchy_role_bindings_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_hierarchy_role_bindings
    ADD CONSTRAINT core_hierarchy_role_bindings_pkey PRIMARY KEY (id);


--
-- Name: core_integrations core_integrations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_integrations
    ADD CONSTRAINT core_integrations_pkey PRIMARY KEY (id);


--
-- Name: core_intercompany_relationships core_intercompany_relationships_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_intercompany_relationships
    ADD CONSTRAINT core_intercompany_relationships_pkey PRIMARY KEY (id);


--
-- Name: core_item_groups core_item_groups_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_item_groups
    ADD CONSTRAINT core_item_groups_pkey PRIMARY KEY (id);


--
-- Name: core_items core_items_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_items
    ADD CONSTRAINT core_items_pkey PRIMARY KEY (id);


--
-- Name: core_notification_preferences core_notification_preferences_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_notification_preferences
    ADD CONSTRAINT core_notification_preferences_pkey PRIMARY KEY (id);


--
-- Name: core_notifications core_notifications_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_notifications
    ADD CONSTRAINT core_notifications_pkey PRIMARY KEY (id);


--
-- Name: core_number_series core_number_series_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_number_series
    ADD CONSTRAINT core_number_series_pkey PRIMARY KEY (id);


--
-- Name: core_permissions core_permissions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_permissions
    ADD CONSTRAINT core_permissions_pkey PRIMARY KEY (id);


--
-- Name: core_role_permissions core_role_permissions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_role_permissions
    ADD CONSTRAINT core_role_permissions_pkey PRIMARY KEY (role_id, permission_id);


--
-- Name: core_roles core_roles_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_roles
    ADD CONSTRAINT core_roles_pkey PRIMARY KEY (id);


--
-- Name: core_sub_departments core_sub_departments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_sub_departments
    ADD CONSTRAINT core_sub_departments_pkey PRIMARY KEY (id);


--
-- Name: core_suppliers core_suppliers_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_suppliers
    ADD CONSTRAINT core_suppliers_pkey PRIMARY KEY (id);


--
-- Name: core_tax_template_lines core_tax_template_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_tax_template_lines
    ADD CONSTRAINT core_tax_template_lines_pkey PRIMARY KEY (id);


--
-- Name: core_tax_templates core_tax_templates_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_tax_templates
    ADD CONSTRAINT core_tax_templates_pkey PRIMARY KEY (id);


--
-- Name: core_taxes core_taxes_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_taxes
    ADD CONSTRAINT core_taxes_pkey PRIMARY KEY (id);


--
-- Name: core_tds_deductions core_tds_deductions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_tds_deductions
    ADD CONSTRAINT core_tds_deductions_pkey PRIMARY KEY (id);


--
-- Name: core_tds_sections core_tds_sections_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_tds_sections
    ADD CONSTRAINT core_tds_sections_pkey PRIMARY KEY (id);


--
-- Name: core_uom_conversions core_uom_conversions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_uom_conversions
    ADD CONSTRAINT core_uom_conversions_pkey PRIMARY KEY (id);


--
-- Name: core_uoms core_uoms_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_uoms
    ADD CONSTRAINT core_uoms_pkey PRIMARY KEY (id);


--
-- Name: core_user_roles core_user_roles_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_user_roles
    ADD CONSTRAINT core_user_roles_pkey PRIMARY KEY (user_id, role_id);


--
-- Name: core_users core_users_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_users
    ADD CONSTRAINT core_users_pkey PRIMARY KEY (id);


--
-- Name: core_warehouses core_warehouses_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_warehouses
    ADD CONSTRAINT core_warehouses_pkey PRIMARY KEY (id);


--
-- Name: fin_accounting_periods fin_accounting_periods_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_accounting_periods
    ADD CONSTRAINT fin_accounting_periods_pkey PRIMARY KEY (id);


--
-- Name: fin_accounts fin_accounts_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_accounts
    ADD CONSTRAINT fin_accounts_pkey PRIMARY KEY (id);


--
-- Name: fin_allocation_rule_lines fin_allocation_rule_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_allocation_rule_lines
    ADD CONSTRAINT fin_allocation_rule_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_allocation_rules fin_allocation_rules_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_allocation_rules
    ADD CONSTRAINT fin_allocation_rules_pkey PRIMARY KEY (id);


--
-- Name: fin_asset_categories fin_asset_categories_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_asset_categories
    ADD CONSTRAINT fin_asset_categories_pkey PRIMARY KEY (id);


--
-- Name: fin_asset_category_books fin_asset_category_books_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_asset_category_books
    ADD CONSTRAINT fin_asset_category_books_pkey PRIMARY KEY (id);


--
-- Name: fin_asset_depreciation_schedules fin_asset_depreciation_schedules_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_asset_depreciation_schedules
    ADD CONSTRAINT fin_asset_depreciation_schedules_pkey PRIMARY KEY (id);


--
-- Name: fin_bank_accounts fin_bank_accounts_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_bank_accounts
    ADD CONSTRAINT fin_bank_accounts_pkey PRIMARY KEY (id);


--
-- Name: fin_bank_transactions fin_bank_transactions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_bank_transactions
    ADD CONSTRAINT fin_bank_transactions_pkey PRIMARY KEY (id);


--
-- Name: fin_budget_lines fin_budget_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_budget_lines
    ADD CONSTRAINT fin_budget_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_budgets fin_budgets_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_budgets
    ADD CONSTRAINT fin_budgets_pkey PRIMARY KEY (id);


--
-- Name: fin_cost_centers fin_cost_centers_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_cost_centers
    ADD CONSTRAINT fin_cost_centers_pkey PRIMARY KEY (id);


--
-- Name: fin_depreciation_books fin_depreciation_books_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_depreciation_books
    ADD CONSTRAINT fin_depreciation_books_pkey PRIMARY KEY (id);


--
-- Name: fin_dimension_types fin_dimension_types_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_dimension_types
    ADD CONSTRAINT fin_dimension_types_pkey PRIMARY KEY (id);


--
-- Name: fin_dimension_values fin_dimension_values_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_dimension_values
    ADD CONSTRAINT fin_dimension_values_pkey PRIMARY KEY (id);


--
-- Name: fin_elimination_entries fin_elimination_entries_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_elimination_entries
    ADD CONSTRAINT fin_elimination_entries_pkey PRIMARY KEY (id);


--
-- Name: fin_elimination_entry_lines fin_elimination_entry_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_elimination_entry_lines
    ADD CONSTRAINT fin_elimination_entry_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_fiscal_years fin_fiscal_years_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_fiscal_years
    ADD CONSTRAINT fin_fiscal_years_pkey PRIMARY KEY (id);


--
-- Name: fin_fixed_assets fin_fixed_assets_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_fixed_assets
    ADD CONSTRAINT fin_fixed_assets_pkey PRIMARY KEY (id);


--
-- Name: fin_fx_revaluation_runs fin_fx_revaluation_runs_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_fx_revaluation_runs
    ADD CONSTRAINT fin_fx_revaluation_runs_pkey PRIMARY KEY (id);


--
-- Name: fin_intercompany_transactions fin_intercompany_transactions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_intercompany_transactions
    ADD CONSTRAINT fin_intercompany_transactions_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_batches fin_journal_batches_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_journal_batches
    ADD CONSTRAINT fin_journal_batches_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_entries fin_journal_entries_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_journal_entries
    ADD CONSTRAINT fin_journal_entries_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_lines fin_journal_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_journal_lines
    ADD CONSTRAINT fin_journal_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_template_lines fin_journal_template_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_journal_template_lines
    ADD CONSTRAINT fin_journal_template_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_templates fin_journal_templates_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_journal_templates
    ADD CONSTRAINT fin_journal_templates_pkey PRIMARY KEY (id);


--
-- Name: fin_line_dimensions fin_line_dimensions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_line_dimensions
    ADD CONSTRAINT fin_line_dimensions_pkey PRIMARY KEY (id);


--
-- Name: fin_payment_allocations fin_payment_allocations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_payment_allocations
    ADD CONSTRAINT fin_payment_allocations_pkey PRIMARY KEY (id);


--
-- Name: fin_payment_entries fin_payment_entries_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_payment_entries
    ADD CONSTRAINT fin_payment_entries_pkey PRIMARY KEY (id);


--
-- Name: fin_period_closings fin_period_closings_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.fin_period_closings
    ADD CONSTRAINT fin_period_closings_pkey PRIMARY KEY (id);


--
-- Name: hcm_appraisal_cycles hcm_appraisal_cycles_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_appraisal_cycles
    ADD CONSTRAINT hcm_appraisal_cycles_pkey PRIMARY KEY (id);


--
-- Name: hcm_appraisals hcm_appraisals_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_appraisals
    ADD CONSTRAINT hcm_appraisals_pkey PRIMARY KEY (id);


--
-- Name: hcm_asset_assignments hcm_asset_assignments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_asset_assignments
    ADD CONSTRAINT hcm_asset_assignments_pkey PRIMARY KEY (id);


--
-- Name: hcm_asset_inventory hcm_asset_inventory_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_asset_inventory
    ADD CONSTRAINT hcm_asset_inventory_pkey PRIMARY KEY (id);


--
-- Name: hcm_asset_requests hcm_asset_requests_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_asset_requests
    ADD CONSTRAINT hcm_asset_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_attendance_records hcm_attendance_records_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_attendance_records
    ADD CONSTRAINT hcm_attendance_records_pkey PRIMARY KEY (id);


--
-- Name: hcm_attendance_regularizations hcm_attendance_regularizations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_attendance_regularizations
    ADD CONSTRAINT hcm_attendance_regularizations_pkey PRIMARY KEY (id);


--
-- Name: hcm_benefit_categories hcm_benefit_categories_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_benefit_categories
    ADD CONSTRAINT hcm_benefit_categories_pkey PRIMARY KEY (id);


--
-- Name: hcm_benefit_category_items hcm_benefit_category_items_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_benefit_category_items
    ADD CONSTRAINT hcm_benefit_category_items_pkey PRIMARY KEY (id);


--
-- Name: hcm_candidates hcm_candidates_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_candidates
    ADD CONSTRAINT hcm_candidates_pkey PRIMARY KEY (id);


--
-- Name: hcm_certifications hcm_certifications_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_certifications
    ADD CONSTRAINT hcm_certifications_pkey PRIMARY KEY (id);


--
-- Name: hcm_company_okrs hcm_company_okrs_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_company_okrs
    ADD CONSTRAINT hcm_company_okrs_pkey PRIMARY KEY (id);


--
-- Name: hcm_document_records hcm_document_records_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_document_records
    ADD CONSTRAINT hcm_document_records_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_benefits hcm_employee_benefits_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_employee_benefits
    ADD CONSTRAINT hcm_employee_benefits_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_education hcm_employee_education_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_employee_education
    ADD CONSTRAINT hcm_employee_education_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_lifecycle_events hcm_employee_lifecycle_events_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_employee_lifecycle_events
    ADD CONSTRAINT hcm_employee_lifecycle_events_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_prior_experience hcm_employee_prior_experience_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_employee_prior_experience
    ADD CONSTRAINT hcm_employee_prior_experience_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_skill_ratings hcm_employee_skill_ratings_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_employee_skill_ratings
    ADD CONSTRAINT hcm_employee_skill_ratings_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_skills hcm_employee_skills_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_employee_skills
    ADD CONSTRAINT hcm_employee_skills_pkey PRIMARY KEY (id);


--
-- Name: hcm_exit_checklist_items hcm_exit_checklist_items_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_exit_checklist_items
    ADD CONSTRAINT hcm_exit_checklist_items_pkey PRIMARY KEY (id);


--
-- Name: hcm_exit_requests hcm_exit_requests_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_exit_requests
    ADD CONSTRAINT hcm_exit_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_expense_claim_lines hcm_expense_claim_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_expense_claim_lines
    ADD CONSTRAINT hcm_expense_claim_lines_pkey PRIMARY KEY (id);


--
-- Name: hcm_expense_claims hcm_expense_claims_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_expense_claims
    ADD CONSTRAINT hcm_expense_claims_pkey PRIMARY KEY (id);


--
-- Name: hcm_final_settlements hcm_final_settlements_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_final_settlements
    ADD CONSTRAINT hcm_final_settlements_pkey PRIMARY KEY (id);


--
-- Name: hcm_goals hcm_goals_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_goals
    ADD CONSTRAINT hcm_goals_pkey PRIMARY KEY (id);


--
-- Name: hcm_hiring_requisitions hcm_hiring_requisitions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_hiring_requisitions
    ADD CONSTRAINT hcm_hiring_requisitions_pkey PRIMARY KEY (id);


--
-- Name: hcm_holidays hcm_holidays_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_holidays
    ADD CONSTRAINT hcm_holidays_pkey PRIMARY KEY (id);


--
-- Name: hcm_interviews hcm_interviews_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_interviews
    ADD CONSTRAINT hcm_interviews_pkey PRIMARY KEY (id);


--
-- Name: hcm_job_applications hcm_job_applications_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_job_applications
    ADD CONSTRAINT hcm_job_applications_pkey PRIMARY KEY (id);


--
-- Name: hcm_job_openings hcm_job_openings_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_job_openings
    ADD CONSTRAINT hcm_job_openings_pkey PRIMARY KEY (id);


--
-- Name: hcm_leave_allocations hcm_leave_allocations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_leave_allocations
    ADD CONSTRAINT hcm_leave_allocations_pkey PRIMARY KEY (id);


--
-- Name: hcm_leave_requests hcm_leave_requests_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_leave_requests
    ADD CONSTRAINT hcm_leave_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_leave_types hcm_leave_types_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_leave_types
    ADD CONSTRAINT hcm_leave_types_pkey PRIMARY KEY (id);


--
-- Name: hcm_loans hcm_loans_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_loans
    ADD CONSTRAINT hcm_loans_pkey PRIMARY KEY (id);


--
-- Name: hcm_offers hcm_offers_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_offers
    ADD CONSTRAINT hcm_offers_pkey PRIMARY KEY (id);


--
-- Name: hcm_onboarding_tasks hcm_onboarding_tasks_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_onboarding_tasks
    ADD CONSTRAINT hcm_onboarding_tasks_pkey PRIMARY KEY (id);


--
-- Name: hcm_onboardings hcm_onboardings_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_onboardings
    ADD CONSTRAINT hcm_onboardings_pkey PRIMARY KEY (id);


--
-- Name: hcm_overtime_requests hcm_overtime_requests_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_overtime_requests
    ADD CONSTRAINT hcm_overtime_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_payroll_runs hcm_payroll_runs_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_payroll_runs
    ADD CONSTRAINT hcm_payroll_runs_pkey PRIMARY KEY (id);


--
-- Name: hcm_policy_documents hcm_policy_documents_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_policy_documents
    ADD CONSTRAINT hcm_policy_documents_pkey PRIMARY KEY (id);


--
-- Name: hcm_recognitions hcm_recognitions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_recognitions
    ADD CONSTRAINT hcm_recognitions_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_components hcm_salary_components_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_salary_components
    ADD CONSTRAINT hcm_salary_components_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_revision_requests hcm_salary_revision_requests_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_salary_revision_requests
    ADD CONSTRAINT hcm_salary_revision_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_slip_lines hcm_salary_slip_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_salary_slip_lines
    ADD CONSTRAINT hcm_salary_slip_lines_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_slips hcm_salary_slips_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_salary_slips
    ADD CONSTRAINT hcm_salary_slips_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_structure_assignments hcm_salary_structure_assignments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_salary_structure_assignments
    ADD CONSTRAINT hcm_salary_structure_assignments_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_structure_lines hcm_salary_structure_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_salary_structure_lines
    ADD CONSTRAINT hcm_salary_structure_lines_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_structures hcm_salary_structures_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_salary_structures
    ADD CONSTRAINT hcm_salary_structures_pkey PRIMARY KEY (id);


--
-- Name: hcm_shift_assignments hcm_shift_assignments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_shift_assignments
    ADD CONSTRAINT hcm_shift_assignments_pkey PRIMARY KEY (id);


--
-- Name: hcm_shifts hcm_shifts_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_shifts
    ADD CONSTRAINT hcm_shifts_pkey PRIMARY KEY (id);


--
-- Name: hcm_tax_declarations hcm_tax_declarations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_tax_declarations
    ADD CONSTRAINT hcm_tax_declarations_pkey PRIMARY KEY (id);


--
-- Name: hcm_training_courses hcm_training_courses_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_training_courses
    ADD CONSTRAINT hcm_training_courses_pkey PRIMARY KEY (id);


--
-- Name: hcm_training_enrollments hcm_training_enrollments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_training_enrollments
    ADD CONSTRAINT hcm_training_enrollments_pkey PRIMARY KEY (id);


--
-- Name: hcm_training_sessions hcm_training_sessions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_training_sessions
    ADD CONSTRAINT hcm_training_sessions_pkey PRIMARY KEY (id);


--
-- Name: hcm_travel_requests hcm_travel_requests_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.hcm_travel_requests
    ADD CONSTRAINT hcm_travel_requests_pkey PRIMARY KEY (id);


--
-- Name: mfg_bom_lines mfg_bom_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_bom_lines
    ADD CONSTRAINT mfg_bom_lines_pkey PRIMARY KEY (id);


--
-- Name: mfg_bom_operations mfg_bom_operations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_bom_operations
    ADD CONSTRAINT mfg_bom_operations_pkey PRIMARY KEY (id);


--
-- Name: mfg_boms mfg_boms_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_boms
    ADD CONSTRAINT mfg_boms_pkey PRIMARY KEY (id);


--
-- Name: mfg_job_card_time_logs mfg_job_card_time_logs_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_job_card_time_logs
    ADD CONSTRAINT mfg_job_card_time_logs_pkey PRIMARY KEY (id);


--
-- Name: mfg_job_cards mfg_job_cards_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_job_cards
    ADD CONSTRAINT mfg_job_cards_pkey PRIMARY KEY (id);


--
-- Name: mfg_machine_utilization_logs mfg_machine_utilization_logs_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_machine_utilization_logs
    ADD CONSTRAINT mfg_machine_utilization_logs_pkey PRIMARY KEY (id);


--
-- Name: mfg_production_costings mfg_production_costings_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_production_costings
    ADD CONSTRAINT mfg_production_costings_pkey PRIMARY KEY (id);


--
-- Name: mfg_production_plan_items mfg_production_plan_items_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_production_plan_items
    ADD CONSTRAINT mfg_production_plan_items_pkey PRIMARY KEY (id);


--
-- Name: mfg_production_plans mfg_production_plans_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_production_plans
    ADD CONSTRAINT mfg_production_plans_pkey PRIMARY KEY (id);


--
-- Name: mfg_work_centers mfg_work_centers_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_work_centers
    ADD CONSTRAINT mfg_work_centers_pkey PRIMARY KEY (id);


--
-- Name: mfg_work_orders mfg_work_orders_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.mfg_work_orders
    ADD CONSTRAINT mfg_work_orders_pkey PRIMARY KEY (id);


--
-- Name: plan_capacity_plans plan_capacity_plans_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_capacity_plans
    ADD CONSTRAINT plan_capacity_plans_pkey PRIMARY KEY (id);


--
-- Name: plan_capacity_plans plan_capacity_plans_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_capacity_plans
    ADD CONSTRAINT plan_capacity_plans_unique UNIQUE (version_id, work_center_id, period_id);


--
-- Name: plan_demand_forecasts plan_demand_forecasts_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_demand_forecasts
    ADD CONSTRAINT plan_demand_forecasts_pkey PRIMARY KEY (id);


--
-- Name: plan_demand_forecasts plan_demand_forecasts_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_demand_forecasts
    ADD CONSTRAINT plan_demand_forecasts_unique UNIQUE (version_id, item_id, warehouse_id, period_id);


--
-- Name: plan_material_requirements plan_material_requirements_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_material_requirements
    ADD CONSTRAINT plan_material_requirements_pkey PRIMARY KEY (id);


--
-- Name: plan_material_requirements plan_material_requirements_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_material_requirements
    ADD CONSTRAINT plan_material_requirements_unique UNIQUE (version_id, item_id, warehouse_id, period_id);


--
-- Name: plan_planning_calendars plan_planning_calendars_name_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_planning_calendars
    ADD CONSTRAINT plan_planning_calendars_name_unique UNIQUE (company_id, name);


--
-- Name: plan_planning_calendars plan_planning_calendars_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_planning_calendars
    ADD CONSTRAINT plan_planning_calendars_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_exceptions plan_planning_exceptions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_planning_exceptions
    ADD CONSTRAINT plan_planning_exceptions_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_periods plan_planning_periods_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_planning_periods
    ADD CONSTRAINT plan_planning_periods_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_periods plan_planning_periods_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_planning_periods
    ADD CONSTRAINT plan_planning_periods_unique UNIQUE (calendar_id, period_no);


--
-- Name: plan_planning_scenarios plan_planning_scenarios_name_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_planning_scenarios
    ADD CONSTRAINT plan_planning_scenarios_name_unique UNIQUE (company_id, name);


--
-- Name: plan_planning_scenarios plan_planning_scenarios_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_planning_scenarios
    ADD CONSTRAINT plan_planning_scenarios_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_versions plan_planning_versions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_planning_versions
    ADD CONSTRAINT plan_planning_versions_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_versions plan_planning_versions_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_planning_versions
    ADD CONSTRAINT plan_planning_versions_unique UNIQUE (scenario_id, version_no);


--
-- Name: plan_resource_plans plan_resource_plans_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_resource_plans
    ADD CONSTRAINT plan_resource_plans_pkey PRIMARY KEY (id);


--
-- Name: plan_resource_plans plan_resource_plans_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_resource_plans
    ADD CONSTRAINT plan_resource_plans_unique UNIQUE (version_id, department_id, designation_id, period_id);


--
-- Name: plan_sales_forecasts plan_sales_forecasts_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_sales_forecasts
    ADD CONSTRAINT plan_sales_forecasts_pkey PRIMARY KEY (id);


--
-- Name: plan_sales_forecasts plan_sales_forecasts_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.plan_sales_forecasts
    ADD CONSTRAINT plan_sales_forecasts_unique UNIQUE (version_id, item_id, customer_id, period_id);


--
-- Name: pm_issues pm_issues_code_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_issues
    ADD CONSTRAINT pm_issues_code_unique UNIQUE (company_id, code);


--
-- Name: pm_issues pm_issues_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_issues
    ADD CONSTRAINT pm_issues_pkey PRIMARY KEY (id);


--
-- Name: pm_meeting_participants pm_meeting_participants_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_meeting_participants
    ADD CONSTRAINT pm_meeting_participants_pkey PRIMARY KEY (id);


--
-- Name: pm_meeting_participants pm_meeting_participants_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_meeting_participants
    ADD CONSTRAINT pm_meeting_participants_unique UNIQUE (meeting_id, employee_id);


--
-- Name: pm_meetings pm_meetings_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_meetings
    ADD CONSTRAINT pm_meetings_pkey PRIMARY KEY (id);


--
-- Name: pm_milestones pm_milestones_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_milestones
    ADD CONSTRAINT pm_milestones_pkey PRIMARY KEY (id);


--
-- Name: pm_project_budgets pm_project_budgets_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_budgets
    ADD CONSTRAINT pm_project_budgets_pkey PRIMARY KEY (id);


--
-- Name: pm_project_categories pm_project_categories_name_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_categories
    ADD CONSTRAINT pm_project_categories_name_unique UNIQUE (company_id, name);


--
-- Name: pm_project_categories pm_project_categories_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_categories
    ADD CONSTRAINT pm_project_categories_pkey PRIMARY KEY (id);


--
-- Name: pm_project_expenses pm_project_expenses_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_expenses
    ADD CONSTRAINT pm_project_expenses_pkey PRIMARY KEY (id);


--
-- Name: pm_project_members pm_project_members_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_members
    ADD CONSTRAINT pm_project_members_pkey PRIMARY KEY (id);


--
-- Name: pm_project_members pm_project_members_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_members
    ADD CONSTRAINT pm_project_members_unique UNIQUE (project_id, employee_id);


--
-- Name: pm_project_phases pm_project_phases_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_phases
    ADD CONSTRAINT pm_project_phases_pkey PRIMARY KEY (id);


--
-- Name: pm_project_phases pm_project_phases_seq_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_phases
    ADD CONSTRAINT pm_project_phases_seq_unique UNIQUE (project_id, sequence);


--
-- Name: pm_project_roles pm_project_roles_name_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_roles
    ADD CONSTRAINT pm_project_roles_name_unique UNIQUE (company_id, name);


--
-- Name: pm_project_roles pm_project_roles_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_roles
    ADD CONSTRAINT pm_project_roles_pkey PRIMARY KEY (id);


--
-- Name: pm_project_status_history pm_project_status_history_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_status_history
    ADD CONSTRAINT pm_project_status_history_pkey PRIMARY KEY (id);


--
-- Name: pm_project_tags pm_project_tags_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_tags
    ADD CONSTRAINT pm_project_tags_pkey PRIMARY KEY (project_id, tag_id);


--
-- Name: pm_project_template_phases pm_project_template_phases_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_template_phases
    ADD CONSTRAINT pm_project_template_phases_pkey PRIMARY KEY (id);


--
-- Name: pm_project_templates pm_project_templates_name_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_templates
    ADD CONSTRAINT pm_project_templates_name_unique UNIQUE (company_id, name);


--
-- Name: pm_project_templates pm_project_templates_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_project_templates
    ADD CONSTRAINT pm_project_templates_pkey PRIMARY KEY (id);


--
-- Name: pm_projects pm_projects_code_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_projects
    ADD CONSTRAINT pm_projects_code_unique UNIQUE (company_id, code);


--
-- Name: pm_projects pm_projects_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_projects
    ADD CONSTRAINT pm_projects_pkey PRIMARY KEY (id);


--
-- Name: pm_resource_allocations pm_resource_allocations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_resource_allocations
    ADD CONSTRAINT pm_resource_allocations_pkey PRIMARY KEY (id);


--
-- Name: pm_risks pm_risks_code_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_risks
    ADD CONSTRAINT pm_risks_code_unique UNIQUE (company_id, code);


--
-- Name: pm_risks pm_risks_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_risks
    ADD CONSTRAINT pm_risks_pkey PRIMARY KEY (id);


--
-- Name: pm_tags pm_tags_name_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_tags
    ADD CONSTRAINT pm_tags_name_unique UNIQUE (company_id, name);


--
-- Name: pm_tags pm_tags_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_tags
    ADD CONSTRAINT pm_tags_pkey PRIMARY KEY (id);


--
-- Name: pm_task_assignments pm_task_assignments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_task_assignments
    ADD CONSTRAINT pm_task_assignments_pkey PRIMARY KEY (id);


--
-- Name: pm_task_assignments pm_task_assignments_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_task_assignments
    ADD CONSTRAINT pm_task_assignments_unique UNIQUE (task_id, employee_id, assignment_role);


--
-- Name: pm_task_checklist_items pm_task_checklist_items_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_task_checklist_items
    ADD CONSTRAINT pm_task_checklist_items_pkey PRIMARY KEY (id);


--
-- Name: pm_task_dependencies pm_task_dependencies_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_task_dependencies
    ADD CONSTRAINT pm_task_dependencies_pkey PRIMARY KEY (id);


--
-- Name: pm_task_dependencies pm_task_dependencies_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_task_dependencies
    ADD CONSTRAINT pm_task_dependencies_unique UNIQUE (task_id, depends_on_task_id);


--
-- Name: pm_tasks pm_tasks_code_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_tasks
    ADD CONSTRAINT pm_tasks_code_unique UNIQUE (company_id, code);


--
-- Name: pm_tasks pm_tasks_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_tasks
    ADD CONSTRAINT pm_tasks_pkey PRIMARY KEY (id);


--
-- Name: pm_time_entries pm_time_entries_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_time_entries
    ADD CONSTRAINT pm_time_entries_pkey PRIMARY KEY (id);


--
-- Name: pm_timesheets pm_timesheets_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_timesheets
    ADD CONSTRAINT pm_timesheets_pkey PRIMARY KEY (id);


--
-- Name: pm_timesheets pm_timesheets_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.pm_timesheets
    ADD CONSTRAINT pm_timesheets_unique UNIQUE (employee_id, week_start_date);


--
-- Name: retail_cash_movements retail_cash_movements_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_cash_movements
    ADD CONSTRAINT retail_cash_movements_pkey PRIMARY KEY (id);


--
-- Name: retail_cash_reconciliations retail_cash_reconciliations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_cash_reconciliations
    ADD CONSTRAINT retail_cash_reconciliations_pkey PRIMARY KEY (id);


--
-- Name: retail_coupons retail_coupons_code_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_coupons
    ADD CONSTRAINT retail_coupons_code_unique UNIQUE (company_id, code);


--
-- Name: retail_coupons retail_coupons_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_coupons
    ADD CONSTRAINT retail_coupons_pkey PRIMARY KEY (id);


--
-- Name: retail_daily_closings retail_daily_closings_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_daily_closings
    ADD CONSTRAINT retail_daily_closings_pkey PRIMARY KEY (id);


--
-- Name: retail_daily_closings retail_daily_closings_store_date; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_daily_closings
    ADD CONSTRAINT retail_daily_closings_store_date UNIQUE (store_id, closing_date);


--
-- Name: retail_gift_card_transactions retail_gift_card_transactions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_gift_card_transactions
    ADD CONSTRAINT retail_gift_card_transactions_pkey PRIMARY KEY (id);


--
-- Name: retail_gift_cards retail_gift_cards_no_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_gift_cards
    ADD CONSTRAINT retail_gift_cards_no_unique UNIQUE (company_id, card_no);


--
-- Name: retail_gift_cards retail_gift_cards_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_gift_cards
    ADD CONSTRAINT retail_gift_cards_pkey PRIMARY KEY (id);


--
-- Name: retail_loyalty_members retail_loyalty_members_code_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_loyalty_members
    ADD CONSTRAINT retail_loyalty_members_code_unique UNIQUE (loyalty_code);


--
-- Name: retail_loyalty_members retail_loyalty_members_cust_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_loyalty_members
    ADD CONSTRAINT retail_loyalty_members_cust_unique UNIQUE (customer_id);


--
-- Name: retail_loyalty_members retail_loyalty_members_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_loyalty_members
    ADD CONSTRAINT retail_loyalty_members_pkey PRIMARY KEY (id);


--
-- Name: retail_loyalty_transactions retail_loyalty_transactions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_loyalty_transactions
    ADD CONSTRAINT retail_loyalty_transactions_pkey PRIMARY KEY (id);


--
-- Name: retail_payment_methods retail_payment_methods_name_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_payment_methods
    ADD CONSTRAINT retail_payment_methods_name_unique UNIQUE (company_id, name);


--
-- Name: retail_payment_methods retail_payment_methods_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_payment_methods
    ADD CONSTRAINT retail_payment_methods_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_exchanges retail_pos_exchanges_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_exchanges
    ADD CONSTRAINT retail_pos_exchanges_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_payments retail_pos_payments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_payments
    ADD CONSTRAINT retail_pos_payments_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_profiles retail_pos_profiles_name_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_profiles
    ADD CONSTRAINT retail_pos_profiles_name_unique UNIQUE (company_id, name);


--
-- Name: retail_pos_profiles retail_pos_profiles_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_profiles
    ADD CONSTRAINT retail_pos_profiles_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_return_items retail_pos_return_items_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_return_items
    ADD CONSTRAINT retail_pos_return_items_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_returns retail_pos_returns_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_returns
    ADD CONSTRAINT retail_pos_returns_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_sale_items retail_pos_sale_items_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_sale_items
    ADD CONSTRAINT retail_pos_sale_items_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_sales retail_pos_sales_no_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_sales
    ADD CONSTRAINT retail_pos_sales_no_unique UNIQUE (company_id, sale_no);


--
-- Name: retail_pos_sales retail_pos_sales_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_sales
    ADD CONSTRAINT retail_pos_sales_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_sessions retail_pos_sessions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_sessions
    ADD CONSTRAINT retail_pos_sessions_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_terminals retail_pos_terminals_code_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_terminals
    ADD CONSTRAINT retail_pos_terminals_code_unique UNIQUE (store_id, terminal_code);


--
-- Name: retail_pos_terminals retail_pos_terminals_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_pos_terminals
    ADD CONSTRAINT retail_pos_terminals_pkey PRIMARY KEY (id);


--
-- Name: retail_price_rules retail_price_rules_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_price_rules
    ADD CONSTRAINT retail_price_rules_pkey PRIMARY KEY (id);


--
-- Name: retail_promotions retail_promotions_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_promotions
    ADD CONSTRAINT retail_promotions_pkey PRIMARY KEY (id);


--
-- Name: retail_stores retail_stores_code_unique; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_stores
    ADD CONSTRAINT retail_stores_code_unique UNIQUE (company_id, code);


--
-- Name: retail_stores retail_stores_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.retail_stores
    ADD CONSTRAINT retail_stores_pkey PRIMARY KEY (id);


--
-- Name: scm_batches scm_batches_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_batches
    ADD CONSTRAINT scm_batches_pkey PRIMARY KEY (id);


--
-- Name: scm_bins scm_bins_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_bins
    ADD CONSTRAINT scm_bins_pkey PRIMARY KEY (item_id, warehouse_id);


--
-- Name: scm_delivery_note_lines scm_delivery_note_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_delivery_note_lines
    ADD CONSTRAINT scm_delivery_note_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_delivery_notes scm_delivery_notes_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_delivery_notes
    ADD CONSTRAINT scm_delivery_notes_pkey PRIMARY KEY (id);


--
-- Name: scm_eway_bills scm_eway_bills_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_eway_bills
    ADD CONSTRAINT scm_eway_bills_pkey PRIMARY KEY (id);


--
-- Name: scm_leads scm_leads_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_leads
    ADD CONSTRAINT scm_leads_pkey PRIMARY KEY (id);


--
-- Name: scm_opportunities scm_opportunities_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_opportunities
    ADD CONSTRAINT scm_opportunities_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_invoice_lines scm_purchase_invoice_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_purchase_invoice_lines
    ADD CONSTRAINT scm_purchase_invoice_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_invoices scm_purchase_invoices_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_purchase_invoices
    ADD CONSTRAINT scm_purchase_invoices_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_order_lines scm_purchase_order_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_purchase_order_lines
    ADD CONSTRAINT scm_purchase_order_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_orders scm_purchase_orders_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_purchase_orders
    ADD CONSTRAINT scm_purchase_orders_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_receipt_lines scm_purchase_receipt_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_purchase_receipt_lines
    ADD CONSTRAINT scm_purchase_receipt_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_receipts scm_purchase_receipts_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_purchase_receipts
    ADD CONSTRAINT scm_purchase_receipts_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_request_lines scm_purchase_request_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_purchase_request_lines
    ADD CONSTRAINT scm_purchase_request_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_requests scm_purchase_requests_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_purchase_requests
    ADD CONSTRAINT scm_purchase_requests_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_returns scm_purchase_returns_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_purchase_returns
    ADD CONSTRAINT scm_purchase_returns_pkey PRIMARY KEY (id);


--
-- Name: scm_quotation_lines scm_quotation_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_quotation_lines
    ADD CONSTRAINT scm_quotation_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_quotations scm_quotations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_quotations
    ADD CONSTRAINT scm_quotations_pkey PRIMARY KEY (id);


--
-- Name: scm_rfq_lines scm_rfq_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_rfq_lines
    ADD CONSTRAINT scm_rfq_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_rfq_suppliers scm_rfq_suppliers_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_rfq_suppliers
    ADD CONSTRAINT scm_rfq_suppliers_pkey PRIMARY KEY (rfq_id, supplier_id);


--
-- Name: scm_rfqs scm_rfqs_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_rfqs
    ADD CONSTRAINT scm_rfqs_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_invoice_lines scm_sales_invoice_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_sales_invoice_lines
    ADD CONSTRAINT scm_sales_invoice_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_invoices scm_sales_invoices_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_sales_invoices
    ADD CONSTRAINT scm_sales_invoices_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_order_lines scm_sales_order_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_sales_order_lines
    ADD CONSTRAINT scm_sales_order_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_orders scm_sales_orders_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_sales_orders
    ADD CONSTRAINT scm_sales_orders_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_returns scm_sales_returns_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_sales_returns
    ADD CONSTRAINT scm_sales_returns_pkey PRIMARY KEY (id);


--
-- Name: scm_serial_nos scm_serial_nos_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_serial_nos
    ADD CONSTRAINT scm_serial_nos_pkey PRIMARY KEY (id);


--
-- Name: scm_shipments scm_shipments_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_shipments
    ADD CONSTRAINT scm_shipments_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_entries scm_stock_entries_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_stock_entries
    ADD CONSTRAINT scm_stock_entries_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_entry_lines scm_stock_entry_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_stock_entry_lines
    ADD CONSTRAINT scm_stock_entry_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_ledger_entries scm_stock_ledger_entries_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_stock_ledger_entries
    ADD CONSTRAINT scm_stock_ledger_entries_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_transfer_lines scm_stock_transfer_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_stock_transfer_lines
    ADD CONSTRAINT scm_stock_transfer_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_transfers scm_stock_transfers_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_stock_transfers
    ADD CONSTRAINT scm_stock_transfers_pkey PRIMARY KEY (id);


--
-- Name: scm_supplier_quotation_lines scm_supplier_quotation_lines_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_supplier_quotation_lines
    ADD CONSTRAINT scm_supplier_quotation_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_supplier_quotations scm_supplier_quotations_pkey; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.scm_supplier_quotations
    ADD CONSTRAINT scm_supplier_quotations_pkey PRIMARY KEY (id);


--
-- Name: core_companies uq_core_companies_code; Type: CONSTRAINT; Schema: _template; Owner: -
--

ALTER TABLE ONLY _template.core_companies
    ADD CONSTRAINT uq_core_companies_code UNIQUE (code);


--
-- Name: core_activity_logs core_activity_logs_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_activity_logs
    ADD CONSTRAINT core_activity_logs_pkey PRIMARY KEY (id);


--
-- Name: core_addresses core_addresses_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_addresses
    ADD CONSTRAINT core_addresses_pkey PRIMARY KEY (id);


--
-- Name: core_approval_actions core_approval_actions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_approval_actions
    ADD CONSTRAINT core_approval_actions_pkey PRIMARY KEY (id);


--
-- Name: core_approval_requests core_approval_requests_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_approval_requests
    ADD CONSTRAINT core_approval_requests_pkey PRIMARY KEY (id);


--
-- Name: core_approval_workflow_steps core_approval_workflow_steps_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_approval_workflow_steps
    ADD CONSTRAINT core_approval_workflow_steps_pkey PRIMARY KEY (id);


--
-- Name: core_approval_workflows core_approval_workflows_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_approval_workflows
    ADD CONSTRAINT core_approval_workflows_pkey PRIMARY KEY (id);


--
-- Name: core_attachments core_attachments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_attachments
    ADD CONSTRAINT core_attachments_pkey PRIMARY KEY (id);


--
-- Name: core_audit_logs core_audit_logs_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_audit_logs
    ADD CONSTRAINT core_audit_logs_pkey PRIMARY KEY (id);


--
-- Name: core_bands core_bands_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_bands
    ADD CONSTRAINT core_bands_pkey PRIMARY KEY (id);


--
-- Name: core_branches core_branches_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_branches
    ADD CONSTRAINT core_branches_pkey PRIMARY KEY (id);


--
-- Name: core_business_units core_business_units_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_business_units
    ADD CONSTRAINT core_business_units_pkey PRIMARY KEY (id);


--
-- Name: core_comments core_comments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_comments
    ADD CONSTRAINT core_comments_pkey PRIMARY KEY (id);


--
-- Name: core_companies core_companies_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_companies
    ADD CONSTRAINT core_companies_pkey PRIMARY KEY (id);


--
-- Name: core_company_groups core_company_groups_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_company_groups
    ADD CONSTRAINT core_company_groups_pkey PRIMARY KEY (id);


--
-- Name: core_company_menu_items core_company_menu_items_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_company_menu_items
    ADD CONSTRAINT core_company_menu_items_pkey PRIMARY KEY (id);


--
-- Name: core_company_modules core_company_modules_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_company_modules
    ADD CONSTRAINT core_company_modules_pkey PRIMARY KEY (id);


--
-- Name: core_company_settings core_company_settings_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_company_settings
    ADD CONSTRAINT core_company_settings_pkey PRIMARY KEY (company_id);


--
-- Name: core_contacts core_contacts_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_contacts
    ADD CONSTRAINT core_contacts_pkey PRIMARY KEY (id);


--
-- Name: core_customers core_customers_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_customers
    ADD CONSTRAINT core_customers_pkey PRIMARY KEY (id);


--
-- Name: core_departments core_departments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_departments
    ADD CONSTRAINT core_departments_pkey PRIMARY KEY (id);


--
-- Name: core_designation_permissions core_designation_permissions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_designation_permissions
    ADD CONSTRAINT core_designation_permissions_pkey PRIMARY KEY (id);


--
-- Name: core_designations core_designations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_designations
    ADD CONSTRAINT core_designations_pkey PRIMARY KEY (id);


--
-- Name: core_employees core_employees_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_employees
    ADD CONSTRAINT core_employees_pkey PRIMARY KEY (id);


--
-- Name: core_field_rules core_field_rules_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_field_rules
    ADD CONSTRAINT core_field_rules_pkey PRIMARY KEY (id);


--
-- Name: core_hierarchy_role_bindings core_hierarchy_role_bindings_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_hierarchy_role_bindings
    ADD CONSTRAINT core_hierarchy_role_bindings_pkey PRIMARY KEY (id);


--
-- Name: core_integrations core_integrations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_integrations
    ADD CONSTRAINT core_integrations_pkey PRIMARY KEY (id);


--
-- Name: core_intercompany_relationships core_intercompany_relationships_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_intercompany_relationships
    ADD CONSTRAINT core_intercompany_relationships_pkey PRIMARY KEY (id);


--
-- Name: core_item_groups core_item_groups_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_item_groups
    ADD CONSTRAINT core_item_groups_pkey PRIMARY KEY (id);


--
-- Name: core_items core_items_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_items
    ADD CONSTRAINT core_items_pkey PRIMARY KEY (id);


--
-- Name: core_notification_preferences core_notification_preferences_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_notification_preferences
    ADD CONSTRAINT core_notification_preferences_pkey PRIMARY KEY (id);


--
-- Name: core_notifications core_notifications_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_notifications
    ADD CONSTRAINT core_notifications_pkey PRIMARY KEY (id);


--
-- Name: core_number_series core_number_series_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_number_series
    ADD CONSTRAINT core_number_series_pkey PRIMARY KEY (id);


--
-- Name: core_permissions core_permissions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_permissions
    ADD CONSTRAINT core_permissions_pkey PRIMARY KEY (id);


--
-- Name: core_role_permissions core_role_permissions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_role_permissions
    ADD CONSTRAINT core_role_permissions_pkey PRIMARY KEY (role_id, permission_id);


--
-- Name: core_roles core_roles_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_roles
    ADD CONSTRAINT core_roles_pkey PRIMARY KEY (id);


--
-- Name: core_sub_departments core_sub_departments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_sub_departments
    ADD CONSTRAINT core_sub_departments_pkey PRIMARY KEY (id);


--
-- Name: core_suppliers core_suppliers_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_suppliers
    ADD CONSTRAINT core_suppliers_pkey PRIMARY KEY (id);


--
-- Name: core_tax_template_lines core_tax_template_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_tax_template_lines
    ADD CONSTRAINT core_tax_template_lines_pkey PRIMARY KEY (id);


--
-- Name: core_tax_templates core_tax_templates_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_tax_templates
    ADD CONSTRAINT core_tax_templates_pkey PRIMARY KEY (id);


--
-- Name: core_taxes core_taxes_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_taxes
    ADD CONSTRAINT core_taxes_pkey PRIMARY KEY (id);


--
-- Name: core_tds_deductions core_tds_deductions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_tds_deductions
    ADD CONSTRAINT core_tds_deductions_pkey PRIMARY KEY (id);


--
-- Name: core_tds_sections core_tds_sections_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_tds_sections
    ADD CONSTRAINT core_tds_sections_pkey PRIMARY KEY (id);


--
-- Name: core_uom_conversions core_uom_conversions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_uom_conversions
    ADD CONSTRAINT core_uom_conversions_pkey PRIMARY KEY (id);


--
-- Name: core_uoms core_uoms_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_uoms
    ADD CONSTRAINT core_uoms_pkey PRIMARY KEY (id);


--
-- Name: core_user_roles core_user_roles_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_user_roles
    ADD CONSTRAINT core_user_roles_pkey PRIMARY KEY (user_id, role_id);


--
-- Name: core_users core_users_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_users
    ADD CONSTRAINT core_users_pkey PRIMARY KEY (id);


--
-- Name: core_warehouses core_warehouses_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_warehouses
    ADD CONSTRAINT core_warehouses_pkey PRIMARY KEY (id);


--
-- Name: fin_accounting_periods fin_accounting_periods_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_accounting_periods
    ADD CONSTRAINT fin_accounting_periods_pkey PRIMARY KEY (id);


--
-- Name: fin_accounts fin_accounts_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_accounts
    ADD CONSTRAINT fin_accounts_pkey PRIMARY KEY (id);


--
-- Name: fin_allocation_rule_lines fin_allocation_rule_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_allocation_rule_lines
    ADD CONSTRAINT fin_allocation_rule_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_allocation_rules fin_allocation_rules_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_allocation_rules
    ADD CONSTRAINT fin_allocation_rules_pkey PRIMARY KEY (id);


--
-- Name: fin_asset_categories fin_asset_categories_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_asset_categories
    ADD CONSTRAINT fin_asset_categories_pkey PRIMARY KEY (id);


--
-- Name: fin_asset_category_books fin_asset_category_books_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_asset_category_books
    ADD CONSTRAINT fin_asset_category_books_pkey PRIMARY KEY (id);


--
-- Name: fin_asset_depreciation_schedules fin_asset_depreciation_schedules_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_asset_depreciation_schedules
    ADD CONSTRAINT fin_asset_depreciation_schedules_pkey PRIMARY KEY (id);


--
-- Name: fin_bank_accounts fin_bank_accounts_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_bank_accounts
    ADD CONSTRAINT fin_bank_accounts_pkey PRIMARY KEY (id);


--
-- Name: fin_bank_transactions fin_bank_transactions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_bank_transactions
    ADD CONSTRAINT fin_bank_transactions_pkey PRIMARY KEY (id);


--
-- Name: fin_budget_lines fin_budget_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_budget_lines
    ADD CONSTRAINT fin_budget_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_budgets fin_budgets_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_budgets
    ADD CONSTRAINT fin_budgets_pkey PRIMARY KEY (id);


--
-- Name: fin_cost_centers fin_cost_centers_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_cost_centers
    ADD CONSTRAINT fin_cost_centers_pkey PRIMARY KEY (id);


--
-- Name: fin_depreciation_books fin_depreciation_books_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_depreciation_books
    ADD CONSTRAINT fin_depreciation_books_pkey PRIMARY KEY (id);


--
-- Name: fin_dimension_types fin_dimension_types_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_dimension_types
    ADD CONSTRAINT fin_dimension_types_pkey PRIMARY KEY (id);


--
-- Name: fin_dimension_values fin_dimension_values_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_dimension_values
    ADD CONSTRAINT fin_dimension_values_pkey PRIMARY KEY (id);


--
-- Name: fin_elimination_entries fin_elimination_entries_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_elimination_entries
    ADD CONSTRAINT fin_elimination_entries_pkey PRIMARY KEY (id);


--
-- Name: fin_elimination_entry_lines fin_elimination_entry_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_elimination_entry_lines
    ADD CONSTRAINT fin_elimination_entry_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_fiscal_years fin_fiscal_years_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_fiscal_years
    ADD CONSTRAINT fin_fiscal_years_pkey PRIMARY KEY (id);


--
-- Name: fin_fixed_assets fin_fixed_assets_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_fixed_assets
    ADD CONSTRAINT fin_fixed_assets_pkey PRIMARY KEY (id);


--
-- Name: fin_fx_revaluation_runs fin_fx_revaluation_runs_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_fx_revaluation_runs
    ADD CONSTRAINT fin_fx_revaluation_runs_pkey PRIMARY KEY (id);


--
-- Name: fin_intercompany_transactions fin_intercompany_transactions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_intercompany_transactions
    ADD CONSTRAINT fin_intercompany_transactions_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_batches fin_journal_batches_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_journal_batches
    ADD CONSTRAINT fin_journal_batches_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_entries fin_journal_entries_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_journal_entries
    ADD CONSTRAINT fin_journal_entries_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_lines fin_journal_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_journal_lines
    ADD CONSTRAINT fin_journal_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_template_lines fin_journal_template_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_journal_template_lines
    ADD CONSTRAINT fin_journal_template_lines_pkey PRIMARY KEY (id);


--
-- Name: fin_journal_templates fin_journal_templates_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_journal_templates
    ADD CONSTRAINT fin_journal_templates_pkey PRIMARY KEY (id);


--
-- Name: fin_line_dimensions fin_line_dimensions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_line_dimensions
    ADD CONSTRAINT fin_line_dimensions_pkey PRIMARY KEY (id);


--
-- Name: fin_payment_allocations fin_payment_allocations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_payment_allocations
    ADD CONSTRAINT fin_payment_allocations_pkey PRIMARY KEY (id);


--
-- Name: fin_payment_entries fin_payment_entries_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_payment_entries
    ADD CONSTRAINT fin_payment_entries_pkey PRIMARY KEY (id);


--
-- Name: fin_period_closings fin_period_closings_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.fin_period_closings
    ADD CONSTRAINT fin_period_closings_pkey PRIMARY KEY (id);


--
-- Name: hcm_appraisal_cycles hcm_appraisal_cycles_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_appraisal_cycles
    ADD CONSTRAINT hcm_appraisal_cycles_pkey PRIMARY KEY (id);


--
-- Name: hcm_appraisals hcm_appraisals_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_appraisals
    ADD CONSTRAINT hcm_appraisals_pkey PRIMARY KEY (id);


--
-- Name: hcm_asset_assignments hcm_asset_assignments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_asset_assignments
    ADD CONSTRAINT hcm_asset_assignments_pkey PRIMARY KEY (id);


--
-- Name: hcm_asset_inventory hcm_asset_inventory_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_asset_inventory
    ADD CONSTRAINT hcm_asset_inventory_pkey PRIMARY KEY (id);


--
-- Name: hcm_asset_requests hcm_asset_requests_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_asset_requests
    ADD CONSTRAINT hcm_asset_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_attendance_records hcm_attendance_records_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_attendance_records
    ADD CONSTRAINT hcm_attendance_records_pkey PRIMARY KEY (id);


--
-- Name: hcm_attendance_regularizations hcm_attendance_regularizations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_attendance_regularizations
    ADD CONSTRAINT hcm_attendance_regularizations_pkey PRIMARY KEY (id);


--
-- Name: hcm_benefit_categories hcm_benefit_categories_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_benefit_categories
    ADD CONSTRAINT hcm_benefit_categories_pkey PRIMARY KEY (id);


--
-- Name: hcm_benefit_category_items hcm_benefit_category_items_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_benefit_category_items
    ADD CONSTRAINT hcm_benefit_category_items_pkey PRIMARY KEY (id);


--
-- Name: hcm_candidates hcm_candidates_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_candidates
    ADD CONSTRAINT hcm_candidates_pkey PRIMARY KEY (id);


--
-- Name: hcm_certifications hcm_certifications_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_certifications
    ADD CONSTRAINT hcm_certifications_pkey PRIMARY KEY (id);


--
-- Name: hcm_company_okrs hcm_company_okrs_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_company_okrs
    ADD CONSTRAINT hcm_company_okrs_pkey PRIMARY KEY (id);


--
-- Name: hcm_document_records hcm_document_records_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_document_records
    ADD CONSTRAINT hcm_document_records_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_benefits hcm_employee_benefits_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_employee_benefits
    ADD CONSTRAINT hcm_employee_benefits_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_education hcm_employee_education_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_employee_education
    ADD CONSTRAINT hcm_employee_education_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_lifecycle_events hcm_employee_lifecycle_events_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_employee_lifecycle_events
    ADD CONSTRAINT hcm_employee_lifecycle_events_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_prior_experience hcm_employee_prior_experience_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_employee_prior_experience
    ADD CONSTRAINT hcm_employee_prior_experience_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_skill_ratings hcm_employee_skill_ratings_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_employee_skill_ratings
    ADD CONSTRAINT hcm_employee_skill_ratings_pkey PRIMARY KEY (id);


--
-- Name: hcm_employee_skills hcm_employee_skills_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_employee_skills
    ADD CONSTRAINT hcm_employee_skills_pkey PRIMARY KEY (id);


--
-- Name: hcm_exit_checklist_items hcm_exit_checklist_items_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_exit_checklist_items
    ADD CONSTRAINT hcm_exit_checklist_items_pkey PRIMARY KEY (id);


--
-- Name: hcm_exit_requests hcm_exit_requests_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_exit_requests
    ADD CONSTRAINT hcm_exit_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_expense_claim_lines hcm_expense_claim_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_expense_claim_lines
    ADD CONSTRAINT hcm_expense_claim_lines_pkey PRIMARY KEY (id);


--
-- Name: hcm_expense_claims hcm_expense_claims_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_expense_claims
    ADD CONSTRAINT hcm_expense_claims_pkey PRIMARY KEY (id);


--
-- Name: hcm_final_settlements hcm_final_settlements_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_final_settlements
    ADD CONSTRAINT hcm_final_settlements_pkey PRIMARY KEY (id);


--
-- Name: hcm_goals hcm_goals_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_goals
    ADD CONSTRAINT hcm_goals_pkey PRIMARY KEY (id);


--
-- Name: hcm_hiring_requisitions hcm_hiring_requisitions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_hiring_requisitions
    ADD CONSTRAINT hcm_hiring_requisitions_pkey PRIMARY KEY (id);


--
-- Name: hcm_holidays hcm_holidays_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_holidays
    ADD CONSTRAINT hcm_holidays_pkey PRIMARY KEY (id);


--
-- Name: hcm_interviews hcm_interviews_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_interviews
    ADD CONSTRAINT hcm_interviews_pkey PRIMARY KEY (id);


--
-- Name: hcm_job_applications hcm_job_applications_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_job_applications
    ADD CONSTRAINT hcm_job_applications_pkey PRIMARY KEY (id);


--
-- Name: hcm_job_openings hcm_job_openings_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_job_openings
    ADD CONSTRAINT hcm_job_openings_pkey PRIMARY KEY (id);


--
-- Name: hcm_leave_allocations hcm_leave_allocations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_leave_allocations
    ADD CONSTRAINT hcm_leave_allocations_pkey PRIMARY KEY (id);


--
-- Name: hcm_leave_requests hcm_leave_requests_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_leave_requests
    ADD CONSTRAINT hcm_leave_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_leave_types hcm_leave_types_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_leave_types
    ADD CONSTRAINT hcm_leave_types_pkey PRIMARY KEY (id);


--
-- Name: hcm_loans hcm_loans_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_loans
    ADD CONSTRAINT hcm_loans_pkey PRIMARY KEY (id);


--
-- Name: hcm_offers hcm_offers_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_offers
    ADD CONSTRAINT hcm_offers_pkey PRIMARY KEY (id);


--
-- Name: hcm_onboarding_tasks hcm_onboarding_tasks_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_onboarding_tasks
    ADD CONSTRAINT hcm_onboarding_tasks_pkey PRIMARY KEY (id);


--
-- Name: hcm_onboardings hcm_onboardings_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_onboardings
    ADD CONSTRAINT hcm_onboardings_pkey PRIMARY KEY (id);


--
-- Name: hcm_overtime_requests hcm_overtime_requests_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_overtime_requests
    ADD CONSTRAINT hcm_overtime_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_payroll_runs hcm_payroll_runs_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_payroll_runs
    ADD CONSTRAINT hcm_payroll_runs_pkey PRIMARY KEY (id);


--
-- Name: hcm_policy_documents hcm_policy_documents_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_policy_documents
    ADD CONSTRAINT hcm_policy_documents_pkey PRIMARY KEY (id);


--
-- Name: hcm_recognitions hcm_recognitions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_recognitions
    ADD CONSTRAINT hcm_recognitions_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_components hcm_salary_components_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_salary_components
    ADD CONSTRAINT hcm_salary_components_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_revision_requests hcm_salary_revision_requests_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_salary_revision_requests
    ADD CONSTRAINT hcm_salary_revision_requests_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_slip_lines hcm_salary_slip_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_salary_slip_lines
    ADD CONSTRAINT hcm_salary_slip_lines_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_slips hcm_salary_slips_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_salary_slips
    ADD CONSTRAINT hcm_salary_slips_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_structure_assignments hcm_salary_structure_assignments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_salary_structure_assignments
    ADD CONSTRAINT hcm_salary_structure_assignments_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_structure_lines hcm_salary_structure_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_salary_structure_lines
    ADD CONSTRAINT hcm_salary_structure_lines_pkey PRIMARY KEY (id);


--
-- Name: hcm_salary_structures hcm_salary_structures_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_salary_structures
    ADD CONSTRAINT hcm_salary_structures_pkey PRIMARY KEY (id);


--
-- Name: hcm_shift_assignments hcm_shift_assignments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_shift_assignments
    ADD CONSTRAINT hcm_shift_assignments_pkey PRIMARY KEY (id);


--
-- Name: hcm_shifts hcm_shifts_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_shifts
    ADD CONSTRAINT hcm_shifts_pkey PRIMARY KEY (id);


--
-- Name: hcm_tax_declarations hcm_tax_declarations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_tax_declarations
    ADD CONSTRAINT hcm_tax_declarations_pkey PRIMARY KEY (id);


--
-- Name: hcm_training_courses hcm_training_courses_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_training_courses
    ADD CONSTRAINT hcm_training_courses_pkey PRIMARY KEY (id);


--
-- Name: hcm_training_enrollments hcm_training_enrollments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_training_enrollments
    ADD CONSTRAINT hcm_training_enrollments_pkey PRIMARY KEY (id);


--
-- Name: hcm_training_sessions hcm_training_sessions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_training_sessions
    ADD CONSTRAINT hcm_training_sessions_pkey PRIMARY KEY (id);


--
-- Name: hcm_travel_requests hcm_travel_requests_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.hcm_travel_requests
    ADD CONSTRAINT hcm_travel_requests_pkey PRIMARY KEY (id);


--
-- Name: mfg_bom_lines mfg_bom_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_bom_lines
    ADD CONSTRAINT mfg_bom_lines_pkey PRIMARY KEY (id);


--
-- Name: mfg_bom_operations mfg_bom_operations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_bom_operations
    ADD CONSTRAINT mfg_bom_operations_pkey PRIMARY KEY (id);


--
-- Name: mfg_boms mfg_boms_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_boms
    ADD CONSTRAINT mfg_boms_pkey PRIMARY KEY (id);


--
-- Name: mfg_job_card_time_logs mfg_job_card_time_logs_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_job_card_time_logs
    ADD CONSTRAINT mfg_job_card_time_logs_pkey PRIMARY KEY (id);


--
-- Name: mfg_job_cards mfg_job_cards_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_job_cards
    ADD CONSTRAINT mfg_job_cards_pkey PRIMARY KEY (id);


--
-- Name: mfg_machine_utilization_logs mfg_machine_utilization_logs_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_machine_utilization_logs
    ADD CONSTRAINT mfg_machine_utilization_logs_pkey PRIMARY KEY (id);


--
-- Name: mfg_production_costings mfg_production_costings_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_production_costings
    ADD CONSTRAINT mfg_production_costings_pkey PRIMARY KEY (id);


--
-- Name: mfg_production_plan_items mfg_production_plan_items_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_production_plan_items
    ADD CONSTRAINT mfg_production_plan_items_pkey PRIMARY KEY (id);


--
-- Name: mfg_production_plans mfg_production_plans_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_production_plans
    ADD CONSTRAINT mfg_production_plans_pkey PRIMARY KEY (id);


--
-- Name: mfg_work_centers mfg_work_centers_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_work_centers
    ADD CONSTRAINT mfg_work_centers_pkey PRIMARY KEY (id);


--
-- Name: mfg_work_orders mfg_work_orders_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.mfg_work_orders
    ADD CONSTRAINT mfg_work_orders_pkey PRIMARY KEY (id);


--
-- Name: plan_capacity_plans plan_capacity_plans_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_capacity_plans
    ADD CONSTRAINT plan_capacity_plans_pkey PRIMARY KEY (id);


--
-- Name: plan_capacity_plans plan_capacity_plans_version_id_work_center_id_period_id_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_capacity_plans
    ADD CONSTRAINT plan_capacity_plans_version_id_work_center_id_period_id_key UNIQUE (version_id, work_center_id, period_id);


--
-- Name: plan_demand_forecasts plan_demand_forecasts_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_demand_forecasts
    ADD CONSTRAINT plan_demand_forecasts_pkey PRIMARY KEY (id);


--
-- Name: plan_demand_forecasts plan_demand_forecasts_version_id_item_id_warehouse_id_perio_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_demand_forecasts
    ADD CONSTRAINT plan_demand_forecasts_version_id_item_id_warehouse_id_perio_key UNIQUE (version_id, item_id, warehouse_id, period_id);


--
-- Name: plan_material_requirements plan_material_requirements_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_material_requirements
    ADD CONSTRAINT plan_material_requirements_pkey PRIMARY KEY (id);


--
-- Name: plan_material_requirements plan_material_requirements_version_id_item_id_warehouse_id__key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_material_requirements
    ADD CONSTRAINT plan_material_requirements_version_id_item_id_warehouse_id__key UNIQUE (version_id, item_id, warehouse_id, period_id);


--
-- Name: plan_planning_calendars plan_planning_calendars_company_id_name_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_planning_calendars
    ADD CONSTRAINT plan_planning_calendars_company_id_name_key UNIQUE (company_id, name);


--
-- Name: plan_planning_calendars plan_planning_calendars_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_planning_calendars
    ADD CONSTRAINT plan_planning_calendars_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_exceptions plan_planning_exceptions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_planning_exceptions
    ADD CONSTRAINT plan_planning_exceptions_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_periods plan_planning_periods_calendar_id_period_no_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_planning_periods
    ADD CONSTRAINT plan_planning_periods_calendar_id_period_no_key UNIQUE (calendar_id, period_no);


--
-- Name: plan_planning_periods plan_planning_periods_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_planning_periods
    ADD CONSTRAINT plan_planning_periods_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_scenarios plan_planning_scenarios_company_id_name_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_planning_scenarios
    ADD CONSTRAINT plan_planning_scenarios_company_id_name_key UNIQUE (company_id, name);


--
-- Name: plan_planning_scenarios plan_planning_scenarios_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_planning_scenarios
    ADD CONSTRAINT plan_planning_scenarios_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_versions plan_planning_versions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_planning_versions
    ADD CONSTRAINT plan_planning_versions_pkey PRIMARY KEY (id);


--
-- Name: plan_planning_versions plan_planning_versions_scenario_id_version_no_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_planning_versions
    ADD CONSTRAINT plan_planning_versions_scenario_id_version_no_key UNIQUE (scenario_id, version_no);


--
-- Name: plan_resource_plans plan_resource_plans_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_resource_plans
    ADD CONSTRAINT plan_resource_plans_pkey PRIMARY KEY (id);


--
-- Name: plan_resource_plans plan_resource_plans_version_id_department_id_designation_id_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_resource_plans
    ADD CONSTRAINT plan_resource_plans_version_id_department_id_designation_id_key UNIQUE (version_id, department_id, designation_id, period_id);


--
-- Name: plan_sales_forecasts plan_sales_forecasts_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_sales_forecasts
    ADD CONSTRAINT plan_sales_forecasts_pkey PRIMARY KEY (id);


--
-- Name: plan_sales_forecasts plan_sales_forecasts_version_id_item_id_customer_id_period__key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.plan_sales_forecasts
    ADD CONSTRAINT plan_sales_forecasts_version_id_item_id_customer_id_period__key UNIQUE (version_id, item_id, customer_id, period_id);


--
-- Name: pm_issues pm_issues_company_id_code_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_issues
    ADD CONSTRAINT pm_issues_company_id_code_key UNIQUE (company_id, code);


--
-- Name: pm_issues pm_issues_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_issues
    ADD CONSTRAINT pm_issues_pkey PRIMARY KEY (id);


--
-- Name: pm_meeting_participants pm_meeting_participants_meeting_id_employee_id_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_meeting_participants
    ADD CONSTRAINT pm_meeting_participants_meeting_id_employee_id_key UNIQUE (meeting_id, employee_id);


--
-- Name: pm_meeting_participants pm_meeting_participants_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_meeting_participants
    ADD CONSTRAINT pm_meeting_participants_pkey PRIMARY KEY (id);


--
-- Name: pm_meetings pm_meetings_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_meetings
    ADD CONSTRAINT pm_meetings_pkey PRIMARY KEY (id);


--
-- Name: pm_milestones pm_milestones_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_milestones
    ADD CONSTRAINT pm_milestones_pkey PRIMARY KEY (id);


--
-- Name: pm_project_budgets pm_project_budgets_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_budgets
    ADD CONSTRAINT pm_project_budgets_pkey PRIMARY KEY (id);


--
-- Name: pm_project_categories pm_project_categories_company_id_name_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_categories
    ADD CONSTRAINT pm_project_categories_company_id_name_key UNIQUE (company_id, name);


--
-- Name: pm_project_categories pm_project_categories_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_categories
    ADD CONSTRAINT pm_project_categories_pkey PRIMARY KEY (id);


--
-- Name: pm_project_expenses pm_project_expenses_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_expenses
    ADD CONSTRAINT pm_project_expenses_pkey PRIMARY KEY (id);


--
-- Name: pm_project_members pm_project_members_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_members
    ADD CONSTRAINT pm_project_members_pkey PRIMARY KEY (id);


--
-- Name: pm_project_members pm_project_members_project_id_employee_id_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_members
    ADD CONSTRAINT pm_project_members_project_id_employee_id_key UNIQUE (project_id, employee_id);


--
-- Name: pm_project_phases pm_project_phases_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_phases
    ADD CONSTRAINT pm_project_phases_pkey PRIMARY KEY (id);


--
-- Name: pm_project_phases pm_project_phases_project_id_sequence_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_phases
    ADD CONSTRAINT pm_project_phases_project_id_sequence_key UNIQUE (project_id, sequence);


--
-- Name: pm_project_roles pm_project_roles_company_id_name_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_roles
    ADD CONSTRAINT pm_project_roles_company_id_name_key UNIQUE (company_id, name);


--
-- Name: pm_project_roles pm_project_roles_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_roles
    ADD CONSTRAINT pm_project_roles_pkey PRIMARY KEY (id);


--
-- Name: pm_project_status_history pm_project_status_history_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_status_history
    ADD CONSTRAINT pm_project_status_history_pkey PRIMARY KEY (id);


--
-- Name: pm_project_tags pm_project_tags_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_tags
    ADD CONSTRAINT pm_project_tags_pkey PRIMARY KEY (project_id, tag_id);


--
-- Name: pm_project_template_phases pm_project_template_phases_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_template_phases
    ADD CONSTRAINT pm_project_template_phases_pkey PRIMARY KEY (id);


--
-- Name: pm_project_templates pm_project_templates_company_id_name_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_templates
    ADD CONSTRAINT pm_project_templates_company_id_name_key UNIQUE (company_id, name);


--
-- Name: pm_project_templates pm_project_templates_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_project_templates
    ADD CONSTRAINT pm_project_templates_pkey PRIMARY KEY (id);


--
-- Name: pm_projects pm_projects_company_id_code_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_projects
    ADD CONSTRAINT pm_projects_company_id_code_key UNIQUE (company_id, code);


--
-- Name: pm_projects pm_projects_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_projects
    ADD CONSTRAINT pm_projects_pkey PRIMARY KEY (id);


--
-- Name: pm_resource_allocations pm_resource_allocations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_resource_allocations
    ADD CONSTRAINT pm_resource_allocations_pkey PRIMARY KEY (id);


--
-- Name: pm_risks pm_risks_company_id_code_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_risks
    ADD CONSTRAINT pm_risks_company_id_code_key UNIQUE (company_id, code);


--
-- Name: pm_risks pm_risks_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_risks
    ADD CONSTRAINT pm_risks_pkey PRIMARY KEY (id);


--
-- Name: pm_tags pm_tags_company_id_name_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_tags
    ADD CONSTRAINT pm_tags_company_id_name_key UNIQUE (company_id, name);


--
-- Name: pm_tags pm_tags_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_tags
    ADD CONSTRAINT pm_tags_pkey PRIMARY KEY (id);


--
-- Name: pm_task_assignments pm_task_assignments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_task_assignments
    ADD CONSTRAINT pm_task_assignments_pkey PRIMARY KEY (id);


--
-- Name: pm_task_assignments pm_task_assignments_task_id_employee_id_assignment_role_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_task_assignments
    ADD CONSTRAINT pm_task_assignments_task_id_employee_id_assignment_role_key UNIQUE (task_id, employee_id, assignment_role);


--
-- Name: pm_task_checklist_items pm_task_checklist_items_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_task_checklist_items
    ADD CONSTRAINT pm_task_checklist_items_pkey PRIMARY KEY (id);


--
-- Name: pm_task_dependencies pm_task_dependencies_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_task_dependencies
    ADD CONSTRAINT pm_task_dependencies_pkey PRIMARY KEY (id);


--
-- Name: pm_task_dependencies pm_task_dependencies_task_id_depends_on_task_id_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_task_dependencies
    ADD CONSTRAINT pm_task_dependencies_task_id_depends_on_task_id_key UNIQUE (task_id, depends_on_task_id);


--
-- Name: pm_tasks pm_tasks_company_id_code_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_tasks
    ADD CONSTRAINT pm_tasks_company_id_code_key UNIQUE (company_id, code);


--
-- Name: pm_tasks pm_tasks_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_tasks
    ADD CONSTRAINT pm_tasks_pkey PRIMARY KEY (id);


--
-- Name: pm_time_entries pm_time_entries_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_time_entries
    ADD CONSTRAINT pm_time_entries_pkey PRIMARY KEY (id);


--
-- Name: pm_timesheets pm_timesheets_employee_id_week_start_date_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_timesheets
    ADD CONSTRAINT pm_timesheets_employee_id_week_start_date_key UNIQUE (employee_id, week_start_date);


--
-- Name: pm_timesheets pm_timesheets_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.pm_timesheets
    ADD CONSTRAINT pm_timesheets_pkey PRIMARY KEY (id);


--
-- Name: retail_cash_movements retail_cash_movements_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_cash_movements
    ADD CONSTRAINT retail_cash_movements_pkey PRIMARY KEY (id);


--
-- Name: retail_cash_reconciliations retail_cash_reconciliations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_cash_reconciliations
    ADD CONSTRAINT retail_cash_reconciliations_pkey PRIMARY KEY (id);


--
-- Name: retail_coupons retail_coupons_company_id_code_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_coupons
    ADD CONSTRAINT retail_coupons_company_id_code_key UNIQUE (company_id, code);


--
-- Name: retail_coupons retail_coupons_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_coupons
    ADD CONSTRAINT retail_coupons_pkey PRIMARY KEY (id);


--
-- Name: retail_daily_closings retail_daily_closings_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_daily_closings
    ADD CONSTRAINT retail_daily_closings_pkey PRIMARY KEY (id);


--
-- Name: retail_daily_closings retail_daily_closings_store_id_closing_date_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_daily_closings
    ADD CONSTRAINT retail_daily_closings_store_id_closing_date_key UNIQUE (store_id, closing_date);


--
-- Name: retail_gift_card_transactions retail_gift_card_transactions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_gift_card_transactions
    ADD CONSTRAINT retail_gift_card_transactions_pkey PRIMARY KEY (id);


--
-- Name: retail_gift_cards retail_gift_cards_company_id_card_no_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_gift_cards
    ADD CONSTRAINT retail_gift_cards_company_id_card_no_key UNIQUE (company_id, card_no);


--
-- Name: retail_gift_cards retail_gift_cards_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_gift_cards
    ADD CONSTRAINT retail_gift_cards_pkey PRIMARY KEY (id);


--
-- Name: retail_loyalty_members retail_loyalty_members_customer_id_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_loyalty_members
    ADD CONSTRAINT retail_loyalty_members_customer_id_key UNIQUE (customer_id);


--
-- Name: retail_loyalty_members retail_loyalty_members_loyalty_code_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_loyalty_members
    ADD CONSTRAINT retail_loyalty_members_loyalty_code_key UNIQUE (loyalty_code);


--
-- Name: retail_loyalty_members retail_loyalty_members_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_loyalty_members
    ADD CONSTRAINT retail_loyalty_members_pkey PRIMARY KEY (id);


--
-- Name: retail_loyalty_transactions retail_loyalty_transactions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_loyalty_transactions
    ADD CONSTRAINT retail_loyalty_transactions_pkey PRIMARY KEY (id);


--
-- Name: retail_payment_methods retail_payment_methods_company_id_name_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_payment_methods
    ADD CONSTRAINT retail_payment_methods_company_id_name_key UNIQUE (company_id, name);


--
-- Name: retail_payment_methods retail_payment_methods_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_payment_methods
    ADD CONSTRAINT retail_payment_methods_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_exchanges retail_pos_exchanges_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_exchanges
    ADD CONSTRAINT retail_pos_exchanges_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_payments retail_pos_payments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_payments
    ADD CONSTRAINT retail_pos_payments_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_profiles retail_pos_profiles_company_id_name_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_profiles
    ADD CONSTRAINT retail_pos_profiles_company_id_name_key UNIQUE (company_id, name);


--
-- Name: retail_pos_profiles retail_pos_profiles_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_profiles
    ADD CONSTRAINT retail_pos_profiles_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_return_items retail_pos_return_items_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_return_items
    ADD CONSTRAINT retail_pos_return_items_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_returns retail_pos_returns_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_returns
    ADD CONSTRAINT retail_pos_returns_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_sale_items retail_pos_sale_items_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_sale_items
    ADD CONSTRAINT retail_pos_sale_items_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_sales retail_pos_sales_company_id_sale_no_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_sales
    ADD CONSTRAINT retail_pos_sales_company_id_sale_no_key UNIQUE (company_id, sale_no);


--
-- Name: retail_pos_sales retail_pos_sales_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_sales
    ADD CONSTRAINT retail_pos_sales_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_sessions retail_pos_sessions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_sessions
    ADD CONSTRAINT retail_pos_sessions_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_terminals retail_pos_terminals_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_terminals
    ADD CONSTRAINT retail_pos_terminals_pkey PRIMARY KEY (id);


--
-- Name: retail_pos_terminals retail_pos_terminals_store_id_terminal_code_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_pos_terminals
    ADD CONSTRAINT retail_pos_terminals_store_id_terminal_code_key UNIQUE (store_id, terminal_code);


--
-- Name: retail_price_rules retail_price_rules_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_price_rules
    ADD CONSTRAINT retail_price_rules_pkey PRIMARY KEY (id);


--
-- Name: retail_promotions retail_promotions_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_promotions
    ADD CONSTRAINT retail_promotions_pkey PRIMARY KEY (id);


--
-- Name: retail_stores retail_stores_company_id_code_key; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_stores
    ADD CONSTRAINT retail_stores_company_id_code_key UNIQUE (company_id, code);


--
-- Name: retail_stores retail_stores_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.retail_stores
    ADD CONSTRAINT retail_stores_pkey PRIMARY KEY (id);


--
-- Name: scm_batches scm_batches_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_batches
    ADD CONSTRAINT scm_batches_pkey PRIMARY KEY (id);


--
-- Name: scm_bins scm_bins_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_bins
    ADD CONSTRAINT scm_bins_pkey PRIMARY KEY (item_id, warehouse_id);


--
-- Name: scm_delivery_note_lines scm_delivery_note_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_delivery_note_lines
    ADD CONSTRAINT scm_delivery_note_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_delivery_notes scm_delivery_notes_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_delivery_notes
    ADD CONSTRAINT scm_delivery_notes_pkey PRIMARY KEY (id);


--
-- Name: scm_eway_bills scm_eway_bills_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_eway_bills
    ADD CONSTRAINT scm_eway_bills_pkey PRIMARY KEY (id);


--
-- Name: scm_leads scm_leads_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_leads
    ADD CONSTRAINT scm_leads_pkey PRIMARY KEY (id);


--
-- Name: scm_opportunities scm_opportunities_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_opportunities
    ADD CONSTRAINT scm_opportunities_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_invoice_lines scm_purchase_invoice_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_purchase_invoice_lines
    ADD CONSTRAINT scm_purchase_invoice_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_invoices scm_purchase_invoices_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_purchase_invoices
    ADD CONSTRAINT scm_purchase_invoices_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_order_lines scm_purchase_order_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_purchase_order_lines
    ADD CONSTRAINT scm_purchase_order_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_orders scm_purchase_orders_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_purchase_orders
    ADD CONSTRAINT scm_purchase_orders_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_receipt_lines scm_purchase_receipt_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_purchase_receipt_lines
    ADD CONSTRAINT scm_purchase_receipt_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_receipts scm_purchase_receipts_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_purchase_receipts
    ADD CONSTRAINT scm_purchase_receipts_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_request_lines scm_purchase_request_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_purchase_request_lines
    ADD CONSTRAINT scm_purchase_request_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_requests scm_purchase_requests_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_purchase_requests
    ADD CONSTRAINT scm_purchase_requests_pkey PRIMARY KEY (id);


--
-- Name: scm_purchase_returns scm_purchase_returns_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_purchase_returns
    ADD CONSTRAINT scm_purchase_returns_pkey PRIMARY KEY (id);


--
-- Name: scm_quotation_lines scm_quotation_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_quotation_lines
    ADD CONSTRAINT scm_quotation_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_quotations scm_quotations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_quotations
    ADD CONSTRAINT scm_quotations_pkey PRIMARY KEY (id);


--
-- Name: scm_rfq_lines scm_rfq_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_rfq_lines
    ADD CONSTRAINT scm_rfq_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_rfq_suppliers scm_rfq_suppliers_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_rfq_suppliers
    ADD CONSTRAINT scm_rfq_suppliers_pkey PRIMARY KEY (rfq_id, supplier_id);


--
-- Name: scm_rfqs scm_rfqs_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_rfqs
    ADD CONSTRAINT scm_rfqs_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_invoice_lines scm_sales_invoice_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_sales_invoice_lines
    ADD CONSTRAINT scm_sales_invoice_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_invoices scm_sales_invoices_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_sales_invoices
    ADD CONSTRAINT scm_sales_invoices_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_order_lines scm_sales_order_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_sales_order_lines
    ADD CONSTRAINT scm_sales_order_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_orders scm_sales_orders_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_sales_orders
    ADD CONSTRAINT scm_sales_orders_pkey PRIMARY KEY (id);


--
-- Name: scm_sales_returns scm_sales_returns_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_sales_returns
    ADD CONSTRAINT scm_sales_returns_pkey PRIMARY KEY (id);


--
-- Name: scm_serial_nos scm_serial_nos_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_serial_nos
    ADD CONSTRAINT scm_serial_nos_pkey PRIMARY KEY (id);


--
-- Name: scm_shipments scm_shipments_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_shipments
    ADD CONSTRAINT scm_shipments_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_entries scm_stock_entries_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_stock_entries
    ADD CONSTRAINT scm_stock_entries_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_entry_lines scm_stock_entry_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_stock_entry_lines
    ADD CONSTRAINT scm_stock_entry_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_ledger_entries scm_stock_ledger_entries_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_stock_ledger_entries
    ADD CONSTRAINT scm_stock_ledger_entries_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_transfer_lines scm_stock_transfer_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_stock_transfer_lines
    ADD CONSTRAINT scm_stock_transfer_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_stock_transfers scm_stock_transfers_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_stock_transfers
    ADD CONSTRAINT scm_stock_transfers_pkey PRIMARY KEY (id);


--
-- Name: scm_supplier_quotation_lines scm_supplier_quotation_lines_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_supplier_quotation_lines
    ADD CONSTRAINT scm_supplier_quotation_lines_pkey PRIMARY KEY (id);


--
-- Name: scm_supplier_quotations scm_supplier_quotations_pkey; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.scm_supplier_quotations
    ADD CONSTRAINT scm_supplier_quotations_pkey PRIMARY KEY (id);


--
-- Name: core_companies uq_core_companies_code; Type: CONSTRAINT; Schema: acme; Owner: -
--

ALTER TABLE ONLY acme.core_companies
    ADD CONSTRAINT uq_core_companies_code UNIQUE (code);


--
-- Name: admin_users admin_users_email_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_email_unique UNIQUE (email);


--
-- Name: admin_users admin_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_pkey PRIMARY KEY (id);


--
-- Name: currencies currencies_code_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.currencies
    ADD CONSTRAINT currencies_code_unique UNIQUE (code);


--
-- Name: currencies currencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.currencies
    ADD CONSTRAINT currencies_pkey PRIMARY KEY (id);


--
-- Name: exchange_rates exchange_rates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rates
    ADD CONSTRAINT exchange_rates_pkey PRIMARY KEY (id);


--
-- Name: menu_items menu_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.menu_items
    ADD CONSTRAINT menu_items_pkey PRIMARY KEY (id);


--
-- Name: modules modules_key_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.modules
    ADD CONSTRAINT modules_key_unique UNIQUE (key);


--
-- Name: modules modules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.modules
    ADD CONSTRAINT modules_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_version_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_version_unique UNIQUE (version);


--
-- Name: subscription_plans subscription_plans_code_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_plans
    ADD CONSTRAINT subscription_plans_code_unique UNIQUE (code);


--
-- Name: subscription_plans subscription_plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscription_plans
    ADD CONSTRAINT subscription_plans_pkey PRIMARY KEY (id);


--
-- Name: tenant_modules tenant_modules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_modules
    ADD CONSTRAINT tenant_modules_pkey PRIMARY KEY (id);


--
-- Name: tenant_modules tenant_modules_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_modules
    ADD CONSTRAINT tenant_modules_unique UNIQUE (tenant_slug, module_code);


--
-- Name: tenants tenants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_slug_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_slug_unique UNIQUE (slug);


--
-- Name: users users_email_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_unique UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: core_designation_permissions_designation_id_idx; Type: INDEX; Schema: _template; Owner: -
--

CREATE INDEX core_designation_permissions_designation_id_idx ON _template.core_designation_permissions USING btree (designation_id);


--
-- Name: core_designations_band_id_idx; Type: INDEX; Schema: _template; Owner: -
--

CREATE INDEX core_designations_band_id_idx ON _template.core_designations USING btree (band_id);


--
-- Name: core_designations_role_id_idx; Type: INDEX; Schema: _template; Owner: -
--

CREATE INDEX core_designations_role_id_idx ON _template.core_designations USING btree (role_id);


--
-- Name: core_roles_band_id_idx; Type: INDEX; Schema: _template; Owner: -
--

CREATE INDEX core_roles_band_id_idx ON _template.core_roles USING btree (band_id);


--
-- Name: core_designation_permissions_designation_id_idx; Type: INDEX; Schema: acme; Owner: -
--

CREATE INDEX core_designation_permissions_designation_id_idx ON acme.core_designation_permissions USING btree (designation_id);


--
-- Name: core_designations_band_id_idx; Type: INDEX; Schema: acme; Owner: -
--

CREATE INDEX core_designations_band_id_idx ON acme.core_designations USING btree (band_id);


--
-- Name: core_designations_role_id_idx; Type: INDEX; Schema: acme; Owner: -
--

CREATE INDEX core_designations_role_id_idx ON acme.core_designations USING btree (role_id);


--
-- Name: core_roles_band_id_idx; Type: INDEX; Schema: acme; Owner: -
--

CREATE INDEX core_roles_band_id_idx ON acme.core_roles USING btree (band_id);


--
-- Name: exchange_rates exchange_rates_from_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rates
    ADD CONSTRAINT exchange_rates_from_fkey FOREIGN KEY (from_currency_id) REFERENCES public.currencies(id);


--
-- Name: exchange_rates exchange_rates_to_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.exchange_rates
    ADD CONSTRAINT exchange_rates_to_fkey FOREIGN KEY (to_currency_id) REFERENCES public.currencies(id);


--
-- Name: menu_items menu_items_module_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.menu_items
    ADD CONSTRAINT menu_items_module_fkey FOREIGN KEY (module_id) REFERENCES public.modules(id);


--
-- Name: tenant_modules tenant_modules_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_modules
    ADD CONSTRAINT tenant_modules_fkey FOREIGN KEY (tenant_slug) REFERENCES public.tenants(slug);


--
-- Name: users users_tenant_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_fkey FOREIGN KEY (tenant_slug) REFERENCES public.tenants(slug);


--
-- PostgreSQL database dump complete
--

