"""Ported 1:1 from the Flutter frontend's kDesignationBands
(lib/data/seed/org_seed.dart), minus the `reportsTo` field — that's
organizational metadata the frontend keeps locally; core.designations
(backend/db/schema.sql) only has columns for name/band/is_active.

Order matters — it's the exact band order the Organization > Designations
tab renders, and is used here to derive the display "band number" since
the table itself only stores the band name as text.
"""

DESIGNATION_BANDS: list[tuple[str, list[str]]] = [
    (
        "Ownership / Founding",
        [
            "Founder",
            "Co-Founder",
            "Chairman",
            "Vice Chairman",
            "Board Member",
            "Managing Partner",
            "Partner",
            "Managing Director (MD)",
            "Organization Owner",
        ],
    ),
    (
        "C-Level Executive",
        [
            "CEO",
            "President",
            "COO",
            "CTO",
            "CFO",
            "CIO",
            "CPO",
            "CMO",
            "CHRO",
            "CRO",
            "CSO",
            "CISO",
            "CLO",
            "CCO",
            "CDO",
            "CXO",
        ],
    ),
    (
        "VP Level",
        [
            "Executive Vice President",
            "Senior Vice President",
            "Vice President",
            "Associate Vice President",
            "Assistant Vice President",
        ],
    ),
    (
        "Director Level",
        [
            "Executive Director",
            "Senior Director",
            "Director",
            "Associate Director",
            "Deputy Director",
            "Assistant Director",
        ],
    ),
    (
        "General Management",
        [
            "Regional General Manager",
            "Senior General Manager",
            "General Manager",
            "Deputy General Manager",
            "Operations General Manager",
        ],
    ),
    (
        "Management",
        [
            "Senior Manager",
            "Manager",
            "Assistant Manager",
            "Management Trainee",
        ],
    ),
    (
        "Team Lead",
        [
            "Technical Lead",
            "Engineering Lead",
            "Project Lead",
            "Delivery Lead",
            "QA Lead",
            "Design Lead",
            "Sales Lead",
            "HR Lead",
            "Finance Lead",
            "Operations Lead",
        ],
    ),
    (
        "Professional / IC (Senior)",
        [
            "Principal Engineer",
            "Solution Architect",
            "Enterprise Architect",
            "Technical Architect",
            "Senior Consultant",
            "Senior Software Engineer",
            "Senior Business Analyst",
            "Senior UX Designer",
        ],
    ),
    (
        "Professional / IC",
        [
            "Consultant",
            "Software Engineer",
            "QA Engineer",
            "DevOps Engineer",
            "Business Analyst",
            "Product Designer",
            "Data Analyst",
            "AI Engineer",
            "Technical Writer",
        ],
    ),
    (
        "Associate / Entry Level",
        [
            "Senior Associate",
            "Associate",
            "Junior Associate",
            "Associate Software Engineer",
            "GET",
            "Apprentice",
            "Intern",
            "Contract Employee",
            "Freelancer",
        ],
    ),
]

# band name -> its 1-based position in DESIGNATION_BANDS (matches the
# "Band N — <name>" labels the frontend renders).
BAND_NUMBERS: dict[str, int] = {
    band_name: i + 1 for i, (band_name, _titles) in enumerate(DESIGNATION_BANDS)
}

# band name -> {title -> its position in that band's seed list}. Used to
# render seeded designations in the same order as the frontend's static
# list rather than alphabetically; anything not in here (e.g. a title added
# later through the API) sorts after the seeded ones.
SEED_TITLE_ORDER: dict[str, dict[str, int]] = {
    band_name: {title: i for i, title in enumerate(titles)}
    for band_name, titles in DESIGNATION_BANDS
}
