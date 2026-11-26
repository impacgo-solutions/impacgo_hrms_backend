"""Leave > Holiday Calendar > Upload Document end to end with real
documents (PDF table, DOCX table, XLSX with date cells, CSV, image-only PDF):
upload -> stored file -> extraction -> review (edit / add / remove) ->
confirm -> holidays on the calendar; duplicates (existing / in file)
skipped, different branch allowed; permissions; failed + cancelled
uploads; no double import.

    cd backend && venv/Scripts/python -m unittest tests.test_holiday_imports -v

Runs inside a transaction that is ALWAYS rolled back (tenant
impacgo-solutions by default, HOLIDAY_TEST_TENANT to override); stored
documents go to a temporary folder.
"""

from __future__ import annotations

import datetime
import io
import os
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException, UploadFile  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models, schemas  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import attendance as attendance_api  # noqa: E402
from app.routers import holiday_imports as api  # noqa: E402

TENANT = os.environ.get("HOLIDAY_TEST_TENANT", "impacgo-solutions")
YEAR = 2031  # a year with no holidays yet


def pdf_table(rows: list[list[str]], title: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table
    from reportlab.lib.styles import getSampleStyleSheet
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4).build([Paragraph(title, getSampleStyleSheet()["Title"]),
                                               Table(rows, colWidths=[40, 90, 80, 150, 140])])
    return buf.getvalue()


def blank_pdf() -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(50, 50, 200, 200, fill=1)  # an "image" -- no text layer
    c.save()
    return buf.getvalue()


