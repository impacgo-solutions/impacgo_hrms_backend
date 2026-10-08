"""Reference + generated data for a second, fully independent demo company
("Vertexa Technologies Pvt Ltd"), covering every module the frontend reads so
that logging in as any of its 11 roles shows a fully populated app -- not
just an org chart.

Everything below is plain data + deterministic generation (seeded RNG, no
wall-clock/randomness that would break re-running this script). The
orchestrator (seed_new_company.py) is responsible for turning this into real
rows via app.crud, in dependency order (company -> roles -> designations ->
branches -> departments -> employees -> everything else).
"""

import random
from datetime import date, timedelta

from .designation_seed_data import BAND_NUMBERS, DESIGNATION_BANDS

COMPANY_NAME = "Vertexa Technologies Pvt Ltd"
COMPANY_LEGAL_NAME = "Vertexa Technologies Private Limited"
COMPANY_META = {
    "gstin": "29AAFCV5678K1Z2",
    "pan": "AAFCV5678K",
    "cin": "U72900KA2015PTC112233",
    "industry": "Information Technology & Services",
    "founded_date": date(2015, 6, 1),
    "address_line1": "Tower 3, Prestige Tech Park",
    "address_line2": "Kadubeesanahalli",
    "city": "Bengaluru",
    "state": "Karnataka",
    "pincode": "560103",
    "country": "India",
}
EMAIL_DOMAIN = "vertexatech.com"

# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

BRANCHES: list[dict] = [
    {"code": "VTX-HQ", "name": "Bengaluru HQ", "country": "India", "state": "Karnataka", "city": "Bengaluru", "tax": "29AAFCV5678K1Z2", "is_head_office": True},
    {"code": "VTX-HYD", "name": "Hyderabad Delivery Center", "country": "India", "state": "Telangana", "city": "Hyderabad", "tax": "36AAFCV5678K1Z5", "is_head_office": False},
    {"code": "VTX-PUN", "name": "Pune Technology Park", "country": "India", "state": "Maharashtra", "city": "Pune", "tax": "27AAFCV5678K1Z8", "is_head_office": False},
    {"code": "VTX-NOI", "name": "Noida Business Park", "country": "India", "state": "Uttar Pradesh", "city": "Noida", "tax": "09AAFCV5678K1Z1", "is_head_office": False},
    {"code": "VTX-SGP", "name": "Singapore Office", "country": "Singapore", "state": None, "city": "Singapore", "tax": "UEN 202145678K", "is_head_office": False},
    {"code": "VTX-RMT", "name": "Remote / Virtual", "country": "Global", "state": None, "city": None, "tax": None, "is_head_office": False},
]

# ---------------------------------------------------------------------------
# Business units / Departments
# ---------------------------------------------------------------------------

BUSINESS_UNITS: list[dict] = [
    {"name": "Technology & Delivery", "cost_center": "CC-VTX-100"},
    {"name": "Products & Innovation", "cost_center": "CC-VTX-200"},
    {"name": "Revenue & Growth", "cost_center": "CC-VTX-300"},
    {"name": "Corporate Services", "cost_center": "CC-VTX-400"},
]

