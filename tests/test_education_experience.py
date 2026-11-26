"""Employee Details > Education & Experience: several education rows, several
previous-employment rows, the level / experience-range dropdowns, validation,
persistence, and the employee lookup (Employee ID, Name, or both).

    cd backend && venv/Scripts/python -m unittest tests.test_education_experience -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant impacgo-solutions by default, EDU_TEST_TENANT to override).
Needs backend/db/add_employee_education_experience_records.sql applied.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas  # noqa: E402
from app.routers import employees as employees_api  # noqa: E402

TENANT = os.environ.get("EDU_TEST_TENANT", "impacgo-solutions")
TODAY = datetime.date.today()


def _edu(level="Bachelors", **kw):
    return {"level": level, "qualification": "B.Tech", "institute": "JNTU", "year_of_passing": 2018, **kw}


def _exp(company="Acme Corp", **kw):
    return {"company_name": company, "domain": "Information Technology", "job_role": "Analyst",
            "start_date": datetime.date(2019, 1, 1), "end_date": datetime.date(2020, 12, 31), **kw}


class EducationExperienceTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.emp = self.db.scalars(
            select(models.Employee).where(models.Employee.is_active.is_(True)).order_by(models.Employee.first_name)
        ).first()
        if self.emp is None:
            self.skipTest("no employee")
        self.company_id = self.emp.company_id

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def _save(self, **payload):
        updates = schemas.EmployeeEducationUpdate(**payload).model_dump(exclude_unset=True)
        crud.update_employee_education(self.db, self.emp.id, updates)
        self.db.flush()
        return crud.get_employee_education(self.db, self.emp.id)

    # ── options + validation ────────────────────────────────────────────────
    def test_options_served_by_the_api(self):
        opts = employees_api.education_options(current_user=None)
        self.assertEqual([o["value"] for o in opts["educationLevels"]],
                         ["10th", "Intermediate", "Diploma", "Bachelors", "Masters", "PhD", "Other"])
        self.assertIn("3-5", [o["value"] for o in opts["experienceRanges"]])
        self.assertIn("Information Technology", opts["domains"])

    def test_validation_rejects_bad_input(self):
        bad = [
            dict(education_records=[_edu(level="Kindergarten")]),
            dict(education_records=[_edu(year_of_passing=1900)]),
            dict(education_records=[_edu(year_of_passing=TODAY.year + 20)]),
            dict(experience_records=[_exp(company="   ")]),
            dict(experience_records=[_exp(end_date=datetime.date(2018, 1, 1))]),
            dict(experience_records=[_exp(start_date=TODAY + datetime.timedelta(days=5), end_date=None)]),
            dict(experience_records=[_exp(end_date=TODAY + datetime.timedelta(days=5))]),
            dict(experience_records=[_exp(start_date=None, end_date=datetime.date(2020, 1, 1))]),
            dict(experience_records=[_exp(years_experience=75)]),
            dict(experience_records=[_exp(years_experience=-1)]),
            dict(total_experience_range="40-50"),
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                schemas.EmployeeEducationUpdate(**payload)

    def test_current_job_drops_the_end_date_and_blank_text_becomes_null(self):
        rec = schemas.ExperienceRecordIn(**_exp(is_current=True, domain="  ", job_role=""))
        self.assertIsNone(rec.end_date)
        self.assertIsNone(rec.domain)
        self.assertIsNone(rec.job_role)

    # ── persistence ─────────────────────────────────────────────────────────
    def test_multiple_education_and_employment_rows_round_trip(self):
        out = self._save(
            education_records=[
                _edu("10th", qualification="SSC", institute="ZP School", year_of_passing=2012, class_percentage="9.2 CGPA"),
                _edu("Intermediate", qualification="MPC", year_of_passing=2014),
                _edu("Bachelors"),
                _edu("Masters", qualification="MBA", year_of_passing=2020, class_percentage="First Class"),
            ],
            experience_records=[
                _exp("Acme Corp"),
                _exp("Globex", domain="Banking & Finance", job_role="Senior Analyst",
                     start_date=datetime.date(2021, 2, 1), end_date=None, is_current=True),
                _exp("Initech", years_experience=1.5, start_date=None, end_date=None),
            ],
            total_experience_range="5-8",
        )
        self.assertEqual([r["level"] for r in out["educationRecords"]], ["10th", "Intermediate", "Bachelors", "Masters"])
        self.assertEqual(out["educationRecords"][3]["levelLabel"], "Master's")
        self.assertEqual(out["educationRecords"][0]["classPercentage"], "9.2 CGPA")
        self.assertEqual(out["qualification"], "MBA")  # highest level drives the summary line
        names = {r["companyName"] for r in out["experienceRecords"]}
        self.assertEqual(names, {"Acme Corp", "Globex", "Initech"})
        acme = next(r for r in out["experienceRecords"] if r["companyName"] == "Acme Corp")
        self.assertEqual(acme["yearsExperience"], 2.0)  # 1 Jan 2019 - 31 Dec 2020, from the dates
        self.assertEqual(acme["jobRole"], "Analyst")
        globex = next(r for r in out["experienceRecords"] if r["companyName"] == "Globex")
        self.assertTrue(globex["isCurrent"])
        self.assertIsNone(globex["endDate"])
        self.assertEqual(next(r for r in out["experienceRecords"] if r["companyName"] == "Initech")["yearsExperience"], 1.5)
        self.assertEqual(out["totalExperienceRange"], "5-8")
        self.assertEqual(out["totalExperienceRangeLabel"], "5 – 8 years")
        self.assertGreater(out["totalExperienceYears"], 3.5)
        self.assertIn("Globex", out["previousEmployers"])

        # Fresh read from the database, not the cached session objects.
        self.db.expire_all()
        again = crud.get_employee_education(self.db, self.emp.id)
        self.assertEqual(len(again["educationRecords"]), 4)
        self.assertEqual(len(again["experienceRecords"]), 3)

    def test_saving_replaces_rows_and_omitted_lists_are_untouched(self):
        self._save(education_records=[_edu("Diploma"), _edu("Bachelors")], experience_records=[_exp("A"), _exp("B")])
        out = self._save(education_records=[_edu("PhD", qualification="PhD CS")])
        self.assertEqual([r["level"] for r in out["educationRecords"]], ["PhD"])
        self.assertEqual(len(out["experienceRecords"]), 2)  # experience wasn't sent
        out = self._save(experience_records=[])
        self.assertEqual(out["experienceRecords"], [])
        self.assertEqual(len(out["educationRecords"]), 1)

    def test_total_experience_can_be_changed_and_cleared(self):
        self.assertEqual(self._save(total_experience_range="fresher")["totalExperienceRange"], "fresher")
        self.assertEqual(self._save(total_experience_range="15+")["totalExperienceRange"], "15+")
        self.assertEqual(self._save(total_experience_range=None)["totalExperienceRange"], "")

    def test_older_single_record_clients_still_work_with_many_rows(self):
        self._save(education_records=[_edu("Diploma"), _edu("Masters", qualification="MCA")],
                   experience_records=[_exp("First"), _exp("Second")])
        out = self._save(qualification="Updated Degree", previous_employer="Updated Co", experience_years="3 years")
        self.assertEqual(len(out["educationRecords"]), 2)  # no MultipleResultsFound, nothing duplicated
        self.assertEqual(len(out["experienceRecords"]), 2)
        self.assertIn("Updated Degree", [r["qualification"] for r in out["educationRecords"]])

    def test_duration_label_matches_the_work_history_table(self):
        self.assertEqual(crud.format_experience_years(1.75), "1 yr 9 months")
        self.assertEqual(crud.format_experience_years(2), "2 yrs")
        self.assertEqual(crud.format_experience_years(0.5), "6 months")
        self.assertEqual(crud.format_experience_years(None), "—")
        self.assertEqual(crud.experience_years_between(datetime.date(2019, 1, 1), datetime.date(2020, 12, 31)), 2.0)

    # ── employee lookup: ID, name, both ─────────────────────────────────────
    def _list(self, **kw):
        page = crud.list_employees_paginated(self.db, self.company_id, limit=200, **kw)
        return page["items"]

    def test_lookup_by_id_by_name_and_by_both(self):
        code, first = self.emp.employee_code, self.emp.first_name
        by_id = self._list(employee_code=code)
        self.assertIn(str(self.emp.id), {str(i["id"]) for i in by_id})
        self.assertTrue(all(code.lower() in i["code"].lower() for i in by_id))

        by_name = self._list(name=first)
        self.assertIn(str(self.emp.id), {str(i["id"]) for i in by_name})

        both = self._list(employee_code=code, name=first)
        self.assertIn(str(self.emp.id), {str(i["id"]) for i in both})
        self.assertLessEqual(len(both), len(by_id))
        self.assertLessEqual(len(both), len(by_name))

    def test_lookup_both_must_match_and_is_case_insensitive(self):
        code, first = self.emp.employee_code, self.emp.first_name
        self.assertEqual(self._list(employee_code=code, name="Zzzzqqq"), [])
        self.assertEqual(self._list(employee_code="NOPE-0000-X", name=first), [])
        self.assertIn(str(self.emp.id), {str(i["id"]) for i in self._list(employee_code=code.lower(), name=first.upper())})

    def test_lookup_name_words_in_any_order_and_wildcards_are_literal(self):
        if not self.emp.last_name:
            self.skipTest("employee has no last name")
        reversed_name = f"{self.emp.last_name} {self.emp.first_name}"
        self.assertIn(str(self.emp.id), {str(i["id"]) for i in self._list(name=reversed_name)})
        self.assertEqual(self._list(name="%"), [])  # % is a literal character, not "everything"
        self.assertEqual(self._list(employee_code="_"), [] if "_" not in self.emp.employee_code else self._list(employee_code="_"))

    def test_existing_free_search_is_unchanged(self):
        self.assertIn(str(self.emp.id), {str(i["id"]) for i in self._list(search=self.emp.employee_code)})
        self.assertIn(str(self.emp.id), {str(i["id"]) for i in self._list(search=self.emp.first_name)})


if __name__ == "__main__":
    unittest.main()
