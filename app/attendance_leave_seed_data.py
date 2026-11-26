"""Ported 1:1 from lib/data/seed/attendance_seed.dart (kShifts) and
lib/data/seed/leave_seed.dart (kLeaveTypes, kHolidays).
"""

# (name, start_time "HH:MM", end_time "HH:MM", is_night). Rotational/Flexible
# shifts have no fixed clock times in the frontend ("Rotates weekly" / "Any
# 8 hrs / day") -- hcm.shifts' start_time/end_time are NOT NULL, so these two
# get placeholder 00:00-00:00 and the frontend keeps showing its own special-
# cased label for them (this table isn't hydrated into the Shifts tab this
# phase — see Phase 2 notes on why "assigned" counts make that unsafe to
# swap in yet).
SHIFTS: list[dict] = [
    {"name": "General Shift", "start": "09:30", "end": "18:30", "is_night": False},
    {"name": "Early Shift", "start": "06:30", "end": "15:30", "is_night": False},
    {"name": "US Overlap Shift", "start": "14:00", "end": "23:00", "is_night": True},
    {"name": "Rotational — Support", "start": "00:00", "end": "00:00", "is_night": False},
    {"name": "Flexible", "start": "00:00", "end": "00:00", "is_night": False},
]

# (name, code) -- code is UNIQUE per company, max 10 chars.
LEAVE_TYPES: list[tuple[str, str]] = [
    ("Casual Leave", "CL"),
    ("Sick Leave", "SL"),
    ("Earned / Privilege Leave", "EL"),
    ("Maternity Leave", "ML"),
    ("Paternity Leave", "PTL"),
    ("Marriage Leave", "MRG"),
    ("Bereavement Leave", "BRV"),
    ("Comp-Off", "CO"),
    ("Loss of Pay (LOP)", "LOP"),
    ("Optional / Festival Holiday", "OPT"),
    ("Sabbatical", "SAB"),
]

# (date "YYYY-MM-DD", name, region) -- "day" (weekday name) is derived from
# the date rather than stored, since it's fully determined by it.
HOLIDAYS: list[tuple[str, str, str]] = [
    ("2026-08-15", "Independence Day", "All Branches"),
    ("2026-08-27", "Ganesh Chaturthi", "Bengaluru, Pune"),
    ("2026-10-02", "Gandhi Jayanti", "All Branches"),
    ("2026-10-20", "Diwali", "All India Branches"),
    ("2026-12-25", "Christmas", "All Branches"),
    ("2026-11-26", "Thanksgiving", "Austin Office"),
]