# code -> (name, business_unit_name, home_branch_code, subs, annual_budget)
DEPARTMENTS: list[dict] = [
    {"code": "EXO", "name": "Executive Office", "business_unit_name": "Corporate Services", "branch_code": "VTX-HQ",
     "subs": ["Office of the CEO", "Strategy & Corporate Affairs"], "annual_budget": 45_000_000},
    {"code": "ENG", "name": "Engineering", "business_unit_name": "Technology & Delivery", "branch_code": "VTX-HQ",
     "subs": ["Frontend", "Backend", "Full Stack", "Mobile", "DevOps", "Cloud Infrastructure", "Site Reliability", "QA & Test Automation"],
     "annual_budget": 180_000_000},
    {"code": "DAT", "name": "Data & Analytics", "business_unit_name": "Products & Innovation", "branch_code": "VTX-HQ",
     "subs": ["Data Science", "Data Platform", "Business Intelligence"], "annual_budget": 60_000_000},
    {"code": "PRD", "name": "Product Management", "business_unit_name": "Products & Innovation", "branch_code": "VTX-HQ",
     "subs": ["Product Strategy", "Product Design", "UX Research"], "annual_budget": 40_000_000},
    {"code": "SLS", "name": "Sales", "business_unit_name": "Revenue & Growth", "branch_code": "VTX-NOI",
     "subs": ["Enterprise Sales", "Inside Sales", "Channel Sales", "Pre-Sales"], "annual_budget": 70_000_000},
    {"code": "MKT", "name": "Marketing", "business_unit_name": "Revenue & Growth", "branch_code": "VTX-NOI",
     "subs": ["Digital Marketing", "Content & Brand", "Events & Field Marketing"], "annual_budget": 35_000_000},
    {"code": "CSM", "name": "Customer Success", "business_unit_name": "Revenue & Growth", "branch_code": "VTX-PUN",
     "subs": ["Customer Support", "Onboarding & Implementation", "Account Management"], "annual_budget": 30_000_000},
    {"code": "HR", "name": "Human Resources", "business_unit_name": "Corporate Services", "branch_code": "VTX-HQ",
     "subs": ["Talent Acquisition", "HR Operations", "Payroll & Benefits", "L&D", "Compliance & ER"], "annual_budget": 25_000_000},
    {"code": "FIN", "name": "Finance", "business_unit_name": "Corporate Services", "branch_code": "VTX-HQ",
     "subs": ["Accounting", "Accounts Payable", "Treasury & FP&A", "Taxation", "Internal Audit"], "annual_budget": 28_000_000},
    {"code": "LEG", "name": "Legal", "business_unit_name": "Corporate Services", "branch_code": "VTX-HQ",
     "subs": ["Corporate Legal", "Contracts & Vendor Management", "Compliance"], "annual_budget": 12_000_000},
    {"code": "IT", "name": "Information Technology", "business_unit_name": "Corporate Services", "branch_code": "VTX-HYD",
     "subs": ["Infrastructure", "IT Helpdesk", "IAM & Security", "Enterprise Applications"], "annual_budget": 22_000_000},
    {"code": "OPS", "name": "Operations", "business_unit_name": "Corporate Services", "branch_code": "VTX-HYD",
     "subs": ["Business Operations", "Program Management", "Facilities & Admin"], "annual_budget": 18_000_000},
]

# Departments that get some headcount spread beyond their home branch (for
# branch-level headcount variety) -> extra branch codes to draw from.
DEPARTMENT_EXTRA_BRANCHES: dict[str, list[str]] = {
    "ENG": ["VTX-HYD", "VTX-PUN", "VTX-SGP", "VTX-RMT"],
    "DAT": ["VTX-PUN", "VTX-RMT"],
    "CSM": ["VTX-SGP", "VTX-RMT"],
    "SLS": ["VTX-SGP"],
}

# ---------------------------------------------------------------------------
# Role -> designation cycle (title strings must exist verbatim in
# DESIGNATION_BANDS so create_employee resolves the *real* seeded
# designation instead of silently minting an "Unassigned"-band one).
# ---------------------------------------------------------------------------

TITLE_TO_BAND: dict[str, str] = {
    title: band_name for band_name, titles in DESIGNATION_BANDS for title in titles
}

