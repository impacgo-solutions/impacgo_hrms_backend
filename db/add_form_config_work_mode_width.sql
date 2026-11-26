-- add_form_config_work_mode_width.sql (2026-10-29)
--
-- "Add custom..." on the Work Mode dropdown (Form Configuration key
-- employee.work_mode) stores labels of up to 120 characters, so every column
-- that keeps a chosen work mode must be that wide (they were 20). Run per
-- tenant schema and on _template. Additive and idempotent (widening only).
-- hcm_attendance's own work_mode (the check-in verdict) is unrelated and untouched.
ALTER TABLE core_employees          ALTER COLUMN work_mode TYPE varchar(120);
ALTER TABLE hcm_job_openings        ALTER COLUMN work_mode TYPE varchar(120);
ALTER TABLE hcm_offers              ALTER COLUMN work_mode TYPE varchar(120);
ALTER TABLE hcm_preboardings        ALTER COLUMN work_mode TYPE varchar(120);
ALTER TABLE hcm_hiring_requisitions ALTER COLUMN work_mode TYPE varchar(120);
