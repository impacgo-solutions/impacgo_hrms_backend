"""Statutory salary TDS (India, Sec. 192) -- old and new regime (M-30).

Pure functions, no DB access, so the arithmetic is unit-testable
(tests/test_tax_engine.py). Rates are the FY 2025-26 rules (Finance Act
2025), which also apply to later years until changed:

New regime (Sec. 115BAC, the default when the employee has not opted out):
  slabs 0-4L nil, 4-8L 5%, 8-12L 10%, 12-16L 15%, 16-20L 20%, 20-24L 25%,
  >24L 30%; standard deduction 75,000; Sec. 87A rebate up to 60,000 when
  taxable income <= 12L, with marginal relief just above 12L; no other
  Chapter VI-A deductions / HRA exemption.
Old regime:
  slabs 0-2.5L nil, 2.5-5L 5%, 5-10L 20%, >10L 30%; standard deduction
  50,000; professional tax (<= 2,500); HRA exemption; 80C (<= 1.5L, incl.
  employee PF), 80D (<= 1L), 80CCD(1B) (<= 50,000), 24(b) home-loan
  interest (<= 2L); Sec. 87A rebate up to 12,500 when taxable <= 5L.
Both: surcharge 10% > 50L, 15% > 1Cr, 25% > 2Cr, 37% > 5Cr (new regime
capped at 25%) with marginal relief; health & education cess 4%.

Monthly TDS = (projected annual tax - TDS already deducted this FY) spread
evenly over the months left in the FY including the current one.
"""

from __future__ import annotations

from dataclasses import dataclass

NEW_REGIME = "new"
OLD_REGIME = "old"

NEW_SLABS = [
    (400_000, 0.0), (800_000, 0.05), (1_200_000, 0.10), (1_600_000, 0.15),
    (2_000_000, 0.20), (2_400_000, 0.25), (float("inf"), 0.30),
]
OLD_SLABS = [(250_000, 0.0), (500_000, 0.05), (1_000_000, 0.20), (float("inf"), 0.30)]

STD_DEDUCTION = {NEW_REGIME: 75_000.0, OLD_REGIME: 50_000.0}
REBATE_LIMIT = {NEW_REGIME: 1_200_000.0, OLD_REGIME: 500_000.0}
REBATE_MAX = {NEW_REGIME: 60_000.0, OLD_REGIME: 12_500.0}
CESS_RATE = 0.04

# Statutory caps on declarations (M-28) -- also enforced by
# schemas.TaxDeclarationCreate.
CAP_80C = 150_000.0
CAP_80D = 100_000.0
CAP_80CCD_1B = 50_000.0
CAP_HOME_LOAN_INTEREST = 200_000.0
CAP_PROFESSIONAL_TAX = 2_500.0

_SURCHARGE = [(50_000_000, 0.37), (20_000_000, 0.25), (10_000_000, 0.15), (5_000_000, 0.10)]


@dataclass
class TaxInputs:
    regime: str
    gross_salary: float  # annual taxable salary (projected)
    hra_exemption: float = 0.0
    section_80c: float = 0.0  # incl. employee PF
    section_80d: float = 0.0
    section_80ccd_1b: float = 0.0
    home_loan_interest: float = 0.0
    professional_tax: float = 0.0


@dataclass
class TaxResult:
    regime: str
    taxable_income: float
    slab_tax: float
    rebate: float
    surcharge: float
    cess: float
    total_tax: float


def normalise_regime(value: str | None) -> str:
    return OLD_REGIME if (value or "").strip().lower() == OLD_REGIME else NEW_REGIME


def slab_tax(income: float, slabs) -> float:
    tax, lower = 0.0, 0.0
    for upper, rate in slabs:
        if income <= lower:
            break
        tax += (min(income, upper) - lower) * rate
        lower = upper
    return tax


def taxable_income(inputs: TaxInputs) -> float:
    regime = normalise_regime(inputs.regime)
    gross = max(0.0, float(inputs.gross_salary or 0))
    if regime == NEW_REGIME:
        return max(0.0, gross - STD_DEDUCTION[NEW_REGIME])
    deductions = (
        STD_DEDUCTION[OLD_REGIME]
        + min(max(0.0, inputs.professional_tax), CAP_PROFESSIONAL_TAX)
        + max(0.0, inputs.hra_exemption)
        + min(max(0.0, inputs.section_80c), CAP_80C)
        + min(max(0.0, inputs.section_80d), CAP_80D)
        + min(max(0.0, inputs.section_80ccd_1b), CAP_80CCD_1B)
        + min(max(0.0, inputs.home_loan_interest), CAP_HOME_LOAN_INTEREST)
    )
    return max(0.0, gross - deductions)


def _tax_after_rebate(income: float, regime: str) -> tuple[float, float]:
    slabs = NEW_SLABS if regime == NEW_REGIME else OLD_SLABS
    tax = slab_tax(income, slabs)
    rebate = 0.0
    limit = REBATE_LIMIT[regime]
    if income <= limit:
        rebate = min(tax, REBATE_MAX[regime])
    elif regime == NEW_REGIME and tax > income - limit:
        # Marginal relief: tax may not exceed the income above 12L.
        rebate = tax - (income - limit)
    return tax, rebate


def compute_annual_tax(inputs: TaxInputs) -> TaxResult:
    regime = normalise_regime(inputs.regime)
    income = round(taxable_income(inputs))
    tax, rebate = _tax_after_rebate(income, regime)
    base = tax - rebate
    surcharge = 0.0
    for threshold, rate in _SURCHARGE:
        if income > threshold:
            if regime == NEW_REGIME:
                rate = min(rate, 0.25)
            surcharge = base * rate
            # Marginal relief: tax + surcharge may not exceed the tax (with
            # the lower surcharge) at the threshold plus the income above it.
            lower_rate = next((r for t, r in _SURCHARGE if t < threshold), 0.0)
            t_tax, t_reb = _tax_after_rebate(threshold, regime)
            at_threshold = (t_tax - t_reb) * (1 + lower_rate)
            cap = at_threshold + (income - threshold)
            if base + surcharge > cap:
                surcharge = max(0.0, cap - base)
            break
    cess = (base + surcharge) * CESS_RATE
    total = round(base + surcharge + cess)
    return TaxResult(regime, float(income), round(tax, 2), round(rebate, 2),
                     round(surcharge, 2), round(cess, 2), float(total))


def monthly_tds(annual_tax: float, tds_already_deducted: float, months_remaining: int) -> float:
    """This month's TDS: the tax still due spread over the months left in
    the FY (including this one). Never negative (excess is not refunded
    through payroll)."""
    months = max(1, int(months_remaining))
    return max(0.0, round((float(annual_tax) - float(tds_already_deducted)) / months))


def hra_exemption(claimed: float, hra_received: float, basic_annual: float) -> float:
    """Old regime HRA exemption honoured from the declaration: the claimed
    amount, never more than the HRA actually received nor 50% of Basic (the
    statutory metro ceiling of Sec. 10(13A))."""
    return max(0.0, min(float(claimed or 0), float(hra_received or 0), 0.5 * float(basic_annual or 0)))
