"""Daily reminder emails / notifications that must go out even when nobody
opens the app that day:

  * Birthdays and work anniversaries -- crud.check_daily_celebrations (the
    same routine GET /notifications runs on read; idempotent per employee,
    type and year via hcm_celebration_log).
  * Exit access revocation -- employees past an approved exit's last
    working day lose login access (app/access_lifecycle.py), every pass.
  * Holiday reminders -- REMINDER_HOLIDAY_DAYS_BEFORE days before each
    company holiday (branch holidays only to that branch's employees);
    once per holiday, marked by its in-app notification rows.

A daemon thread in the API process wakes every REMINDER_POLL_MINUTES and,
per tenant and company, runs the jobs once the company's local time has
reached REMINDER_SEND_HOUR. Several API workers are safe: each tenant pass
holds a PostgreSQL advisory lock, and every job is idempotent in the
database. Emails are queued through email_service (sent after commit,
logged in core_email_logs, EMAIL_ALLOWED_DOMAINS respected).
"""

from __future__ import annotations

import datetime
import logging
import threading
import uuid
import zlib

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import crud, database, email_service, models
from .config import settings

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop = threading.Event()


def _lock_key(slug: str, company_id: uuid.UUID) -> int:
    return zlib.crc32(f"hrms-reminders:{slug}:{company_id}".encode()) & 0x7FFFFFFF


def holiday_reminders(db: Session, company_id: uuid.UUID, today: datetime.date) -> int:
    """Reminds employees of the holidays falling REMINDER_HOLIDAY_DAYS_BEFORE
    days from [today]. Returns how many holidays were announced."""
    days_before = max(0, int(settings.reminder_holiday_days_before))
    target = today + datetime.timedelta(days=days_before)
    holidays = db.scalars(
        select(models.Holiday).where(models.Holiday.company_id == company_id, models.Holiday.holiday_date == target)
    ).all()
    announced = 0
    for holiday in holidays:
        # Once per holiday: its in-app notifications are the marker.
        if db.scalar(
            select(models.Notification.id).where(
                models.Notification.company_id == company_id,
                models.Notification.entity_type == "holiday",
                models.Notification.entity_id == holiday.id,
            ).limit(1)
        ) is not None:
            continue
        employees_q = select(models.Employee).where(
            models.Employee.company_id == company_id, models.Employee.is_active.is_(True)
        )
        if holiday.branch_id is not None:
            employees_q = employees_q.where(models.Employee.branch_id == holiday.branch_id)
        employees = db.scalars(employees_q).all()
        branch = db.get(models.Branch, holiday.branch_id) if holiday.branch_id else None
        when = "Tomorrow" if days_before == 1 else ("Today" if days_before == 0 else f"In {days_before} days")
        long_date = f"{holiday.holiday_date.strftime('%A')}, {holiday.holiday_date.day} {holiday.holiday_date.strftime('%B %Y')}"
        if holiday.is_optional:
            title = f"Optional Holiday {when}: {holiday.name}"
            body = (f"{long_date} is an optional holiday ({holiday.name}). If you'd like to take it, "
                    "apply for it in Leave before the day.")
        else:
            title = f"Holiday {when}: {holiday.name}"
            body = f"{long_date} is a company holiday ({holiday.name})" + (
                f" for the {branch.name} branch." if branch else ".")
        employee_ids = [e.id for e in employees]
        user_ids = set(db.scalars(
            select(models.User.id).where(models.User.employee_id.in_(employee_ids), models.User.status == "active")
        ).all()) if employee_ids else set()
        for user_id in user_ids:
            crud.create_notification(db, company_id, user_id, title=title, body=body,
                                     entity_type="holiday", entity_id=holiday.id)
        for employee in employees:
            email_service.send_holiday_reminder_email(
                employee.work_email, db=db, company_id=company_id, holiday_id=holiday.id,
                subject=title, heading=title, body=body, holiday_name=holiday.name,
                holiday_date=long_date, optional=holiday.is_optional,
                branch_name=branch.name if branch else None,
            )
        announced += 1
    return announced