def docx_table(rows: list[list[str]]) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    cells = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r) + "</w:tr>"
                    for r in rows)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>'
           f'<w:p><w:r><w:t>Holiday List {YEAR}</w:t></w:r></w:p><w:tbl>{cells}</w:tbl></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def xlsx_sheet(rows: list[list]) -> bytes:
    """Strings -> shared strings; datetime.date -> a real date-formatted serial."""
    strings: list[str] = []
    x = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    body = []
    for ri, row in enumerate(rows, start=1):
        cells = []
        for ci, v in enumerate(row):
            ref = f"{chr(65 + ci)}{ri}"
            if isinstance(v, datetime.date):
                serial = (v - datetime.date(1899, 12, 30)).days
                cells.append(f'<c r="{ref}" s="1"><v>{serial}</v></c>')
            else:
                strings.append(str(v))
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        body.append(f'<row r="{ri}">{"".join(cells)}</row>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{x}">' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/styles.xml", f'<styleSheet xmlns="{x}"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{x}"><sheetData>{"".join(body)}</sheetData></worksheet>')
    return buf.getvalue()


class HolidayImportTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.tmp = tempfile.TemporaryDirectory()
        mock.patch("app.storage.uploads_root", lambda: Path(self.tmp.name)).start()
        mock.patch("app.routers.holiday_imports.uploads_root", lambda: Path(self.tmp.name)).start()
        users = self.db.scalars(select(models.User)).all()
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                           and r.name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("no Owner")
        self.company_id = self.owner.company_id
        self.branch = self.db.scalars(select(models.Branch).where(models.Branch.company_id == self.company_id)).first()
        if self.branch is None:
            self.skipTest("no branch")
        self.branch_label = self.branch.name.replace(" Branch", "")

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()
        self.tmp.cleanup()

    def upload(self, content: bytes, filename: str, user=None):
        return api.upload_holiday_document(UploadFile(io.BytesIO(content), filename=filename),
                                           db=self.db, current_user=user or self.owner)

    def confirm(self, out, rows=None):
        rows = rows if rows is not None else [
            schemas.HolidayImportConfirmRow(date=r.date, name=r.name, branch_id=r.branch_id, is_optional=r.is_optional)
            for r in out.rows]
        return api.confirm_holiday_import(out.id, schemas.HolidayImportConfirmIn(rows=rows),
                                          db=self.db, current_user=self.owner)

    def calendar(self):
        return [h for h in attendance_api.list_holidays(db=self.db, current_user=self.owner)
                if h.date.startswith(str(YEAR))]

    # ── formats ────────────────────────────────────────────────────────
    def test_pdf_table_full_flow(self):
        content = pdf_table([
            ["S.No", "Date", "Day", "Holiday", "Applicable Branch"],
            ["1", f"01-01-{YEAR}", "Wednesday", "New Year", "All"],
            ["2", f"14 Jan {YEAR}", "Tuesday", "Sankranthi", self.branch_label],
            ["3", f"26/01/{YEAR}", "Sunday", "Republic Day", "All Branches"],
            ["4", f"15-08-{YEAR}", "Friday", "Independence Day", "All"],
        ], f"Holiday Calendar {YEAR}")
        out = self.upload(content, "holidays.pdf")
        self.assertEqual(out.status, "extracted")
        got = [(r.date.isoformat(), r.day, r.name, r.branch_name) for r in out.rows]
        self.assertEqual(got, [
            (f"{YEAR}-01-01", "Wednesday", "New Year", "All Branches"),
            (f"{YEAR}-01-14", "Tuesday", "Sankranthi", self.branch.name),
            (f"{YEAR}-01-26", "Sunday", "Republic Day", "All Branches"),
            (f"{YEAR}-08-15", "Friday", "Independence Day", "All Branches"),
        ])
        imp = self.db.get(models.HolidayImport, out.id)
        self.assertTrue((Path(self.tmp.name) / imp.file_url.removeprefix("/media/")).exists())  # original stored
        self.assertEqual(api.download_holiday_document(out.id, db=self.db, current_user=self.owner).body, content)

        # Review: rename one, drop one, add one.
        rows = [schemas.HolidayImportConfirmRow(date=r.date, name=r.name, branch_id=r.branch_id) for r in out.rows]
        rows[0].name = "New Year's Day"
        rows.pop(3)
        rows.append(schemas.HolidayImportConfirmRow(date=datetime.date(YEAR, 10, 2), name="Gandhi Jayanthi"))
        done = self.confirm(out, rows)
        self.assertEqual((done.status, done.imported_count, done.skipped_count), ("imported", 4, 0))
        cal = self.calendar()
        self.assertEqual([(h.date, h.day, h.name, h.region) for h in cal], [
            (f"{YEAR}-01-01", "Wednesday", "New Year's Day", "All Branches"),
            (f"{YEAR}-01-14", "Tuesday", "Sankranthi", self.branch.name),
            (f"{YEAR}-01-26", "Sunday", "Republic Day", "All Branches"),
            (f"{YEAR}-10-02", "Thursday", "Gandhi Jayanthi", "All Branches"),
        ])
        self.assertTrue(all(h.imported for h in cal))
        with self.assertRaises(HTTPException) as ctx:  # never imported twice
            self.confirm(out, rows)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_docx_xlsx_csv(self):
        docx = self.upload(docx_table([["Date", "Holiday", "Location"],
                                       [f"March 19, {YEAR}", "Ugadi", self.branch_label],
                                       [f"{YEAR}-11-08", "Deepavali (Optional)", "All"]]), "list.docx")
        self.assertEqual([(r.date.isoformat(), r.name, r.branch_name, r.is_optional) for r in docx.rows], [
            (f"{YEAR}-03-19", "Ugadi", self.branch.name, False),
            (f"{YEAR}-11-08", "Deepavali", "All Branches", True)])
        xlsx = self.upload(xlsx_sheet([["Date", "Day", "Occasion"],
                                       [datetime.date(YEAR, 12, 25), "Thursday", "Christmas"]]), "cal.xlsx")
        self.assertEqual([(r.date, r.name) for r in xlsx.rows], [(datetime.date(YEAR, 12, 25), "Christmas")])
        csv = self.upload(f"Holiday,Date\nGood Friday,18/04/{YEAR}\nHoli,{YEAR}-03-10\n".encode(), "h.csv")
        self.assertEqual(sorted((r.date.isoformat(), r.name) for r in csv.rows),
                         [(f"{YEAR}-03-10", "Holi"), (f"{YEAR}-04-18", "Good Friday")])

    # ── duplicates / applicability ─────────────────────────────────────
    def test_duplicates_skipped_different_branch_allowed(self):
        crud.create_holiday(self.db, self.company_id, datetime.date(YEAR, 5, 1), "May Day")  # all branches
        csv = (f"Date,Holiday,Branch\n01-05-{YEAR},may  day,All\n"          # same holiday -> duplicate
               f"01-05-{YEAR},May Day,{self.branch_label}\n"                # other applicability -> allowed
               f"02-05-{YEAR},Founders Day,All\n02-05-{YEAR},Founders Day,All\n")  # repeated in file
        out = self.upload(csv.encode(), "dups.csv")
        flags = {(r.date.isoformat(), r.branch_name): r.duplicate for r in out.rows}
        self.assertTrue(flags[(f"{YEAR}-05-01", "All Branches")])
        self.assertFalse(flags[(f"{YEAR}-05-01", self.branch.name)])
        self.assertEqual(len(out.rows), 3)  # the in-file repeat is collapsed at extraction
        rows = [schemas.HolidayImportConfirmRow(date=r.date, name=r.name, branch_id=r.branch_id) for r in out.rows]
        rows.append(rows[-1].model_copy())  # HR adds the same row twice
        done = self.confirm(out, rows)
        self.assertEqual((done.imported_count, done.skipped_count), (2, 2))
        reasons = sorted(s["reason"] for s in done.result["skipped"])
        self.assertEqual(reasons, ["Already on the holiday calendar", "Listed twice in this upload"])
        # Add Holiday follows the same rule now.
        with self.assertRaises(ValueError):
            crud.create_holiday(self.db, self.company_id, datetime.date(YEAR, 5, 2), "founders DAY")
        crud.create_holiday(self.db, self.company_id, datetime.date(YEAR, 5, 2), "Founders Day", branch_id=self.branch.id)

    def test_unknown_branch_and_wrong_weekday_are_flagged(self):
        out = self.upload(f"Date,Day,Holiday,Branch\n{YEAR}-06-10,Monday,Local Fair,Atlantis\n".encode(), "w.csv")
        r = out.rows[0]
        self.assertEqual(r.branch_name, "All Branches")
        self.assertTrue(any("Branch not found: Atlantis" in w for w in r.warnings))
        self.assertTrue(any("is a Tuesday" in w for w in r.warnings))  # 10 Jun 2031 is a Tuesday
        with self.assertRaises(HTTPException):  # a branch from another company is refused
            self.confirm(out, [schemas.HolidayImportConfirmRow(date=r.date, name=r.name, branch_id=uuid.uuid4())])

    # ── failures / permissions ─────────────────────────────────────────
    def test_image_only_pdf_fails_but_is_recorded(self):
        with self.assertRaises(HTTPException) as ctx:
            self.upload(blank_pdf(), "scan.pdf")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("scanned", ctx.exception.detail["message"])
        imp = self.db.get(models.HolidayImport, uuid.UUID(ctx.exception.detail["import_id"]))
        self.assertEqual(imp.status, "failed")
        for bad, name in ((b"hello", "x.exe"), (b"not a pdf", "fake.pdf"), (b"", "e.csv")):
            with self.assertRaises(HTTPException):
                self.upload(bad, name)

    def test_cancel_and_permission(self):
        out = self.upload(f"Date,Holiday\n{YEAR}-07-07,Test Day\n".encode(), "c.csv")
        self.assertEqual(api.cancel_holiday_import(out.id, db=self.db, current_user=self.owner).status, "cancelled")
        with self.assertRaises(HTTPException):
            self.confirm(out)
        self.assertEqual(self.calendar(), [])
        employee = next((u for u in self.db.scalars(select(models.User).where(models.User.company_id == self.company_id))
                         if (r := crud.get_user_primary_role(self.db, u.id)) is not None and r.name != _OWNER_ROLE_NAME
                         and crud.effective_user_matrix(self.db, u).get("leave_approval") not in ("e", "a")), None)
        if employee is not None:
            with self.assertRaises(HTTPException) as ctx:
                api._require_holiday_admin(employee, self.db)
            self.assertEqual(ctx.exception.status_code, 403)
        history = api.list_holiday_imports(limit=20, db=self.db, current_user=self.owner)
        self.assertIn(out.id, [h.id for h in history])


if __name__ == "__main__":
    unittest.main()
