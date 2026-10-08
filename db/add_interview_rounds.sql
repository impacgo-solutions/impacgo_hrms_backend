-- Round-based interviews (Recruitment > Job Opening > Interview Rounds):
-- while creating a job opening, HR/the opening creator defines interview
-- rounds (Round 1, Technical, HR, Final, ...) as SHARED sessions -- one
-- date/time + a checkbox-picked interviewer pool per round, applying to
-- every candidate who reaches that stage (a walk-in / panel day), not one
-- invite per candidate. Interviewers are notified with no accept/reject
-- step; if unavailable they contact the Owner/HR, who edits the round.
--
--   hcm_interview_rounds             NEW: one row per round per job opening
--                                     (round_key = the stage's key, so a
--                                     round is 1:1 with an interview_stages
--                                     pipeline stage by construction)
--   hcm_interview_round_interviewers NEW: the eligible-interviewer pool for
--                                     a round (soft-removable so reassigning
--                                     an interviewer keeps history)
--   hcm_interviews                   + round_id (set = this row is one
--                                     candidate's instance of a shared round;
--                                     NULL = an existing/legacy interview,
--                                     completely unaffected), start/end and
--                                     HR-internal-decision columns
--
-- The old invite -> accept/decline -> schedule flow
-- (backend/db/add_interview_invitations.sql) is untouched and keeps working
-- for any interview with round_id IS NULL; only new-style rounds skip it.
--
-- Idempotent; every tenant schema + _template. No existing row is deleted
-- and no existing column/value is altered -- additive only.

CREATE OR REPLACE FUNCTION pg_temp.ir_apply(s text) RETURNS void AS $f$
BEGIN
    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_interview_rounds (
            id uuid PRIMARY KEY,
            company_id uuid NOT NULL REFERENCES %1$I.core_companies(id),
            opening_id uuid NOT NULL REFERENCES %1$I.hcm_job_openings(id),
            round_key varchar(40) NOT NULL,
            name varchar(120) NOT NULL,
            sort_order smallint NOT NULL DEFAULT 1,
            interview_type varchar(20) NOT NULL DEFAULT 'interview'
                CHECK (interview_type IN ('interview', 'assessment')),
            scheduled_at timestamptz,
            scheduled_end timestamptz,
            mode varchar(12) CHECK (mode IN ('in_person', 'video', 'phone')),
            location text,
            meeting_link text,
            notes text,
            status varchar(12) NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled', 'cancelled')),
            cancel_reason text,
            reschedule_count smallint NOT NULL DEFAULT 0,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_by uuid,
            updated_at timestamptz,
            UNIQUE (opening_id, round_key)
        )
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_interview_rounds_opening ON %1$I.hcm_interview_rounds (opening_id, sort_order)', s);

    EXECUTE format($q$
        CREATE TABLE IF NOT EXISTS %1$I.hcm_interview_round_interviewers (
            id uuid PRIMARY KEY,
            round_id uuid NOT NULL REFERENCES %1$I.hcm_interview_rounds(id),
            employee_id uuid NOT NULL REFERENCES %1$I.core_employees(id),
            assigned_at timestamptz NOT NULL DEFAULT now(),
            assigned_by uuid,
            removed_at timestamptz,
            removed_by uuid
        )
    $q$, s);
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ix_iri_active ON %1$I.hcm_interview_round_interviewers (round_id, employee_id) WHERE removed_at IS NULL', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_iri_employee ON %1$I.hcm_interview_round_interviewers (employee_id) WHERE removed_at IS NULL', s);

    EXECUTE format($q$
        ALTER TABLE %1$I.hcm_interviews
            ADD COLUMN IF NOT EXISTS round_id uuid REFERENCES %1$I.hcm_interview_rounds(id),
            ADD COLUMN IF NOT EXISTS started_at timestamptz,
            ADD COLUMN IF NOT EXISTS started_by uuid,
            ADD COLUMN IF NOT EXISTS ended_at timestamptz,
            ADD COLUMN IF NOT EXISTS ended_by uuid,
            ADD COLUMN IF NOT EXISTS hr_decision varchar(12)
                CHECK (hr_decision IN ('advance', 'reject', 'hold')),
            ADD COLUMN IF NOT EXISTS hr_decision_notes text,
            ADD COLUMN IF NOT EXISTS hr_decision_at timestamptz,
            ADD COLUMN IF NOT EXISTS hr_decision_by uuid
    $q$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_interviews_round ON %1$I.hcm_interviews (round_id) WHERE round_id IS NOT NULL', s);
END
$f$ LANGUAGE plpgsql;

DO $$
DECLARE
    tenant RECORD;
BEGIN
    PERFORM pg_temp.ir_apply('_template');
    FOR tenant IN
        SELECT t.slug FROM public.tenants t
        WHERE EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = t.slug AND table_name = 'hcm_interviews'
        )
    LOOP
        PERFORM pg_temp.ir_apply(tenant.slug);
    END LOOP;
END $$;
