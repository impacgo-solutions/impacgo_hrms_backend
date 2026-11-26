"""People > Transfers & Promotions: the list's From / To show the values each
record actually saved -- branch / business-unit / sub-department transfers and
manager changes used to come back as "—" -> "—".

    cd backend && venv/Scripts/python -m unittest tests.test_lifecycle_from_to -v

The first class needs no database. The second runs against the configured
database inside a transaction that is ALWAYS rolled back (tenant
impacgo-solutions by default, LIFECYCLE_TEST_TENANT to override).
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, models  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import employees as employees_api  # noqa: E402

TENANT = os.environ.get("LIFECYCLE_TEST_TENANT", "impacgo-solutions")
_REFS = ("designation", "department", "sub_department", "branch", "business_unit")


def _event(**kw):
    """A lifecycle row with every from_/to_ field empty unless given. Pass
    org units as names (from_branch="A"); their ids are derived."""
    values = {}
    for ref in _REFS:
        for side in ("from", "to"):
            name = kw.pop(f"{side}_{ref}", None)
            values[f"{side}_{ref}"] = SimpleNamespace(name=name) if name else None
            values[f"{side}_{ref}_id"] = uuid.uuid5(uuid.NAMESPACE_DNS, name) if name else None
    for field in ("reporting_manager_id", "dotted_line_manager_id", "ctc"):
        for side in ("from", "to"):
            values[f"{side}_{field}"] = kw.pop(f"{side}_{field}", None)
    assert not kw, kw
    return SimpleNamespace(**values)


class FromToTextTests(unittest.TestCase):
    def test_branch_transfer_shows_both_branches(self):
        e = _event(from_branch="Visakhapatnam Branch", to_branch="Hyderabad Branch")
        self.assertEqual(crud.lifecycle_event_from_to(e, {}), ("Visakhapatnam Branch", "Hyderabad Branch"))

    def test_business_unit_and_sub_department_transfers(self):
        self.assertEqual(crud.lifecycle_event_from_to(_event(from_business_unit="Retail", to_business_unit="SaaS"), {}),
                         ("Retail", "SaaS"))
        self.assertEqual(crud.lifecycle_event_from_to(_event(from_sub_department="QA", to_sub_department="Dev"), {}),
                         ("QA", "Dev"))

    def test_unchanged_fields_are_left_out(self):
        # A branch transfer that carried the (unchanged) department along.
        e = _event(from_branch="A", to_branch="B", from_department="IT", to_department="IT")
        self.assertEqual(crud.lifecycle_event_from_to(e, {}), ("A", "B"))

    def test_first_assignment_and_removal_read_none(self):
        self.assertEqual(crud.lifecycle_event_from_to(_event(to_branch="B"), {}), ("None", "B"))
        self.assertEqual(crud.lifecycle_event_from_to(_event(from_branch="A"), {}), ("A", "None"))

    def test_manager_change_names_the_manager_and_which_one(self):
        old, new, same = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        names = {old: "Soumya T", new: "Srikar T", same: "Asha R"}
        e = _event(from_reporting_manager_id=same, to_reporting_manager_id=same,
                   from_dotted_line_manager_id=old, to_dotted_line_manager_id=new)
        self.assertEqual(crud.lifecycle_event_from_to(e, names),
                         ("Second Reporting Manager: Soumya T", "Second Reporting Manager: Srikar T"))
        e = _event(from_reporting_manager_id=old, to_reporting_manager_id=new)
        self.assertEqual(crud.lifecycle_event_from_to(e, names),
                         ("Reporting Manager: Soumya T", "Reporting Manager: Srikar T"))

    def test_manager_no_longer_on_the_roster(self):
        e = _event(from_reporting_manager_id=uuid.uuid4(), to_reporting_manager_id=None)
        self.assertEqual(crud.lifecycle_event_from_to(e, {}),
                         ("Reporting Manager: Former employee", "Reporting Manager: None"))

    def test_several_changes_in_one_record_are_listed_with_labels(self):
        old, new = uuid.uuid4(), uuid.uuid4()
        e = _event(from_branch="A", to_branch="B", from_reporting_manager_id=old, to_reporting_manager_id=new)
        self.assertEqual(crud.lifecycle_event_from_to(e, {old: "Ravi", new: "Priya"}),
                         ("Branch: A; Reporting Manager: Ravi", "Branch: B; Reporting Manager: Priya"))

    def test_ctc_only_promotion_shows_the_two_amounts(self):
        e = _event(from_designation="Engineer", to_designation="Engineer", from_ctc=850000, to_ctc=1000000)
        self.assertEqual(crud.lifecycle_event_from_to(e, {}), ("₹8,50,000", "₹10,00,000"))

    def test_record_whose_values_never_differ_still_shows_them(self):
        e = _event(from_designation="Engineer", to_designation="Engineer")
        self.assertEqual(crud.lifecycle_event_from_to(e, {}), ("Engineer", "Engineer"))

    def test_empty_record(self):
        self.assertEqual(crud.lifecycle_event_from_to(_event(), {}), ("—", "—"))


class ListEndpointTests(unittest.TestCase):
    """GET /api/employees/lifecycle-events end to end, for records written the
    way the app writes them (crud.record_employee_change)."""

    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        self.owner = next((u for u in users if (r := crud.get_user_primary_role(self.db, u.id)) is not None
                           and r.name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("no Owner")
        self.emps = self.db.scalars(select(models.Employee).where(
            models.Employee.company_id == self.owner.company_id, models.Employee.is_active.is_(True))).all()
        self.branches = self.db.scalars(select(models.Branch).where(
            models.Branch.company_id == self.owner.company_id)).all()
        if len(self.emps) < 3 or len(self.branches) < 2:
            self.skipTest("needs 3 employees and 2 branches")

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def _row(self, event):
        rows = employees_api.list_lifecycle_events(limit=500, offset=0, db=self.db, current_user=self.owner)
        return next(r for r in rows if r.id == event.id)

    def test_branch_transfer_and_manager_change_show_their_saved_values(self):
        emp, mgr_a, mgr_b = self.emps[0], self.emps[1], self.emps[2]
        a, b = self.branches[0], self.branches[1]
        transfer = crud.record_employee_change(self.db, emp, {"branch_id": a.id}, {"branch_id": b.id})
        row = self._row(transfer)
        self.assertEqual((row.type, row.from_value, row.to_value), ("Transfer", a.name, b.name))

        change = crud.record_employee_change(self.db, emp, {"reporting_manager_id": mgr_a.id},
                                             {"reporting_manager_id": mgr_b.id})
        row = self._row(change)
        self.assertEqual(row.type, "Reporting Change")
        self.assertEqual(row.from_value, f"Reporting Manager: {crud._full_name(mgr_a)}")
        self.assertEqual(row.to_value, f"Reporting Manager: {crud._full_name(mgr_b)}")

        # The detail (View / Edit) carries the same saved values.
        detail = employees_api.get_lifecycle_event(event_id=transfer.id, db=self.db, current_user=self.owner)
        self.assertEqual((detail["from_branch"]["name"], detail["to_branch"]["name"]), (a.name, b.name))

    def test_no_row_in_the_list_is_blank_when_it_saved_a_change(self):
        emp = self.emps[0]
        crud.record_employee_change(self.db, emp, {"branch_id": self.branches[0].id},
                                    {"branch_id": self.branches[1].id})
        for row in employees_api.list_lifecycle_events(limit=500, offset=0, db=self.db, current_user=self.owner):
            self.assertFalse(row.from_value == row.to_value == "—", f"{row.type} {row.id} shows no From/To")


if __name__ == "__main__":
    unittest.main()
