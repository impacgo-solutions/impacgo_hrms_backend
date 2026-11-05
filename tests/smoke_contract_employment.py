"""Live API smoke test for contract employment, against a REAL tenant on a
running backend -- it writes real rows, verifies them, then deletes
everything it created (in a `finally`, even when a check fails) and
confirms nothing is left.

Start the backend with email and the reminder scheduler off, then run:

    EMAIL_ENABLED=false REMINDERS_ENABLED=false venv/Scripts/python -m uvicorn app.main:app --port 8001
    venv/Scripts/python tests/smoke_contract_employment.py [--tenant impacgo-solutions] [--base-url http://127.0.0.1:8001]

Flow: Contract opening -> candidate -> select -> rate-based offer (no CTC)
-> approve -> send (no email) -> accept -> preboarding (details, documents,
verification) -> confirm joining -> create employee (end date + rate
copied) -> contract-end reminder (30-day milestone) -> leave refused for
the contractor -> renew (new end date + rate, history) -> convert to
permanent (history) -> cleanup.

Cleanup only ever deletes rows that reference an id this run created (fresh
random UUIDs), plus their uploaded files. The tenant's employee-number
series counter is NOT rewound (one number is consumed, harmless).
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import sys
import uuid
from pathlib import Path

import requests
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import crud, models, reminders, security  # noqa: E402
from app.database import engine  # noqa: E402
from app.deps import _OWNER_ROLE_NAME  # noqa: E402
from app.storage import uploads_root  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
MARK = "[SMOKE-CONTRACT]"
CREATED: dict[str, set] = {"ids": set(), "public_users": set(), "dirs": set()}


def track(*ids):
    for i in ids:
        if i:
            CREATED["ids"].add(str(i))


def tenant_session(slug):
    from sqlalchemy.orm import Session
    conn = engine.connect()
    conn.execute(text(f'SET search_path TO "{slug}", public'))
    conn.commit()
    return conn, Session(bind=conn)


def token_for(db, slug, user: models.User) -> str:
    pu = db.scalars(select(models.PublicUser).where(models.PublicUser.email == user.email)).first()
    tv = int(db.execute(text("SELECT token_version FROM public.users WHERE id = :i"), {"i": pu.id}).scalar() or 0)
    roles = [r.name for r in crud.get_user_roles(db, user.id)]
    return security.create_access_token(user_id=user.id, tenant_slug=slug, company_id=user.company_id,
                                        employee_id=user.employee_id, roles=roles, public_user_id=pu.id,
                                        token_version=tv, password_hash=pu.password_hash)


class Api:
    def __init__(self, base, token):
        self.base, self.h = base.rstrip("/"), {"Authorization": f"Bearer {token}"}

    def __call__(self, method, path, body=None, expect=(200, 201), files=None):
        r = requests.request(method, self.base + path, json=None if files else body, files=files,
                             headers=self.h, timeout=120)
        expect = (expect,) if isinstance(expect, int) else expect
        if r.status_code not in expect:
            raise AssertionError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r


CHECKS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        raise AssertionError(name)


STOP_AFTER_HIRE = False


def run(slug, base):
    conn, db = tenant_session(slug)
    try:
        users = db.scalars(select(models.User).where(models.User.employee_id.is_not(None),
                                                     models.User.status == "active")).all()
        owner = next(u for u in users if (r := crud.get_user_primary_role(db, u.id)) and r.name == _OWNER_ROLE_NAME)
        hr = next(u for u in users if u.id != owner.id and crud.effective_user_matrix(db, u).get("recruitment") in ("e", "a"))
        dept = db.scalars(select(models.Department).where(models.Department.company_id == owner.company_id)).first()
        branch = db.scalars(select(models.Branch).where(models.Branch.company_id == owner.company_id)).first()
        role_name = next(r.name for r in db.scalars(select(models.Role)).all()
                         if r.name != _OWNER_ROLE_NAME and ("IC" in r.name or r.name.startswith("Associate")))
        as_hr, as_owner = Api(base, token_for(db, slug, hr)), Api(base, token_for(db, slug, owner))
        # plain values -- the session closes below
        from types import SimpleNamespace as NS
        owner = NS(id=owner.id, employee_id=owner.employee_id, email=owner.email, company_id=owner.company_id)
        hr = NS(id=hr.id, employee_id=hr.employee_id, email=hr.email)
        dept, branch = NS(id=dept.id), NS(id=branch.id)
        db.rollback()
    finally:
        db.close()
        conn.close()

    today = datetime.date.today()
    end = today + datetime.timedelta(days=20)
    print(f"tenant {slug}: HR={hr.email}, Owner={owner.email}")

    # Phase 1 -- offer stage
    o = as_hr("POST", "/api/recruitment/job-openings", {
        "title": f"{MARK} Integration Contractor", "department_id": str(dept.id), "branch_id": str(branch.id),
        "vacancies": 1, "description": MARK, "employment_type": "Contract", "work_mode": "Remote",
        "reporting_manager_id": str(owner.employee_id), "hiring_team": [str(hr.employee_id)], "interview_stages": []})
    track(o["id"])
    as_hr("POST", f"/api/recruitment/job-openings/{o['id']}/publish")
    email = f"smoke.contract.{uuid.uuid4().hex[:8]}@example.com"
    c = as_hr("POST", "/api/recruitment/candidates", {"name": "Smoke Contractor", "email": email,
                                                      "phone": "9" + uuid.uuid4().int.__str__()[:9],
                                                      "source": "Job Board", "opening_id": o["id"]})
    app_id = c["application_id"]
    track(c["id"], app_id)
    for action in ("start_screening", "shortlist", "select"):
        as_hr("POST", f"/api/recruitment/applications/{app_id}/actions/{action}", {})
    r = requests.post(f"{base}/api/recruitment/offers", json={"application_id": app_id, "joining_date": today.isoformat()},
                      headers=as_hr.h, timeout=60)
    check("contract offer without rate terms is refused (422)", r.status_code == 422, r.text[:200])
    offer = as_hr("POST", "/api/recruitment/offers", {"application_id": app_id, "joining_date": today.isoformat(),
                                                      "rate_amount": 1800, "rate_unit": "daily",
                                                      "contract_end_date": end.isoformat(), "terms": MARK})
    track(offer["id"])
    check("offer is rate-based, no CTC needed",
          (offer["compensation_type"], offer["rate_unit"], offer["contract_end_date"]) == ("rate", "daily", end.isoformat()))
    as_hr("POST", f"/api/recruitment/offers/{offer['id']}/submit", {})
    as_owner("POST", f"/api/recruitment/offers/{offer['id']}/decision", {"decision": "approve"})
    as_hr("POST", f"/api/recruitment/offers/{offer['id']}/send", {"email": False})
    as_hr("POST", f"/api/recruitment/offers/{offer['id']}/accept", {"source": "email"})

    # Preboarding -> employee
    pb_id = as_hr("GET", f"/api/recruitment/applications/{app_id}")["preboarding"]["id"]
    track(pb_id)
    CREATED["dirs"].add(("preboarding", pb_id))
    as_hr("POST", f"/api/recruitment/preboarding/{pb_id}/send-tasks", {"email": False})
    as_hr("PUT", f"/api/recruitment/preboarding/{pb_id}/details", {
        "personal": {"first_name": "Smoke", "last_name": "Contractor", "gender": "Male", "date_of_birth": "1993-01-01"},
        "contact": {"personal_phone": "9876500031", "current_address": MARK},
        "emergency": {"name": "Smoke Kin", "relation": "Sibling", "phone": "9876500032"},
        "bank": {"bank_name": "HDFC", "account_no": "123456789012", "ifsc": "HDFC0001234", "account_holder": "Smoke Contractor"}})
    pb = as_hr("GET", f"/api/recruitment/preboarding/{pb_id}")
    for t in pb["tasks"]:
        track(t["id"])
    for t in [t for t in pb["tasks"] if t["task_type"] == "document" and t["required"]]:
        pb = as_hr("POST", f"/api/recruitment/preboarding/tasks/{t['id']}/documents",
                   files={"file": (f"{t['name']}.pdf", PDF, "application/pdf")})
        doc = next(x for x in pb["tasks"] if x["id"] == t["id"])["documents"][0]
        track(doc["id"])
        as_hr("POST", f"/api/recruitment/preboarding/documents/{doc['id']}/review", {"decision": "approve"})
    for t in as_hr("GET", f"/api/recruitment/preboarding/{pb_id}")["tasks"]:
        if t["task_type"] == "acknowledgement" and t["status"] != "completed":
            as_hr("PATCH", f"/api/recruitment/preboarding/tasks/{t['id']}", {"status": "completed"})
        if t["task_type"] == "verification" and t["required"]:
            as_hr("PATCH", f"/api/recruitment/preboarding/tasks/{t['id']}", {"status": "verified"})
    as_hr("POST", f"/api/recruitment/preboarding/{pb_id}/confirm-joining", {"joining_date": today.isoformat()})
    work_email = f"smoke.contract.{uuid.uuid4().hex[:6]}@impacgo.com"
    CREATED["public_users"].add(work_email)
    done = as_hr("POST", f"/api/recruitment/preboarding/{pb_id}/create-employee",
                 {"work_email": work_email, "role_name": role_name, "password": "Smoke@12345"})["result"]
    emp_id = done["employee_id"]
    track(emp_id)
    CREATED["dirs"].add(("employee_document", emp_id))

    # Phase 2 -- employee record
    conn, db = tenant_session(slug)
    try:
        emp = db.get(models.Employee, uuid.UUID(emp_id))
        user = db.scalars(select(models.User).where(models.User.employee_id == emp.id)).first()
        track(user.id if user else None)
        check("employee carries contract end date + rate",
              (emp.contract_end_date, float(emp.contract_rate_amount), emp.contract_rate_unit) == (end, 1800.0, "daily"),
              f"{emp.contract_end_date} {emp.contract_rate_amount} {emp.contract_rate_unit}")
        check("employee is a contract employee", crud.is_contract_employee(emp), emp.employment_type)
        contractor_api = Api(base, token_for(db, slug, user))
        # Phase 5 -- reminder (20 days left -> 30-day milestone), HR/Owner notified
        sent = reminders.contract_end_reminders(db, emp.company_id, today)
        db.commit()
        notes = db.scalars(select(models.Notification).where(models.Notification.entity_type == "contract_expiry",
                                                             models.Notification.entity_id == emp.id)).all()
        check("contract-end reminder sent to HR / Owner",
              sent == 1 and owner.id in {n.user_id for n in notes} and all("[30d·" in n.body for n in notes),
              f"sent={sent}, recipients={len(notes)}")
        check("reminder is sent once", reminders.contract_end_reminders(db, emp.company_id, today) == 0)
        db.commit()
        lt = next(lt for lt in db.scalars(select(models.LeaveType).where(models.LeaveType.company_id == emp.company_id)).all()
                  if lt.code.upper() != "LOP")
    finally:
        db.close()
        conn.close()

    if STOP_AFTER_HIRE:
        print(f"stopped after hire: employee {emp_id}")
        return
    # Phase 4 -- leave
    nxt = today + datetime.timedelta(days=3)
    r = requests.post(f"{base}/api/leave-requests", json={"employee_id": emp_id, "leave_type_name": lt.name,
                                                          "from_date": nxt.isoformat(), "to_date": nxt.isoformat(),
                                                          "days": 1, "reason": MARK}, headers=contractor_api.h, timeout=60)
    check(f"contractor can't take '{lt.name}' (409)", r.status_code == 409, r.text[:200])

    # Phase 5 -- renew + convert
    new_end = end + datetime.timedelta(days=180)
    ren = as_hr("POST", f"/api/employees/{emp_id}/contract/renew",
                {"new_end_date": new_end.isoformat(), "new_rate_amount": 2000, "notes": MARK})
    track(ren["event_id"])
    check("renewal moves the end date and rate",
          (ren["contract_end_date"], ren["contract_rate_amount"]) == (new_end.isoformat(), 2000.0))
    events = [e for e in as_hr("GET", "/api/employees/lifecycle-events?limit=500") if e["employee_id"] == emp_id]
    check("renewal is in the employee history", any(e["type"] == "Contract Renewal" for e in events))
    conv = as_hr("POST", f"/api/employees/{emp_id}/contract/convert-to-permanent", {"notes": MARK})
    track(conv["event_id"])
    check("conversion makes them permanent full-time",
          (conv["employment_type"], conv["contract_end_date"]) == ("full_time", None))
    events = [e for e in as_hr("GET", "/api/employees/lifecycle-events?limit=500") if e["employee_id"] == emp_id]
    check("conversion is in the employee history", any(e["type"] == "Converted to Permanent" for e in events))


def cleanup(slug, *, by_marker: bool = False):
    """Delete every row in the tenant schema (and public.users) that this
    run created: the tracked ids plus, transitively, every row that
    references one of them (history, audit logs, notifications, approval
    rows, documents ...) -- never a row they merely point AT. With
    by_marker, leftovers of an earlier run are found by MARK first.
    One transaction; nothing is deleted unless every reference is gone."""
    with engine.connect() as conn:
        tx = conn.begin()
        ids = set(CREATED["ids"])
        if by_marker:
            ids |= {str(i) for i in conn.execute(text(
                f'SELECT id FROM "{slug}".hcm_job_openings WHERE title LIKE :m OR description LIKE :m'),
                {"m": f"%{MARK}%"}).scalars()}
            ids |= {str(i) for i in conn.execute(text(
                f"SELECT id FROM \"{slug}\".hcm_candidates WHERE email LIKE 'smoke.contract.%@example.com'")).scalars()}
            CREATED["public_users"] |= set(conn.execute(text(
                "SELECT email FROM public.users WHERE email LIKE 'smoke.contract.%@impacgo.com'")).scalars())
            CREATED["public_users"] |= set(conn.execute(text(
                "SELECT email FROM public.admin_users WHERE email LIKE 'smoke.contract.%@impacgo.com'")).scalars())
        if not ids:
            tx.rollback()
            print("cleanup: nothing to remove")
            return
        cols = conn.execute(text(
            "SELECT c.table_name, c.column_name FROM information_schema.columns c "
            "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.table_schema = :s AND c.data_type = 'uuid' AND t.table_type = 'BASE TABLE'"), {"s": slug}).all()
        has_id = {t for t, c in cols if c == "id"}
        # Closure: rows referencing our ids are ours too.
        while True:
            grown = set()
            for table, column in cols:
                if table in has_id and column != "id":
                    grown |= {str(i) for i in conn.execute(text(
                        f'SELECT id FROM "{slug}"."{table}" WHERE "{column}"::text = ANY(:ids)'), {"ids": list(ids)}).scalars()}
            if grown <= ids:
                break
            ids |= grown
        ids = list(ids)
        removed = 0
        for _pass in range(15):
            progress = False
            for table, column in cols:
                sp = conn.begin_nested()
                try:
                    n = conn.execute(text(f'DELETE FROM "{slug}"."{table}" WHERE "{column}"::text = ANY(:ids)'),
                                     {"ids": ids}).rowcount
                    sp.commit()
                    if n:
                        removed += n
                        progress = True
                except Exception:
                    sp.rollback()
            if not progress:
                break
        for e in CREATED["public_users"]:
            removed += conn.execute(text("DELETE FROM public.users WHERE lower(email) = lower(:e)"), {"e": e}).rowcount
            # Creating an employee also mirrors the login into public.admin_users.
            removed += conn.execute(text("DELETE FROM public.admin_users WHERE lower(email) = lower(:e)"), {"e": e}).rowcount
        left = sum(conn.execute(text(f'SELECT count(*) FROM "{slug}"."{t}" WHERE "{c}"::text = ANY(:ids)'),
                                {"ids": ids}).scalar() for t, c in cols)
        left += sum(conn.execute(text("SELECT count(*) FROM public.users WHERE lower(email) = lower(:e)"), {"e": e}).scalar()
                    for e in CREATED["public_users"])
        left += sum(conn.execute(text("SELECT count(*) FROM public.admin_users WHERE lower(email) = lower(:e)"), {"e": e}).scalar()
                    for e in CREATED["public_users"])
        if left:
            tx.rollback()
            raise RuntimeError(f"cleanup incomplete: {left} rows still reference smoke ids -- nothing deleted")
        tx.commit()
    for kind, i in CREATED["dirs"]:
        d = uploads_root() / kind / str(i)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    if by_marker:  # upload folders of rows found by marker
        for kind in ("preboarding", "employee_document"):
            for i in ids:
                d = uploads_root() / kind / i
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
    print(f"cleanup: removed {removed} rows; 0 rows left referencing the {len(ids)} smoke ids")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="impacgo-solutions")
    ap.add_argument("--base-url", default="http://127.0.0.1:8001")
    ap.add_argument("--cleanup-only", action="store_true", help="remove leftovers of earlier runs (by marker)")
    ap.add_argument("--stop-after-hire", action="store_true",
                    help="create the contract employee and stop (no cleanup); ids saved to --state")
    ap.add_argument("--state", default=str(Path(__file__).with_name("smoke_contract_state.json")))
    a = ap.parse_args()
    import json
    if a.cleanup_only:
        if Path(a.state).exists():
            st = json.loads(Path(a.state).read_text())
            CREATED["ids"] |= set(st["ids"])
            CREATED["public_users"] |= set(st["public_users"])
            CREATED["dirs"] |= {tuple(d) for d in st["dirs"]}
        cleanup(a.tenant, by_marker=True)
        Path(a.state).unlink(missing_ok=True)
        return
    if a.stop_after_hire:
        global STOP_AFTER_HIRE
        STOP_AFTER_HIRE = True
        try:
            run(a.tenant, a.base_url)
        finally:
            Path(a.state).write_text(json.dumps({"ids": sorted(CREATED["ids"]), "public_users": sorted(CREATED["public_users"]),
                                                 "dirs": sorted(CREATED["dirs"])}))
            print(f"state saved to {a.state} -- run --cleanup-only afterwards")
        return
    failed = None
    try:
        run(a.tenant, a.base_url)
    except Exception as exc:  # report, then still clean up
        failed = exc
        print(f"SMOKE FAILED: {exc}")
    finally:
        cleanup(a.tenant, by_marker=True)
    print(f"{sum(ok for _, ok, _ in CHECKS)}/{len(CHECKS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
