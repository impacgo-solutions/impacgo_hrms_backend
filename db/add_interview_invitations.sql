-- Interview invitations (Recruitment > Interviews): interviewers are invited
-- first and accept / reject on availability; the organizer then schedules
-- the time, and each interviewer confirms, declines or asks to reschedule.
--
--   * hcm_interviews.scheduled_at may be empty while an interview is still
--     at the invitation stage (status invited / ready_to_schedule).
--   * hcm_interview_participants.attendance holds each interviewer's answer
--     (pending / accepted / declined / reschedule); the new columns keep the
--     reason, proposed availability, who invited them and who replaced a
--     declined interviewer (the declined row is kept -- nothing is deleted).
--
-- Idempotent; applied to every tenant schema and _template.

DO $$
DECLARE s text;
BEGIN
  FOR s IN
    SELECT table_schema FROM information_schema.tables WHERE table_name = 'hcm_interview_participants'
  LOOP
    EXECUTE format('ALTER TABLE %I.hcm_interviews ALTER COLUMN scheduled_at DROP NOT NULL', s);
    EXECUTE format('ALTER TABLE %I.hcm_interview_participants
                      ADD COLUMN IF NOT EXISTS response_reason text,
                      ADD COLUMN IF NOT EXISTS proposed_availability text,
                      ADD COLUMN IF NOT EXISTS responded_at timestamptz,
                      ADD COLUMN IF NOT EXISTS invited_at timestamptz,
                      ADD COLUMN IF NOT EXISTS invited_by uuid,
                      ADD COLUMN IF NOT EXISTS replaced_by uuid', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_interview_participants_employee
                      ON %I.hcm_interview_participants (employee_id)', s);
  END LOOP;
END $$;
