"""Ported 1:1 from lib/data/seed/org_seed.dart (kBranches, kDepartments,
kBusinessUnits) so the backend can seed the same demo organization structure
the frontend has always shown, and the Organization tab reads real matching
data instead of a coincidentally-identical local copy.
"""

BRANCHES: list[dict] = [
    {"code": "BLR-HQ", "name": "Bengaluru HQ", "country": "India", "state": "Karnataka", "city": "Bengaluru", "tax": "29AACCB1234F1Z5"},
    {"code": "PNE-DC", "name": "Pune Delivery Center", "country": "India", "state": "Maharashtra", "city": "Pune", "tax": "27AACCB1234F1Z2"},
    {"code": "GGN-OFC", "name": "Gurugram Office", "country": "India", "state": "Haryana", "city": "Gurugram", "tax": "06AACCB1234F1Z9"},
    {"code": "AUS-OFC", "name": "Austin Office", "country": "USA", "state": "Texas", "city": "Austin", "tax": "EIN 84-3299110"},
    {"code": "REMOTE", "name": "Remote / Virtual", "country": "Global", "state": None, "city": None, "tax": None},
]

# name -> business unit name, ported from Department.parent in org_seed.dart
BUSINESS_UNITS: list[dict] = [
    {"name": "Products Business Unit", "cost_center": "CC-100"},
    {"name": "Revenue Business Unit", "cost_center": "CC-200"},
    {"name": "Corporate Services", "cost_center": "CC-300"},
]

DEPARTMENTS: list[dict] = [
    {
        "code": "ENG", "name": "Engineering", "business_unit_name": "Products Business Unit",
        "subs": ["Frontend", "Backend", "Full Stack", "Mobile", "QA", "DevOps", "Cloud", "AI/ML", "Data Engineering", "Cyber Security", "Architecture"],
    },
    {
        "code": "PRD", "name": "Product", "business_unit_name": "Products Business Unit",
        "subs": ["Product Management", "Product Design", "UX Research", "Business Analysis"],
    },
    {
        "code": "SLS", "name": "Sales", "business_unit_name": "Revenue Business Unit",
        "subs": ["Enterprise Sales", "Inside Sales", "B2B Sales", "Pre-Sales", "Business Development"],
    },
    {
        "code": "MKT", "name": "Marketing", "business_unit_name": "Revenue Business Unit",
        "subs": ["Digital Marketing", "Content Marketing", "SEO/SEM", "Branding", "Events"],
    },
    {
        "code": "CSM", "name": "Customer Success", "business_unit_name": "Revenue Business Unit",
        "subs": ["Customer Support", "Implementation", "Account Management"],
    },
    {
        "code": "HR", "name": "Human Resources", "business_unit_name": "Corporate Services",
        "subs": ["Recruitment", "HR Operations", "Payroll", "L&D", "Compliance"],
    },
    {
        "code": "FIN", "name": "Finance", "business_unit_name": "Corporate Services",
        "subs": ["Accounting", "Accounts Payable", "Treasury", "Taxation", "Audit"],
    },
    {
        "code": "LEG", "name": "Legal", "business_unit_name": "Corporate Services",
        "subs": ["Corporate Legal", "Contracts", "Compliance"],
    },
    {
        "code": "IT", "name": "IT", "business_unit_name": "Corporate Services",
        "subs": ["Infrastructure", "Help Desk", "IAM", "IT Security"],
    },
    {
        "code": "OPS", "name": "Operations", "business_unit_name": "Corporate Services",
        "subs": ["Business Operations", "Program Management", "Facilities", "Vendor Management"],
    },
]
