"""Import the Frappe HR exports HRMS/Attendance.xlsx and
HRMS/"Leave Ledger Entry.xlsx" (Frappe "Data Import Template" format) into
the 'impacgo-solutions' tenant.

Decisions agreed with the team (15 Oct 2026):
  * Frappe employee -> HRMS employee: EMPLOYEE_MAP. "Venkata Sikar
    Tantravahi" (EMP-369) is Srikar Tantravahi (EMP-0001).
  * EMP-111 Venkata Swetha Tantravahi and EMP-389 V A J J Murty Tantravahi
    are created (NEW_EMPLOYEES) through crud.create_employee -- the same path
    and validation as HRMS Add Employee -- with a temporary-password login.
  * Leave Allocation rows set the 2026-27 allocation (allocated_days) per
    employee + leave type; Leave Application rows become APPROVED leave
    requests (history; no approval request is created). used_days is then
    recomputed from approved requests with the app's own helper
    (leave_policy._requested_days_in_window), as routers/leave.py does.
  * Attendance rows become hcm_attendance_records (one per employee per
    day; an existing day is left untouched). Frappe in/out times are company
    time (IST). source = "import".

Idempotent. Dry run by default (rolled back); --execute commits in one
transaction after writing a JSON backup of the allocations it changes.

Usage (from backend/):
    venv\\Scripts\\python -m app.import_frappe_attendance_leave
    venv\\Scripts\\python -m app.import_frappe_attendance_leave --execute --passwords-out <file>
    # leave ledger only, from a newer export:
    venv\\Scripts\\python -m app.import_frappe_attendance_leave --skip-attendance --ledger-file "../Leave Ledger Entry (2).xlsx"
"""

import argparse
import collections
import datetime
import json
import secrets
import string
import uuid
from pathlib import Path

import openpyxl
from sqlalchemy import func, select, text

from . import crud, leave_policy, models, schemas
from .database import SessionLocal, set_session_tenant_slug, set_tenant_context

TENANT_SLUG = "impacgo-solutions"
COMPANY_ID = uuid.UUID("4fd9da4f-397f-4fbd-be50-e1245b5226cf")
SOURCE_DIR = Path(__file__).resolve().parents[2]
ATTENDANCE_FILE = SOURCE_DIR / "Attendance.xlsx"
LEDGER_FILE = SOURCE_DIR / "Leave Ledger Entry.xlsx"
BACKUP_DIR = Path(__file__).resolve().parent.parent / "db_backups"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SOURCE_TAG = "import_frappe_attendance_leave"

# Frappe employee id -> HRMS work email.
EMPLOYEE_MAP = {
    "EMP-112": "adityaram@impacgo.com", "EMP-113": "bhushan@impacgo.com", "EMP-114": "nagasai@impacgo.com",
    "EMP-115": "manisha@impacgo.com", "EMP-117": "surya@impacgo.com", "EMP-118": "mayank@impacgo.com",
    "EMP-369": "srikar@impacgo.com", "HR-EMP-379": "soumya@impacgo.com",
    "EMP-111": "swetha@impacgo.com", "EMP-389": "vajjmurty@impacgo.com",
}
NEW_EMPLOYEES = {
    "EMP-111": {"first_name": "Venkata Swetha", "last_name": "Tantravahi", "email": "swetha@impacgo.com",
                "designation": "Consultant"},
    "EMP-389": {"first_name": "V A J J Murty", "last_name": "Tantravahi", "email": "vajjmurty@impacgo.com",
                "designation": "Marketing Manager"},
}
NEW_EMPLOYEE_JOINING = datetime.date(2026, 3, 1)   # their first attendance day in the export
LEAVE_TYPE_MAP = {"earned leaves": "Earned Leave", "sick leave": "Sick Leave", "bereavement": "Bereavement Leave"}
STATUS_MAP = {"Present": "present", "Absent": "absent", "On Leave": "on_leave", "Half Day": "half_day",
              "Work From Home": "present"}


def frappe_rows(path: Path) -> list[dict]:
    """Rows of a Frappe Data Import Template: the row whose 2nd cell is
    'name' holds the fieldnames; data follows the 'Start entering data
    below this line' marker."""
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    fi = next(i for i, r in enumerate(rows) if len(r) > 1 and r[1] == "name")
    start = next(i for i, r in enumerate(rows) if i > fi and r[0] and "start entering data" in str(r[0]).lower()) + 1
    fields = rows[fi]
    return [{f: v for f, v in zip(fields, r) if f} for r in rows[start:] if any(v is not None for v in r[1:])]


def d(v) -> datetime.date:
    return v.date() if isinstance(v, datetime.datetime) else datetime.datetime.strptime(str(v)[:10], "%d-%m-%Y").date()


