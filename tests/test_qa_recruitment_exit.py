"""QA fixes -- Recruitment & other HR modules (M-38, M-40, L-26..L-30).

    cd backend && venv/Scripts/python -m unittest tests.test_qa_recruitment_exit -v

Real routes against the dev DB (tenant acme) in an always-rolled-back
transaction.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402

from app import crud, schemas  # noqa: E402
from tests.test_qa_employees_org import IC, _QABase  # noqa: E402


class RecruitmentInput(_QABase):
    def test_m40_empty_requisition_and_candidate(self):
        self.assertEqual(self.client.post("/api/recruitment/requisitions", json={}).status_code, 422)
        self.assertEqual(self.client.post("/api/recruitment/candidates", json={}).status_code, 422)
        self.assertEqual(self.client.post("/api/recruitment/candidates", json={"name": "  "}).status_code, 422)

    def test_m38_malformed_values_are_422_not_500(self):
        cases = [
            ("put", "/api/recruitment/settings", {"offer_expiry_days": "abc"}),
            ("post", "/api/recruitment/requisitions", {"designation_title": "QA Dev", "positions_count": "many"}),
            ("post", "/api/recruitment/requisitions", {"designation_title": "QA Dev", "requested_by": "nope"}),
            ("post", "/api/recruitment/candidates", {"name": "QA Cand", "years_experience": "ten"}),
        ]
        for method, path, body in cases:
            r = getattr(self.client, method)(path, json=body)
            self.assertEqual(r.status_code, 422, f"{path} {body} -> {r.status_code} {r.text[:200]}")

    def test_l26_permission_checked_before_state(self):
        r = self.client.post("/api/recruitment/requisitions", json={"designation_title": "QA Opening"})
        self.assertEqual(r.status_code, 201, r.text)
        req_id = r.json()["id"]
        self.as_(self.login(self.new_employee(), IC))
        resp = self.client.post(f"/api/recruitment/requisitions/{req_id}/create-opening")
        self.assertEqual(resp.status_code, 403, resp.text)


class OtherModules(_QABase):
    def test_l27_negative_counts(self):
        with self.assertRaises(ValidationError):
            schemas.ReviewCycleCreate(name="Q", from_date="2026-01-01", to_date="2026-03-01", participant_count=-1)
        with self.assertRaises(ValidationError):
            schemas.TrainingSessionCreate(title="T", session_date="2026-01-01", attendee_count=-3)

    def test_l28_certification_expiry(self):
        with self.assertRaises(ValidationError):
            schemas.CertificationCreate(employee_id=self.owner.employee_id, name="C",
                                        issue_date="2026-05-01", expiry_date="2026-04-01")

    def test_l29_course_duration(self):
        for bad in ("-5h", "0 hrs", "abc"):
            with self.assertRaises(ValidationError, msg=bad):
                schemas.CourseCreate(name="C", type="online", duration=bad, category="Tech")
        self.assertEqual(schemas.CourseCreate(name="C", type="online", duration="16 hrs", category="T").duration,
                         "16 hrs")

    def test_l30_skill_ratings_scoped(self):
        manager = self.new_employee()
        report = self.new_employee(reporting_manager_id=manager.id)
        outsider = self.new_employee()
        crud.upsert_skill_rating(self.db, report.id, "Python", "Advanced")
        crud.upsert_skill_rating(self.db, outsider.id, "Python", "Expert")
        self.db.flush()

        def seen():
            rows = self.client.get("/api/skill-ratings", params={"limit": 500})
            self.assertEqual(rows.status_code, 200, rows.text)
            return {r["employee_id"] for r in rows.json()}

        self.as_(self.login(manager, IC))
        ids = seen()
        self.assertIn(str(report.id), ids)
        self.assertNotIn(str(outsider.id), ids)
        self.as_(self.login(outsider, IC))
        self.assertEqual(seen(), {str(outsider.id)})
        self.as_(self.owner)
        self.assertTrue({str(report.id), str(outsider.id)} <= seen())


if __name__ == "__main__":
    unittest.main()
