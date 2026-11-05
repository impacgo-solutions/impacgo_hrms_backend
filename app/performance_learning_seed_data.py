"""Ported 1:1 from lib/data/seed/performance_seed.dart (kOkrs,
kReviewCycles) and lib/data/seed/learning_seed.dart (kCourses).
"""

# (level, title, owner_name, progress_pct)
OKRS: list[tuple[str, str, str, int]] = [
    ("Company", "Grow ARR to $18M this fiscal year", "CEO Office", 58),
    ("Department", "Ship Mobile App v3 with < 1% crash rate", "Engineering", 64),
    ("Department", "Improve NPS from 42 to 55", "Customer Success", 37),
    ("Individual", "Complete Payments Gateway 2.0 migration", "Amit Verma", 41),
    ("Individual", "Reduce onboarding time-to-hire to 21 days", "Priya Sharma", 70),
]

# (name, from_date, to_date, status, participant_count)
REVIEW_CYCLES: list[dict] = [
    {
        "name": "Q1 FY26 Performance Review",
        "from_date": "2026-01-01",
        "to_date": "2026-04-15",
        "status": "completed",
        "participant_count": 256,
    },
    {
        "name": "Q2 FY26 Performance Review",
        "from_date": "2026-04-01",
        "to_date": "2026-07-15",
        "status": "in_progress",
        "participant_count": 256,
    },
    {
        "name": "Annual Review FY25-26",
        "from_date": "2025-04-01",
        "to_date": "2026-09-30",
        "status": "scheduled",
        "participant_count": 256,
    },
]

# (title, course_type, category, duration_hours, duration_label)
COURSES: list[tuple[str, str, str, float, str]] = [
    ("AWS Solutions Architect — Associate", "external", "Cloud", 40, "40 hrs"),
    ("Effective People Management", "internal", "Leadership", 12, "12 hrs"),
    ("Advanced React Patterns", "internal", "Engineering", 16, "16 hrs"),
    ("POSH & Workplace Compliance", "mandatory", "Compliance", 2, "2 hrs"),
    ("Financial Modelling Basics", "external", "Finance", 20, "20 hrs"),
]
