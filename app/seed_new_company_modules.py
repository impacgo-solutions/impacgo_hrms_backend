"""Per-module seed phases for the second demo company (Vertexa Technologies).
Split out of seed_new_company.py purely for file-size sanity -- imported and
called from there. Every function takes the already-created `company` +
`employees` (dict[employee_code, models.Employee]) and does its own
idempotency guard (a coarse "does this company already have rows here?"
check) so the whole script is safe to re-run.
"""

import datetime
import uuid
from itertools import cycle

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import crud, models
from .benefits_assets_documents_seed_data import ASSET_INVENTORY, DOC_TEMPLATES, POLICY_DOCS
from .performance_learning_seed_data import COURSES, REVIEW_CYCLES
from .projects_seed_data import PROJECTS
from .recruitment_seed_data import JOB_OPENINGS

TODAY = datetime.date(2026, 7, 23)


def _already_has_rows(db, model, **filters) -> bool:
    query = select(func.count()).select_from(model)
    for key, value in filters.items():
        query = query.where(getattr(model, key) == value)
    return (db.scalar(query) or 0) > 0


def _emp_list(employees: dict[str, models.Employee]) -> list[models.Employee]:
    return list(employees.values())


# ---------------------------------------------------------------------------
# Leave allocations + requests
# ---------------------------------------------------------------------------


def ensure_leave_allocations_and_requests(db, company, employees, leave_types) -> None:
    fiscal_year_id = uuid.uuid5(uuid.NAMESPACE_OID, f"fy-{TODAY.year}")
    allocation_ok = True
    if not _already_has_rows(db, models.LeaveAllocation, employee_id=_emp_list(employees)[0].id):
        allocated_by_code = {"CL": 18, "SL": 12, "EL": 24, "CO": 4}
        for employee in _emp_list(employees):
            for code, days in allocated_by_code.items():
                lt = leave_types.get(code)
                if lt is None:
                    continue
                try:
                    crud.create_leave_allocation(db, employee.id, lt.id, fiscal_year_id, days)
                    db.commit()
                except IntegrityError as exc:
                    db.rollback()
                    if allocation_ok:
                        print(f"Skipped leave allocations (fin.fiscal_years row missing?): {exc}")
                    allocation_ok = False
                    break
            if not allocation_ok:
                break
    if allocation_ok:
        print("Seeded leave allocations")

    if _already_has_rows(db, models.LeaveRequest, company_id=company.id):
        return

    emps = _emp_list(employees)
    leave_name_by_code = {code: lt.name for code, lt in leave_types.items()}
    count = 0
    for i, employee in enumerate(emps):
        if i % 3 != 0:  # roughly a third of the roster has a leave request on file
            continue
        code = ["CL", "SL", "EL"][i % 3]
        lt_name = leave_name_by_code.get(code, "Casual Leave")
        days = 1 if code == "SL" else 2
        offset = (i % 45) - 20  # spread across ~2 months back / ~3 weeks forward
        from_date = TODAY + datetime.timedelta(days=offset)
        to_date = from_date + datetime.timedelta(days=days - 1)
        leave_request = crud.create_leave_request(
            db, company.id, employee.id, lt_name, from_date, to_date, float(days),
            "Personal work" if code == "CL" else ("Not feeling well" if code == "SL" else "Planned time off"),
            False,
        )
        bucket = i % 5
        if bucket < 3:  # approved
            leave_request.status = "approved"
            leave_request.approver_id = employee.reporting_manager_id
            leave_request.approved_at = datetime.datetime.now(datetime.timezone.utc)
            if allocation_ok:
                try:
                    crud.adjust_leave_allocation_used(db, employee.id, leave_request.leave_type_id, float(days))
                except Exception:
                    pass
        elif bucket == 3:
            leave_request.status = "rejected"
            leave_request.approver_id = employee.reporting_manager_id
        # else: leave as "pending"
        db.commit()
        count += 1
    print(f"Seeded {count} leave requests")


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


def ensure_attendance(db, company, employees) -> None:
    if _already_has_rows(db, models.AttendanceRecord, company_id=company.id):
        return

    emps = _emp_list(employees)
    days_back = 30
    count = 0
    for day_offset in range(days_back, 0, -1):
        the_date = TODAY - datetime.timedelta(days=day_offset)
        if the_date.weekday() >= 5:  # skip weekends
            continue
        for i, employee in enumerate(emps):
            roll = (i + day_offset) % 20
            if roll == 0:
                status = "absent"
            elif roll == 1:
                status = "on_leave"
            elif roll == 2:
                status = "half_day"
            else:
                status = "present"
            check_in = check_out = work_hours = None
            if status in ("present", "half_day"):
                check_in = datetime.datetime.combine(
                    the_date, datetime.time(9, 30), tzinfo=datetime.timezone.utc
                )
                hours = 4.0 if status == "half_day" else 8.5
                check_out = check_in + datetime.timedelta(hours=hours)
                work_hours = hours
            # Note: hcm.attendance_records.overtime_hours is a real DB column
            # but models.AttendanceRecord doesn't map it (crud.py reads it via
            # getattr(..., "overtime_hours", None) defensively) -- leaving it
            # unset here lets the column's DB-side DEFAULT 0 apply.
            record = models.AttendanceRecord(
                id=uuid.uuid4(), company_id=company.id, employee_id=employee.id,
                attendance_date=the_date, check_in=check_in, check_out=check_out,
                work_hours=work_hours, status=status, source="web",
            )
            db.add(record)
            count += 1
        db.commit()
    print(f"Seeded {count} attendance records")

    # A handful of regularization requests (pending + approved)
    for i, employee in enumerate(emps[:10]):
        reg_date = TODAY - datetime.timedelta(days=3 + i)
        reg = crud.create_regularization(
            db, employee.id, reg_date, "Forgot to clock in on time",
            datetime.datetime.combine(reg_date, datetime.time(9, 40), tzinfo=datetime.timezone.utc), None,
        )
        if i % 2 == 0:
            reg.status = "approved"
            reg.approver_id = employee.reporting_manager_id
        db.commit()
    print("Seeded attendance regularizations")


