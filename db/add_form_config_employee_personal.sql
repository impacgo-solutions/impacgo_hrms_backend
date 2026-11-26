-- add_form_config_employee_personal.sql (2026-10-29)
--
-- Form Configuration: Gender / Marital Status / Nationality of an employee are
-- now option lists users can extend ("Add custom..."). Labels may be up to 120
-- characters, so widen the columns (they were 15 / 20 / 50). Tenant table --
-- run per tenant schema and on _template. Additive and idempotent (widening a
-- varchar never loses data).
ALTER TABLE core_employees ALTER COLUMN gender         TYPE varchar(120);
ALTER TABLE core_employees ALTER COLUMN marital_status TYPE varchar(120);
ALTER TABLE core_employees ALTER COLUMN nationality    TYPE varchar(120);
