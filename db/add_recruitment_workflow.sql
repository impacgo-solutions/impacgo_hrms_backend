-- Recruitment workflow (Hiring Request -> Approval -> Job Opening ->
-- Application -> Screening -> Interview stages + feedback -> Selection ->
-- Offer (approval, send, response) -> Preboarding (tasks, documents,
-- verification) -> Joining -> Create Employee).
--
-- Reuses the existing recruitment tables and only ADDS what they lack:
--   hcm_hiring_requisitions   + position / budget / workflow columns, company_id (backfilled)
--   hcm_job_openings          + requisition link, pipeline stages, hiring team
--   hcm_candidates            + profile, resume, compensation columns
--   hcm_job_applications      + status (backfilled from stage), hold / reject / withdraw data
--   hcm_interviews            + stage, status (backfilled), mode, link, end time
--   hcm_offers                + terms, version, approval / send / response tracking
-- New tables:
--   hcm_recruitment_settings, hcm_recruitment_history,
--   hcm_interview_participants (backfilled from interviewer_id), hcm_interview_feedback,
--   hcm_preboardings, hcm_preboarding_tasks, hcm_preboarding_documents,
--   hcm_candidate_portal_links
--
-- Idempotent; every tenant schema + _template. No existing row is deleted
-- and no existing value is overwritten (backfills only fill NEW columns).

