"""Recruitment workflow end to end, through the real HTTP API
(/api/recruitment/... and the candidate portal):

  Hiring Request -> Approval -> Job Opening -> Publish -> Candidate ->
  Apply -> Screen -> Shortlist -> Technical / Manager / HR interviews with
  mandatory feedback -> Select -> Offer -> Approve -> Send -> Accept (portal)
  -> Preboarding -> Documents (reject + resubmit) -> Verification ->
  Confirm Joining -> Create Employee (idempotent) -> People

plus the alternate paths (requisition rejected / changes requested, reject
at screening, reject after interview, hold / resume, withdraw, offer
declined / expired / withdrawn, verification failed, joining cancelled,
no-show), permission and tenant checks, and the drag-and-drop guard.

    cd backend && venv/Scripts/python -m unittest tests.test_recruitment_workflow -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant impacgo-solutions by default, RECRUITMENT_TEST_TENANT
to override); uploads go to a temporary folder and no email is sent.
"""

from __future__ import annotations

import datetime
import io
import os
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, email_service, models  # noqa: E402
from app import recruitment_onboarding as onb  # noqa: E402
from app.database import get_db  # noqa: E402
from app.deps import _OWNER_ROLE_NAME, get_current_user  # noqa: E402
from app.main import app  # noqa: E402

