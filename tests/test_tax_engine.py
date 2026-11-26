"""M-30: statutory TDS arithmetic (app/tax_engine.py) against known values.

    cd backend && venv/Scripts/python -m unittest tests.test_tax_engine -v
"""

import unittest

from app import tax_engine as te


def tax(regime, gross, **kw):
    return te.compute_annual_tax(te.TaxInputs(regime=regime, gross_salary=gross, **kw)).total_tax


class NewRegimeTests(unittest.TestCase):
    def test_nil_up_to_12_75_lakh_salary(self):
        # 12,75,000 - 75,000 std = 12L taxable: slab tax 60,000, fully rebated.
        self.assertEqual(tax("new", 1_275_000), 0)

    def test_marginal_relief_just_above_12_lakh(self):
        # Taxable 12,25,000: slab tax 63,750 > 25,000 income above 12L -> 25,000 + 4% cess.
        self.assertEqual(tax("new", 1_300_000), 26_000)

    def test_16_lakh_taxable(self):
        # 20,000 + 40,000 + 60,000 = 1,20,000 + 4,800 cess.
        self.assertEqual(tax("new", 1_675_000), 124_800)

    def test_24_lakh_taxable(self):
        self.assertEqual(tax("new", 2_475_000), 312_000)

    def test_surcharge_above_50_lakh(self):
        # Taxable 60L: 3,00,000 + 36L x 30% = 13,80,000; surcharge 10%; cess 4%.
        self.assertEqual(tax("new", 6_075_000), 1_578_720)

    def test_surcharge_marginal_relief(self):
        # Taxable 50,10,000: tax 10,83,000; at 50L 10,80,000 + 10,000 excess
        # = 10,90,000 cap -> surcharge 7,000 instead of 1,08,300.
        self.assertEqual(tax("new", 5_085_000), 1_133_600)

    def test_declarations_ignored_in_new_regime(self):
        self.assertEqual(tax("new", 1_675_000, section_80c=150_000, hra_exemption=100_000), 124_800)


class OldRegimeTests(unittest.TestCase):
    def test_rebate_up_to_5_lakh(self):
        self.assertEqual(tax("old", 550_000), 0)

    def test_with_80c(self):
        # 10,50,000 - 50,000 std - 1,50,000 80C = 8,50,000: 12,500 + 70,000 = 82,500 + 3,300 cess.
        self.assertEqual(tax("old", 1_050_000, section_80c=150_000), 85_800)

    def test_15_lakh_taxable(self):
        self.assertEqual(tax("old", 1_550_000), 273_000)

    def test_caps_are_applied(self):
        capped = tax("old", 2_000_000, section_80c=150_000, section_80d=100_000,
                     section_80ccd_1b=50_000, home_loan_interest=200_000, professional_tax=2_500)
        over = tax("old", 2_000_000, section_80c=900_000, section_80d=900_000,
                   section_80ccd_1b=900_000, home_loan_interest=900_000, professional_tax=9_000)
        self.assertEqual(capped, over)

    def test_negative_declarations_never_raise_tax(self):
        self.assertEqual(tax("old", 1_550_000, section_80c=-500_000, hra_exemption=-1), 273_000)

    def test_hra_exemption_limits(self):
        self.assertEqual(te.hra_exemption(500_000, 240_000, 600_000), 240_000)
        self.assertEqual(te.hra_exemption(500_000, 400_000, 600_000), 300_000)
        self.assertEqual(te.hra_exemption(-5, 400_000, 600_000), 0)


class MonthlySpreadTests(unittest.TestCase):
    def test_spread_over_remaining_months(self):
        self.assertEqual(te.monthly_tds(120_000, 0, 12), 10_000)
        self.assertEqual(te.monthly_tds(120_000, 60_000, 6), 10_000)
        self.assertEqual(te.monthly_tds(120_000, 130_000, 3), 0)

    def test_regime_default_is_new(self):
        self.assertEqual(te.normalise_regime(None), "new")
        self.assertEqual(te.normalise_regime("OLD"), "old")


if __name__ == "__main__":
    unittest.main()
