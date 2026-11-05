"""Ported 1:1 from lib/data/seed/work_seed.dart (kProjects). Task board
cards (kTasksBoard) aren't seeded -- see TaskBoardCard model docstring.
"""

# (name, client_name, project_type, pm_name, status, start_date, end_date, budget_display, progress_pct)
PROJECTS: list[dict] = [
    {
        "name": "Mobile App Redesign",
        "client_name": "Internal Product",
        "project_type": "Product",
        "pm_name": "Meera Krishnan",
        "status": "in_progress",
        "start_date": "2026-02-01",
        "end_date": "2026-09-30",
        "budget": "₹1.2Cr",
        "progress_pct": 64,
    },
    {
        "name": "Payments Gateway 2.0",
        "client_name": "FinEdge Corp",
        "project_type": "Client — Fixed Bid",
        "pm_name": "Amit Verma",
        "status": "in_progress",
        "start_date": "2026-04-10",
        "end_date": "2026-08-15",
        "budget": "₹86L",
        "progress_pct": 41,
    },
    {
        "name": "Client Onboarding Portal",
        "client_name": "Northwind Retail",
        "project_type": "Client — T&M",
        "pm_name": "Divya Menon",
        "status": "in_progress",
        "start_date": "2026-01-20",
        "end_date": "2026-07-31",
        "budget": "₹54L",
        "progress_pct": 82,
    },
    {
        "name": "Internal Analytics Suite",
        "client_name": "Internal Product",
        "project_type": "Product",
        "pm_name": "Aditya Rao",
        "status": "planning",
        "start_date": "2026-08-01",
        "end_date": "2027-01-31",
        "budget": "₹95L",
        "progress_pct": 6,
    },
    {
        "name": "Platform Hardening",
        "client_name": "Internal — Security",
        "project_type": "Internal",
        "pm_name": "Arjun Verma",
        "status": "completed",
        "start_date": "2025-11-01",
        "end_date": "2026-03-01",
        "budget": "₹22L",
        "progress_pct": 100,
    },
]
