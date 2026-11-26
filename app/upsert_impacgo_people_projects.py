"""Insert/update the Impacgo employee + project master data supplied in
HRMS_Employee_Project_Data_Insert_Update_Prompt.md, in the real
'impacgo-solutions' tenant (company 4fd9da4f-..., "Impacgo Solutions Pvt
Ltd") -- see seed_impacgo_solutions_projects.py for why it's this tenant
and not 'impacgo' / 'acme'.

Decisions agreed with the team (15 Oct 2026):
  * Existing employees keep their HRMS values; only blank phone numbers are
    filled. Conflicts (designation, joining date) are reported, not applied.
  * Employees the source puts in "IT" move to a new "IT" department.
    "General" is WorkTrack's label for "no department" -- an employee
    created with it goes to a "General" department, exactly as WorkTrack's
    Add Employee does (routers/worktrack.py::_department).
  * Missing employees are created with today's (company) date as a
    PLACEHOLDER joining date -- WorkTrack's Add Employee rule -- and a
    temporary-password login, through crud.create_employee (same path and
    validation as HRMS Add Employee). Their designation is the source
    "Role", as in WorkTrack.
  * Placeholder phone numbers (123456789, 12345678910, ...) are never
    stored. No employee-project allocation is created or changed.
  * Projects are matched on a normalised name (case, spaces, punctuation,
    Unicode dashes) or code; existing ones are never modified.

Rows deliberately NOT applied (reported as needing clarification):
  * 2nd "srikar" (Director, +91 1233456789) -- same login email as the
    existing CEO Srikar Tantravahi; one email can only be one person.
  * "sai naga" <palchurinagasains@gmail.com> -- that login belongs to the
    'etorgroups' tenant; adding it here would cross tenants.

Idempotent: re-running only does what is still missing. Dry run by
default (everything is rolled back); --execute commits in one transaction
after writing a JSON backup of every row it is about to change.

Usage (from backend/):
    venv\\Scripts\\python -m app.upsert_impacgo_people_projects            # dry run
    venv\\Scripts\\python -m app.upsert_impacgo_people_projects --execute
"""

import argparse
import datetime
import json
import re
import secrets
import string
import unicodedata
import uuid
from pathlib import Path

from sqlalchemy import func, select, text

from . import crud, models, schemas
from .database import SessionLocal, set_session_tenant_slug, set_tenant_context

TENANT_SLUG = "impacgo-solutions"
COMPANY_ID = uuid.UUID("4fd9da4f-397f-4fbd-be50-e1245b5226cf")
BRANCH_NAME = "Visakhapatnam Branch"   # every existing Impacgo employee's branch
NEW_EMPLOYEE_ROLE = "Professional / IC Employee"   # WorkTrack's default login role
BACKUP_DIR = Path(__file__).resolve().parent.parent / "db_backups"

# Source table, as supplied. None = "Not provided".
EMPLOYEES = [
    {"name": "jai", "department": "General", "joined": None, "location": None, "employment_type": None,
     "designation": None, "email": "jai@impacgo.com", "phone": None, "role": "D365FO Technical Consultant"},
    {"name": "chandra", "department": "IT", "joined": None, "location": None, "employment_type": None,
     "designation": None, "email": "chandra@impacgo.com", "phone": None, "role": "D365FO Technical Consultant"},
    {"name": "Surya", "department": "IT", "joined": "Dec 2025", "location": "Visakhapatnam", "employment_type": "Full Time",
     "designation": "Junior Developer", "email": "surya@impacgo.com", "phone": "7032497574", "role": "Frontend Developer"},
    {"name": "Naveen", "department": "IT", "joined": None, "location": None, "employment_type": None,
     "designation": None, "email": "naveen@impacgo.com", "phone": "12345678910", "role": "SCM Consultant"},
    {"name": "Mayank", "department": "IT", "joined": None, "location": None, "employment_type": None,
     "designation": None, "email": "mayank@impacgo.com", "phone": "+91 7838566462", "role": "D365 Solution Architect"},
    {"name": "sai naga", "department": "Finance", "joined": "2026-07-03", "location": None, "employment_type": None,
     "designation": None, "email": "palchurinagasains@gmail.com", "phone": None, "role": "Employee"},
    {"name": "Srikar", "department": "IT", "joined": None, "location": None, "employment_type": None,
     "designation": None, "email": "srikar@impacgo.com", "phone": "9866323418", "role": "System Administrator"},
    {"name": "Aditya", "department": "IT", "joined": "30-06-2025", "location": "Vizag", "employment_type": "Full Time",
     "designation": "JR developer", "email": "adityaram@impacgo.com", "phone": "123456789", "role": "Backend Developer"},
    {"name": "Mounika", "department": "IT", "joined": None, "location": None, "employment_type": None,
     "designation": None, "email": "mounika@impacgo.com", "phone": "12345678910", "role": "Finance Consultant"},
    {"name": "Manisha", "department": "IT", "joined": "14 July 2025", "location": "Visakhapatnam, Andhra Pradesh",
     "employment_type": "Full Time", "designation": "Technical Consultant", "email": "manisha@impacgo.com",
     "phone": "+91 9014773844", "role": "Technical Consultant"},
    {"name": "Bhushan", "department": "IT", "joined": "July 1, 2025", "location": "Visakhapatnam, India",
     "employment_type": "Full Time", "designation": "Junior Developer", "email": "bhushan@impacgo.com",
     "phone": "9581616566", "role": "Frontend Developer"},
    {"name": "srikar", "department": "IT", "joined": None, "location": None, "employment_type": None,
     "designation": None, "email": "srikar@impacgo.com", "phone": "+91 1233456789", "role": "Director"},
    {"name": "Naga Sai", "department": "IT", "joined": "June 31, 2025", "location": "Visakhapatnam",
     "employment_type": "Full Time", "designation": "Junior Developer", "email": "nagasai@impacgo.com",
     "phone": "8639396738", "role": "Backend Developer"},
]

