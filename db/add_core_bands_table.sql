-- Tenant-specific Band master table -- backs the new "Band Management"
-- system (backend/app/routers/bands.py). Each company manages its own Band
-- rows independently; company_id scoping plus this project's existing
-- schema-per-tenant isolation (each tenant schema gets its own copy of this
-- table, same as every other core_* table) gives complete tenant isolation.
--
-- band_number is a stable per-company integer identity, auto-assigned at
-- creation time (max+1 per company) -- it exists so core_employees.band
-- (an existing, unrelated integer column with no FK, untouched by this
-- migration) keeps meaning the same thing it always has: the Add Employee
-- "Grade / Band" dropdown still writes a plain int there, just now sourced
-- from this table instead of the old hardcoded designation_seed_data.py
-- list. No FK is added anywhere -- this is purely additive, matching how
-- every other "no DB-level unique/foreign key, app-level advisory lock
-- instead" table in this schema already works (core_designations,
-- core_branches, etc.).
--
-- Run against every schema that needs it: _template (future tenants) and
-- every already-provisioned tenant schema (acme today).

CREATE TABLE IF NOT EXISTS _template.core_bands (
    id           uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id   uuid NOT NULL,
    name         character varying(80) NOT NULL,
    code         character varying(20) NOT NULL,
    band_number  integer NOT NULL,
    description  text,
    is_active    boolean DEFAULT true NOT NULL,
    created_by   uuid,
    created_at   timestamp with time zone DEFAULT now() NOT NULL,
    updated_by   uuid,
    updated_at   timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT core_bands_pkey PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS acme.core_bands (
    id           uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id   uuid NOT NULL,
    name         character varying(80) NOT NULL,
    code         character varying(20) NOT NULL,
    band_number  integer NOT NULL,
    description  text,
    is_active    boolean DEFAULT true NOT NULL,
    created_by   uuid,
    created_at   timestamp with time zone DEFAULT now() NOT NULL,
    updated_by   uuid,
    updated_at   timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT core_bands_pkey PRIMARY KEY (id)
);

-- Backward compatibility: seed every EXISTING company (in the acme schema)
-- with the same 10 bands the app has always shown via the hardcoded
-- backend/app/designation_seed_data.py DESIGNATION_BANDS list, in the same
-- order/numbering -- so no existing tenant's Add Employee "Grade / Band"
-- dropdown changes what it offers on day one. New companies created after
-- this migration get the same defaults lazily (see
-- crud.get_or_seed_company_bands), not from this one-time INSERT.
INSERT INTO acme.core_bands (company_id, name, code, band_number, description)
SELECT c.id, band.name, band.code, band.band_number, NULL
FROM acme.core_companies c
CROSS JOIN (VALUES
    (1, 'Ownership / Founding', 'BAND-01'),
    (2, 'C-Level Executive', 'BAND-02'),
    (3, 'VP Level', 'BAND-03'),
    (4, 'Director Level', 'BAND-04'),
    (5, 'General Management', 'BAND-05'),
    (6, 'Management', 'BAND-06'),
    (7, 'Team Lead', 'BAND-07'),
    (8, 'Professional / IC (Senior)', 'BAND-08'),
    (9, 'Professional / IC', 'BAND-09'),
    (10, 'Associate / Entry Level', 'BAND-10')
) AS band(band_number, name, code)
WHERE NOT EXISTS (
    SELECT 1 FROM acme.core_bands b WHERE b.company_id = c.id
);
