-- Adds the missing `suffix` column to core.number_series so it can drive
-- configurable Employee Code generation (prefix + zero-padded sequence +
-- suffix, e.g. EMP-0001, ACME-0001-HR). The table already had
-- prefix/padding/current_no/fiscal_infix from the original schema but no
-- suffix -- see backend/app/models.py NumberSeries and
-- backend/app/crud.py generate_employee_code.
--
-- Run against every schema that has a core_number_series table: _template
-- (so future tenants get it) and every already-provisioned tenant schema
-- (acme today).

ALTER TABLE _template.core_number_series
    ADD COLUMN IF NOT EXISTS suffix character varying(20) DEFAULT '' NOT NULL;

ALTER TABLE acme.core_number_series
    ADD COLUMN IF NOT EXISTS suffix character varying(20) DEFAULT '' NOT NULL;

-- Seed a series for "impacgo people" (the company this app's default
-- login/seed data actually targets, see .env DEFAULT_COMPANY_NAME) that
-- continues its existing IPP-001..IPP-039 convention instead of jumping to
-- a generic EMP-0001 default that would look out of place next to 39 real
-- employees. Every other company gets its series lazily auto-created on
-- first use (see crud.generate_employee_code) with a generic EMP- default
-- -- this is the one company worth a deliberate, continuity-preserving seed.
INSERT INTO acme.core_number_series (id, company_id, doctype, prefix, suffix, fiscal_infix, padding, current_no)
SELECT gen_random_uuid(), c.id, 'employee', 'IPP-', '', false, 3, 39
FROM acme.core_companies c
WHERE c.name = 'impacgo people'
  AND NOT EXISTS (
      SELECT 1 FROM acme.core_number_series ns
      WHERE ns.company_id = c.id AND ns.doctype = 'employee'
  );
