"""Ported 1:1 from the Salary Structure tab's component rows
(lib/screens/payroll/payroll_screen.dart _salaryStructureFull/_salaryStructureSelf)
-- a reference catalog, since no per-employee salary_structure_assignments/
salary_slips are seeded this phase (see Phase 3 notes: nothing in the
frontend reads live payroll data yet, so fabricating it would have no
payoff and risks looking like real numbers that aren't).
"""

# (code, name, component_type, calc_type, is_taxable)
SALARY_COMPONENTS: list[tuple[str, str, str, str, bool]] = [
    ("BASIC", "Basic Salary", "earning", "fixed", True),
    ("HRA", "House Rent Allowance", "earning", "fixed", True),
    ("CONV", "Travel / Conveyance Allowance", "earning", "fixed", False),
    ("MEAL", "Food / Meal Allowance", "earning", "fixed", False),
    ("SPECIAL", "Special Allowance", "earning", "fixed", True),
    ("VARIABLE", "Variable Pay / Bonus", "earning", "fixed", True),
    ("PF", "Provident Fund (PF)", "deduction", "fixed", False),
    ("PT", "Professional Tax", "deduction", "fixed", False),
    ("TDS", "Income Tax (TDS)", "deduction", "fixed", False),
]