ROLE_DESIGNATION_CYCLE: dict[str, list[str]] = {
    "Organization Owner / CEO": ["CEO"],
    "C-Level Executive": ["COO", "CTO", "CFO", "CHRO"],
    "General Manager / Sr. Manager": [
        "General Manager", "Senior General Manager", "Regional General Manager",
        "Deputy General Manager", "Operations General Manager",
    ],
    "Manager": ["Manager", "Senior Manager", "Assistant Manager"],
    "Team Lead": [
        "Technical Lead", "Engineering Lead", "Project Lead", "Delivery Lead",
        "QA Lead", "Design Lead", "Sales Lead", "HR Lead", "Finance Lead", "Operations Lead",
    ],
    "HR / Recruitment Staff": ["Business Analyst", "Consultant", "Senior Consultant"],
    "Finance / Payroll Staff": ["Business Analyst", "Consultant", "Senior Consultant"],
    "IT / System Admin": ["DevOps Engineer", "Consultant", "Senior Consultant"],
    "Professional / IC Employee": [
        "Software Engineer", "Senior Software Engineer", "QA Engineer", "DevOps Engineer",
        "Business Analyst", "Product Designer", "Data Analyst", "AI Engineer",
        "Consultant", "Technical Writer", "Senior Business Analyst",
    ],
    "Associate / Intern": [
        "Senior Associate", "Associate", "Junior Associate", "Associate Software Engineer",
        "GET", "Apprentice", "Intern", "Contract Employee", "Freelancer",
    ],
}

# VP/Director tier assigned individually (title, department_code, reports_to_code_key)
VP_DIRECTOR_PLAN = [
    {"title": "Vice President", "department": "ENG", "label": "VP Engineering", "reports_to": "CTO"},
    {"title": "Vice President", "department": "PRD", "label": "VP Product", "reports_to": "COO"},
    {"title": "Director", "department": "SLS", "label": "Director Sales", "reports_to": "COO"},
    {"title": "Director", "department": "MKT", "label": "Director Marketing", "reports_to": "COO"},
    {"title": "Director", "department": "HR", "label": "Director HR", "reports_to": "CHRO"},
    {"title": "Senior Director", "department": "FIN", "label": "Director Finance", "reports_to": "CFO"},
]

# department_code -> which C-level title it should escalate to when it has
# no VP/Director of its own.
DEPT_NO_VP_ESCALATION = {
    "DAT": "VP Product", "CSM": "Director Sales", "LEG": "CFO", "IT": "COO", "OPS": "COO",
}

# department_code -> staffing plan for the tiers below GM.
STAFFING: dict[str, dict[str, int]] = {
    "ENG": {"manager": 4, "team_lead": 6, "ic": 16, "associate": 4},
    "DAT": {"manager": 1, "team_lead": 2, "ic": 5, "associate": 1},
    "PRD": {"manager": 1, "team_lead": 1, "ic": 3, "associate": 0},
    "SLS": {"manager": 3, "team_lead": 3, "ic": 6, "associate": 2},
    "MKT": {"manager": 2, "team_lead": 2, "ic": 4, "associate": 1},
    "CSM": {"manager": 2, "team_lead": 2, "ic": 4, "associate": 1},
    "HR": {"manager": 1, "team_lead": 1, "hr_staff": 5, "associate": 1},
    "FIN": {"manager": 1, "team_lead": 0, "finance_staff": 5, "associate": 1},
    "LEG": {"manager": 0, "team_lead": 0, "ic": 2, "associate": 0},
    "IT": {"manager": 1, "team_lead": 1, "it_staff": 4, "associate": 0},
    "OPS": {"manager": 0, "team_lead": 0, "ic": 0, "associate": 0},
}
GM_DEPARTMENTS = [d["code"] for d in DEPARTMENTS if d["code"] != "EXO"]

# ---------------------------------------------------------------------------
# Name pools (deterministic RNG -- reproducible across re-runs)
# ---------------------------------------------------------------------------

_RNG = random.Random(20260723)

