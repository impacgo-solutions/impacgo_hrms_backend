"""Full & Final Settlement end to end: prepare (HR-entered lines) ->
approve (maker-checker) -> paid, reopen, validation, reference items, the
F&F Settlement Statement (template -> generate -> PDF) and permissions.

    cd backend && venv/Scripts/python -m unittest tests.test_fnf -v

Runs against the configured database inside a transaction that is ALWAYS
rolled back (tenant Infyq by default, FNF_TEST_TENANT to override);
generated PDFs go to a temporary folder. Nothing is left behind.
"""

from __future__ import annotations

import datetime
import os
import re
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import exit_letters as letters_api  # noqa: E402
from app.routers import fnf as api  # noqa: E402

TENANT = os.environ.get("FNF_TEST_TENANT", "Infyq")

LINES = [
    {"line_type": "earning", "component": "Salary for days worked", "description": "01 Aug 2026 – 31 Aug 2026",
     "amount": Decimal("45000.00")},
    {"line_type": "earning", "component": "Leave encashment", "description": "Earned Leave — 12 days",
     "amount": Decimal("12500.50")},
    {"line_type": "deduction", "component": "Loan / advance recovery", "description": "Salary advance",
     "amount": Decimal("10000.00")},
    {"line_type": "deduction", "component": "TDS (income tax)", "description": None, "amount": Decimal("3200.00")},
]


class FnfTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.tmp = tempfile.TemporaryDirectory()
        mock.patch("app.storage.uploads_root", lambda: Path(self.tmp.name)).start()
        self.owner = self._user(lambda role, u: role.name == _OWNER_ROLE_NAME)
        if self.owner is None:
            self.skipTest("no Owner user in this tenant")
        exits = [
            e for e in self.db.scalars(select(models.ExitRequestModel)).all()
            if self._company_ok(e)
        ]
        self.exit = next((e for e in exits if e.status == "completed"), None) or next(
            (e for e in exits if e.status in crud.FNF_EXIT_STATUSES), None)
        self.not_ready = next((e for e in exits if e.status in ("submitted", "sent_back", "pending")), None)
        if self.exit is None:
            self.skipTest("no approved / in-clearance / completed exit in this tenant")
        # Start from no settlement for this exit (rolled back afterwards).
        old = crud.get_final_settlement(self.db, self.exit.id)
        if old is not None:
            self.db.execute(delete(models.FinalSettlementLine).where(models.FinalSettlementLine.settlement_id == old.id))
            self.db.delete(old)
            self.db.flush()

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()
        self.tmp.cleanup()

    def _company_ok(self, e):
        emp = self.db.get(models.Employee, e.employee_id)
        return emp is not None and emp.company_id == self.owner.company_id

    def _user(self, pred):
        company_id = getattr(getattr(self, "owner", None), "company_id", None)
        for u in self.db.scalars(select(models.User)).all():
            if company_id is not None and u.company_id != company_id:
                continue
            role = crud.get_user_primary_role(self.db, u.id)
            if role is not None and pred(role, u):
                return u
        return None

    def _level(self, user, column="payroll_process"):
        return crud.effective_user_matrix(self.db, user).get(column)

    def _save(self, user=None, lines=LINES, notes="Settled as per policy."):
        return api.save_fnf(self.exit.id, schemas.FnfSaveRequest(lines=lines, notes=notes),
                            db=self.db, current_user=user or self.owner)

    # ── workflow ──────────────────────────────────────────────────────────
    def test_prepare_approve_pay_flow(self):
        out = api.get_fnf(self.exit.id, db=self.db, current_user=self.owner)
        self.assertEqual(out.fnf_status, "none")
        self.assertTrue(out.can_edit)
        self.assertFalse(out.can_approve)

        out = self._save()
        self.assertEqual(out.fnf_status, "draft")
        s = out.settlement
        self.assertEqual(s.payable_amount, 57500.50)
        self.assertEqual(s.recovery_amount, 13200.00)
        self.assertEqual(s.net_amount, 44300.50)
        self.assertEqual([l.component for l in s.lines], [l["component"] for l in LINES])  # order kept
        self.assertIsNotNone(s.prepared_by_name)

        # Saving again replaces the lines (no duplicates) and recomputes.
        out = self._save(lines=LINES[:1])
        self.assertEqual(len(out.settlement.lines), 1)
        self.assertEqual(out.settlement.net_amount, 45000.00)
        out = self._save()
        self.assertEqual(self.db.scalar(
            select(text("count(*)")).select_from(models.FinalSettlementLine)
            .where(models.FinalSettlementLine.settlement_id == out.settlement.id)), 4)

        self.assertTrue(out.can_approve)  # Owner may approve their own
        out = api.approve_fnf(self.exit.id, schemas.FnfApproveRequest(notes="Checked"),
                              db=self.db, current_user=self.owner)
        self.assertEqual(out.fnf_status, "approved")
        self.assertFalse(out.can_edit)
        self.assertTrue(out.can_mark_paid)
        with self.assertRaises(HTTPException) as ctx:  # approved = locked
            self._save()
        self.assertEqual(ctx.exception.status_code, 409)

        today = crud.company_today(self.db, self.owner.company_id)
        with self.assertRaises(HTTPException):  # future payment date
            api.mark_fnf_paid(self.exit.id, schemas.FnfMarkPaidRequest(
                payment_date=today + datetime.timedelta(days=1), payment_mode="Bank transfer"),
                db=self.db, current_user=self.owner)
        out = api.mark_fnf_paid(self.exit.id, schemas.FnfMarkPaidRequest(
            payment_date=today, payment_mode="Bank transfer", payment_reference="UTR123456"),
            db=self.db, current_user=self.owner)
        self.assertEqual(out.fnf_status, "paid")
        self.assertEqual(out.settlement.payment_reference, "UTR123456")
        self.assertFalse(out.can_reopen)
        with self.assertRaises(HTTPException):  # paid cannot be reopened
            api.reopen_fnf(self.exit.id, schemas.FnfReopenRequest(reason="Mistake"),
                           db=self.db, current_user=self.owner)
        with self.assertRaises(HTTPException):  # nor edited
            self._save()

    def test_reopen_returns_to_draft(self):
        self._save()
        api.approve_fnf(self.exit.id, schemas.FnfApproveRequest(), db=self.db, current_user=self.owner)
        out = api.reopen_fnf(self.exit.id, schemas.FnfReopenRequest(reason="TDS recalculated"),
                             db=self.db, current_user=self.owner)
        self.assertEqual(out.fnf_status, "draft")
        self.assertIsNone(out.settlement.approved_at)
        self.assertIn("TDS recalculated", out.settlement.approval_notes)
        self.assertTrue(out.can_edit)

    def test_negative_net_and_validation(self):
        out = self._save(lines=[
            {"line_type": "earning", "component": "Salary for days worked", "amount": Decimal("5000")},
            {"line_type": "deduction", "component": "Notice period shortfall recovery", "amount": Decimal("20000")},
        ])
        self.assertEqual(out.settlement.net_amount, -15000.00)  # recoverable from the employee
        for bad in ({"line_type": "bonus", "component": "X", "amount": 1},
                    {"line_type": "earning", "component": "", "amount": 1},
                    {"line_type": "earning", "component": "X", "amount": 0},
                    {"line_type": "earning", "component": "X", "amount": -5},
                    {"line_type": "earning", "component": "X", "amount": Decimal("1.234")}):
            with self.assertRaises(ValidationError):
                schemas.FnfLineIn(**bad)
        with self.assertRaises(ValidationError):
            schemas.FnfSaveRequest(lines=[LINES[0]] * 61)
        with self.assertRaises(HTTPException):  # approve without lines
            self._save(lines=[])
            api.approve_fnf(self.exit.id, schemas.FnfApproveRequest(), db=self.db, current_user=self.owner)
        if self.not_ready is not None:
            with self.assertRaises(HTTPException) as ctx:  # exit not approved yet
                api.save_fnf(self.not_ready.id, schemas.FnfSaveRequest(lines=LINES),
                             db=self.db, current_user=self.owner)
            self.assertEqual(ctx.exception.status_code, 409)

    def test_maker_checker(self):
        preparer = self._user(lambda role, u: role.name != _OWNER_ROLE_NAME and self._level(u) == "a")
        if preparer is None:
            self.skipTest("no non-Owner Payroll (Process) Admin in this tenant")
        out = self._save(user=preparer)
        self.assertFalse(out.can_approve)
        self.assertIn("another payroll approver", out.approve_blocked_reason)
        with self.assertRaises(HTTPException) as ctx:
            api.approve_fnf(self.exit.id, schemas.FnfApproveRequest(), db=self.db, current_user=preparer)
        self.assertEqual(ctx.exception.status_code, 409)
        out = api.approve_fnf(self.exit.id, schemas.FnfApproveRequest(), db=self.db, current_user=self.owner)
        self.assertEqual(out.fnf_status, "approved")

    def test_permissions(self):
        outsider = self._user(lambda role, u: role.name != _OWNER_ROLE_NAME and self._level(u) not in ("e", "a"))
        if outsider is not None:
            with self.assertRaises(HTTPException) as ctx:
                api._require_payroll(outsider, self.db)
            self.assertEqual(ctx.exception.status_code, 403)
        editor = self._user(lambda role, u: role.name != _OWNER_ROLE_NAME and self._level(u) == "e")
        if editor is not None:
            self._save(user=editor)
            with self.assertRaises(HTTPException) as ctx:  # Edit may prepare, not approve
                api.approve_fnf(self.exit.id, schemas.FnfApproveRequest(), db=self.db, current_user=editor)
            self.assertEqual(ctx.exception.status_code, 403)

    def test_statement_needs_payroll_access(self):
        """HR-documents users without Payroll (Process) access (e.g.
        recruitment only) can't preview, generate or read F&F statements."""
        from app.deps import HR_DOCUMENT_COLUMNS, has_column_access
        hr_only = self._user(lambda role, u: role.name != _OWNER_ROLE_NAME
                             and has_column_access(self.db, u, *HR_DOCUMENT_COLUMNS)
                             and self._level(u) not in ("e", "a"))
        if hr_only is None:
            self.skipTest("no HR-documents user without payroll access in this tenant")
        with self.assertRaises(HTTPException) as ctx:
            letters_api.preview_exit_letter(self.exit.id, letter_type="fnf_statement",
                                            db=self.db, current_user=hr_only)
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(HTTPException) as ctx:
            letters_api.generate_exit_letter(self.exit.id, schemas.ExitLetterGenerateRequest(),
                                             letter_type="fnf_statement", db=self.db, current_user=hr_only)
        self.assertEqual(ctx.exception.status_code, 403)
        letter = models.ExitLetter(letter_type="fnf_statement", company_id=hr_only.company_id,
                                   employee_id=self.exit.employee_id)
        self.assertFalse(letters_api._can_read_letter(self.db, hr_only, letter))

    # ── reference items ───────────────────────────────────────────────────
    def test_reference_items_are_real(self):
        detail = crud.get_exit_letter_detail(self.db, self.owner.company_id, self.exit.id)
        items = crud.fnf_reference_items(self.db, self.owner.company_id, detail)
        categories = {i["category"] for i in items}
        self.assertIn("gratuity", categories)  # always evaluated when DOJ + LWD exist
        for item in items:
            self.assertIn(item["severity"], ("info", "action"))
            if item["suggested_amount"] is not None:
                self.assertGreater(item["suggested_amount"], 0)
                self.assertTrue(item["basis"])  # every suggested figure says how
            if item["suggested_line_type"]:
                self.assertIn(item["suggested_component"],
                              crud.FNF_EARNING_COMPONENTS + crud.FNF_DEDUCTION_COMPONENTS)
        loans = self.db.scalars(select(models.Loan).where(
            models.Loan.employee_id == self.exit.employee_id, models.Loan.status == "active",
            models.Loan.outstanding_balance > 0)).all()
        self.assertEqual(sum(1 for i in items if i["category"] == "loan"), len(loans))

    def test_completed_service(self):
        d = datetime.date
        self.assertEqual(crud._completed_service(d(2021, 4, 1), d(2026, 3, 31)), (5, 0, 0))
        self.assertEqual(crud._completed_service(d(2022, 5, 8), d(2026, 8, 31)), (4, 3, 24))
        self.assertEqual(crud._completed_service(d(2020, 1, 15), d(2026, 1, 14)), (6, 0, 0))

    # ── statement ─────────────────────────────────────────────────────────
    def test_statement_generation(self):
        dart = (Path(__file__).resolve().parents[2] / "lib" / "models" / "exit_letter_models.dart").read_text(encoding="utf-8")
        html = re.search(r"kFnfStatementStarterHtml = r'''(.*?)''';", dart, re.S).group(1)
        css = re.search(r"kFnfStatementStarterCss = r'''(.*?)''';", dart, re.S).group(1)
        letters_api.save_exit_letter_template(
            schemas.ExitLetterTemplateUpdate(name="Standard F&F Statement", html_body=html, css_styles=css,
                                             signatory_name="Priya Sharma", signatory_designation="Head - HR"),
            letter_type="fnf_statement", db=self.db, current_user=self.owner)

        self._save()
        with self.assertRaises(HTTPException) as ctx:  # draft -> not eligible
            letters_api.generate_exit_letter(self.exit.id, schemas.ExitLetterGenerateRequest(),
                                             letter_type="fnf_statement", db=self.db, current_user=self.owner)
        self.assertEqual(ctx.exception.status_code, 409)
        summary = letters_api.get_exit_request_letters(self.exit.id, db=self.db, current_user=self.owner)
        self.assertFalse(summary.eligibility["fnf_statement"]["eligible"])

        api.approve_fnf(self.exit.id, schemas.FnfApproveRequest(), db=self.db, current_user=self.owner)
        preview = letters_api.preview_exit_letter(self.exit.id, letter_type="fnf_statement",
                                                  db=self.db, current_user=self.owner).rendered_html
        self.assertNotIn("{{", preview)
        for value in ("Salary for days worked", "Earned Leave — 12 days", "₹57,500.50", "₹13,200.00",
                      "₹44,300.50", "Rupees Forty Four Thousand Three Hundred and Fifty Paise Only",
                      "Net amount payable to the employee", "Priya Sharma"):
            self.assertIn(value, preview)
        self.assertNotIn("&lt;tr&gt;", preview)  # rows inserted as HTML, not escaped text

        letter = letters_api.generate_exit_letter(self.exit.id, schemas.ExitLetterGenerateRequest(),
                                                  letter_type="fnf_statement", db=self.db, current_user=self.owner)
        self.assertTrue(letter.letter_number.startswith("FNF/"))
        self.assertEqual(letter.letter_title, "Full & Final Settlement Statement")
        stored = self.db.get(models.ExitLetter, letter.id)
        self.assertTrue(crud.exit_letter_file_bytes(stored).startswith(b"%PDF"))

        # Paid: the statement carries the payment details on regeneration.
        api.mark_fnf_paid(self.exit.id, schemas.FnfMarkPaidRequest(
            payment_date=crud.company_today(self.db, self.owner.company_id), payment_mode="Bank transfer",
            payment_reference="UTR998877"), db=self.db, current_user=self.owner)
        again = letters_api.generate_exit_letter(self.exit.id, schemas.ExitLetterGenerateRequest(),
                                                 letter_type="fnf_statement", db=self.db, current_user=self.owner)
        self.assertEqual(again.version, 2)
        self.assertIn("UTR998877", self.db.get(models.ExitLetter, again.id).rendered_html)
        self.assertNotIn("UTR998877", self.db.get(models.ExitLetter, letter.id).rendered_html)  # v1 unchanged

    def test_legacy_settlement_without_lines(self):
        """Older rows (totals only, 'posted'/'paid') read correctly but cannot
        issue a statement without a breakdown."""
        legacy = models.FinalSettlement(id=__import__("uuid").uuid4(), exit_id=self.exit.id, payable_amount=1000,
                                        recovery_amount=0, net_amount=1000, status="posted")
        self.db.add(legacy)
        self.db.flush()
        self.assertEqual(crud.fnf_status(legacy), "approved")
        ok, reason = crud.fnf_statement_eligibility(self.db, self.exit)
        self.assertFalse(ok)
        self.assertIn("itemised breakdown", reason)


if __name__ == "__main__":
    unittest.main()
