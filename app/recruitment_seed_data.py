"""Ported 1:1 from lib/data/seed/recruitment_seed.dart (kJobOpenings,
kCandidatePipeline, kInterviews, kOffers).
"""

# (title, department_name, employment_type, vacancies, status, posted_date)
JOB_OPENINGS: list[dict] = [
    {
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "employment_type": "full_time",
        "vacancies": 2,
        "status": "open",
        "posted_date": "2026-06-15",
    },
    {
        "title": "Product Marketing Manager",
        "department": "Marketing",
        "employment_type": "full_time",
        "vacancies": 1,
        "status": "open",
        "posted_date": "2026-06-20",
    },
    {
        "title": "QA Automation Engineer",
        "department": "Engineering",
        "employment_type": "full_time",
        "vacancies": 3,
        "status": "open",
        "posted_date": "2026-06-28",
    },
    {
        "title": "HR Business Partner",
        "department": "Human Resources",
        "employment_type": "full_time",
        "vacancies": 1,
        "status": "on_hold",
        "posted_date": "2026-05-30",
    },
    {
        "title": "Summer Interns — Engineering",
        "department": "Engineering",
        "employment_type": "intern",
        "vacancies": 5,
        "status": "open",
        "posted_date": "2026-07-01",
    },
]

# (candidate_name, applied_to_job_title, years_experience, stage). "stage"
# matches kCandidatePipeline's kanban column keys, lowercased.
CANDIDATES: list[tuple[str, str, float, str]] = [
    ("Vikram Rathore", "Senior Backend Engineer", 6, "applied"),
    ("Ishita Bose", "QA Automation Engineer", 3, "applied"),
    ("Farhan Ali", "HR Business Partner", 5, "applied"),
    ("Ananya Gupta", "Product Marketing Manager", 4, "screened"),
    ("Devendra Singh", "Senior Backend Engineer", 7, "screened"),
    ("Priyanka Joshi", "QA Automation Engineer", 4, "interview"),
    ("Rahul Chawla", "Senior Backend Engineer", 5, "interview"),
    ("Simran Kaur", "Product Marketing Manager", 6, "offer"),
    ("Tanvi Deshpande", "QA Automation Engineer", 3, "hired"),
]

# (candidate_name, job_title, round_no, scheduled_at ISO, result). Seeded for
# API completeness only -- not hydrated into the frontend (see Interview
# model docstring).
INTERVIEWS: list[dict] = [
    {
        "candidate": "Rahul Chawla",
        "job_title": "Senior Backend Engineer",
        "round_no": 2,
        "scheduled_at": "2026-07-11T15:00:00+05:30",
        "result": None,
    },
    {
        "candidate": "Priyanka Joshi",
        "job_title": "QA Automation Engineer",
        "round_no": 1,
        "scheduled_at": "2026-07-10T11:00:00+05:30",
        "result": None,
    },
    {
        "candidate": "Devendra Singh",
        "job_title": "Senior Backend Engineer",
        "round_no": 1,
        "scheduled_at": "2026-07-09T17:00:00+05:30",
        "result": None,
    },
]

# (candidate_name, job_title, offered_ctc, offer_date, proposed_joining_date, status)
OFFERS: list[dict] = [
    {
        "candidate": "Simran Kaur",
        "job_title": "Product Marketing Manager",
        "offered_ctc": 2400000,
        "offer_date": "2026-07-08",
        "proposed_joining_date": "2026-08-03",
        "status": "sent",
    },
    {
        "candidate": "Tanvi Deshpande",
        "job_title": "QA Automation Engineer",
        "offered_ctc": 1150000,
        "offer_date": "2026-07-05",
        "proposed_joining_date": "2026-07-20",
        "status": "accepted",
    },
]