CONTRACT_REMINDER_DAYS = (30, 15, 7)


def contract_reminder_user_ids(db: Session, company_id: uuid.UUID, employee: models.Employee) -> set:
    """Owner(s), HR (People edit access) and the employee's reporting
    manager -- active login accounts only."""
    from .deps import _OWNER_ROLE_NAME

    users = db.scalars(select(models.User).where(models.User.company_id == company_id,
                                                 models.User.status == "active")).all()
    out = set()
    for u in users:
        role = crud.get_user_primary_role(db, u.id)
        if (role is not None and role.name == _OWNER_ROLE_NAME) or crud.can_access_people_module(db, u, "edit"):
            out.add(u.id)
        elif employee.reporting_manager_id and u.employee_id == employee.reporting_manager_id:
            out.add(u.id)
    out.discard(next((u.id for u in users if u.employee_id == employee.id), None))  # not the contractor
    return out


def contract_end_reminders(db: Session, company_id: uuid.UUID, today: datetime.date) -> int:
    """Contract employees whose contract_end_date is within 30 / 15 / 7
    days: notify HR, the Owner and the reporting manager. Once per employee,
    milestone and end date (the in-app notification is the marker), so a
    renewal (new end date) starts the reminders again; a missed day still
    sends the nearest milestone, never several at once. Reminders only --
    nothing is ever ended or changed automatically."""
    horizon = today + datetime.timedelta(days=max(CONTRACT_REMINDER_DAYS))
    employees = db.scalars(select(models.Employee).where(
        models.Employee.company_id == company_id, models.Employee.is_active.is_(True),
        models.Employee.contract_end_date.is_not(None),
        models.Employee.contract_end_date >= today, models.Employee.contract_end_date <= horizon,
    )).all()
    sent = 0
    for employee in employees:
        if not crud.is_contract_employee(employee):
            continue
        days_left = (employee.contract_end_date - today).days
        milestone = min(m for m in CONTRACT_REMINDER_DAYS if days_left <= m)
        end = employee.contract_end_date
        marker = f"[{milestone}d·{end.isoformat()}]"
        if db.scalar(select(models.Notification.id).where(
            models.Notification.company_id == company_id,
            models.Notification.entity_type == "contract_expiry",
            models.Notification.entity_id == employee.id,
            models.Notification.body.contains(marker),
        ).limit(1)) is not None:
            continue
        name = f"{employee.first_name} {employee.last_name or ''}".strip()
        when = "today" if days_left == 0 else ("tomorrow" if days_left == 1 else f"in {days_left} days")
        title = f"Contract ends {when}: {name}"[:150]
        body = (f"{name}'s contract ends on {end.strftime('%d %b %Y')}. Renew it, convert them to permanent, "
                f"or start their exit in People. Nothing changes automatically. {marker}")
        for user_id in contract_reminder_user_ids(db, company_id, employee):
            crud.create_notification(db, company_id, user_id, title=title, body=body,
                                     entity_type="contract_expiry", entity_id=employee.id)
        sent += 1
    return sent


