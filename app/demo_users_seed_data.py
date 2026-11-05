"""Ported 1:1 from assets/data/dummy_credentials.json (the Flutter
frontend's demo login list) -- one login per built-in role, seeded as real
core.users/core.employees rows so the frontend can authenticate against the
backend instead of the bundled JSON. (email, password, role name) tuples;
role name must match a name in rbac_columns.BUILTIN_ROLES.
"""

DEMO_USERS: list[tuple[str, str, str]] = [
    ("owner@impacgo.com", "Owner@123", "Organization Owner / CEO"),
    ("clevel@impacgo.com", "Clevel@123", "C-Level Executive"),
    ("vpdirector@impacgo.com", "VpDir@123", "VP / Director"),
    ("gm@impacgo.com", "GenMgr@123", "General Manager / Sr. Manager"),
    ("manager@impacgo.com", "Manager@123", "Manager"),
    ("teamlead@impacgo.com", "TeamLead@123", "Team Lead"),
    ("hrstaff@impacgo.com", "HrStaff@123", "HR / Recruitment Staff"),
    ("financestaff@impacgo.com", "Finance@123", "Finance / Payroll Staff"),
    ("itadmin@impacgo.com", "ItAdmin@123", "IT / System Admin"),
    ("employee@impacgo.com", "Employee@123", "Professional / IC Employee"),
    ("intern@impacgo.com", "Intern@123", "Associate / Intern"),
]
