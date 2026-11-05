"""Form Configuration x Performance (goal level, review cycle status),
Recruitment (candidate source), Learning (course type) and Assets (type,
status) plus custom fields on goal / review_cycle / candidate / course /
asset.

    cd backend && venv/Scripts/python -m unittest tests.test_form_config_perf_recruit_learning_assets -v

Router / service functions are called directly in a transaction that is
ALWAYS rolled back.
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import crud, database, form_config as fc, models, recruitment_workflow as rw, schemas  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.routers import assets as assets_api  # noqa: E402
from app.routers import form_config as cfg_api  # noqa: E402
from app.routers import learning as learning_api  # noqa: E402
from app.routers import performance as perf_api  # noqa: E402

TENANT = os.environ.get("TEST_TENANT", "impacgo-solutions")


class FormConfigPerfRecruitLearningAssetsTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        users = self.db.scalars(select(models.User).where(models.User.employee_id.is_not(None))).all()
        self.owner = next((u for u in users
                           if (crud.get_user_primary_role(self.db, u.id) or models.Role()).name == _OWNER_ROLE_NAME), None)
        if self.owner is None:
            self.skipTest("tenant needs an Owner user")
        self.admin = self.owner
        self.company_id = self.owner.company_id
        self.n = 0

    def tearDown(self):
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    # ── helpers ──
    def cfg(self, fn, *a, **k):
        return fn(*a, db=self.db, current_user=self.admin, **k)

    def add_option(self, field_key, label):
        return self.cfg(cfg_api.create_option, cfg_api.OptionCreate(field_key=field_key, label=label))

    def option_row(self, field_key, value):
        return next(r for r in fc._rows(self.db, self.company_id)[field_key] if r.value == value)

    def hide(self, field_key, value):
        self.cfg(cfg_api.update_option, self.option_row(field_key, value).id, cfg_api.OptionUpdate(is_active=False))

    def make_field(self, form, label, ftype, **kw):
        return self.cfg(cfg_api.create_custom_field,
                        cfg_api.CustomFieldCreate(form_key=form, label=label, field_type=ftype, **kw))

    def code(self, fn, *a, **k):
        with self.assertRaises(HTTPException) as ctx:
            fn(*a, **k)
        return ctx.exception.status_code

    def custom_suite(self, form, create, read_all, entity_id_of):
        """Shared checks of a form's custom fields: required / type errors,
        persistence, list read-back, hidden field, tenant scoping."""
        note = self.make_field(form, "Ref Note", "text", is_required=True)
        self.make_field(form, "Budget", "number")
        self.make_field(form, "Due On", "date")
        self.make_field(form, "Tier", "dropdown", options=["Gold", "Silver"])
        self.make_field(form, "Urgent", "checkbox")
        self.make_field(form, "Remarks", "textarea")
        # required missing -> 422; wrong types -> 422; unknown key -> 422
        self.assertEqual(self.code(create, {}), 422)
        self.assertEqual(self.code(create, {"ref_note": "x", "budget": "abc"}), 422)
        self.assertEqual(self.code(create, {"ref_note": "x", "due_on": "31/12/2026"}), 422)
        self.assertEqual(self.code(create, {"ref_note": "x", "tier": "Bronze"}), 422)
        self.assertEqual(self.code(create, {"ref_note": "x", "urgent": "maybe"}), 422)
        self.assertEqual(self.code(create, {"ref_note": "x", "ghost": "1"}), 422)
        created = create({"ref_note": " R-1 ", "budget": "12.50", "due_on": "2027-01-31", "tier": "silver",
                          "urgent": True, "remarks": "multi\nline"})
        expected = {"ref_note": "R-1", "budget": "12.5", "due_on": "2027-01-31", "tier": "Silver",
                    "urgent": "true", "remarks": "multi\nline"}
        self.assertEqual(created["custom_fields"], expected)
        listed = next(r for r in read_all() if entity_id_of(r) == entity_id_of(created))
        self.assertEqual(listed["custom_fields"], expected)
        # tenant scoping: another company has none of these fields / values
        other = uuid.uuid4()
        self.assertEqual(self.code(fc.validate_custom_values, self.db, other, form, {"ref_note": "x"}), 422)
        self.assertEqual(fc.load_custom_values(self.db, other, form, [entity_id_of(created)]), {})
        # a hidden field no longer shows nor accepts values
        self.cfg(cfg_api.update_custom_field, note.id, cfg_api.CustomFieldUpdate(is_active=False))
        self.assertEqual(self.code(create, {"ref_note": "x"}), 422)
        again = next(r for r in read_all() if entity_id_of(r) == entity_id_of(created))
        self.assertNotIn("ref_note", again["custom_fields"])
        return created

    # ── goal ──
    def goal_create(self, level="Company", custom=None):
        self.n += 1
        out = perf_api.create_okr(
            schemas.OkrCreate(level=level, title=f"Goal {self.n}", owner="Someone", custom_fields=custom),
            db=self.db, current_user=self.admin)
        return out.model_dump()

    def test_goal_level_options(self):
        self.assertEqual(self.goal_create("company")["level"], "Company")
        # logic-bearing: admins can't add new levels, only rename / hide
        self.assertEqual(self.code(self.add_option, "goal.level", "Team"), 409)
        self.assertEqual(self.code(self.goal_create, "Team"), 422)
        self.cfg(cfg_api.update_option, self.option_row("goal.level", "Department").id,
                 cfg_api.OptionUpdate(label="Dept Goal"))
        d = self.goal_create("Department")
        self.assertEqual((d["level"], d["level_label"]), ("Department", "Dept Goal"))
        self.hide("goal.level", "Department")
        self.assertEqual(self.code(self.goal_create, "Department"), 422)
        # the stored legacy row still lists with its label
        row = next(o for o in perf_api.list_okrs(db=self.db, current_user=self.admin, limit=500, offset=0)
                   if str(o.id) == str(d["id"]))
        self.assertEqual(row.level, "Department")

    def test_goal_custom_fields(self):
        self.custom_suite(
            "goal",
            lambda c: self.goal_create("Company", c),
            lambda: [o.model_dump() for o in perf_api.list_okrs(db=self.db, current_user=self.admin, limit=500, offset=0)],
            lambda r: r["id"])

    # ── review cycle ──
    def cycle_create(self, status="scheduled", custom=None):
        self.n += 1
        out = perf_api.create_review_cycle(
            schemas.ReviewCycleCreate(
                name=f"Cycle {uuid.uuid4().hex[:8]}", from_date=datetime.date(2027, 1, 1),
                to_date=datetime.date(2027, 6, 30), status=status, custom_fields=custom),
            db=self.db, current_user=self.admin)
        return out.model_dump()

    def test_review_cycle_status_options(self):
        self.assertEqual(self.cycle_create("in_progress")["status"], "In Progress")
        self.assertEqual(self.code(self.add_option, "review_cycle.status", "Paused"), 409)
        self.assertEqual(self.code(self.cycle_create, "paused"), 422)
        self.cfg(cfg_api.update_option, self.option_row("review_cycle.status", "scheduled").id,
                 cfg_api.OptionUpdate(label="Upcoming"))
        c = self.cycle_create("scheduled")
        self.assertEqual((c["status"], c["status_key"]), ("Upcoming", "scheduled"))
        self.assertIn("Upcoming", [x.status for x in perf_api.list_review_cycles(db=self.db, current_user=self.admin)])
        self.hide("review_cycle.status", "completed")
        self.assertEqual(self.code(self.cycle_create, "completed"), 422)

    def test_review_cycle_custom_fields(self):
        self.custom_suite(
            "review_cycle",
            lambda c: self.cycle_create("scheduled", c),
            lambda: [r.model_dump() for r in perf_api.list_review_cycles(db=self.db, current_user=self.admin)],
            lambda r: r["id"])

    # ── course ──
    def course_create(self, ctype="Internal", custom=None):
        self.n += 1
        out = learning_api.create_course(
            schemas.CourseCreate(name=f"Course {uuid.uuid4().hex[:8]}", type=ctype, duration="4 hrs",
                                 category="Eng", custom_fields=custom),
            db=self.db, current_user=self.admin)
        return out.model_dump()

    def course_list(self):
        return [c.model_dump() for c in learning_api.list_courses(db=self.db, current_user=self.admin)]

    def test_course_type_options(self):
        self.assertEqual(self.course_create("internal")["type"], "Internal")
        self.assertEqual(self.code(self.course_create, "Blended"), 422)
        self.add_option("course.type", "Blended Learning Programme For Leaders")
        added = self.course_create("blended learning programme for leaders")
        self.assertEqual(added["type"], "Blended Learning Programme For Leaders")
        listed = next(c for c in self.course_list() if c["id"] == added["id"])
        self.assertEqual(listed["type"], "Blended Learning Programme For Leaders")
        # rename shows in the list; hide rejects new use but legacy rows still list
        self.cfg(cfg_api.update_option, self.option_row("course.type", "External").id,
                 cfg_api.OptionUpdate(label="Vendor"))
        ext = self.course_create("External")
        self.assertEqual(next(c for c in self.course_list() if c["id"] == ext["id"])["type"], "Vendor")
        self.hide("course.type", "External")
        self.assertEqual(self.code(self.course_create, "External"), 422)
        self.assertEqual(next(c for c in self.course_list() if c["id"] == ext["id"])["type"], "Vendor")
        # legacy lower-case stored value ('mandatory') still resolves to its label
        legacy = models.TrainingCourse(id=uuid.uuid4(), company_id=self.company_id, title="Legacy",
                                       course_type="mandatory", category="x", duration_label="1 hr")
        self.db.add(legacy)
        self.db.flush()
        self.assertEqual(next(c for c in self.course_list() if c["id"] == legacy.id)["type"], "Mandatory")

    def test_course_custom_fields(self):
        self.custom_suite("course", lambda c: self.course_create("Internal", c), self.course_list, lambda r: r["id"])

    # ── asset ──
    def asset_create(self, atype="Laptop", status="available", custom=None):
        self.n += 1
        out = assets_api.create_asset_inventory_item(
            schemas.AssetInventoryItemCreate(
                tag=f"T-{uuid.uuid4().hex[:8]}", type=atype, model="M1", status=status, value="45000",
                purchased="2025-01-01", custom_fields=custom),
            db=self.db, current_user=self.admin)
        return out.model_dump()

    def asset_list(self):
        return [a.model_dump() for a in assets_api.list_asset_inventory(
            db=self.db, current_user=self.admin, limit=500, offset=0)]

    def test_asset_type_options(self):
        self.assertEqual(self.asset_create("laptop")["type"], "Laptop")
        # types the seeded inventory uses are part of the built-in defaults
        self.assertTrue({"Mobile Device", "SIM Card", "ID Card"} <= {v for v, _ in fc.FIELDS["asset.type"].defaults})
        self.assertEqual(self.code(self.asset_create, "Drone"), 422)
        self.add_option("asset.type", "Drone")
        self.assertEqual(self.asset_create("drone")["type"], "Drone")
        self.hide("asset.type", "Drone")
        self.assertEqual(self.code(self.asset_create, "Drone"), 422)
        # legacy free-text types keep listing
        legacy = models.AssetInventoryItem(id=uuid.uuid4(), company_id=self.company_id, asset_tag="LEG-1",
                                           asset_type="Projector", model="x", status="available")
        self.db.add(legacy)
        self.db.flush()
        self.assertEqual(next(a for a in self.asset_list() if a["id"] == legacy.id)["type"], "Projector")

    def test_asset_status_options(self):
        a = self.asset_create(status="In Repair")            # older clients send the label
        self.assertEqual((a["status"], a["status_key"]), ("In Repair", "in_repair"))
        self.assertEqual(self.asset_create(status="available")["status"], "Available")
        self.assertEqual(self.code(self.add_option, "asset.status", "Lost"), 409)
        self.assertEqual(self.code(self.asset_create, status="lost"), 422)
        self.cfg(cfg_api.update_option, self.option_row("asset.status", "in_repair").id,
                 cfg_api.OptionUpdate(label="Under Repair"))
        b = self.asset_create(status="in_repair")
        self.assertEqual((b["status"], b["status_key"]), ("Under Repair", "in_repair"))
        self.assertEqual(next(x for x in self.asset_list() if x["id"] == b["id"])["status"], "Under Repair")
        # hidden status can't be picked, but an asset already in it can be re-saved
        self.hide("asset.status", "in_repair")
        self.assertEqual(self.code(self.asset_create, status="in_repair"), 422)
        out = assets_api.update_asset_inventory_status(
            b["id"], schemas.AssetInventoryItemStatusUpdate(status="in_repair"), db=self.db, current_user=self.admin)
        self.assertEqual(out.status_key, "in_repair")
        out = assets_api.update_asset_inventory_status(
            b["id"], schemas.AssetInventoryItemStatusUpdate(status="Available"), db=self.db, current_user=self.admin)
        self.assertEqual(out.status, "Available")
        self.assertEqual(self.code(assets_api.update_asset_inventory_status, b["id"],
                                   schemas.AssetInventoryItemStatusUpdate(status="in_repair"),
                                   db=self.db, current_user=self.admin), 422)
        # legacy 'repair' key shows as the configured In Repair label
        legacy = models.AssetInventoryItem(id=uuid.uuid4(), company_id=self.company_id, asset_tag="LEG-2",
                                           asset_type="Laptop", model="x", status="repair")
        self.db.add(legacy)
        self.db.flush()
        self.assertEqual(next(x for x in self.asset_list() if x["id"] == legacy.id)["status"], "Under Repair")

    def test_asset_custom_fields(self):
        created = self.custom_suite(
            "asset", lambda c: self.asset_create("Laptop", "available", c), self.asset_list, lambda r: r["id"])
        # status PATCH response keeps the values; delete removes them
        out = assets_api.update_asset_inventory_status(
            created["id"], schemas.AssetInventoryItemStatusUpdate(status="Assigned"),
            db=self.db, current_user=self.admin)
        self.assertEqual(out.custom_fields["budget"], "12.5")
        assets_api.delete_asset_inventory_item(created["id"], db=self.db, current_user=self.admin)
        self.assertEqual(fc.load_custom_values(self.db, self.company_id, "asset", [created["id"]]), {})

    # ── candidate ──
    def ctx(self):
        return rw.make_ctx(self.db, self.admin)

    def cand_create(self, source="Job Board", custom=None):
        self.n += 1
        c = rw.create_candidate(self.ctx(), {
            "name": f"Cand {self.n}", "email": f"c{uuid.uuid4().hex[:8]}@example.com", "source": source,
            "custom_fields": custom})
        return rw.candidate_out(self.ctx(), c, detail=True)

    def test_candidate_source_options_and_filter_list(self):
        self.assertEqual(self.cand_create("job board")["source"], "Job Board")
        self.assertEqual(self.code(self.cand_create, "Billboard"), 422)
        self.add_option("candidate.source", "Billboard")
        out = self.cand_create("Billboard")
        self.assertEqual(out["source"], "Billboard")
        # the Recruitment settings list (pipeline "All sources" filter) follows the configuration
        self.assertIn("Billboard", rw.get_settings(self.db, self.company_id)["sources"])
        self.cfg(cfg_api.update_option, self.option_row("candidate.source", "Agency").id,
                 cfg_api.OptionUpdate(label="Staffing Agency"))
        self.assertEqual(self.cand_create("Agency")["source_label"], "Staffing Agency")
        # hide: new use rejected, an unchanged legacy value still saves on edit
        self.hide("candidate.source", "Billboard")
        self.assertIn("Billboard", rw.search_candidates(self.ctx(), out["name"])["items"][0]["source"])
        self.assertNotIn("Billboard", rw.get_settings(self.db, self.company_id)["sources"])
        self.assertEqual(self.code(self.cand_create, "Billboard"), 422)
        c = rw.get_candidate(self.ctx(), out["id"])
        rw.update_candidate(self.ctx(), c, {"source": "Billboard", "notes": "kept legacy source"})
        self.assertEqual(c.notes, "kept legacy source")
        self.assertEqual(self.code(rw.update_candidate, self.ctx(), c, {"source": "Nowhere"}), 422)
        rw.update_candidate(self.ctx(), c, {"source": "Recruiter"})
        self.assertEqual(c.source, "Recruiter")

    def test_recruitment_settings_sources_edit_syncs_form_config(self):
        rw.save_settings(self.ctx(), {"sources": ["Recruiter", "Podcast"]})
        values = fc.option_values(self.db, self.company_id, "candidate.source")
        self.assertEqual(values, ["Recruiter", "Podcast"])
        self.assertEqual(self.cand_create("Podcast")["source"], "Podcast")
        self.assertEqual(self.code(self.cand_create, "Agency"), 422)      # hidden by the sync

    def test_candidate_custom_fields(self):
        created = self.custom_suite(
            "candidate", lambda c: self.cand_create("Recruiter", c),
            lambda: rw.search_candidates(self.ctx(), "Cand ", limit=100)["items"], lambda r: r["id"])
        # edit: partial update touches only the keys sent; the rest persist
        self.make_field("candidate", "Shoe Size", "number")
        c = rw.get_candidate(self.ctx(), created["id"])
        rw.update_candidate(self.ctx(), c, {"custom_fields": {"shoe_size": "9"}})
        out = rw.candidate_out(self.ctx(), c, detail=True)
        self.assertEqual(out["custom_fields"]["shoe_size"], "9")
        self.assertEqual(out["custom_fields"]["budget"], "12.5")
        rw.update_candidate(self.ctx(), c, {"custom_fields": {"shoe_size": ""}})
        self.assertNotIn("shoe_size", rw.candidate_out(self.ctx(), c, detail=True)["custom_fields"])
        self.assertEqual(self.code(rw.update_candidate, self.ctx(), c, {"custom_fields": {"shoe_size": "abc"}}), 422)


if __name__ == "__main__":
    unittest.main()