def run_tenant(slug: str, *, force: bool = False) -> dict:
    """One pass for one tenant. [force] ignores REMINDER_SEND_HOUR (tests /
    manual runs). Each company runs in its own transaction holding a
    transaction-scoped advisory lock (released on commit / rollback), so
    concurrent API workers never announce the same thing twice."""
    db = crud.open_tenant_session(slug)
    done: dict = {}
    try:
        companies = db.scalars(select(models.Company).where(models.Company.is_active.is_(True))).all()
        db.commit()
        for company in companies:
            # Overtime sessions start / end by the clock -- every pass,
            # whatever the hour (app/overtime.py).
            try:
                from . import overtime
                if overtime.advance_sessions(db, company.id):
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                db.rollback()
                logger.exception("Overtime sessions failed for tenant %s company %s", slug, company.id)
            # Shift-based Clock In / Clock Out / Work Entry reminders -- every pass.
            try:
                from . import shift_reminders
                shift_reminders.run_company(db, company.id)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Shift reminders failed for tenant %s company %s", slug, company.id)
            # Missing Attendance & Work Compliance: each date once, after the
            # company's EOD cutoff (app/compliance.py) -- every pass.
            try:
                from . import compliance
                if compliance.run_company(db, company.id):
                    db.commit()
                else:
                    db.commit()  # the run log rows (even with no exceptions)
            except Exception:
                db.rollback()
                logger.exception("Compliance check failed for tenant %s company %s", slug, company.id)
            # M-14: absences -- a past working day with no punch, leave,
            # holiday or regularization becomes 'absent' (the status payroll
            # LOP reads). HRMS tenants only; idempotent (app/attendance_rules.py).
            try:
                from . import attendance_rules, auth_state
                if auth_state.is_hrms_tenant(db, slug) and any(
                        attendance_rules.mark_absences(db, company.id).values()):
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                db.rollback()
                logger.exception("Absence marking failed for tenant %s company %s", slug, company.id)
            # Recruitment: sent offers past their expiry date -> expired
            # (application closed, history recorded) -- every pass.
            try:
                from . import recruitment_onboarding
                if crud.is_module_enabled(db, company.id, "recruitment") and \
                        recruitment_onboarding.expire_due_offers(db, company.id):
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                db.rollback()
                logger.exception("Offer expiry failed for tenant %s company %s", slug, company.id)
            # Exits whose last working day has been reached: employee ->
            # 'exited', login + sessions revoked (app/access_lifecycle.py) --
            # every pass, so access ends as soon as that day starts.
            # HRMS ('hcm') tenants only -- never another application's tenant.
            try:
                from . import access_lifecycle, auth_state
                if auth_state.is_hrms_tenant(db, slug) and access_lifecycle.deactivate_due_exits(db, company.id):
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                db.rollback()
                logger.exception("Exit access revocation failed for tenant %s company %s", slug, company.id)
            # N-04: current-fiscal-year leave allocations for every eligible
            # employee (rolls over at fiscal-year start) -- HRMS tenants only.
            try:
                from . import leave_policy
                if leave_policy.run_company(db, slug, company.id):
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                db.rollback()
                logger.exception("Leave allocation job failed for tenant %s company %s", slug, company.id)
            now = crud.company_now(db, company.id)
            if not force and now.hour < int(settings.reminder_send_hour):
                continue
            result = {"celebrations": False, "holidays": 0, "contract_reminders": 0}
            try:
                got = db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                 {"k": _lock_key(slug, company.id)}).scalar()
                if not got:
                    db.rollback()
                    done[str(company.id)] = {"skipped": "running in another worker"}
                    continue
                result["celebrations"] = crud.check_daily_celebrations(db, company.id)
                result["holidays"] = holiday_reminders(db, company.id, now.date())
                result["contract_reminders"] = contract_end_reminders(db, company.id, now.date())
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Reminders failed for tenant %s company %s", slug, company.id)
                result["error"] = True
            done[str(company.id)] = result
    finally:
        db.close()
    return done


def active_tenant_slugs() -> list[str]:
    db = database.SessionLocal()
    try:
        return list(db.scalars(
            select(models.Tenant.slug).where(models.Tenant.is_active.is_(True))
        ).all())
    finally:
        db.close()


def run_once(*, force: bool = False) -> dict:
    results = {}
    for slug in active_tenant_slugs():
        try:
            results[slug] = run_tenant(slug, force=force)
        except Exception:
            logger.exception("Reminder pass failed for tenant %s", slug)
            results[slug] = {"error": True}
    return results


def _loop() -> None:
    interval = max(1, int(settings.reminder_poll_minutes)) * 60
    delay = 30  # first pass shortly after startup
    while not _stop.wait(delay):
        delay = interval
        try:
            run_once()
        except Exception:
            logger.exception("Reminder pass failed")


def start() -> None:
    global _thread
    if not settings.reminders_enabled or (_thread is not None and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="hrms-reminders", daemon=True)
    _thread.start()
    logger.info("Daily reminders scheduled (every %s min, from %02d:00 company time).",
                settings.reminder_poll_minutes, settings.reminder_send_hour)


def stop() -> None:
    _stop.set()
