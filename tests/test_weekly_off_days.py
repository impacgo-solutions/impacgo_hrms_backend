"""General Settings > Weekly Off Days: saved company-wise and applied by
leave days, overtime / shift working-day checks, the absence job and payroll
working days. Unset = the old working_days_per_week rule, unchanged.

    cd backend && venv/Scripts/python -m unittest tests.test_weekly_off_days -v

Real routes against the dev DB inside a transaction that is ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crud, leave_policy, models, overtime, weekly_off  # noqa: E402
from tests.test_qa_employees_org import _QABase  # noqa: E402

# Mon 2 Nov 2026 .. Sun 8 Nov 2026 -- no seeded holidays are relied on.
MON = datetime.date(2026, 11, 2)
FRI, SAT, SUN = MON + datetime.timedelta(days=4), MON + datetime.timedelta(days=5), MON + datetime.timedelta(days=6)


class WeeklyOffRules(unittest.TestCase):
    def test_unset_keeps_the_old_rule(self):
        self.assertEqual(weekly_off.off_day_names(SimpleNamespace(weekly_off_days=None, working_days_per_week=5)),
                         ["saturday", "sunday"])
        self.assertEqual(weekly_off.off_day_names(SimpleNamespace(weekly_off_days=None, working_days_per_week=6)),
                         ["sunday"])
        self.assertEqual(weekly_off.off_day_names(None), ["saturday", "sunday"])

    def test_saved_choice_wins(self):
        row = SimpleNamespace(weekly_off_days="sunday,friday", working_days_per_week=5)
        self.assertEqual(weekly_off.off_day_names(row), ["friday", "sunday"])
        self.assertEqual(weekly_off.off_weekdays(row), frozenset({4, 6}))
        self.assertEqual(weekly_off.off_day_names(SimpleNamespace(weekly_off_days="", working_days_per_week=7)), [])

    def test_normalize(self):
        self.assertEqual(weekly_off.normalize(["Sunday", "SATURDAY", "sunday"]), ["saturday", "sunday"])
        with self.assertRaises(ValueError):
            weekly_off.normalize(["funday"])
        with self.assertRaises(ValueError):
            weekly_off.normalize(list(weekly_off.WEEKDAYS))


class WeeklyOffSettingsApi(_QABase):
    def setUp(self):
        super().setUp()
        # Holidays in the test week would hide the weekly-off effect.
        self.db.query(models.Holiday).filter(
            models.Holiday.company_id == self.company_id,
            models.Holiday.holiday_date.between(MON, SUN)).delete(synchronize_session=False)
        crud.upsert_company_settings(self.db, self.company_id, {"working_days_per_week": 5})
        crud.get_company_settings(self.db, self.company_id).weekly_off_days = None
        self.db.flush()

    def patch(self, **body):
        return self.client.patch("/api/company-settings", json=body)

    def test_get_reports_the_derived_days_until_saved(self):
        r = self.client.get("/api/company-settings")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["weekly_off_days"], ["saturday", "sunday"])
        self.assertEqual(r.json()["working_days_per_week"], 5)

    def test_save_sunday_only(self):
        r = self.patch(weekly_off_days=["Sunday"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["weekly_off_days"], ["sunday"])
        self.assertEqual(r.json()["working_days_per_week"], 6)
        self.assertEqual(crud.get_company_settings(self.db, self.company_id).weekly_off_days, "sunday")
        self.assertEqual(self.client.get("/api/company-settings").json()["weekly_off_days"], ["sunday"])

    def test_validation(self):
        self.assertEqual(self.patch(weekly_off_days=["Funday"]).status_code, 422)
        self.assertEqual(self.patch(weekly_off_days=list(weekly_off.WEEKDAYS)).status_code, 422)
        self.assertEqual(self.patch(weekly_off_days=["saturday", "sunday"], working_days_per_week=6).status_code, 422)
        r = self.patch(weekly_off_days=["saturday", "sunday"], working_days_per_week=5)
        self.assertEqual(r.status_code, 200, r.text)

    def test_changing_only_the_count_resets_mismatched_days(self):
        self.patch(weekly_off_days=["friday", "saturday"])
        r = self.patch(working_days_per_week=6)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["weekly_off_days"], ["sunday"])
        # A count that still fits keeps the saved days.
        self.patch(weekly_off_days=["friday"])
        r = self.patch(working_days_per_week=6, default_currency="INR")
        self.assertEqual(r.json()["weekly_off_days"], ["friday"])

    def test_applied_to_leave_overtime_and_payroll(self):
        branch_id = self.template_emp.branch_id
        week = leave_policy.working_dates(self.db, self.company_id, branch_id, MON, SUN)
        self.assertEqual(len(week), 5)
        self.assertNotIn(SAT, week)
        self.patch(weekly_off_days=["friday"])
        week = leave_policy.working_dates(self.db, self.company_id, branch_id, MON, SUN)
        self.assertEqual(len(week), 6)
        self.assertNotIn(FRI, week)
        self.assertIn(SAT, week)
        self.assertIn(SUN, week)
        self.assertEqual(overtime._working_day(self.db, self.company_id, branch_id, FRI), (False, "Weekly off"))
        self.assertTrue(overtime._working_day(self.db, self.company_id, branch_id, SUN)[0])
        working, _ = crud._compute_working_days_and_lop(
            self.db, self.company_id, self.template_emp.id, branch_id, MON, SUN)
        self.assertEqual(working, 6.0)
        # Other companies are untouched.
        other = self.db.query(models.CompanySettings).filter(
            models.CompanySettings.company_id != self.company_id).first()
        if other is not None:
            self.assertNotEqual(other.weekly_off_days, "friday")


if __name__ == "__main__":
    unittest.main()