# (source row index, reason) -- never applied, always reported.
CLARIFY = {
    11: "Same login email as the existing CEO Srikar Tantravahi (EMP-0001) but a different role/phone; "
        "one email can only belong to one person. Not applied.",
    5: "palchurinagasains@gmail.com is a login in the 'etorgroups' tenant (Etor Groups); creating it in "
       "impacgo-solutions would cross tenants. Not applied.",
}

PROJECTS = [
    "D365 F&O Rollout — Ironing Boy", "Archiquant Construction IQ (ACIQ)", "Resonac Materials Malaysia Sdn Bhd",
    "Imax Precision India Pvt Ltd (IMAX)", "Meetings Update", "Imax D365 - Go-Live support (Urgent)",
    "StockLyte – Inventory Management", "D365 F&O Implementation — Rosti", "Impacgo Chain",
    "RSGS - REAP System Integration", "Construction Planner", "D365 Development & AMS — Perfect Model Solutions",
    "D365 F&O Implementation — Hitachi", "Work Task Application", "Impacgo Retail", "Etor Mart",
    "Palakollu Website", "CALVIQ – Dairy Farm Management", "Impacgo Project",
    "HealthVault – Family Health Records", "Impacgo HRMS", "Impacgo Website", "D365 Platform Support — ETOR",
    "Impacgo ERP", "Partner App", "Impacgo Make", "Etor Website", "OurLocker", "KSN function hall",
    "Impacgo Finance", "Frappe HR", "FarmYieldIQ", "D365 Development — Daxonet", "Impacgo Plan", "Work Track",
    "D365 F&O Implementation — Resonac", "Impacgo Solutions Internal Project",
]


def norm_name(value: str) -> str:
    """Case/space/punctuation/Unicode-dash-insensitive key."""
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"[‐-―−\-]", " ", value)
    value = re.sub(r"[^\w&]+", " ", value)
    return " ".join(value.split())


