"""Reports include ALL history -- records migrated from Frappe HR (attendance
source "import", leave "Imported from Frappe HR ...") and WorkTrack (work
entries / timesheets) alongside records created in HRMS -- with no loss and
no double counting, overall and employee-, department- and project-wise.

Every report figure is checked against a direct SQL count/sum of the same
tables. Read-only: runs in a transaction that is ALWAYS rolled back
(tenant impacgo-solutions by default, REPORTS_TEST_TENANT to override).

    cd backend && venv/Scripts/python -m unittest tests.test_reports_migrated_history -v
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models  # noqa: E402
from app import report_helpers as rh  # noqa: E402
from app.routers import reports as R  # noqa: E402

TENANT = os.environ.get("REPORTS_TEST_TENANT", "impacgo-solutions")
ALL_FROM, ALL_TO = datetime.date(2000, 1, 1), datetime.date(2026, 12, 31)


def _acc(t):
    return crud.ReportAccess(scope="org", employee_ids=None, report_types=(t,))


class MigratedHistoryReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.owner = next((u for u in self.db.scalars(select(models.User).where(models.User.status == "active"))
                           if crud.is_fallback_approver(self.db, u)), None)
        if self.owner is None:
            self.skipTest("no org-wide user")
        self.cid = self.owner.company_id
        if not self.sql("select count(*) from hcm_attendance_records where source = 'import'")[0][0]:
            self.skipTest("tenant has no migrated attendance")

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def sql(self, q, **k):
        return self.db.execute(text(q), {"c": self.cid, **k}).all()

    # ── attendance ──────────────────────────────────────────────────────
    def attendance(self, f=ALL_FROM, t=ALL_TO, **kw):
        args = dict(branch_id=None, department_id=None, designation_id=None, manager_id=None, employee_id=None)
        args.update(kw)
        return R.attendance_report(f, t, compare_previous=False, db=self.db, current_user=self.owner,
                                   access=_acc("attendance"), **args)

    def test_attendance_counts_every_record_imported_and_new(self):
        rep = self.attendance()
        col = rep.columns.index
        got = {k: sum(int(r[col(k)]) for r in rep.rows) for k in ("Present", "Absent", "Late", "On Leave")}
        raw = dict(self.sql("""select case when a.status in ('present','half_day','regularized','early_in','off_shift')
                 then 'Present' when a.status='absent' then 'Absent' when a.status='late' then 'Late'
                 when a.status='on_leave' then 'On Leave' else a.status end, count(*)
            from hcm_attendance_records a join core_employees e on e.id=a.employee_id
            where e.company_id=:c and attendance_date between :f and :t group by 1""", f=ALL_FROM, t=ALL_TO))
        self.assertEqual(got, {k: raw.get(k, 0) for k in got})
        self.assertEqual(sum(raw.values()), sum(got.values()), f"statuses outside every column: {raw}")
        hours = self.sql("""select coalesce(sum(work_hours),0) from hcm_attendance_records a
            join core_employees e on e.id=a.employee_id where e.company_id=:c""")[0][0]
        self.assertAlmostEqual(float(rep.summary["Total Hours"]), float(hours), delta=0.06)
        self.assertEqual(rep.summary["Leave Days"], str(got["On Leave"]))
        donut = {p.label: p.value for p in rep.charts[0].series[0].points}
        self.assertEqual(donut["On Leave"], got["On Leave"])

    def test_attendance_employee_and_department_wise_include_imported_days(self):
        emp_rows = self.sql("""select a.employee_id, count(*) from hcm_attendance_records a
            where a.source='import' group by 1 order by 2 desc limit 3""")
        for emp_id, _n in emp_rows:
            rep = self.attendance(employee_id=[emp_id])
            col = rep.columns.index
            shown = sum(int(rep.rows[0][col(k)]) for k in ("Present", "Absent", "Late", "On Leave"))
            raw = self.sql("select count(*) from hcm_attendance_records where employee_id=:e", e=emp_id)[0][0]
            self.assertEqual(shown, raw)
        for dept_id, raw in self.sql("""select e.department_id, count(*) from hcm_attendance_records a
                join core_employees e on e.id=a.employee_id where e.company_id=:c and e.department_id is not null
                group by 1"""):
            rep = self.attendance(department_id=dept_id)
            col = rep.columns.index
            self.assertEqual(sum(int(r[col(k)]) for r in rep.rows for k in ("Present", "Absent", "Late", "On Leave")),
                             raw)

    def test_attendance_date_ranges_split_history_exactly(self):
        lo, hi = self.sql("select min(attendance_date), max(attendance_date) from hcm_attendance_records "
                          "where source='import'")[0]
        mid = lo + (hi - lo) / 2
        col = None
        total = 0
        for f, t in ((ALL_FROM, mid), (mid + datetime.timedelta(days=1), ALL_TO)):
            rep = self.attendance(f, t)
            col = rep.columns.index
            total += sum(int(r[col(k)]) for r in rep.rows for k in ("Present", "Absent", "Late", "On Leave"))
        raw = self.sql("""select count(*) from hcm_attendance_records a join core_employees e on e.id=a.employee_id
            where e.company_id=:c""")[0][0]
        self.assertEqual(total, raw, "two halves add up to the whole: no loss or overlap at the boundary")

    # ── leave ───────────────────────────────────────────────────────────
    def test_leave_report_lists_migrated_and_new_requests_once(self):
        rep = R.leave_report(ALL_FROM, ALL_TO, None, None, None, None, None, None, False, db=self.db,
                             current_user=self.owner, access=_acc("leave"))
        raw = self.sql("""select count(*), coalesce(sum(l.days) filter (where l.status='approved'),0),
                   count(*) filter (where l.reason like 'Imported from Frappe%')
            from hcm_leave_requests l join core_employees e on e.id=l.employee_id where e.company_id=:c""")[0]
        self.assertGreater(raw[2], 0, "migrated leave present")
        self.assertEqual(len(rep.rows), raw[0])
        self.assertEqual(rep.summary["Total Requests"], str(raw[0]))
        self.assertAlmostEqual(float(rep.summary["Approved Days"]), float(raw[1]), delta=0.06)

    # ── work entries / timesheets / utilization / project-wise ─────────
    def raw_work(self, extra="", **k):
        return self.sql(f"""select count(*), coalesce(sum(w.hours),0) from pm_time_entries w
            join pm_timesheets t on t.id=w.timesheet_id join core_employees e on e.id=t.employee_id
            where e.company_id=:c {extra}""", **k)[0]

    def test_work_hours_reports_cover_all_entries(self):
        n, hours = self.raw_work()
        everything = self.sql("select count(*), coalesce(sum(hours),0) from pm_time_entries")[0]
        self.assertEqual((n, hours), tuple(everything), "every entry reachable via its timesheet")
        we = R.work_entry_report(ALL_FROM, ALL_TO, None, None, None, None, None, None, None, False, db=self.db,
                                 current_user=self.owner, access=_acc("work-entry"))
        self.assertEqual(len(we.rows), n)
        self.assertAlmostEqual(float(we.summary["Total Hours"]), float(hours), delta=0.06)
        ts = R.timesheet_report(ALL_FROM, ALL_TO, None, db=self.db, current_user=self.owner,
                                access=_acc("timesheet"), payroll_scope="org")
        self.assertAlmostEqual(float(ts.summary["Total Hours"]), float(hours), delta=0.06)
        self.assertAlmostEqual(sum(float(r[ts.columns.index("Total Hrs")]) for r in ts.rows), float(hours), delta=0.5)
        ut = R.utilization_report(ALL_FROM, ALL_TO, None, db=self.db, current_user=self.owner,
                                  access=_acc("utilization"), payroll_scope="org")
        self.assertAlmostEqual(float(ut.summary["Total Logged Hours"]), float(hours), delta=0.06)

    def test_work_entry_project_and_employee_wise(self):
        for pid, n, hours in self.sql("""select w.project_id, count(*), sum(w.hours) from pm_time_entries w
                group by 1 order by 2 desc limit 4"""):
            rep = R.work_entry_report(ALL_FROM, ALL_TO, None, None, None, None, None, pid, None, False, db=self.db,
                                      current_user=self.owner, access=_acc("work-entry"))
            self.assertEqual(len(rep.rows), n)
            self.assertAlmostEqual(float(rep.summary["Total Hours"]), float(hours), delta=0.06)
        for emp_id, n in self.sql("""select t.employee_id, count(*) from pm_time_entries w
                join pm_timesheets t on t.id=w.timesheet_id group by 1 order by 2 desc limit 3"""):
            rep = R.work_entry_report(ALL_FROM, ALL_TO, None, None, None, None, [emp_id], None, None, False,
                                      db=self.db, current_user=self.owner, access=_acc("work-entry"))
            self.assertEqual(len(rep.rows), n)

    def test_oldest_migrated_month_is_reported(self):
        first = self.sql("select min(entry_date) from pm_time_entries")[0][0]
        f, t = first.replace(day=1), (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - \
            datetime.timedelta(days=1)
        n, hours = self.raw_work("and w.entry_date between :f and :t", f=f, t=t)
        rep = R.work_entry_report(f, t, None, None, None, None, None, None, None, False, db=self.db,
                                  current_user=self.owner, access=_acc("work-entry"))
        self.assertEqual((len(rep.rows), Decimal(rep.summary["Total Hours"])), (n, Decimal(f"{hours:.1f}")))

    # ── "All Time" range charts ─────────────────────────────────────────
    def test_all_time_trend_starts_at_first_data_month(self):
        pts = rh.bucket_daily_values({datetime.date(2025, 12, 3): 2.0, datetime.date(2026, 2, 1): 1.0},
                                     datetime.date(2000, 1, 1), datetime.date(2026, 3, 31))
        self.assertEqual([p.label for p in pts], ["Dec 2025", "Jan 2026", "Feb 2026", "Mar 2026"])
        self.assertEqual([p.value for p in pts], [2.0, 0.0, 1.0, 0.0])
        short = rh.bucket_daily_values({}, datetime.date(2026, 1, 1), datetime.date(2026, 6, 30))
        self.assertEqual(len(short), 6, "normal ranges unchanged")


if __name__ == "__main__":
    unittest.main()