TENANT = os.environ.get("RECRUITMENT_TEST_TENANT", "impacgo-solutions")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class _NoCloseSession:
    """Portal requests open their own session -- hand them the test one."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


class RecruitmentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.tmp = tempfile.TemporaryDirectory()
        self.sent_offers = []
        patches = [
            mock.patch("app.storage.uploads_root", lambda: Path(self.tmp.name)),
            mock.patch.object(email_service._executor, "submit", lambda *a, **k: None),
            mock.patch("app.routers.recruitment.offer_letter_pdf", lambda db, company, detail: (PDF, "candidate")),
            mock.patch.object(email_service, "send_offer_letter", self._fake_send_offer),
            mock.patch("app.routers.candidate_portal.database.SessionLocal", lambda: _NoCloseSession(self.db)),
        ]
        for p in patches:
            p.start()
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        role = {u.id: crud.get_user_primary_role(self.db, u.id) for u in users}
        self.owner = next((u for u in users if role[u.id] and role[u.id].name == _OWNER_ROLE_NAME), None)
        self.hr = next((u for u in users if u is not self.owner
                        and crud.effective_user_matrix(self.db, u).get("recruitment") in ("e", "a")), None)
        self.plain = next((u for u in users if crud.effective_user_matrix(self.db, u).get("recruitment") in (None, "n")
                           and role[u.id] and role[u.id].name != _OWNER_ROLE_NAME), None)
        if not (self.owner and self.hr and self.plain):
            self.skipTest("tenant needs an Owner, a Recruitment editor and an employee without Recruitment access")
        self.company_id = self.owner.company_id
        self.dept = self.db.scalars(select(models.Department).where(models.Department.company_id == self.company_id)).first()
        self.branch = self.db.scalars(select(models.Branch).where(models.Branch.company_id == self.company_id)).first()
        self.role_name = next(r.name for r in self.db.scalars(select(models.Role)).all()
                              if r.name not in (_OWNER_ROLE_NAME,) and "IC" in r.name or r.name.startswith("Associate"))
        self.user = self.hr
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()
        self.tmp.cleanup()

    def _fake_send_offer(self, db, **kw):
        self.sent_offers.append(kw)
        return email_service.SendResult("SENT", None)

    # ── helpers ───────────────────────────────────────────────────────────
    def call(self, method, path, body=None, *, as_=None, expect=200, files=None):
        self.user = as_ or self.hr
        r = self.client.request(method, "/api/recruitment" + path, json=body, files=files)
        self.assertEqual(r.status_code, expect, f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    def history(self, application_id):
        return [h["action"] for h in self.call("GET", f"/applications/{application_id}")["history"]]

    def make_opening(self, title="Backend Engineer", stages=None, vacancies=1):
        body = {"title": title, "department_id": str(self.dept.id), "branch_id": str(self.branch.id),
                "vacancies": vacancies, "description": "Build and run APIs.", "employment_type": "Full-time",
                "work_mode": "Hybrid", "reporting_manager_id": str(self.owner.employee_id),
                "hiring_team": [str(self.hr.employee_id)]}
        if stages is not None:
            body["interview_stages"] = stages
        o = self.call("POST", "/job-openings", body, expect=201)
        return self.call("POST", f"/job-openings/{o['id']}/publish")

    def make_application(self, opening_id, name="Ravi Kumar", email=None):
        email = email or f"ravi.{uuid.uuid4().hex[:6]}@example.com"
        c = self.call("POST", "/candidates", {"name": name, "email": email, "phone": "98" + uuid.uuid4().int.__str__()[:8],
                                              "years_experience": 4, "expected_ctc": 900000, "source": "Job Board",
                                              "opening_id": opening_id}, expect=201)
        return c["application_id"], c["id"]

    def act(self, app_id, action, body=None, expect=200, as_=None):
        return self.call("POST", f"/applications/{app_id}/actions/{action}", body or {}, expect=expect, as_=as_)

    def interview_round(self, app_id, recommendation="pass", panel=None):
        """Schedule (in the past so it can be held), complete, feedback."""
        panel = panel or [self.owner]
        start = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)).isoformat()
        iv = self.call("POST", "/interviews", {"application_id": app_id, "scheduled_at": start, "mode": "video",
                                               "meeting_link": "https://meet.example.com/x",
                                               "participant_ids": [str(u.employee_id) for u in panel]}, expect=201)
        iv = self.call("POST", f"/interviews/{iv['id']}/complete", {})
        self.assertEqual(iv["status"], "feedback_pending")
        for u in panel:
            iv = self.call("POST", f"/interviews/{iv['id']}/feedback",
                           {"recommendation": recommendation, "rating": 4, "comments": "Solid.",
                            "scores": {s: 4 for s in iv["scorecard"]}}, as_=u)
        self.assertEqual(iv["status"], "feedback_completed")
        return iv

    def to_selected(self, app_id):
        self.act(app_id, "start_screening")
        self.act(app_id, "shortlist")
        for _ in range(3):
            self.interview_round(app_id)
            self.act(app_id, "pass_stage", {"comments": "Good"})
        return self.act(app_id, "select", {"comments": "Strong hire"})

    def to_sent_offer(self, app_id, **extra):
        today = datetime.date.today()
        o = self.call("POST", "/offers", {"application_id": app_id, "offered_ctc": 1200000,
                                          "joining_date": (today + datetime.timedelta(days=20)).isoformat(),
                                          "probation_months": 6, "notice_period_days": 60, **extra}, expect=201)
        o = self.call("POST", f"/offers/{o['id']}/submit", {})
        self.assertEqual(o["status"], "approval_pending")
        o = self.call("POST", f"/offers/{o['id']}/decision", {"decision": "approve"}, as_=self.owner)
        self.assertEqual(o["status"], "approved")
        o = self.call("POST", f"/offers/{o['id']}/send", {"email": True})
        self.assertEqual(o["status"], "sent")
        return o

    def portal_path(self):
        body = self.sent_offers[-1]["message"]
        m = re.search(r"/api/candidate-portal/[^\s]+", body)
        self.assertIsNotNone(m, body)
        return m.group(0)

    # ── the critical end-to-end scenario (spec §56) ───────────────────────
    def test_full_flow_hiring_request_to_employee(self):
        # Hiring request -> approval (Owner) -> draft opening created
        req = self.call("POST", "/requisitions", {
            "designation_title": "Backend Engineer", "department_id": str(self.dept.id),
            "branch_id": str(self.branch.id), "positions_count": 1, "employment_type": "Full-time",
            "work_mode": "Hybrid", "reporting_manager_id": str(self.owner.employee_id),
            "salary_min": 900000, "salary_max": 1500000, "experience_min": 3, "experience_max": 6,
            "justification": "Team growth", "job_description": "Build and run APIs.", "priority": "high",
            "hiring_team": [str(self.hr.employee_id)], "submit": True}, expect=201)
        self.assertEqual(req["status"], "pending")
        self.assertNotIn("approve", req["actions"])  # requester can't approve their own
        req = self.call("POST", f"/requisitions/{req['id']}/decision", {"decision": "approve", "comments": "OK"},
                        as_=self.owner)
        self.assertEqual(req["status"], "approved")
        self.assertIsNotNone(req["opening_id"])
        opening = self.call("GET", f"/job-openings/{req['opening_id']}")
        self.assertEqual(opening["status"], "draft")
        self.assertEqual(opening["salary_max"], 1500000)
        self.assertEqual([s["name"] for s in opening["interview_stages"]], ["Technical Round", "Manager Round", "HR Round"])
        # Applications are refused until published.
        cand = self.call("POST", "/candidates", {"name": "Ravi Kumar", "email": "ravi.kumar.e2e@example.com",
                                                 "phone": "9876500011", "years_experience": 4}, expect=201)
        self.call("POST", "/applications", {"candidate_id": cand["id"], "opening_id": opening["id"]}, expect=409)
        self.call("POST", f"/job-openings/{opening['id']}/publish")
        # Duplicate candidate is detected.
        dup = self.call("POST", "/candidates", {"name": "R Kumar", "email": "RAVI.KUMAR.E2E@example.com"}, expect=409)
        self.assertEqual(dup["detail"]["code"], "duplicate_candidate")
        app_ = self.call("POST", "/applications", {"candidate_id": cand["id"], "opening_id": opening["id"],
                                                   "source": "Employee Referral"}, expect=201)
        app_id = app_["id"]
        self.call("POST", "/applications", {"candidate_id": cand["id"], "opening_id": opening["id"]}, expect=409)

        # Screening can't skip ahead, drag-and-drop can't bypass rules.
        self.act(app_id, "select", expect=409)
        self.act(app_id, "move", {"target": "interview"}, expect=409)
        self.act(app_id, "move", {"target": "screening"})
        self.act(app_id, "request_info", {"message": "Please share your notice period."})
        self.act(app_id, "shortlist")

        # Three stages, each needs mandatory feedback before it can be passed.
        for n, stage in enumerate(["Technical Round", "Manager Round", "HR Round"]):
            if n == 0:
                # Plain employee on the panel: sees & reviews only their interview.
                iv = self.interview_round(app_id, panel=[self.plain])
                self.call("GET", f"/interviews/{iv['id']}", as_=self.plain)
                self.call("GET", f"/applications/{app_id}", as_=self.plain)
                self.call("GET", "/offers", as_=self.plain, expect=403)
                mine = self.call("GET", "/interviews", as_=self.plain)
                self.assertTrue(all(i["is_participant"] for i in mine["items"]))
                # a non-panelist can't give feedback
                self.call("POST", f"/interviews/{iv['id']}/feedback", {"recommendation": "pass", "comments": "x"},
                          as_=self.owner, expect=403)
            else:
                start = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
                iv = self.call("POST", "/interviews", {"application_id": app_id, "scheduled_at": start,
                                                       "participant_ids": [str(self.owner.employee_id)]}, expect=201)
                self.assertEqual(iv["stage_name"], stage)
                self.act(app_id, "pass_stage", expect=409)  # interview still scheduled
                self.call("POST", f"/interviews/{iv['id']}/complete", {})
                self.act(app_id, "pass_stage", expect=409)  # feedback pending
                self.call("POST", f"/interviews/{iv['id']}/feedback",
                          {"recommendation": "pass", "rating": 5, "comments": "Great"}, as_=self.owner)
            a = self.act(app_id, "pass_stage", {"comments": f"{stage} cleared"})
        self.assertIn("select", a["actions"])
        a = self.act(app_id, "select", {"comments": "Hire"})
        self.assertEqual(a["status"], "selected")

        # Offer: prefilled from requisition/opening, approval by someone else, send.
        today = datetime.date.today()
        offer = self.call("POST", "/offers", {"application_id": app_id, "offered_ctc": 1250000,
                                              "joining_date": (today + datetime.timedelta(days=15)).isoformat(),
                                              "probation_months": 6}, expect=201)
        self.assertEqual(offer["status"], "draft")
        self.assertEqual(offer["designation"], "Backend Engineer")
        self.assertEqual(offer["work_mode"], "Hybrid")
        self.assertEqual(offer["reporting_manager"]["id"], str(self.owner.employee_id))
        self.call("POST", f"/offers/{offer['id']}/send", {}, expect=409)  # not approved
        self.call("POST", "/offers", {"application_id": app_id, "offered_ctc": 1}, expect=409)  # one active offer
        offer = self.call("POST", f"/offers/{offer['id']}/submit", {})
        self.call("POST", f"/offers/{offer['id']}/decision", {"decision": "approve"}, expect=403)  # preparer
        offer = self.call("POST", f"/offers/{offer['id']}/decision",
                          {"decision": "request_changes", "comments": "Add benefits"}, as_=self.owner)
        self.assertEqual(offer["status"], "draft")
        self.call("PATCH", f"/offers/{offer['id']}", {"benefits": "Health insurance"})
        self.call("POST", f"/offers/{offer['id']}/submit", {})
        offer = self.call("POST", f"/offers/{offer['id']}/decision", {"decision": "approve"}, as_=self.owner)
        offer = self.call("POST", f"/offers/{offer['id']}/send", {"email": True})
        self.assertEqual(offer["status"], "sent")
        self.assertEqual(self.sent_offers[-1]["to"], "ravi.kumar.e2e@example.com")

        # Candidate opens the portal link (viewed) and accepts.
        portal = self.portal_path()
        page = self.client.get(portal)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Offer of employment", page.text)
        self.assertEqual(self.call("GET", f"/offers/{offer['id']}")["status"], "viewed")
        r = self.client.post(portal + "/accept", data={"confirm": "yes", "message": "Excited!"})
        self.assertIn("Thank you", r.text)
        self.assertEqual(self.client.get(portal).status_code, 404)  # link revoked after response
        offer = self.call("GET", f"/offers/{offer['id']}")
        self.assertEqual((offer["status"], offer["response_source"]), ("accepted", "portal"))
        a = self.call("GET", f"/applications/{app_id}")
        self.assertEqual(a["status"], "preboarding")
        pb_id = a["preboarding"]["id"]

        # Preboarding: send tasks, candidate fills details + uploads, HR reviews.
        pb = self.call("POST", f"/preboarding/{pb_id}/send-tasks", {"email": False})
        self.assertEqual(pb["status"], "documents_pending")
        self.call("POST", f"/preboarding/{pb_id}/confirm-joining", {}, expect=409)
        self.call("POST", f"/preboarding/{pb_id}/create-employee", {}, expect=409)
        self.call("PUT", f"/preboarding/{pb_id}/details", {
            "personal": {"first_name": "Ravi", "last_name": "Kumar", "gender": "Male", "date_of_birth": "1996-04-02",
                         "pan": "ABCDE1234F"},
            "contact": {"personal_phone": "9876500011", "current_address": "Hyderabad"},
            "emergency": {"name": "Sita Kumar", "relation": "Mother", "phone": "9876500012"},
            "bank": {"bank_name": "HDFC", "account_no": "123456789012", "ifsc": "HDFC0001234", "account_holder": "Ravi Kumar"}})
        self.call("PUT", f"/preboarding/{pb_id}/details", {"personal": {"pan": "BAD"}}, expect=422)
        pb = self.call("GET", f"/preboarding/{pb_id}")
        docs = [t for t in pb["tasks"] if t["task_type"] == "document" and t["required"]]
        first = True
        for t in docs:
            pb = self.call("POST", f"/preboarding/tasks/{t['id']}/documents", files={"file": (f"{t['name']}.pdf", PDF, "application/pdf")},
                           expect=201)
            doc = next(x for x in pb["tasks"] if x["id"] == t["id"])["documents"][0]
            if first:  # reject -> resubmission required -> upload again -> approve
                pb = self.call("POST", f"/preboarding/documents/{doc['id']}/review", {"decision": "reject"}, expect=422)
                pb = self.call("POST", f"/preboarding/documents/{doc['id']}/review",
                               {"decision": "reject", "comments": "Blurry scan"})
                self.assertEqual(next(x for x in pb["tasks"] if x["id"] == t["id"])["status"], "resubmission_required")
                pb = self.call("POST", f"/preboarding/tasks/{t['id']}/documents",
                               files={"file": ("again.pdf", PDF, "application/pdf")}, expect=201)
                task = next(x for x in pb["tasks"] if x["id"] == t["id"])
                self.assertEqual([d["version"] for d in task["documents"]], [2, 1])
                doc = task["documents"][0]
                first = False
            self.call("POST", f"/preboarding/documents/{doc['id']}/review", {"decision": "approve"})
        ack = next(t for t in pb["tasks"] if t["task_type"] == "acknowledgement")
        pb = self.call("PATCH", f"/preboarding/tasks/{ack['id']}", {"status": "completed"})
        self.assertEqual(pb["status"], "verification_pending")
        ver = next(t for t in pb["tasks"] if t["task_type"] == "verification" and t["required"])
        pb = self.call("PATCH", f"/preboarding/tasks/{ver['id']}", {"status": "verified"})
        self.assertEqual(pb["status"], "ready_to_join")
        self.assertTrue(all(c["ok"] for c in pb["checklist"]), pb["checklist"])

        # To-Do: joining confirmation shows up with the exact record.
        todo = self.call("GET", "/my-actions")
        self.assertTrue(any(i["kind"] == "joining_confirmation" and i["entity_id"] == pb_id for i in todo))
        pb = self.call("POST", f"/preboarding/{pb_id}/confirm-joining", {"joining_date": today.isoformat()})
        self.assertEqual(pb["joining_status"], "confirmed")

        # Create Employee through the People creation logic; idempotent.
        prefill = self.call("GET", f"/preboarding/{pb_id}/employee-prefill")
        self.assertEqual(prefill["first_name"], "Ravi")
        self.assertEqual(prefill["bank_ifsc"], "HDFC0001234")
        work_email = f"ravi.kumar.{uuid.uuid4().hex[:6]}@impacgo.com"
        body = {"work_email": work_email, "role_name": self.role_name, "password": "Joiner#Strong42"}
        self.call("POST", f"/preboarding/{pb_id}/create-employee", body, as_=self.plain, expect=403)
        # An existing employee with the joiner's PAN is reported first (409,
        # with the matches) -- made deterministic here, inside the rolled-back
        # transaction, instead of depending on the tenant's data.
        owner_emp = self.db.get(models.Employee, self.owner.employee_id)
        owner_pan, owner_emp.pan = owner_emp.pan, "ABCDE1234F"
        self.db.flush()
        dup = self.call("POST", f"/preboarding/{pb_id}/create-employee", body, expect=409)
        self.assertIn("An existing employee matches this joiner", dup["detail"]["message"])
        owner_emp.pan = owner_pan
        self.db.flush()
        body = {**body, "ignore_matches": True}  # HR confirmed it's a different person
        with mock.patch.object(crud, "create_employee", side_effect=ValueError("Employee code already exists")):
            fail = self.call("POST", f"/preboarding/{pb_id}/create-employee", body, expect=409)
        self.assertIn("already exists", fail["detail"]["message"])
        self.assertEqual(self.call("GET", f"/preboarding/{pb_id}")["employee_status"], "failed")
        self.assertEqual(self.call("GET", f"/applications/{app_id}")["status"], "preboarding")  # not hired
        done = self.call("POST", f"/preboarding/{pb_id}/create-employee", body)  # retry
        emp_id = done["result"]["employee_id"]
        self.assertFalse(done["result"]["already_created"])
        again = self.call("POST", f"/preboarding/{pb_id}/create-employee", body)
        self.assertTrue(again["result"]["already_created"])
        self.assertEqual(again["result"]["employee_id"], emp_id)

        emp = self.db.get(models.Employee, uuid.UUID(emp_id))
        self.assertEqual((emp.first_name, emp.last_name, emp.work_email), ("Ravi", "Kumar", work_email))
        self.assertEqual(emp.department_id, self.dept.id)
        self.assertEqual(emp.reporting_manager_id, self.owner.employee_id)
        self.assertEqual(emp.pan, "ABCDE1234F")
        self.assertEqual(self.db.scalar(select(text("count(*)")).select_from(models.Employee)
                                        .where(models.Employee.work_email == work_email)), 1)
        self.assertEqual(len(crud.get_employee_documents(self.db, emp.id)), len(docs))  # approved docs copied
        self.assertIsNotNone(crud.find_user_by_email(self.db, work_email))  # login account (People logic)

        a = self.call("GET", f"/applications/{app_id}")
        self.assertEqual((a["status"], a["employee_id"]), ("hired", emp_id))
        pb = self.call("GET", f"/preboarding/{pb_id}")
        self.assertEqual((pb["status"], pb["employee_status"]), ("completed", "created"))
        hist = self.history(app_id)
        for expected in ("Application created", "Moved to Screening", "Shortlisted", "Moved to Technical Round",
                         "Passed Technical Round", "Passed HR Round", "Candidate selected", "Offer created",
                         "Offer submitted for approval", "Offer approved", "Offer sent", "Offer viewed by candidate",
                         "Offer accepted", "Preboarding record created", "Joining confirmed", "Employee created"):
            self.assertTrue(any(h.startswith(expected) for h in hist), f"missing history: {expected}")
        audits = self.db.scalar(text("SELECT count(*) FROM core_audit_logs WHERE doctype LIKE 'recruitment_%' "
                                     "AND document_id = :i"), {"i": uuid.UUID(app_id)})
        self.assertGreater(audits, 10)
        summary = self.call("GET", "/summary")
        self.assertGreaterEqual(summary["applications"]["hired"], 1)
        self.assertIsNotNone(summary["time_to_hire_days"])

    # ── alternate paths (spec §57) ────────────────────────────────────────
    def test_requisition_reject_and_request_changes(self):
        body = {"designation_title": "QA Engineer", "department_id": str(self.dept.id), "positions_count": 2,
                "justification": "Backfill"}
        req = self.call("POST", "/requisitions", body, expect=201)
        self.assertEqual(req["status"], "draft")
        self.call("PATCH", f"/requisitions/{req['id']}", {"positions_count": 3})
        self.call("POST", f"/requisitions/{req['id']}/submit", {})
        self.call("POST", f"/requisitions/{req['id']}/decision", {"decision": "reject"}, as_=self.owner, expect=422)
        req = self.call("POST", f"/requisitions/{req['id']}/decision",
                        {"decision": "request_changes", "comments": "Justify 3 heads"}, as_=self.owner)
        self.assertEqual((req["status"], req["decision_notes"]), ("sent_back", "Justify 3 heads"))
        self.call("PATCH", f"/requisitions/{req['id']}", {"justification": "New client project, 3 heads"})
        req = self.call("POST", f"/requisitions/{req['id']}/submit", {})
        self.assertEqual((req["status"], req["version"]), ("pending", 2))
        req = self.call("POST", f"/requisitions/{req['id']}/decision",
                        {"decision": "reject", "comments": "No budget"}, as_=self.owner)
        self.assertEqual(req["status"], "rejected")
        self.assertIsNone(req["opening_id"])  # no job opening
        self.call("POST", f"/requisitions/{req['id']}/create-opening", expect=409)
        acts = [h["action"] for h in req["history"]]
        self.assertIn("Changes requested", acts)
        self.assertIn("Resubmitted for approval", acts)
        self.assertIn("Rejected", acts)
        self.call("POST", f"/requisitions/{req['id']}/decision", {"decision": "approve"}, as_=self.owner, expect=409)
        # Unauthorized / cross-tenant.
        self.call("POST", "/requisitions", body, as_=self.plain, expect=403)
        outsider = models.User(id=uuid.uuid4(), company_id=uuid.uuid4(), email="x@other.com", password_hash="x",
                               employee_id=None)
        with mock.patch("app.recruitment_workflow.make_ctx",
                        lambda db, u: __import__("app.recruitment_workflow", fromlist=["Ctx"]).Ctx(
                            db, u, u.company_id, "a", False, "Outsider")):
            self.call("GET", f"/requisitions/{req['id']}", as_=outsider, expect=404)

    def test_reject_at_screening_hold_resume_and_withdraw(self):
        opening = self.make_opening("Support Engineer")
        a1, cand = self.make_application(opening["id"], "Anil")
        self.act(a1, "start_screening")
        self.act(a1, "reject", {}, expect=422)  # reason mandatory
        a = self.act(a1, "reject", {"reason": "Skills mismatch", "comments": "No Linux"})
        self.assertEqual((a["status"], a["rejection_reason"]), ("rejected", "Skills mismatch"))
        self.call("POST", "/interviews", {"application_id": a1, "scheduled_at": "2030-01-01T10:00:00",
                                          "participant_ids": [str(self.owner.employee_id)]}, expect=409)
        self.call("POST", "/offers", {"application_id": a1, "offered_ctc": 10}, expect=409)
        # the candidate stays searchable, and can apply to another opening
        self.assertEqual(self.call("GET", f"/candidates/{cand}")["applications"][0]["status"], "rejected")
        other = self.make_opening("Support Engineer II")
        self.call("POST", "/applications", {"candidate_id": cand, "opening_id": other["id"]}, expect=201)
        # reopen (admin) goes back to where it was
        a = self.act(a1, "reopen", {"comments": "Reconsider"})
        self.assertEqual(a["status"], "screening")

        a2, _ = self.make_application(opening["id"], "Bala")
        self.act(a2, "start_screening")
        self.act(a2, "shortlist")
        self.interview_round(a2)
        a = self.act(a2, "hold", {"reason": "Budget review", "review_date": "2030-01-10"})
        self.assertEqual((a["status"], a["previous_status"]), ("on_hold", "interview"))
        self.act(a2, "pass_stage", expect=409)  # no forward progress on hold
        a = self.act(a2, "resume", {})
        self.assertEqual((a["status"], a["current_stage"]), ("interview", "Technical Round"))
        a = self.act(a2, "withdraw", {"reason": "Took another offer", "source": "candidate"})
        self.assertEqual(a["status"], "withdrawn")
        self.assertIn("Application withdrawn", self.history(a2))

    def test_reject_after_interview_feedback(self):
        opening = self.make_opening("Data Analyst")
        a1, _ = self.make_application(opening["id"])
        self.act(a1, "start_screening")
        self.act(a1, "shortlist")
        self.interview_round(a1, recommendation="fail")
        a = self.act(a1, "reject", {"reason": "Failed interview"})
        self.assertEqual(a["status"], "rejected")
        d = self.call("GET", f"/applications/{a1}")
        self.assertEqual(d["stages"][0]["state"], "fail")

    def test_offer_declined_expired_withdrawn(self):
        opening = self.make_opening("Designer", stages=[{"name": "Portfolio Review", "type": "assessment"}], vacancies=3)
        # declined via portal
        a1, _ = self.make_application(opening["id"], "Deepa")
        self.act(a1, "start_screening")
        self.act(a1, "shortlist")
        self.interview_round(a1)
        self.act(a1, "pass_stage")
        self.act(a1, "select")
        o1 = self.to_sent_offer(a1)
        r = self.client.post(self.portal_path() + "/decline", data={"reason": "Relocation"})
        self.assertIn("Response recorded", r.text)
        o1 = self.call("GET", f"/offers/{o1['id']}")
        self.assertEqual((o1["status"], o1["decline_reason"]), ("declined", "Relocation"))
        a = self.call("GET", f"/applications/{a1}")
        self.assertEqual((a["status"], a["closed_reason"]), ("closed", "offer_declined"))
        self.assertIsNone(a["preboarding"])
        # reopen -> selected -> a new offer version is possible
        self.act(a1, "reopen", {"comments": "Candidate reconsidered"})
        o1b = self.call("POST", "/offers", {"application_id": a1, "offered_ctc": 1300000,
                                            "joining_date": "2031-01-05"}, expect=201)
        self.assertEqual(o1b["version"], 2)

        # expired
        a2, _ = self.make_application(opening["id"], "Esha")
        self.act(a2, "start_screening")
        self.act(a2, "shortlist")
        self.interview_round(a2)
        self.act(a2, "pass_stage")
        self.act(a2, "select")
        o2 = self.to_sent_offer(a2)
        self.db.get(models.Offer, uuid.UUID(o2["id"])).expiry_date = datetime.date.today() - datetime.timedelta(days=1)
        self.db.flush()
        self.call("POST", f"/offers/{o2['id']}/accept", {}, expect=409)
        self.assertEqual(self.call("GET", f"/offers/{o2['id']}")["status"], "expired")
        self.assertEqual(self.call("GET", f"/applications/{a2}")["closed_reason"], "offer_expired")

        # withdrawn after sending
        a3, _ = self.make_application(opening["id"], "Farah")
        self.act(a3, "start_screening")
        self.act(a3, "shortlist")
        self.interview_round(a3)
        self.act(a3, "pass_stage")
        self.act(a3, "select")
        o3 = self.to_sent_offer(a3)
        self.call("POST", f"/offers/{o3['id']}/withdraw", {}, expect=422)
        o3 = self.call("POST", f"/offers/{o3['id']}/withdraw", {"reason": "Position frozen"})
        self.assertEqual(o3["status"], "withdrawn")
        self.assertEqual(self.call("GET", f"/applications/{a3}")["status"], "closed")
        self.assertEqual(self.db.scalar(select(text("count(*)")).select_from(models.Preboarding)
                                        .where(models.Preboarding.application_id.in_(
                                            [uuid.UUID(a2), uuid.UUID(a3)]))), 0)

    def _preboarding_ready_candidate(self, name):
        opening = self.make_opening(f"Ops {name}", stages=[])
        a, _ = self.make_application(opening["id"], name)
        self.act(a, "start_screening")
        self.act(a, "shortlist")
        self.act(a, "select")
        o = self.to_sent_offer(a)
        self.call("POST", f"/offers/{o['id']}/accept", {"source": "email"})
        pb_id = self.call("GET", f"/applications/{a}")["preboarding"]["id"]
        self.call("POST", f"/preboarding/{pb_id}/send-tasks", {"email": False})
        return a, pb_id

    def test_verification_failure_policy_and_joining_cancel_no_show(self):
        a, pb_id = self._preboarding_ready_candidate("Gita")
        pb = self.call("GET", f"/preboarding/{pb_id}")
        ver = next(t for t in pb["tasks"] if t["task_type"] == "verification" and t["required"])
        self.call("PATCH", f"/preboarding/tasks/{ver['id']}", {"status": "failed"}, expect=422)
        pb = self.call("PATCH", f"/preboarding/tasks/{ver['id']}", {"status": "failed", "reason": "Fake degree"})
        self.assertEqual(pb["status"], "failed")  # default policy: hold for review
        self.call("POST", f"/preboarding/{pb_id}/create-employee", {}, expect=409)
        pb = self.call("POST", f"/preboarding/{pb_id}/reinitiate", {"reason": "New documents received"})
        self.assertNotEqual(pb["status"], "failed")
        # joining cancelled -> closed, no employee
        pb = self.call("POST", f"/preboarding/{pb_id}/cancel-joining", {"reason": "Candidate backed out"})
        self.assertEqual(pb["status"], "cancelled")
        self.assertEqual(self.call("GET", f"/applications/{a}")["status"], "closed")

        # reject policy
        self.call("PUT", "/settings", {"verification_failure_policy": "reject"})
        a2, pb2 = self._preboarding_ready_candidate("Hari")
        ver = next(t for t in self.call("GET", f"/preboarding/{pb2}")["tasks"]
                   if t["task_type"] == "verification" and t["required"])
        pb = self.call("PATCH", f"/preboarding/tasks/{ver['id']}", {"status": "failed", "reason": "Criminal record"})
        self.assertEqual(pb["status"], "cancelled")
        self.assertEqual(self.call("GET", f"/applications/{a2}")["status"], "rejected")

        # no-show: override verification (admin), confirm, no-show, close
        self.call("PUT", "/settings", {"verification_failure_policy": "hold",
                                       "preboarding_checklist": [{"category": "verification", "name": "Document verification",
                                                                  "task_type": "verification", "required": True}]})
        a3, pb3 = self._preboarding_ready_candidate("Indu")
        pb = self.call("POST", f"/preboarding/{pb3}/override-verification", {"reason": "Verified offline by BGV vendor"})
        self.assertEqual(pb["status"], "ready_to_join")
        self.call("POST", f"/preboarding/{pb3}/confirm-joining", {"joining_date": datetime.date.today().isoformat()})
        pb = self.call("POST", f"/preboarding/{pb3}/no-show", {"reason": "Did not report"})
        self.assertEqual(pb["joining_status"], "no_show")
        pb = self.call("POST", f"/preboarding/{pb3}/close-no-show", {})
        self.assertEqual(pb["status"], "cancelled")
        a = self.call("GET", f"/applications/{a3}")
        self.assertEqual((a["status"], a["closed_reason"], a["employee_id"]), ("closed", "no_show", None))

    def test_legacy_endpoints_go_through_the_workflow(self):
        """The Approvals inbox (PATCH /api/hiring-requisitions/{id}) and the
        old Kanban "Move to" (PATCH /api/candidates/{id}/stage) use the same
        validated transitions as the Recruitment screen."""
        req = self.call("POST", "/requisitions", {
            "designation_title": "Inbox Approved Role", "department_id": str(self.dept.id),
            "branch_id": str(self.branch.id), "employment_type": "Full-time", "work_mode": "Office",
            "reporting_manager_id": str(self.owner.employee_id), "justification": "Backfill",
            "job_description": "Role description", "submit": True}, expect=201)
        self.user = self.owner
        r = self.client.patch(f"/api/hiring-requisitions/{req['id']}", json={"status": "approved", "decision_notes": "OK"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "approved")
        req = self.call("GET", f"/requisitions/{req['id']}")
        self.assertIsNotNone(req["opening_id"])  # the linked opening was created
        self.assertIn("Approved", [h["action"] for h in req["history"]])
        r = self.client.patch(f"/api/hiring-requisitions/{req['id']}", json={"status": "rejected", "decision_notes": "x"})
        self.assertEqual(r.status_code, 409)

        opening = self.make_opening("Legacy Board Role")
        app_id, _ = self.make_application(opening["id"], "Kavya")
        self.user = self.hr
        r = self.client.patch(f"/api/candidates/{app_id}/stage", json={"stage": "offer"})
        self.assertEqual(r.status_code, 409, r.text)  # can't jump to Offer
        r = self.client.patch(f"/api/candidates/{app_id}/stage", json={"stage": "screened"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.call("GET", f"/applications/{app_id}")["status"], "screening")

        # A legacy application sitting in Interview with no stage yet can
        # enter the opening's first configured stage.
        a = self.db.get(models.JobApplication, uuid.UUID(app_id))
        a.status, a.stage, a.stage_index = "interview", "interview", None
        self.db.flush()
        d = self.call("GET", f"/applications/{app_id}")
        self.assertIn("advance", d["actions"])
        d = self.act(app_id, "advance", {})
        self.assertEqual(d["current_stage"], "Technical Round")

    def test_permissions_and_compensation_visibility(self):
        self.call("GET", "/job-openings", as_=self.plain, expect=403)
        self.call("GET", "/pipeline", as_=self.plain, expect=403)
        self.call("PUT", "/settings", {"offer_expiry_days": 5}, as_=self.plain, expect=403)
        opening = self.make_opening("Security Engineer")
        view_ctx = __import__("app.recruitment_workflow", fromlist=["Ctx"])
        with mock.patch("app.routers.recruitment_workflow.rw.make_ctx",
                        lambda db, u: view_ctx.Ctx(db, u, u.company_id, "v", False, "Viewer")):
            o = self.call("PATCH", f"/job-openings/{opening['id']}", {"salary_max": 1}, as_=self.plain, expect=403)
            self.call("PATCH", f"/job-openings/{opening['id']}", {"salary_min": 1}, as_=self.plain, expect=403)
        self.call("PATCH", f"/job-openings/{opening['id']}", {"salary_min": 500000, "salary_max": 800000})
        with mock.patch("app.routers.recruitment_workflow.rw.make_ctx",
                        lambda db, u: view_ctx.Ctx(db, u, u.company_id, "v", False, "Viewer")):
            o = self.call("GET", f"/job-openings/{opening['id']}", as_=self.plain)
        self.assertIsNone(o["salary_max"])  # view-only users don't see compensation
        self.assertEqual(self.call("GET", f"/job-openings/{opening['id']}")["salary_max"], 800000)

    def inbox(self, as_):
        self.user = as_
        r = self.client.get("/api/approvals/inbox-sources")
        self.assertEqual(r.status_code, 200, r.text[:600])
        return r.json()["sources"]

    def notices(self, interview_id, title=None, entity_type=None):
        q = select(models.Notification).where(models.Notification.entity_id == uuid.UUID(interview_id))
        if title:
            q = q.where(models.Notification.title == title)
        if entity_type:
            q = q.where(models.Notification.entity_type == entity_type)
        return {n.user_id for n in self.db.scalars(q).all()}

    def test_interview_invitation_workflow(self):
        """Invite -> accept / reject -> replace -> schedule the time -> confirm /
        decline / request reschedule -> reschedule, with history throughout."""
        others = [u for u in self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None),
                                                                       models.User.status == "active")).all()
                  if u.id not in (self.owner.id, self.hr.id, self.plain.id)]
        if not others:
            self.skipTest("needs a fourth employee user")
        second = others[0]
        opening = self.make_opening("Invite Engineer")  # created by self.hr -> the organizer
        app_id, _ = self.make_application(opening["id"], "Meera")
        self.act(app_id, "start_screening")
        a = self.act(app_id, "shortlist")
        self.assertIn("invite_interviewers", a["actions"])

        # 1. invite two interviewers -- no time yet; only they are notified
        iv = self.call("POST", "/interview-invitations", {"application_id": app_id, "mode": "video",
                                                          "participant_ids": [str(self.plain.employee_id),
                                                                              str(second.employee_id)]}, expect=201)
        ivid = iv["id"]
        self.assertEqual((iv["status"], iv["awaiting_time"], iv["scheduled_at"]), ("invited", True, None))
        self.assertEqual([p["response"] for p in iv["participants"]], ["pending", "pending"])
        self.assertEqual(self.notices(ivid, entity_type="interview_invitation"), {self.plain.id, second.id})
        # no duplicate invitation / interview for the same stage
        self.call("POST", "/interview-invitations", {"application_id": app_id,
                                                     "participant_ids": [str(self.owner.employee_id)]}, expect=409)
        future = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0) + datetime.timedelta(days=2)
        self.call("POST", "/interviews", {"application_id": app_id, "scheduled_at": future.isoformat(),
                                          "participant_ids": [str(self.owner.employee_id)]}, expect=409)
        self.assertNotIn("invite_interviewers", self.call("GET", f"/applications/{app_id}")["actions"])
        self.act(app_id, "pass_stage", expect=409)
        # an interviewer without Recruitment access can't manage the invitation
        self.call("POST", f"/interviews/{ivid}/invite", {"participant_ids": [str(self.owner.employee_id)]},
                  as_=self.plain, expect=403)

        # 2. the pending-invitations worklist asks each interviewer for availability
        # (interview invitations are no longer surfaced in the combined Approvals
        # inbox -- the new round-based flow has no accept/reject step there;
        # this dedicated endpoint still serves the legacy invite-flow directly).
        # Matched by id rather than assumed to be items[0]: the shared dev tenant
        # may have other genuinely pending legacy invitations for the same employee.
        row = next(x for x in self.call("GET", "/interviews/invitations/pending", as_=self.plain)["items"]
                  if x["id"] == ivid)
        self.assertEqual((row["id"], row["phase"], row["options"]), (ivid, "availability", ["accept", "decline"]))
        self.call("POST", f"/interviews/{ivid}/respond", {"response": "reschedule", "reason": "x",
                                                         "proposed_availability": "y"}, as_=self.plain, expect=409)

        # 3. one rejects (reason required) -> shown as rejected, the creator is told
        self.call("POST", f"/interviews/{ivid}/respond", {"response": "reject"}, as_=second, expect=422)
        out = self.call("POST", f"/interviews/{ivid}/respond", {"response": "reject", "reason": "Travelling"},
                        as_=second)
        self.assertEqual(out["status"], "invited")
        declined = next(p for p in out["participants"] if p["id"] == str(second.employee_id))
        self.assertEqual((declined["response"], declined["reason"]), ("declined", "Travelling"))
        self.assertEqual(self.notices(ivid, title="Interviewer rejected the invitation"), {self.hr.id})
        self.assertNotIn(ivid, [x["id"] for x in self.call("GET", "/interviews/invitations/pending", as_=second)["items"]])
        self.call("POST", f"/interviews/{ivid}/respond", {"response": "accept"}, as_=second, expect=409)

        # 4. the other accepts -> ready to schedule
        out = self.call("POST", f"/interviews/{ivid}/respond", {"response": "accept"}, as_=self.plain)
        self.assertEqual(out["status"], "ready_to_schedule")
        self.assertIn(self.hr.id, self.notices(ivid, title="Interviewer accepted"))
        hr_view = self.call("GET", f"/interviews/{ivid}")
        self.assertTrue({"invite", "replace", "schedule_time", "cancel"} <= set(hr_view["actions"]))
        self.assertEqual(hr_view["needs_replacement"], [crud.employee_display_name(self.db, second.employee_id)])
        kinds = {x["kind"] for x in self.call("GET", "/my-actions") if x.get("entity_id") == ivid}
        self.assertEqual(kinds, {"interview_schedule", "interview_replace"})

        # 5. replace the one who rejected -- their row stays, the candidate is untouched
        self.call("POST", f"/interviews/{ivid}/invite", {"participant_ids": [str(second.employee_id)],
                                                        "replaces": str(second.employee_id)}, expect=409)
        self.call("POST", f"/interviews/{ivid}/invite", {"participant_ids": [str(self.plain.employee_id)]},
                  expect=409)
        out = self.call("POST", f"/interviews/{ivid}/invite", {"participant_ids": [str(self.owner.employee_id)],
                                                              "replaces": str(second.employee_id)})
        by_id = {p["id"]: p for p in out["participants"]}
        self.assertEqual(by_id[str(second.employee_id)]["response"], "declined")
        self.assertEqual(by_id[str(second.employee_id)]["replaced_by"]["id"], str(self.owner.employee_id))
        self.assertEqual(by_id[str(self.owner.employee_id)]["response"], "pending")
        self.assertEqual(out["needs_replacement"], [])
        self.assertIn(self.owner.id, self.notices(ivid, entity_type="interview_invitation"))
        self.call("POST", f"/interviews/{ivid}/invite", {"participant_ids": [str(self.hr.employee_id)],
                                                        "replaces": str(second.employee_id)}, expect=409)
        self.assertEqual(self.call("GET", f"/applications/{app_id}")["status"], "interview")

        # 6. schedule the time -- no double booking of an interviewer
        app2, _ = self.make_application(opening["id"], "Kiran")
        self.act(app2, "start_screening")
        self.act(app2, "shortlist")
        self.call("POST", "/interviews", {"application_id": app2, "scheduled_at": future.isoformat(),
                                          "participant_ids": [str(self.plain.employee_id)]}, expect=201)
        self.call("POST", f"/interviews/{ivid}/schedule", {"scheduled_at": future.isoformat()}, as_=self.plain,
                  expect=403)
        clash = self.call("POST", f"/interviews/{ivid}/schedule",
                          {"scheduled_at": (future + datetime.timedelta(minutes=30)).isoformat()}, expect=409)
        self.assertIn("already has an interview", str(clash))
        slot = future + datetime.timedelta(hours=3)
        out = self.call("POST", f"/interviews/{ivid}/schedule", {"scheduled_at": slot.isoformat(),
                                                                "meeting_link": "https://meet.example.com/z"})
        self.assertEqual((out["status"], out["awaiting_time"]), ("scheduled", False))
        by_id = {p["id"]: p["response"] for p in out["participants"]}
        self.assertEqual(by_id, {str(self.plain.employee_id): "pending", str(self.owner.employee_id): "pending",
                                 str(second.employee_id): "declined"})
        self.assertIn(self.plain.id, self.notices(ivid, title="Interview scheduled"))
        self.assertNotIn(second.id, self.notices(ivid, title="Interview scheduled"))

        # 7. the scheduled invitation: accept / decline / request reschedule
        row = next(x for x in self.call("GET", "/interviews/invitations/pending", as_=self.plain)["items"]
                  if x["id"] == ivid)
        self.assertEqual((row["phase"], row["options"]), ("schedule", ["accept", "decline", "reschedule"]))
        self.call("POST", f"/interviews/{ivid}/respond", {"response": "reschedule", "reason": "Clash"},
                  as_=self.plain, expect=422)
        out = self.call("POST", f"/interviews/{ivid}/respond",
                        {"response": "reschedule", "reason": "Client call",
                         "proposed_availability": "Tomorrow after 3 PM"}, as_=self.plain)
        self.assertEqual(out["status"], "reschedule_requested")
        mine = next(p for p in out["participants"] if p["id"] == str(self.plain.employee_id))
        self.assertEqual(mine["proposed_availability"], "Tomorrow after 3 PM")
        self.assertIn(self.hr.id, self.notices(ivid, title="Reschedule requested"))
        self.assertIn("interview_reschedule",
                      {x["kind"] for x in self.call("GET", "/my-actions") if x.get("entity_id") == ivid})

        # 8. the organizer picks a new time; everyone confirms it again
        out = self.call("POST", f"/interviews/{ivid}/reschedule",
                        {"scheduled_at": (slot + datetime.timedelta(days=1)).isoformat(), "reason": "Per request"})
        self.assertEqual((out["status"], out["reschedule_count"]), ("scheduled", 1))
        self.assertEqual({p["id"]: p["response"] for p in out["participants"]}[str(self.plain.employee_id)], "pending")
        self.assertIn(self.plain.id, self.notices(ivid, title="Interview rescheduled"))
        self.call("POST", f"/interviews/{ivid}/respond", {"response": "accept"}, as_=self.plain)
        out = self.call("POST", f"/interviews/{ivid}/respond", {"response": "accept"}, as_=self.owner)
        self.assertEqual(out["status"], "scheduled")
        self.assertNotIn(ivid, [r["id"] for r in self.call("GET", "/interviews/invitations/pending", as_=self.plain)["items"]])
        # answered invitations are no longer unread
        self.assertEqual(self.db.scalar(select(func.count()).select_from(models.Notification).where(
            models.Notification.entity_id == uuid.UUID(ivid), models.Notification.user_id == self.plain.id,
            models.Notification.entity_type == "interview_invitation", models.Notification.read_at.is_(None))), 0)

        # 9. the complete invitation / scheduling history
        actions = [h["action"] for h in self.call("GET", f"/interviews/{ivid}")["history"]]
        for expected in ("Interviewers invited -- Technical Round", "Interview invitation rejected",
                         "Interview invitation accepted", "Interview scheduled -- Technical Round",
                         "Reschedule requested", "Interview rescheduled", "Interview time accepted"):
            self.assertIn(expected, actions)
        self.assertTrue(any(x.startswith("Interviewer replaced") for x in actions))
        # a feedback-less decliner isn't asked for feedback / can't give it
        self.call("POST", f"/interviews/{ivid}/feedback", {"recommendation": "pass", "comments": "x"},
                  as_=second, expect=403)

    def test_interview_round_workflow(self):
        """Round-based interviews (Recruitment > Job Opening > Interview
        Rounds): a shared session per stage with a checkbox-picked panel,
        no accept/reject -- candidates are auto-attached on stage-advance,
        interviewers just Start/End, and the organizer's internal decision
        drives the pipeline, with conflict checks and history throughout."""
        others = [u for u in self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None),
                                                                       models.User.status == "active")).all()
                  if u.id not in (self.owner.id, self.hr.id, self.plain.id)]
        if not others:
            self.skipTest("needs a fourth employee user")
        second = others[0]
        opening = self.make_opening("Round Engineer")  # created by self.hr -> the organizer
        stages = [s["key"] for s in self.call("GET", f"/job-openings/{opening['id']}")["interview_stages"]]
        technical, manager = stages[0], stages[1]
        start = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).replace(microsecond=0)

        # 1. define the round: date/time + a checkbox-picked panel of two
        r = self.call("POST", f"/job-openings/{opening['id']}/rounds",
                      {"round_key": technical, "scheduled_at": start.isoformat(), "mode": "video",
                       "meeting_link": "https://meet.example.com/round",
                       "employee_ids": [str(self.plain.employee_id), str(second.employee_id)]}, expect=201)
        rid = r["id"]
        self.assertEqual(r["status"], "scheduled")
        self.assertEqual({e["id"] for e in r["interviewers"]}, {str(self.plain.employee_id), str(second.employee_id)})
        # only the organizer (opening creator / Recruitment editor) can define rounds
        self.call("POST", f"/job-openings/{opening['id']}/rounds",
                  {"round_key": manager, "scheduled_at": start.isoformat(),
                   "employee_ids": [str(self.plain.employee_id)]}, as_=self.plain, expect=403)
        # a stage can't have two round definitions
        self.call("POST", f"/job-openings/{opening['id']}/rounds",
                  {"round_key": technical, "scheduled_at": start.isoformat(),
                   "employee_ids": [str(self.owner.employee_id)]}, expect=409)
        # no double-booking an interviewer across two round sessions
        self.call("POST", f"/job-openings/{opening['id']}/rounds",
                  {"round_key": manager, "scheduled_at": (start + datetime.timedelta(minutes=30)).isoformat(),
                   "employee_ids": [str(self.plain.employee_id)]}, expect=409)

        # 2. a candidate reaching the stage is auto-attached -- no invite step
        app_id, _ = self.make_application(opening["id"], "Asha")
        self.act(app_id, "start_screening")
        self.act(app_id, "shortlist")
        a = self.act(app_id, "advance")
        iv = next(x for x in a["interviews"] if x["round_id"] == rid)
        ivid = iv["id"]
        self.assertEqual((iv["status"], iv["scheduled_at"], iv["mode"], iv["meeting_link"]),
                         ("scheduled", start.isoformat(), "video", "https://meet.example.com/round"))
        self.assertEqual({p["id"] for p in iv["participants"]}, {str(self.plain.employee_id), str(second.employee_id)})
        self.assertEqual(self.notices(rid, entity_type="interview_round"), {self.plain.id, second.id})
        self.assertNotIn("respond", iv["actions"])  # no accept/reject step

        # 3. only an assigned interviewer can Start / End -- no accept/reject
        self.call("POST", f"/interviews/{ivid}/start", as_=self.owner, expect=403)
        iv = self.call("POST", f"/interviews/{ivid}/start", as_=self.plain)
        self.assertEqual(iv["status"], "in_progress")
        self.call("POST", f"/interviews/{ivid}/end", as_=self.owner, expect=403)
        iv = self.call("POST", f"/interviews/{ivid}/end", {"notes": "Went well"}, as_=second)
        self.assertEqual(iv["status"], "ended")
        self.assertIn(self.hr.id, self.notices(ivid, title="Interview ended -- decision needed"))
        self.assertIn({"kind": "interview_round_ended", "entity_id": ivid},
                      [{"kind": x["kind"], "entity_id": x["entity_id"]} for x in self.call("GET", "/my-actions")])

        # 4. only the organizer records the internal decision; it drives the pipeline
        self.call("POST", f"/interviews/{ivid}/complete-round", {"decision": "advance"}, as_=self.plain, expect=403)
        iv = self.call("POST", f"/interviews/{ivid}/complete-round", {"decision": "advance", "comments": "Great fit"})
        self.assertEqual((iv["status"], iv["hr_decision"]), ("completed", "advance"))
        after = self.call("GET", f"/applications/{app_id}")
        self.assertEqual(after["stages"][0]["state"], "pass")
        self.assertEqual(after["stage_index"], 1)  # moved on to the next stage
        self.assertEqual(after["stages"][1]["state"], "current")
        actions = [h["action"] for h in after["history"]]
        for expected in ("Added to round -- " + r["name"], "Interview started", "Interview ended",
                         "Round completed -- advance", "Passed " + r["name"]):
            self.assertIn(expected, actions)

        # 5. a second candidate at the same round session shares it
        app2, _ = self.make_application(opening["id"], "Vikram")
        self.act(app2, "start_screening")
        self.act(app2, "shortlist")
        a2 = self.act(app2, "advance")
        iv2 = next(x for x in a2["interviews"] if x["round_id"] == rid)
        self.assertNotEqual(iv2["id"], ivid)
        self.assertEqual(iv2["scheduled_at"], iv["scheduled_at"])

        # 6. a cancelled round cancels every still-open candidate instance
        out = self.call("POST", f"/interview-rounds/{rid}/cancel", {"reason": "Panel unavailable"})
        self.assertEqual(out["status"], "cancelled")
        cancelled = next(x for x in self.call("GET", f"/applications/{app2}")["interviews"] if x["id"] == iv2["id"])
        self.assertEqual(cancelled["status"], "cancelled")

    def test_round_flow_emails_the_candidate_with_dedicated_templates(self):
        """Every candidate-facing step of a round uses its own email
        template: scheduled on attach, rescheduled on a time change,
        rejected / next-step only when HR leaves the email box ticked, and
        cancelled when the round is called off."""
        kinds: list[tuple[str, str]] = []
        real = email_service.render_email

        def spy(kind, ctx, **kw):
            kinds.append((kind, ctx.get("candidate_name") or ""))
            return real(kind, ctx, **kw)

        mock.patch.object(email_service, "render_email", spy).start()
        opening = self.make_opening("Mail Engineer")
        technical = self.call("GET", f"/job-openings/{opening['id']}")["interview_stages"][0]["key"]
        start = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)).replace(microsecond=0)
        r = self.call("POST", f"/job-openings/{opening['id']}/rounds",
                      {"round_key": technical, "scheduled_at": start.isoformat(), "mode": "video",
                       "meeting_link": "https://meet.example.com/mail",
                       "employee_ids": [str(self.plain.employee_id)]}, expect=201)

        def reach_round(name):
            app_id, _ = self.make_application(opening["id"], name)
            self.act(app_id, "start_screening")
            self.act(app_id, "shortlist")
            kinds.clear()
            a = self.act(app_id, "advance")
            return app_id, next(x for x in a["interviews"] if x["round_id"] == r["id"])["id"]

        # attached to a scheduled round -> "Interview Scheduled"
        app1, iv1 = reach_round("Meera")
        self.assertIn(("candidate_interview_scheduled", "Meera"), kinds)

        # internal reject with the (default-ticked) email box -> polite "Rejection"
        kinds.clear()
        self.call("POST", f"/interviews/{iv1}/start", as_=self.plain)
        self.call("POST", f"/interviews/{iv1}/end", {}, as_=self.plain)
        self.call("POST", f"/interviews/{iv1}/complete-round",
                  {"decision": "reject", "comments": "Weak on SQL -- internal only", "email_candidate": True})
        self.assertEqual([k for k, _ in kinds], ["candidate_rejected"])
        self.assertEqual(self.call("GET", f"/applications/{app1}")["status"], "rejected")

        # same, box unticked -> no email at all
        app2, iv2 = reach_round("Kiran")
        kinds.clear()
        self.call("POST", f"/interviews/{iv2}/start", as_=self.plain)
        self.call("POST", f"/interviews/{iv2}/end", {}, as_=self.plain)
        self.call("POST", f"/interviews/{iv2}/complete-round", {"decision": "reject", "email_candidate": False})
        self.assertEqual(kinds, [])

        # advance with the box ticked -> "Next Step"
        app3, iv3 = reach_round("Neha")
        kinds.clear()
        self.call("POST", f"/interviews/{iv3}/start", as_=self.plain)
        self.call("POST", f"/interviews/{iv3}/end", {}, as_=self.plain)
        self.call("POST", f"/interviews/{iv3}/complete-round", {"decision": "advance", "email_candidate": True})
        self.assertIn("candidate_next_step", [k for k, _ in kinds])

        # a still-scheduled candidate gets "Rescheduled" when the round moves,
        # then "Cancelled" when it is called off
        app4, _ = reach_round("Arjun")
        kinds.clear()
        self.call("PATCH", f"/interview-rounds/{r['id']}",
                  {"scheduled_at": (start + datetime.timedelta(days=1)).isoformat()})
        self.assertIn(("candidate_interview_rescheduled", "Arjun"), kinds)
        kinds.clear()
        self.call("POST", f"/interview-rounds/{r['id']}/cancel", {"reason": "Panel unavailable"})
        self.assertIn(("candidate_interview_cancelled", "Arjun"), kinds)

    def test_round_defined_in_the_stage_editor_at_opening_creation(self):
        """The panel + schedule can be set right in the Job Opening's stage
        editor (interview_stages[i].employee_ids/scheduled_at/...) instead of
        as a separate 'Define Round' step -- the round is materialized the
        moment the opening is saved, re-saving is idempotent (no duplicate
        round / no re-notify storm), a conflicting panel is rejected on the
        opening save itself, and the legacy Schedule/Invite actions disappear
        for a stage that already has a round (they stay for one that doesn't)."""
        others = [u for u in self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None),
                                                                       models.User.status == "active")).all()
                  if u.id not in (self.owner.id, self.hr.id, self.plain.id)]
        if not others:
            self.skipTest("needs a fourth employee user")
        second = others[0]
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)).replace(microsecond=0)
        stages = [
            {"key": "technical", "name": "Technical Round", "type": "interview", "mandatory": True,
             "feedback_required": True, "min_interviewers": 1, "scorecard": ["Technical skills"],
             "employee_ids": [str(self.plain.employee_id), str(second.employee_id)],
             "scheduled_at": future.isoformat(), "mode": "video", "meeting_link": "https://meet.example.com/auto"},
            {"key": "manager", "name": "Manager Round", "type": "interview", "mandatory": True,
             "feedback_required": True, "min_interviewers": 1, "scorecard": []},
        ]

        # 1. the round is created automatically -- no /job-openings/{id}/rounds POST here
        opening = self.make_opening("Auto Round Engineer", stages=stages)
        rounds = self.call("GET", f"/job-openings/{opening['id']}/rounds")["items"]
        technical = next(r for r in rounds if r["round_key"] == "technical")
        self.assertEqual(technical["status"], "scheduled")
        self.assertEqual({e["id"] for e in technical["interviewers"]}, {str(self.plain.employee_id), str(second.employee_id)})
        self.assertEqual(technical["meeting_link"], "https://meet.example.com/auto")
        self.assertEqual([r["round_key"] for r in rounds], ["technical"])  # manager stage has no panel/time -> no round

        # 2. re-saving the same stages is idempotent -- no duplicate / no 409
        self.call("PATCH", f"/job-openings/{opening['id']}", {"interview_stages": stages})
        rounds_again = self.call("GET", f"/job-openings/{opening['id']}/rounds")["items"]
        self.assertEqual(len(rounds_again), 1)
        self.assertEqual(rounds_again[0]["id"], technical["id"])

        # 3. a conflicting panel is rejected on the opening save itself
        clash_stages = [
            {"key": "technical", "name": "Technical Round", "type": "interview", "scorecard": []},
            {"key": "manager", "name": "Manager Round", "type": "interview", "scorecard": [],
             "employee_ids": [str(self.plain.employee_id)],
             "scheduled_at": (future + datetime.timedelta(minutes=30)).isoformat()},
        ]
        clash = self.call("POST", "/job-openings", {
            "title": "Clashing Opening", "department_id": str(self.dept.id), "branch_id": str(self.branch.id),
            "vacancies": 1, "employment_type": "Full-time", "work_mode": "Hybrid",
            "interview_stages": clash_stages}, expect=409)
        self.assertIn("already has the Technical Round", str(clash))

        # 4. candidate auto-attaches; legacy Schedule/Invite are gone for the
        # round-covered stage but still offered for the one without a round
        app_id, _ = self.make_application(opening["id"], "Round Editor Candidate")
        self.act(app_id, "start_screening")
        shortlisted = self.act(app_id, "shortlist")
        self.assertNotIn("schedule_interview", shortlisted["actions"])
        self.assertNotIn("invite_interviewers", shortlisted["actions"])
        self.assertIn("advance", shortlisted["actions"])
        entered = self.act(app_id, "advance")
        self.assertTrue(any(x["round_id"] == technical["id"] for x in entered["interviews"]))
        self.assertNotIn("schedule_interview", entered["actions"])
        self.assertNotIn("invite_interviewers", entered["actions"])
        # complete the technical round so the candidate reaches the manager
        # stage, which has no round defined -- the legacy actions return
        iv = next(x for x in entered["interviews"] if x["round_id"] == technical["id"])
        self.call("POST", f"/interviews/{iv['id']}/start", as_=self.plain)
        self.call("POST", f"/interviews/{iv['id']}/end", {}, as_=second)
        advanced = self.call("POST", f"/interviews/{iv['id']}/complete-round", {"decision": "advance"})
        self.assertEqual(advanced["status"], "completed")
        on_manager = self.call("GET", f"/applications/{app_id}")
        self.assertEqual(on_manager["stages"][1]["state"], "current")
        self.assertIn("schedule_interview", on_manager["actions"])
        self.assertIn("invite_interviewers", on_manager["actions"])

    def test_inbox_shows_only_requests_in_the_viewers_approval_path(self):
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        checked = 0
        for u in users:
            if crud.is_fallback_approver(self.db, u):
                continue
            subtree = {str(e) for e in crud._reporting_subtree_ids(self.db, u.employee_id, u.company_id)}
            me = str(u.employee_id)
            for key, rows in self.inbox(u).items():
                if key == "interview_invitations":
                    continue
                req_field = "requested_by" if key == "hiring_requisitions" else "employee_id"
                for r in rows or []:
                    related = (r.get("can_decide") or str(r.get(req_field)) == me or str(r.get(req_field)) in subtree
                               or me in (str(r.get("approver_id")), str(r.get("approved_by"))))
                    if not related:  # acted on a workflow step
                        related = self.db.scalar(
                            select(models.ApprovalAction.id)
                            .join(models.ApprovalRequest, models.ApprovalRequest.id == models.ApprovalAction.request_id)
                            .where(models.ApprovalRequest.document_id == uuid.UUID(r["id"]),
                                   models.ApprovalAction.actor_id == u.id).limit(1)) is not None
                    self.assertTrue(related, f"{key} row {r.get('id')} shown to unrelated {u.email}")
            checked += 1
            if checked >= 6:
                break
        if not checked:
            self.skipTest("no non-admin users")


if __name__ == "__main__":
    unittest.main()
