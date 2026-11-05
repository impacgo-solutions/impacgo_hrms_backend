-- add_form_config_perf_recruit_learning_assets.sql (2026-10-28)
--
-- Form Configuration wiring for Performance / Recruitment / Learning / Assets.
-- Run AFTER add_form_configuration.sql, per tenant schema and on _template.
-- Additive and idempotent.

-- 1. Admin-added dropdown options are stored verbatim, so the columns that
--    hold them must be wide enough (they were sized for the old fixed lists).
ALTER TABLE hcm_training_courses ALTER COLUMN course_type TYPE varchar(60);
ALTER TABLE hcm_candidates       ALTER COLUMN source      TYPE varchar(120);
ALTER TABLE hcm_job_applications ALTER COLUMN source      TYPE varchar(120);

-- 2. Recruitment Settings used to hold its own "Candidate sources" list
--    (hcm_recruitment_settings.sources). The list now lives in Form
--    Configuration (candidate.source); carry a customised list over once, for
--    companies that have not been given the built-in option rows yet.
INSERT INTO hcm_form_field_options
       (id, company_id, field_key, value, label, sort_order, is_active, is_system)
SELECT gen_random_uuid(), s.company_id, 'candidate.source', t.val, t.val, (t.ord - 1)::int, true, false
FROM hcm_recruitment_settings s
CROSS JOIN LATERAL jsonb_array_elements_text(s.sources) WITH ORDINALITY AS t(val, ord)
WHERE s.sources IS NOT NULL
  AND jsonb_typeof(s.sources) = 'array'
  AND length(btrim(t.val)) > 0
  AND NOT EXISTS (SELECT 1 FROM hcm_form_field_options o
                  WHERE o.company_id = s.company_id AND o.field_key = 'candidate.source')
ON CONFLICT DO NOTHING;