# ---------------------------------------------------------------------------
# Payroll
# ---------------------------------------------------------------------------


def _salary_breakdown(annual_ctc: int) -> tuple[dict[str, float], float, float, float]:
    monthly = annual_ctc / 12
    basic = round(monthly * 0.40)
    hra = round(monthly * 0.20)
    conv = 1600
    meal = 1500
    special = max(0, round(monthly - basic - hra - conv - meal))
    pf = min(round(basic * 0.12), 1800)
    pt = 200
    if annual_ctc <= 700_000:
        tds_annual = 0
    elif annual_ctc <= 1_200_000:
        tds_annual = (annual_ctc - 700_000) * 0.05
    elif annual_ctc <= 2_000_000:
        tds_annual = 25_000 + (annual_ctc - 1_200_000) * 0.10
    else:
        tds_annual = 105_000 + (annual_ctc - 2_000_000) * 0.20
    tds = round(tds_annual / 12)
    lines = {"BASIC": basic, "HRA": hra, "CONV": conv, "MEAL": meal, "SPECIAL": special,
             "VARIABLE": 0, "PF": pf, "PT": pt, "TDS": tds}
    gross = basic + hra + conv + meal + special
    deductions = pf + pt + tds
    return lines, gross, deductions, gross - deductions


def ensure_payroll(db, company, employees, salary_components) -> None:
    if _already_has_rows(db, models.PayrollRun, company_id=company.id):
        return

    emps = _emp_list(employees)
    for month in (4, 5, 6):
        run = models.PayrollRun(
            id=uuid.uuid4(), company_id=company.id, run_no=f"PR-2026-{month:02d}",
            period_month=month, period_year=2026,
            from_date=datetime.date(2026, month, 1),
            to_date=datetime.date(2026, month, 28 if month == 2 else 30),
            status="paid",
        )
        db.add(run)
        db.flush()
        for i, employee in enumerate(emps):
            lines, gross, deductions, net = _salary_breakdown(employee.annual_ctc or 600_000)
            lop_days = 1.0 if (i % 20 == 0) else 0.0
            slip = models.SalarySlip(
                id=uuid.uuid4(), payroll_run_id=run.id, employee_id=employee.id,
                working_days=22.0, lop_days=lop_days, gross_pay=gross,
                total_deductions=deductions, net_pay=net, status="processed",
            )
            db.add(slip)
            db.flush()
            for code, amount in lines.items():
                component = salary_components.get(code)
                if component is None or amount == 0:
                    continue
                db.add(models.SalarySlipLine(
                    id=uuid.uuid4(), slip_id=slip.id, component_id=component.id, amount=amount
                ))
        db.commit()
        print(f"Seeded payroll run {run.run_no} ({len(emps)} slips)")

    categories = ["Travel", "Internet Reimbursement", "Client Entertainment", "Office Supplies", "Training"]
    for i, employee in enumerate(emps[:20]):
        claim = crud.create_reimbursement(
            db, company.id, employee.id, categories[i % len(categories)],
            float(1500 + i * 350), TODAY - datetime.timedelta(days=10 + i),
        )
        claim.status = ["pending", "approved", "rejected"][i % 3]
        db.commit()
    print("Seeded reimbursements")

    for i, employee in enumerate(emps[:12]):
        principal = 100_000 + i * 25_000
        crud.create_loan(
            db, employee.id, "Salary Advance" if i % 2 == 0 else "Vehicle Loan",
            float(principal), float(round(principal / 10)), float(principal - (principal // 10) * (i % 5)),
        )
    db.commit()
    print("Seeded loans")

    for i, employee in enumerate(emps):
        try:
            crud.create_tax_declaration(
                db, employee.id, "2026-2027", "new" if i % 2 == 0 else "old",
                float(80_000 if i % 2 else 0), float(50_000 if i % 3 == 0 else 0),
            )
        except ValueError:
            db.rollback()
            continue
        db.commit()
    print("Seeded tax declarations")


# ---------------------------------------------------------------------------
# Recruitment
# ---------------------------------------------------------------------------

_EXTRA_JOB_OPENINGS = [
    {"title": "Enterprise Account Executive", "department": "Sales", "employment_type": "full_time",
     "vacancies": 3, "status": "open", "posted_date": "2026-06-10"},
    {"title": "Data Scientist", "department": "Data & Analytics", "employment_type": "full_time",
     "vacancies": 2, "status": "open", "posted_date": "2026-06-18"},
    {"title": "Customer Success Manager", "department": "Customer Success", "employment_type": "full_time",
     "vacancies": 2, "status": "closed", "posted_date": "2026-04-01"},
]
# NOTE: candidate names below are deliberately NOT reused from
# recruitment_seed_data.CANDIDATES/INTERVIEWS/OFFERS (the ones app/seed.py
# seeds for the default company). crud.find_application_by_candidate_and_
# opening() looks up a JobApplication by (candidate name, job title) with NO
# company_id filter at all -- db.scalar() silently returns whichever
# matching row comes first across ALL companies rather than raising on
# ambiguity. Reusing an identical (name, job_title) pair here would attach
# our interview/offer rows to the OTHER company's application instead of
# this one's -- verified locally: it doesn't error, it just silently
# corrupts the wrong tenant's data. Distinct names sidestep the bug
# entirely rather than patching that (pre-existing, shared) crud function.
_VERTEXA_CANDIDATES = [
    ("Naveen Bhaskaran", "Senior Backend Engineer", 6, "applied"),
    ("Ishika Talwar", "QA Automation Engineer", 3, "applied"),
    ("Farhan Siddiqui", "HR Business Partner", 5, "applied"),
    ("Ananya Krishnamurthy", "Product Marketing Manager", 4, "screened"),
    ("Devansh Oberoi", "Senior Backend Engineer", 7, "screened"),
    ("Priyanka Vaidya", "QA Automation Engineer", 4, "interview"),
    ("Rohan Balachandran", "Senior Backend Engineer", 5, "interview"),
    ("Simran Bedi", "Product Marketing Manager", 6, "offer"),
    ("Tanvi Ramaswamy", "QA Automation Engineer", 3, "hired"),
    ("Arvind Kannan", "Enterprise Account Executive", 8, "interview"),
    ("Meenal Purohit", "Data Scientist", 4, "screened"),
    ("Rakesh Iyengar", "Customer Success Manager", 6, "hired"),
]
_VERTEXA_INTERVIEWS = [
    {"candidate": "Rohan Balachandran", "job_title": "Senior Backend Engineer", "round_no": 2, "scheduled_at": "2026-07-11T15:00:00+05:30"},
    {"candidate": "Priyanka Vaidya", "job_title": "QA Automation Engineer", "round_no": 1, "scheduled_at": "2026-07-10T11:00:00+05:30"},
    {"candidate": "Devansh Oberoi", "job_title": "Senior Backend Engineer", "round_no": 1, "scheduled_at": "2026-07-09T17:00:00+05:30"},
    {"candidate": "Arvind Kannan", "job_title": "Enterprise Account Executive", "round_no": 2, "scheduled_at": "2026-07-14T10:00:00+05:30"},
]
_VERTEXA_OFFERS = [
    {"candidate": "Simran Bedi", "job_title": "Product Marketing Manager", "offered_ctc": 2_400_000,
     "offer_date": "2026-07-08", "proposed_joining_date": "2026-08-03", "status": "sent"},
    {"candidate": "Tanvi Ramaswamy", "job_title": "QA Automation Engineer", "offered_ctc": 1_150_000,
     "offer_date": "2026-07-05", "proposed_joining_date": "2026-07-20", "status": "accepted"},
    {"candidate": "Rakesh Iyengar", "job_title": "Customer Success Manager", "offered_ctc": 2_600_000,
     "offer_date": "2026-04-20", "proposed_joining_date": "2026-05-15", "status": "accepted"},
]


def ensure_recruitment(db, company, departments) -> None:
    if _already_has_rows(db, models.JobOpening, company_id=company.id):
        return

    for j in [*JOB_OPENINGS, *_EXTRA_JOB_OPENINGS]:
        try:
            crud.create_job_opening(
                db, company.id, j["title"], j["department"], j["employment_type"], j["vacancies"],
                status=j["status"], posted_date=datetime.date.fromisoformat(j["posted_date"]),
            )
            db.commit()
        except ValueError:
            db.rollback()
            continue

    for name, job_title, years_experience, stage in _VERTEXA_CANDIDATES:
        opening = crud.find_job_opening_by_title(db, company.id, job_title)
        if opening is None:
            continue
        candidate = crud.create_candidate(db, company.id, name, years_experience)
        crud.create_job_application(db, opening.id, candidate.id, stage)
        db.commit()

    for i in _VERTEXA_INTERVIEWS:
        application = crud.find_application_by_candidate_and_opening(db, company.id, i["candidate"], i["job_title"])
        if application is None:
            continue
        crud.create_interview(db, application.id, i["round_no"], datetime.datetime.fromisoformat(i["scheduled_at"]))
        db.commit()

    for o in _VERTEXA_OFFERS:
        application = crud.find_application_by_candidate_and_opening(db, company.id, o["candidate"], o["job_title"])
        if application is None:
            continue
        crud.create_offer(
            db, application.id, o["offered_ctc"], datetime.date.fromisoformat(o["offer_date"]),
            datetime.date.fromisoformat(o["proposed_joining_date"]), o["status"],
        )
        db.commit()
    print("Seeded recruitment (openings/candidates/interviews/offers)")


# ---------------------------------------------------------------------------
# Performance / Learning
# ---------------------------------------------------------------------------


def ensure_performance_and_learning(db, company, employees) -> None:
    emps = _emp_list(employees)

    if not _already_has_rows(db, models.CompanyOkr, company_id=company.id):
        okrs = [
            ("Company", "Grow ARR to $24M this fiscal year", "CEO Office", 52),
            ("Department", "Ship Platform v4 with < 0.5% crash rate", "Engineering", 61),
            ("Department", "Improve NPS from 46 to 58", "Customer Success", 44),
            ("Individual", f"Close 12 enterprise deals this half", emps[20].first_name + " " + (emps[20].last_name or ""), 38),
            ("Individual", "Reduce time-to-hire to 18 days", emps[min(57, len(emps)-1)].first_name + " " + (emps[min(57, len(emps)-1)].last_name or ""), 66),
        ]
        for level, title, owner_name, progress in okrs:
            okr = crud.create_okr(db, company.id, level, title, owner_name)
            okr.progress_pct = progress
        db.commit()
        print("Seeded OKRs")

    cycles: dict[str, models.AppraisalCycle] = {}
    if not _already_has_rows(db, models.AppraisalCycle, company_id=company.id):
        for r in REVIEW_CYCLES:
            review_cycle = crud.create_review_cycle(
                db, company.id, r["name"], datetime.date.fromisoformat(r["from_date"]),
                datetime.date.fromisoformat(r["to_date"]), r["status"], r["participant_count"],
            )
            db.commit()
            cycles[review_cycle.status] = review_cycle
        print("Seeded review cycles")
    else:
        for review_cycle in db.scalars(select(models.AppraisalCycle).where(models.AppraisalCycle.company_id == company.id)):
            cycles[review_cycle.status] = review_cycle

    completed_cycle = cycles.get("completed")
    in_progress_cycle = cycles.get("in_progress")
    ratings = ["Exceeds", "Meets", "Meets", "Below"]
    if completed_cycle is not None and not _already_has_rows(db, models.Appraisal, cycle_id=completed_cycle.id):
        for i, employee in enumerate(emps):
            appraisal = models.Appraisal(
                id=uuid.uuid4(), cycle_id=completed_cycle.id, employee_id=employee.id,
                reviewer_id=employee.reporting_manager_id, self_score=round(3.0 + (i % 5) * 0.4, 1),
                manager_score=round(3.2 + (i % 4) * 0.4, 1), final_rating=ratings[i % len(ratings)],
                status="completed",
            )
            db.add(appraisal)
        db.commit()
        print("Seeded appraisals")

    if in_progress_cycle is not None and not _already_has_rows(db, models.Goal, cycle_id=in_progress_cycle.id):
        for i, employee in enumerate(emps):
            goal = models.Goal(
                id=uuid.uuid4(), cycle_id=in_progress_cycle.id, employee_id=employee.id,
                title=f"Q2 FY26 goal — {employee.department.name if employee.department else 'General'} delivery",
                weight_pct=100, progress_pct=float((i * 7) % 100), status="active",
            )
            db.add(goal)
        db.commit()
        print("Seeded goals")

    courses: dict[str, models.TrainingCourse] = {}
    if not _already_has_rows(db, models.TrainingCourse, company_id=company.id):
        for title, course_type, category, duration_hours, duration_label in COURSES:
            course = crud.create_training_course(db, company.id, title, course_type, category, duration_hours, duration_label)
            db.commit()
            courses[title] = course
        print("Seeded training courses")
    else:
        for c in db.scalars(select(models.TrainingCourse).where(models.TrainingCourse.company_id == company.id)):
            courses[c.title] = c

    if courses and not _already_has_rows(db, models.TrainingEnrollment, course_id=next(iter(courses.values())).id):
        course_cycle = cycle(courses.values())
        statuses = ["completed", "in_progress", "enrolled"]
        for i, employee in enumerate(emps[:60]):
            course = next(course_cycle)
            enrollment = models.TrainingEnrollment(
                id=uuid.uuid4(), course_id=course.id, employee_id=employee.id,
                status=statuses[i % len(statuses)],
                completion_date=TODAY - datetime.timedelta(days=i) if statuses[i % len(statuses)] == "completed" else None,
                score=float(70 + i % 30) if statuses[i % len(statuses)] == "completed" else None,
            )
            db.add(enrollment)
        db.commit()
        print("Seeded training enrollments")

    if not _already_has_rows(db, models.TrainingSession, company_id=company.id):
        sessions = [
            ("New Hire Orientation", "2026-07-05", "HR Team", True, 24),
            ("Leadership Offsite — FY26 H2 Kickoff", "2026-08-10", "External Facilitator", False, 30),
            ("Annual Security Awareness Training", "2026-06-20", "IT Security", True, 121),
        ]
        for title, session_date, trainer, mandatory, attendees in sessions:
            crud.create_training_session(
                db, company.id, title, datetime.date.fromisoformat(session_date), trainer, mandatory, attendees
            )
        db.commit()
        print("Seeded training sessions")

    if not _already_has_rows(db, models.Certification, employee_id=emps[0].id):
        cert_targets = [
            ("AWS Certified Solutions Architect", "AWS", 60, 700),
            ("PMP", "PMI", 400, 1000),
            ("Certified ScrumMaster", "Scrum Alliance", 30, 400),
        ]
        for i, employee in enumerate(emps[:25]):
            name, issuer, issued_days_ago, expiry_days_from_now = cert_targets[i % len(cert_targets)]
            crud.create_certification(
                db, employee.id, name, issuer,
                TODAY - datetime.timedelta(days=issued_days_ago),
                TODAY + datetime.timedelta(days=expiry_days_from_now if i % 5 else 45),
            )
        db.commit()
        print("Seeded standalone certifications (expiry variety)")


# ---------------------------------------------------------------------------
# Benefits / Assets / Documents
# ---------------------------------------------------------------------------

_EXTRA_ASSETS = [
    ("VTX-LT-1001", "Laptop", "Dell Latitude 7440", "available", "₹92,000", "2025-08-10"),
    ("VTX-LT-1002", "Laptop", 'MacBook Pro 14" M3', "assigned", "₹1,95,000", "2025-09-05"),
    ("VTX-LT-1003", "Laptop", "Lenovo ThinkPad X1", "assigned", "₹1,05,000", "2025-02-14"),
    ("VTX-MB-2001", "Mobile Device", "iPhone 15", "assigned", "₹72,000", "2025-05-01"),
    ("VTX-MB-2002", "Mobile Device", "Samsung Galaxy S24", "available", "₹58,000", "2025-06-10"),
    ("VTX-SM-3001", "SIM Card", "Corporate Postpaid", "assigned", "—", "2025-01-01"),
    ("VTX-ID-4001", "ID Card", "RFID Access Card", "assigned", "₹150", "2022-06-01"),
    ("VTX-LI-5001", "Software License", "JetBrains All Products", "assigned", "₹29,000 / yr", "2026-01-01"),
    ("VTX-LI-5002", "Software License", "Figma Organization", "assigned", "₹18,000 / yr", "2026-02-01"),
    ("VTX-LT-1004", "Laptop", "MacBook Air M2", "repair", "₹1,18,000", "2024-07-01"),
]


def ensure_benefits_assets_documents(db, company, employees) -> None:
    emps = _emp_list(employees)

    if not _already_has_rows(db, models.EmployeeBenefits, employee_id=emps[0].id):
        for i, employee in enumerate(emps):
            crud.upsert_employee_benefits(
                db, employee.id,
                insurance_plan="Premium Family Floater" if i % 3 == 0 else "Standard Health Cover",
                esop_units=500 if employee.band and employee.band <= 5 else 0,
                dependents_covered=i % 4,
                learning_budget_total=50_000.0,
                learning_budget_used=float((i * 1500) % 45_000),
                cab_facility=i % 4 == 0,
                meal_card=True,
                internet_reimbursement=i % 2 == 0,
                wellness_program=i % 3 != 0,
            )
        db.commit()
        print("Seeded employee benefits")

    assets: dict[str, models.AssetInventoryItem] = {}
    if not _already_has_rows(db, models.AssetInventoryItem, company_id=company.id):
        for tag, asset_type, model, status, value, purchased in [*ASSET_INVENTORY, *_EXTRA_ASSETS]:
            try:
                asset = crud.create_asset_inventory_item(
                    db, company.id, tag.replace("AST-", "VTX-"), asset_type, model, status,
                    crud.parse_inr_amount(value), crud.parse_date_safe(purchased),
                )
                db.commit()
                assets[asset.asset_tag] = asset
            except ValueError:
                db.rollback()
        print(f"Seeded {len(assets)} asset inventory items")
    else:
        for a in db.scalars(select(models.AssetInventoryItem).where(models.AssetInventoryItem.company_id == company.id)):
            assets[a.asset_tag] = a

    assigned_tags = [tag for tag, a in assets.items() if a.status == "assigned"]
    if assigned_tags and not _already_has_rows(db, models.AssetAssignment, employee_id=emps[0].id):
        for i, tag in enumerate(assigned_tags):
            employee = emps[i % len(emps)]
            assignment = crud.create_asset_assignment(
                db, assets[tag].id, employee.id, TODAY - datetime.timedelta(days=90 + i * 10)
            )
            if i % 4 == 0:  # a few returned, to populate the Returns tab
                assignment.returned_on = TODAY - datetime.timedelta(days=5)
                assignment.return_condition = "Good"
                assignment.return_notes = "Returned on offboarding checklist completion"
        db.commit()
        print("Seeded asset assignments")

    if not _already_has_rows(db, models.AssetRequestModel, employee_id=emps[0].id):
        for i, employee in enumerate(emps[:8]):
            request = crud.create_asset_request(
                db, employee.id, ["Laptop", "Mobile Device", "Software License"][i % 3],
                "Replacement for outdated hardware" if i % 2 else "New joiner setup",
            )
            if i % 2 == 0:
                request.status = "approved"
        db.commit()
        print("Seeded asset requests")

    if not _already_has_rows(db, models.DocumentRecord, employee_id=emps[0].id):
        for employee in emps:
            crud.create_document_record(db, employee.id, "Education Documents", "Verified", TODAY - datetime.timedelta(days=200), None)
            crud.create_document_record(db, employee.id, "Professional Documentation", "Verified", TODAY - datetime.timedelta(days=180), None)
        db.commit()
        print("Seeded employee document records")

    if not _already_has_rows(db, models.PolicyDocument, company_id=company.id):
        for name, version, effective_date, ack in POLICY_DOCS:
            try:
                crud.create_policy_document(db, company.id, "policy", name, version, datetime.date.fromisoformat(effective_date), ack)
                db.commit()
            except ValueError:
                db.rollback()
        for name in DOC_TEMPLATES:
            try:
                crud.create_policy_document(db, company.id, "template", name, None, None, None)
                db.commit()
            except ValueError:
                db.rollback()
        print("Seeded policy documents & templates")


# ---------------------------------------------------------------------------
# Projects / Work / Travel & Expense
# ---------------------------------------------------------------------------

_EXTRA_PROJECTS = [
    {"name": "Vertexa Data Lakehouse", "client_name": "Internal Product", "project_type": "Product",
     "pm_name": "Internal PMO", "status": "in_progress", "start_date": "2026-03-01", "end_date": "2026-12-15",
     "budget": "₹1.4Cr", "progress_pct": 48},
    {"name": "Global Retail CX Revamp", "client_name": "Northbridge Retail Group", "project_type": "Client — T&M",
     "pm_name": "Internal PMO", "status": "in_progress", "start_date": "2026-05-01", "end_date": "2026-11-30",
     "budget": "₹68L", "progress_pct": 30},
    {"name": "APAC Expansion Enablement", "client_name": "Internal — Sales", "project_type": "Internal",
     "pm_name": "Internal PMO", "status": "planning", "start_date": "2026-08-01", "end_date": "2027-02-28",
     "budget": "₹35L", "progress_pct": 5},
]


def ensure_projects_work_travel(db, company, employees, departments, branches) -> None:
    emps = _emp_list(employees)

    projects: dict[str, models.Project] = {}
    # pm_projects status check: draft|planning|active|on_hold|completed|cancelled
    _PRJ_STATUS = {"in_progress": "active", "active": "active", "planning": "planning",
                   "completed": "completed", "on_hold": "on_hold", "cancelled": "cancelled"}
    if not _already_has_rows(db, models.Project, company_id=company.id):
        for p in [*PROJECTS, *_EXTRA_PROJECTS]:
            try:
                project = crud.create_project(
                    db, company.id, p["name"], _PRJ_STATUS.get(p["status"], "planning"),
                    planned_start_date=datetime.date.fromisoformat(p["start_date"]),
                    planned_end_date=datetime.date.fromisoformat(p["end_date"]),
                )
                db.commit()
                projects[project.name] = project
            except ValueError:
                db.rollback()
        print(f"Seeded {len(projects)} projects")
    else:
        for pr in db.scalars(select(models.Project).where(models.Project.company_id == company.id)):
            projects[pr.name] = pr

    active_projects = [p for p in projects.values() if p.status in ("active", "planning")]
    if active_projects and not _already_has_rows(db, models.ProjectAllocation, employee_id=emps[0].id):
        proj_cycle = cycle(active_projects)
        for i, employee in enumerate(emps[:70]):
            project = next(proj_cycle)
            try:
                crud.create_project_allocation(
                    db, project.id, employee.id,
                    [50, 75, 100][i % 3],
                    start_date=TODAY,
                )
            except IntegrityError:
                db.rollback()
                continue
        db.commit()
        print("Seeded project allocations")

    in_progress_projects = [p for p in projects.values() if p.status == "active"]
    if in_progress_projects and not _already_has_rows(db, models.Sprint, project_id=in_progress_projects[0].id):
        for project in in_progress_projects:
            crud.create_sprint(db, project.id, "Sprint 1", TODAY - datetime.timedelta(days=28), TODAY - datetime.timedelta(days=14))
            crud.create_sprint(db, project.id, "Sprint 2", TODAY - datetime.timedelta(days=14), TODAY)
            crud.create_release(db, project.id, f"{project.name} — v1.0", TODAY + datetime.timedelta(days=30), "First GA release")
        db.commit()
        print("Seeded sprints & releases")

    # pm_tasks status check: todo|in_progress|in_review|blocked|done|cancelled
    _TASK_STATUS = {"Backlog": "todo", "In Progress": "in_progress", "In Review": "in_review", "Done": "done"}
    if in_progress_projects and not _already_has_rows(db, models.TaskBoardCard, project_id=in_progress_projects[0].id):
        columns = ["Backlog", "In Progress", "In Review", "Done"]
        for i, project in enumerate(in_progress_projects):
            for j, column in enumerate(columns):
                crud.create_task_board_card(
                    db, project.id, _TASK_STATUS[column], f"{project.name} — task {j + 1}",
                    company_id=company.id,
                )
        db.commit()
        print("Seeded task board cards")

    # Create timesheets first so work entries can reference them
    timesheets: dict[tuple, models.Timesheet] = {}  # (employee_id, week_start) -> ts
    if not _already_has_rows(db, models.Timesheet, employee_id=emps[0].id):
        for week_offset in (1, 2):
            week_start = TODAY - datetime.timedelta(days=TODAY.weekday() + 7 * week_offset)
            for i, employee in enumerate(emps[:50]):
                try:
                    ts = crud.create_timesheet(db, company.id, employee.id, week_start, 40.0, 32.0)
                    ts.status = ["submitted", "approved", "draft"][i % 3]
                    timesheets[(employee.id, week_start)] = ts
                except ValueError:
                    db.rollback()
                    continue
        db.commit()
        print("Seeded timesheets")
    else:
        for ts in db.scalars(select(models.Timesheet).where(
            models.Timesheet.employee_id.in_([e.id for e in emps[:50]])
        )):
            timesheets[(ts.employee_id, ts.week_start)] = ts

    project_list = list(projects.values())
    if project_list and not _already_has_rows(db, models.WorkEntry, project_id=project_list[0].id):
        for day_offset in range(14, 0, -1):
            the_date = TODAY - datetime.timedelta(days=day_offset)
            if the_date.weekday() >= 5:
                continue
            date_week_start = the_date - datetime.timedelta(days=the_date.weekday())
            for i, employee in enumerate(emps[:50]):
                project = project_list[(i + day_offset) % len(project_list)]
                ts = timesheets.get((employee.id, date_week_start))
                if ts is None:
                    continue
                try:
                    crud.create_work_entry(
                        db, ts.id, project.id, the_date,
                        category="Development" if i % 3 else "Meetings",
                        start_time=datetime.time(9, 30), end_time=datetime.time(18, 0),
                        hours=8.0,
                        description="Feature development" if i % 2 == 0 else "Client sync & delivery",
                        is_billable=i % 3 != 0,
                    )
                except Exception:
                    db.rollback()
                    continue
        db.commit()
        print("Seeded work entries")

    if not _already_has_rows(db, models.TravelRequestModel, employee_id=emps[0].id):
        destinations = ["Singapore", "Hyderabad", "Dubai", "Pune", "Noida"]
        for i, employee in enumerate(emps[:15]):
            request = crud.create_travel_request(
                db, employee.id, "Client workshop" if i % 2 else "Internal offsite",
                destinations[i % len(destinations)], TODAY + datetime.timedelta(days=5 + i),
                TODAY + datetime.timedelta(days=8 + i), ["Flight", "Train", "Cab"][i % 3],
                float(15_000 + i * 2_000),
            )
            request.status = ["pending", "approved", "rejected"][i % 3]
        db.commit()
        print("Seeded travel requests")

    expense_reports_exist = (db.scalar(
        select(func.count()).select_from(models.ExpenseClaim).where(
            models.ExpenseClaim.company_id == company.id, models.ExpenseClaim.claim_no.like("EXP-%")
        )
    ) or 0) > 0
    if not expense_reports_exist:
        for i, employee in enumerate(emps[:15]):
            line_items = [
                {"date": TODAY - datetime.timedelta(days=6 + i), "category": "Travel", "description": "Client visit cab fare", "amount": 1200.0},
                {"date": TODAY - datetime.timedelta(days=5 + i), "category": "Meals", "description": "Client dinner", "amount": 2400.0},
            ]
            try:
                claim = crud.create_expense_report(db, company.id, employee.id, f"Client visit — {employee.first_name}", TODAY - datetime.timedelta(days=4 + i), line_items)
                claim.status = ["submitted", "approved", "paid"][i % 3]
                db.commit()
            except ValueError:
                db.rollback()
        print("Seeded expense reports")


# ---------------------------------------------------------------------------
# Exit requests / Lifecycle events
# ---------------------------------------------------------------------------


def ensure_exit_and_lifecycle(db, company, employees, departments) -> None:
    emps = _emp_list(employees)
    exiting = [e for e in emps if e.designation and e.designation.band in ("Associate / Entry Level", "Professional / IC")][:3]

    if exiting and not _already_has_rows(db, models.ExitRequestModel, employee_id=exiting[0].id):
        for i, employee in enumerate(exiting):
            resignation_date = TODAY - datetime.timedelta(days=30 - i * 5)
            last_working_day = resignation_date + datetime.timedelta(days=45)
            exit_request = models.ExitRequestModel(
                id=uuid.uuid4(), employee_id=employee.id, resignation_date=resignation_date,
                last_working_day=last_working_day, reason="Career growth opportunity",
                status="in_clearance" if i == 0 else "submitted",
            )
            db.add(exit_request)
            db.flush()
            tasks = [
                ("Final settlement review", "pending"),
                ("Access revocation", "pending" if i else "done"),
                ("Knowledge transfer", "done" if i == 0 else "pending"),
                ("Asset return", "pending"),
            ]
            for task, status in tasks:
                db.add(models.ExitChecklistItem(
                    id=uuid.uuid4(), exit_id=exit_request.id, task=task, status=status,
                    owner_id=employee.reporting_manager_id,
                ))
            db.add(models.FinalSettlement(
                id=uuid.uuid4(), exit_id=exit_request.id,
                payable_amount=float((employee.annual_ctc or 600_000) / 12), recovery_amount=5_000.0,
                net_amount=float((employee.annual_ctc or 600_000) / 12) - 5_000.0,
                status="draft" if i else "posted",
            ))
        db.commit()
        print(f"Seeded {len(exiting)} exit requests")

    promotable = [e for e in emps if e.designation and e.designation.band == "Professional / IC"][3:7]
    if promotable and not _already_has_rows(db, models.EmployeeLifecycleEvent, employee_id=promotable[0].id):
        for employee in promotable:
            db.add(models.EmployeeLifecycleEvent(
                id=uuid.uuid4(), employee_id=employee.id, event_type="promotion",
                event_date=TODAY - datetime.timedelta(days=60),
                from_designation_id=employee.designation_id, to_designation_id=employee.designation_id,
                from_department_id=employee.department_id, to_department_id=employee.department_id,
                from_ctc=int((employee.annual_ctc or 700_000) * 0.85), to_ctc=employee.annual_ctc,
            ))
        db.commit()
        print(f"Seeded {len(promotable)} lifecycle events")


# ---------------------------------------------------------------------------
# Recognitions / Skill ratings
# ---------------------------------------------------------------------------

_BADGES = ["Employee of the Month", "Above & Beyond", "Client Champion", "Innovation Award", "Team Player"]


def ensure_recognitions_and_skills(db, company, employees) -> None:
    emps = _emp_list(employees)

    if not _already_has_rows(db, models.Recognition, employee_id=emps[0].id):
        for i, employee in enumerate(emps[:15]):
            crud.create_recognition(
                db, employee.id, _BADGES[i % len(_BADGES)],
                "Recognized for outstanding contribution this quarter",
                TODAY - datetime.timedelta(days=10 + i * 7),
            )
        db.commit()
        print("Seeded recognitions")

    if not _already_has_rows(db, models.EmployeeSkillRating, employee_id=emps[0].id):
        skills = ["Python", "Communication", "Leadership", "Problem Solving", "Client Management"]
        levels = ["Beginner", "Intermediate", "Advanced", "Expert"]
        for i, employee in enumerate(emps[:40]):
            crud.upsert_skill_rating(db, employee.id, skills[i % len(skills)], levels[i % len(levels)])
            crud.upsert_skill_rating(db, employee.id, skills[(i + 1) % len(skills)], levels[(i + 2) % len(levels)])
        db.commit()
        print("Seeded skill ratings")


# ---------------------------------------------------------------------------
# Hiring requisitions / Salary revision requests
# ---------------------------------------------------------------------------


def ensure_hiring_and_salary_revisions(db, company, employees, departments) -> None:
    emps = _emp_list(employees)
    requesters = [e for e in emps if e.department_id == departments["HR"].id][:3] or emps[:3]

    if not _already_has_rows(db, models.HiringRequisition, requested_by=requesters[0].id):
        plans = [
            ("Senior Software Engineer", "ENG", 3, "Backlog growth in platform team"),
            ("Sales Development Representative", "SLS", 2, "Pipeline expansion for H2"),
            ("People Partner", "HR", 1, "Support headcount growth across delivery centers"),
        ]
        # Fall back to first available department code if the hardcoded code is absent
        _dept_codes = list(departments.keys())
        for i, (title, dept_code, count, justification) in enumerate(plans):
            requester = requesters[i % len(requesters)]
            resolved = dept_code if dept_code in departments else _dept_codes[i % len(_dept_codes)]
            try:
                crud.create_hiring_requisition(
                    db, company.id, requester.id, title, departments[resolved].name, count, justification
                )
                db.commit()
            except ValueError:
                db.rollback()
        print("Seeded hiring requisitions")

    if not _already_has_rows(db, models.SalaryRevisionRequest, employee_id=emps[10].id):
        for i, employee in enumerate(emps[10:16]):
            current = employee.annual_ctc or 900_000
            crud.create_salary_revision_request(
                db, employee.id, current, int(current * 1.12),
                "Annual performance-linked increment recommendation",
            )
        db.commit()
        print("Seeded salary revision requests")


# ---------------------------------------------------------------------------
# Notifications / Integrations
# ---------------------------------------------------------------------------

_INTEGRATIONS = [
    ("Slack", "Team messaging and approval notifications", True),
    ("Microsoft Teams", "Video meetings and chat", False),
    ("Google Workspace", "Calendar & email sync", True),
    ("Zoom", "Video conferencing for interviews", True),
    ("GitHub", "Engineering source control", True),
    ("Jira", "Sprint & task tracking", False),
]


def ensure_notifications_and_integrations(db, company, employees) -> None:
    if not _already_has_rows(db, models.Integration, company_id=company.id):
        for name, description, is_connected in _INTEGRATIONS:
            crud.upsert_integration(db, company.id, name, description, is_connected)
        db.commit()
        print("Seeded integrations")

    emps = _emp_list(employees)[:6]
    users = db.scalars(
        select(models.User).where(models.User.company_id == company.id, models.User.employee_id.in_([e.id for e in emps]))
    ).all()
    if users and not _already_has_rows(db, models.NotificationPreference, user_id=users[0].id):
        for user in users:
            crud.upsert_notification_preference(db, user.id, "leave_request_status", "email", True)
            crud.upsert_notification_preference(db, user.id, "approval_pending", "push", True)
        db.commit()
        print("Seeded notification preferences")


# ---------------------------------------------------------------------------
# Audit logs
# ---------------------------------------------------------------------------


def ensure_audit_logs(db, company, employees) -> None:
    if _already_has_rows(db, models.AuditLog, company_id=company.id):
        return
    emps = _emp_list(employees)
    users_by_employee = {
        u.employee_id: u for u in db.scalars(
            select(models.User).where(models.User.company_id == company.id)
        )
    }
    actions = [
        ("create", "employee"), ("update", "leave_request"), ("create", "asset_inventory_item"),
        ("update", "reimbursement"), ("create", "project"), ("update", "role_matrix"),
    ]
    for i, (action, doctype) in enumerate(actions):
        employee = emps[i % len(emps)]
        user = users_by_employee.get(employee.id)
        crud.create_audit_log(
            db, company.id, user.id if user else None, action, doctype, employee.id,
        )
    db.commit()
    print("Seeded audit log entries")
