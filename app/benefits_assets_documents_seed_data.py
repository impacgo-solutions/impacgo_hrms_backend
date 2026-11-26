"""Ported 1:1 from lib/data/seed/assets_seed.dart (kAssetInventory) and
lib/data/seed/documents_seed.dart (kPolicyDocs, kDocTemplates). No custom
benefit categories are seeded -- WorkforceStore.customBenefitCategories
starts empty in the frontend too, growing only via New Benefit Plan.
"""

# (asset_tag, asset_type, model, status, value_display, purchased_on)
ASSET_INVENTORY: list[tuple[str, str, str, str, str, str]] = [
    ("AST-LT-2201", "Laptop", 'MacBook Pro 14" M3', "assigned", "₹1,89,000", "2025-01-15"),
    ("AST-LT-2214", "Laptop", "Dell Latitude 5440", "available", "₹78,000", "2025-03-02"),
    ("AST-MB-7201", "Mobile Device", "iPhone 14", "assigned", "₹65,000", "2024-11-20"),
    ("AST-SM-3301", "SIM Card", "Corporate Postpaid", "assigned", "—", "2024-11-20"),
    ("AST-ID-5201", "ID Card", "RFID Access Card", "assigned", "₹150", "2022-03-14"),
    ("AST-LI-9001", "Software License", "JetBrains All Products", "assigned", "₹28,500 / yr", "2026-01-01"),
    ("AST-LT-2299", "Laptop", "MacBook Air M2", "repair", "₹1,15,000", "2024-06-18"),
]

# (name, version, effective_date, acknowledgement_pct)
POLICY_DOCS: list[tuple[str, str, str, float]] = [
    ("Employee Handbook v4.2", "4.2", "2026-01-01", 92),
    ("Leave & Attendance Policy", "2.1", "2025-10-01", 88),
    ("Code of Conduct", "3.0", "2024-04-01", 97),
    ("Information Security Policy", "1.5", "2026-03-15", 74),
    ("POSH Policy", "2.0", "2023-01-01", 99),
]

DOC_TEMPLATES: list[str] = [
    "Offer Letter Template",
    "Appointment Letter Template",
    "NDA Template",
    "Employment Agreement Template",
    "Experience Letter Template",
    "Relieving Letter Template",
    "Full & Final Settlement Template",
]
