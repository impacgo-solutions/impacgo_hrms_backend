"""Which salary components the payroll engine adds by itself.

These are never part of a salary structure: a structure line is paid every
month to everyone on the structure, while these are added only when there
is something real to pay or recover for that employee in that run --
approved overtime (app/overtime.py), approved travel / expense
reimbursements (Reimbursement Components), LOP, asset recovery, TDS
estimate. Used by the structure save validation and shown in the editor.
"""

from __future__ import annotations

import re

SYSTEM_CODES = {
    "OT-PAY": "Overtime Pay is added automatically from approved overtime (Overtime Settings).",
    "LOP-DED": "Loss of Pay is calculated automatically from approved LOP leave (Deductions Settings).",
    "ASSET-DED": "Asset recovery is deducted automatically from approved recoveries (Deductions Settings).",
    "TDS-EST": "Statutory TDS is calculated automatically when auto-TDS is on.",
    "ARREARS": "Arrears are added automatically for back-dated changes to locked payroll periods.",
    "ARR-REC": "Arrears recovery is added automatically for back-dated changes to locked payroll periods.",
    "LOAN-EMI": "Loan EMIs are recovered automatically from active loans.",
    "DED-CF": "Deductions that didn't fit in a previous month's pay are carried forward automatically.",
}

_OVERTIME = re.compile(r"over\s*-?\s*time|\bot\b", re.I)
_REIMBURSEMENT = re.compile(r"reimburs|expense", re.I)


def validate_structure_lines(db, lines: list[dict]) -> None:
    """M-35: the percent-of-CTC earning lines of one structure may add up
    to at most 100% of CTC. Raises ValueError (router -> 409)."""
    from . import models

    total = 0.0
    for line in lines:
        if line.get("percent_of") != "ctc" or line.get("percent") is None:
            continue
        component = db.get(models.SalaryComponent, line["component_id"])
        if component is not None and component.component_type == "earning":
            total += float(line["percent"])
    if total > 100.0001:
        raise ValueError(f"Percent-of-CTC earnings add up to {total:g}% -- they can't exceed 100% of CTC.")


def automatic_reason(name: str | None, code: str | None) -> str | None:
    """Why a component cannot be put in a salary structure, or None."""
    code_u = (code or "").strip().upper()
    if code_u in SYSTEM_CODES:
        return SYSTEM_CODES[code_u]
    text = f"{name or ''} {code or ''}"
    if _OVERTIME.search(text):
        return ("Overtime is paid automatically, only for approved overtime and only when Overtime Settings "
                "is set to Payable -- it is not part of a salary structure.")
    if _REIMBURSEMENT.search(text):
        return ("Travel and expense reimbursements are paid automatically from approved requests "
                "(Reimbursement Components) -- they are not part of a salary structure.")
    return None
