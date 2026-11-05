"""Contract expiry: Expire / Extend, automatic expiry, expiry alerts.

A contract employee's access follows their contract:
  * expire_contract -- Expire Contract (manual) or the scheduled pass
    (expire_due_contracts, end date passed): contract_status -> 'expired',
    login blocked straight away (access_lifecycle.sync_login_access bumps the
    token version and closes the WebSocket, so open sessions end on their
    next request), history + audit, and an alert.
  * extend_contract -- Extend Contract: a new future end date (crud.
    renew_contract), contract active again, login restored unless something
    else (an exit, an inactive status) still blocks it.
  * contract_expiry_reminders -- daily: N days before the end date (company
    setting, default 3) the Owner(s) get an in-app + email alert, HR and the
    reporting manager an in-app one.

Everything is scoped to one company inside its tenant schema (the scheduler
runs per tenant session); nothing here touches non-contract employees.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import access_lifecycle, crud, models, notification_settings

logger = logging.getLogger(__name__)

DEFAULT_NOTICE_DAYS = 3
ENTITY_TYPE = "contract_expiry"


def notice_days(db: Session, company_id: uuid.UUID) -> int:
    row = db.get(models.CompanySettings, company_id)
    value = getattr(row, "contract_expiry_notice_days", None) if row is not None else None
    return int(value) if value else DEFAULT_NOTICE_DAYS


def pay_end_date(db: Session, employee: models.Employee) -> datetime.date | None:
    """Last day a contract employee is paid for: their contract end date, or
    the (company-local) day it was expired by hand if that's earlier. None
    for non-contract employees and contracts without an end date."""
    if not crud.is_contract_employee(employee):
        return None
    end = employee.contract_end_date
    if (employee.contract_status or "") == "expired" and employee.contract_expired_at is not None \
            and employee.contract_expiry_reason == "manual":
        expired_day = employee.contract_expired_at.astimezone(
            crud.company_tzinfo(db, employee.company_id)).date()
        end = expired_day if end is None else min(end, expired_day)
    return end


def _name(employee: models.Employee) -> str:
    return f"{employee.first_name} {employee.last_name or ''}".strip()


def _fmt(d: datetime.date | None) -> str:
    return d.strftime("%d %b %Y") if d else "—"


def alert_recipients(db: Session, company_id: uuid.UUID, employee: models.Employee) -> tuple[set, set]:
    """(all recipient user ids, owner user ids). All = Owner(s), HR (People
    edit access) and the employee's reporting manager -- active logins only,
    never the contractor themselves."""
    from .deps import _OWNER_ROLE_NAME

    users = db.scalars(select(models.User).where(models.User.company_id == company_id,
                                                 models.User.status == "active")).all()
    everyone, owners = set(), set()
    for u in users:
        if u.employee_id == employee.id:
            continue
        role = crud.get_user_primary_role(db, u.id)
        if role is not None and role.name == _OWNER_ROLE_NAME:
            owners.add(u.id)
            everyone.add(u.id)
        elif crud.can_access_people_module(db, u, "edit"):
            everyone.add(u.id)
        elif employee.reporting_manager_id and u.employee_id == employee.reporting_manager_id:
            everyone.add(u.id)
    return everyone, owners


def _owner_emails(db: Session, owner_ids: set) -> set[str]:
    out = set()
    for u in db.scalars(select(models.User).where(models.User.id.in_(owner_ids))).all() if owner_ids else []:
        emp = db.get(models.Employee, u.employee_id) if u.employee_id else None
        address = (emp.work_email if emp is not None else None) or u.email
        if address:
            out.add(address.strip())
    return out


def _send_alert(
    db: Session, company_id: uuid.UUID, employee: models.Employee, *, event_key: str, title: str,
    heading: str, body: str, details: dict[str, str], marker: str,
) -> None:
    """In-app to Owner(s) + HR + reporting manager, email to the Owner(s);
    each channel honours Administration > Notifications."""
    from . import email_service

    everyone, owners = alert_recipients(db, company_id, employee)
    if notification_settings.channel_enabled(db, company_id, event_key, "inapp"):
        for user_id in everyone:
            crud.create_notification(db, company_id, user_id, title=title[:150], body=body,
                                     entity_type=ENTITY_TYPE, entity_id=employee.id)
    if notification_settings.channel_enabled(db, company_id, event_key, "email"):
        for to in _owner_emails(db, owners):
            email_service.send_request_email(
                to, subject=title[:150], heading=heading, intro_line=body, details=details,
                entity_type=ENTITY_TYPE, entity_id=employee.id, from_display_name=None,
                cta_label="Open Employee Profile", db=db, company_id=company_id,
                email_type=email_service.GENERAL,
                idempotency_key=f"{event_key}:{employee.id}:{marker}:{to.lower()}",
            )


def _resync_pay(db: Session, employee: models.Employee, from_date: datetime.date | None) -> None:
    """Draft payroll runs from [from_date] pick up the new pay window."""
    today = crud.company_today(db, employee.company_id)
    start = min(from_date, today) if from_date else today
    crud.resync_draft_payroll_runs(db, employee.company_id, employee.id, start)


def expire_contract(
    db: Session, employee: models.Employee, *, actor_user_id: uuid.UUID | None,
    reason: str | None, source: str,
) -> models.EmployeeLifecycleEvent:
    """Ends a contract now (source 'manual') or because its end date passed
    (source 'auto'): marks it expired, blocks login (ending every open
    session), records history + audit and alerts. Caller commits."""
    if not crud.is_contract_employee(employee):
        raise crud.ContractActionError(409, "Only contract employees have a contract to expire.")
    if (employee.contract_status or "") == "expired":
        raise crud.ContractActionError(409, "This contract has already expired.")
    company_id = employee.company_id
    today = crud.company_today(db, company_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    reason = (reason or "").strip() or None
    employee.contract_status = "expired"
    employee.contract_expired_at = now
    employee.contract_expired_by = actor_user_id
    employee.contract_expiry_reason = source
    if actor_user_id is not None:
        employee.updated_by = actor_user_id
    event = models.EmployeeLifecycleEvent(
        id=uuid.uuid4(), employee_id=employee.id, event_type="contract_expired", event_date=today,
        from_contract_end_date=employee.contract_end_date, to_contract_end_date=employee.contract_end_date,
        from_contract_rate=employee.contract_rate_amount, to_contract_rate=employee.contract_rate_amount,
        contract_rate_unit=employee.contract_rate_unit,
        notes=reason or ("Contract end date reached." if source == "auto" else None),
    )
    db.add(event)
    crud.create_audit_log(db, company_id, actor_user_id, "contract_expire", "employee", employee.id, changes={
        "source": source, "reason": reason,
        "contract_end_date": employee.contract_end_date.isoformat() if employee.contract_end_date else None,
    })
    if access_lifecycle.sync_login_access(db, employee) == "deactivated":
        crud.create_audit_log(db, company_id, actor_user_id, "access_revoked", "employee", employee.id,
                              changes={"reason": "contract_expired", "source": source})
    _resync_pay(db, employee, employee.contract_end_date if source == "auto" else today)

    name, code = _name(employee), employee.employee_code or "—"
    expired_on = employee.contract_end_date if source == "auto" and employee.contract_end_date else today
    title = f"Contract expired: {name} ({code})"
    body = (f"{name} ({code})'s contract expired on {_fmt(expired_on)}"
            + (f" (scheduled end date {_fmt(employee.contract_end_date)})" if source == "manual" and employee.contract_end_date else "")
            + ". Their login access is blocked until the contract is extended.")
    details = {"Employee ID": code, "Employee": name, "Contract End Date": _fmt(employee.contract_end_date),
               "Expired On": _fmt(expired_on), "How": "Expired by hand" if source == "manual" else "End date reached"}
    if reason:
        details["Reason"] = reason
    _send_alert(db, company_id, employee, event_key="contract_expired", title=title, heading="Contract Expired",
                body=body, details=details, marker=now.isoformat())
    return event


def extend_contract(
    db: Session, employee: models.Employee, actor: models.User, *, new_end_date: datetime.date,
    new_rate_amount: float | None = None, new_rate_unit: str | None = None, notes: str | None = None,
) -> models.EmployeeLifecycleEvent:
    """Extend Contract: new end date (crud.renew_contract validates it),
    contract active again, login restored unless something else still
    blocks it (an exit, an inactive status). Caller commits."""
    old_end = employee.contract_end_date
    event = crud.renew_contract(db, employee, actor, new_end_date=new_end_date,
                                new_rate_amount=new_rate_amount, new_rate_unit=new_rate_unit, notes=notes)
    if access_lifecycle.sync_login_access(db, employee) == "reactivated":
        crud.create_audit_log(db, employee.company_id, actor.id, "access_restored", "employee", employee.id,
                              changes={"reason": "contract_extended",
                                       "contract_end_date": new_end_date.isoformat()})
    _resync_pay(db, employee, old_end)
    return event


def restore_after_conversion(db: Session, employee: models.Employee, actor: models.User,
                             old_end: datetime.date | None) -> None:
    """Convert to Permanent clears the contract; restore login if that was
    the only thing blocking it. Caller commits."""
    if access_lifecycle.sync_login_access(db, employee) == "reactivated":
        crud.create_audit_log(db, employee.company_id, actor.id, "access_restored", "employee", employee.id,
                              changes={"reason": "contract_converted"})
    _resync_pay(db, employee, old_end)


def expire_due_contracts(db: Session, company_id: uuid.UUID) -> int:
    """Scheduled pass (every reminder poll): active contracts whose end date
    is before today (company timezone) expire automatically. Idempotent --
    only contracts not yet marked expired are picked. Caller commits."""
    today = crud.company_today(db, company_id)
    due = db.scalars(select(models.Employee).where(
        models.Employee.company_id == company_id,
        models.Employee.is_active.is_(True),
        models.Employee.contract_end_date.is_not(None),
        models.Employee.contract_end_date < today,
        or_(models.Employee.contract_status.is_(None), models.Employee.contract_status != "expired"),
    ).with_for_update(skip_locked=True)).all()  # several API workers: each row expires once
    count = 0
    for employee in due:
        if not crud.is_contract_employee(employee) or access_lifecycle.is_exited_status(employee.status):
            continue
        expire_contract(db, employee, actor_user_id=None, reason=None, source="auto")
        count += 1
        logger.info("Contract expired automatically for employee %s (end date %s)",
                    employee.id, employee.contract_end_date)
    return count


def contract_expiry_reminders(db: Session, company_id: uuid.UUID, today: datetime.date) -> int:
    """Daily: contracts ending within the company's notice window (default
    3 days) get one alert per employee and end date -- an extension (new end
    date) starts a new one."""
    n = notice_days(db, company_id)
    employees = db.scalars(select(models.Employee).where(
        models.Employee.company_id == company_id, models.Employee.is_active.is_(True),
        models.Employee.contract_end_date.is_not(None),
        models.Employee.contract_end_date >= today,
        models.Employee.contract_end_date <= today + datetime.timedelta(days=n),
        or_(models.Employee.contract_status.is_(None), models.Employee.contract_status != "expired"),
    )).all()
    sent = 0
    for employee in employees:
        if not crud.is_contract_employee(employee) or access_lifecycle.is_exited_status(employee.status):
            continue
        end = employee.contract_end_date
        marker = f"[notice·{end.isoformat()}]"
        if not notification_settings.mark_fired(db, company_id, "contract_end_reminder",
                                                uuid.uuid5(employee.id, marker)):
            continue
        days_left = (end - today).days
        when = "today" if days_left == 0 else ("tomorrow" if days_left == 1 else f"in {days_left} days")
        name, code = _name(employee), employee.employee_code or "—"
        title = f"Contract ends {when}: {name} ({code})"
        body = (f"{name} ({code})'s contract ends on {_fmt(end)} "
                f"({days_left} day{'s' if days_left != 1 else ''} left). Their login access will be blocked "
                f"automatically when it expires unless the contract is extended.")
        _send_alert(db, company_id, employee, event_key="contract_end_reminder", title=title,
                    heading="Contract Ending Soon", body=body, marker=marker,
                    details={"Employee ID": code, "Employee": name, "Contract End Date": _fmt(end),
                             "Days Left": str(days_left)})
        sent += 1
    return sent