CREATE OR REPLACE FUNCTION pg_temp.rw_apply(s text) RETURNS void AS $f$
BEGIN
    -- status columns: wider, so the full status vocabularies fit
    EXECUTE format('ALTER TABLE %1$I.hcm_hiring_requisitions ALTER COLUMN status TYPE varchar(20)', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_offers ALTER COLUMN status TYPE varchar(20)', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_job_openings ALTER COLUMN status TYPE varchar(20)', s);
    EXECUTE format('ALTER TABLE %1$I.hcm_job_applications ALTER COLUMN stage TYPE varchar(20)', s);

    -- ── requisitions ────────────────────────────────────────────────────
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_hiring_requisitions
            ADD COLUMN IF NOT EXISTS company_id uuid,
            ADD COLUMN IF NOT EXISTS employment_type varchar(20),
            ADD COLUMN IF NOT EXISTS work_mode varchar(20),
            ADD COLUMN IF NOT EXISTS branch_id uuid,
            ADD COLUMN IF NOT EXISTS sub_department_id uuid,
            ADD COLUMN IF NOT EXISTS reporting_manager_id uuid,
            ADD COLUMN IF NOT EXISTS target_joining_date date,
            ADD COLUMN IF NOT EXISTS experience_min numeric(4,1),
            ADD COLUMN IF NOT EXISTS experience_max numeric(4,1),
            ADD COLUMN IF NOT EXISTS salary_min numeric(14,2),
            ADD COLUMN IF NOT EXISTS salary_max numeric(14,2),
            ADD COLUMN IF NOT EXISTS required_skills text,
            ADD COLUMN IF NOT EXISTS qualifications text,
            ADD COLUMN IF NOT EXISTS job_description text,
            ADD COLUMN IF NOT EXISTS budget_cost_center varchar(120),
            ADD COLUMN IF NOT EXISTS priority varchar(10),
            ADD COLUMN IF NOT EXISTS hiring_team jsonb NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS interview_stages jsonb,
            ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1,
            ADD COLUMN IF NOT EXISTS submitted_at timestamptz,
            ADD COLUMN IF NOT EXISTS cancel_reason text,
            ADD COLUMN IF NOT EXISTS opening_id uuid,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS created_at timestamptz,
            ADD COLUMN IF NOT EXISTS updated_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamptz
    $q$, s);
    EXECUTE format($q$
        UPDATE %1$I.hcm_hiring_requisitions r SET company_id = e.company_id
        FROM %1$I.core_employees e
        WHERE r.company_id IS NULL AND e.id = r.requested_by
    $q$, s);

    -- ── job openings ────────────────────────────────────────────────────
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_job_openings
            ADD COLUMN IF NOT EXISTS requisition_id uuid,
            ADD COLUMN IF NOT EXISTS work_mode varchar(20),
            ADD COLUMN IF NOT EXISTS reporting_manager_id uuid,
            ADD COLUMN IF NOT EXISTS experience_min numeric(4,1),
            ADD COLUMN IF NOT EXISTS experience_max numeric(4,1),
            ADD COLUMN IF NOT EXISTS salary_min numeric(14,2),
            ADD COLUMN IF NOT EXISTS salary_max numeric(14,2),
            ADD COLUMN IF NOT EXISTS required_skills text,
            ADD COLUMN IF NOT EXISTS qualifications text,
            ADD COLUMN IF NOT EXISTS target_joining_date date,
            ADD COLUMN IF NOT EXISTS hiring_team jsonb NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS interview_stages jsonb,
            ADD COLUMN IF NOT EXISTS publish_scope varchar(10),
            ADD COLUMN IF NOT EXISTS published_at timestamptz,
            ADD COLUMN IF NOT EXISTS closed_at timestamptz,
            ADD COLUMN IF NOT EXISTS deleted_at timestamptz
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_job_openings_requisition ON %1$I.hcm_job_openings (requisition_id)', s);

    -- ── candidates ──────────────────────────────────────────────────────
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_candidates
            ADD COLUMN IF NOT EXISTS current_company varchar(150),
            ADD COLUMN IF NOT EXISTS current_designation varchar(150),
            ADD COLUMN IF NOT EXISTS current_ctc numeric(14,2),
            ADD COLUMN IF NOT EXISTS expected_ctc numeric(14,2),
            ADD COLUMN IF NOT EXISTS notice_period_days smallint,
            ADD COLUMN IF NOT EXISTS current_location varchar(150),
            ADD COLUMN IF NOT EXISTS skills text,
            ADD COLUMN IF NOT EXISTS qualification varchar(200),
            ADD COLUMN IF NOT EXISTS work_authorization varchar(80),
            ADD COLUMN IF NOT EXISTS linkedin_url varchar(300),
            ADD COLUMN IF NOT EXISTS resume_url text,
            ADD COLUMN IF NOT EXISTS resume_filename varchar(255),
            ADD COLUMN IF NOT EXISTS resume_uploaded_at timestamptz,
            ADD COLUMN IF NOT EXISTS notes text,
            ADD COLUMN IF NOT EXISTS created_at timestamptz,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamptz,
            ADD COLUMN IF NOT EXISTS deleted_at timestamptz
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_candidates_email ON %1$I.hcm_candidates (company_id, lower(email))', s);

    -- ── applications ────────────────────────────────────────────────────
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_job_applications
            ADD COLUMN IF NOT EXISTS status varchar(20),
            ADD COLUMN IF NOT EXISTS stage_index smallint,
            ADD COLUMN IF NOT EXISTS stage_results jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS previous_status varchar(20),
            ADD COLUMN IF NOT EXISTS hold_reason text,
            ADD COLUMN IF NOT EXISTS hold_review_date date,
            ADD COLUMN IF NOT EXISTS rejection_reason varchar(200),
            ADD COLUMN IF NOT EXISTS rejection_notes text,
            ADD COLUMN IF NOT EXISTS withdrawal_source varchar(20),
            ADD COLUMN IF NOT EXISTS withdrawal_reason text,
            ADD COLUMN IF NOT EXISTS closed_reason varchar(40),
            ADD COLUMN IF NOT EXISTS source varchar(40),
            ADD COLUMN IF NOT EXISTS owner_id uuid,
            ADD COLUMN IF NOT EXISTS applied_at timestamptz,
            ADD COLUMN IF NOT EXISTS current_stage_entered_at timestamptz,
            ADD COLUMN IF NOT EXISTS notes text,
            ADD COLUMN IF NOT EXISTS employee_id uuid,
            ADD COLUMN IF NOT EXISTS created_at timestamptz,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS updated_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamptz,
            ADD COLUMN IF NOT EXISTS deleted_at timestamptz
    $q$, s);
    -- Existing applications: status from the legacy stage, dates from created_at.
    EXECUTE format($q$
        UPDATE %1$I.hcm_job_applications SET status = CASE lower(stage)
                WHEN 'screened' THEN 'screening'
                WHEN 'interview' THEN 'interview'
                WHEN 'offer' THEN 'offer'
                WHEN 'hired' THEN 'hired'
                ELSE 'applied' END
        WHERE status IS NULL
    $q$, s);
    EXECUTE format('UPDATE %1$I.hcm_job_applications SET applied_at = COALESCE(created_at, now()) WHERE applied_at IS NULL', s);
    EXECUTE format('UPDATE %1$I.hcm_job_applications SET current_stage_entered_at = COALESCE(created_at, now()) WHERE current_stage_entered_at IS NULL', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_job_applications_opening ON %1$I.hcm_job_applications (opening_id, status)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_job_applications_candidate ON %1$I.hcm_job_applications (candidate_id)', s);

    -- ── interviews ──────────────────────────────────────────────────────
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_interviews
            ADD COLUMN IF NOT EXISTS stage_key varchar(40),
            ADD COLUMN IF NOT EXISTS stage_name varchar(120),
            ADD COLUMN IF NOT EXISTS interview_type varchar(20),
            ADD COLUMN IF NOT EXISTS status varchar(24),
            ADD COLUMN IF NOT EXISTS scheduled_end timestamptz,
            ADD COLUMN IF NOT EXISTS mode varchar(12),
            ADD COLUMN IF NOT EXISTS location text,
            ADD COLUMN IF NOT EXISTS meeting_link text,
            ADD COLUMN IF NOT EXISTS candidate_attendance varchar(12),
            ADD COLUMN IF NOT EXISTS notes text,
            ADD COLUMN IF NOT EXISTS cancel_reason text,
            ADD COLUMN IF NOT EXISTS reschedule_count smallint NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS feedback_required boolean NOT NULL DEFAULT true,
            ADD COLUMN IF NOT EXISTS completed_at timestamptz,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS created_at timestamptz,
            ADD COLUMN IF NOT EXISTS updated_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamptz
    $q$, s);
    EXECUTE format($q$
        UPDATE %1$I.hcm_interviews SET status = CASE WHEN result IS NULL THEN 'scheduled' ELSE 'feedback_completed' END
        WHERE status IS NULL
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_interviews_application ON %1$I.hcm_interviews (application_id)', s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_interview_participants (
            id uuid PRIMARY KEY,
            interview_id uuid NOT NULL REFERENCES %1$I.hcm_interviews(id),
            employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
            role varchar(20) NOT NULL DEFAULT 'interviewer',
            attendance varchar(12),
            UNIQUE (interview_id, employee_id)
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_interview_participants_employee ON %1$I.hcm_interview_participants (employee_id)', s);
    EXECUTE format($q$
        INSERT INTO %1$I.hcm_interview_participants (id, interview_id, employee_id)
        SELECT gen_random_uuid(), i.id, i.interviewer_id FROM %1$I.hcm_interviews i
        WHERE i.interviewer_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM %1$I.core_employees e WHERE e.id = i.interviewer_id)
        ON CONFLICT (interview_id, employee_id) DO NOTHING
    $q$, s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_interview_feedback (
            id uuid PRIMARY KEY,
            interview_id uuid NOT NULL REFERENCES %1$I.hcm_interviews(id),
            interviewer_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
            recommendation varchar(12) NOT NULL CHECK (recommendation IN ('pass', 'fail', 'hold')),
            rating smallint CHECK (rating BETWEEN 1 AND 5),
            scores jsonb NOT NULL DEFAULT '{}'::jsonb,
            strengths text,
            concerns text,
            comments text,
            submitted_by uuid,
            submitted_at timestamptz NOT NULL,
            updated_at timestamptz,
            UNIQUE (interview_id, interviewer_id)
        )
    $q$, s);

    -- ── offers ──────────────────────────────────────────────────────────
    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_offers
            ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1,
            ADD COLUMN IF NOT EXISTS designation varchar(150),
            ADD COLUMN IF NOT EXISTS department_id uuid,
            ADD COLUMN IF NOT EXISTS branch_id uuid,
            ADD COLUMN IF NOT EXISTS work_mode varchar(20),
            ADD COLUMN IF NOT EXISTS employment_type varchar(20),
            ADD COLUMN IF NOT EXISTS reporting_manager_id uuid,
            ADD COLUMN IF NOT EXISTS salary_breakup text,
            ADD COLUMN IF NOT EXISTS benefits text,
            ADD COLUMN IF NOT EXISTS probation_months smallint,
            ADD COLUMN IF NOT EXISTS notice_period_days smallint,
            ADD COLUMN IF NOT EXISTS working_hours varchar(80),
            ADD COLUMN IF NOT EXISTS expiry_date date,
            ADD COLUMN IF NOT EXISTS terms text,
            ADD COLUMN IF NOT EXISTS submitted_at timestamptz,
            ADD COLUMN IF NOT EXISTS submitted_by uuid,
            ADD COLUMN IF NOT EXISTS approved_at timestamptz,
            ADD COLUMN IF NOT EXISTS approved_by uuid,
            ADD COLUMN IF NOT EXISTS decision_notes text,
            ADD COLUMN IF NOT EXISTS sent_at timestamptz,
            ADD COLUMN IF NOT EXISTS sent_count integer NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_sent_to varchar(150),
            ADD COLUMN IF NOT EXISTS viewed_at timestamptz,
            ADD COLUMN IF NOT EXISTS responded_at timestamptz,
            ADD COLUMN IF NOT EXISTS response_source varchar(20),
            ADD COLUMN IF NOT EXISTS accepted_at timestamptz,
            ADD COLUMN IF NOT EXISTS decline_reason text,
            ADD COLUMN IF NOT EXISTS withdraw_reason text,
            ADD COLUMN IF NOT EXISTS created_by uuid,
            ADD COLUMN IF NOT EXISTS created_at timestamptz,
            ADD COLUMN IF NOT EXISTS updated_by uuid,
            ADD COLUMN IF NOT EXISTS updated_at timestamptz
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_offers_application ON %1$I.hcm_offers (application_id)', s);

    -- ── settings / history ──────────────────────────────────────────────
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_recruitment_settings (
            company_id uuid PRIMARY KEY REFERENCES %1$I.core_companies(id),
            offer_approval_required boolean NOT NULL DEFAULT true,
            offer_expiry_days smallint NOT NULL DEFAULT 7,
            default_interview_stages jsonb,
            sources jsonb,
            rejection_reasons jsonb,
            preboarding_checklist jsonb,
            verification_failure_policy varchar(10) NOT NULL DEFAULT 'hold'
                CHECK (verification_failure_policy IN ('hold', 'reject')),
            allow_verification_override boolean NOT NULL DEFAULT true,
            updated_by uuid,
            updated_at timestamptz
        )
    $q$, s);
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_recruitment_history (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL,
            entity_type varchar(30) NOT NULL,
            entity_id uuid NOT NULL,
            application_id uuid,
            candidate_id uuid,
            action varchar(60) NOT NULL,
            old_status varchar(30),
            new_status varchar(30),
            comments text,
            metadata jsonb,
            actor_user_id uuid,
            actor_name varchar(150),
            created_at timestamptz NOT NULL
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_recruitment_history_application ON %1$I.hcm_recruitment_history (application_id, created_at)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_recruitment_history_entity ON %1$I.hcm_recruitment_history (entity_type, entity_id, created_at)', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_recruitment_history_candidate ON %1$I.hcm_recruitment_history (candidate_id, created_at)', s);

    -- ── preboarding ─────────────────────────────────────────────────────
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_preboardings (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
            application_id uuid NOT NULL UNIQUE REFERENCES %1$I.hcm_job_applications(id),
            candidate_id uuid NOT NULL REFERENCES %1$I.hcm_candidates(id),
            offer_id uuid REFERENCES %1$I.hcm_offers(id),
            status varchar(24) NOT NULL,
            joining_date date,
            designation varchar(150),
            department_id uuid,
            branch_id uuid,
            reporting_manager_id uuid,
            employment_type varchar(20),
            work_mode varchar(20),
            joiner_details jsonb NOT NULL DEFAULT '{}'::jsonb,
            verification_status varchar(24) NOT NULL DEFAULT 'pending',
            verification_override boolean NOT NULL DEFAULT false,
            override_reason text,
            joining_status varchar(12),
            joining_confirmed_at timestamptz,
            joining_confirmed_by uuid,
            joining_notes text,
            cancel_reason text,
            no_show_reason text,
            employee_status varchar(12) NOT NULL DEFAULT 'not_created',
            employee_id uuid,
            employee_error text,
            employee_created_at timestamptz,
            started_at timestamptz,
            completed_at timestamptz,
            created_by uuid,
            created_at timestamptz,
            updated_by uuid,
            updated_at timestamptz
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_preboardings_company ON %1$I.hcm_preboardings (company_id, status)', s);
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_preboarding_tasks (
            id uuid PRIMARY KEY,
            preboarding_id uuid NOT NULL REFERENCES %1$I.hcm_preboardings(id),
            category varchar(30) NOT NULL,
            name varchar(150) NOT NULL,
            task_type varchar(16) NOT NULL
                CHECK (task_type IN ('document', 'information', 'verification', 'acknowledgement')),
            field_group varchar(20),
            required boolean NOT NULL DEFAULT true,
            status varchar(24) NOT NULL,
            sort_order smallint NOT NULL DEFAULT 0,
            due_date date,
            notes text,
            review_notes text,
            completed_at timestamptz,
            completed_by uuid,
            created_at timestamptz,
            updated_at timestamptz
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_preboarding_tasks_parent ON %1$I.hcm_preboarding_tasks (preboarding_id, sort_order)', s);
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_preboarding_documents (
            id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES %1$I.hcm_preboarding_tasks(id),
            preboarding_id uuid NOT NULL REFERENCES %1$I.hcm_preboardings(id),
            version integer NOT NULL,
            file_url text NOT NULL,
            original_filename varchar(255) NOT NULL,
            content_type varchar(100),
            file_size integer,
            status varchar(16) NOT NULL CHECK (status IN ('under_review', 'approved', 'rejected')),
            review_notes text,
            uploaded_by uuid,
            uploaded_by_name varchar(150),
            uploaded_via varchar(12) NOT NULL DEFAULT 'hr',
            uploaded_at timestamptz NOT NULL,
            reviewed_by uuid,
            reviewed_by_name varchar(150),
            reviewed_at timestamptz,
            UNIQUE (task_id, version)
        )
    $q$, s);

    -- Candidate self-service links (offer response, preboarding uploads):
    -- only a SHA-256 of the token is stored.
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_candidate_portal_links (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL,
            purpose varchar(12) NOT NULL CHECK (purpose IN ('offer', 'preboarding')),
            entity_id uuid NOT NULL,
            token_hash varchar(64) NOT NULL UNIQUE,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            last_used_at timestamptz,
            created_by uuid,
            created_at timestamptz NOT NULL
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_candidate_portal_links_entity ON %1$I.hcm_candidate_portal_links (purpose, entity_id)', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.rw_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_job_applications'
        )
    LOOP
        PERFORM pg_temp.rw_apply(tenant.slug);
    END LOOP;
END $$;