FIRST_NAMES_M = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Krishna", "Ishaan", "Rohan", "Kabir", "Aryan",
    "Dhruv", "Karthik", "Nikhil", "Siddharth", "Rahul", "Amitabh", "Vikram", "Sanjay", "Rajesh", "Anand",
    "Manish", "Suresh", "Deepak", "Ashwin", "Gaurav", "Harsh", "Naveen", "Pranav", "Tarun", "Varun",
    "Yash", "Abhinav", "Girish", "Kunal", "Mohit", "Nitin", "Om", "Pratik", "Raghav", "Sameer",
    "Wei Ming", "Jia Hao", "Marcus Tan", "Daniel Lim",
]
FIRST_NAMES_F = [
    "Aadhya", "Diya", "Ananya", "Ishita", "Kavya", "Meera", "Priya", "Riya", "Sanya", "Tara",
    "Anjali", "Divya", "Neha", "Pooja", "Radhika", "Shreya", "Sneha", "Swati", "Vidya", "Nisha",
    "Kritika", "Lavanya", "Mahima", "Namrata", "Ojasvi", "Pallavi", "Ritika", "Sakshi", "Trisha", "Urvashi",
    "Yamini", "Aditi", "Bhavna", "Chitra", "Deepa", "Esha", "Farah", "Gauri", "Hema", "Ipsita",
    "Wei Ling", "Hui Min", "Michelle Lee", "Sarah Goh",
]
LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Iyer", "Nair", "Menon", "Rao", "Reddy", "Kulkarni", "Joshi",
    "Malhotra", "Chawla", "Kapoor", "Bhatia", "Khanna", "Mehta", "Shah", "Patel", "Desai", "Trivedi",
    "Agarwal", "Bansal", "Chopra", "Dutta", "Ghosh", "Banerjee", "Mukherjee", "Sinha", "Pillai", "Krishnan",
    "Rathore", "Bose", "Ali", "Singh", "Chauhan", "Yadav", "Mishra", "Pandey", "Tiwari", "Saxena",
    "Tan", "Lim", "Lee", "Goh", "Ng", "Wong",
]

_STATE_CODES = ["29", "36", "27", "09", "07", "19"]


def _fake_pan(seq: int) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return f"{letters[seq % 26]}{letters[(seq * 7) % 26]}{letters[(seq * 13) % 26]}P{seq % 10000:04d}{letters[(seq * 3) % 26]}"


def _fake_ifsc(seq: int) -> tuple[str, str]:
    banks = [("HDFC Bank", "HDFC0"), ("ICICI Bank", "ICIC0"), ("State Bank of India", "SBIN0"),
             ("Axis Bank", "UTIB0"), ("Kotak Mahindra Bank", "KKBK0")]
    name, prefix = banks[seq % len(banks)]
    return name, f"{prefix}{seq % 10000:06d}".ljust(11, "0")[:11]


def _fake_phone(seq: int) -> str:
    return f"+91 9{(700000000 + seq * 37) % 100000000:08d}"


def _fake_pincode(seq: int) -> str:
    return f"{560000 + (seq % 900):06d}"


def ctc_for_band(band_num: int, is_ceo: bool = False) -> int:
    if is_ceo:
        return _RNG.randint(11_000_000, 13_000_000)
    ranges = {
        2: (8_000_000, 9_800_000),   # C-level
        3: (5_800_000, 7_000_000),   # VP
        4: (4_500_000, 5_600_000),   # Director
        5: (2_800_000, 3_600_000),   # GM
        6: (1_600_000, 2_200_000),   # Manager
        7: (1_100_000, 1_500_000),   # Team Lead
        8: (700_000, 1_450_000),     # Professional/IC + staff
        10: (350_000, 620_000),      # Associate/Intern
    }
    lo, hi = ranges.get(band_num, (600_000, 900_000))
    return _RNG.randint(lo, hi) // 1000 * 1000


def _dob_for_band(band_num: int, is_ceo: bool = False) -> str:
    today = date(2026, 7, 23)
    age_ranges = {2: (48, 58) if is_ceo else (42, 54), 3: (38, 48), 4: (36, 46), 5: (34, 44),
                  6: (30, 40), 7: (27, 35), 8: (24, 34), 10: (21, 27)}
    lo, hi = age_ranges.get(band_num, (25, 35))
    age = _RNG.randint(lo, hi)
    birth_year = today.year - age
    month = _RNG.randint(1, 12)
    day = _RNG.randint(1, 28)
    return f"{month:02d}/{day:02d}/{birth_year}"


def _doj_for_band(band_num: int) -> date:
    today = date(2026, 7, 23)
    tenure_ranges = {2: (900, 3600), 3: (700, 3000), 4: (600, 2600), 5: (500, 2200),
                     6: (365, 1800), 7: (240, 1400), 8: (60, 1100), 10: (10, 400)}
    lo, hi = tenure_ranges.get(band_num, (60, 900))
    return today - timedelta(days=_RNG.randint(lo, hi))


