-- add_form_config_work_documents.sql (2026-10-29)
--
-- Form Configuration: Learning Documents category and Employee Document type
-- are now configurable lists users can add to ("Add custom..."). Admin / user
-- added options are stored verbatim (up to 120 characters), so the columns
-- that hold them are widened (they were sized for the old fixed lists).
-- Run per tenant schema and on _template. Additive and idempotent.

ALTER TABLE hcm_learning_documents ALTER COLUMN category      TYPE varchar(120);
ALTER TABLE hcm_document_records   ALTER COLUMN document_type TYPE varchar(120);
