"""Administration > Notifications -- the admin-controlled, per-company,
per-event, per-channel (email / inapp) on/off matrix.

EVENT_CATALOG is the single source of truth for which notification types
exist, which channels each one supports, and what each channel's default is
(matching exactly what the code does today when no override row exists in
core_notification_settings -- see backend/db/add_notification_settings.sql).
A handful of rows mark a channel "(new)" with default False: those are
genuinely new capabilities added alongside this module (e.g. an email for
contract-end reminders, an in-app row for promotion announcements) that do
not fire for anyone until an admin explicitly turns them on.

This module is intentionally free of any import from crud.py/email_service.py
so every other module can import it without risk of a circular import.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

# Each entry: event_key, category, label, and per-channel {available, default}.
EVENT_CATALOG: list[dict] = [
    # ---- Holidays & Celebrations --------------------------------------
    {"event_key": "holiday_reminder", "category": "Holidays & Celebrations", "label": "Holiday reminder",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "birthday", "category": "Holidays & Celebrations", "label": "Birthday wishes",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "work_anniversary", "category": "Holidays & Celebrations", "label": "Work anniversary",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    # ---- Contracts -------------------------------------------------------
    # Email goes to the Organization Owner(s); in-app also to HR and the
    # reporting manager (app/contract_lifecycle.py).
    {"event_key": "contract_end_reminder", "category": "Contracts", "label": "Contract expiring soon",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "contract_expired", "category": "Contracts", "label": "Contract expired",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},

    # ---- Leave & Attendance -------------------------------------------
    {"event_key": "leave_applied", "category": "Leave & Attendance", "label": "Leave request submitted",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "leave_decision", "category": "Leave & Attendance", "label": "Leave approved / rejected",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "leave_withdrawal", "category": "Leave & Attendance", "label": "Leave withdrawal request & decision",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "attendance_regularization", "category": "Leave & Attendance", "label": "Attendance regularization",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "work_entry_request", "category": "Leave & Attendance", "label": "Work entry notice",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "timesheet_request", "category": "Leave & Attendance", "label": "Timesheet submit / decide / unlock",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "attendance_exceptions_digest", "category": "Leave & Attendance", "label": "Attendance exceptions daily digest",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "overtime_request", "category": "Leave & Attendance", "label": "Overtime request submit & decide",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "overtime_missed_clock", "category": "Leave & Attendance", "label": "Overtime missed clock-in/out",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "overtime_completed", "category": "Leave & Attendance", "label": "Overtime completed notice",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "shift_reminder", "category": "Leave & Attendance", "label": "Shift clock-in/out & work-entry reminder",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},

    # ---- Payroll ---------------------------------------------------------
    {"event_key": "reimbursement_claim", "category": "Payroll", "label": "Reimbursement / expense claim",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "salary_revision_request", "category": "Payroll", "label": "Salary revision request",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "travel_request", "category": "Payroll", "label": "Travel request",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "expense_report", "category": "Payroll", "label": "Expense report",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "fnf_settlement", "category": "Payroll", "label": "Full & Final Settlement lifecycle",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},

    # ---- Recruitment & Onboarding ---------------------------------------
    {"event_key": "hiring_requisition", "category": "Recruitment & Onboarding", "label": "Hiring requisition submit & decide",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "offer_approval_needed", "category": "Recruitment & Onboarding", "label": "Offer approval needed",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "offer_decision_internal", "category": "Recruitment & Onboarding", "label": "Offer approved / decision recorded",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "offer_accepted", "category": "Recruitment & Onboarding", "label": "Candidate accepted offer (internal)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "offer_declined", "category": "Recruitment & Onboarding", "label": "Candidate declined offer (internal)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "offer_expired", "category": "Recruitment & Onboarding", "label": "Offer expired (internal)",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "new_joiner_ready", "category": "Recruitment & Onboarding", "label": "New joiner ready to join",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "verification_status_changed", "category": "Recruitment & Onboarding", "label": "Verification status changed",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "verification_failed", "category": "Recruitment & Onboarding", "label": "Verification failed",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "candidate_document_uploaded", "category": "Recruitment & Onboarding", "label": "Candidate document uploaded (internal)",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "new_joiner_assigned", "category": "Recruitment & Onboarding", "label": "New joiner in your team (to manager)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "new_joiner_no_show", "category": "Recruitment & Onboarding", "label": "New joiner no-show",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "positions_filled", "category": "Recruitment & Onboarding", "label": "Positions filled",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "candidate_hired_notice", "category": "Recruitment & Onboarding", "label": "Candidate hired (employee record created)",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "candidate_interview_scheduled", "category": "Recruitment & Onboarding", "label": "Interview scheduled (candidate)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": False, "default": False}}},
    {"event_key": "candidate_interview_rescheduled", "category": "Recruitment & Onboarding", "label": "Interview rescheduled (candidate)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": False, "default": False}}},
    {"event_key": "candidate_interview_cancelled", "category": "Recruitment & Onboarding", "label": "Interview cancelled (candidate)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": False, "default": False}}},
    {"event_key": "candidate_next_step", "category": "Recruitment & Onboarding", "label": "Moved to next stage (candidate)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": False, "default": False}}},
    {"event_key": "candidate_rejected", "category": "Recruitment & Onboarding", "label": "Application rejected (candidate)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": False, "default": False}}},
    {"event_key": "candidate_offer_withdrawn", "category": "Recruitment & Onboarding", "label": "Offer withdrawn (candidate)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": False, "default": False}}},
    {"event_key": "candidate_welcome", "category": "Recruitment & Onboarding", "label": "Welcome / joining formalities (candidate)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": False, "default": False}}},
    {"event_key": "candidate_document_reupload", "category": "Recruitment & Onboarding", "label": "Document re-upload request (candidate)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": False, "default": False}}},
    {"event_key": "candidate_info_requested", "category": "Recruitment & Onboarding", "label": "Additional info requested (candidate)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": False, "default": False}}},

    # ---- Interview Coordination (internal -- interviewers/recruiters) ---
    {"event_key": "job_opening_ready", "category": "Interview Coordination", "label": "Job opening ready to publish",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "candidate_selected_internal", "category": "Interview Coordination", "label": "Candidate selected (internal)",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "ready_for_final_selection", "category": "Interview Coordination", "label": "Ready for final selection",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "interview_scheduled_internal", "category": "Interview Coordination", "label": "Interview scheduled / invitation (internal)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "interview_cancelled_internal", "category": "Interview Coordination", "label": "Interview / round cancelled (internal)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "interview_response_internal", "category": "Interview Coordination", "label": "Interviewer response (accept/decline/reschedule)",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "interview_feedback_pending", "category": "Interview Coordination", "label": "Interview feedback pending",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "interview_feedback_complete", "category": "Interview Coordination", "label": "Interview feedback complete",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "interview_round_scheduled", "category": "Interview Coordination", "label": "Interview round scheduled",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "interview_round_membership_changed", "category": "Interview Coordination", "label": "Interview round membership changed",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "interview_decision_needed", "category": "Interview Coordination", "label": "Interview ended -- decision needed",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},
    {"event_key": "interview_outcome_recorded", "category": "Interview Coordination", "label": "Interview outcome recorded",
     "channels": {"email": {"available": True, "default": False}, "inapp": {"available": True, "default": True}}},

    # ---- Offboarding / Exit ----------------------------------------------
    {"event_key": "exit_request", "category": "Offboarding / Exit", "label": "Exit request submit & decide",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},

    # ---- Projects / Task Board --------------------------------------------
    {"event_key": "task_assigned", "category": "Projects / Task Board", "label": "Task assigned",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "task_started", "category": "Projects / Task Board", "label": "Task started",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "task_submitted_for_review", "category": "Projects / Task Board", "label": "Task submitted for review",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "task_reviewer_changed", "category": "Projects / Task Board", "label": "Task reviewer changed",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "task_approved_completed", "category": "Projects / Task Board", "label": "Task approved / completed",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "task_changes_requested", "category": "Projects / Task Board", "label": "Task changes requested",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},
    {"event_key": "task_reassigned", "category": "Projects / Task Board", "label": "Task reassigned",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},

    # ---- Assets ------------------------------------------------------------
    {"event_key": "asset_request", "category": "Assets", "label": "Asset request submit & decide",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": True}}},

    # ---- People --------------------------------------------------------------
    {"event_key": "promotion_announcement", "category": "People", "label": "Promotion announcement",
     "channels": {"email": {"available": True, "default": True}, "inapp": {"available": True, "default": False}}},
]

_CATALOG_BY_KEY: dict[str, dict] = {entry["event_key"]: entry for entry in EVENT_CATALOG}


def channel_enabled(db: Session, company_id: uuid.UUID | None, event_key: str | None, channel: str) -> bool:
    """Effective on/off for one (company, event, channel). Falls back to the
    catalog default when no override row exists -- this is what keeps every
    existing notification firing exactly as before until an admin changes a
    setting. Unknown event_key/company_id/channel never silently suppresses
    a send (returns True) -- this module must never be the reason a real
    notification silently stops being investigable."""
    if event_key is None or company_id is None:
        return True
    entry = _CATALOG_BY_KEY.get(event_key)
    if entry is None or channel not in entry["channels"]:
        return True
    spec = entry["channels"][channel]
    if not spec["available"]:
        return False
    row = db.execute(
        text(
            "SELECT enabled FROM core_notification_settings "
            "WHERE company_id = :company_id AND event_key = :event_key AND channel = :channel"
        ),
        {"company_id": company_id, "event_key": event_key, "channel": channel},
    ).scalar()
    return spec["default"] if row is None else bool(row)


def mark_fired(db: Session, company_id: uuid.UUID, event_key: str, entity_id: uuid.UUID) -> bool:
    """Atomic, channel-independent "has this (company, event, entity)
    already been announced" marker -- True the first time (caller should
    proceed and may then independently gate each channel), False if this
    exact (company, event, entity) already fired. Needed because a reminder
    job's own idempotency must not depend on whether the in-app or email
    channel happened to be enabled at the time (see add_notification_settings.sql)."""
    row = db.execute(
        text(
            "INSERT INTO core_notification_fired (id, company_id, event_key, entity_id, fired_at) "
            "VALUES (:id, :company_id, :event_key, :entity_id, :fired_at) "
            "ON CONFLICT (company_id, event_key, entity_id) DO NOTHING "
            "RETURNING id"
        ),
        {
            "id": uuid.uuid4(),
            "company_id": company_id,
            "event_key": event_key,
            "entity_id": entity_id,
            "fired_at": datetime.datetime.now(datetime.timezone.utc),
        },
    ).first()
    return row is not None


def get_settings(db: Session, company_id: uuid.UUID) -> list[dict]:
    """Full catalog, grouped for the API response, each channel carrying its
    availability and this company's current effective value."""
    rows = db.execute(
        text("SELECT event_key, channel, enabled FROM core_notification_settings WHERE company_id = :company_id"),
        {"company_id": company_id},
    ).all()
    overrides: dict[tuple[str, str], bool] = {(r.event_key, r.channel): r.enabled for r in rows}
    items = []
    for entry in EVENT_CATALOG:
        channels = []
        for channel, spec in entry["channels"].items():
            enabled = (
                False
                if not spec["available"]
                else overrides.get((entry["event_key"], channel), spec["default"])
            )
            channels.append({"channel": channel, "available": spec["available"], "enabled": enabled})
        items.append({
            "event_key": entry["event_key"],
            "category": entry["category"],
            "label": entry["label"],
            "channels": channels,
        })
    return items