SKILL_POOL = [
    "Python", "Java", "TypeScript", "React", "Flutter", "Node.js", "AWS", "Kubernetes", "Docker",
    "PostgreSQL", "Kafka", "Terraform", "GraphQL", "CI/CD", "Salesforce", "SAP", "Excel Modelling",
    "SEO", "Google Ads", "Figma", "Jira", "Six Sigma", "Labour Law", "GST Compliance", "IFRS",
]
CERT_POOL = [
    "AWS Certified Solutions Architect", "PMP", "Certified ScrumMaster", "Google Analytics Certified",
    "SHRM-CP", "CFA Level 1", "CPA", "ITIL Foundation", "CISSP", "Six Sigma Green Belt",
]
INSTITUTES = [
    "IIT Bombay", "IIT Delhi", "IIM Bangalore", "BITS Pilani", "NIT Trichy", "Delhi University",
    "Anna University", "VIT Vellore", "Manipal Institute of Technology", "Symbiosis International University",
]
QUALIFICATIONS = ["B.Tech", "M.Tech", "MBA", "B.Com", "M.Com", "B.Sc", "MCA", "BBA", "CA", "LLB"]


def _pick_name(gender: str) -> tuple[str, str]:
    first = _RNG.choice(FIRST_NAMES_M if gender == "Male" else FIRST_NAMES_F)
    last = _RNG.choice(LAST_NAMES)
    return first, last