def ts(v) -> datetime.datetime | None:
    if not v:
        return None
    if isinstance(v, datetime.datetime):
        return v.replace(tzinfo=IST)
    return datetime.datetime.strptime(str(v)[:19], "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)


def created(v) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(v)[:26]).replace(tzinfo=IST) if v else None
    except ValueError:
        return None


def temp_password() -> str:
    return "Wt@" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(9))


def run(execute: bool, passwords_out: Path | None, ledger_file: Path = LEDGER_FILE,
        skip_attendance: bool = False) -> None:
    ledger = frappe_rows(ledger_file)
    attendance = [] if skip_attendance else frappe_rows(ATTENDANCE_FILE)
    unknown = sorted({r["employee"] for r in attendance + ledger} - set(EMPLOYEE_MAP))
    if unknown:
        raise RuntimeError(f"Frappe employees with no HRMS mapping: {unknown}")

    set_tenant_context(TENANT_SLUG)
    db = SessionLocal()
    db.execute(text(f'SET search_path TO "{TENANT_SLUG}", public'))
    set_session_tenant_slug(db, TENANT_SLUG)
    report = collections.defaultdict(list)
    passwords = {}
    try:
        branch = db.scalar(select(models.Branch).where(models.Branch.company_id == COMPANY_ID,
                                                       models.Branch.name == "Visakhapatnam Branch"))

        def employee(email):
            return db.scalar(select(models.Employee).where(models.Employee.company_id == COMPANY_ID,
                                                           func.lower(models.Employee.work_email) == email))

        # ── new employees ──
        for code, n in NEW_EMPLOYEES.items():
            if employee(n["email"]) is not None:
                continue
            if crud.find_user_by_email(db, n["email"]) is not None:
                raise RuntimeError(f"{n['email']} already has a login in another tenant")
            pw = temp_password()
            emp = crud.create_employee(db, COMPANY_ID, schemas.EmployeeCreate(
                employee_code=code, first_name=n["first_name"], last_name=n["last_name"], work_email=n["email"],
                role_name="Professional / IC Employee", password=pw, date_of_joining=NEW_EMPLOYEE_JOINING,
                employment_type="full_time", status="active", branch_name=branch.name, department_name="IT",
                designation_name=n["designation"], create_designation=True))
            crud.create_audit_log(db, COMPANY_ID, None, "create", "employee", emp.id, changes={"source": SOURCE_TAG})
            passwords[n["email"]] = pw
            report["employees created"].append(f"{n['first_name']} {n['last_name']} ({code}) <{n['email']}>, "
                                               f"{n['designation']}, IT, joined {NEW_EMPLOYEE_JOINING}")
        db.flush()
        emps = {code: employee(email) for code, email in EMPLOYEE_MAP.items()}

        # ── leave types / fiscal year ──
        types = {t.name: t for t in db.scalars(select(models.LeaveType).where(models.LeaveType.company_id == COMPANY_ID))}
        fy = db.scalar(select(models.FiscalYear).where(models.FiscalYear.company_id == COMPANY_ID,
                                                       models.FiscalYear.start_date == datetime.date(2026, 4, 1)))

        def leave_type(raw):
            return types[LEAVE_TYPE_MAP[" ".join(str(raw).split()).lower()]]

        # ── allocations ──
        wanted = collections.defaultdict(float)
        for r in ledger:
            if r["transaction_type"] == "Leave Allocation":
                if (d(r["from_date"]), d(r["to_date"])) != (fy.start_date, fy.end_date):
                    raise RuntimeError(f"Allocation outside {fy.name}: {r}")
                wanted[(r["employee"], leave_type(r["leave_type"]).id)] += float(r["leaves"])
        backup_allocs = []
        for (code, type_id), days in sorted(wanted.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
            emp = emps[code]
            alloc = db.scalar(select(models.LeaveAllocation).where(
                models.LeaveAllocation.employee_id == emp.id, models.LeaveAllocation.leave_type_id == type_id,
                models.LeaveAllocation.fiscal_year_id == fy.id))
            name = next(t.name for t in types.values() if t.id == type_id)
            if alloc is None:
                alloc = crud.create_leave_allocation(db, employee_id=emp.id, leave_type_id=type_id,
                                                     fiscal_year_id=fy.id, allocated_days=days)
                report["allocations created"].append(f"{emp.first_name} {name}: {days:g}")
            elif float(alloc.allocated_days) != days:
                backup_allocs.append({"id": str(alloc.id), "allocated_days": float(alloc.allocated_days),
                                      "used_days": float(alloc.used_days or 0)})
                report["allocations changed"].append(f"{emp.first_name} {name}: {float(alloc.allocated_days):g} -> {days:g}")
                alloc.allocated_days = days

        # ── leave applications -> approved leave requests ──
        approver = emps["HR-EMP-379"]   # the ledger entries were made by soumya@impacgo.com
        touched = set()
        for r in sorted((r for r in ledger if r["transaction_type"] == "Leave Application"),
                        key=lambda r: (r["employee"], d(r["from_date"]))):
            emp, lt = emps[r["employee"]], leave_type(r["leave_type"])
            days, f, t = -float(r["leaves"]), d(r["from_date"]), d(r["to_date"])
            label = f"{emp.first_name} {lt.name} {f}..{t} ({days:g} d, {r['transaction_name']})"
            if db.scalar(select(models.LeaveRequest.id).where(
                    models.LeaveRequest.employee_id == emp.id, models.LeaveRequest.leave_type_id == lt.id,
                    models.LeaveRequest.from_date == f, models.LeaveRequest.to_date == t).limit(1)):
                report["leave requests already present"].append(label)
                continue
            db.add(models.LeaveRequest(
                id=uuid.uuid4(), company_id=COMPANY_ID, employee_id=emp.id, leave_type_id=lt.id, from_date=f,
                to_date=t, days=days, is_half_day=days == 0.5, reason=f"Imported from Frappe HR ({r['transaction_name']})",
                status="approved", approver_id=approver.id, approved_at=created(r["creation"])))
            touched.add((emp.id, lt.id))
            report["leave requests created"].append(label)
        db.flush()
        for emp_id, type_id in touched | {(emps[c].id, t) for c, t in wanted}:
            alloc = db.scalar(select(models.LeaveAllocation).where(
                models.LeaveAllocation.employee_id == emp_id, models.LeaveAllocation.leave_type_id == type_id,
                models.LeaveAllocation.fiscal_year_id == fy.id))
            if alloc is not None:
                alloc.used_days = leave_policy._requested_days_in_window(
                    db, emp_id, type_id, ("approved",), fy.start_date, fy.end_date)

        # ── attendance ──
        have = {(e, dd) for e, dd in db.execute(select(models.AttendanceRecord.employee_id,
                                                       models.AttendanceRecord.attendance_date)
                                                .where(models.AttendanceRecord.company_id == COMPANY_ID))}
        added = collections.Counter()
        for r in attendance:
            emp, day = emps[r["employee"]], d(r["attendance_date"])
            if (emp.id, day) in have:
                report["attendance days already present"].append(f"{emp.first_name} {day}")
                continue
            cin, cout = ts(r["in_time"]), ts(r["out_time"])
            hours = float(r["working_hours"] or 0)
            if hours <= 0 and cin and cout and cout > cin:
                hours = round((cout - cin).total_seconds() / 3600, 2)
            db.add(models.AttendanceRecord(
                id=uuid.uuid4(), company_id=COMPANY_ID, employee_id=emp.id, attendance_date=day,
                check_in=cin, check_out=cout, work_hours=hours or None, overtime_hours=0,
                status=STATUS_MAP[r["status"]], source="import"))
            have.add((emp.id, day))
            added[f"{emp.first_name} {emp.last_name or ''}".strip()] += 1
        db.flush()
        report["attendance days created"] = [f"{k}: {v}" for k, v in sorted(added.items())] + [f"TOTAL {sum(added.values())} of {len(attendance)}"]
        crud.create_audit_log(db, COMPANY_ID, None, "import", "attendance_leave", None,
                              changes={"source": SOURCE_TAG, "attendance_rows": sum(added.values()),
                                       "leave_requests": len(report["leave requests created"])})

        if execute:
            BACKUP_DIR.mkdir(exist_ok=True)
            out = BACKUP_DIR / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_frappe_import_allocations_backup.json"
            out.write_text(json.dumps({"allocations_before": backup_allocs}, indent=2), encoding="utf-8")
            db.commit()
            report["backup"] = [str(out)]
            if passwords and passwords_out:
                passwords_out.write_text("".join(f"{k}\t{v}\n" for k, v in passwords.items()), encoding="utf-8")
                report["temp passwords"] = [f"written to {passwords_out}"]
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("=" * 78)
    print(("EXECUTED (committed)" if execute else "DRY RUN (rolled back)") + f" -- tenant '{TENANT_SLUG}'")
    print("=" * 78)
    for section, items in report.items():
        print(f"\n[{section}] {len(items)}")
        for item in items:
            print(f"  - {item}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--execute", action="store_true")
    p.add_argument("--passwords-out", type=Path)
    p.add_argument("--ledger-file", type=Path, default=LEDGER_FILE,
                   help="Frappe Leave Ledger Entry export (default: HRMS/'Leave Ledger Entry.xlsx')")
    p.add_argument("--skip-attendance", action="store_true", help="import the leave ledger only")
    a = p.parse_args()
    run(a.execute, a.passwords_out, a.ledger_file, a.skip_attendance)