def usable_phone(phone: str | None) -> str | None:
    """None for blanks and obvious placeholders (sequential digit runs)."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    local = digits[2:] if digits.startswith("91") and len(digits) == 12 else digits
    if len(local) != 10 or local in "1234567890123456789" or local.startswith("12334") or len(set(local)) <= 2:
        return None
    return phone.strip()


def parse_joined(value: str) -> datetime.date | None:
    """Strict: a full, real calendar date or None (never guesses a day)."""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Wt@" + "".join(secrets.choice(alphabet) for _ in range(9))


def employee_snapshot(e: models.Employee) -> dict:
    return {c.name: (str(v) if v is not None else None)
            for c in models.Employee.__table__.columns for v in [getattr(e, c.key)]}


def run(execute: bool) -> None:
    set_tenant_context(TENANT_SLUG)
    db = SessionLocal()
    db.execute(text(f'SET search_path TO "{TENANT_SLUG}", public'))
    set_session_tenant_slug(db, TENANT_SLUG)
    report = {"employees": {"inserted": [], "updated": [], "unchanged": [], "clarify": [], "conflicts": []},
              "projects": {"inserted": [], "updated": [], "matched": [], "clarify": []},
              "departments_created": [], "temp_passwords": {}}
    backup = {"taken_at": datetime.datetime.now().isoformat(), "tenant": TENANT_SLUG, "employees": []}

    try:
        company = db.get(models.Company, COMPANY_ID)
        if company is None:
            raise RuntimeError(f"Company {COMPANY_ID} not found in tenant '{TENANT_SLUG}'")
        branch = db.scalar(select(models.Branch).where(models.Branch.company_id == COMPANY_ID,
                                                       func.lower(models.Branch.name) == BRANCH_NAME.lower()))
        if branch is None:
            raise RuntimeError(f"Branch '{BRANCH_NAME}' not found -- refusing to guess a branch")
        today = crud.company_today(db, COMPANY_ID)

        # Every existing department has the same head + HR representative;
        # a new one gets them too, or moved employees would lose their HR
        # representative in the approval chain (org_hierarchy.py).
        def common(column) -> uuid.UUID | None:
            """The one value every department that sets `column` agrees on."""
            values = set(db.scalars(select(column).where(models.Department.company_id == COMPANY_ID,
                                                         column.is_not(None),
                                                         models.Department.name.not_in(("IT", "General")))))
            return values.pop() if len(values) == 1 else None

        common_head = common(models.Department.head_employee_id)
        common_hr = common(models.Department.hr_representative_id)

        def department(name: str) -> models.Department:
            d = crud.get_department_by_name(db, COMPANY_ID, name)
            if d is None:
                d = crud.create_department(db, COMPANY_ID, schemas.DepartmentCreate(
                    name=name, branch_id=branch.id, head_employee_id=common_head, hr_representative_id=common_hr))
                crud.create_audit_log(db, COMPANY_ID, None, "create", "department", d.id,
                                      changes={"source": "upsert_impacgo_people_projects"})
                report["departments_created"].append(f"{d.name} ({d.code})")
            elif d.name in ("IT", "General") and (d.head_employee_id is None or d.hr_representative_id is None):
                # Repair departments created by an earlier run of this script.
                filled = {}
                if d.head_employee_id is None and common_head:
                    d.head_employee_id, filled["head_employee_id"] = common_head, str(common_head)
                if d.hr_representative_id is None and common_hr:
                    crud.set_department_hr_reps(db, d, [common_hr])
                    filled["hr_representative_id"] = str(common_hr)
                if filled:
                    crud.create_audit_log(db, COMPANY_ID, None, "update", "department", d.id,
                                          changes={"source": "upsert_impacgo_people_projects", **filled})
                    report["departments_created"].append(f"{d.name} ({d.code}): leadership filled {sorted(filled)}")
            return d

        for existing_name in ("IT", "General"):
            if crud.get_department_by_name(db, COMPANY_ID, existing_name) is not None:
                department(existing_name)

        desig_names = {d.id: d.name for d in db.scalars(select(models.Designation)
                                                        .where(models.Designation.company_id == COMPANY_ID))}

        # ── employees ──
        for idx, row in enumerate(EMPLOYEES):
            label = f"{row['name']} <{row['email']}>"
            if idx in CLARIFY:
                report["employees"]["clarify"].append(f"{label} [{row['role']}]: {CLARIFY[idx]}")
                continue
            email = row["email"].strip().lower()
            emp = db.scalar(select(models.Employee).where(models.Employee.company_id == COMPANY_ID,
                                                          func.lower(models.Employee.work_email) == email))
            if emp is None:
                user = crud.find_user_by_email(db, email)
                if user is not None:
                    report["employees"]["clarify"].append(f"{label}: a login with this email exists outside this "
                                                          "tenant's employee list. Not applied.")
                    continue
                first = row["name"].strip()
                first = first[:1].upper() + first[1:]
                password = temp_password()
                payload = schemas.EmployeeCreate(
                    first_name=first, last_name=None, work_email=email, role_name=NEW_EMPLOYEE_ROLE,
                    password=password, date_of_joining=today, employment_type="full_time", status="active",
                    branch_name=branch.name, department_name=department(row["department"]).name,
                    designation_name=row["role"], personal_phone=usable_phone(row["phone"]),
                    create_designation=True,
                )
                emp = crud.create_employee(db, COMPANY_ID, payload)
                crud.create_audit_log(db, COMPANY_ID, None, "create", "employee", emp.id,
                                      changes={"source": "upsert_impacgo_people_projects",
                                               "date_of_joining": "placeholder (not supplied)"})
                report["temp_passwords"][email] = password
                notes = [f"code {emp.employee_code}", f"dept {row['department']}", f"designation {row['role']}",
                         f"joined {today} (PLACEHOLDER)", "branch Visakhapatnam (location not supplied)",
                         "employment type full_time (not supplied)"]
                if row["phone"] and not usable_phone(row["phone"]):
                    notes.append(f"placeholder phone '{row['phone']}' not stored")
                report["employees"]["inserted"].append(f"{label}: " + "; ".join(notes))
                continue

            # Existing employee: fill blanks + agreed department move; report conflicts.
            before = employee_snapshot(emp)
            changes = {}
            phone = usable_phone(row["phone"])
            if row["phone"] and not phone:
                report["employees"]["conflicts"].append(f"{label}: placeholder phone '{row['phone']}' not stored")
            elif phone and not emp.personal_phone:
                emp.personal_phone = phone
                changes["personal_phone"] = [None, phone]
            elif phone and re.sub(r"\D", "", phone)[-10:] != re.sub(r"\D", "", emp.personal_phone or "")[-10:]:
                report["employees"]["conflicts"].append(
                    f"{label}: phone HRMS '{emp.personal_phone}' vs source '{row['phone']}' (kept HRMS)")
            if row["department"] == "IT":
                it = department("IT")
                if emp.department_id != it.id:
                    old = db.get(models.Department, emp.department_id) if emp.department_id else None
                    emp.department_id = it.id
                    emp.sub_department_id = None   # sub-departments belong to the old department
                    changes["department"] = [old.name if old else None, "IT"]
            current = desig_names.get(emp.designation_id) or ""
            source_titles = {t for t in (row["designation"], row["role"]) if t}
            if row["role"] == "System Administrator":   # WorkTrack's admin-login label, not a job title
                source_titles.discard("System Administrator")
            if source_titles and current.casefold() not in {t.casefold() for t in source_titles}:
                report["employees"]["conflicts"].append(
                    f"{label}: designation HRMS '{current}' vs source "
                    f"{' / '.join(repr(t) for t in sorted(source_titles))} (kept HRMS)")
            if row["joined"]:
                parsed = parse_joined(row["joined"])
                if parsed is None:
                    report["employees"]["conflicts"].append(
                        f"{label}: source joining date '{row['joined']}' is invalid/incomplete; "
                        f"kept HRMS {emp.date_of_joining}")
                elif parsed != emp.date_of_joining:
                    report["employees"]["conflicts"].append(
                        f"{label}: joining date HRMS {emp.date_of_joining} vs source {parsed} (kept HRMS)")
            if changes:
                backup["employees"].append(before)
                crud.create_audit_log(db, COMPANY_ID, None, "update", "employee", emp.id,
                                      changes={"source": "upsert_impacgo_people_projects", **changes})
                report["employees"]["updated"].append(
                    f"{label} ({emp.employee_code}): " + "; ".join(f"{k} {a!r} -> {b!r}" for k, (a, b) in changes.items()))
            else:
                report["employees"]["unchanged"].append(f"{label} ({emp.employee_code})")
        db.flush()

        # ── projects (match only; nothing is invented for a missing one) ──
        existing = list(db.scalars(select(models.Project).where(models.Project.company_id == COMPANY_ID)))
        by_name = {}
        for p in existing:
            by_name.setdefault(norm_name(p.name), []).append(p)
        by_code = {norm_name(p.code): p for p in existing if p.code}
        for name in PROJECTS:
            hits = by_name.get(norm_name(name)) or ([by_code[norm_name(name)]] if norm_name(name) in by_code else [])
            if len(hits) == 1:
                p = hits[0]
                exact = "" if p.name == name else f" (stored as {p.name!r})"
                report["projects"]["matched"].append(f"{name} -> {p.code} [{p.status}]{exact}")
            elif len(hits) > 1:
                report["projects"]["clarify"].append(f"{name}: {len(hits)} existing projects match -- "
                                                     + ", ".join(p.code for p in hits))
            else:
                report["projects"]["clarify"].append(
                    f"{name}: not in the project master; needs status/start date/department before it can be added")

        if execute:
            BACKUP_DIR.mkdir(exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = BACKUP_DIR / f"{stamp}_impacgo_people_projects_backup.json"
            backup_file.write_text(json.dumps(backup, indent=2), encoding="utf-8")
            db.commit()
            report["backup_file"] = str(backup_file)
        else:
            db.rollback()
            report["temp_passwords"] = {k: "(generated on --execute)" for k in report["temp_passwords"]}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print("=" * 78)
    print(("EXECUTED (committed)" if execute else "DRY RUN (rolled back)") + f" -- tenant '{TENANT_SLUG}'")
    print("=" * 78)
    for section in ("employees", "projects"):
        for key, items in report[section].items():
            print(f"\n[{section}] {key}: {len(items)}")
            for item in items:
                print(f"  - {item}")
    print(f"\n[departments created]: {report['departments_created'] or 'none'}")
    if report.get("backup_file"):
        print(f"[backup] {report['backup_file']}")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--execute", action="store_true", help="commit (default: dry run, rolled back)")
    parser.add_argument("--passwords-out", help="file to write new logins' temporary passwords to (--execute)")
    args = parser.parse_args()
    result = run(args.execute)
    if args.execute and result["temp_passwords"]:
        out = Path(args.passwords_out) if args.passwords_out else None
        if out:
            out.write_text("\n".join(f"{k}\t{v}" for k, v in result["temp_passwords"].items()) + "\n", encoding="utf-8")
            print(f"[temp passwords] written to {out}")
        else:
            print("[temp passwords]")
            for k, v in result["temp_passwords"].items():
                print(f"  {k}\t{v}")