def build_employees() -> list[dict]:
    """Returns the full employee roster in top-down creation order (manager
    always precedes report). Each dict's `reporting_manager_code` refers to
    another dict's `employee_code` earlier in the same list."""

    employees: list[dict] = []
    seq = 0
    # department_code -> {"gm": code, "managers": [codes], "team_leads": [codes]}
    dept_people: dict[str, dict[str, list[str]]] = {d["code"]: {"managers": [], "team_leads": []} for d in DEPARTMENTS}
    title_holder: dict[str, str] = {}  # e.g. "CTO" -> employee_code, "CEO" -> employee_code

    def _new_code() -> str:
        nonlocal seq
        seq += 1
        return f"VTX-{seq:04d}"

    def _make(role_name: str, designation: str, department_code: str, reports_to: str | None,
              branch_code: str | None = None, sub_department: str | None = None,
              team: str | None = None, title_key: str | None = None) -> dict:
        nonlocal seq
        code = _new_code()
        band_name = TITLE_TO_BAND.get(designation, "Professional / IC")
        band_num = BAND_NUMBERS.get(band_name, 8)
        gender = "Male" if seq % 2 == 1 else "Female"
        first, last = _pick_name(gender)
        dept = next(d for d in DEPARTMENTS if d["code"] == department_code)
        branch = branch_code or dept["branch_code"]
        is_ceo = designation == "CEO"
        edu_idx = seq % len(INSTITUTES)
        emp = {
            "employee_code": code,
            "first_name": first,
            "last_name": last,
            "work_email": f"{first.lower().replace(' ', '.')}.{last.lower()}.{seq}@{EMAIL_DOMAIN}",
            "role_name": role_name,
            "password": "123456",
            "phone": _fake_phone(seq),
            "gender": gender,
            "date_of_birth": _dob_for_band(band_num, is_ceo),
            "date_of_joining": _doj_for_band(band_num),
            "employment_type": "intern" if role_name == "Associate / Intern" and seq % 4 == 0 else "full_time",
            "status": "Probation" if (date(2026, 7, 23) - _doj_for_band(band_num)).days < 90 and band_num >= 8 and seq % 6 == 0 else "Active",
            "branch_code": branch,
            "department_code": department_code,
            "sub_department": sub_department or _RNG.choice(dept["subs"]),
            "designation_name": designation,
            "band_name": band_name,
            "numeric_band": band_num,
            "team": team or f"{dept['name']} — {_RNG.choice(dept['subs'])}",
            "work_mode": ["Hybrid", "Office", "Remote"][seq % 3],
            "annual_ctc": ctc_for_band(band_num, is_ceo),
            "reporting_manager_code": reports_to,
            "pan": _fake_pan(seq),
            "bank_name": _fake_ifsc(seq)[0],
            "bank_account_no": f"{10000000000 + seq * 91}",
            "bank_ifsc": _fake_ifsc(seq)[1],
            "qualification": QUALIFICATIONS[seq % len(QUALIFICATIONS)],
            "institute": INSTITUTES[edu_idx],
            "specialization": "Computer Science" if department_code in ("ENG", "DAT", "IT") else "General Management",
            "year_of_passing": 2026 - (band_num * 2 + seq % 5),
            "previous_employer": f"{LAST_NAMES[seq % len(LAST_NAMES)]} {'Technologies' if seq % 2 else 'Consulting'} Ltd" if band_num < 10 else None,
            "experience_years": str(round(max(0.5, (2026 - int(_doj_for_band(band_num).year)) + band_num * 0.6), 1)),
            "domain": dept["name"],
            "skills": _RNG.sample(SKILL_POOL, k=min(4, len(SKILL_POOL))),
            "certifications": _RNG.sample(CERT_POOL, k=1) if band_num <= 7 and seq % 3 == 0 else [],
            "emergency_contact_name": f"{_RNG.choice(FIRST_NAMES_M + FIRST_NAMES_F)} {last}",
            "emergency_contact_relation": _RNG.choice(["Spouse", "Parent", "Sibling"]),
            "emergency_contact_phone": _fake_phone(seq + 500),
            "blood_group": _RNG.choice(["A+", "B+", "O+", "AB+", "O-", "A-"]),
            "nationality": "Singaporean" if branch == "VTX-SGP" else "Indian",
            "marital_status": "Single" if band_num >= 8 else _RNG.choice(["Single", "Married"]),
            "personal_email": f"{first.lower()}.{last.lower()}{seq}@gmail.com",
            "personal_phone": _fake_phone(seq + 900),
            "current_address": f"{100 + seq}, {_RNG.choice(['Green Park', 'Lake View', 'MG Road', 'Sector 12', 'Whitefield'])}, {branch}",
            "permanent_address": f"{200 + seq}, {_RNG.choice(['Civil Lines', 'Model Town', 'Anna Nagar', 'Jubilee Hills'])}",
        }
        if title_key:
            title_holder[title_key] = code
        return emp

    # --- Tier 0: CEO ---
    ceo = _make("Organization Owner / CEO", "CEO", "EXO", None, title_key="CEO")
    employees.append(ceo)

    # --- Tier 1: C-Level ---
    for title in ROLE_DESIGNATION_CYCLE["C-Level Executive"]:
        dept_map = {"CTO": "ENG", "CFO": "FIN", "CHRO": "HR", "COO": "OPS"}
        e = _make("C-Level Executive", title, dept_map[title], ceo["employee_code"], title_key=title)
        employees.append(e)

    # --- Tier 2: VP / Director ---
    for plan in VP_DIRECTOR_PLAN:
        reports_to_code = title_holder[plan["reports_to"]]
        e = _make("VP / Director", plan["title"], plan["department"], reports_to_code,
                   title_key=plan["label"])
        employees.append(e)

    # --- Tier 3: GM / Sr. Manager (one per non-EXO department) ---
    for dept_code in GM_DEPARTMENTS:
        vp_entry = next((p for p in VP_DIRECTOR_PLAN if p["department"] == dept_code), None)
        if vp_entry is not None:
            reports_to_code = title_holder[vp_entry["label"]]
        else:
            reports_to_code = title_holder[DEPT_NO_VP_ESCALATION[dept_code]]
        title = ROLE_DESIGNATION_CYCLE["General Manager / Sr. Manager"][
            GM_DEPARTMENTS.index(dept_code) % len(ROLE_DESIGNATION_CYCLE["General Manager / Sr. Manager"])
        ]
        e = _make("General Manager / Sr. Manager", title, dept_code, reports_to_code)
        employees.append(e)
        dept_people[dept_code]["gm"] = e["employee_code"]

    # --- Tier 4: Managers ---
    mgr_titles = ROLE_DESIGNATION_CYCLE["Manager"]
    for dept_code in GM_DEPARTMENTS:
        count = STAFFING.get(dept_code, {}).get("manager", 0)
        for i in range(count):
            e = _make("Manager", mgr_titles[i % len(mgr_titles)], dept_code, dept_people[dept_code]["gm"])
            employees.append(e)
            dept_people[dept_code]["managers"].append(e["employee_code"])

    # --- Tier 5: Team Leads ---
    tl_titles = ROLE_DESIGNATION_CYCLE["Team Lead"]
    for dept_code in GM_DEPARTMENTS:
        count = STAFFING.get(dept_code, {}).get("team_lead", 0)
        managers = dept_people[dept_code]["managers"]
        for i in range(count):
            reports_to = managers[i % len(managers)] if managers else dept_people[dept_code]["gm"]
            e = _make("Team Lead", tl_titles[i % len(tl_titles)], dept_code, reports_to)
            employees.append(e)
            dept_people[dept_code]["team_leads"].append(e["employee_code"])

    # --- Tier 6: functional staff (HR / Finance / IT) ---
    for role_name, staffing_key, dept_code in [
        ("HR / Recruitment Staff", "hr_staff", "HR"),
        ("Finance / Payroll Staff", "finance_staff", "FIN"),
        ("IT / System Admin", "it_staff", "IT"),
    ]:
        count = STAFFING.get(dept_code, {}).get(staffing_key, 0)
        titles = ROLE_DESIGNATION_CYCLE[role_name]
        managers = dept_people[dept_code]["managers"] or [dept_people[dept_code]["gm"]]
        for i in range(count):
            e = _make(role_name, titles[i % len(titles)], dept_code, managers[i % len(managers)])
            employees.append(e)

    # --- Tier 7: Professional / IC ---
    ic_titles = ROLE_DESIGNATION_CYCLE["Professional / IC Employee"]
    for dept_code in GM_DEPARTMENTS:
        count = STAFFING.get(dept_code, {}).get("ic", 0)
        team_leads = dept_people[dept_code]["team_leads"]
        managers = dept_people[dept_code]["managers"]
        reports_pool = team_leads or managers or [dept_people[dept_code]["gm"]]
        extra_branches = DEPARTMENT_EXTRA_BRANCHES.get(dept_code, [])
        for i in range(count):
            branch_code = None
            if extra_branches and i % 3 == 0:
                branch_code = extra_branches[i % len(extra_branches)]
            e = _make(
                "Professional / IC Employee", ic_titles[i % len(ic_titles)], dept_code,
                reports_pool[i % len(reports_pool)], branch_code=branch_code,
            )
            employees.append(e)

    # --- Tier 8: Associate / Intern ---
    assoc_titles = ROLE_DESIGNATION_CYCLE["Associate / Intern"]
    for dept_code in GM_DEPARTMENTS:
        count = STAFFING.get(dept_code, {}).get("associate", 0)
        team_leads = dept_people[dept_code]["team_leads"]
        managers = dept_people[dept_code]["managers"]
        reports_pool = team_leads or managers or [dept_people[dept_code]["gm"]]
        for i in range(count):
            e = _make(
                "Associate / Intern", assoc_titles[i % len(assoc_titles)], dept_code,
                reports_pool[i % len(reports_pool)],
            )
            employees.append(e)

    return employees


EMPLOYEES = build_employees()
EMPLOYEE_BY_CODE = {e["employee_code"]: e for e in EMPLOYEES}

# One quick-login account per role for the handoff cheat-sheet (first person
# found in EMPLOYEES holding that role).
QUICK_LOGIN_BY_ROLE: dict[str, dict] = {}
for _e in EMPLOYEES:
    QUICK_LOGIN_BY_ROLE.setdefault(_e["role_name"], _e)