def set_setting(
    db: Session,
    company_id: uuid.UUID,
    event_key: str,
    channel: str,
    enabled: bool,
    updated_by: uuid.UUID,
) -> dict:
    """Upsert one (company, event, channel) override. Raises ValueError for
    an unknown event_key/channel or a channel this event doesn't support
    (the router turns that into a 422)."""
    entry = _CATALOG_BY_KEY.get(event_key)
    if entry is None:
        raise ValueError(f"Unknown notification event: {event_key}")
    spec = entry["channels"].get(channel)
    if spec is None:
        raise ValueError(f"Unknown channel '{channel}' for event {event_key}")
    if not spec["available"]:
        raise ValueError(f"Event {event_key} has no {channel} channel")
    now = datetime.datetime.now(datetime.timezone.utc)
    db.execute(
        text(
            "INSERT INTO core_notification_settings "
            "(id, company_id, event_key, channel, enabled, updated_by, updated_at) "
            "VALUES (:id, :company_id, :event_key, :channel, :enabled, :updated_by, :updated_at) "
            "ON CONFLICT (company_id, event_key, channel) "
            "DO UPDATE SET enabled = :enabled, updated_by = :updated_by, updated_at = :updated_at"
        ),
        {
            "id": uuid.uuid4(),
            "company_id": company_id,
            "event_key": event_key,
            "channel": channel,
            "enabled": enabled,
            "updated_by": updated_by,
            "updated_at": now,
        },
    )
    return {
        "event_key": event_key,
        "category": entry["category"],
        "label": entry["label"],
        "channels": [
            {"channel": ch, "available": s["available"], "enabled": enabled if ch == channel else channel_enabled(db, company_id, event_key, ch)}
            for ch, s in entry["channels"].items()
        ],
    }
